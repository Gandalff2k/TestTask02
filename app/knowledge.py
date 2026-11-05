import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_QUERY_WORDS = 30
MIN_WORD_LENGTH = 3
WORD_SIMILARITY_THRESHOLD = 0.5
MIN_ARTICLE_SCORE = 0.35
BODY_WEIGHT = 0.8

STOPWORDS = frozenset(
    {
        "аби",
        "або",
        "але",
        "будь",
        "вам",
        "вас",
        "вечір",
        "вже",
        "вона",
        "вони",
        "воно",
        "все",
        "всі",
        "він",
        "день",
        "для",
        "добрий",
        "дуже",
        "дякую",
        "его",
        "если",
        "здравствуйте",
        "как",
        "коли",
        "ласка",
        "мене",
        "мені",
        "може",
        "можете",
        "можна",
        "можу",
        "над",
        "нам",
        "нас",
        "нема",
        "немає",
        "нет",
        "них",
        "ніж",
        "обо",
        "однак",
        "при",
        "привіт",
        "про",
        "підкажіть",
        "так",
        "також",
        "там",
        "тому",
        "треба",
        "тут",
        "хочу",
        "це",
        "цей",
        "цим",
        "цих",
        "чем",
        "чого",
        "чому",
        "что",
        "чтобы",
        "що",
        "щоб",
        "яка",
        "яке",
        "який",
        "якщо",
        "які",
    }
)


@dataclass(frozen=True, slots=True)
class ArticleDocument:
    slug: str
    title: str
    tags: str
    body: str


@dataclass(frozen=True, slots=True)
class ArticleHit:
    slug: str
    title: str
    body: str
    score: float


class ArticleFormatError(ValueError):
    pass


def parse_article(path: Path) -> ArticleDocument:
    slug = path.stem
    if not SLUG_PATTERN.match(slug):
        raise ArticleFormatError(f"{path.name}: slug must be lowercase latin words with dashes")

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ArticleFormatError(f"{path.name}: first line must be '# Title'")
    title = lines[0].removeprefix("# ").strip()

    rest = lines[1:]
    tags = ""
    if rest and rest[0].startswith("tags:"):
        tags = rest[0].removeprefix("tags:").strip()
        rest = rest[1:]

    body = "\n".join(rest).strip()
    if not title or not body:
        raise ArticleFormatError(f"{path.name}: title and body must not be empty")
    return ArticleDocument(slug=slug, title=title, tags=tags, body=body)


def load_articles(directory: Path) -> list[ArticleDocument]:
    articles = [parse_article(path) for path in sorted(directory.glob("*.md"))]
    if not articles:
        raise ArticleFormatError(f"No articles found in {directory}")
    return articles


async def sync_articles(session: AsyncSession, articles: list[ArticleDocument]) -> int:
    if not articles:
        raise ArticleFormatError("Refusing to sync an empty knowledge base")

    statement = insert(Article).values(
        [{"slug": a.slug, "title": a.title, "tags": a.tags, "body": a.body} for a in articles]
    )
    statement = statement.on_conflict_do_update(
        index_elements=[Article.slug],
        set_={
            "title": statement.excluded.title,
            "tags": statement.excluded.tags,
            "body": statement.excluded.body,
            "updated_at": text("now()"),
        },
    )
    await session.execute(statement)
    removed = await session.execute(
        delete(Article).where(Article.slug.not_in([a.slug for a in articles]))
    )
    return int(removed.rowcount)  # type: ignore[attr-defined]


def query_words(query: str) -> list[str]:
    words: list[str] = []
    for word in re.findall(r"\w+", query.lower()):
        if len(word) >= MIN_WORD_LENGTH and word not in STOPWORDS and word not in words:
            words.append(word)
    return words[:MAX_QUERY_WORDS]


_SEARCH_SQL = text(
    """
    SELECT a.slug, a.title, a.body, sum(m.similarity) / :word_count AS score
    FROM articles AS a
    CROSS JOIN unnest(CAST(:words AS text[])) AS w(word)
    CROSS JOIN LATERAL (
        SELECT greatest(
            word_similarity(w.word, a.title || ' ' || a.tags),
            :body_weight * word_similarity(w.word, a.body)
        ) AS similarity
    ) AS m
    WHERE m.similarity >= :word_threshold
    GROUP BY a.id
    HAVING sum(m.similarity) / :word_count >= :min_score
    ORDER BY score DESC, a.slug
    LIMIT :limit
    """
)


async def search_articles(session: AsyncSession, query: str, limit: int = 3) -> list[ArticleHit]:
    words = query_words(query)
    if not words:
        return []
    rows = await session.execute(
        _SEARCH_SQL,
        {
            "words": words,
            "word_count": len(words),
            "body_weight": BODY_WEIGHT,
            "word_threshold": WORD_SIMILARITY_THRESHOLD,
            "min_score": MIN_ARTICLE_SCORE,
            "limit": limit,
        },
    )
    return [
        ArticleHit(slug=row.slug, title=row.title, body=row.body, score=float(row.score))
        for row in rows
    ]


async def get_article(session: AsyncSession, slug: str) -> Article | None:
    return await session.scalar(select(Article).where(Article.slug == slug))


async def list_articles(session: AsyncSession) -> list[Article]:
    return list((await session.scalars(select(Article).order_by(Article.title))).all())
