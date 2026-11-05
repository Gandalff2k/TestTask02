from datetime import datetime, timedelta

from scripts.llm_rate import max_in_window, parse_timestamps
from scripts.load_test import percentile

T0 = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001


def at(*seconds: float) -> list[datetime]:
    return [T0 + timedelta(seconds=s) for s in seconds]


def test_window_is_half_open() -> None:
    assert max_in_window(at(0, 30, 59.999, 60), timedelta(seconds=60)) == 3


def test_window_finds_densest_minute() -> None:
    timestamps = at(*range(0, 120, 2), *[100 + i / 10 for i in range(5)])

    assert max_in_window(sorted(timestamps), timedelta(seconds=60)) == 35


def test_empty_input() -> None:
    assert max_in_window([], timedelta(seconds=60)) == 0


def test_parses_only_marker_lines() -> None:
    lines = [
        "2026-11-05 19:10:10,580 INFO app.llm.rate_limit llm_request_started\n",
        "2026-11-05 19:10:10,767 INFO app.llm.keyless keyless_request_finished\n",
        "2026-11-05 19:10:09,001 INFO app.llm.rate_limit llm_request_started\n",
    ]

    assert parse_timestamps(lines) == [
        datetime(2026, 11, 5, 19, 10, 9, 1000),  # noqa: DTZ001
        datetime(2026, 11, 5, 19, 10, 10, 580000),  # noqa: DTZ001
    ]


def test_percentile() -> None:
    values = [float(i) for i in range(1, 101)]

    assert percentile(values, 0.5) == 51.0
    assert percentile(values, 0.99) == 100.0
