"""Tests for the local finding-review desk."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from Driftwatch.Cli import app
from Driftwatch.Http.ReviewApp import create_review_app
from Driftwatch.Services.AutonomousWatch import SourceWatch, WatchBoard
from Driftwatch.Services.ReviewDesk import ReviewDesk


def _finding(fingerprint: str = "abc", file_path: str = "app/Payments.py") -> dict:
    return {
        "fingerprint": fingerprint,
        "change_type": "param_renamed",
        "severity": "breaking",
        "surface_ref": "api:stripe/op:post_v1_charges/param:source",
        "confidence": 0.91,
        "detail": "source renamed",
        "renamed_to": "payment_method",
        "usage": {
            "file_path": file_path,
            "start_line": 12,
            "match_kind": "param_keyword",
            "symbol_ref": "py:stripe.Charge.create",
            "snippet": "stripe.Charge.create(source=tok)",
            "scope": "checkout",
        },
    }


def _write(root: Path, name: str, payload: object) -> None:
    (root / name).write_text(json.dumps(payload), encoding="utf-8")


def test_board_loads_findings_and_ignores_other_json(tmp_path: Path) -> None:
    _write(tmp_path, "acme-billing.json", [_finding()])
    _write(tmp_path, "deltas.json", [{"surface_ref": "api:stripe/op:post_v1_charges"}])

    board = ReviewDesk(tmp_path).board()

    assert board.summary.findings == 1
    assert board.findings[0].repo == "acme-billing"
    assert board.findings[0].file_path == "app/Payments.py"
    assert any(item.name == "deltas.json" and item.error for item in board.files)


def test_labels_round_trip_and_precision_ignores_unsure(tmp_path: Path) -> None:
    _write(tmp_path, "one.json", [_finding("one"), _finding("two", "app/Other.py")])
    desk = ReviewDesk(tmp_path)
    true_key = desk.board().findings[0].key
    false_key = desk.board().findings[1].key

    desk.set_verdict(true_key, "true", "real call")
    desk.set_verdict(false_key, "unsure", "")
    summary = desk.board().summary

    assert summary.true_count == 1
    assert summary.unsure_count == 1
    assert summary.precision == 1

    desk.set_verdict(false_key, "false", "decoy")
    labeled = desk.board()
    assert labeled.summary.precision == 0.5
    assert labeled.findings[0].note == "real call"

    desk.set_verdict(true_key, None, "")
    assert desk.board().summary.true_count == 0


def test_unknown_label_and_escaped_symlink_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "reviews"
    root.mkdir()
    outside = tmp_path / "secret.json"
    _write(tmp_path, "secret.json", [_finding("hidden")])
    (root / "linked.json").symlink_to(outside)
    desk = ReviewDesk(root)

    assert desk.board().summary.findings == 0
    with pytest.raises(KeyError):
        desk.set_verdict("linked.json:hidden", "true", "")


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        ReviewDesk(tmp_path / "missing")


def test_review_page_serves_board_and_saves_a_label(tmp_path: Path) -> None:
    _write(tmp_path, "acme.json", [_finding()])
    client = TestClient(create_review_app(ReviewDesk(tmp_path)))

    page = client.get("/")
    assert page.status_code == 200
    assert "Review desk" in page.text
    assert client.get("/assets/styles.css").status_code == 200
    assert client.get("/assets/js/main.js").status_code == 200

    board = client.get("/api/board").json()
    key = board["findings"][0]["key"]
    saved = client.put("/api/verdicts", json={"key": key, "verdict": "true", "note": "checked"})

    assert saved.status_code == 200
    assert saved.json()["summary"]["precision"] == 1
    missing = client.put("/api/verdicts", json={"key": "missing", "verdict": "true"})
    assert missing.status_code == 404


def test_watch_endpoint_stays_disconnected_without_a_backend(tmp_path: Path) -> None:
    client = TestClient(create_review_app(ReviewDesk(tmp_path)))

    assert client.get("/api/watch").json() == {"connected": False, "sources": []}
    assert "Provider search" in client.get("/").text


def test_watch_endpoint_returns_backend_sources(tmp_path: Path) -> None:
    class Watch:
        def status(self) -> WatchBoard:
            return WatchBoard(
                sources=(
                    SourceWatch(
                        "stripe",
                        "openapi",
                        "https://github.com/stripe/openapi",
                        900,
                        None,
                        None,
                        0,
                        "unchanged",
                        None,
                        "built",
                        12,
                    ),
                )
            )

    client = TestClient(create_review_app(ReviewDesk(tmp_path), Watch()))
    body = client.get("/api/watch").json()

    assert body["connected"] is True
    assert body["sources"][0]["provider"] == "stripe"
    assert body["sources"][0]["indexed_entries"] == 12


def test_review_command_reports_a_database_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_database_url: str) -> tuple[object, object]:
        raise ValueError("could not connect to the Driftwatch database")

    monkeypatch.setattr("Driftwatch.Http.ReviewApp._connect_watch", fail)
    result = CliRunner().invoke(
        app,
        ["review", "--root", str(tmp_path), "--database-url", "postgresql://invalid"],
    )

    assert result.exit_code != 0
    assert "could not connect" in result.output


def test_review_command_rejects_a_public_bind(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["review", "--root", str(tmp_path), "--host", "0.0.0.0"],
    )

    assert result.exit_code != 0
    assert "loopback" in result.output
