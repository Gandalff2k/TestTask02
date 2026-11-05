import logging
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_session, templates
from app.models import TicketStatus
from app.tickets import apply_transition, count_by_status, get_ticket, list_tickets

logger = logging.getLogger(__name__)

PAGE_SIZE = 50

STATUS_LABELS = {
    TicketStatus.RECEIVED: "очікує генерації",
    TicketStatus.GENERATING: "генерується",
    TicketStatus.PENDING_REVIEW: "чекає перевірки",
    TicketStatus.APPROVED: "підтверджено, в черзі на надсилання",
    TicketStatus.SENDING: "надсилається",
    TicketStatus.SENT: "надіслано",
    TicketStatus.REJECTED: "відхилено",
    TicketStatus.DRAFT_FAILED: "помилка генерації",
    TicketStatus.SEND_FAILED: "помилка надсилання",
    TicketStatus.DELIVERY_UNKNOWN: "доставка невідома",
}


class Action(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REGENERATE = "regenerate"
    RESEND = "resend"
    MARK_SENT = "mark-sent"


@dataclass(frozen=True, slots=True)
class Transition:
    label: str
    from_statuses: frozenset[TicketStatus]
    to_status: TicketStatus


TRANSITIONS = {
    Action.APPROVE: Transition(
        "Підтвердити і надіслати", frozenset({TicketStatus.PENDING_REVIEW}), TicketStatus.APPROVED
    ),
    Action.REJECT: Transition(
        "Відхилити", frozenset({TicketStatus.PENDING_REVIEW}), TicketStatus.REJECTED
    ),
    Action.REGENERATE: Transition(
        "Згенерувати ще раз", frozenset({TicketStatus.DRAFT_FAILED}), TicketStatus.RECEIVED
    ),
    Action.RESEND: Transition(
        "Надіслати ще раз",
        frozenset({TicketStatus.SEND_FAILED, TicketStatus.DELIVERY_UNKNOWN}),
        TicketStatus.APPROVED,
    ),
    Action.MARK_SENT: Transition(
        "Клієнт отримав, позначити надісланим",
        frozenset({TicketStatus.DELIVERY_UNKNOWN}),
        TicketStatus.SENT,
    ),
}

_basic = HTTPBasic(auto_error=False)


def require_operator(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> None:
    settings: Settings = request.app.state.settings
    if settings.operator_password is None:
        return
    expected_password = settings.operator_password.get_secret_value().encode()
    valid = credentials is not None and (
        secrets.compare_digest(credentials.username.encode(), settings.operator_username.encode())
        & secrets.compare_digest(credentials.password.encode(), expected_password)
    )
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Operator credentials required",
            headers={"WWW-Authenticate": 'Basic realm="operator"'},
        )


def require_same_origin(request: Request) -> None:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
        raise HTTPException(status_code=403, detail="Cross-site request rejected")
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-site request rejected")


router = APIRouter(dependencies=[Depends(require_operator)])

Session = Annotated[AsyncSession, Depends(get_session)]


def available_actions(status: str) -> list[tuple[Action, Transition]]:
    return [(action, t) for action, t in TRANSITIONS.items() if status in t.from_statuses]


@router.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse("/tickets", status_code=303)


@router.get("/tickets", response_class=HTMLResponse)
async def tickets_index(
    request: Request,
    session: Session,
    status: Annotated[TicketStatus | None, Query()] = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
) -> HTMLResponse:
    tickets = await list_tickets(session, status, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "tickets_list.html",
        {
            "tickets": tickets[:PAGE_SIZE],
            "has_next": len(tickets) > PAGE_SIZE,
            "page": page,
            "status": status,
            "counts": await count_by_status(session),
            "labels": STATUS_LABELS,
        },
    )


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_detail(
    request: Request,
    session: Session,
    ticket_id: int,
    result: Literal["done", "stale"] | None = None,
) -> HTMLResponse:
    ticket = await get_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return templates.TemplateResponse(
        request,
        "ticket_detail.html",
        {
            "ticket": ticket,
            "actions": available_actions(ticket.status),
            "result": result,
            "labels": STATUS_LABELS,
        },
    )


@router.post("/tickets/{ticket_id}/{action}", dependencies=[Depends(require_same_origin)])
async def ticket_action(session: Session, ticket_id: int, action: Action) -> RedirectResponse:
    if await get_ticket(session, ticket_id) is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    transition = TRANSITIONS[action]
    applied = await apply_transition(
        session, ticket_id, transition.from_statuses, transition.to_status
    )
    await session.commit()
    logger.info("operator_action ticket=%d action=%s applied=%s", ticket_id, action, applied)
    result = "done" if applied else "stale"
    return RedirectResponse(f"/tickets/{ticket_id}?result={result}", status_code=303)
