"""Apply byte-range edits to source, and emit a unified diff.

Splicing bytes rather than re-printing a syntax tree is a deliberate trust decision. A
pretty-printed AST round-trip reformats the entire file, so a two-line semantic change
arrives as a four-hundred-line diff that no reviewer reads -- the fastest way for an
automated pull request to be closed unexamined. A splice touches only the bytes it must,
leaving whitespace, comments and import order exactly as the author wrote them.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from Driftwatch import Tsutil as tsutil


class SpliceError(RuntimeError):
    """Raised when a set of edits cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class Edit:
    """A single byte-range replacement."""

    start_byte: int
    end_byte: int
    replacement: bytes
    rationale: str

    def __post_init__(self) -> None:
        if self.start_byte < 0 or self.end_byte < self.start_byte:
            raise ValueError(f"invalid edit range: {self.start_byte}..{self.end_byte}")


def apply_edits(src: bytes, edits: list[Edit]) -> bytes:
    """Apply edits to source bytes.

    Edits are applied from the end of the file backwards so that earlier offsets stay valid.

    Args:
        src: Original file contents.
        edits: Edits to apply. Must not overlap.

    Returns:
        The patched bytes.

    Raises:
        SpliceError: if any edit overlaps another or falls outside the file.
    """
    if not edits:
        return src
    ordered = sorted(edits, key=lambda e: e.start_byte)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.start_byte < previous.end_byte:
            raise SpliceError(
                f"overlapping edits: {previous.start_byte}..{previous.end_byte} "
                f"and {current.start_byte}..{current.end_byte}"
            )
    if ordered[-1].end_byte > len(src):
        raise SpliceError(f"edit ends at {ordered[-1].end_byte}, file is {len(src)} bytes")

    out = src
    for edit in reversed(ordered):
        out = out[: edit.start_byte] + edit.replacement + out[edit.end_byte :]
    return out


def verify_patch(patched: bytes, expected_edits: int, lang: str = "python") -> None:
    """Check that a patched file still parses.

    This is the cheapest rung of the verification ladder and catches most bad patches before
    a container is ever started.

    Raises:
        SpliceError: if the patched source contains syntax errors.
    """
    if expected_edits <= 0:
        return
    tree = tsutil.parse(patched, lang)  # type: ignore[arg-type]
    if tsutil.has_errors(tree):
        raise SpliceError("patched source does not parse cleanly")


def unified_diff(original: bytes, patched: bytes, path: str) -> str:
    """Render a unified diff between two versions of a file."""
    before = original.decode("utf-8", errors="replace").splitlines(keepends=True)
    after = patched.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(before, after, fromfile=f"a/{path}", tofile=f"b/{path}", n=3)
    )


def diff_stats(diff_text: str) -> tuple[int, int]:
    """Return ``(lines_added, lines_removed)`` for a unified diff."""
    added = sum(1 for line in diff_text.splitlines() if line.startswith("+") and line[:3] != "+++")
    removed = sum(
        1 for line in diff_text.splitlines() if line.startswith("-") and line[:3] != "---"
    )
    return added, removed
