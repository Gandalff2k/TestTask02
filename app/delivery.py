from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

import httpx


class DeliveryOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRY = "retry"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    outcome: DeliveryOutcome
    detail: str
    retry_after_seconds: float | None = None


_RETRYABLE_STATUSES = {408, 425, 429}


def parse_allowed_hosts(raw: str) -> frozenset[str]:
    return frozenset(host.strip().lower() for host in raw.split(",") if host.strip())


def is_allowed_url(url: str, allowed_hosts: frozenset[str]) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and (parts.hostname or "") in allowed_hosts


def classify_response(response: httpx.Response) -> DeliveryResult:
    status = response.status_code
    if response.is_success:
        return DeliveryResult(DeliveryOutcome.DELIVERED, f"helpdesk returned {status}")
    if status in _RETRYABLE_STATUSES or status >= 500:
        return DeliveryResult(
            DeliveryOutcome.RETRY,
            f"helpdesk returned {status}",
            retry_after_seconds=_parse_retry_after(response),
        )
    return DeliveryResult(DeliveryOutcome.FAILED, f"helpdesk returned {status}")


async def send_reply(
    client: httpx.AsyncClient,
    reply_url: str,
    ticket_id: str,
    message: str,
    allowed_hosts: frozenset[str],
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
) -> DeliveryResult:
    if not is_allowed_url(reply_url, allowed_hosts):
        return DeliveryResult(DeliveryOutcome.FAILED, "reply_url host is not in the allowlist")
    try:
        response = await client.post(
            reply_url,
            json={"ticket_id": ticket_id, "message": message},
            timeout=httpx.Timeout(read_timeout_seconds, connect=connect_timeout_seconds),
            follow_redirects=False,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        return DeliveryResult(DeliveryOutcome.RETRY, f"request not sent: {type(exc).__name__}")
    except (httpx.UnsupportedProtocol, httpx.LocalProtocolError) as exc:
        return DeliveryResult(DeliveryOutcome.FAILED, f"request not sent: {type(exc).__name__}")
    except httpx.TransportError as exc:
        # The request may have reached the helpdesk; resending could duplicate the reply.
        return DeliveryResult(DeliveryOutcome.UNKNOWN, f"no response: {type(exc).__name__}")
    return classify_response(response)


def _parse_retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["retry-after"]))
    except (KeyError, ValueError):
        return None
