"""Resolve TypeScript SDK and raw HTTP usages with tree-sitter."""

from __future__ import annotations

from dataclasses import replace

from Driftwatch import Tsutil as tsutil
from Driftwatch.Mapping.Model import SurfaceMap
from Driftwatch.Match.Resolver import Usage


def resolve_file(src: bytes, file_path: str, smap: SurfaceMap) -> list[Usage]:
    """Resolve TypeScript calls, request keys, and raw provider paths."""
    tree = tsutil.parse(src, "typescript")
    if tsutil.has_errors(tree):
        return []
    usages = _resolve_calls(tree.root_node, src, file_path, smap)
    usages.extend(_resolve_raw_http(tree.root_node, src, file_path, smap))
    return _number_occurrences(usages)


def _resolve_calls(root: object, src: bytes, file_path: str, smap: SurfaceMap) -> list[Usage]:
    """Resolve member calls by matching their final symbol path."""
    out: list[Usage] = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        path = tsutil.member_path(function, src)
        if path is None:
            continue
        matches = [
            entry
            for entry in smap.entries
            if entry.symbol_ref.rsplit(":", 1)[-1] == path
            or entry.symbol_ref.endswith(f".{path}")
        ]
        if not matches:
            continue
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        snippet = tsutil.text(node, src)
        entry = matches[0]
        out.append(
            Usage(
                surface_ref=entry.surface_ref,
                match_kind="operation_call",
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                symbol_ref=entry.symbol_ref,
                snippet=snippet,
                resolution_confidence=entry.confidence,
                scope=_scope(node, src),
                candidate_count=len(matches),
            )
        )
        for argument in arguments.named_children:
            if argument.type != "object":
                continue
            for pair in argument.named_children:
                if pair.type not in ("pair", "property_signature"):
                    continue
                key = pair.child_by_field_name("key")
                if key is None:
                    continue
                name = tsutil.text(key, src).strip("\"'")
                if name not in smap.params_for(entry.surface_ref):
                    continue
                out.append(
                    replace(
                        out[-1],
                        match_kind="param_dict_key",
                        start_byte=pair.start_byte,
                        end_byte=pair.end_byte,
                        start_line=pair.start_point[0] + 1,
                        end_line=pair.end_point[0] + 1,
                        snippet=tsutil.text(pair, src),
                        evidence=(name,),
                    )
                )
    return out


def _resolve_raw_http(root: object, src: bytes, file_path: str, smap: SurfaceMap) -> list[Usage]:
    """Resolve literal provider paths passed to fetch, axios, or client.request."""
    out: list[Usage] = []
    for node in _walk(root):
        if node.type not in ("string", "template_string"):
            continue
        value = tsutil.text(node, src).strip("\"'`")
        if not value.startswith("/"):
            continue
        for entry in smap.entries:
            descriptor = entry.source_locator or ""
            if value != descriptor:
                continue
            out.append(
                Usage(
                    surface_ref=entry.surface_ref,
                    match_kind="raw_http_path",
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                    symbol_ref=None,
                    snippet=tsutil.text(node, src),
                    resolution_confidence=0.8,
                )
            )
    return out


def _scope(node: object, src: bytes) -> str:
    """Return the enclosing TypeScript function name when available."""
    function = tsutil.enclosing_function(node)  # type: ignore[arg-type]
    if function is None:
        return "<module>"
    name = function.child_by_field_name("name")
    return tsutil.text(name, src) if name is not None else "<anonymous>"


def _number_occurrences(usages: list[Usage]) -> list[Usage]:
    """Number identical usages to keep finding fingerprints stable."""
    counts: dict[tuple[str, str, str], int] = {}
    out: list[Usage] = []
    for usage in usages:
        key = (usage.file_path, usage.scope, usage.surface_ref)
        index = counts.get(key, 0)
        counts[key] = index + 1
        out.append(replace(usage, occurrence=index) if index else usage)
    return out


def _walk(node: object):
    """Yield a tree-sitter node and all descendants."""
    yield node
    for child in node.named_children:  # type: ignore[attr-defined]
        yield from _walk(child)
