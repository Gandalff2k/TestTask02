import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.main import create_app
from app.models import Ticket, TicketStatus

URL = "/webhooks/helpdesk"


def payload(**overrides: Any) -> dict[str, Any]:
    return {
        "event_id": "evt_8f2a",
        "ticket_id": "T-1042",
        "customer_email": "user@example.com",
        "message": "Не можу експортувати звіт у PDF",
        "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies",
    } | overrides


async def tickets(factory: async_sessionmaker[AsyncSession]) -> list[Ticket]:
    async with factory() as session:
        return list((await session.scalars(select(Ticket).order_by(Ticket.id))).all())


async def test_new_event_is_committed_before_response(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(URL, json=payload())

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    [ticket] = await tickets(session_factory)
    assert ticket.event_id == "evt_8f2a"
    assert ticket.ticket_id == "T-1042"
    assert ticket.customer_email == "user@example.com"
    assert ticket.message == "Не можу експортувати звіт у PDF"
    assert ticket.reply_url == "https://helpdesk.example.com/tickets/T-1042/replies"
    assert ticket.status == TicketStatus.RECEIVED
    assert ticket.attempts == 0
    assert ticket.created_at.tzinfo is not None


async def test_repeated_event_is_duplicate_and_keeps_first_payload(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await client.post(URL, json=payload())

    response = await client.post(URL, json=payload(message="інший текст"))

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate"}
    [ticket] = await tickets(session_factory)
    assert ticket.message == "Не можу експортувати звіт у PDF"


async def test_concurrent_duplicates_create_one_row(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    responses = await asyncio.gather(*(client.post(URL, json=payload()) for _ in range(20)))

    assert {response.status_code for response in responses} == {200}
    statuses = [response.json()["status"] for response in responses]
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == 19
    assert len(await tickets(session_factory)) == 1


async def test_same_ticket_with_new_event_is_a_separate_request(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await client.post(URL, json=payload(event_id="evt_1"))
    await client.post(URL, json=payload(event_id="evt_2"))

    assert len(await tickets(session_factory)) == 2


async def test_burst_of_distinct_events_is_fully_stored(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    responses = await asyncio.gather(
        *(client.post(URL, json=payload(event_id=f"evt_{i}")) for i in range(50))
    )

    assert {response.status_code for response in responses} == {200}
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Ticket))
    assert count == 50


@pytest.mark.parametrize(
    "body",
    [
        {},
        payload(event_id=""),
        payload(event_id="x" * 256),
        payload(message=""),
        payload(message="x" * 50_001),
        payload(reply_url="not a url"),
        payload(reply_url="ftp://helpdesk.example.com/replies"),
        payload(event_id=123),
    ],
    ids=[
        "empty",
        "blank_event",
        "long_event",
        "blank_message",
        "long_message",
        "bad_url",
        "bad_scheme",
        "wrong_type",
    ],
)
async def test_invalid_body_is_rejected(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    body: dict[str, Any],
) -> None:
    response = await client.post(URL, json=body)

    assert response.status_code == 422
    assert await tickets(session_factory) == []


async def test_malformed_json_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        URL, content=b"{not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 422


async def test_non_utf8_body_is_a_client_error(client: httpx.AsyncClient) -> None:
    response = await client.post(
        URL,
        content='{"message":"Привіт"}'.encode("cp1251"),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400


async def test_nul_bytes_are_stripped_not_failed(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(URL, json=payload(message="до\x00брий день"))

    assert response.status_code == 200
    [ticket] = await tickets(session_factory)
    assert ticket.message == "добрий день"


async def test_lone_surrogate_in_message_does_not_lose_ticket(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    body = (
        b'{"event_id":"e","ticket_id":"t","customer_email":"a@b.c",'
        b'"message":"hi\\ud800!","reply_url":"https://h.test/r"}'
    )

    response = await client.post(URL, content=body, headers={"content-type": "application/json"})

    assert response.status_code == 200
    [ticket] = await tickets(session_factory)
    assert ticket.message == "hi!"


async def test_lone_surrogate_in_url_is_a_client_error_not_a_crash(
    client: httpx.AsyncClient,
) -> None:
    body = (
        b'{"event_id":"e","ticket_id":"t","customer_email":"a@b.c",'
        b'"message":"hi","reply_url":"https://h.test/\\ud800"}'
    )

    response = await client.post(URL, content=body, headers={"content-type": "application/json"})

    assert response.status_code == 422


async def test_validation_error_does_not_echo_input(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json=payload(reply_url="not a url"))

    assert response.status_code == 422
    assert "user@example.com" not in response.text
    assert "not a url" not in response.text


async def test_declared_oversized_body_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, content=b"x" * 1_048_577)

    assert response.status_code == 413


async def test_oversized_streamed_body_is_rejected(client: httpx.AsyncClient) -> None:
    async def stream() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * 524_288

    response = await client.post(URL, content=stream())

    assert response.status_code == 413


async def test_status_constraint_rejects_unknown_value(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            Ticket(
                event_id="e",
                ticket_id="t",
                customer_email="a@b.c",
                message="m",
                reply_url="https://h.test/r",
                status="bogus",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_unreachable_database_returns_503() -> None:
    application = create_app(Settings(database_url="postgresql+asyncpg://u:p@127.0.0.1:1/none"))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.post(URL, json=payload())
    await application.state.engine.dispose()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


async def test_hanging_database_answers_within_deadline(app: FastAPI) -> None:
    cleanup_finished = asyncio.Event()

    @asynccontextmanager
    async def hanging_session() -> AsyncIterator[None]:
        try:
            await asyncio.Event().wait()
            yield
        finally:
            await asyncio.shield(asyncio.sleep(0.3))
            cleanup_finished.set()

    app.state.session_factory = hanging_session
    app.state.settings = Settings(database_url="unused", webhook_db_timeout_seconds=0.05)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await asyncio.wait_for(http_client.post(URL, json=payload()), timeout=0.25)

    assert response.status_code == 503
    assert not cleanup_finished.is_set()
    await asyncio.wait_for(cleanup_finished.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert app.state.abandoned_writes == set()
