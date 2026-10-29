"""Deterministic codemods, each targeting one change type.

A codemod plans byte-range edits from a finding's own resolved location. It never searches
the file again, so it cannot drift onto a different occurrence than the one that was matched
and scored.
"""

from __future__ import annotations

from typing import Protocol

from driftwatch import tsutil
from driftwatch.fix.splice import Edit
from driftwatch.match.scorer import Finding
from driftwatch.surfaces import SurfaceRef


class Codemod(Protocol):
    """Plans edits for one class of change."""

    name: str
    change_types: frozenset[str]

    def plan(self, finding: Finding, src: bytes) -> list[Edit]:
        """Return the edits needed to migrate one finding, or an empty list if unsupported."""
        ...


class RenameKeywordArgument:
    """Rename a keyword argument at a resolved call site.

    Rewrites only the argument's name token, so the value expression -- however long or
    multi-line -- is preserved byte for byte.
    """

    name = "rename_keyword_argument"
    change_types = frozenset({"param_renamed"})

    def plan(self, finding: Finding, src: bytes) -> list[Edit]:
        """Plan a single edit renaming the keyword argument."""
        if finding.usage.match_kind != "param_keyword" or finding.renamed_to is None:
            return []
        old_name = SurfaceRef.parse(finding.surface_ref).subident
        new_name = SurfaceRef.parse(finding.renamed_to).subident
        if not old_name or not new_name:
            return []

        node = _node_at(src, finding.usage.start_byte, finding.usage.end_byte, "keyword_argument")
        if node is None:
            return []
        name_node = node.child_by_field_name("name")
        if name_node is None or tsutil.text(name_node, src) != old_name:
            return []
        return [
            Edit(
                start_byte=name_node.start_byte,
                end_byte=name_node.end_byte,
                replacement=new_name.encode("utf-8"),
                rationale=f"rename keyword argument {old_name} -> {new_name}",
            )
        ]


class RenameDictKey:
    """Rename a string key in a dict literal passed to a raw HTTP call."""

    name = "rename_dict_key"
    change_types = frozenset({"param_renamed"})

    def plan(self, finding: Finding, src: bytes) -> list[Edit]:
        """Plan a single edit renaming the dict key, preserving its original quote style."""
        if finding.usage.match_kind != "param_dict_key" or finding.renamed_to is None:
            return []
        old_name = SurfaceRef.parse(finding.surface_ref).subident
        new_name = SurfaceRef.parse(finding.renamed_to).subident
        if not old_name or not new_name:
            return []

        node = _node_at(src, finding.usage.start_byte, finding.usage.end_byte, "pair")
        if node is None:
            return []
        key_node = node.child_by_field_name("key")
        if key_node is None or tsutil.string_value(key_node, src) != old_name:
            return []
        quote = tsutil.text(key_node, src)[:1]
        return [
            Edit(
                start_byte=key_node.start_byte,
                end_byte=key_node.end_byte,
                replacement=f"{quote}{new_name}{quote}".encode(),
                rationale=f"rename dict key {old_name} -> {new_name}",
            )
        ]


class ReplaceStringLiteral:
    """Swap a deprecated string-valued identifier, such as a retired model id."""

    name = "replace_string_literal"
    change_types = frozenset({"model_deprecated", "enum_value_removed"})

    def __init__(self, replacements: dict[str, str]) -> None:
        """Store the ``{old_value: new_value}`` mapping this codemod may apply."""
        self._replacements = replacements

    def plan(self, finding: Finding, src: bytes) -> list[Edit]:
        """Plan a single edit replacing the literal, preserving its quote style."""
        if finding.usage.match_kind != "string_literal":
            return []
        node = _node_at(src, finding.usage.start_byte, finding.usage.end_byte, "string")
        if node is None:
            return []
        current = tsutil.string_value(node, src)
        if current is None:
            return []
        new_value = self._replacements.get(current)
        if new_value is None:
            return []
        quote = tsutil.text(node, src)[:1]
        if quote not in ("'", '"'):
            return []
        return [
            Edit(
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                replacement=f"{quote}{new_value}{quote}".encode(),
                rationale=f"replace {current!r} with {new_value!r}",
            )
        ]


def default_registry(replacements: dict[str, str] | None = None) -> list[Codemod]:
    """Return the built-in codemods, highest precision first."""
    return [
        RenameKeywordArgument(),
        RenameDictKey(),
        ReplaceStringLiteral(replacements or {}),
    ]


def plan_edits(finding: Finding, src: bytes, registry: list[Codemod]) -> tuple[str, list[Edit]]:
    """Find the first codemod that can migrate a finding.

    Returns:
        ``(codemod_name, edits)``, or ``("", [])`` when no codemod applies. An empty result is
        a normal outcome: the finding becomes an alert or an issue instead of a pull request.
    """
    for codemod in registry:
        if finding.change_type not in codemod.change_types:
            continue
        edits = codemod.plan(finding, src)
        if edits:
            return codemod.name, edits
    return "", []


def _node_at(src: bytes, start: int, end: int, node_type: str):
    """Return the node of ``node_type`` covering a byte range, searching outwards.

    The scorer records the range of the matched node, but a codemod sometimes needs its
    parent (the ``pair`` around a dict key, say), so walk up until the type matches.
    """
    tree = tsutil.parse(src, "python")
    node = tree.root_node.descendant_for_byte_range(start, max(start, end - 1))
    while node is not None and node.type != node_type:
        node = node.parent
    return node
