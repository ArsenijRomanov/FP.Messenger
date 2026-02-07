import asyncio
import json
import websockets
from handlers import *
from config import MAX_MESSAGE_SIZE, SERVER_HOST, SERVER_PORT

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

async def start_server(host=SERVER_HOST, port=SERVER_PORT):
    """Запускает WebSocket-сервер."""
    logger.info(f"Starting server on ws://{host}:{port}")
    try:
        async with websockets.serve(ws_handler, host, port):
            await asyncio.Future()
    except Exception as e:
        logger.critical(f"Server error: {e}", exc_info=True)
        raise
