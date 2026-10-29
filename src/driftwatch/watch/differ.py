"""Set-diff two surface indexes and classify each delta into a change type.

Classification here is purely mechanical: it reads descriptor transitions, never prose. The
LLM's job downstream is to explain and date a change, not to discover it. That split is what
keeps a hallucinated breaking change out of a customer's inbox.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from driftwatch.surfaces import SurfaceDescriptor, SurfaceIndex, SurfaceRef

Severity = Literal["breaking", "behavioral", "deprecating", "deadline", "additive"]

#: Change types we never auto-fix, regardless of confidence. A silent behavioural change
#: needs a human to decide what the correct behaviour now is.
NEVER_AUTOFIX: frozenset[str] = frozenset({"default_changed", "enum_value_added"})

_SEVERITY_BY_TYPE: dict[str, Severity] = {
    "operation_added": "additive",
    "operation_removed": "breaking",
    "operation_deprecated": "deprecating",
    "param_added_optional": "additive",
    "param_added_required": "breaking",
    "param_removed": "breaking",
    "param_renamed": "breaking",
    "param_type_changed": "breaking",
    "param_deprecated": "deprecating",
    "param_became_required": "breaking",
    "param_became_optional": "additive",
    "response_field_added": "additive",
    "response_field_removed": "breaking",
    "response_field_renamed": "breaking",
    "response_field_type_changed": "breaking",
    "response_field_deprecated": "deprecating",
    "object_added": "additive",
    "object_removed": "breaking",
    "enum_value_added": "behavioral",
    "enum_value_removed": "breaking",
    "default_changed": "behavioral",
    "nullability_changed": "behavioral",
    "doc_changed": "additive",
}


@dataclass(frozen=True, slots=True)
class Delta:
    """One classified difference between two revisions of a surface."""

    surface_ref: str
    change_type: str
    severity: Severity
    op: Literal["added", "removed", "modified"]
    before: SurfaceDescriptor | None
    after: SurfaceDescriptor | None
    changed_attrs: tuple[str, ...] = ()
    #: Set when a removal and an addition were paired into a single rename.
    renamed_to: str | None = None
    detail: str = ""

    @property
    def fingerprint(self) -> str:
        """Content hash used for idempotent persistence and PR dedup."""
        basis = f"{self.surface_ref}|{self.change_type}|{self.renamed_to or ''}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    @property
    def auto_fixable(self) -> bool:
        """Whether a deterministic codemod may be attempted for this change type."""
        return self.change_type not in NEVER_AUTOFIX

    def code_probes(self) -> list[str]:
        """Literal strings worth grepping for to find affected code.

        These drive the matcher's cheap candidate-generation pass, so they must be
        over-inclusive rather than precise.
        """
        probes: list[str] = []
        ref = SurfaceRef.parse(self.surface_ref)
        if ref.subident:
            probes.append(ref.subident)
        if ref.kind == "op" and self.after is not None and self.after.type_sig:
            path = self.after.type_sig.split(" ", 1)[-1]
            probes.append(path.split("{")[0].rstrip("/"))
        elif ref.kind == "op" and self.before is not None and self.before.type_sig:
            path = self.before.type_sig.split(" ", 1)[-1]
            probes.append(path.split("{")[0].rstrip("/"))
        if ref.kind == "model":
            probes.append(ref.ident)
        removed_enum = _removed_enum_values(self.before, self.after)
        probes.extend(removed_enum)
        return [p for p in dict.fromkeys(probes) if p]


def diff_indexes(
    before: SurfaceIndex, after: SurfaceIndex, *, include_additive: bool = False
) -> list[Delta]:
    """Diff two surface indexes.

    Args:
        before: Index of the earlier revision.
        after: Index of the later revision.
        include_additive: Keep purely additive deltas. They are ~90% of volume and are
            suppressed by default, since alerting on them is what makes a tool feel like spam.

    Returns:
        Classified deltas, sorted most severe first.
    """
    before_keys = set(before.entries)
    after_keys = set(after.entries)

    removed = before_keys - after_keys
    added = after_keys - before_keys
    common = before_keys & after_keys

    deltas: list[Delta] = []
    renames = _pair_renames(removed, added, before, after)
    paired = {r for r, _ in renames} | {a for _, a in renames}

    for old_ref, new_ref in renames:
        kind = SurfaceRef.parse(old_ref).subkind or SurfaceRef.parse(old_ref).kind
        change_type = "param_renamed" if kind == "param" else "response_field_renamed"
        deltas.append(
            Delta(
                surface_ref=old_ref,
                change_type=change_type,
                severity=_SEVERITY_BY_TYPE[change_type],
                op="modified",
                before=before.entries[old_ref],
                after=after.entries[new_ref],
                changed_attrs=("name",),
                renamed_to=new_ref,
                detail=f"{_leaf(old_ref)} -> {_leaf(new_ref)}",
            )
        )

    for ref in sorted(removed - paired):
        descriptor = before.entries[ref]
        change_type = _removal_type(ref)
        deltas.append(
            Delta(
                surface_ref=ref,
                change_type=change_type,
                severity=_SEVERITY_BY_TYPE[change_type],
                op="removed",
                before=descriptor,
                after=None,
                detail=f"{_leaf(ref)} no longer exists",
            )
        )

    for ref in sorted(added - paired):
        descriptor = after.entries[ref]
        change_type = _addition_type(ref, descriptor)
        severity = _SEVERITY_BY_TYPE[change_type]
        if severity == "additive" and not include_additive:
            continue
        deltas.append(
            Delta(
                surface_ref=ref,
                change_type=change_type,
                severity=severity,
                op="added",
                before=None,
                after=descriptor,
                detail=f"{_leaf(ref)} is new",
            )
        )

    for ref in sorted(common):
        old = before.entries[ref]
        new = after.entries[ref]
        for delta in _classify_modification(ref, old, new):
            if delta.severity == "additive" and not include_additive:
                continue
            deltas.append(delta)

    order = {"breaking": 0, "deadline": 1, "behavioral": 2, "deprecating": 3, "additive": 4}
    deltas.sort(key=lambda d: (order[d.severity], d.surface_ref))
    return deltas


def _classify_modification(ref: str, old: SurfaceDescriptor, new: SurfaceDescriptor) -> list[Delta]:
    """Classify every meaningful transition between two descriptors of the same surface."""
    if old.shape() == new.shape():
        if old.doc_hash != new.doc_hash:
            return [
                Delta(
                    surface_ref=ref,
                    change_type="doc_changed",
                    severity="additive",
                    op="modified",
                    before=old,
                    after=new,
                    changed_attrs=("doc_hash",),
                    detail="documentation only",
                )
            ]
        return []

    out: list[Delta] = []
    leaf = _leaf(ref)
    is_param = "/param:" in ref

    if not old.deprecated and new.deprecated:
        change_type = (
            "operation_deprecated"
            if new.kind == "op"
            else ("param_deprecated" if is_param else "response_field_deprecated")
        )
        out.append(_mod(ref, change_type, old, new, ("deprecated",), f"{leaf} marked deprecated"))

    if old.required is False and new.required is True:
        change_type = "param_became_required" if is_param else "response_field_type_changed"
        out.append(_mod(ref, change_type, old, new, ("required",), f"{leaf} is now required"))
    elif old.required is True and new.required is False:
        out.append(
            _mod(ref, "param_became_optional", old, new, ("required",), f"{leaf} now optional")
        )

    if old.type_sig != new.type_sig:
        change_type = "param_type_changed" if is_param else "response_field_type_changed"
        out.append(
            _mod(
                ref,
                change_type,
                old,
                new,
                ("type_sig",),
                f"{leaf}: {old.type_sig} -> {new.type_sig}",
            )
        )

    if old.default != new.default:
        out.append(
            _mod(
                ref,
                "default_changed",
                old,
                new,
                ("default",),
                f"{leaf} default {old.default!r} -> {new.default!r}",
            )
        )

    old_enum = set(old.enum_values or ())
    new_enum = set(new.enum_values or ())
    if old_enum - new_enum:
        out.append(
            _mod(
                ref,
                "enum_value_removed",
                old,
                new,
                ("enum_values",),
                f"{leaf} dropped {sorted(old_enum - new_enum)}",
            )
        )
    if new_enum - old_enum:
        out.append(
            _mod(
                ref,
                "enum_value_added",
                old,
                new,
                ("enum_values",),
                f"{leaf} gained {sorted(new_enum - old_enum)}",
            )
        )

    if old.nullable != new.nullable and old.type_sig == new.type_sig:
        out.append(
            _mod(
                ref,
                "nullability_changed",
                old,
                new,
                ("nullable",),
                f"{leaf} nullable {old.nullable} -> {new.nullable}",
            )
        )

    return out


def _mod(
    ref: str,
    change_type: str,
    old: SurfaceDescriptor,
    new: SurfaceDescriptor,
    attrs: tuple[str, ...],
    detail: str,
) -> Delta:
    """Construct a ``modified`` delta with the severity implied by its change type."""
    return Delta(
        surface_ref=ref,
        change_type=change_type,
        severity=_SEVERITY_BY_TYPE[change_type],
        op="modified",
        before=old,
        after=new,
        changed_attrs=attrs,
        detail=detail,
    )


def _pair_renames(
    removed: set[str], added: set[str], before: SurfaceIndex, after: SurfaceIndex
) -> list[tuple[str, str]]:
    """Pair a removal with an addition on the same parent when the shape is identical.

    A rename looks like a removal plus an addition, and reporting it as two unrelated
    breaking changes both doubles the noise and loses the one fact that makes it
    auto-fixable: the new name.
    """
    by_parent_removed: dict[str, list[str]] = {}
    for ref in removed:
        parsed = SurfaceRef.parse(ref)
        if parsed.subident is None:
            continue
        by_parent_removed.setdefault(parsed.parent, []).append(ref)

    pairs: list[tuple[str, str]] = []
    used_added: set[str] = set()
    for ref in sorted(added):
        parsed = SurfaceRef.parse(ref)
        if parsed.subident is None:
            continue
        candidates = by_parent_removed.get(parsed.parent, [])
        new_shape = after.entries[ref].shape()
        exact = [
            c for c in candidates if c not in used_added and before.entries[c].shape() == new_shape
        ]
        # Only an unambiguous single match counts; two same-shaped candidates could pair either
        # way, and a wrong pairing produces a confident, wrong codemod.
        if len(exact) == 1:
            pairs.append((exact[0], ref))
            used_added.add(ref)
            by_parent_removed[parsed.parent] = [c for c in candidates if c != exact[0]]
    return pairs


def _removal_type(ref: str) -> str:
    """Return the change type for a disappeared surface."""
    parsed = SurfaceRef.parse(ref)
    if parsed.subkind == "param":
        return "param_removed"
    if parsed.subkind == "field":
        return "response_field_removed"
    if parsed.kind == "op":
        return "operation_removed"
    return "object_removed"


def _addition_type(ref: str, descriptor: SurfaceDescriptor) -> str:
    """Return the change type for a newly appeared surface."""
    parsed = SurfaceRef.parse(ref)
    if parsed.subkind == "param":
        return "param_added_required" if descriptor.required else "param_added_optional"
    if parsed.subkind == "field":
        return "response_field_added"
    if parsed.kind == "op":
        return "operation_added"
    return "object_added"


def _removed_enum_values(
    before: SurfaceDescriptor | None, after: SurfaceDescriptor | None
) -> list[str]:
    """Return enum values present before but absent after."""
    if before is None or after is None:
        return []
    return sorted(set(before.enum_values or ()) - set(after.enum_values or ()))


def _leaf(ref: str) -> str:
    """Return the most specific identifier in a surface ref, for human-readable detail."""
    parsed = SurfaceRef.parse(ref)
    return parsed.subident or parsed.ident
