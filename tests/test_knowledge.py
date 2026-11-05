from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.knowledge import (
    ArticleDocument,
    ArticleFormatError,
    load_articles,
    parse_article,
    query_words,
    search_articles,
    sync_articles,
)
from app.models import Article

KB_DIR = Path(__file__).parent.parent / "kb"


@pytest.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        await sync_articles(session, load_articles(KB_DIR))
        await session.commit()


async def stored(factory: async_sessionmaker[AsyncSession]) -> dict[str, Article]:
    async with factory() as session:
        return {a.slug: a for a in (await session.scalars(select(Article))).all()}


def write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_parses_title_tags_and_body(tmp_path: Path) -> None:
    path = write(tmp_path, "export-pdf.md", "# Заголовок\ntags: a, b\n\nТекст\nдалі\n")

    assert parse_article(path) == ArticleDocument(
        slug="export-pdf", title="Заголовок", tags="a, b", body="Текст\nдалі"
    )


def test_tags_are_optional(tmp_path: Path) -> None:
    assert parse_article(write(tmp_path, "a.md", "# T\nbody")).tags == ""


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("Bad Slug.md", "# T\nbody"),
        ("a.md", "no title\nbody"),
        ("a.md", "# T\ntags: x"),
        ("a.md", ""),
    ],
)
def test_rejects_malformed_article(tmp_path: Path, name: str, content: str) -> None:
    with pytest.raises(ArticleFormatError):
        parse_article(write(tmp_path, name, content))


def test_real_knowledge_base_is_valid() -> None:
    articles = load_articles(KB_DIR)

    assert 5 <= len(articles) <= 10
    assert len({a.slug for a in articles}) == len(articles)


def test_empty_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ArticleFormatError):
        load_articles(tmp_path)


async def test_sync_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    before = {slug: a.id for slug, a in (await stored(session_factory)).items()}

    async with session_factory() as session:
        removed = await sync_articles(session, load_articles(KB_DIR))
        await session.commit()

    after = {slug: a.id for slug, a in (await stored(session_factory)).items()}
    assert removed == 0
    assert after == before


async def test_sync_updates_changed_and_removes_missing(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    async with session_factory() as session:
        removed = await sync_articles(
            session, [ArticleDocument(slug="export-pdf", title="Новий", tags="", body="Інше")]
        )
        await session.commit()

    articles = await stored(session_factory)
    assert removed == len(load_articles(KB_DIR)) - 1
    assert list(articles) == ["export-pdf"]
    assert articles["export-pdf"].title == "Новий"


async def test_sync_refuses_to_wipe_knowledge_base(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(ArticleFormatError):
            await sync_articles(session, [])


def test_query_words_drop_short_words_stopwords_and_duplicates() -> None:
    assert query_words("Не можу експортувати звіт у PDF, звіт!") == ["експортувати", "звіт", "pdf"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Не можу експортувати звіт у PDF", "export-pdf"),
        ("як вивантажити звіти в пдф", "export-pdf"),
        ("забув пароль, не можу зайти", "password-reset"),
        ("як додати колегу в команду", "team-roles"),
        ("скільки коштує тариф", "billing-plans"),
        ("хочу повернути гроші за підписку", "billing-plans"),
        ("Excel показує незрозумілі символи замість букв у CSV", "export-excel-csv"),
        ("не приходить код підтвердження з телефону", "two-factor-auth"),
        ("дані з гугл таблиці не оновлюються", "data-import"),
        ("щотижнева розсилка не приходить на пошту", "scheduled-reports"),
    ],
)
async def test_search_ranks_relevant_article_first(
    session_factory: async_sessionmaker[AsyncSession], seeded: None, query: str, expected: str
) -> None:
    async with session_factory() as session:
        hits = await search_articles(session, query)

    assert hits[0].slug == expected
    assert len(hits) <= 3
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


@pytest.mark.parametrize(
    "query",
    [
        "Який сьогодні курс долара?",
        "Привіт",
        "Порадьте рецепт борщу",
        "Коли у вас відкриється офіс у Львові?",
        "",
        "!!! ???",
    ],
)
async def test_search_returns_nothing_for_off_topic(
    session_factory: async_sessionmaker[AsyncSession], seeded: None, query: str
) -> None:
    async with session_factory() as session:
        assert await search_articles(session, query) == []


async def test_search_treats_sql_like_input_as_text(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    async with session_factory() as session:
        hits = await search_articles(session, "pdf'); DROP TABLE articles; --")

    assert all(hit.slug for hit in hits)
    assert len(await stored(session_factory)) == len(load_articles(KB_DIR))


async def test_kb_pages(client: httpx.AsyncClient, seeded: None) -> None:
    index = await client.get("/kb")
    article = await client.get("/kb/export-pdf")

    assert index.status_code == 200
    assert 'href="/kb/export-pdf"' in index.text
    assert article.status_code == 200
    assert "Як експортувати звіт у PDF" in article.text


@pytest.mark.parametrize(("slug", "status"), [("missing", 404), ("Bad_Slug", 422)])
async def test_kb_unknown_or_invalid_slug(
    client: httpx.AsyncClient, seeded: None, slug: str, status: int
) -> None:
    assert (await client.get(f"/kb/{slug}")).status_code == status


async def test_kb_page_escapes_html(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await sync_articles(
            session,
            [
                ArticleDocument(
                    slug="x", title="<script>alert(1)</script>", tags="", body="<b>b</b>"
                )
            ],
        )
        await session.commit()

    response = await client.get("/kb/x")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "<b>b</b>" not in response.text
