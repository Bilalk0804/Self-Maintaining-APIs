"""Stage 2: resolve code to API surfaces with tree-sitter.

The job is to turn ``client.charges.create(source=tok)`` into
``api:stripe/op:post_v1_charges/param:source``, and to *refuse* to do so for
``billing.charges.create(source=tok)`` where ``billing`` is an in-house class. Text search
cannot tell those apart; that distinction is the entire value of this stage.

Resolution proceeds in four passes over each candidate file:

1. **Imports** build an alias table (``import stripe as pay`` -> ``pay`` means ``stripe``).
2. **Client bindings** extend it (``client = stripe.StripeClient(k)`` -> ``client`` is rooted
   at ``stripe``), including bindings created from a ``from stripe import StripeClient``.
3. **Call sites** flatten their attribute chain, rewrite the root through the alias table, and
   look the result up in the Surface Map. Keyword arguments become parameter usages.
4. **Response reads** use assignment tracking: a variable assigned from a matched call is
   bound to that operation's response object, so ``charge.source`` resolves to a field.

Pass 4 is intra-procedural, assignment-only, and does not merge branches. A response field
reached through a helper's return value is missed. That is a deliberate recall gap: a silent
false positive costs far more trust than a known blind spot.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from driftwatch import tsutil
from driftwatch.mapping.model import SurfaceMap

MatchKind = Literal[
    "operation_call",
    "param_keyword",
    "param_dict_key",
    "response_attribute",
    "response_subscript",
    "string_literal",
    "raw_http_path",
    "version_pin",
]

#: Constructors that produce a provider client, mapped to the SDK root they bind to.
_CLIENT_CONSTRUCTORS: dict[str, str] = {
    "stripe.StripeClient": "stripe",
    "StripeClient": "stripe",
    "openai.OpenAI": "openai",
    "OpenAI": "openai",
    "openai.AsyncOpenAI": "openai",
    "AsyncOpenAI": "openai",
    "razorpay.Client": "razorpay",
    "Client": "razorpay",
}

_IMPORT_Q = """
[
  (import_statement name: (dotted_name) @plain)
  (import_statement (aliased_import name: (dotted_name) @aliased alias: (identifier) @alias))
  (import_from_statement module_name: (dotted_name) @from_module (dotted_name) @from_name)
  (import_from_statement
    module_name: (dotted_name) @from_module
    (aliased_import name: (dotted_name) @from_name alias: (identifier) @from_alias))
]
"""

_ASSIGN_CALL_Q = """
(assignment left: (identifier) @target right: (call function: (_) @callee) @call) @asg
"""

_CALL_Q = "(call function: (_) @callee arguments: (argument_list) @args) @call"

_ATTRIBUTE_READ_Q = "(attribute object: (identifier) @base attribute: (identifier) @attr) @node"
_SUBSCRIPT_READ_Q = """
(subscript value: (identifier) @base subscript: (string) @key) @node
"""
_STRING_Q = "(string) @s"
_PIN_Q = """
(assignment left: (attribute attribute: (identifier) @attr) right: (string) @value) @asg
"""


@dataclass(frozen=True, slots=True)
class Usage:
    """One resolved reference from code to an API surface."""

    surface_ref: str
    match_kind: MatchKind
    file_path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    symbol_ref: str | None
    snippet: str
    #: How certain the resolution itself is, before change-specific scoring.
    resolution_confidence: float
    #: Enclosing function name, or ``"<module>"``. Part of a finding's identity because it is
    #: stable under reformatting, unlike a line number, yet still distinguishes two identical
    #: snippets in different functions.
    scope: str = "<module>"
    #: Index of this usage among identical snippets in the same scope, for the same reason.
    occurrence: int = 0
    #: Number of surfaces the resolved symbol could refer to. Greater than one means the
    #: symbol is genuinely ambiguous in user code, not that the map is wrong.
    candidate_count: int = 1
    evidence: tuple[str, ...] = ()


@dataclass(slots=True)
class _Scope:
    """Alias and binding tables accumulated for one file."""

    #: Local name -> SDK root module (``pay`` -> ``stripe``).
    roots: dict[str, str] = field(default_factory=dict)
    #: Local name -> fully qualified imported symbol (``StripeClient`` -> ``stripe.StripeClient``).
    imported: dict[str, str] = field(default_factory=dict)
    #: Variable -> SDK root, for client instances.
    clients: dict[str, str] = field(default_factory=dict)
    #: Variable -> operation surface ref, for values returned by a matched call.
    responses: dict[str, str] = field(default_factory=dict)


def resolve_file(
    src: bytes,
    file_path: str,
    smap: SurfaceMap,
    *,
    literal_probes: dict[str, str] | None = None,
) -> list[Usage]:
    """Resolve every API surface usage in one Python file.

    Args:
        src: File contents.
        file_path: Repository-relative path, recorded on each usage.
        smap: Surface Map for the provider being matched.
        literal_probes: Optional ``{literal_value: surface_ref}`` for string-valued surfaces
            such as deprecated model identifiers.

    Returns:
        Resolved usages, which the scorer then weighs against a specific change.
    """
    tree = tsutil.parse(src, "python")
    if tsutil.has_errors(tree):
        # A file we cannot parse cleanly gets text-only treatment elsewhere; guessing at a
        # broken parse tree produces wrong line numbers and wrong patches.
        return []

    scope = _Scope()
    _collect_imports(tree.root_node, src, scope)
    _collect_clients(tree.root_node, src, scope)

    by_symbol = smap.by_symbol()
    usages: list[Usage] = []
    usages.extend(_resolve_calls(tree.root_node, src, file_path, scope, smap, by_symbol))
    usages.extend(_resolve_response_reads(tree.root_node, src, file_path, scope, smap))
    usages.extend(_resolve_raw_http(tree.root_node, src, file_path, smap))
    if literal_probes:
        usages.extend(_resolve_literals(tree.root_node, src, file_path, literal_probes))
    usages.extend(_resolve_version_pins(tree.root_node, src, file_path, scope))
    return _number_occurrences(usages)


def _number_occurrences(usages: list[Usage]) -> list[Usage]:
    """Assign an occurrence index to usages that are otherwise indistinguishable."""
    counts: dict[tuple[str, str, str, str], int] = {}
    out: list[Usage] = []
    for usage in usages:
        key = (usage.file_path, usage.scope, usage.surface_ref, " ".join(usage.snippet.split()))
        index = counts.get(key, 0)
        counts[key] = index + 1
        out.append(replace(usage, occurrence=index) if index else usage)
    return out


def _collect_imports(root: object, src: bytes, scope: _Scope) -> None:
    """Populate alias tables from import statements."""
    for m in tsutil.matches(root, _IMPORT_Q, "python"):  # type: ignore[arg-type]
        for node in m.get("plain") or []:
            name = tsutil.text(node, src)
            scope.roots[name.split(".")[0]] = name.split(".")[0]
        aliased = m.get("aliased") or []
        alias = m.get("alias") or []
        if aliased and alias:
            scope.roots[tsutil.text(alias[0], src)] = tsutil.text(aliased[0], src).split(".")[0]
        module_nodes = m.get("from_module") or []
        name_nodes = m.get("from_name") or []
        alias_nodes = m.get("from_alias") or []
        if module_nodes and name_nodes:
            module = tsutil.text(module_nodes[0], src)
            imported = tsutil.text(name_nodes[0], src)
            local = tsutil.text(alias_nodes[0], src) if alias_nodes else imported
            scope.imported[local] = f"{module}.{imported}"


def _collect_clients(root: object, src: bytes, scope: _Scope) -> None:
    """Bind variables assigned from a provider client constructor to that SDK root."""
    for m in tsutil.matches(root, _ASSIGN_CALL_Q, "python"):  # type: ignore[arg-type]
        targets = m.get("target") or []
        callees = m.get("callee") or []
        if not targets or not callees:
            continue
        callee = callees[0]
        path = (
            tsutil.attribute_path(callee, src)
            if callee.type in ("attribute", "identifier")
            else None
        )
        if path is None:
            continue
        resolved = _rewrite_root(path, scope)
        root_module = _CLIENT_CONSTRUCTORS.get(resolved) or _CLIENT_CONSTRUCTORS.get(path)
        if root_module is None:
            # Resolve `from stripe import StripeClient` style constructors too.
            qualified = scope.imported.get(path)
            if qualified:
                root_module = _CLIENT_CONSTRUCTORS.get(qualified)
        if root_module is not None and root_module in scope.roots.values():
            scope.clients[tsutil.text(targets[0], src)] = root_module


def _rewrite_root(dotted: str, scope: _Scope) -> str:
    """Rewrite the first segment of a dotted path through the alias and client tables."""
    head, _, tail = dotted.partition(".")
    if head in scope.clients:
        base = scope.clients[head]
    elif head in scope.roots:
        base = scope.roots[head]
    elif head in scope.imported:
        base = scope.imported[head]
    else:
        return dotted
    return f"{base}.{tail}" if tail else base


def _resolve_calls(
    root: object,
    src: bytes,
    file_path: str,
    scope: _Scope,
    smap: SurfaceMap,
    by_symbol: dict[str, list],
) -> list[Usage]:
    """Resolve call sites to operations, and their keyword arguments to parameters."""
    out: list[Usage] = []
    for m in tsutil.matches(root, _CALL_Q, "python"):  # type: ignore[arg-type]
        callees, args = m.get("callee") or [], m.get("args") or []
        calls = m.get("call") or []
        if not callees or not args or not calls:
            continue
        callee = callees[0]
        if callee.type not in ("attribute", "identifier"):
            continue
        dotted = tsutil.attribute_path(callee, src)
        if dotted is None:
            continue
        # The root must resolve through an import or a client binding. This single check is
        # what rejects `billing.charges.create(...)` on an in-house class.
        head = dotted.split(".")[0]
        if head not in scope.roots and head not in scope.clients and head not in scope.imported:
            continue
        resolved = _rewrite_root(dotted, scope)
        symbol = f"py:{resolved}"
        entries = by_symbol.get(symbol)
        if not entries:
            continue

        surfaces = sorted({e.surface_ref for e in entries})
        ambiguous = len(surfaces) > 1
        # A bare class-qualified symbol such as `SessionService.create` really is ambiguous in
        # user code; report every candidate but mark the resolution as weaker.
        confidence = 0.6 if ambiguous else 1.0
        rooted_via = "client_binding" if head in scope.clients else "import_alias"

        for surface in surfaces:
            out.append(
                _usage(
                    surface,
                    "operation_call",
                    file_path,
                    calls[0],
                    src,
                    symbol,
                    confidence,
                    len(surfaces),
                    (f"root:{head}", f"via:{rooted_via}"),
                )
            )
            known_params = smap.params_for(surface)
            for kw_name, kw_node in _keyword_args(args[0], src):
                # Membership in the SDK's parameter list is a confidence signal, never a
                # filter. A parameter that upstream renamed or removed is precisely the one
                # missing from a newer SDK, so filtering on it would discard every finding
                # for the change types most worth reporting.
                known = not known_params or kw_name in known_params
                out.append(
                    _usage(
                        f"{surface}/param:{kw_name}",
                        "param_keyword",
                        file_path,
                        kw_node,
                        src,
                        symbol,
                        confidence if known else confidence * 0.9,
                        len(surfaces),
                        (f"kwarg:{kw_name}", "in_sdk_params" if known else "absent_from_sdk"),
                    )
                )
    return out


def _keyword_args(args: object, src: bytes) -> list[tuple[str, object]]:
    """Return ``(name, node)`` for each keyword argument of a call."""
    out: list[tuple[str, object]] = []
    for child in args.named_children:  # type: ignore[attr-defined]
        if child.type != "keyword_argument":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is not None:
            out.append((tsutil.text(name_node, src), child))
    return out


def _resolve_response_reads(
    root: object, src: bytes, file_path: str, scope: _Scope, smap: SurfaceMap
) -> list[Usage]:
    """Resolve attribute and subscript reads on values returned by matched calls."""
    by_symbol = smap.by_symbol()
    for m in tsutil.matches(root, _ASSIGN_CALL_Q, "python"):  # type: ignore[arg-type]
        targets, callees = m.get("target") or [], m.get("callee") or []
        if not targets or not callees or callees[0].type not in ("attribute", "identifier"):
            continue
        dotted = tsutil.attribute_path(callees[0], src)
        if dotted is None:
            continue
        head = dotted.split(".")[0]
        if head not in scope.roots and head not in scope.clients and head not in scope.imported:
            continue
        entries = by_symbol.get(f"py:{_rewrite_root(dotted, scope)}")
        if entries and len({e.surface_ref for e in entries}) == 1:
            scope.responses[tsutil.text(targets[0], src)] = entries[0].surface_ref

    out: list[Usage] = []
    read_queries = (
        (_ATTRIBUTE_READ_Q, "response_attribute"),
        (_SUBSCRIPT_READ_Q, "response_subscript"),
    )
    for query, kind in read_queries:
        for m in tsutil.matches(root, query, "python"):  # type: ignore[arg-type]
            bases = m.get("base") or []
            nodes = m.get("node") or []
            if not bases or not nodes:
                continue
            var = tsutil.text(bases[0], src)
            op_surface = scope.responses.get(var)
            if op_surface is None:
                continue
            object_name = smap.returns_for(op_surface)
            if object_name is None:
                continue
            if kind == "response_attribute":
                member = tsutil.text((m.get("attr") or [None])[0], src)
            else:
                member = tsutil.string_value((m.get("key") or [None])[0], src)
            if not member:
                continue
            out.append(
                _usage(
                    f"api:{smap.provider}/obj:{object_name}/field:{member}",
                    kind,  # type: ignore[arg-type]
                    file_path,
                    nodes[0],
                    src,
                    None,
                    0.9,
                    1,
                    (f"response_of:{op_surface}", f"var:{var}"),
                )
            )
    return out


def _resolve_raw_http(root: object, src: bytes, file_path: str, smap: SurfaceMap) -> list[Usage]:
    """Resolve provider endpoints called over raw HTTP via a URL string literal."""
    host = f"api.{smap.provider}.com"
    known_ops = {e.surface_ref for e in smap.entries}
    out: list[Usage] = []
    for m in tsutil.matches(root, _CALL_Q, "python"):  # type: ignore[arg-type]
        callees, args = m.get("callee") or [], m.get("args") or []
        if not callees or not args:
            continue
        dotted = tsutil.attribute_path(callees[0], src) or ""
        verb = dotted.rsplit(".", 1)[-1].lower()
        if verb not in ("get", "post", "put", "patch", "delete"):
            continue
        for child in args[0].named_children:
            url = tsutil.string_value(child, src)
            if not url or host not in url:
                continue
            path = url.split(host, 1)[1].split("?")[0].rstrip("/")
            from driftwatch.surfaces import op_ref

            surface = op_ref(smap.provider, verb, path)
            if surface not in known_ops:
                continue
            out.append(
                _usage(
                    surface,
                    "raw_http_path",
                    file_path,
                    child,
                    src,
                    None,
                    0.85,
                    1,
                    (f"url:{url}",),
                )
            )
            for key, node in _dict_keys(args[0], src):
                out.append(
                    _usage(
                        f"{surface}/param:{key}",
                        "param_dict_key",
                        file_path,
                        node,
                        src,
                        None,
                        0.8,
                        1,
                        (f"dict_key:{key}",),
                    )
                )
    return out


def _dict_keys(args: object, src: bytes) -> list[tuple[str, object]]:
    """Return ``(key, node)`` for string keys of dict literals passed to a call."""
    out: list[tuple[str, object]] = []
    for child in args.named_children:  # type: ignore[attr-defined]
        value = child.child_by_field_name("value") if child.type == "keyword_argument" else child
        if value is None or value.type != "dictionary":
            continue
        for pair in value.named_children:
            if pair.type != "pair":
                continue
            key_node = pair.child_by_field_name("key")
            key = tsutil.string_value(key_node, src) if key_node is not None else None
            if key:
                out.append((key, pair))
    return out


def _resolve_literals(
    root: object, src: bytes, file_path: str, probes: dict[str, str]
) -> list[Usage]:
    """Match string-valued surfaces such as model identifiers.

    Only string *nodes* are considered, so the same identifier sitting in a comment is
    correctly ignored.
    """
    out: list[Usage] = []
    for m in tsutil.matches(root, _STRING_Q, "python"):  # type: ignore[arg-type]
        for node in m.get("s") or []:
            value = tsutil.string_value(node, src)
            surface = probes.get(value) if value else None
            if surface is None:
                continue
            out.append(_usage(surface, "string_literal", file_path, node, src, None, 0.95, 1, ()))
    return out


def _resolve_version_pins(root: object, src: bytes, file_path: str, scope: _Scope) -> list[Usage]:
    """Find pinned API version assignments such as ``stripe.api_version = "2024-06-20"``."""
    out: list[Usage] = []
    for m in tsutil.matches(root, _PIN_Q, "python"):  # type: ignore[arg-type]
        attrs, values, asgs = m.get("attr") or [], m.get("value") or [], m.get("asg") or []
        if not attrs or not values or not asgs:
            continue
        if tsutil.text(attrs[0], src) != "api_version":
            continue
        pinned = tsutil.string_value(values[0], src)
        if not pinned:
            continue
        root_name = next(iter(scope.roots.values()), "unknown")
        out.append(
            _usage(
                f"api:{root_name}/version:{pinned}",
                "version_pin",
                file_path,
                asgs[0],
                src,
                None,
                1.0,
                1,
                (f"pinned:{pinned}",),
            )
        )
    return out


def _usage(
    surface_ref: str,
    kind: MatchKind,
    file_path: str,
    node: object,
    src: bytes,
    symbol: str | None,
    confidence: float,
    candidates: int,
    evidence: tuple[str, ...],
) -> Usage:
    """Build a :class:`Usage` from a matched node."""
    return Usage(
        surface_ref=surface_ref,
        match_kind=kind,
        file_path=file_path,
        start_line=node.start_point[0] + 1,  # type: ignore[attr-defined]
        end_line=node.end_point[0] + 1,  # type: ignore[attr-defined]
        start_byte=node.start_byte,  # type: ignore[attr-defined]
        end_byte=node.end_byte,  # type: ignore[attr-defined]
        symbol_ref=symbol,
        snippet=tsutil.text(node, src)[:200],  # type: ignore[arg-type]
        resolution_confidence=confidence,
        scope=_scope_name(node, src),
        candidate_count=candidates,
        evidence=evidence,
    )


def _scope_name(node: object, src: bytes) -> str:
    """Return the name of the function enclosing a node, or ``"<module>"``."""
    fn = tsutil.enclosing_function(node)  # type: ignore[arg-type]
    if fn is None:
        return "<module>"
    name_node = fn.child_by_field_name("name")
    return tsutil.text(name_node, src) if name_node is not None else "<lambda>"
