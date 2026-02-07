from typing import Dict

rooms: Dict[str, dict] = {}  # room_id -> {'name': str, 'members': set(ws), 'queue': asyncio.Queue(), 'task': Task}
clients: Dict[object, dict] = {}  # websocket -> {'display_name': str or None, 'outgoing': asyncio.Queue(), 'rooms': set(room_id)}
unique_usernames: Dict[str, object] = {} # username -> ws
