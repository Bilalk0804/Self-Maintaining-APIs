"""Tests for the unattended provider source watcher."""

from __future__ import annotations

from datetime import UTC, datetime

from Driftwatch.Services.AutonomousWatch import AutonomousWatch, WatchStatus


class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple]:
        return self.rows


class Connection:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.queries: list[str] = []
        self.commits = 0

    def execute(self, query: str, params: tuple = ()) -> Result:
        self.queries.append(query)
        return Result(self.rows)

    def commit(self) -> None:
        self.commits += 1


class Cycle:
    def __init__(self, worked: bool) -> None:
        self.worked = worked
        self.calls = 0

    def run_once(self) -> bool:
        self.calls += 1
        return self.worked


def test_cycle_polls_before_it_indexes() -> None:
    order: list[str] = []

    class Ordered:
        def __init__(self, name: str, worked: bool) -> None:
            self.name = name
            self.worked = worked

        def run_once(self) -> bool:
            order.append(self.name)
            return self.worked

    watch = AutonomousWatch(
        Ordered("poll", True), Ordered("index", False), WatchStatus(Connection([]))
    )

    assert watch.run_once() is True
    assert order == ["poll", "index"]


def test_idle_cycle_reports_no_work() -> None:
    watch = AutonomousWatch(Cycle(False), Cycle(False), WatchStatus(Connection([])))

    assert watch.run_once() is False


def test_status_maps_the_latest_poll_and_index() -> None:
    polled = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    connection = Connection(
        [
            (
                "paypal",
                "openapi",
                "https://github.com/paypal/paypal-rest-api-specifications",
                900,
                polled,
                None,
                2,
                "changed",
                None,
                "built",
                40,
            )
        ]
    )
    board = WatchStatus(connection).load()
    source = board.sources[0]

    assert "api_source" in connection.queries[0]
    assert connection.commits == 1
    assert source.provider == "paypal"
    assert source.last_status == "changed"
    assert source.last_polled_at == polled.isoformat()
    assert source.next_poll_at is None
    assert source.consecutive_failures == 2
    assert source.indexed_entries == 40
