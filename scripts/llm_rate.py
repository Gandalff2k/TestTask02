import sys
from bisect import bisect_left
from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import pairwise

MARKER = "llm_request_started"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S,%f"


def parse_timestamps(lines: Iterable[str]) -> list[datetime]:
    return sorted(
        datetime.strptime(line[:23], TIMESTAMP_FORMAT)  # noqa: DTZ007
        for line in lines
        if MARKER in line
    )


def max_in_window(timestamps: list[datetime], window: timedelta) -> int:
    return max(
        (bisect_left(timestamps, start + window) - i for i, start in enumerate(timestamps)),
        default=0,
    )


def main() -> None:
    timestamps = parse_timestamps(sys.stdin)
    gaps = [(b - a).total_seconds() for a, b in pairwise(timestamps)]
    print(f"llm_requests={len(timestamps)}")
    print(f"max_in_any_60s={max_in_window(timestamps, timedelta(seconds=60))}")
    if gaps:
        print(f"min_gap_seconds={min(gaps):.3f}")


if __name__ == "__main__":
    main()
