"""Derive a Surface Map by parsing generated Python SDK source.

The whole approach rests on one property of modern provider SDKs: they are generated from the
OpenAPI spec, so the HTTP path sits in the source as a string literal next to the method that
calls it. That makes the endpoint-to-symbol map mechanical rather than hand-curated.

Everything is keyed on a fully-qualified ``(module, class_name)`` pair rather than a bare
class name. ``stripe-python`` contains three distinct ``SessionService`` classes (checkout,
billing_portal, financial_connections) and two ``Transaction`` resources, so bare names
collide and a collision binds a method to the wrong endpoint -- which is worse than no
binding at all, because it yields a confident, wrong patch.

Five code shapes are handled, all present in ``stripe-python``:

1. Service methods -- ``self._request("get", "/v1/charges", ...)`` inside ``class ChargeService``.
2. Legacy resource methods -- ``cls._static_request("post", cls.class_url())`` inside
   ``class Charge``, where the path derives from ``OBJECT_NAME``.
3. Accessor wiring -- the generated ``{"sessions": ["stripe.checkout._session_service",
   "SessionService"]}`` lazy-service maps, which turn ``SessionService.create`` into the
   ``client.checkout.sessions.create`` that real code writes. These carry the owning module,
   so the accessor graph is exact rather than inferred.
4. Request parameter sets -- generated ``TypedDict``s, resolved through each service module's
   own ``from ... import`` lines so the right namespace wins.
5. Response types -- the declared return annotation, mapped to a resource ``OBJECT_NAME``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from driftwatch import tsutil
from driftwatch.mapping.model import PROVENANCE_CONFIDENCE, SurfaceMap, SurfaceMapEntry
from driftwatch.surfaces import op_ref

#: Methods on the SDK's internal request helper that carry (verb, path) positionally.
_REQUEST_HELPERS = frozenset(
    {"_request", "_request_async", "_static_request", "_static_request_async"}
)

_HTTP_VERBS = frozenset({"get", "post", "delete", "put", "patch"})

#: Roots of the accessor graph, mapped to the attribute prefix user code writes.
_ACCESSOR_ROOTS = {"V1Services": "v1", "V2Services": "v2"}

_CLASS_Q = "(class_definition name: (identifier) @name body: (block) @body) @cls"
_FUNC_Q = """
(function_definition name: (identifier) @name parameters: (parameters) @params body: (block) @body) @fn
"""
_CALL_Q = """
(call function: (attribute attribute: (identifier) @helper) arguments: (argument_list) @args) @call
"""
_ASSIGN_STR_Q = "(assignment left: (identifier) @name right: (string) @value) @asg"
_ASSIGN_DICT_Q = "(assignment right: (dictionary) @dict) @asg"
_IMPORT_FROM_Q = """
(import_from_statement module_name: (dotted_name) @module (dotted_name) @name)
"""
_ANNOTATED_Q = "(assignment left: (identifier) @name type: (type) @type) @asg"
_TYPED_PARAM_Q = """
[
  (typed_parameter (identifier) @pname type: (type) @ptype)
  (typed_default_parameter name: (identifier) @pname type: (type) @ptype)
]
"""

#: Pulls ``ChargeListParams`` out of an annotation such as ``Optional["ChargeListParams"]``.
_QUOTED_TYPE_RE = re.compile(r'["\']([A-Za-z_][A-Za-z0-9_.]*)["\']')
#: Strips SDK container generics so ``ListObject[Charge]`` resolves to ``Charge``.
_CONTAINER_RE = re.compile(r"(?:ListObject|SearchResultObject|Response|Awaitable)\[(.+)\]")

ClassKey = tuple[str, str]
"""A ``(module, class_name)`` pair. Module is dotted, e.g. ``stripe.checkout._session_service``."""


@dataclass(slots=True)
class _Method:
    """One SDK method and the HTTP operation it performs."""

    class_key: ClassKey
    method_name: str
    verb: str
    path: str
    params_key: ClassKey | None
    returns: str | None
    locator: str


@dataclass(slots=True)
class _Module:
    """Everything extracted from a single SDK source file."""

    dotted: str
    methods: list[_Method] = field(default_factory=list)
    object_names: dict[ClassKey, str] = field(default_factory=dict)
    custom_class_url: set[ClassKey] = field(default_factory=set)
    #: ``{owner_class_key: {accessor_attr: target_class_key}}``
    accessors: dict[ClassKey, dict[str, ClassKey]] = field(default_factory=dict)
    typed_dicts: dict[ClassKey, list[str]] = field(default_factory=dict)


def derive_stripe_python(
    sdk_root: Path, provider: str = "stripe", sdk_version: str = "unknown"
) -> SurfaceMap:
    """Build a Surface Map from a ``stripe-python`` checkout.

    Args:
        sdk_root: Directory containing the ``stripe`` package (the repository root).
        provider: Provider slug used when minting surface refs.
        sdk_version: Version label recorded on the resulting map.

    Returns:
        A :class:`SurfaceMap` with operation bindings, per-operation request parameter names
        and per-operation response object names.

    Raises:
        FileNotFoundError: if ``sdk_root`` contains no ``stripe`` package.
    """
    pkg = sdk_root / "stripe"
    if not pkg.is_dir():
        raise FileNotFoundError(f"expected a 'stripe' package under {sdk_root}")

    methods: list[_Method] = []
    object_names: dict[ClassKey, str] = {}
    custom_class_url: set[ClassKey] = set()
    accessors: dict[ClassKey, dict[str, ClassKey]] = {}
    typed_dicts: dict[ClassKey, list[str]] = {}

    for path in sorted(p for p in pkg.rglob("*.py") if "/test" not in p.as_posix()):
        src = path.read_bytes()
        tree = tsutil.parse(src, "python")
        if tsutil.has_errors(tree):
            continue
        mod = _parse_module(tree, src, _dotted_module(path, sdk_root))
        methods.extend(mod.methods)
        object_names.update(mod.object_names)
        custom_class_url |= mod.custom_class_url
        typed_dicts.update(mod.typed_dicts)
        for owner, attrs in mod.accessors.items():
            accessors.setdefault(owner, {}).update(attrs)

    resolved = [
        m
        for m in (_resolve_path(m, object_names, custom_class_url) for m in methods)
        if m is not None
    ]

    smap = SurfaceMap(
        provider=provider, language="python", sdk_package="stripe", sdk_version=sdk_version
    )
    aliases = _build_symbol_aliases(accessors)
    class_to_object = _unambiguous_class_objects(object_names)
    confidence = PROVENANCE_CONFIDENCE["sdk_source_derived"]
    seen: set[tuple[str, str]] = set()

    for m in resolved:
        surface = op_ref(provider, m.verb, m.path)
        params_type = m.params_key[1] if m.params_key else None
        for symbol in _symbols_for(m, aliases):
            if (surface, symbol) in seen:
                continue
            seen.add((surface, symbol))
            smap.add(
                SurfaceMapEntry(
                    surface_ref=surface,
                    symbol_ref=symbol,
                    confidence=confidence,
                    source_locator=m.locator,
                    params_type=params_type,
                )
            )
        if m.params_key is not None:
            known = smap.op_params.setdefault(surface, [])
            known.extend(f for f in typed_dicts.get(m.params_key, ()) if f not in known)
        if m.returns and surface not in smap.op_returns:
            object_name = class_to_object.get(m.returns.rsplit(".", 1)[-1])
            if object_name:
                smap.op_returns[surface] = object_name

    return smap


def _dotted_module(path: Path, sdk_root: Path) -> str:
    """Convert a file path into the dotted module name the SDK imports it by."""
    rel = path.relative_to(sdk_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _parse_module(tree: object, src: bytes, dotted: str) -> _Module:
    """Extract methods, object names, accessor maps and TypedDicts from one module."""
    mod = _Module(dotted=dotted)
    imports = _import_map(tree, src)
    is_params_module = dotted.startswith("stripe.params")
    # The accessor wiring lives in a module-level `_subservices` dict, owned by the single
    # service class that module defines.
    subservices = _lazy_service_map(tree.root_node, src)  # type: ignore[attr-defined]

    for cls_match in tsutil.matches(tree.root_node, _CLASS_Q, "python"):  # type: ignore[attr-defined]
        name_nodes = cls_match.get("name") or []
        body_nodes = cls_match.get("body") or []
        if not name_nodes or not body_nodes:
            continue
        class_name = tsutil.text(name_nodes[0], src)
        body = body_nodes[0]
        class_key: ClassKey = (dotted, class_name)

        if is_params_module:
            fields = _annotated_fields(body, src)
            if fields:
                mod.typed_dicts[class_key] = fields
            continue

        for asg in tsutil.matches(body, _ASSIGN_STR_Q, "python"):
            lhs, val = asg.get("name") or [], asg.get("value") or []
            if lhs and val and tsutil.text(lhs[0], src) == "OBJECT_NAME":
                literal = tsutil.string_value(val[0], src)
                if literal:
                    mod.object_names[class_key] = literal

        if subservices and class_name.endswith(("Service", "Services")):
            mod.accessors[class_key] = dict(subservices)

        for fn in tsutil.matches(body, _FUNC_Q, "python"):
            fn_names = fn.get("name") or []
            if fn_names and tsutil.text(fn_names[0], src) == "class_url":
                # The class builds its own URL, so OBJECT_NAME is not a reliable basis for
                # the path. Emitting nothing beats emitting a plausible wrong endpoint.
                mod.custom_class_url.add(class_key)
                continue
            method = _extract_method(fn, src, class_key, imports, dotted)
            if method is not None:
                mod.methods.append(method)

    return mod


def _import_map(tree: object, src: bytes) -> dict[str, str]:
    """Map each ``from X import Y`` name to its source module.

    This is how a bare ``"SessionCreateParams"`` annotation is resolved to the right one of
    several same-named generated TypedDicts.
    """
    out: dict[str, str] = {}
    for m in tsutil.matches(tree.root_node, _IMPORT_FROM_Q, "python"):  # type: ignore[attr-defined]
        module_nodes = m.get("module") or []
        name_nodes = m.get("name") or []
        if not module_nodes:
            continue
        module = tsutil.text(module_nodes[0], src)
        for node in name_nodes:
            name = tsutil.text(node, src)
            if name != module:
                out[name.rsplit(".", 1)[-1]] = module
    return out


def _lazy_service_map(node: object, src: bytes) -> dict[str, ClassKey]:
    """Extract ``{"sessions": ["stripe.checkout._session_service", "SessionService"]}`` maps.

    The generated SDK declares its service wiring as a module-level ``_subservices`` dict
    literal that names the owning module. That makes the accessor graph exact, where the
    sibling type annotation (``sessions: "SessionService"``) is ambiguous across the three
    different ``SessionService`` classes.
    """
    out: dict[str, ClassKey] = {}
    for asg in tsutil.matches(node, _ASSIGN_DICT_Q, "python"):  # type: ignore[arg-type]
        for dict_node in asg.get("dict") or []:
            for pair in dict_node.named_children:
                if pair.type != "pair":
                    continue
                key_node = pair.child_by_field_name("key")
                val_node = pair.child_by_field_name("value")
                if key_node is None or val_node is None or val_node.type != "list":
                    continue
                attr = tsutil.string_value(key_node, src)
                items = [
                    tsutil.string_value(c, src)
                    for c in val_node.named_children
                    if c.type == "string"
                ]
                if attr and len(items) == 2 and items[0] and items[1]:
                    out[attr] = (items[0], items[1])
    return out


def _annotated_fields(body: object, src: bytes) -> list[str]:
    """Return annotated attribute names declared directly in a class body."""
    fields: list[str] = []
    for asg in tsutil.matches(body, _ANNOTATED_Q, "python"):  # type: ignore[arg-type]
        lhs = asg.get("name") or []
        if lhs:
            name = tsutil.text(lhs[0], src)
            if not name.startswith("_"):
                fields.append(name)
    return fields


def _extract_method(
    fn: dict[str, list[object]],
    src: bytes,
    class_key: ClassKey,
    imports: dict[str, str],
    dotted: str,
) -> _Method | None:
    """Build a :class:`_Method` from one function definition, if it issues a request."""
    name_nodes = fn.get("name") or []
    body_nodes = fn.get("body") or []
    if not name_nodes or not body_nodes:
        return None
    method_name = tsutil.text(name_nodes[0], src)  # type: ignore[arg-type]
    if method_name.startswith("_"):
        return None

    found = _find_request_call(body_nodes[0], src)  # type: ignore[arg-type]
    if found is None:
        return None
    verb, path_expr, line = found

    params_key: ClassKey | None = None
    for p in tsutil.matches(fn["params"][0], _TYPED_PARAM_Q, "python"):  # type: ignore[index,arg-type]
        pname_nodes, ptype_nodes = p.get("pname") or [], p.get("ptype") or []
        if not pname_nodes or not ptype_nodes:
            continue
        if tsutil.text(pname_nodes[0], src) != "params":  # type: ignore[arg-type]
            continue
        match = _QUOTED_TYPE_RE.search(tsutil.text(ptype_nodes[0], src))  # type: ignore[arg-type]
        if match:
            type_name = match.group(1).rsplit(".", 1)[-1]
            params_key = (imports.get(type_name, dotted), type_name)

    returns = None
    fn_nodes = fn.get("fn") or []
    if fn_nodes:
        rt = fn_nodes[0].child_by_field_name("return_type")  # type: ignore[attr-defined]
        if rt is not None:
            returns = _return_class(tsutil.text(rt, src))

    return _Method(
        class_key=class_key,
        method_name=method_name,
        verb=verb,
        path=path_expr,
        params_key=params_key,
        returns=returns,
        locator=f"{class_key[0].replace('.', '/')}.py:{line}",
    )


def _return_class(annotation: str) -> str | None:
    """Reduce a return annotation to the bare resource class it yields.

    ``'ListObject[Charge]'`` and ``'"Charge"'`` both reduce to ``Charge``.
    """
    text = annotation.strip().strip("\"'")
    for _ in range(3):
        match = _CONTAINER_RE.fullmatch(text)
        if match is None:
            break
        text = match.group(1).strip().strip("\"'")
    return text if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", text) else None


def _find_request_call(body: object, src: bytes) -> tuple[str, str, int] | None:
    """Locate the first request-helper call in a method body.

    Returns:
        ``(verb, path_expression, line_number)`` where ``path_expression`` is either a literal
        path or one of the sentinels ``@class_url`` / ``@instance_url``, or ``None`` if the
        body issues no recognisable request.
    """
    for call in tsutil.matches(body, _CALL_Q, "python"):  # type: ignore[arg-type]
        helper_nodes, args_nodes = call.get("helper") or [], call.get("args") or []
        if not helper_nodes or not args_nodes:
            continue
        if tsutil.text(helper_nodes[0], src) not in _REQUEST_HELPERS:
            continue
        positional = [
            c for c in args_nodes[0].named_children if c.type not in ("keyword_argument", "comment")
        ]
        if len(positional) < 2:
            continue
        verb = tsutil.string_value(positional[0], src)
        if verb is None or verb.lower() not in _HTTP_VERBS:
            continue
        path = _path_expression(positional[1], src)
        if path is None:
            continue
        return verb.lower(), path, args_nodes[0].start_point[0] + 1
    return None


def _path_expression(node: object, src: bytes) -> str | None:
    """Reduce a path argument expression to a literal path or a resolvable sentinel."""
    literal = tsutil.string_value(node, src)  # type: ignore[arg-type]
    if literal is not None:
        return literal
    if node.type != "call":  # type: ignore[attr-defined]
        return None
    func = node.child_by_field_name("function")  # type: ignore[attr-defined]
    if func is None or func.type != "attribute":
        return None
    attr = func.child_by_field_name("attribute")
    obj = func.child_by_field_name("object")
    if attr is None or obj is None:
        return None
    match tsutil.text(attr, src):
        case "format":
            # "/v1/charges/{charge}/capture".format(charge=...)
            return tsutil.string_value(obj, src)
        case "class_url":
            return "@class_url"
        case "instance_url":
            return "@instance_url"
    return None


def _resolve_path(
    method: _Method,
    object_names: dict[ClassKey, str],
    custom_class_url: set[ClassKey],
) -> _Method | None:
    """Replace ``@class_url`` / ``@instance_url`` sentinels with concrete paths.

    Stripe's ``APIResource.class_url`` builds ``/v1/{OBJECT_NAME with '.' -> '/'}s``, so the
    resource's ``OBJECT_NAME`` recovers the path without executing anything. Returns ``None``
    when the path cannot be determined statically, because a missing binding only costs
    recall while a wrong one produces a confident, wrong patch.
    """
    if not method.path.startswith("@"):
        return method
    if method.class_key in custom_class_url:
        return None
    object_name = object_names.get(method.class_key)
    if object_name is None:
        return None

    segments = object_name.split(".")
    # v2 resources carry their own version prefix in OBJECT_NAME and must not be nested under
    # /v1, which would yield paths like /v1/v2/core/accounts.
    version = "v1"
    if segments[0] in ("v1", "v2"):
        version, segments = segments[0], segments[1:]
    if not segments:
        return None
    class_url = f"/{version}/" + "/".join(segments) + "s"

    method.path = class_url if method.path == "@class_url" else f"{class_url}/{{id}}"
    return method


def _unambiguous_class_objects(object_names: dict[ClassKey, str]) -> dict[str, str]:
    """Collapse ``(module, class) -> OBJECT_NAME`` to ``class -> OBJECT_NAME``.

    Classes whose bare name maps to more than one object name are omitted: a return type
    annotation carries no module, so an ambiguous name cannot be resolved correctly and
    guessing would bind response fields to the wrong resource.
    """
    candidates: dict[str, set[str]] = {}
    for (_module, class_name), object_name in object_names.items():
        candidates.setdefault(class_name, set()).add(object_name)
    return {k: next(iter(v)) for k, v in candidates.items() if len(v) == 1}


def _build_symbol_aliases(
    accessors: dict[ClassKey, dict[str, ClassKey]],
) -> dict[ClassKey, list[str]]:
    """Map each service class to the attribute paths real code uses to reach it.

    Walks the accessor graph from ``V1Services`` / ``V2Services``, so ``ChargeService`` yields
    both ``v1.charges`` and the top-level ``charges`` alias that predates the ``v1``
    namespace, and nested services such as ``checkout.sessions`` fall out of the same walk.
    """
    aliases: dict[ClassKey, list[str]] = {}
    roots = {
        key: prefix
        for key in accessors
        for root_name, prefix in _ACCESSOR_ROOTS.items()
        if key[1] == root_name
    }

    def walk(owner: ClassKey, prefix: list[str], depth: int) -> None:
        if depth > 5:
            return
        for attr, target in accessors.get(owner, {}).items():
            trail = [*prefix, attr]
            paths = aliases.setdefault(target, [])
            joined = ".".join(trail)
            if joined not in paths:
                paths.append(joined)
            if target != owner:
                walk(target, trail, depth + 1)

    for root_key, prefix in roots.items():
        walk(root_key, [prefix], 0)
        if prefix == "v1":
            # StripeClient re-exports v1 services at the top level for backwards compatibility.
            walk(root_key, [], 0)
    return aliases


def _symbols_for(method: _Method, aliases: dict[ClassKey, list[str]]) -> list[str]:
    """Return every symbol reference that reaches this method in user code."""
    out = [f"py:stripe.{method.class_key[1]}.{method.method_name}"]
    out.extend(f"py:stripe.{p}.{method.method_name}" for p in aliases.get(method.class_key, ()))
    return out
