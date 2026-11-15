"""A JSON Schema validator for exactly the subset described by ``metaschema.json``.

Unlike a general-purpose validator, every reported :class:`Issue` carries the
*instance path* of the Element at fault.  The merge engine uses those paths to
decide where a schema violation must be "bubbled up" from.

Where an Issue is reported:

* ``type``, ``enum``, ``const``, numeric and string keywords: at the Element.
* ``required``, ``dependentRequired``, ``min/maxProperties`` and
  ``additionalProperties: false``: at the Object that is missing/has the key.
* ``min/maxItems`` and ``uniqueItems``: at the Array.
* ``anyOf``, ``oneOf``, ``not``: at the Element the combinator applies to
  (the failures inside individual alternatives are not reported separately).
* ``allOf`` and ``$ref``: transparent - their sub-issues are reported as is.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

from .jsonutil import canonical, is_number, json_type, to_pointer, unescape_token

TYPE_NAMES = ("null", "boolean", "integer", "number", "string", "array", "object")


class SchemaError(ValueError):
    """The schema is malformed or uses features outside the supported subset."""

    def __init__(self, message: str, issues=()):
        super().__init__(message)
        self.issues = list(issues)


class DocumentInvalidError(ValueError):
    """A Document supplied to the merger is not valid in the schema."""

    def __init__(self, message: str, issues=()):
        super().__init__(message)
        self.issues = list(issues)


@dataclass(frozen=True)
class Issue:
    """One schema violation, located by instance path."""

    path: tuple
    keyword: str
    message: str

    @property
    def pointer(self) -> str:
        return to_pointer(self.path)

    def __str__(self) -> str:
        return f"{self.pointer or '(root)'}: {self.message}"


def _short(value: Any, limit: int = 60) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _has_type(value: Any, name: str) -> bool:
    if name == "integer":
        return is_number(value) and (isinstance(value, int) or value.is_integer())
    if name == "number":
        return is_number(value)
    return json_type(value) == name


class Validator:
    """Validates Documents against one Schema (assumed to be in the subset)."""

    def __init__(self, schema: Any):
        self.schema = schema
        self._patterns: dict[str, re.Pattern] = {}

    # -- public API -------------------------------------------------------
    def errors(self, instance: Any) -> list[Issue]:
        out: list[Issue] = []
        self._check(instance, self.schema, (), out, frozenset())
        return out

    def is_valid(self, instance: Any) -> bool:
        return not self.errors(instance)

    def validate(self, instance: Any, what: str = "document") -> None:
        issues = self.errors(instance)
        if issues:
            lines = "\n  ".join(str(i) for i in issues[:20])
            raise DocumentInvalidError(f"{what} is not valid in the schema:\n  {lines}", issues)

    def resolve(self, ref: str) -> Any:
        """Resolve a local ``$ref`` ("#" or "#/json/pointer") against the root schema."""
        if not isinstance(ref, str) or not ref.startswith("#"):
            raise SchemaError(f"unsupported $ref {ref!r}: only local references are supported")
        fragment = ref[1:]
        if fragment == "":
            return self.schema
        if not fragment.startswith("/"):
            raise SchemaError(f"unsupported $ref {ref!r}: anchors are not supported")
        node = self.schema
        for raw in fragment[1:].split("/"):
            token = unescape_token(unquote(raw))
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                raise SchemaError(f"unresolvable $ref {ref!r}")
        return node

    # -- implementation ---------------------------------------------------
    def _pattern(self, pattern: str) -> re.Pattern:
        rx = self._patterns.get(pattern)
        if rx is None:
            rx = self._patterns[pattern] = re.compile(pattern)
        return rx

    def _ok(self, inst, schema, path, active) -> bool:
        tmp: list[Issue] = []
        self._check(inst, schema, path, tmp, active)
        return not tmp

    def _check(self, inst, schema, path, out, active) -> None:
        if schema is True:
            return
        if schema is False:
            out.append(Issue(path, "false", "no value is allowed here"))
            return

        if "$ref" in schema:
            target = self.resolve(schema["$ref"])
            key = (id(target), path)
            if key not in active:  # guards against $ref loops that consume nothing
                self._check(inst, target, path, out, active | {key})

        if "type" in schema:
            types = schema["type"]
            types = [types] if isinstance(types, str) else types
            if not any(_has_type(inst, t) for t in types):
                out.append(Issue(path, "type", f"expected {' or '.join(types)}, found {json_type(inst)}"))
        if "enum" in schema:
            c = canonical(inst)
            if all(canonical(e) != c for e in schema["enum"]):
                out.append(Issue(path, "enum", f"{_short(inst)} is not one of {_short(schema['enum'])}"))
        if "const" in schema and canonical(schema["const"]) != canonical(inst):
            out.append(Issue(path, "const", f"{_short(inst)} is not {_short(schema['const'])}"))

        if is_number(inst):
            self._numeric(inst, schema, path, out)
        elif isinstance(inst, str):
            self._string(inst, schema, path, out)
        elif isinstance(inst, list):
            self._array(inst, schema, path, out, active)
        elif isinstance(inst, dict):
            self._object(inst, schema, path, out, active)

        for sub in schema.get("allOf", ()):
            self._check(inst, sub, path, out, active)
        if "anyOf" in schema and not any(self._ok(inst, s, path, active) for s in schema["anyOf"]):
            out.append(Issue(path, "anyOf", f"does not match any of the {len(schema['anyOf'])} alternatives in anyOf"))
        if "oneOf" in schema:
            n = sum(1 for s in schema["oneOf"] if self._ok(inst, s, path, active))
            if n != 1:
                out.append(Issue(path, "oneOf", f"matches {n} alternatives in oneOf (exactly one is required)"))
        if "not" in schema and self._ok(inst, schema["not"], path, active):
            out.append(Issue(path, "not", "matches a schema it must not match"))

    def _numeric(self, inst, schema, path, out) -> None:
        if "minimum" in schema and inst < schema["minimum"]:
            out.append(Issue(path, "minimum", f"{inst} is less than {schema['minimum']}"))
        if "maximum" in schema and inst > schema["maximum"]:
            out.append(Issue(path, "maximum", f"{inst} is greater than {schema['maximum']}"))
        if "exclusiveMinimum" in schema and inst <= schema["exclusiveMinimum"]:
            out.append(Issue(path, "exclusiveMinimum", f"{inst} is not greater than {schema['exclusiveMinimum']}"))
        if "exclusiveMaximum" in schema and inst >= schema["exclusiveMaximum"]:
            out.append(Issue(path, "exclusiveMaximum", f"{inst} is not less than {schema['exclusiveMaximum']}"))
        if "multipleOf" in schema:
            m = schema["multipleOf"]
            if isinstance(inst, int) and isinstance(m, int):
                ok = inst % m == 0
            else:
                q = inst / m
                ok = math.isfinite(q) and abs(q - round(q)) <= 1e-9 * max(1.0, abs(q))
            if not ok:
                out.append(Issue(path, "multipleOf", f"{inst} is not a multiple of {m}"))

    def _string(self, inst, schema, path, out) -> None:
        if "minLength" in schema and len(inst) < schema["minLength"]:
            out.append(Issue(path, "minLength", f"string is shorter than {schema['minLength']}"))
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            out.append(Issue(path, "maxLength", f"string is longer than {schema['maxLength']}"))
        if "pattern" in schema and not self._pattern(schema["pattern"]).search(inst):
            out.append(Issue(path, "pattern", f"{_short(inst)} does not match /{schema['pattern']}/"))

    def _array(self, inst, schema, path, out, active) -> None:
        if "items" in schema:
            for i, item in enumerate(inst):
                self._check(item, schema["items"], path + (i,), out, active)
        if "minItems" in schema and len(inst) < schema["minItems"]:
            out.append(Issue(path, "minItems", f"array has {len(inst)} items, fewer than {schema['minItems']}"))
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            out.append(Issue(path, "maxItems", f"array has {len(inst)} items, more than {schema['maxItems']}"))
        if schema.get("uniqueItems"):
            seen = set()
            for item in inst:
                c = canonical(item)
                if c in seen:
                    out.append(Issue(path, "uniqueItems", f"array contains duplicate item {_short(item)}"))
                    break
                seen.add(c)

    def _object(self, inst, schema, path, out, active) -> None:
        props = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in inst.items():
            matched = False
            if key in props:
                matched = True
                self._check(value, props[key], path + (key,), out, active)
            for pat, sub in patterns.items():
                if self._pattern(pat).search(key):
                    matched = True
                    self._check(value, sub, path + (key,), out, active)
            if not matched:
                if additional is False:
                    out.append(Issue(path, "additionalProperties", f"unexpected property {key!r}"))
                elif additional is not True:
                    self._check(value, additional, path + (key,), out, active)
        for key in schema.get("required", ()):
            if key not in inst:
                out.append(Issue(path, "required", f"missing required property {key!r}"))
        for key, needed in schema.get("dependentRequired", {}).items():
            if key in inst:
                for other in needed:
                    if other not in inst:
                        out.append(Issue(path, "dependentRequired", f"property {key!r} requires {other!r}"))
        if "minProperties" in schema and len(inst) < schema["minProperties"]:
            out.append(Issue(path, "minProperties", f"object has fewer than {schema['minProperties']} properties"))
        if "maxProperties" in schema and len(inst) > schema["maxProperties"]:
            out.append(Issue(path, "maxProperties", f"object has more than {schema['maxProperties']} properties"))


# -- schema checking ---------------------------------------------------------

METASCHEMA: dict = json.loads((Path(__file__).parent / "metaschema.json").read_text(encoding="utf-8"))
_META_VALIDATOR = Validator(METASCHEMA)

_SCHEMA_KEYS = ("items", "additionalProperties", "not")
_SCHEMA_MAP_KEYS = ("properties", "patternProperties", "$defs", "definitions")
_SCHEMA_LIST_KEYS = ("allOf", "anyOf", "oneOf")


def iter_subschemas(schema: Any, path: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    """Yield ``(path, subschema)`` for the schema and every nested subschema."""
    yield path, schema
    if not isinstance(schema, dict):
        return
    for k in _SCHEMA_KEYS:
        if k in schema:
            yield from iter_subschemas(schema[k], path + (k,))
    for k in _SCHEMA_MAP_KEYS:
        for name, sub in schema.get(k, {}).items():
            yield from iter_subschemas(sub, path + (k, name))
    for k in _SCHEMA_LIST_KEYS:
        for i, sub in enumerate(schema.get(k, ())):
            yield from iter_subschemas(sub, path + (k, i))


def check_schema(schema: Any) -> Validator:
    """Check ``schema`` against the metaschema and return a :class:`Validator` for it.

    Beyond the metaschema this also checks that every ``$ref`` resolves to a
    subschema and that every regular expression compiles.
    """
    issues = _META_VALIDATOR.errors(schema)
    if issues:
        lines = "\n  ".join(str(i) for i in issues)
        raise SchemaError(f"schema is malformed or outside the supported subset:\n  {lines}", issues)
    validator = Validator(schema)
    subschemas = list(iter_subschemas(schema))
    known = {id(s) for _, s in subschemas}
    problems = []
    for path, sub in subschemas:
        if not isinstance(sub, dict):
            continue
        where = to_pointer(path) or "(root)"
        if "$ref" in sub:
            try:
                target = validator.resolve(sub["$ref"])
            except SchemaError as exc:
                problems.append(f"{where}: {exc}")
            else:
                if id(target) not in known:
                    problems.append(f"{where}: $ref {sub['$ref']!r} does not point at a subschema")
        regexes = ([sub["pattern"]] if "pattern" in sub else []) + list(sub.get("patternProperties", {}))
        for rx in regexes:
            try:
                re.compile(rx)
            except re.error as exc:
                problems.append(f"{where}: invalid regular expression {rx!r}: {exc}")
    if problems:
        raise SchemaError("schema is not usable:\n  " + "\n  ".join(problems))
    return validator
