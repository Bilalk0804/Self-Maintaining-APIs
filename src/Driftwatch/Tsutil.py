"""Thin tree-sitter helpers: cached parsers, query execution, node text.

Isolates the tree-sitter 0.26 ``QueryCursor`` API so the rest of the codebase never has to
care about binding churn across releases.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser, Query, QueryCursor, Tree

Lang = Literal["python", "typescript", "tsx"]


@lru_cache(maxsize=4)
def language(lang: Lang) -> Language:
    """Return the cached ``Language`` for a supported language name."""
    match lang:
        case "python":
            return Language(tree_sitter_python.language())
        case "typescript":
            return Language(tree_sitter_typescript.language_typescript())
        case "tsx":
            return Language(tree_sitter_typescript.language_tsx())
    raise ValueError(f"unsupported language: {lang!r}")


@lru_cache(maxsize=4)
def parser(lang: Lang) -> Parser:
    """Return a cached parser for a supported language name."""
    return Parser(language(lang))


def parse(src: bytes, lang: Lang = "python") -> Tree:
    """Parse source bytes into a syntax tree."""
    return parser(lang).parse(src)


@lru_cache(maxsize=64)
def _compiled(lang: Lang, pattern: str) -> Query:
    return Query(language(lang), pattern)


def captures(node: Node, pattern: str, lang: Lang = "python") -> dict[str, list[Node]]:
    """Run a query over ``node`` and return captures grouped by capture name."""
    cursor = QueryCursor(_compiled(lang, pattern))
    return cursor.captures(node)


def matches(node: Node, pattern: str, lang: Lang = "python") -> list[dict[str, list[Node]]]:
    """Run a query over ``node`` and return per-match capture dicts.

    Unlike :func:`captures`, this keeps captures from the same pattern match together, which
    matters whenever two captures must be read as a pair (an HTTP verb and its path, say).
    """
    cursor = QueryCursor(_compiled(lang, pattern))
    return [caps for _, caps in cursor.matches(node)]


def text(node: Node, src: bytes) -> str:
    """Return the source text spanned by ``node``."""
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def string_value(node: Node, src: bytes) -> str | None:
    """Return the literal value of a string node, or ``None`` if it is not a plain literal.

    Rejects f-strings and any string containing interpolation, since those cannot be
    statically resolved to a single value.
    """
    if node.type not in ("string", "concatenated_string"):
        return None
    raw = text(node, src)
    if node.type == "concatenated_string":
        parts = [string_value(c, src) for c in node.named_children if c.type == "string"]
        return "".join(p for p in parts if p) if all(p is not None for p in parts) else None
    if any(c.type == "interpolation" for c in node.children):
        return None
    body = raw
    for prefix in ('f"', "f'", 'rb"', 'br"', 'b"', 'r"', "b'", "r'"):
        if body.startswith(prefix):
            if prefix.startswith("f"):
                return None
            body = body[len(prefix) - 1 :]
            break
    for quote in ('"""', "'''", '"', "'"):
        if body.startswith(quote) and body.endswith(quote) and len(body) >= 2 * len(quote):
            return body[len(quote) : -len(quote)]
    return None


def attribute_path(node: Node, src: bytes) -> str | None:
    """Flatten a Python attribute or identifier chain into a dotted string.

    ``client.v1.charges.create`` becomes ``"client.v1.charges.create"``. Returns ``None`` if
    the chain contains a non-name link such as a subscript or a call, because the resulting
    path would not be a resolvable symbol.
    """
    parts: list[str] = []
    cur: Node | None = node
    while cur is not None:
        if cur.type == "identifier":
            parts.append(text(cur, src))
            break
        if cur.type != "attribute":
            return None
        attr = cur.child_by_field_name("attribute")
        obj = cur.child_by_field_name("object")
        if attr is None or obj is None:
            return None
        parts.append(text(attr, src))
        cur = obj
    return ".".join(reversed(parts)) if parts else None


def member_path(node: Node, src: bytes) -> str | None:
    """Flatten a TypeScript member-expression chain into a dotted string."""
    parts: list[str] = []
    cur: Node | None = node
    while cur is not None:
        if cur.type == "identifier":
            parts.append(text(cur, src))
            break
        if cur.type != "member_expression":
            return None
        prop = cur.child_by_field_name("property")
        obj = cur.child_by_field_name("object")
        if prop is None or obj is None:
            return None
        parts.append(text(prop, src))
        cur = obj
    return ".".join(reversed(parts)) if parts else None


def has_errors(tree: Tree) -> bool:
    """Return True if the tree contains a syntax error or a missing node."""
    return tree.root_node.has_error


def enclosing_function(node: Node) -> Node | None:
    """Return the nearest enclosing function definition, if any."""
    cur = node.parent
    while cur is not None:
        if cur.type in ("function_definition", "function_declaration", "method_definition"):
            return cur
        cur = cur.parent
    return None
