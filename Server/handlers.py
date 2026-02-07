from operations import *
from database import unique_usernames

async def handle_set_username(ws, payload):
    """{'action': 'set_username', 'username': 'unique_name'}"""
    username = payload.get('username', '').strip()

    if not username:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'Username cannot be empty'
        })
        return

    if len(username) < 3 or len(username) > 20:
        await send_to_ws_safe(ws, {
            'action': 'error',
            'message': 'Username must be at least 3 characters long and at most 20'
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
