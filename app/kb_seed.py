import asyncio
import logging

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.knowledge import load_articles, sync_articles
from app.logs import configure_logging

logger = logging.getLogger(__name__)


async def seed() -> None:
    settings = get_settings()
    articles = load_articles(settings.kb_dir)
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            removed = await sync_articles(session, articles)
            await session.commit()
    finally:
        await engine.dispose()
    logger.info("kb_seeded articles=%d removed=%d", len(articles), removed)


if __name__ == "__main__":
    configure_logging(get_settings().log_level)
    asyncio.run(seed())
