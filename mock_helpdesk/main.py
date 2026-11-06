import asyncio
import logging
import random
import re
import secrets
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

_URL = re.compile(r"https?://[^\s<>\"']+")


def _anchor(match: re.Match[str]) -> str:
    url = match.group(0)
    return f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'


def linkify(text: str) -> Markup:
    # The text is escaped first, so only the anchors added here are raw HTML.
    return Markup(_URL.sub(_anchor, str(escape(text))))  # noqa: S704


templates.env.filters["linkify"] = linkify

WEBHOOK_TIMEOUT_SECONDS = 2.0
WEBHOOK_ATTEMPTS = 3
JOURNAL_LIMIT = 200


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_", extra="ignore")

    error_rate: float = Field(default=0.2, ge=0, le=1)
    slow_rate: float = Field(default=0.05, ge=0, le=1)
    accept_then_slow_rate: float = Field(default=0.05, ge=0, le=1)
    slow_seconds: float = 15.0
    service_webhook_url: str = "http://api:8000/webhooks/helpdesk"
    reply_base_url: str = "http://mock-helpdesk:8001"


@dataclass(frozen=True, slots=True)
class ReceivedReply:
    received_at: datetime
    ticket_id: str
    message: str
    behaviour: str


@dataclass(frozen=True, slots=True)
class SentEvent:
    sent_at: datetime
    event_id: str
    ticket_id: str
    message: str
    outcome: str


@dataclass(slots=True)
class MockState:
    rng: random.Random
    replies: list[ReceivedReply] = field(default_factory=list)
    events: list[SentEvent] = field(default_factory=list)

    def duplicate_count(self) -> int:
        counts = Counter((r.ticket_id, r.message) for r in self.replies)
        return sum(count - 1 for count in counts.values())


class ReplyBody(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1, max_length=50_000)


def create_app(settings: MockSettings, rng: random.Random | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        await application.state.client.aclose()

    application = FastAPI(title="Mock helpdesk", lifespan=lifespan)
    application.state.settings = settings
    application.state.mock = MockState(rng=rng or random.Random())  # noqa: S311
    application.state.client = httpx.AsyncClient()

    @application.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "OK"

    @application.post("/tickets/{ticket_id}/replies", status_code=201)
    async def receive_reply(ticket_id: str, body: ReplyBody, request: Request) -> JSONResponse:
        config: MockSettings = request.app.state.settings
        state: MockState = request.app.state.mock
        if body.ticket_id != ticket_id:
            return JSONResponse({"error": "ticket_id mismatch"}, status_code=400)

        roll = state.rng.random()
        if roll < config.error_rate:
            logger.info("reply_rejected_503 ticket_id=%r", ticket_id)
            return JSONResponse({"error": "temporarily unavailable"}, status_code=503)
        roll -= config.error_rate
        if roll < config.slow_rate:
            logger.info("reply_hung_without_accepting ticket_id=%r", ticket_id)
            await asyncio.sleep(config.slow_seconds)
            return JSONResponse({"error": "timed out"}, status_code=504)
        roll -= config.slow_rate

        accepted_slowly = roll < config.accept_then_slow_rate
        behaviour = "accepted, answered late" if accepted_slowly else "accepted"
        state.replies.append(ReceivedReply(datetime.now(UTC), ticket_id, body.message, behaviour))
        logger.info("reply_accepted ticket_id=%r late=%s", ticket_id, accepted_slowly)
        if accepted_slowly:
            await asyncio.sleep(config.slow_seconds)
        return JSONResponse({"status": "created"}, status_code=201)

    @application.get("/replies")
    async def replies(request: Request) -> dict[str, object]:
        state: MockState = request.app.state.mock
        return {
            "total": len(state.replies),
            "duplicates": state.duplicate_count(),
            "replies": [
                {
                    "received_at": r.received_at.isoformat(),
                    "ticket_id": r.ticket_id,
                    "message": r.message,
                    "behaviour": r.behaviour,
                }
                for r in state.replies
            ],
        }

    @application.delete("/replies", status_code=204)
    async def reset(request: Request) -> None:
        state: MockState = request.app.state.mock
        state.replies.clear()
        state.events.clear()

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        state: MockState = request.app.state.mock
        counts = Counter((r.ticket_id, r.message) for r in state.replies)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "settings": request.app.state.settings,
                "replies": list(reversed(state.replies[-JOURNAL_LIMIT:])),
                "events": list(reversed(state.events[-JOURNAL_LIMIT:])),
                "duplicates": state.duplicate_count(),
                "counts": counts,
            },
        )

    @application.post("/tickets")
    async def create_ticket(
        request: Request,
        message: Annotated[str, Form(min_length=1, max_length=5000)],
        customer_email: Annotated[str, Form(max_length=320)] = "customer@example.com",
        send_twice: Annotated[bool, Form()] = False,
    ) -> RedirectResponse:
        config: MockSettings = request.app.state.settings
        state: MockState = request.app.state.mock
        ticket_id = f"T-{secrets.randbelow(900_000) + 100_000}"
        event_id = f"evt_{secrets.token_hex(6)}"
        payload = {
            "event_id": event_id,
            "ticket_id": ticket_id,
            "customer_email": customer_email,
            "message": message,
            "reply_url": f"{config.reply_base_url.rstrip('/')}/tickets/{ticket_id}/replies",
        }
        copies = 2 if send_twice else 1
        outcomes = await asyncio.gather(
            *(deliver_webhook(request.app.state.client, config, payload) for _ in range(copies))
        )
        for outcome in outcomes:
            state.events.append(SentEvent(datetime.now(UTC), event_id, ticket_id, message, outcome))
        return RedirectResponse("/", status_code=303)

    return application


async def deliver_webhook(
    client: httpx.AsyncClient, config: MockSettings, payload: dict[str, str]
) -> str:
    outcomes: list[str] = []
    for attempt in range(1, WEBHOOK_ATTEMPTS + 1):
        try:
            response = await client.post(
                config.service_webhook_url, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            outcomes.append(f"#{attempt}: {type(exc).__name__}")
        else:
            body = response.text[:100]
            outcomes.append(f"#{attempt}: {response.status_code} {body}")
            if response.is_success:
                break
        await asyncio.sleep(0.5 * attempt)
    return "; ".join(outcomes)


app = create_app(MockSettings())
