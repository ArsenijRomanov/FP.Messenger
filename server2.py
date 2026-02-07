import asyncio
import json
import uuid
import time
import logging
from typing import Dict, Set
import websockets
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('chat_server.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

MAX_MESSAGE_SIZE = 1024 * 1024

# --- Глобальное состояние ---
rooms: Dict[str, dict] = {}  # room_id -> {'name': str, 'members': set(ws), 'queue': asyncio.Queue(), 'task': Task}
clients: Dict[object, dict] = {}  # websocket -> {'display_name': str or None, 'outgoing': asyncio.Queue(), 'rooms': set(room_id)}
unique_usernames: Dict[str, object] = {} # username -> ws


# --- Утилиты ---
def now_ts():
    return int(time.time())


def json_msg(obj):
    return json.dumps(obj, ensure_ascii=False)


# --- Команды для работы с комнатами ---
def create_room_record(name: str):
    room_id = uuid.uuid4().hex[:8]
    queue = asyncio.Queue()
    rooms[room_id] = {
        'id': room_id,
        'name': name,
        'members': set(),
        'queue': queue,
        'task': None
    }
    return rooms[room_id]


async def ensure_room_dispatcher(room_id: str):
    room = rooms.get(room_id)
    if room is None:
        logger.warning(f"Attempted to ensure dispatcher for non-existent room: {room_id}")
        return
    if room['task'] is None or room['task'].done():
        room['task'] = asyncio.create_task(room_dispatcher(room_id))


async def room_dispatcher(room_id: str):
    """Consumer: читает из room.queue и рассылает всем участникам через их outgoing очереди."""
    room = rooms.get(room_id)
    queue: asyncio.Queue = room['queue']
    logger.debug(f"Room dispatcher started for room: {room_id}")

    try:
        while True:
            item = await queue.get()
            members = list(room['members'])

            for ws in members:
                client = clients.get(ws)
                if client is None:
                    continue
                out_q: asyncio.Queue = client['outgoing']

                try:
                    out_q.put_nowait(item)
                except asyncio.QueueFull:
                    logger.warning(f"Client is too slow, disconnecting")

                    await send_to_ws_safe(ws, {
                        'action': 'error',
                        'message': 'Too slow, disconnecting'
                    })
                    await unregister_client(ws)
            queue.task_done()
    except asyncio.CancelledError:
        logger.info(f"Room dispatcher cancelled for room: {room_id}")
        # Очищаем очередь при отмене
        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
            except Exception as e:
                logger.error(f"Error while clearing queue on cancel: {e}")
        return
    except Exception as e:
        logger.critical(f"Unhandled exception in room dispatcher {room_id}: {e}", exc_info=True)
        raise


# --- Персональные операции для клиента ---
async def register_client(ws):
    clients[ws] = {
        'display_name': None,
        'outgoing': asyncio.Queue(maxsize=100),
        'rooms': set(),
        'writer_task': None
    }
    clients[ws]['writer_task'] = asyncio.create_task(client_writer(ws, clients[ws]['outgoing']))
    return clients[ws]


async def unregister_client(ws):
    client = clients.pop(ws, None)
    if client:
        display_name = client.get('display_name', 'unknown')

        if display_name in unique_usernames:
            del unique_usernames[display_name]

        for room_id in list(client['rooms']):
            await leave_room(room_id, ws, notify=True)

        writer = client.get('writer_task')
        if writer:
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error cancelling writer task: {e}", exc_info=True)


async def client_writer(ws, outgoing_queue: asyncio.Queue):
    """Отправляет все сообщения из per-client outgoing очереди в websocket."""
    try:
        while True:
            item = await outgoing_queue.get()
            await send_to_ws_safe(ws, item)
            outgoing_queue.task_done()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Error in client_writer: {e}", exc_info=True)


async def send_to_ws_safe(ws, obj):
    try:
        await ws.send(json_msg(obj))
    except (ConnectionClosedOK, ConnectionClosedError):
        pass
    except Exception as e:
        logger.error(f"Error sending message: {e}", exc_info=True)
        pass


async def join_room(room_id: str, ws, display_name: str):
    room = rooms.get(room_id)
    if room is None:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': f'Room {room_id} not found'
        })
        return

    if ws in room['members']:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'Already in this room'
        })
        return

    client = clients.get(ws)
    if client is None:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'Client not registered'
        })
        return

    room['members'].add(ws)
    client['rooms'].add(room_id)
    client['display_name'] = display_name or client.get('display_name') or 'Anon'

    await send_to_ws_safe(ws, {
        'action': 'joined',
        'room': {
            'id': room_id,
            'name': room['name']
        }
    })

    await room['queue'].put({
        'action': 'user_joined',
        'room_id': room_id,
        'user': client['display_name'],
        'ts': now_ts()
    })


async def leave_room(room_id: str, ws, notify=True):
    room = rooms.get(room_id)
    if room is None:
        return

    client_name = None
    client = clients.get(ws)
    if client:
        client_name = client.get('display_name', 'unknown')

    if ws in room['members']:
        room['members'].remove(ws)

    if client:
        client['rooms'].discard(room_id)

    if notify and client_name:
        await room['queue'].put({
            'action': 'user_left',
            'room_id': room_id,
            'user': client_name,
            'ts': now_ts()
        })


async def handle_set_username(ws, payload):
    """{'action': 'set_username', 'username': 'unique_name'}"""
    username = payload.get('username', '').strip()

    if not username:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'Username cannot be empty'
        })
        return

    if len(username) < 3:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'Username must be at least 3 characters long'
        })
        return

    if len(username) > 20:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'Username must be less than 20 characters'
        })
        return

    if username in unique_usernames:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': f'Username "{username}" is already taken. Please choose another.'
        })
        return

    client = clients.get(ws)
    if not client:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'Client not registered'
        })
        return

    client['display_name'] = username
    unique_usernames[username] = ws

    await send_to_ws_safe(ws, {
        'action': 'username_set',
        'username': username,
        'message': f'Welcome, {username}!'
    })


async def handle_private_message(ws, payload):
    """{'action': 'private_message', 'to': 'recipient_name', 'text': 'message'}"""
    recipient_name = payload.get('to')
    text = payload.get('text', '')

    if not recipient_name:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'private message requires recipient name'
        })
        return

    if not text:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'private message text is empty'
        })
        return

    client = clients.get(ws)
    if not client:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'client not registered'
        })
        return

    sender_name = client.get('display_name', 'Anon')

    recipient_ws = unique_usernames.get(recipient_name)

    if not recipient_ws:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': f'User "{recipient_name}" not found or offline'
        })
        return

    message = {
        'action': 'private_message',
        'from': sender_name,
        'to': recipient_name,
        'text': text,
        'ts': now_ts()
    }

    await clients[recipient_ws]['outgoing'].put(message)

    # Подтверждение отправителю
    await send_to_ws_safe(ws, {
        'action': 'private_message_sent',
        'to': recipient_name,
        'text': text,
        'ts': now_ts()
    })


# --- Команды обработки клиентских сообщений ---
async def handle_create_room(ws, payload):
    name = payload.get('name') or 'Без названия'
    room = create_room_record(name)
    await ensure_room_dispatcher(room['id'])

    await send_to_ws_safe(ws, {
        'action': 'room_created',
        'room': {'id': room['id'], 'name': room['name']}
    })


async def handle_list_rooms(ws, payload):
    lst = [{'id': r['id'], 'name': r['name'], 'members': len(r['members'])} for r in rooms.values()]
    await send_to_ws_safe(ws, {
        'action': 'rooms_list',
        'rooms': lst
    })


async def handle_join(ws, payload):
    room_id = payload.get('room_id')
    display_name = payload.get('display_name') or 'Anon'
    if not room_id:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'join requires room_id'
        })
        return
    await join_room(room_id, ws, display_name)


async def handle_leave(ws, payload):
    room_id = payload.get('room_id')
    if not room_id:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'leave requires room_id'
        })
        return
    await leave_room(room_id, ws, notify=True)


async def handle_message(ws, payload):
    room_id = payload.get('room_id')
    text = payload.get('text', '')

    if not room_id:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'message requires room_id'
        })
        return

    room = rooms.get(room_id)
    if room is None:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'room not found'
        })
        return

    if ws not in room['members']:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'not joined to room'
        })
        return

    client = clients.get(ws)
    name = client.get('display_name') if client else 'Anon'

    await room['queue'].put({
        'action': 'message',
        'room_id': room_id,
        'from': name,
        'text': text,
        'ts': now_ts(),
    })


# Map action -> handler
ACTION_HANDLERS = {
    'set_username': handle_set_username,
    'private_message': handle_private_message,
    'create_room': handle_create_room,
    'list_rooms': handle_list_rooms,
    'join': handle_join,
    'leave': handle_leave,
    'message': handle_message,
}


async def ws_handler(ws):
    """Основной обработчик подключения клиента."""
    await register_client(ws)

    try:
        await send_to_ws_safe(ws, {
            'action': 'welcome',
            'message': 'Welcome to chat! Please choose a unique username (3-20 characters).'
        })

        async for raw in ws:
            if len(raw) > MAX_MESSAGE_SIZE:
                await send_to_ws_safe(ws, {
                    'action': 'error',
                    'message': f'Message too large. Max size: {MAX_MESSAGE_SIZE} bytes'
                })
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as e:
                await send_to_ws_safe(ws, {
                    'action': 'error',
                    'message': 'invalid json'
                })
                continue
            except Exception as e:
                logger.error(f"Unexpected error parsing message: {e}", exc_info=True)
                await send_to_ws_safe(ws, {
                    'action': 'error',
                    'message': 'internal error'
                })
                continue

            action = payload.get('action')
            handler = ACTION_HANDLERS.get(action)

            if handler:
                try:
                    await handler(ws, payload)
                except Exception as e:
                    logger.error(f"Handler error for action '{action}': {e}", exc_info=True)
                    await send_to_ws_safe(ws, {
                        'action': 'error',
                        'message': f'handler error: {str(e)[:100]}'
                    })
            else:
                logger.warning(f"Unknown action from {ws.remote_address}: {action}")
                await send_to_ws_safe(ws, {
                    'action': 'error',
                    'message': f'unknown action {action}'
                })

    except (ConnectionClosedOK, ConnectionClosedError):
        pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.critical(f"Unhandled exception in ws_handler: {e}", exc_info=True)
    finally:
        await unregister_client(ws)


# --- Запуск сервера ---
async def start_server(host='localhost', port=8765):
    """Запускает WebSocket-сервер."""
    logger.info(f"Starting server on ws://{host}:{port}")
    try:
        async with websockets.serve(ws_handler, host, port):
            await asyncio.Future()
    except Exception as e:
        logger.critical(f"Server error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        logger.info("Server stopped")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)