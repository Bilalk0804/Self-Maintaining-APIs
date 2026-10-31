from __future__ import annotations

from datetime import UTC, datetime

import pytest

from Driftwatch.Services.Growth import (
    AlertService,
    DashboardService,
    FeedItem,
    calibrate,
    check_usage,
    render_rss,
)


class Cursor:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple]:
        return self.rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, *responses: list[tuple]) -> None:
        self.responses = list(responses)
        self.queries: list[str] = []
        self.commits = 0

    def execute(self, query: str, params: tuple = ()) -> Cursor:
        self.queries.append(query)
        return Cursor(self.responses.pop(0) if self.responses else [])

    def commit(self) -> None:
        self.commits += 1


def test_dashboard_reads_impact_view() -> None:
    connection = Connection(
        [
            (
                "change-1",
                "stripe",
                "param_removed",
                "breaking",
                "Source removed",
                None,
                None,
                2,
                3,
                0.9,
            )
        ]
    )

    rows = DashboardService(connection).upcoming_impact("account-1")

    assert rows[0].repos_affected == 2
    assert "v_upcoming_impact" in connection.queries[0]


def test_rss_escapes_feed_content() -> None:
    feed = render_rss(
        "Driftwatch",
        "https://example.test/rss",
        [
            FeedItem(
                "change-1",
                "A <breaking> change",
                "Use & verify",
                "https://example.test/change-1",
                datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
    )

    assert b"&lt;breaking&gt;" in feed
    assert b"Use &amp; verify" in feed


def test_alert_enqueue_is_deduplicated() -> None:
    connection = Connection([("alert-1",)], [])
    service = AlertService(connection)

    assert service.enqueue("account", "breaking", "dedup-1") is True
    assert service.enqueue("account", "breaking", "dedup-1") is False


def test_calibration_requires_observations_and_is_conservative() -> None:
    outcomes = [(0.95, "merged_clean"), (0.9, "closed_wrong"), (0.8, "merged_with_edits")]

    assert calibrate("param_renamed", outcomes, minimum_observations=5) is None
    result = calibrate("param_renamed", outcomes, minimum_observations=3)
    assert result is not None
    assert result.calibrated_confidence == pytest.approx(2 / 3)


def test_usage_meter_enforces_plan_limit() -> None:
    assert check_usage("pro", 4_999, 1) == 0
    with pytest.raises(RuntimeError, match="limit exceeded"):
        check_usage("free", 100, 1)
