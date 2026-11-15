"""Helpers for working with deserialized JSON values (Documents and Elements).

Terminology used throughout the package:

* A *Document* is a deserialized JSON document (``json.loads`` output).
* An *Element* is an Object (``dict``), Array (``list``) or Scalar
  (``str``, ``int``, ``float``, ``bool`` or ``None``) inside a Document.
* A *path* is a tuple of object keys (``str``) and array indexes (``int``).
"""
from __future__ import annotations

import math
from typing import Any

OBJECT, ARRAY, SCALAR = "object", "array", "scalar"


def json_type(value: Any) -> str:
    """Return the JSON Schema type name of ``value``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"{type(value).__name__} is not a JSON value")


def is_number(value: Any) -> bool:
    """True for JSON numbers.  Python booleans are *not* numbers here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def element_kind(value: Any) -> str:
    """Classify an Element as ``object``, ``array`` or ``scalar``."""
    if isinstance(value, dict):
        return OBJECT
    if isinstance(value, list):
        return ARRAY
    return SCALAR


def canonical(value: Any) -> Any:
    """A hashable form of ``value`` that follows JSON equality.

    ``1 == 1.0`` but ``1 != True``; object key order is irrelevant.
    """
    if value is None:
        return ("z",)
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return ("n", value)
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, list):
        return ("a", tuple(canonical(v) for v in value))
    if isinstance(value, dict):
        return ("o", tuple(sorted((k, canonical(v)) for k, v in value.items())))
    raise TypeError(f"{type(value).__name__} is not a JSON value")


def json_equal(a: Any, b: Any) -> bool:
    """Structural JSON equality."""
    return canonical(a) == canonical(b)


def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def to_pointer(path) -> str:
    """Render a path tuple as an RFC 6901 JSON Pointer (``""`` is the root)."""
    return "".join("/" + escape_token(str(p)) for p in path)


def resolve_pointer(document: Any, pointer: str) -> Any:
    """Return the Element of ``document`` addressed by a JSON Pointer."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON pointer {pointer!r}")
    node = document
    for raw in pointer[1:].split("/"):
        token = unescape_token(raw)
        if isinstance(node, dict):
            node = node[token]
        elif isinstance(node, list):
            node = node[int(token)]
        else:
            raise KeyError(pointer)
    return node


def check_json(value: Any, what: str = "value") -> None:
    """Raise ``TypeError`` unless ``value`` is a pure deserialized JSON value."""
    stack = [(value, ())]
    while stack:
        item, path = stack.pop()
        if item is None or isinstance(item, (bool, str, int)):
            continue
        where = to_pointer(path) or "(root)"
        if isinstance(item, float):
            if not math.isfinite(item):
                raise TypeError(f"{what} {where}: non-finite number {item!r}")
            continue
        if isinstance(item, list):
            stack.extend((v, path + (i,)) for i, v in enumerate(item))
            continue
        if isinstance(item, dict):
            for k, v in item.items():
                if not isinstance(k, str):
                    raise TypeError(f"{what} {where}: object key {k!r} is not a string")
                stack.append((v, path + (k,)))
            continue
        raise TypeError(f"{what} {where}: {type(item).__name__} is not a JSON value")


def get_at(document: Any, path) -> Any:
    node = document
    for p in path:
        node = node[p]
    return node


def set_at(document: Any, path, value: Any) -> Any:
    """Return a copy of ``document`` with the Element at ``path`` replaced.

    Only the containers along ``path`` are copied; everything else is shared.
    """
    if not path:
        return value
    head = path[0]
    if isinstance(document, dict):
        out = dict(document)
    else:
        out = list(document)
    out[head] = set_at(document[head], path[1:], value)
    return out


def is_under(path, prefix) -> bool:
    """True if ``path`` equals ``prefix`` or lies inside it."""
    return len(path) >= len(prefix) and tuple(path[: len(prefix)]) == tuple(prefix)
