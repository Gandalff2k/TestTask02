import asyncio
import json
import random
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.delivery import (
    DeliveryOutcome,
    is_allowed_url,
    parse_allowed_hosts,
    send_reply,
)
from app.models import Ticket, TicketStatus
from app.tickets import ClaimedDelivery, claim_for_delivery, mark_sent
from app.worker import DeliveryConfig, DeliveryWorker
from mock_helpdesk.main import MockSettings
from mock_helpdesk.main import create_app as create_mock

S = TicketStatus
ALLOWED = frozenset({"helpdesk.test"})
REPLY_URL = "http://helpdesk.test/tickets/T-1/replies"

Handler = Callable[[httpx.Request], httpx.Response]


def config(**overrides: object) -> DeliveryConfig:
    defaults: dict[str, object] = {
        "concurrency": 5,
        "poll_seconds": 0.02,
        "lease_seconds": 60.0,
        "max_attempts": 3,
        "retry_base_seconds": 5.0,
        "retry_max_seconds": 300.0,
        "connect_timeout_seconds": 1.0,
        "read_timeout_seconds": 1.0,
        "allowed_hosts": ALLOWED,
    }
    return DeliveryConfig(**(defaults | overrides))  # type: ignore[arg-type]


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def delivery_worker(
    factory: async_sessionmaker[AsyncSession],
    transport: httpx.AsyncBaseTransport,
    **overrides: object,
) -> DeliveryWorker:
    return DeliveryWorker(
        factory,
        httpx.AsyncClient(transport=transport),
        config(**overrides),
        rng=random.Random(0),  # noqa: S311
    )


async def add_approved(
    factory: async_sessionmaker[AsyncSession],
    count: int = 1,
    reply_url: str = REPLY_URL,
    draft_text: str | None = "Відповідь клієнту",
) -> list[int]:
    ids = []
    async with factory() as session:
        for i in range(count):
            ticket = Ticket(
                event_id=f"evt_{i}",
                ticket_id=f"T-{i}",
                customer_email="user@example.com",
                message="Не можу експортувати звіт",
                reply_url=reply_url.replace("T-1", f"T-{i}"),
                status=S.APPROVED,
                draft_text=draft_text if draft_text is None else f"{draft_text} #{i}",
                attempts=0,
            )
            session.add(ticket)
            await session.flush()
            ids.append(ticket.id)
        await session.commit()
    return ids


async def claim(
    factory: async_sessionmaker[AsyncSession], lease_seconds: float = 60.0
) -> list[ClaimedDelivery]:
    async with factory() as session:
        claimed = await claim_for_delivery(session, 10, lease_seconds)
        await session.commit()
    return claimed


async def all_tickets(factory: async_sessionmaker[AsyncSession]) -> list[Ticket]:
    async with factory() as session:
        return list((await session.scalars(select(Ticket).order_by(Ticket.id))).all())


async def make_due(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(text("UPDATE tickets SET next_attempt_at = now()"))
        await session.commit()


async def deliver_once(
    factory: async_sessionmaker[AsyncSession], handler: Handler, **overrides: object
) -> Ticket:
    [item] = await claim(factory)
    await delivery_worker(factory, httpx.MockTransport(handler), **overrides).process(item)
    [ticket] = await all_tickets(factory)
    return ticket


def test_allowlist() -> None:
    hosts = parse_allowed_hosts(" Mock-Helpdesk , helpdesk.test,")

    assert hosts == {"mock-helpdesk", "helpdesk.test"}
    assert is_allowed_url("http://mock-helpdesk:8001/tickets/1/replies", hosts)
    assert is_allowed_url("https://HELPDESK.test/r", hosts)
    assert not is_allowed_url("http://169.254.169.254/latest/meta-data", hosts)
    assert not is_allowed_url("http://helpdesk.test.evil.com/r", hosts)
    assert not is_allowed_url("http://evil.com/?x=helpdesk.test", hosts)
    assert not is_allowed_url("http://helpdesk.test@evil.com/r", hosts)
    assert not is_allowed_url("file://helpdesk.test/etc/passwd", hosts)


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(201), DeliveryOutcome.DELIVERED),
        (httpx.Response(200), DeliveryOutcome.DELIVERED),
        (httpx.Response(500), DeliveryOutcome.RETRY),
        (httpx.Response(503), DeliveryOutcome.RETRY),
        (httpx.Response(429), DeliveryOutcome.RETRY),
        (httpx.Response(400), DeliveryOutcome.FAILED),
        (httpx.Response(404), DeliveryOutcome.FAILED),
        (httpx.Response(302, headers={"location": "http://evil/"}), DeliveryOutcome.FAILED),
        (httpx.ConnectError("refused"), DeliveryOutcome.RETRY),
        (httpx.ConnectTimeout("slow dns"), DeliveryOutcome.RETRY),
        (httpx.ReadTimeout("no answer"), DeliveryOutcome.UNKNOWN),
        (httpx.WriteTimeout("stalled"), DeliveryOutcome.UNKNOWN),
        (httpx.RemoteProtocolError("closed"), DeliveryOutcome.UNKNOWN),
        (httpx.ReadError("reset"), DeliveryOutcome.UNKNOWN),
    ],
    ids=str,
)
async def test_send_reply_classifies_outcome(
    response: httpx.Response | Exception, outcome: DeliveryOutcome
) -> None:
    recorder = Recorder(response)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))

    result = await send_reply(client, REPLY_URL, "T-1", "текст", ALLOWED, 1.0, 1.0)

    assert result.outcome is outcome
    assert json.loads(recorder.requests[0].content) == {"ticket_id": "T-1", "message": "текст"}


async def test_disallowed_host_is_never_requested() -> None:
    recorder = Recorder(httpx.Response(201))
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))

    result = await send_reply(
        client, "http://169.254.169.254/latest", "T-1", "m", ALLOWED, 1.0, 1.0
    )

    assert result.outcome is DeliveryOutcome.FAILED
    assert recorder.requests == []


async def test_sending_is_committed_before_the_request(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_approved(session_factory)
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        async with session_factory() as session:
            seen.append(await session.scalar(select(Ticket.status)) or "")
        return httpx.Response(201)

    [item] = await claim(session_factory)
    await delivery_worker(session_factory, httpx.MockTransport(handler)).process(item)

    assert seen == [S.SENDING]


async def test_created_marks_sent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await add_approved(session_factory)
    recorder = Recorder(httpx.Response(201))

    ticket = await deliver_once(session_factory, recorder)

    assert ticket.status == S.SENT
    assert ticket.locked_until is None
    assert json.loads(recorder.requests[0].content) == {
        "ticket_id": "T-0",
        "message": "Відповідь клієнту #0",
    }


async def test_server_error_is_retried_then_sent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_approved(session_factory)
    recorder = Recorder(httpx.Response(503, headers={"retry-after": "12"}), httpx.Response(201))
    worker = delivery_worker(session_factory, httpx.MockTransport(recorder))

    [first] = await claim(session_factory)
    await worker.process(first)
    [after_first] = await all_tickets(session_factory)
    await make_due(session_factory)
    [second] = await claim(session_factory)
    await worker.process(second)

    assert after_first.status == S.APPROVED
    assert "503" in (after_first.last_error or "")
    [ticket] = await all_tickets(session_factory)
    assert ticket.status == S.SENT
    assert ticket.attempts == 2
    assert len(recorder.requests) == 2


async def test_read_timeout_is_never_retried(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_approved(session_factory)
    recorder = Recorder(httpx.ReadTimeout("slow"))

    ticket = await deliver_once(session_factory, recorder)

    assert ticket.status == S.DELIVERY_UNKNOWN
    assert await claim(session_factory) == []
    assert len(recorder.requests) == 1


async def test_client_error_fails_without_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_approved(session_factory)

    ticket = await deliver_once(session_factory, Recorder(httpx.Response(422)))

    assert ticket.status == S.SEND_FAILED
    assert "422" in (ticket.last_error or "")


async def test_disallowed_reply_url_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_approved(session_factory, reply_url="https://helpdesk.example.com/tickets/T-1/r")
    recorder = Recorder(httpx.Response(201))

    ticket = await deliver_once(session_factory, recorder)

    assert ticket.status == S.SEND_FAILED
    assert "allowlist" in (ticket.last_error or "")
    assert recorder.requests == []


async def test_missing_draft_fails(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await add_approved(session_factory, draft_text=None)

    ticket = await deliver_once(session_factory, Recorder(httpx.Response(201)))

    assert ticket.status == S.SEND_FAILED


async def test_retries_are_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await add_approved(session_factory)
    worker = delivery_worker(
        session_factory, httpx.MockTransport(Recorder(httpx.ConnectError("down")))
    )

    for _ in range(3):
        [item] = await claim(session_factory)
        await worker.process(item)
        await make_due(session_factory)

    [ticket] = await all_tickets(session_factory)
    assert ticket.status == S.SEND_FAILED
    assert "exhausted" in (ticket.last_error or "")
    assert await claim(session_factory) == []


async def test_abandoned_sending_becomes_unknown_not_resent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_approved(session_factory)
    await claim(session_factory)
    async with session_factory() as session:
        await session.execute(text("UPDATE tickets SET locked_until = now() - interval '1s'"))
        await session.commit()
    recorder = Recorder(httpx.Response(201))
    worker = delivery_worker(session_factory, httpx.MockTransport(recorder))

    async with session_factory() as session:
        claimed = await worker.claim(session, 10)
        await session.commit()

    assert claimed == []
    [ticket] = await all_tickets(session_factory)
    assert ticket.status == S.DELIVERY_UNKNOWN
    assert recorder.requests == []


async def test_stale_sender_cannot_overwrite(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_approved(session_factory)
    [item] = await claim(session_factory)
    async with session_factory() as session:
        await session.execute(text("UPDATE tickets SET status = 'delivery_unknown'"))
        await session.commit()

    async with session_factory() as session:
        assert await mark_sent(session, item) is False
        await session.commit()

    [ticket] = await all_tickets(session_factory)
    assert ticket.status == S.DELIVERY_UNKNOWN


async def test_parallel_workers_send_each_reply_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_approved(session_factory, 30)
    recorder = Recorder(httpx.Response(201))
    transport = httpx.MockTransport(recorder)
    stop = asyncio.Event()
    workers = [delivery_worker(session_factory, transport) for _ in range(3)]
    running = [asyncio.create_task(w.run(stop)) for w in workers]
    try:
        async with asyncio.timeout(5):
            while any(t.status != S.SENT for t in await all_tickets(session_factory)):
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.gather(*running)

    assert len(recorder.requests) == 30
    assert len({r.content for r in recorder.requests}) == 30


async def test_end_to_end_with_flaky_mock_has_no_duplicates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mock = create_mock(
        MockSettings(error_rate=0.5, slow_rate=0, accept_then_slow_rate=0),
        rng=random.Random(1),  # noqa: S311
    )
    await add_approved(session_factory, 20, reply_url="http://helpdesk.test/tickets/T-1/replies")
    worker = delivery_worker(
        session_factory,
        httpx.ASGITransport(app=mock),
        max_attempts=10,
        retry_base_seconds=0.0,
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    try:
        async with asyncio.timeout(10):
            while any(t.status != S.SENT for t in await all_tickets(session_factory)):
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await running

    assert len(mock.state.mock.replies) == 20
    assert mock.state.mock.duplicate_count() == 0
    assert max(t.attempts for t in await all_tickets(session_factory)) > 1
