
import asyncio
import json
import socket
import pytest
import websockets

import server2


async def send_json(ws, obj):
    await ws.send(json.dumps(obj, ensure_ascii=False))


async def recv_until(ws, *, action=None, predicate=None, timeout=2.0):
    """Читает сообщения до тех пор, пока не найдёт нужное."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"didn't receive action={action}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg = json.loads(raw)
        if action is None and predicate is None:
            return msg
        if action is not None and msg.get("action") == action:
            return msg
        if predicate is not None and predicate(msg):
            return msg


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
async def reset_server_state():
    # отменим все фоновые задачи между тестами
    for r in list(server2.rooms.values()):
        t = r.get("task")
        if t and not t.done():
            t.cancel()
            try:
                await t
            except Exception:
                pass
    for c in list(server2.clients.values()):
        t = c.get("writer_task")
        if t and not t.done():
            t.cancel()
            try:
                await t
            except Exception:
                pass

    server2.rooms.clear()
    server2.clients.clear()
    server2.unique_usernames.clear()

    yield

    for r in list(server2.rooms.values()):
        t = r.get("task")
        if t and not t.done():
            t.cancel()
            try:
                await t
            except Exception:
                pass
    for c in list(server2.clients.values()):
        t = c.get("writer_task")
        if t and not t.done():
            t.cancel()
            try:
                await t
            except Exception:
                pass
    server2.rooms.clear()
    server2.clients.clear()
    server2.unique_usernames.clear()


@pytest.fixture
async def running_server():
    port = get_free_port()
    server = await websockets.serve(server2.ws_handler, "127.0.0.1", port)
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_e2e_room_lifecycle_and_broadcast(running_server):
    url = running_server

    async with websockets.connect(url) as a, websockets.connect(url) as b:
        await recv_until(a, action="welcome")
        await recv_until(b, action="welcome")

        await send_json(a, {"action": "set_username", "username": "alice"})
        await send_json(b, {"action": "set_username", "username": "bob"})
        await recv_until(a, action="username_set")
        await recv_until(b, action="username_set")

        await send_json(a, {"action": "create_room", "name": "room"})
        created = await recv_until(a, action="room_created")
        room_id = created["room"]["id"]

        # list_rooms must include our room
        await send_json(a, {"action": "list_rooms"})
        rooms_list = await recv_until(a, action="rooms_list")
        assert any(r["id"] == room_id and r["name"] == "room" for r in rooms_list["rooms"])

        # join both
        await send_json(a, {"action": "join", "room_id": room_id, "display_name": "alice"})
        await send_json(b, {"action": "join", "room_id": room_id, "display_name": "bob"})
        await recv_until(a, action="joined")
        await recv_until(b, action="joined")

        # both should see user_joined (order can vary)
        await recv_until(a, action="user_joined")
        await recv_until(b, action="user_joined")

        # send message from alice -> bob must receive
        await send_json(a, {"action": "message", "room_id": room_id, "text": "hi"})
        msg_b = await recv_until(b, action="message")
        assert msg_b["text"] == "hi"
        assert msg_b["from"] == "alice"
        assert msg_b["room_id"] == room_id

        # leave bob -> alice should see user_left
        await send_json(b, {"action": "leave", "room_id": room_id})
        left_a = await recv_until(a, action="user_left")
        assert left_a["user"] == "bob"

        # list_rooms should show members count == 1 (bob ушёл)
        await send_json(a, {"action": "list_rooms"})
        rooms_list2 = await recv_until(a, action="rooms_list")
        rec = next(r for r in rooms_list2["rooms"] if r["id"] == room_id)
        assert rec["members"] == 1


@pytest.mark.asyncio
async def test_e2e_private_message(running_server):
    url = running_server
    async with websockets.connect(url) as a, websockets.connect(url) as b:
        await recv_until(a, action="welcome")
        await recv_until(b, action="welcome")

        await send_json(a, {"action": "set_username", "username": "alice"})
        await send_json(b, {"action": "set_username", "username": "bob"})
        await recv_until(a, action="username_set")
        await recv_until(b, action="username_set")

        await send_json(a, {"action": "private_message", "to": "bob", "text": "hello"})
        sent = await recv_until(a, action="private_message_sent")
        assert sent["to"] == "bob"
        assert sent["text"] == "hello"

        pm = await recv_until(b, action="private_message")
        assert pm["from"] == "alice"
        assert pm["to"] == "bob"
        assert pm["text"] == "hello"


@pytest.mark.asyncio
async def test_e2e_username_uniqueness(running_server):
    url = running_server
    async with websockets.connect(url) as a, websockets.connect(url) as b:
        await recv_until(a, action="welcome")
        await recv_until(b, action="welcome")

        await send_json(a, {"action": "set_username", "username": "same"})
        await recv_until(a, action="username_set")

        await send_json(b, {"action": "set_username", "username": "same"})
        err = await recv_until(b, action="error")
        assert "already taken" in err["message"]


@pytest.mark.asyncio
async def test_e2e_disconnect_cleanup_removes_username_and_membership(running_server):
    url = running_server
    a = await websockets.connect(url)
    b = await websockets.connect(url)
    try:
        await recv_until(a, action="welcome")
        await recv_until(b, action="welcome")

        await send_json(a, {"action": "set_username", "username": "alice"})
        await send_json(b, {"action": "set_username", "username": "bob"})
        await recv_until(a, action="username_set")
        await recv_until(b, action="username_set")

        await send_json(a, {"action": "create_room", "name": "room"})
        room_id = (await recv_until(a, action="room_created"))["room"]["id"]

        await send_json(a, {"action": "join", "room_id": room_id, "display_name": "alice"})
        await send_json(b, {"action": "join", "room_id": room_id, "display_name": "bob"})
        await recv_until(a, action="joined")
        await recv_until(b, action="joined")

        # закрываем bob: сервер должен очистить unique_usernames и membership комнаты
        await b.close()

        # подождём, пока сработает finally -> unregister_client
        def cleanup_done():
            return "bob" not in server2.unique_usernames

        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if cleanup_done():
                break
            await asyncio.sleep(0.02)
        assert "bob" not in server2.unique_usernames

        # bob должен быть удалён из members комнаты
        assert all(ws for ws in server2.rooms[room_id]["members"] if server2.clients.get(ws, {}).get("display_name") != "bob")
    finally:
        await a.close()
        if not b.closed:
            await b.close()