import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.tickets import NewTicket, insert_ticket_if_new

logger = logging.getLogger(__name__)

router = APIRouter()


class HelpdeskEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    ticket_id: str = Field(min_length=1, max_length=255)
    customer_email: str = Field(min_length=1, max_length=320)
    message: str = Field(min_length=1, max_length=50_000)
    reply_url: HttpUrl

    @field_validator("event_id", "ticket_id", "customer_email", "message", mode="before")
    @classmethod
    def drop_unstorable_characters(cls, value: object) -> object:
        # Postgres rejects NUL and lone surrogates; a failure here would lose the ticket.
        if not isinstance(value, str):
            return value
        return value.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8")


class WebhookResponse(BaseModel):
    status: Literal["accepted", "duplicate"]


async def _store_ticket(factory: async_sessionmaker[AsyncSession], ticket: NewTicket) -> bool:
    async with factory() as session:
        created = await insert_ticket_if_new(session, ticket)
        await session.commit()
    return created


def _storage_unavailable(event_id: str, reason: str) -> HTTPException:
    logger.warning("webhook_store_failed event_id=%r error=%s", event_id, reason)
    return HTTPException(
        status_code=503,
        detail="Storage unavailable, retry later",
        headers={"Retry-After": "1"},
    )


@router.post("/webhooks/helpdesk")
async def receive_helpdesk_event(event: HelpdeskEvent, request: Request) -> WebhookResponse:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    timeout: float = request.app.state.settings.webhook_db_timeout_seconds
    abandoned: set[asyncio.Task[bool]] = request.app.state.abandoned_writes
    ticket = NewTicket(
        event_id=event.event_id,
        ticket_id=event.ticket_id,
        customer_email=event.customer_email,
        message=event.message,
        reply_url=str(event.reply_url),
    )

    write = asyncio.create_task(_store_ticket(factory, ticket))
    done, _ = await asyncio.wait({write}, timeout=timeout)
    if not done:
        # Closing a connection to a hung database takes seconds, the helpdesk waits only 2.
        write.cancel()
        abandoned.add(write)
        write.add_done_callback(abandoned.discard)
        raise _storage_unavailable(event.event_id, "TimeoutError")

    try:
        created = write.result()
    except (SQLAlchemyError, OSError) as exc:
        raise _storage_unavailable(event.event_id, type(exc).__name__) from exc

    logger.info(
        "webhook_%s event_id=%r ticket_id=%r",
        "accepted" if created else "duplicate",
        event.event_id,
        event.ticket_id,
    )
    return WebhookResponse(status="accepted" if created else "duplicate")
