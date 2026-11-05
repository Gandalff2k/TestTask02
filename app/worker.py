import asyncio
import logging
import signal

from app.config import get_settings
from app.logs import configure_logging

logger = logging.getLogger(__name__)


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("worker_started")
    await stop.wait()
    logger.info("worker_stopped")


if __name__ == "__main__":
    configure_logging(get_settings().log_level)
    asyncio.run(run())
