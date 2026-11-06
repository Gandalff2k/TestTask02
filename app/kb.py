from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_session, templates
from app.knowledge import SLUG_PATTERN, get_article, list_articles

router = APIRouter()

Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/kb", response_class=HTMLResponse)
async def kb_index(request: Request, session: Session) -> HTMLResponse:
    articles = await list_articles(session)
    return templates.TemplateResponse(
        request, "kb_list.html", {"articles": articles, **customer_links(request)}
    )


@router.get("/kb/{slug}", response_class=HTMLResponse)
async def kb_article(
    request: Request,
    session: Session,
    slug: Annotated[str, Path(max_length=100, pattern=SLUG_PATTERN.pattern)],
) -> HTMLResponse:
    article = await get_article(session, slug)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return templates.TemplateResponse(
        request, "kb_article.html", {"article": article, **customer_links(request)}
    )


def customer_links(request: Request) -> dict[str, str | None]:
    settings: Settings = request.app.state.settings
    return {"support_portal_url": settings.support_portal_url}
