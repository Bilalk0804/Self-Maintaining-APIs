"""Dashboard, feed, alert, calibration, and usage-metering services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from xml.etree.ElementTree import Element, SubElement, tostring


class GrowthConnection(Protocol):
    """Minimal SQL connection used by dashboard and alert services."""

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        """Execute a parameterized query."""

    def commit(self) -> None:
        """Commit a transaction."""


@dataclass(frozen=True, slots=True)
class ImpactSummary:
    """Dashboard row describing one published change's open blast radius."""

    change_id: str
    provider: str
    change_type: str
    severity: str
    title: str
    deadline_on: str | None
    days_remaining: int | None
    repos_affected: int
    findings_open: int
    best_confidence: float | None


class DashboardService:
    """Read dashboard impact summaries from the canonical database view."""

    def __init__(self, connection: GrowthConnection) -> None:
        self._connection = connection

    def upcoming_impact(self, account_id: str) -> list[ImpactSummary]:
        """Return open published changes ordered by urgency."""
        cursor = self._connection.execute(
            """
                SELECT change_id, provider, change_type, severity, title, deadline_on,
                       days_remaining, repos_affected, findings_open, best_confidence
                FROM v_upcoming_impact
                WHERE account_id = %s
                ORDER BY days_remaining NULLS LAST, severity, title
            """,
            (account_id,),
        )
        return [ImpactSummary(*row) for row in cursor.fetchall()]


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One public or account-scoped change feed item."""

    change_id: str
    title: str
    summary: str
    url: str
    published_at: datetime


def render_rss(title: str, feed_url: str, items: list[FeedItem]) -> bytes:
    """Render a small RSS 2.0 feed with escaped user-facing content."""
    root = Element("rss", {"version": "2.0"})
    channel = SubElement(root, "channel")
    SubElement(channel, "title").text = title
    SubElement(channel, "link").text = feed_url
    SubElement(channel, "description").text = "Driftwatch API change feed"
    for item in items:
        entry = SubElement(channel, "item")
        SubElement(entry, "guid", {"isPermaLink": "false"}).text = item.change_id
        SubElement(entry, "title").text = item.title
        SubElement(entry, "description").text = item.summary
        SubElement(entry, "link").text = item.url
        SubElement(entry, "pubDate").text = item.published_at.astimezone(UTC).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
    return tostring(root, encoding="utf-8", xml_declaration=True)


class AlertService:
    """Create deduplicated alert records and deliver pending records."""

    def __init__(self, connection: GrowthConnection) -> None:
        self._connection = connection

    def enqueue(
        self,
        account_id: str,
        severity: str,
        dedup_key: str,
        *,
        change_id: str | None = None,
        repo_id: str | None = None,
        channel_id: str | None = None,
    ) -> bool:
        """Insert one pending alert, returning false when already present."""
        row = self._connection.execute(
            """
                INSERT INTO alert (account_id, channel_id, change_id, repo_id, severity, dedup_key)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (dedup_key) DO NOTHING
                RETURNING id
            """,
            (account_id, channel_id, change_id, repo_id, severity, dedup_key),
        ).fetchone()
        self._connection.commit()
        return row is not None

    def deliver_pending(
        self,
        deliver: Callable[[str, str], None],
        *,
        limit: int = 50,
    ) -> int:
        """Deliver pending alerts and mark each outcome explicitly."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        cursor = self._connection.execute(
            """
                SELECT id, dedup_key FROM alert
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT %s
            """,
            (limit,),
        )
        delivered = 0
        for alert_id, dedup_key in cursor.fetchall():
            try:
                deliver(str(alert_id), dedup_key)
            except (OSError, RuntimeError, ValueError) as exc:
                self._connection.execute(
                    "UPDATE alert SET status = 'failed', error = %s WHERE id = %s",
                    (str(exc), alert_id),
                )
            else:
                self._connection.execute(
                    "UPDATE alert SET status = 'sent', sent_at = now() WHERE id = %s",
                    (alert_id,),
                )
                delivered += 1
        self._connection.commit()
        return delivered


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Observed precision used to recalibrate a change type."""

    change_type: str
    outcomes: int
    merge_rate: float
    clean_merge_rate: float
    average_predicted: float
    calibrated_confidence: float


def calibrate(
    change_type: str,
    outcomes: list[tuple[float, str]],
    *,
    minimum_observations: int = 5,
) -> CalibrationResult | None:
    """Calculate conservative confidence from observed merge outcomes."""
    if minimum_observations <= 0:
        raise ValueError("minimum_observations must be positive")
    if len(outcomes) < minimum_observations:
        return None
    merged = sum(outcome in {"merged_clean", "merged_with_edits"} for _, outcome in outcomes)
    clean = sum(outcome == "merged_clean" for _, outcome in outcomes)
    average = sum(confidence for confidence, _ in outcomes) / len(outcomes)
    observed = merged / len(outcomes)
    return CalibrationResult(
        change_type=change_type,
        outcomes=len(outcomes),
        merge_rate=observed,
        clean_merge_rate=clean / len(outcomes),
        average_predicted=average,
        calibrated_confidence=min(average, observed),
    )


PLAN_LIMITS = {"free": 100, "pro": 5_000, "team": 25_000, "agency": 100_000}


def check_usage(plan: str, used: int, requested: int = 1) -> int:
    """Return remaining monthly scan units or reject a plan-limit breach."""
    if plan not in PLAN_LIMITS:
        raise ValueError(f"unsupported plan {plan!r}")
    if used < 0 or requested <= 0:
        raise ValueError("usage values are invalid")
    remaining = PLAN_LIMITS[plan] - used
    if requested > remaining:
        raise RuntimeError(f"{plan} plan limit exceeded: {used}+{requested}>{PLAN_LIMITS[plan]}")
    return remaining - requested
