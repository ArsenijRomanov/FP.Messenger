import logging

MAX_MESSAGE_SIZE = 1024 * 1024
MAX_QUEUE_SIZE = 200
SERVER_HOST = 'localhost'
SERVER_PORT = 8765

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('chat_server.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)
