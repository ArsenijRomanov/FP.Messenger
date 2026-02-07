
import asyncio
import json
import types
import pytest

import server2
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
from websockets.frames import Close


class FakeWebSocket:
    """Минимальная заглушка websocket для unit-тестов ws_handler/handlers."""
    def __init__(self, incoming=None, *, raise_on_send=None):
        self._incoming = list(incoming or [])
        self.sent = []
        self.remote_address = ("127.0.0.1", 12345)
        self._raise_on_send = raise_on_send

    async def send(self, data):
        if self._raise_on_send:
            raise self._raise_on_send
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


def _sent_as_json(ws: FakeWebSocket):
    return [json.loads(x) for x in ws.sent]


async def _wait_until(predicate, timeout=1.0, step=0.01):
    start = asyncio.get_running_loop().time()
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() - start > timeout:
            raise TimeoutError("condition not met")
        await asyncio.sleep(step)


@pytest.fixture(autouse=True)
async def reset_server_state():
    """Сбрасывает глобальные dict'ы сервера и корректно отменяет фоновые задачи."""
    # pre-clean
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

    # post-clean (на случай, если тест упал)
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


@pytest.mark.asyncio
async def test_json_msg_keeps_unicode():
    s = server2.json_msg({"text": "Привет"})
    assert "Привет" in s
    assert "\\u" not in s  # ensure_ascii=False


@pytest.mark.asyncio
async def test_create_room_record_structure():
    r = server2.create_room_record("room")
    assert r["id"] in server2.rooms
    assert len(r["id"]) == 8
    assert r["name"] == "room"
    assert isinstance(r["members"], set)
    assert isinstance(r["queue"], asyncio.Queue)
    assert r["task"] is None


@pytest.mark.asyncio
async def test_ensure_room_dispatcher_nonexistent_does_not_crash():
    # просто не должно упасть
    await server2.ensure_room_dispatcher("no-such-room")


@pytest.mark.asyncio
async def test_send_to_ws_safe_swallows_connection_closed():
    ws_ok = FakeWebSocket(raise_on_send=ConnectionClosedOK(Close(1000, "OK"), Close(1000, "OK")))
    await server2.send_to_ws_safe(ws_ok, {"action": "x"})  # не должно бросить

    ws_err = FakeWebSocket(raise_on_send=ConnectionClosedError(Close(1006, "abnormal"), Close(1006, "abnormal")))
    await server2.send_to_ws_safe(ws_err, {"action": "x"})  # не должно бросить


@pytest.mark.asyncio
async def test_handle_set_username_validations_and_success():
    ws = FakeWebSocket()
    await server2.register_client(ws)

    # empty
    await server2.handle_set_username(ws, {"action": "set_username", "username": ""})
    assert _sent_as_json(ws)[-1]["action"] == "error"

    # too short
    await server2.handle_set_username(ws, {"action": "set_username", "username": "ab"})
    assert _sent_as_json(ws)[-1]["action"] == "error"

    # too long
    await server2.handle_set_username(ws, {"action": "set_username", "username": "a" * 21})
    assert _sent_as_json(ws)[-1]["action"] == "error"

    # ok
    await server2.handle_set_username(ws, {"action": "set_username", "username": "alice"})
    last = _sent_as_json(ws)[-1]
    assert last["action"] == "username_set"
    assert server2.clients[ws]["display_name"] == "alice"
    assert server2.unique_usernames["alice"] is ws

    # duplicate
    ws2 = FakeWebSocket()
    await server2.register_client(ws2)
    await server2.handle_set_username(ws2, {"action": "set_username", "username": "alice"})
    last2 = _sent_as_json(ws2)[-1]
    assert last2["action"] == "error"


@pytest.mark.asyncio
async def test_handle_set_username_client_not_registered():
    ws = FakeWebSocket()
    await server2.handle_set_username(ws, {"action": "set_username", "username": "bob"})
    assert _sent_as_json(ws)[-1]["action"] == "error"


@pytest.mark.asyncio
async def test_join_room_error_branches():
    ws = FakeWebSocket()
    await server2.register_client(ws)

    # room not found
    await server2.join_room("missing", ws, "alice")
    assert _sent_as_json(ws)[-1]["action"] == "error"

    # create room and join twice
    room = server2.create_room_record("room")
    await server2.ensure_room_dispatcher(room["id"])
    await server2.join_room(room["id"], ws, "alice")
    assert _sent_as_json(ws)[-1]["action"] == "joined"

    await server2.join_room(room["id"], ws, "alice")
    assert _sent_as_json(ws)[-1]["action"] == "error"


@pytest.mark.asyncio
async def test_leave_room_is_idempotent():
    ws = FakeWebSocket()
    await server2.register_client(ws)
    room = server2.create_room_record("room")
    await server2.ensure_room_dispatcher(room["id"])

    # leaving without join should not crash
    await server2.leave_room(room["id"], ws, notify=True)

    await server2.join_room(room["id"], ws, "alice")
    await server2.leave_room(room["id"], ws, notify=True)
    assert ws not in server2.rooms[room["id"]]["members"]
    assert room["id"] not in server2.clients[ws]["rooms"]


@pytest.mark.asyncio
async def test_room_dispatcher_broadcasts_and_cancel_clears_queue():
    room = server2.create_room_record("room")
    await server2.ensure_room_dispatcher(room["id"])

    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    await server2.register_client(ws1)
    await server2.register_client(ws2)

    await server2.join_room(room["id"], ws1, "alice")
    await server2.join_room(room["id"], ws2, "bob")

    # отправим сообщение в комнату
    await room["queue"].put({"action": "message", "room_id": room["id"], "from": "alice", "text": "hi", "ts": 1})

    # дождаться доставки в обоих ws (через client_writer)
    def got_message(ws):
        return any(m.get("action") == "message" and m.get("text") == "hi" for m in _sent_as_json(ws))

    await _wait_until(lambda: got_message(ws1) and got_message(ws2), timeout=1.5)

    # проверим отмену dispatcher и очистку очереди
    await room["queue"].put({"action": "message", "room_id": room["id"], "from": "alice", "text": "later", "ts": 2})
    t = room["task"]
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    # после отмены dispatcher он очищает очередь
    assert room["queue"].empty()


@pytest.mark.asyncio
async def test_room_dispatcher_disconnects_slow_client_on_queue_full():
    room = server2.create_room_record("room")
    await server2.ensure_room_dispatcher(room["id"])

    ws = FakeWebSocket()
    await server2.register_client(ws)
    await server2.join_room(room["id"], ws, "alice")

    # "замедлим" клиента: остановим writer_task и поставим маленькую очередь, уже заполненную
    writer = server2.clients[ws]["writer_task"]
    writer.cancel()
    try:
        await writer
    except asyncio.CancelledError:
        pass
    except Exception:
        pass

    slow_q = asyncio.Queue(maxsize=1)
    slow_q.put_nowait({"action": "dummy"})
    server2.clients[ws]["outgoing"] = slow_q

    await room["queue"].put({"action": "message", "room_id": room["id"], "from": "alice", "text": "x", "ts": 1})

    # dispatcher должен отправить ошибку и удалить клиента
    def disconnected():
        return (ws not in server2.clients) and any(m.get("action") == "error" and "Too slow" in m.get("message","")
                                                  for m in _sent_as_json(ws))

    await _wait_until(disconnected, timeout=1.5)


@pytest.mark.asyncio
async def test_handle_private_message_all_branches():
    # errors: missing recipient / empty text / not registered / offline
    ws = FakeWebSocket()
    await server2.register_client(ws)
    await server2.handle_set_username(ws, {"action":"set_username", "username":"alice"})

    await server2.handle_private_message(ws, {"action":"private_message", "text":"hi"})
    assert _sent_as_json(ws)[-1]["action"] == "error"

    await server2.handle_private_message(ws, {"action":"private_message", "to":"bob", "text":""})
    assert _sent_as_json(ws)[-1]["action"] == "error"

    ws_unreg = FakeWebSocket()
    await server2.handle_private_message(ws_unreg, {"action":"private_message", "to":"bob", "text":"hi"})
    assert _sent_as_json(ws_unreg)[-1]["action"] == "error"

    await server2.handle_private_message(ws, {"action":"private_message", "to":"bob", "text":"hi"})
    assert _sent_as_json(ws)[-1]["action"] == "error"

    # success
    ws2 = FakeWebSocket()
    await server2.register_client(ws2)
    await server2.handle_set_username(ws2, {"action":"set_username", "username":"bob"})

    await server2.handle_private_message(ws, {"action":"private_message", "to":"bob", "text":"hello"})
    # sender gets confirmation
    assert any(m.get("action") == "private_message_sent" and m.get("to") == "bob" for m in _sent_as_json(ws))

    # recipient gets private_message via outgoing -> writer
    def recipient_got():
        return any(m.get("action") == "private_message" and m.get("from") == "alice" and m.get("text") == "hello"
                   for m in _sent_as_json(ws2))
    await _wait_until(recipient_got, timeout=1.5)


@pytest.mark.asyncio
async def test_handle_message_all_error_branches_and_success_broadcast():
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    await server2.register_client(ws1)
    await server2.register_client(ws2)

    await server2.handle_message(ws1, {"action":"message", "text":"x"})
    assert _sent_as_json(ws1)[-1]["action"] == "error"

    await server2.handle_message(ws1, {"action":"message", "room_id":"missing", "text":"x"})
    assert _sent_as_json(ws1)[-1]["action"] == "error"

    room = server2.create_room_record("room")
    await server2.ensure_room_dispatcher(room["id"])

    await server2.handle_message(ws1, {"action":"message", "room_id":room["id"], "text":"x"})
    assert _sent_as_json(ws1)[-1]["action"] == "error"

    await server2.join_room(room["id"], ws1, "alice")
    await server2.join_room(room["id"], ws2, "bob")

    await server2.handle_message(ws1, {"action":"message", "room_id":room["id"], "text":"hello"})
    def got():
        return any(m.get("action") == "message" and m.get("text") == "hello" for m in _sent_as_json(ws2))
    await _wait_until(got, timeout=1.5)


@pytest.mark.asyncio
async def test_ws_handler_welcome_invalid_json_unknown_action_too_large_and_cleanup(monkeypatch):
    # добавим handler, который падает, чтобы покрыть ветку handler error
    async def boom(ws, payload):
        raise RuntimeError("boom")

    monkeypatch.setitem(server2.ACTION_HANDLERS, "boom", boom)

    huge = "a" * (server2.MAX_MESSAGE_SIZE + 1)
    incoming = [
        "{not json}",  # invalid json
        json.dumps({"action": "unknown"}),  # unknown action
        huge,  # too large
        json.dumps({"action": "boom"}),  # handler exception
    ]
    ws = FakeWebSocket(incoming=incoming)

    await server2.ws_handler(ws)

    msgs = _sent_as_json(ws)

    assert msgs[0]["action"] == "welcome"
    assert any(m.get("action") == "error" and m.get("message") == "invalid json" for m in msgs)
    assert any(m.get("action") == "error" and "unknown action" in m.get("message","") for m in msgs)
    assert any(m.get("action") == "error" and "Message too large" in m.get("message","") for m in msgs)
    assert any(m.get("action") == "error" and "handler error" in m.get("message","") for m in msgs)

    # должен удалить клиента из clients в finally
    assert ws not in server2.clients
