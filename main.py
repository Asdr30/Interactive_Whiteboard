import asyncio
import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# ──────────────────────────────────────────────────────────────────────────────
# In-memory room store
# ──────────────────────────────────────────────────────────────────────────────
# rooms[room_id] = {
#     "connections":  list[WebSocket],
#     "history":      list[dict],        ← full event log for late-join replay
#     "page_count":   int,               ← mirrors the client page count
#     "cleanup_task": asyncio.Task|None  ← grace-period deletion timer
# }
rooms: dict[str, dict[str, Any]] = {}

GRACE_SECONDS: int = 30  # keep empty room alive for this long before wiping it


def _ensure_room(room_id: str) -> dict:
    """Return the room dict, creating it if it doesn't exist yet."""
    if room_id not in rooms:
        rooms[room_id] = {
            "connections":  [],
            "history":      [],
            "page_count":   1,
            "cleanup_task": None,
        }
    return rooms[room_id]


async def _grace_delete(room_id: str) -> None:
    """Wait GRACE_SECONDS, then delete the room if it is still empty."""
    await asyncio.sleep(GRACE_SECONDS)
    room = rooms.get(room_id)
    if room is not None and not room["connections"]:
        del rooms[room_id]


# ──────────────────────────────────────────────────────────────────────────────
# Connection manager
# ──────────────────────────────────────────────────────────────────────────────
class ConnectionManager:

    async def connect(self, room_id: str, ws: WebSocket) -> None:
        await ws.accept()
        room = _ensure_room(room_id)

        # Cancel any pending grace-period cleanup — a new joiner revives the room
        task: asyncio.Task | None = room["cleanup_task"]
        if task and not task.done():
            task.cancel()
        room["cleanup_task"] = None

        room["connections"].append(ws)

        # Send the full event log to the new joiner so they can replay state
        await ws.send_text(json.dumps({
            "type":       "sync",
            "history":    room["history"],
            "page_count": room["page_count"],
        }))

    async def disconnect(self, room_id: str, ws: WebSocket) -> None:
        room = rooms.get(room_id)
        if room is None:
            return
        if ws in room["connections"]:
            room["connections"].remove(ws)
        # Start the grace timer if the room is now empty
        if not room["connections"]:
            room["cleanup_task"] = asyncio.create_task(_grace_delete(room_id))

    async def broadcast(
        self, room_id: str, message: str, sender: WebSocket
    ) -> None:
        room = rooms.get(room_id)
        if room is None:
            return
        dead: list[WebSocket] = []
        for conn in room["connections"]:
            if conn is sender:
                continue
            try:
                await conn.send_text(message)
            except Exception:
                dead.append(conn)
        for conn in dead:
            await self.disconnect(room_id, conn)


manager = ConnectionManager()

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Collaborative Whiteboard")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health():
    """Used by the Fly.io health check and for basic monitoring."""
    return {
        "status": "ok",
        "active_rooms": len(rooms),
        "total_connections": sum(len(r["connections"]) for r in rooms.values()),
    }


@app.websocket("/ws/{room_id}")
async def ws_endpoint(websocket: WebSocket, room_id: str) -> None:
    await manager.connect(room_id, websocket)
    try:
        while True:
            raw   = await websocket.receive_text()
            event = json.loads(raw)
            etype = event.get("type", "draw")
            room  = _ensure_room(room_id)   # room is always present here

            # All client-originated events go into the persistent history
            # so late joiners can replay them via the sync message.
            room["history"].append(event)

            # Keep the server-side page count in sync with client actions
            if etype == "page_add":
                room["page_count"] += 1
            elif etype == "page_delete" and room["page_count"] > 1:
                room["page_count"] -= 1
            # clear_page and draw don't change page_count

            # Forward to every other client in the room
            await manager.broadcast(room_id, raw, websocket)

    except WebSocketDisconnect:
        await manager.disconnect(room_id, websocket)