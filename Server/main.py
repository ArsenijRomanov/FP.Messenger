import asyncio
from server import start_server
from config import logger

if __name__ == "__main__":
    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        logger.info("Server stopped")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)