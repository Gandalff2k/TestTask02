import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.base import (
    LLMAuthError,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMRetryableError,
)
from app.llm.keyless import KeylessProvider
from app.models import Ticket, TicketStatus
from app.tickets import (
    ClaimedTicket,
    NewTicket,
    claim_for_generation,
    insert_ticket_if_new,
    save_draft,
)
from app.worker import GenerationWorker, WorkerConfig

GOOD_OUTPUT = json.dumps(
    {"reply": "Відкрийте звіт і натисніть Експорт.", "article_ids": ["export-pdf"]}
)

Reply = str | LLMError | Callable[[], Awaitable[str]]


class FakeProvider:
    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            if isinstance(reply, LLMError):
                raise reply
            if callable(reply):
                return LLMResponse(text=await reply(), model="fake")
            return LLMResponse(text=reply, model="fake")
        finally:
            self.in_flight -= 1


def config(**overrides: object) -> WorkerConfig:
    defaults: dict[str, object] = {
        "concurrency": 30,
        "poll_seconds": 0.02,
        "lease_seconds": 60.0,
        "max_attempts": 3,
        "retry_base_seconds": 5.0,
        "retry_max_seconds": 300.0,
        "public_base_url": "https://support.example.com",
    }
    return WorkerConfig(**(defaults | overrides))  # type: ignore[arg-type]


def worker(
    factory: async_sessionmaker[AsyncSession], provider: object, **overrides: object
) -> GenerationWorker:
    return GenerationWorker(
        factory,
        provider,  # type: ignore[arg-type]
        config(**overrides),
        rng=random.Random(0),  # noqa: S311
    )


async def add_tickets(
    factory: async_sessionmaker[AsyncSession],
    count: int = 1,
    message: str = "Не можу експортувати звіт у PDF",
) -> None:
    async with factory() as session:
        for i in range(count):
            await insert_ticket_if_new(
                session,
                NewTicket(
                    event_id=f"evt_{i}",
                    ticket_id=f"T-{i}",
                    customer_email="user@example.com",
                    message=message,
                    reply_url="https://helpdesk.example.com/replies",
                ),
            )
        await session.commit()


async def claim(
    factory: async_sessionmaker[AsyncSession],
    limit: int = 10,
    lease_seconds: float = 60.0,
    max_attempts: int = 3,
) -> list[ClaimedTicket]:
    async with factory() as session:
        claimed = await claim_for_generation(session, limit, lease_seconds, max_attempts)
        await session.commit()
    return claimed


async def all_tickets(factory: async_sessionmaker[AsyncSession]) -> list[Ticket]:
    async with factory() as session:
        return list((await session.scalars(select(Ticket).order_by(Ticket.id))).all())


async def expire_leases(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE tickets SET locked_until = now() - interval '1s' "
                "WHERE locked_until IS NOT NULL"
            )
        )
        await session.commit()


async def make_due(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(text("UPDATE tickets SET next_attempt_at = now()"))
        await session.commit()


async def run_until(
    generation_worker: GenerationWorker,
    factory: async_sessionmaker[AsyncSession],
    done: Callable[[list[Ticket]], bool],
    timeout: float = 5.0,
) -> list[Ticket]:
    stop = asyncio.Event()
    running = asyncio.create_task(generation_worker.run(stop))
    try:
        async with asyncio.timeout(timeout):
            while not done(tickets := await all_tickets(factory)):
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await running
    return tickets


async def test_claims_are_disjoint_under_concurrency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_tickets(session_factory, 20)

    batches = await asyncio.gather(*(claim(session_factory, limit=6) for _ in range(5)))

    ids = [ticket.id for batch in batches for ticket in batch]
    assert len(ids) == 20
    assert len(set(ids)) == 20


async def test_claim_respects_lease_and_schedule(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_tickets(session_factory, 2)
    [first, second] = await claim(session_factory)
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE tickets SET status = 'received', locked_until = NULL, "
                "next_attempt_at = now() + interval '1h' WHERE id = :id"
            ),
            {"id": second.id},
        )
        await session.commit()

    assert await claim(session_factory) == []

    await expire_leases(session_factory)
    [reclaimed] = await claim(session_factory)
    assert reclaimed.id == first.id
    assert reclaimed.attempts == 2


async def test_stale_owner_cannot_overwrite_reclaimed_ticket(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_tickets(session_factory)
    [stale] = await claim(session_factory)
    await expire_leases(session_factory)
    [current] = await claim(session_factory)

    async with session_factory() as session:
        stale_saved = await save_draft(session, stale, "old", [])
        current_saved = await save_draft(session, current, "new", [])
        await session.commit()

    assert (stale_saved, current_saved) == (False, True)
    [ticket] = await all_tickets(session_factory)
    assert ticket.draft_text == "new"


async def test_repeatedly_abandoned_ticket_fails_instead_of_looping(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_tickets(session_factory)
    for _ in range(3):
        assert len(await claim(session_factory, max_attempts=3)) == 1
        await expire_leases(session_factory)

    assert await claim(session_factory, max_attempts=3) == []
    [ticket] = await all_tickets(session_factory)
    assert ticket.status == TicketStatus.DRAFT_FAILED
    assert ticket.locked_until is None
    assert "lease" in (ticket.last_error or "")


async def test_successful_generation_stores_draft_with_links(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    await add_tickets(session_factory)
    provider = FakeProvider(GOOD_OUTPUT)
    [ticket] = await claim(session_factory)

    await worker(session_factory, provider).process(ticket)

    [stored] = await all_tickets(session_factory)
    assert stored.status == TicketStatus.PENDING_REVIEW
    assert stored.locked_until is None
    assert stored.draft_text is not None
    assert stored.draft_text.startswith("Відкрийте звіт")
    assert "https://support.example.com/kb/export-pdf" in stored.draft_text
    assert stored.draft_sources == [
        {
            "slug": "export-pdf",
            "title": "Як експортувати звіт у PDF",
            "url": "https://support.example.com/kb/export-pdf",
        }
    ]
    sent = json.loads(provider.requests[0].user)
    assert sent["customer_message"] == "Не можу експортувати звіт у PDF"
    assert sent["articles"][0]["id"] == "export-pdf"


async def test_keyless_mode_flows_through_the_same_pipeline(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    await add_tickets(session_factory)
    probe = FakeProvider(LLMAuthError("401"))
    provider = KeylessProvider(probe, 0.0, 0.0)
    [ticket] = await claim(session_factory)

    await worker(session_factory, provider).process(ticket)

    [stored] = await all_tickets(session_factory)
    assert len(probe.requests) == 1
    assert stored.status == TicketStatus.PENDING_REVIEW
    assert stored.draft_sources
    assert stored.draft_sources[0]["slug"] == "export-pdf"


async def test_retryable_error_reschedules_with_retry_after(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    await add_tickets(session_factory)
    [ticket] = await claim(session_factory)

    await worker(session_factory, FakeProvider(LLMRetryableError("429", 30))).process(ticket)

    [stored] = await all_tickets(session_factory)
    assert stored.status == TicketStatus.RECEIVED
    assert stored.locked_until is None
    assert stored.attempts == 1
    assert "429" in (stored.last_error or "")
    delay = (stored.next_attempt_at - datetime.now(UTC)).total_seconds()
    assert delay == pytest.approx(30, abs=2)


async def test_invalid_model_output_is_retried(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    await add_tickets(session_factory)
    [ticket] = await claim(session_factory)

    await worker(session_factory, FakeProvider("not json")).process(ticket)

    [stored] = await all_tickets(session_factory)
    assert stored.status == TicketStatus.RECEIVED
    assert "DraftFormatError" in (stored.last_error or "")


async def test_permanent_error_fails_immediately(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    await add_tickets(session_factory)
    [ticket] = await claim(session_factory)

    await worker(session_factory, FakeProvider(LLMAuthError("bad key"))).process(ticket)

    [stored] = await all_tickets(session_factory)
    assert stored.status == TicketStatus.DRAFT_FAILED
    assert "LLMAuthError" in (stored.last_error or "")


async def test_retries_are_bounded(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    await add_tickets(session_factory)
    generation_worker = worker(session_factory, FakeProvider(LLMRetryableError("503")))

    for _ in range(3):
        [ticket] = await claim(session_factory, max_attempts=3)
        await generation_worker.process(ticket)
        await make_due(session_factory)

    [stored] = await all_tickets(session_factory)
    assert stored.status == TicketStatus.DRAFT_FAILED
    assert stored.attempts == 3
    assert await claim(session_factory) == []


async def test_hanging_provider_is_cut_off_and_retried(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    async def hang() -> str:
        await asyncio.Event().wait()
        return ""

    await add_tickets(session_factory)
    [ticket] = await claim(session_factory, lease_seconds=0.1)

    await asyncio.wait_for(
        worker(session_factory, FakeProvider(hang), lease_seconds=0.1).process(ticket), timeout=1
    )

    [stored] = await all_tickets(session_factory)
    assert stored.status == TicketStatus.RECEIVED
    assert "TimeoutError" in (stored.last_error or "")


async def test_crash_in_pipeline_does_not_kill_worker(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    async def explode() -> str:
        raise ZeroDivisionError

    await add_tickets(session_factory)
    [ticket] = await claim(session_factory)

    await worker(session_factory, FakeProvider(explode)).process(ticket)

    [stored] = await all_tickets(session_factory)
    assert stored.status == TicketStatus.RECEIVED
    assert "ZeroDivisionError" in (stored.last_error or "")


async def test_run_processes_backlog_within_concurrency_limit(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    await add_tickets(session_factory, 25)
    provider = FakeProvider(GOOD_OUTPUT)

    tickets = await run_until(
        worker(session_factory, provider, concurrency=4),
        session_factory,
        lambda ts: all(t.status == TicketStatus.PENDING_REVIEW for t in ts),
    )

    assert len(tickets) == 25
    assert len(provider.requests) == 25
    assert provider.max_in_flight <= 4


async def test_two_workers_never_generate_the_same_ticket(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    await add_tickets(session_factory, 30)
    provider = FakeProvider(GOOD_OUTPUT)
    stop = asyncio.Event()
    workers = [worker(session_factory, provider, concurrency=5) for _ in range(2)]
    running = [asyncio.create_task(w.run(stop)) for w in workers]
    try:
        async with asyncio.timeout(5):
            while any(
                t.status != TicketStatus.PENDING_REVIEW for t in await all_tickets(session_factory)
            ):
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.gather(*running)

    assert len(provider.requests) == 30
    assert {t.attempts for t in await all_tickets(session_factory)} == {1}


async def test_stop_waits_for_in_flight_generation(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> str:
        started.set()
        await release.wait()
        return GOOD_OUTPUT

    await add_tickets(session_factory)
    stop = asyncio.Event()
    running = asyncio.create_task(worker(session_factory, FakeProvider(slow)).run(stop))
    await asyncio.wait_for(started.wait(), timeout=2)

    stop.set()
    await asyncio.sleep(0.05)
    assert not running.done()
    release.set()
    await asyncio.wait_for(running, timeout=2)

    [stored] = await all_tickets(session_factory)
    assert stored.status == TicketStatus.PENDING_REVIEW
