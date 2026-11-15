"""Shared helpers for the test-suite."""
import copy
import random
from importlib import metadata

import pytest

from schemerge import SchemaMerger, apply_diff, compute_diff, json_equal

PERMISSIVE = True  # the boolean schema `true` accepts every Document


def reference_jsonschema(minversion=(4, 0), reason=""):
    """Import the optional ``jsonschema`` library used for cross-checks, or skip.

    The version is read from package metadata (``jsonschema.__version__`` is
    deprecated) and compared numerically.
    """
    jsonschema = pytest.importorskip("jsonschema")
    raw = metadata.version("jsonschema")
    parts = []
    for piece in raw.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    if tuple(parts) < tuple(minversion):
        wanted = ".".join(map(str, minversion))
        pytest.skip(f"needs jsonschema>={wanted}, found {raw}" + (f": {reason}" if reason else ""))
    return jsonschema


def run_merge(schema, base, *versions, **options):
    """Merge ``versions`` onto ``base`` and check the result is valid in ``schema``."""
    merger = SchemaMerger(schema, **options)
    merger.set_base(base)
    for v in versions:
        diff = merger.add_version(v)
        assert json_equal(apply_diff(base, diff), v), "diff must reproduce the version"
    result = merger.merge_report()
    assert merger.validator.is_valid(result.document), result.document
    return result


def merged(schema, base, *versions, **options):
    return run_merge(schema, base, *versions, **options).document


def kinds(result):
    return sorted(c.kind for c in result.conflicts)


# -- random documents and mutations (used by the fuzz tests) ----------------

KEYS = list("abcdef")
SCALARS = [0, 1, 2, -1, 1.5, "x", "y", "z", "", True, False, None]


def random_json(rng, depth=3):
    roll = rng.random()
    if depth <= 0 or roll < 0.4:
        return rng.choice(SCALARS)
    if roll < 0.7:
        return {k: random_json(rng, depth - 1) for k in rng.sample(KEYS, rng.randint(0, 4))}
    return [random_json(rng, depth - 1) for _ in range(rng.randint(0, 4))]


def iter_paths(doc, path=()):
    yield path, doc
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield from iter_paths(v, path + (k,))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from iter_paths(v, path + (i,))


def _get(doc, path):
    for p in path:
        doc = doc[p]
    return doc


class _Hole:
    pass


def _drop_hole(doc):
    for path, value in iter_paths(doc):
        if isinstance(value, dict):
            for k, v in list(value.items()):
                if isinstance(v, _Hole):
                    del value[k]
                    return
        elif isinstance(value, list):
            for i, v in enumerate(value):
                if isinstance(v, _Hole):
                    del value[i]
                    return


def _insert(container, value, rng):
    if isinstance(container, dict):
        container[rng.choice(KEYS + ["g", "h"])] = value
    else:
        container.insert(rng.randint(0, len(container)), value)


def mutate(doc, rng, steps=3):
    """Apply random edits: set, insert, delete, move, reorder, wrap, rename."""
    doc = copy.deepcopy(doc)
    for _ in range(steps):
        entries = list(iter_paths(doc))
        containers = [(p, v) for p, v in entries if isinstance(v, (dict, list))]
        inner = [(p, v) for p, v in entries if p]
        op = rng.choice(["set", "insert", "delete", "move", "swap", "wrap", "rename"])
        if op == "set" and inner:
            p, _ = rng.choice(inner)
            _get(doc, p[:-1])[p[-1]] = random_json(rng, 1)
        elif op == "insert" and containers:
            _, c = rng.choice(containers)
            _insert(c, random_json(rng, 2), rng)
        elif op == "delete" and inner:
            p, _ = rng.choice(inner)
            del _get(doc, p[:-1])[p[-1]]
        elif op == "move" and inner:
            p, value = rng.choice(inner)
            targets = [(q, c) for q, c in containers if q[:len(p)] != p]
            if targets:
                _, c = rng.choice(targets)
                _get(doc, p[:-1])[p[-1]] = _Hole()
                _insert(c, value, rng)
                _drop_hole(doc)
        elif op == "swap":
            lists = [v for _, v in containers if isinstance(v, list) and len(v) > 1]
            if lists:
                lst = rng.choice(lists)
                i, j = rng.sample(range(len(lst)), 2)
                lst[i], lst[j] = lst[j], lst[i]
        elif op == "wrap" and inner:
            p, value = rng.choice(inner)
            _get(doc, p[:-1])[p[-1]] = {"w": value} if rng.random() < 0.5 else [value]
        elif op == "rename":
            objs = [v for _, v in containers if isinstance(v, dict) and v]
            if objs:
                o = rng.choice(objs)
                k = rng.choice(list(o))
                new = rng.choice(["g", "h", "k"])
                if new not in o:
                    o[new] = o.pop(k)
    return doc
