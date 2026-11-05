from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, func, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket, TicketStatus


@dataclass(frozen=True, slots=True)
class NewTicket:
    event_id: str
    ticket_id: str
    customer_email: str
    message: str
    reply_url: str


@dataclass(frozen=True, slots=True)
class ClaimedTicket:
    id: int
    event_id: str
    ticket_id: str
    message: str
    attempts: int
    lease: datetime


async def insert_ticket_if_new(session: AsyncSession, ticket: NewTicket) -> bool:
    statement = (
        insert(Ticket)
        .values(
            event_id=ticket.event_id,
            ticket_id=ticket.ticket_id,
            customer_email=ticket.customer_email,
            message=ticket.message,
            reply_url=ticket.reply_url,
        )
        .on_conflict_do_nothing(index_elements=[Ticket.event_id])
        .returning(Ticket.id)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none() is not None


_CLAIM_FOR_GENERATION_SQL = text(
    """
    WITH candidates AS (
        SELECT id
        FROM tickets
        WHERE (status = 'received' AND next_attempt_at <= now())
           OR (status = 'generating' AND locked_until < now())
        ORDER BY next_attempt_at, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    ), exhausted AS (
        SELECT t.id, t.attempts >= :max_attempts AS is_exhausted
        FROM tickets AS t JOIN candidates AS c ON c.id = t.id
    )
    UPDATE tickets AS t
    SET status = CASE WHEN e.is_exhausted THEN 'draft_failed' ELSE 'generating' END,
        attempts = t.attempts + CASE WHEN e.is_exhausted THEN 0 ELSE 1 END,
        locked_until = CASE
            WHEN e.is_exhausted THEN NULL
            ELSE now() + make_interval(secs => CAST(:lease_seconds AS double precision))
        END,
        last_error = CASE
            WHEN e.is_exhausted THEN 'Generation abandoned too many times (lease expired)'
            ELSE t.last_error
        END,
        updated_at = now()
    FROM exhausted AS e
    WHERE t.id = e.id
    RETURNING t.id, t.event_id, t.ticket_id, t.message, t.attempts, t.locked_until, t.status
    """
)


async def claim_for_generation(
    session: AsyncSession, limit: int, lease_seconds: float, max_attempts: int
) -> list[ClaimedTicket]:
    rows = await session.execute(
        _CLAIM_FOR_GENERATION_SQL,
        {"limit": limit, "lease_seconds": lease_seconds, "max_attempts": max_attempts},
    )
    return [
        ClaimedTicket(
            id=row.id,
            event_id=row.event_id,
            ticket_id=row.ticket_id,
            message=row.message,
            attempts=row.attempts,
            lease=row.locked_until,
        )
        for row in rows
        if row.status == TicketStatus.GENERATING
    ]


def _owned(ticket: ClaimedTicket) -> ColumnElement[bool]:
    return and_(
        Ticket.id == ticket.id,
        Ticket.status == TicketStatus.GENERATING,
        Ticket.locked_until == ticket.lease,
    )


async def _update_owned(session: AsyncSession, ticket: ClaimedTicket, **values: Any) -> bool:
    statement = update(Ticket).where(_owned(ticket)).values(locked_until=None, **values)
    result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount == 1


async def save_draft(
    session: AsyncSession, ticket: ClaimedTicket, text: str, sources: list[dict[str, str]]
) -> bool:
    return await _update_owned(
        session,
        ticket,
        status=TicketStatus.PENDING_REVIEW,
        draft_text=text,
        draft_sources=sources,
        last_error=None,
    )


async def reschedule_generation(
    session: AsyncSession, ticket: ClaimedTicket, delay_seconds: float, error: str
) -> bool:
    return await _update_owned(
        session,
        ticket,
        status=TicketStatus.RECEIVED,
        next_attempt_at=func.now() + timedelta(seconds=delay_seconds),
        last_error=error,
    )


async def fail_generation(session: AsyncSession, ticket: ClaimedTicket, error: str) -> bool:
    return await _update_owned(session, ticket, status=TicketStatus.DRAFT_FAILED, last_error=error)
