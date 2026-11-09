"""Resolve JavaScript and TypeScript SDK calls with tree-sitter.

Node and browser SDKs spell resources in camelCase (``paymentIntents``) on a client
variable, while the derived surface map records the Python client's snake_case aliases
(``payment_intents``). A call counts only when its receiver is the provider SDK, so a
local object that happens to have a ``create`` method is not a finding. A class field
(``this.stripe``) and a function (``getStripe()``) count when they hold that client.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import replace

from tree_sitter import Node

from Driftwatch import Tsutil as tsutil
from Driftwatch.Mapping.Model import SurfaceMap, SurfaceMapEntry
from Driftwatch.Match.Resolver import Usage

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")
_UNWRAP = frozenset(
    {
        "await_expression",
        "parenthesized_expression",
        "non_null_expression",
        "as_expression",
    }
)


class _Clients:
    """Identifiers in one file that hold the provider SDK client."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        #: Class-body start byte to field names. Node objects are not stable across walks.
        self.members: dict[int, set[str]] = {}
        self.factories: set[str] = set()


def resolve_file(src: bytes, file_path: str, smap: SurfaceMap) -> list[Usage]:
    """Resolve SDK calls, request keys, and raw provider paths in one script file.

    Args:
        src: Source bytes of a ``.js``, ``.jsx``, ``.mjs``, ``.ts``, or ``.tsx`` file.
        file_path: Path relative to the repository, used as the finding anchor.
        smap: Surface map whose symbol tails are matched against client calls.

    Returns:
        Usages whose receiver is the provider SDK. A file that does not parse is empty.
    """
    lang: tsutil.Lang = "tsx" if file_path.endswith((".tsx", ".jsx")) else "typescript"
    tree = tsutil.parse(src, lang)
    if tsutil.has_errors(tree):
        return []
    root = tree.root_node
    clients = _clients(root, src, smap.sdk_package)
    usages = _resolve_calls(root, src, file_path, smap, clients)
    usages.extend(_resolve_raw_http(root, src, file_path, smap))
    return _number_occurrences(usages)


def _resolve_calls(
    root: Node,
    src: bytes,
    file_path: str,
    smap: SurfaceMap,
    clients: _Clients,
) -> list[Usage]:
    """Resolve member calls whose receiver is the provider client."""
    exact, tails = _symbol_indexes(smap)
    out: list[Usage] = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        path = tsutil.member_path(function, src)
        chosen = (
            _match_call(path, clients, smap.sdk_package, exact, tails) if path is not None else []
        )
        if not chosen:
            tail = _sdk_tail(function, src, clients)
            if tail is not None:
                chosen = _one_surface(list(tails.get(".".join(_snake(part) for part in tail), ())))
        if len(chosen) != 1:
            continue
        entry = chosen[0]
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        snippet = tsutil.text(node, src)[:200]
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
                candidate_count=1,
            )
        )
        out.extend(_param_usages(arguments, src, out[-1], smap.params_for(entry.surface_ref)))
    return out


def _param_usages(
    arguments: Node,
    src: bytes,
    call: Usage,
    params: frozenset[str],
) -> list[Usage]:
    """Return one usage per object key that is a known parameter of the call."""
    found: list[Usage] = []
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
            if name not in params:
                continue
            found.append(
                replace(
                    call,
                    surface_ref=f"{call.surface_ref}/param:{name}",
                    match_kind="param_dict_key",
                    start_byte=pair.start_byte,
                    end_byte=pair.end_byte,
                    start_line=pair.start_point[0] + 1,
                    end_line=pair.end_point[0] + 1,
                    snippet=tsutil.text(pair, src)[:200],
                    evidence=(name,),
                )
            )
    return found


def _match_call(
    path: str,
    clients: _Clients,
    package: str,
    exact: dict[str, list[SurfaceMapEntry]],
    tails: dict[str, list[SurfaceMapEntry]],
) -> list[SurfaceMapEntry]:
    """Return the unique surface binding for one member-call path."""
    hits = list(exact.get(path, ()))
    parts = path.split(".")
    if len(parts) >= 3 and (parts[0] in clients.names or parts[0] == package):
        tail = ".".join(_snake(part) for part in parts[1:])
        hits.extend(tails.get(tail, ()))
    return _one_surface(hits)


def _symbol_indexes(
    smap: SurfaceMap,
) -> tuple[dict[str, list[SurfaceMapEntry]], dict[str, list[SurfaceMapEntry]]]:
    """Index symbols by their full path and by the resource path after the package name."""
    exact: dict[str, list[SurfaceMapEntry]] = {}
    tails: dict[str, list[SurfaceMapEntry]] = {}
    prefix = f"{smap.sdk_package}."
    for entry in smap.entries:
        body = entry.symbol_ref.split(":", 1)[-1]
        exact.setdefault(body, []).append(entry)
        if not body.startswith(prefix):
            continue
        tail = body[len(prefix) :]
        if tail.startswith(("v1.", "v2.")):
            tail = tail.split(".", 1)[1]
        if tail.endswith("_async"):
            tail = tail[: -len("_async")]
        if "." not in tail:
            continue
        tails.setdefault(tail, []).append(entry)
    return exact, tails


def _one_surface(entries: list[SurfaceMapEntry]) -> list[SurfaceMapEntry]:
    """Keep the best binding for each surface, and refuse a call that hits two surfaces."""
    best: dict[str, SurfaceMapEntry] = {}
    for entry in entries:
        current = best.get(entry.surface_ref)
        if current is None or _prefer(entry, current):
            best[entry.surface_ref] = entry
    chosen = list(best.values())
    if len(chosen) != 1:
        return []
    return chosen


def _prefer(candidate: SurfaceMapEntry, current: SurfaceMapEntry) -> bool:
    """Prefer a synchronous, unversioned symbol when several aliases share a surface."""
    return _rank(candidate) < _rank(current)


def _rank(entry: SurfaceMapEntry) -> tuple[bool, bool, str]:
    symbol = entry.symbol_ref
    return (symbol.endswith("_async"), ".v1." in symbol or ".v2." in symbol, symbol)


def _clients(root: Node, src: bytes, package: str) -> _Clients:
    """Return the client variables, class fields, and factory functions in one file."""
    constructors = _imported_constructors(root, src, package)
    clients = _Clients()
    clients.factories.update(_local_factories(root, src, package))

    def produces(node: Node) -> bool:
        current = _unwrap(node)
        if _constructs_sdk(current, constructors) or _imports_sdk_module(current, src, package):
            return True
        if current.type == "identifier" and _node_text(current) in clients.names:
            return True
        if current.type == "call_expression":
            return _callee_name(current) in clients.factories
        if current.type != "member_expression":
            return False
        obj = current.child_by_field_name("object")
        prop = current.child_by_field_name("property")
        if obj is None or prop is None or _unwrap(obj).type != "this":
            return False
        body = _enclosing_class_body(current)
        return body is not None and _node_text(prop) in clients.members.get(body.start_byte, ())

    changed = True
    while changed:
        changed = False
        for node in _walk(root):
            if node.type == "variable_declarator":
                changed |= _bind_declarator(node, clients, produces)
            elif node.type == "assignment_expression":
                changed |= _bind_assignment(node, clients, produces)
            elif node.type == "public_field_definition":
                changed |= _bind_field(node, clients, produces)
            elif node.type == "function_declaration":
                changed |= _bind_function(node, clients, produces)
    return clients


def _bind_declarator(node: Node, clients: _Clients, produces: Callable[[Node], bool]) -> bool:
    """Record a variable that holds the client, or an arrow function that returns it."""
    name = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    if name is None or name.type != "identifier" or value is None:
        return False
    ident = _node_text(name)
    changed = False
    if produces(value) and ident not in clients.names:
        clients.names.add(ident)
        changed = True
    fn = _unwrap(value)
    if (
        fn.type in ("arrow_function", "function_expression")
        and ident not in clients.factories
        and _returns_client(fn, produces)
    ):
        clients.factories.add(ident)
        changed = True
    return changed


def _bind_assignment(node: Node, clients: _Clients, produces: Callable[[Node], bool]) -> bool:
    """Record ``stripe = new Stripe()`` and ``this.stripe = new Stripe()``."""
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None or not produces(right):
        return False
    if left.type == "identifier":
        ident = _node_text(left)
        if ident in clients.names:
            return False
        clients.names.add(ident)
        return True
    if left.type != "member_expression":
        return False
    obj = left.child_by_field_name("object")
    prop = left.child_by_field_name("property")
    if obj is None or prop is None or _unwrap(obj).type != "this":
        return False
    if prop.type not in ("property_identifier", "identifier"):
        return False
    body = _enclosing_class_body(left)
    if body is None:
        return False
    return _add_member(clients, body, _node_text(prop))


def _bind_field(node: Node, clients: _Clients, produces: Callable[[Node], bool]) -> bool:
    """Record a class field initialized with the client."""
    name = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    if name is None or value is None or not produces(value):
        return False
    if name.type not in ("property_identifier", "identifier"):
        return False
    body = _enclosing_class_body(node)
    if body is None:
        return False
    return _add_member(clients, body, _node_text(name))


def _bind_function(node: Node, clients: _Clients, produces: Callable[[Node], bool]) -> bool:
    """Record a function whose returned value is the client."""
    name = node.child_by_field_name("name")
    if name is None or name.type != "identifier":
        return False
    ident = _node_text(name)
    if ident in clients.factories or not _returns_client(node, produces):
        return False
    clients.factories.add(ident)
    return True


def _add_member(clients: _Clients, body: Node, name: str) -> bool:
    bucket = clients.members.setdefault(body.start_byte, set())
    if name in bucket:
        return False
    bucket.add(name)
    return True


def _returns_client(fn: Node, produces: Callable[[Node], bool]) -> bool:
    """Return whether a function returns the client from its own body."""
    body = fn.child_by_field_name("body")
    if body is None:
        return False
    if body.type != "statement_block":
        return produces(body)
    for node in _walk(body):
        if node.type != "return_statement" or not node.named_children:
            continue
        owner = _enclosing_callable(node)
        if owner is None or not _same_node(owner, fn):
            continue
        if produces(node.named_children[0]):
            return True
    return False


def _sdk_tail(function: Node, src: bytes, clients: _Clients) -> list[str] | None:
    """Return the resource path after ``this.field`` or ``factory()``, when that is the client."""
    parts: list[str] = []
    cur: Node | None = function
    while cur is not None:
        cur = _unwrap(cur)
        if cur.type != "member_expression":
            break
        prop = cur.child_by_field_name("property")
        obj = cur.child_by_field_name("object")
        if prop is None or obj is None or prop.type not in ("property_identifier", "identifier"):
            return None
        parts.append(_node_text(prop))
        cur = obj
    parts.reverse()
    if cur is None or len(parts) < 2:
        return None
    cur = _unwrap(cur)
    if cur.type == "call_expression" and _callee_name(cur) in clients.factories:
        return parts
    if cur.type != "this" or parts[0] not in _members_of(cur, clients):
        return None
    rest = parts[1:]
    return rest if len(rest) >= 2 else None


def _members_of(node: Node, clients: _Clients) -> set[str]:
    body = _enclosing_class_body(node)
    if body is None:
        return set()
    return clients.members.get(body.start_byte, set())


def _same_node(left: Node, right: Node) -> bool:
    """Return whether two wrappers refer to the same syntax node."""
    return (
        left.type == right.type
        and left.start_byte == right.start_byte
        and left.end_byte == right.end_byte
    )


def _callee_name(node: Node) -> str:
    """Return the identifier a call invokes, or an empty string for any other callee."""
    function = node.child_by_field_name("function")
    if function is None:
        return ""
    function = _unwrap(function)
    return _node_text(function) if function.type == "identifier" else ""


def _unwrap(node: Node) -> Node:
    """Peel await, parentheses, non-null, and ``as`` wrappers off a value."""
    current = node
    while current.type in _UNWRAP and current.named_children:
        current = current.named_children[0]
    return current


def _enclosing_class_body(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None:
        if cur.type == "class_body":
            return cur
        cur = cur.parent
    return None


def _enclosing_callable(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None:
        if cur.type in (
            "function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
        ):
            return cur
        cur = cur.parent
    return None


def _local_factories(root: Node, src: bytes, package: str) -> set[str]:
    """Return named imports from a local module whose filename is the SDK package.

    ``import { getStripe } from '@/lib/billing/stripe'`` is the cross-file form of a
    function that returns the client. The npm package itself is not one of these modules:
    its default export is the constructor.
    """
    found: set[str] = set()
    for node in _walk(root):
        if node.type != "import_statement":
            continue
        source = node.child_by_field_name("source")
        if source is None or not _is_local_sdk_file(_string_literal(source, src), package):
            continue
        clause = node.child_by_field_name("name") or _import_clause(node)
        if clause is None:
            continue
        for child in clause.named_children:
            if child.type != "named_imports":
                continue
            for spec in child.named_children:
                alias = spec.child_by_field_name("alias")
                imported = spec.child_by_field_name("name")
                chosen = alias if alias is not None else imported
                if chosen is not None and chosen.type == "identifier":
                    found.add(_node_text(chosen))
    return found


def _is_local_sdk_file(specifier: str, package: str) -> bool:
    """Return whether a specifier is a project file named after the SDK, not the SDK package."""
    if not _is_sdk_module(specifier, package):
        return False
    return specifier != package and not specifier.startswith(f"{package}/")


def _imported_constructors(root: Node, src: bytes, package: str) -> set[str]:
    """Return local names bound to the SDK class, such as ``Stripe`` from ``'stripe'``."""
    names: set[str] = set()
    for node in _walk(root):
        if node.type != "import_statement":
            continue
        source = node.child_by_field_name("source")
        if source is None or not _is_sdk_module(_string_literal(source, src), package):
            continue
        clause = node.child_by_field_name("name") or _import_clause(node)
        if clause is None:
            continue
        if clause.type == "identifier":
            names.add(tsutil.text(clause, src))
            continue
        for child in clause.named_children:
            if child.type == "identifier":
                names.add(tsutil.text(child, src))
            elif child.type == "named_imports":
                for spec in child.named_children:
                    imported = spec.child_by_field_name("name")
                    if imported is not None and imported.type == "identifier":
                        names.add(tsutil.text(imported, src))
    return names


def _import_clause(node: Node) -> Node | None:
    for child in node.named_children:
        if child.type == "import_clause":
            return child
    return None


def _constructs_sdk(value: Node, constructors: set[str]) -> bool:
    """Return whether a value is ``new Stripe(...)``, including under ``await``."""
    current = value
    while current.type in ("await_expression", "parenthesized_expression"):
        inner = current.named_children[0] if current.named_children else None
        if inner is None:
            return False
        current = inner
    if current.type != "new_expression":
        return False
    constructor = current.child_by_field_name("constructor")
    return (
        constructor is not None
        and constructor.type == "identifier"
        and _node_text(constructor) in constructors
    )


def _node_text(node: Node) -> str:
    source = node.text
    if source is None:
        return ""
    return source.decode("utf-8", errors="replace")


def _imports_sdk_module(value: Node, src: bytes, package: str) -> bool:
    """Return whether a value is a static or dynamic import of the SDK module."""
    for node in _walk(value):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None:
            continue
        kind = function.type
        name = _node_text(function) if kind == "identifier" else ""
        if kind != "import" and name not in {"import", "require"}:
            continue
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            continue
        for child in arguments.named_children:
            if child.type in ("string", "template_string") and _is_sdk_module(
                _string_literal(child, src),
                package,
            ):
                return True
    return False


def _is_sdk_module(specifier: str, package: str) -> bool:
    """Return whether a module specifier is the SDK or a local file named after it."""
    if not specifier or not package:
        return False
    if specifier == package or specifier.startswith(f"{package}/"):
        return True
    stem = specifier.rsplit("/", 1)[-1]
    for suffix in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem == package


def _string_literal(node: Node, src: bytes) -> str:
    return tsutil.text(node, src).strip("\"'`")


def _snake(name: str) -> str:
    """Convert one camelCase identifier to snake_case."""
    return _CAMEL_RE.sub("_", name).lower()


def _resolve_raw_http(root: Node, src: bytes, file_path: str, smap: SurfaceMap) -> list[Usage]:
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
                    snippet=tsutil.text(node, src)[:200],
                    resolution_confidence=0.8,
                )
            )
    return out


def _scope(node: Node, src: bytes) -> str:
    """Return the enclosing TypeScript function name when available."""
    function = tsutil.enclosing_function(node)
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


def _walk(node: Node):
    """Yield a tree-sitter node and all descendants iteratively."""
    stack: deque[Node] = deque([node])
    while stack:
        current = stack.pop()
        yield current
        for child in reversed(current.named_children):
            stack.append(child)
