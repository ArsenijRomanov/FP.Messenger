import uuid
import asyncio
from utils import now_ts, json_msg
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
from config import logger
from config import MAX_QUEUE_SIZE
from database import rooms, clients

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


async def register_client(ws):
    clients[ws] = {
        'display_name': None,
        'outgoing': asyncio.Queue(maxsize=MAX_QUEUE_SIZE),
        'rooms': set(),
        'writer_task': None
    }

    clients[ws]['writer_task'] = asyncio.create_task(client_writer(ws, clients[ws]['outgoing']))
    return clients[ws]

async def unregister_client(ws):
    client = clients.pop(ws, None)
    if client:

        for room_id in list(client['rooms']):
            await leave_room(room_id, ws, notify=True)

        writer = client.get('writer_task')
        if writer:
            writer.cancel()
            try:
                await writer
            except Exception:
                pass

async def client_writer(ws, outgoing_queue: asyncio.Queue):
    """Отправляет все сообщения из per-client outgoing очереди в websocket."""
    try:
        while True:
            item = await outgoing_queue.get()
            await send_to_ws_safe(ws, item)
            outgoing_queue.task_done()
    except asyncio.CancelledError:
        return
    except Exception:
        return