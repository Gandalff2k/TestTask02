import asyncio
import json
import random
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI

from mock_helpdesk.main import MockSettings, create_app

REPLY = {"ticket_id": "T-1", "message": "Відповідь"}

ClientFor = Callable[[FastAPI], httpx.AsyncClient]


def mock(**settings: object) -> FastAPI:
    defaults: dict[str, object] = {
        "error_rate": 0.0,
        "slow_rate": 0.0,
        "accept_then_slow_rate": 0.0,
        "slow_seconds": 0.05,
        "service_webhook_url": "http://service.test/webhooks/helpdesk",
        "reply_base_url": "http://mock-helpdesk:8001",
    }
    return create_app(
        MockSettings.model_validate(defaults | settings),
        rng=random.Random(0),  # noqa: S311
    )


@pytest.fixture
async def client_for() -> AsyncIterator[ClientFor]:
    clients: list[httpx.AsyncClient] = []

    def make(app: FastAPI) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m")
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


async def test_accepts_reply_and_records_it(client_for: ClientFor) -> None:
    app = mock()
    client = client_for(app)

    response = await client.post("/tickets/T-1/replies", json=REPLY)

    assert response.status_code == 201
    journal = (await client.get("/replies")).json()
    assert journal["total"] == 1
    assert journal["duplicates"] == 0
    assert journal["replies"][0]["message"] == "Відповідь"


async def test_counts_duplicates(client_for: ClientFor) -> None:
    client = client_for(mock())

    for _ in range(3):
        await client.post("/tickets/T-1/replies", json=REPLY)

    assert (await client.get("/replies")).json()["duplicates"] == 2
    assert "дублікатів: 2" in (await client.get("/")).text


async def test_server_error_does_not_record(client_for: ClientFor) -> None:
    client = client_for(mock(error_rate=1.0))

    response = await client.post("/tickets/T-1/replies", json=REPLY)

    assert response.status_code == 503
    assert (await client.get("/replies")).json()["total"] == 0


async def test_hang_without_accepting(client_for: ClientFor) -> None:
    client = client_for(mock(slow_rate=1.0))

    response = await client.post("/tickets/T-1/replies", json=REPLY)

    assert response.status_code == 504
    assert (await client.get("/replies")).json()["total"] == 0


async def test_accept_then_answer_late(client_for: ClientFor) -> None:
    client = client_for(mock(accept_then_slow_rate=1.0, slow_seconds=0.3))

    request = asyncio.create_task(client.post("/tickets/T-1/replies", json=REPLY))
    await asyncio.sleep(0.1)

    assert not request.done()
    assert (await client.get("/replies")).json()["total"] == 1
    assert (await request).status_code == 201


async def test_rejects_mismatched_ticket(client_for: ClientFor) -> None:
    client = client_for(mock())

    response = await client.post("/tickets/T-2/replies", json=REPLY)

    assert response.status_code == 400


async def test_form_sends_webhook_with_reply_url(client_for: ClientFor) -> None:
    app = mock()
    sent: list[dict[str, str]] = []

    def service(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "accepted"})

    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(service))
    client = client_for(app)

    response = await client.post("/tickets", data={"message": "Не можу експортувати"})

    assert response.status_code == 303
    [payload] = sent
    assert payload["message"] == "Не можу експортувати"
    assert payload["event_id"].startswith("evt_")
    assert payload["reply_url"] == (
        f"http://mock-helpdesk:8001/tickets/{payload['ticket_id']}/replies"
    )
    assert "200" in (await client.get("/")).text


async def test_send_twice_reuses_event_id(client_for: ClientFor) -> None:
    app = mock()
    sent: list[dict[str, str]] = []

    def service(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(service))

    await client_for(app).post("/tickets", data={"message": "m", "send_twice": "true"})

    assert len(sent) == 2
    assert sent[0] == sent[1]


async def test_webhook_is_retried_like_a_real_helpdesk(client_for: ClientFor) -> None:
    app = mock()
    calls: list[int] = []

    def service(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200)

    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(service))

    await client_for(app).post("/tickets", data={"message": "m"})

    assert len(calls) == 2


async def test_reset_clears_journal(client_for: ClientFor) -> None:
    client = client_for(mock())
    await client.post("/tickets/T-1/replies", json=REPLY)

    assert (await client.delete("/replies")).status_code == 204
    assert (await client.get("/replies")).json()["total"] == 0


async def test_journal_escapes_html(client_for: ClientFor) -> None:
    client = client_for(mock())
    await client.post(
        "/tickets/T-1/replies", json={"ticket_id": "T-1", "message": "<script>x</script>"}
    )

    assert "<script>x</script>" not in (await client.get("/")).text
