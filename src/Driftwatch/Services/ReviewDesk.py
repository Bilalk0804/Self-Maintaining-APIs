"""Local desk for hand-labeling findings from scraped repository scans."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_VERDICTS = frozenset({"true", "false", "unsure"})
_MAX_BYTES = 8_000_000
_MAX_NOTE = 500
_VERDICT_FILE = "verdicts.json"


@dataclass(frozen=True, slots=True)
class ReviewFile:
    """One JSON file discovered in the review directory."""

    name: str
    findings: int
    skipped: int
    error: str | None


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One finding plus the label currently stored for it."""

    key: str
    source: str
    repo: str
    fingerprint: str
    change_type: str
    severity: str
    surface_ref: str
    confidence: float
    detail: str
    renamed_to: str | None
    file_path: str
    start_line: int
    match_kind: str
    symbol_ref: str | None
    snippet: str
    scope: str
    verdict: str | None
    note: str


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    """Counts used to judge whether the matcher is precise enough to email people."""

    findings: int
    true_count: int
    false_count: int
    unsure_count: int
    unlabeled_count: int
    precision: float | None


@dataclass(frozen=True, slots=True)
class ReviewBoard:
    """Everything the review page renders for one directory."""

    root_name: str
    files: tuple[ReviewFile, ...]
    findings: tuple[ReviewFinding, ...]
    summary: ReviewSummary


class ReviewDesk:
    """Read finding lists from a directory and persist hand labels beside them."""

    def __init__(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"review root is not a directory: {root}")
        self.root = resolved
        self._lock = threading.Lock()

    def board(self) -> ReviewBoard:
        """Load every finding file and join it with saved labels.

        Returns:
            The current board. A file that is not a finding list is reported on
            ``files`` and contributes no findings.
        """
        with self._lock:
            verdicts, verdict_error = _read_verdicts(self.root / _VERDICT_FILE)
            files: list[ReviewFile] = []
            findings: list[ReviewFinding] = []
            if verdict_error is not None:
                files.append(ReviewFile(_VERDICT_FILE, 0, 0, verdict_error))
            for path in self._finding_files():
                loaded, skipped, error = _load_finding_file(path)
                files.append(ReviewFile(path.name, len(loaded), skipped, error))
                for item in loaded:
                    label = verdicts.get(item["key"], {})
                    findings.append(_with_label(item, label))
            return ReviewBoard(
                self.root.name,
                tuple(files),
                tuple(findings),
                _summarize(findings),
            )

    def set_verdict(self, key: str, verdict: str | None, note: str) -> None:
        """Store or clear the label for one finding.

        Args:
            key: Finding key returned by :meth:`board`.
            verdict: ``true``, ``false``, ``unsure``, or ``None`` to clear it.
            note: Short reviewer note stored with the label.

        Raises:
            KeyError: The key is not on the current board.
            ValueError: The verdict or the verdict file is unusable.
        """
        if verdict is not None and verdict not in _VERDICTS:
            raise ValueError(f"unsupported verdict {verdict!r}")
        if len(note) > _MAX_NOTE:
            raise ValueError(f"note exceeds {_MAX_NOTE} characters")
        with self._lock:
            known = {item["key"] for item in self._unlabeled_findings()}
            if key not in known:
                raise KeyError(key)
            path = self.root / _VERDICT_FILE
            stored, error = _read_verdicts(path)
            if error is not None:
                raise ValueError(error)
            if verdict is None:
                stored.pop(key, None)
            else:
                stored[key] = {"verdict": verdict, "note": note.strip()}
            _write_verdicts(path, stored)

    def _finding_files(self) -> list[Path]:
        found: list[Path] = []
        for path in sorted(self.root.glob("*.json")):
            if path.name == _VERDICT_FILE or path.is_symlink():
                continue
            resolved = path.resolve()
            if resolved.is_file() and resolved.is_relative_to(self.root):
                found.append(resolved)
        return found

    def _unlabeled_findings(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self._finding_files():
            loaded, _skipped, _error = _load_finding_file(path)
            rows.extend(loaded)
        return rows


def _load_finding_file(path: Path) -> tuple[list[dict[str, Any]], int, str | None]:
    if path.stat().st_size > _MAX_BYTES:
        return [], 0, "file exceeds 8 MB"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], 0, "file is not UTF-8 JSON"
    if not isinstance(payload, list):
        return [], 0, "expected a list of findings"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for index, item in enumerate(payload):
        row = _row_from(item, path.name, index)
        if row is None:
            skipped += 1
            continue
        key = str(row["key"])
        if key in seen:
            key = f"{key}:{index}"
            row["key"] = key
        seen.add(key)
        rows.append(row)
    error = None if rows or not payload else "no finding objects in the list"
    return rows, skipped, error


def _row_from(item: object, source: str, index: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    usage = item.get("usage")
    change_type = item.get("change_type")
    if not isinstance(usage, dict) or not isinstance(change_type, str):
        return None
    file_path = usage.get("file_path")
    if not isinstance(file_path, str):
        return None
    fingerprint = item.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        fingerprint = f"row-{index}"
    try:
        confidence = float(item.get("confidence") or 0)
        start_line = int(usage.get("start_line") or 0)
    except (TypeError, ValueError):
        return None
    renamed = item.get("renamed_to")
    symbol = usage.get("symbol_ref")
    return {
        "key": f"{source}:{fingerprint}",
        "source": source,
        "repo": _repo_name(source),
        "fingerprint": fingerprint,
        "change_type": change_type,
        "severity": _text(item.get("severity")),
        "surface_ref": _text(item.get("surface_ref")),
        "confidence": confidence,
        "detail": _text(item.get("detail")),
        "renamed_to": renamed if isinstance(renamed, str) else None,
        "file_path": file_path,
        "start_line": start_line,
        "match_kind": _text(usage.get("match_kind")),
        "symbol_ref": symbol if isinstance(symbol, str) else None,
        "snippet": _text(usage.get("snippet")),
        "scope": _text(usage.get("scope")),
    }


def _with_label(row: dict[str, Any], label: object) -> ReviewFinding:
    verdict = None
    note = ""
    if isinstance(label, dict) and label.get("verdict") in _VERDICTS:
        verdict = str(label["verdict"])
        raw_note = label.get("note")
        note = raw_note if isinstance(raw_note, str) else ""
    return ReviewFinding(
        key=str(row["key"]),
        source=str(row["source"]),
        repo=str(row["repo"]),
        fingerprint=str(row["fingerprint"]),
        change_type=str(row["change_type"]),
        severity=str(row["severity"]),
        surface_ref=str(row["surface_ref"]),
        confidence=float(row["confidence"]),
        detail=str(row["detail"]),
        renamed_to=row["renamed_to"] if isinstance(row["renamed_to"], str) else None,
        file_path=str(row["file_path"]),
        start_line=int(row["start_line"]),
        match_kind=str(row["match_kind"]),
        symbol_ref=row["symbol_ref"] if isinstance(row["symbol_ref"], str) else None,
        snippet=str(row["snippet"]),
        scope=str(row["scope"]),
        verdict=verdict,
        note=note,
    )


def _summarize(findings: list[ReviewFinding]) -> ReviewSummary:
    true_count = sum(item.verdict == "true" for item in findings)
    false_count = sum(item.verdict == "false" for item in findings)
    unsure_count = sum(item.verdict == "unsure" for item in findings)
    labeled = true_count + false_count
    precision = None if labeled == 0 else round(true_count / labeled, 4)
    return ReviewSummary(
        findings=len(findings),
        true_count=true_count,
        false_count=false_count,
        unsure_count=unsure_count,
        unlabeled_count=len(findings) - true_count - false_count - unsure_count,
        precision=precision,
    )


def _read_verdicts(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    if path.is_symlink() or not path.resolve().is_relative_to(path.parent.resolve()):
        return {}, "verdicts.json escapes the review directory"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, "verdicts.json is not UTF-8 JSON"
    if not isinstance(payload, dict):
        return {}, "verdicts.json must be an object"
    return payload, None


def _write_verdicts(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _repo_name(source: str) -> str:
    name = source
    for suffix in (".findings.json", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
