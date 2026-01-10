import uuid, asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from urllib.parse import unquote

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------- QUESTIONS ----------------
QUESTIONS = [
    {
        "q": "What is FastAPI?",
        "options": [
            {"id": "A", "text": "Framework"},
            {"id": "B", "text": "DB"},
            {"id": "C", "text": "OS"},
            {"id": "D", "text": "Browser"},
        ],
        "answer": "A"
    },
    {
        "q": "WebSocket is for?",
        "options": [
            {"id": "A", "text": "Mail"},
            {"id": "B", "text": "Realtime"},
            {"id": "C", "text": "Storage"},
            {"id": "D", "text": "Auth"},
        ],
        "answer": "B"
    }
]

QUESTION_TIME = 15
BASE_SCORE = 1000

games = {}

# ---------------- CREATE GAME ----------------
@app.post("/create-game")
async def create_game():
    pin = str(uuid.uuid4())[:6].upper()
    games[pin] = {
        "question_index": -1,
        "started": False,
        "locked": True,
        "question_start": None,
        "players": {},
        "answered": set(),
        "answers_count": {}
    }
    return {"pin": pin}

# ---------------- CHECK PIN ----------------
@app.get("/check-pin/{pin}")
def check_pin(pin: str):
    game = games.get(pin)
    if not game:
        return {"valid": False}
    return {"valid": True, "started": game["started"]}

# ---------------- NEXT QUESTION ----------------
@app.post("/next/{pin}")
async def next_question(pin: str):
    game = games.get(pin)
    if not game:
        raise HTTPException(404)

    game["started"] = True
    game["question_index"] += 1

    if game["question_index"] >= len(QUESTIONS):
        await send_leaderboard(pin, finished=True)
        return {"status": "finished"}

    q = QUESTIONS[game["question_index"]]

    game["locked"] = False
    game["answered"] = set()
    game["answers_count"] = {o["id"]: 0 for o in q["options"]}
    game["question_start"] = asyncio.get_event_loop().time()

    for p in game["players"].values():
        if p["ws"]:
            await p["ws"].send_json({
                "type": "question",
                "q": q["q"],
                "options": q["options"],
                "time": QUESTION_TIME
            })

    asyncio.create_task(lock_question(pin, game["question_index"]))
    return {"status": "sent"}

# ---------------- LOCK QUESTION ----------------
async def lock_question(pin: str, q_index: int):
    await asyncio.sleep(QUESTION_TIME)

    game = games.get(pin)
    if not game or game["question_index"] != q_index:
        return

    game["locked"] = True
    correct_id = QUESTIONS[q_index]["answer"]

    for p in game["players"].values():
        if p["ws"]:
            await p["ws"].send_json({
                "type": "lock",
                "correct_id": correct_id
            })

    # 🔥 Notify host only
    await notify_question_end(pin)



OPTION_COLOR_MAP = {
    "A": "blue",
    "B": "yellow",
    "C": "red",
    "D": "green",
}

COLOR_OPTION_MAP = {v: k for k, v in OPTION_COLOR_MAP.items()}

# ---------------- WEBSOCKET ----------------
@app.websocket("/ws/{pin}/{player_id}/{name}")
async def player_ws(ws: WebSocket, pin: str, player_id: str, name: str):
    print("🟢 WS CONNECT ATTEMPT:", pin, player_id, name)
    name = unquote(name)

    if pin not in games:
        await ws.close(code=1008)
        return

    game = games[pin]

    role = "player"
    if player_id in ("HOST", "SCREEN"):
        role = player_id

    await ws.accept()
    print("✅ WS CONNECTED:", pin, player_id)
    game["players"][player_id] = {
        "name": name,
        "score": game["players"].get(player_id, {}).get("score", 0),
        "ws": ws,
        "role": role
    }


    await broadcast_players(pin)

    try:
        while True:
            data = await ws.receive_json()
            print("RAW WS DATA:", data)

            if role != "player":
                continue

            if data.get("type") != "answer":
                continue

            if game["locked"] or player_id in game["answered"]:
                continue

            selected_color = data.get("option")   # "blue", "yellow", etc.
            selected_id = COLOR_OPTION_MAP.get(selected_color)

            if not selected_id:
                continue   # safety

            # ✅ INCREMENT ANSWER COUNT (THIS WAS MISSING)
            game["answers_count"][selected_id] += 1
            

            correct_id = QUESTIONS[game["question_index"]]["answer"]
            correct_color = OPTION_COLOR_MAP[correct_id]

            print(
                "ANSWER DEBUG →",
                "Player:", game["players"][player_id]["name"],
                "| Selected:", selected_color,
                "| Correct ID:", correct_id,
                "| Correct Color:", correct_color
            )

            if selected_color == correct_color:
                elapsed = asyncio.get_event_loop().time() - game["question_start"]
                remaining = max(0, QUESTION_TIME - elapsed)
                score = int(BASE_SCORE * (remaining / QUESTION_TIME))
                game["players"][player_id]["score"] += score



            await send_answer_stats(pin)

    except WebSocketDisconnect:
        game["players"][player_id]["ws"] = None
        await broadcast_players(pin)

# ---------------- ANSWER STATS ----------------
async def send_answer_stats(pin):
    
    game = games[pin]
    print(game["answers_count"])
    payload = {"type": "stats", "counts": game["answers_count"]}

    for p in game["players"].values():
        if p["role"] in ("HOST", "SCREEN") and p["ws"]:
            await p["ws"].send_json(payload)

# ---------------- QUESTION END (HOST NOTIFY) ----------------
async def notify_question_end(pin):
    game = games[pin]
    payload = {"type": "question_end"}

    for p in game["players"].values():
        if p["role"] == "HOST" and p["ws"]:
            await p["ws"].send_json(payload)

# ---------------- PLAYERS ----------------
async def broadcast_players(pin):
    game = games[pin]
    visible = [{"name": p["name"]} for p in game["players"].values() if p["role"] == "player"]

    for p in game["players"].values():
        if p["ws"]:
            await p["ws"].send_json({"type": "players", "data": visible})

# ---------------- LEADERBOARD ----------------
async def send_leaderboard(pin, finished=False):
    game = games[pin]

    leaderboard = sorted(
        [p for p in game["players"].values() if p["role"] == "player"],
        key=lambda x: x["score"],
        reverse=True
    )

    payload = {
        "type": "leaderboard",
        "finished": finished,
        "data": [{"name": p["name"], "score": p["score"]} for p in leaderboard]
    }

    for p in game["players"].values():
        if p["ws"]:
            await p["ws"].send_json(payload if finished or p["role"] in ("HOST", "SCREEN")
                                   else {"type": "wait", "message": "⏳ Waiting..."})


@app.post("/show-chart/{pin}")
async def show_chart(pin: str):
    game = games[pin]
    for p in game["players"].values():
        if p["role"] == "SCREEN" and p["ws"]:
            await p["ws"].send_json({"type": "show_chart"})
    return {"ok": True}


@app.post("/show-leaderboard/{pin}")
async def show_leaderboard_api(pin: str):
    await send_leaderboard(pin)
    return {"ok": True}


