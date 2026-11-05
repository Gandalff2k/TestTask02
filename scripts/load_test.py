import argparse
import asyncio
import json
import random
import secrets
import statistics
import time
from collections import Counter
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
from sqlalchemy.engine import make_url

from app.config import get_settings

MESSAGES = [
    "Не можу експортувати звіт у PDF",
    "Забув пароль, не можу зайти в акаунт",
    "Як додати колегу в команду?",
    "Скільки коштує тариф для команди з 5 людей?",
    "Excel показує незрозумілі символи замість букв у CSV",
    "Не приходить код підтвердження на телефон",
    "Дані з Google таблиці не оновлюються",
    "Щотижнева розсилка звіту не приходить на пошту",
    "Хочу повернути гроші за підписку",
    "Коли у вас відкриється офіс у Львові?",
]

HELPDESK_TIMEOUT_SECONDS = 2.0
HELPDESK_MAX_ATTEMPTS = 12
HELPDESK_MAX_BACKOFF_SECONDS = 10.0


@dataclass(slots=True)
class Stats:
    latencies: list[float] = field(default_factory=list)
    outcomes: Counter[str] = field(default_factory=Counter)
    acknowledged: set[str] = field(default_factory=set)
    gave_up: set[str] = field(default_factory=set)


async def deliver(
    client: httpx.AsyncClient, base_url: str, payload: dict[str, str], stats: Stats
) -> None:
    for attempt in range(HELPDESK_MAX_ATTEMPTS):
        started = time.monotonic()
        try:
            response = await client.post(
                f"{base_url}/webhooks/helpdesk", json=payload, timeout=HELPDESK_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            stats.outcomes[type(exc).__name__] += 1
        else:
            stats.latencies.append(time.monotonic() - started)
            stats.outcomes[str(response.status_code)] += 1
            if response.is_success:
                stats.acknowledged.add(payload["event_id"])
                return
        await asyncio.sleep(min(2.0**attempt, HELPDESK_MAX_BACKOFF_SECONDS))
    stats.gave_up.add(payload["event_id"])


async def send_events(args: argparse.Namespace, run_id: str, stats: Stats) -> int:
    rng = random.Random(args.seed)  # noqa: S311
    interval = args.duration / args.events
    started = time.monotonic()
    tasks: list[asyncio.Task[None]] = []
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=200)) as client:
        for i in range(args.events):
            ticket_id = f"{run_id}-{i}"
            payload = {
                "event_id": f"evt_{run_id}_{i}",
                "ticket_id": ticket_id,
                "customer_email": f"user{i}@example.com",
                "message": rng.choice(MESSAGES),
                "reply_url": f"{args.mock_url}/tickets/{ticket_id}/replies",
            }
            copies = 3 if rng.random() < args.concurrent_rate else 1
            tasks += [
                asyncio.create_task(deliver(client, args.base_url, payload, stats))
                for _ in range(copies)
            ]
            if rng.random() < args.duplicate_rate:
                tasks.append(
                    asyncio.create_task(
                        delayed(rng.uniform(1, 30), deliver(client, args.base_url, payload, stats))
                    )
                )
            await asyncio.sleep(max(0.0, started + (i + 1) * interval - time.monotonic()))
            if (i + 1) % 50 == 0:
                print(
                    f"[{time.monotonic() - started:6.0f}s] sent {i + 1}/{args.events}", flush=True
                )
        await asyncio.gather(*tasks)
    return len(tasks)


async def delayed(seconds: float, coroutine: Coroutine[Any, Any, None]) -> None:
    await asyncio.sleep(seconds)
    await coroutine


async def status_counts(connection: asyncpg.Connection, run_id: str) -> dict[str, int]:
    rows = await connection.fetch(
        "SELECT status, count(*) AS n FROM tickets WHERE event_id LIKE $1 GROUP BY status",
        f"evt_{run_id}_%",
    )
    return {row["status"]: row["n"] for row in rows}


async def wait_until(
    connection: asyncpg.Connection,
    run_id: str,
    busy: set[str],
    limit_seconds: float,
    label: str,
) -> tuple[dict[str, int], float]:
    started = time.monotonic()
    while True:
        counts = await status_counts(connection, run_id)
        in_progress = sum(n for status, n in counts.items() if status in busy)
        elapsed = time.monotonic() - started
        print(f"[{label} {elapsed:6.0f}s] {dict(sorted(counts.items()))}", flush=True)
        if in_progress == 0 or elapsed > limit_seconds:
            return counts, elapsed
        await asyncio.sleep(10)


async def approve_all(connection: asyncpg.Connection, base_url: str, run_id: str) -> Counter[str]:
    ids = [
        row["id"]
        for row in await connection.fetch(
            "SELECT id FROM tickets WHERE event_id LIKE $1 AND status = 'pending_review'",
            f"evt_{run_id}_%",
        )
    ]
    results: Counter[str] = Counter()
    semaphore = asyncio.Semaphore(20)

    async def approve(client: httpx.AsyncClient, ticket_id: int) -> None:
        async with semaphore:
            response = await client.post(f"{base_url}/tickets/{ticket_id}/approve")
            results[response.headers.get("location", "").rsplit("=", 1)[-1]] += 1

    async with httpx.AsyncClient(timeout=10) as client:
        await asyncio.gather(*(approve(client, i) for i in ids for _ in range(2)))
    return results


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


async def main(args: argparse.Namespace) -> None:
    run_id = args.run_id or f"L{secrets.token_hex(3)}"
    stats = Stats()
    print(f"run_id={run_id} events={args.events} duration={args.duration}s", flush=True)

    load_started = time.monotonic()
    requests_planned = await send_events(args, run_id, stats)
    send_seconds = time.monotonic() - load_started

    url = make_url(get_settings().database_url).set(drivername="postgresql")
    connection = await asyncpg.connect(url.render_as_string(hide_password=False))
    try:
        generated, generation_wait = await wait_until(
            connection, run_id, {"received", "generating"}, args.drain_timeout, "generation"
        )
        rows = sum(generated.values())
        summary: dict[str, object] = {
            "run_id": run_id,
            "events": args.events,
            "webhook_deliveries_planned": requests_planned,
            "webhook_requests": sum(stats.outcomes.values()),
            "webhook_outcomes": dict(stats.outcomes),
            "webhook_latency_p50_ms": round(statistics.median(stats.latencies) * 1000, 1),
            "webhook_latency_p99_ms": round(percentile(stats.latencies, 0.99) * 1000, 1),
            "webhook_latency_max_ms": round(max(stats.latencies) * 1000, 1),
            "events_acknowledged": len(stats.acknowledged),
            "events_never_acknowledged": len(stats.gave_up - stats.acknowledged),
            "rows_in_db": rows,
            "acknowledged_but_missing": len(stats.acknowledged) - rows,
            "send_seconds": round(send_seconds),
            "drain_after_send_seconds": round(generation_wait),
            "statuses_after_generation": generated,
        }

        if args.approve:
            summary["approve_results"] = dict(await approve_all(connection, args.base_url, run_id))
            delivered, delivery_wait = await wait_until(
                connection, run_id, {"approved", "sending"}, args.drain_timeout, "delivery"
            )
            async with httpx.AsyncClient(timeout=10) as client:
                journal = (await client.get(f"{args.mock_url}/replies")).json()
            per_ticket = Counter(
                r["ticket_id"] for r in journal["replies"] if r["ticket_id"].startswith(run_id)
            )
            summary |= {
                "delivery_seconds": round(delivery_wait),
                "statuses_after_delivery": delivered,
                "mock_replies_for_run": sum(per_ticket.values()),
                "mock_duplicate_replies_for_run": sum(n - 1 for n in per_ticket.values()),
            }
    finally:
        await connection.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate a helpdesk under peak load.")
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--mock-url", default="http://mock-helpdesk:8001")
    parser.add_argument("--events", type=int, default=500)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--duplicate-rate", type=float, default=0.2)
    parser.add_argument("--concurrent-rate", type=float, default=0.05)
    parser.add_argument("--drain-timeout", type=float, default=1800.0)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
