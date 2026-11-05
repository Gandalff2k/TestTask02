import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Ticket, TicketStatus
from app.tickets import claim_for_generation

S = TicketStatus


async def add_ticket(
    factory: async_sessionmaker[AsyncSession],
    status: TicketStatus = S.PENDING_REVIEW,
    message: str = "Не можу експортувати звіт у PDF",
    draft_text: str | None = "Відкрийте звіт і натисніть Експорт.",
    attempts: int = 1,
    event_id: str = "evt_1",
) -> int:
    async with factory() as session:
        ticket = Ticket(
            event_id=event_id,
            ticket_id="T-1042",
            customer_email="user@example.com",
            message=message,
            reply_url="https://helpdesk.example.com/r",
            status=status,
            draft_text=draft_text,
            draft_sources=[
                {"slug": "export-pdf", "title": "Експорт", "url": "http://x/kb/export-pdf"}
            ],
            attempts=attempts,
            last_error="old error",
        )
        session.add(ticket)
        await session.commit()
        return ticket.id


async def reload(factory: async_sessionmaker[AsyncSession], ticket_id: int) -> Ticket:
    async with factory() as session:
        ticket = await session.get(Ticket, ticket_id)
        assert ticket is not None
        return ticket


async def act(
    client: httpx.AsyncClient, ticket_id: int, action: str, **headers: str
) -> httpx.Response:
    return await client.post(f"/tickets/{ticket_id}/{action}", headers=headers)


def result_of(response: httpx.Response) -> str:
    assert response.status_code == 303
    return response.headers["location"].rsplit("result=", 1)[1]


@pytest.mark.parametrize(
    ("action", "start", "end"),
    [
        ("approve", S.PENDING_REVIEW, S.APPROVED),
        ("reject", S.PENDING_REVIEW, S.REJECTED),
        ("regenerate", S.DRAFT_FAILED, S.RECEIVED),
        ("resend", S.SEND_FAILED, S.APPROVED),
        ("resend", S.DELIVERY_UNKNOWN, S.APPROVED),
        ("mark-sent", S.DELIVERY_UNKNOWN, S.SENT),
    ],
)
async def test_allowed_transition(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    action: str,
    start: TicketStatus,
    end: TicketStatus,
) -> None:
    ticket_id = await add_ticket(session_factory, status=start, attempts=4)

    response = await act(client, ticket_id, action)

    assert result_of(response) == "done"
    assert response.headers["location"].startswith(f"/tickets/{ticket_id}?")
    ticket = await reload(session_factory, ticket_id)
    assert ticket.status == end
    assert ticket.attempts == 0
    assert ticket.last_error is None


@pytest.mark.parametrize(
    ("action", "start"),
    [
        ("approve", S.APPROVED),
        ("approve", S.REJECTED),
        ("approve", S.GENERATING),
        ("approve", S.SENT),
        ("reject", S.SENT),
        ("regenerate", S.PENDING_REVIEW),
        ("resend", S.SENT),
        ("resend", S.SENDING),
        ("mark-sent", S.SEND_FAILED),
    ],
)
async def test_disallowed_transition_changes_nothing(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    action: str,
    start: TicketStatus,
) -> None:
    ticket_id = await add_ticket(session_factory, status=start, attempts=4)

    assert result_of(await act(client, ticket_id, action)) == "stale"

    ticket = await reload(session_factory, ticket_id)
    assert ticket.status == start
    assert ticket.attempts == 4


async def test_concurrent_approvals_apply_once(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await add_ticket(session_factory)

    responses = await asyncio.gather(*(act(client, ticket_id, "approve") for _ in range(10)))

    assert sorted(result_of(r) for r in responses) == ["done"] + ["stale"] * 9


async def test_approve_and_reject_race_has_one_winner(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await add_ticket(session_factory)

    responses = await asyncio.gather(
        *(act(client, ticket_id, action) for action in ["approve", "reject"] * 5)
    )

    assert [result_of(r) for r in responses].count("done") == 1
    assert (await reload(session_factory, ticket_id)).status in {S.APPROVED, S.REJECTED}


async def test_regenerated_ticket_goes_back_to_queue(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await add_ticket(session_factory, status=S.DRAFT_FAILED, attempts=5)

    await act(client, ticket_id, "regenerate")

    async with session_factory() as session:
        [claimed] = await claim_for_generation(session, 10, 60.0, 5)
    assert claimed.id == ticket_id
    assert claimed.attempts == 1


async def test_unknown_action_and_ticket(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await add_ticket(session_factory)

    assert (await act(client, ticket_id, "delete")).status_code == 422
    assert (await act(client, 999_999, "approve")).status_code == 404
    assert (await client.get("/tickets/999999")).status_code == 404
    assert (await reload(session_factory, ticket_id)).status == S.PENDING_REVIEW


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
    ],
)
async def test_cross_site_posts_are_rejected(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    headers: dict[str, str],
) -> None:
    ticket_id = await add_ticket(session_factory)

    response = await act(client, ticket_id, "approve", **headers)

    assert response.status_code == 403
    assert (await reload(session_factory, ticket_id)).status == S.PENDING_REVIEW


async def test_same_origin_post_is_accepted(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await add_ticket(session_factory)

    response = await act(
        client, ticket_id, "approve", origin="http://test", **{"sec-fetch-site": "same-origin"}
    )

    assert result_of(response) == "done"


async def test_list_filters_by_status_and_counts(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_ticket(session_factory, status=S.PENDING_REVIEW, event_id="a", message="перше")
    await add_ticket(session_factory, status=S.SENT, event_id="b", message="друге")

    everything = await client.get("/tickets")
    pending = await client.get("/tickets", params={"status": "pending_review"})

    assert everything.status_code == 200
    assert "перше" in everything.text
    assert "друге" in everything.text
    assert "усі (2)" in everything.text
    assert "чекає перевірки (1)" in everything.text
    assert "перше" in pending.text
    assert "друге" not in pending.text
    assert (await client.get("/tickets", params={"status": "bogus"})).status_code == 422


async def test_list_paginates(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    for i in range(51):
        await add_ticket(session_factory, event_id=f"e{i}", message=f"msg-{i:02d}-end")

    first = await client.get("/tickets")
    second = await client.get("/tickets", params={"page": 2})

    assert "наступна" in first.text
    assert first.text.count("-end") == 50
    assert second.text.count("-end") == 1
    assert "msg-00-end" in second.text


async def test_root_redirects_to_tickets(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/tickets"


async def test_detail_shows_actions_for_current_status(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    pending = await add_ticket(session_factory, event_id="a")
    sent = await add_ticket(session_factory, status=S.SENT, event_id="b")

    pending_page = (await client.get(f"/tickets/{pending}")).text
    sent_page = (await client.get(f"/tickets/{sent}")).text

    assert f'action="/tickets/{pending}/approve"' in pending_page
    assert f'action="/tickets/{pending}/reject"' in pending_page
    assert "<form" not in sent_page
    assert "http://x/kb/export-pdf" in pending_page


async def test_detail_escapes_customer_and_model_text(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await add_ticket(
        session_factory,
        message="<script>alert('customer')</script>",
        draft_text="<img src=x onerror=alert('model')>",
    )

    page = (await client.get(f"/tickets/{ticket_id}")).text
    listing = (await client.get("/tickets")).text

    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;" in page
    assert "<script>alert" not in listing


async def test_result_banner_only_accepts_known_values(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await add_ticket(session_factory)

    assert (await client.get(f"/tickets/{ticket_id}?result=<b>x</b>")).status_code == 422
    assert "Статус уже змінився" in (await client.get(f"/tickets/{ticket_id}?result=stale")).text


@pytest.fixture
async def protected_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    app.state.settings = app.state.settings.model_copy(
        update={"operator_password": SecretStr("s3cret")}
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_password_protects_operator_pages_only(
    protected_client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ticket_id = await add_ticket(session_factory)

    assert (await protected_client.get("/tickets")).status_code == 401
    assert (await protected_client.get("/tickets", auth=("operator", "wrong"))).status_code == 401
    assert (await protected_client.get("/tickets", auth=("admin", "s3cret"))).status_code == 401
    assert (await protected_client.post(f"/tickets/{ticket_id}/approve")).status_code == 401
    assert (await protected_client.get("/tickets", auth=("operator", "s3cret"))).status_code == 200
    assert (await protected_client.get("/health")).status_code == 200
    assert (await protected_client.get("/kb")).status_code == 200
    assert (await reload(session_factory, ticket_id)).status == S.PENDING_REVIEW


async def test_unauthorized_response_asks_for_basic_auth(
    protected_client: httpx.AsyncClient,
) -> None:
    response = await protected_client.get("/tickets")

    assert response.headers["www-authenticate"].startswith("Basic")
