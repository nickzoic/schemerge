"""Randomised properties of diff + merge."""
import copy
import random

import pytest

from _helpers import mutate, random_json
from schemerge import SchemaMerger, Validator, json_equal

# -- generic documents --------------------------------------------------------


@pytest.mark.parametrize("seed", range(150))
def test_single_version_merge_reproduces_the_version(seed):
    rng = random.Random(seed)
    base = {"r": random_json(rng, 4)}
    version = mutate(base, rng, rng.randint(1, 5))
    m = SchemaMerger(True)
    m.set_base(base)
    m.add_version(version)
    assert json_equal(m.merge(), version)


@pytest.mark.parametrize("seed", range(150))
def test_merging_a_version_with_itself_is_idempotent(seed):
    rng = random.Random(1000 + seed)
    base = {"r": random_json(rng, 4)}
    version = mutate(base, rng, rng.randint(1, 5))
    m = SchemaMerger(True)
    m.set_base(base)
    m.add_version(version)
    m.add_version(version)
    assert json_equal(m.merge(), version)


@pytest.mark.parametrize("seed", range(150))
def test_edits_in_disjoint_subtrees_combine_exactly(seed):
    rng = random.Random(2000 + seed)
    base = {"left": {"x": random_json(rng, 3)}, "right": {"y": random_json(rng, 3)}}
    v1 = copy.deepcopy(base)
    v1["left"] = mutate(base["left"], rng, rng.randint(1, 4))
    v2 = copy.deepcopy(base)
    v2["right"] = mutate(base["right"], rng, rng.randint(1, 4))
    m = SchemaMerger(True)
    m.set_base(base)
    m.add_version(v1)
    m.add_version(v2)
    result = m.merge_report()
    assert json_equal(result.document, {"left": v1["left"], "right": v2["right"]})
    assert result.conflicts == []


@pytest.mark.parametrize("seed", range(150))
def test_arbitrary_concurrent_edits_never_crash(seed):
    rng = random.Random(3000 + seed)
    base = {"r": random_json(rng, 4), "s": [random_json(rng, 2) for _ in range(3)]}
    versions = [mutate(base, rng, rng.randint(1, 5)) for _ in range(rng.randint(2, 4))]
    for policy in ("keep", "delete"):
        m = SchemaMerger(True, on_delete_conflict=policy)
        m.set_base(base)
        for v in versions:
            m.add_version(v)
        m.merge_report()


# -- a constrained schema: the result must always be valid ----------------------

SCHEMA = {
    "type": "object",
    "required": ["items", "meta", "mode"],
    "additionalProperties": False,
    "properties": {
        "items": {"type": "array", "maxItems": 5, "uniqueItems": True, "items": {"$ref": "#/$defs/item"}},
        "meta": {"type": "object", "maxProperties": 3, "additionalProperties": {"type": ["string", "integer"]}},
        "mode": {"enum": ["a", "b", "c"]},
        "pair": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer"}},
    },
    "dependentRequired": {"pair": ["mode"]},
    "$defs": {
        "item": {
            "type": "object", "required": ["id"], "additionalProperties": False,
            "properties": {
                "id": {"type": "integer", "minimum": 0},
                "tags": {"type": "array", "maxItems": 3, "uniqueItems": True, "items": {"type": "string"}},
                "note": {"type": "string"},
                "kind": {"enum": ["x", "y"]},
            },
            "oneOf": [{"properties": {"kind": {"const": "x"}}, "required": ["kind"]},
                      {"not": {"required": ["kind"]}}],
        }
    },
}
VALID = Validator(SCHEMA)


def random_item(rng):
    item = {"id": rng.randint(0, 6)}
    if rng.random() < 0.6:
        item["tags"] = rng.sample(["t1", "t2", "t3", "t4"], rng.randint(0, 3))
    if rng.random() < 0.4:
        item["note"] = rng.choice(["n1", "n2"])
    if rng.random() < 0.3:
        item["kind"] = "x"
    return item


def schema_mutation(doc, rng):
    doc = copy.deepcopy(doc)
    items = doc["items"]
    op = rng.choice(["add", "remove", "swap", "tag+", "tag-", "note", "meta+", "meta-",
                     "mode", "move-tag", "pair", "kind"])
    if op == "add":
        items.insert(rng.randint(0, len(items)), random_item(rng))
    elif op == "remove" and items:
        items.pop(rng.randrange(len(items)))
    elif op == "swap" and len(items) > 1:
        i, j = rng.sample(range(len(items)), 2)
        items[i], items[j] = items[j], items[i]
    elif op == "tag+" and items:
        rng.choice(items).setdefault("tags", []).append(rng.choice(["t1", "t2", "t3", "t4"]))
    elif op == "tag-" and items:
        it = rng.choice(items)
        if it.get("tags"):
            it["tags"].pop(rng.randrange(len(it["tags"])))
    elif op == "note" and items:
        it = rng.choice(items)
        if "note" in it and rng.random() < 0.5:
            del it["note"]
        else:
            it["note"] = rng.choice(["n1", "n2", "n3"])
    elif op == "meta+":
        doc["meta"][rng.choice("pqrs")] = rng.choice([1, 2, "v"])
    elif op == "meta-" and doc["meta"]:
        del doc["meta"][rng.choice(list(doc["meta"]))]
    elif op == "mode":
        doc["mode"] = rng.choice("abc")
    elif op == "move-tag" and len(items) > 1:
        a, b = rng.sample(items, 2)
        if a.get("tags"):
            b.setdefault("tags", []).append(a["tags"].pop())
    elif op == "pair":
        if "pair" in doc and rng.random() < 0.5:
            del doc["pair"]
        else:
            doc["pair"] = [rng.randint(0, 3), rng.randint(0, 3)]
    elif op == "kind" and items:
        it = rng.choice(items)
        if "kind" in it:
            del it["kind"]
        else:
            it["kind"] = "x"
    return doc


def random_valid_version(base, rng):
    for _ in range(200):
        doc = base
        for _ in range(rng.randint(1, 4)):
            doc = schema_mutation(doc, rng)
        if VALID.is_valid(doc):
            return doc
    return base


def random_valid_base(rng):
    while True:
        doc = {"items": [random_item(rng) for _ in range(rng.randint(0, 4))],
               "meta": {"p": 1}, "mode": "a"}
        if VALID.is_valid(doc):
            return doc


BUBBLED = []


@pytest.mark.parametrize("seed", range(300))
@pytest.mark.parametrize("policy", ["keep", "delete"])
def test_merge_result_is_always_valid(seed, policy):
    rng = random.Random(seed)
    base = random_valid_base(rng)
    versions = [random_valid_version(base, rng) for _ in range(rng.randint(2, 4))]
    m = SchemaMerger(SCHEMA, on_delete_conflict=policy)
    m.set_base(base)
    for v in versions:
        m.add_version(v)
    result = m.merge_report()
    assert VALID.is_valid(result.document), (base, versions, result.document)
    BUBBLED.extend(result.bubbled)


def test_fuzzing_exercised_bubbling():
    # runs after the parametrised test above (pytest keeps file order)
    assert len(BUBBLED) >= 2
    assert all(b.issues and b.resolution for b in BUBBLED)


# -- conflicting array scalars under array constraints ----------------------------

SCALAR_SCHEMA = {
    "type": "object",
    "properties": {
        "unique": {"type": "array", "uniqueItems": True, "maxItems": 6, "items": {"type": "integer"}},
        "short": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
        "pair": {"type": "array", "items": {"type": "integer"},
                 "oneOf": [{"maxItems": 2, "minItems": 2}, {"minItems": 4, "maxItems": 4}]},
    },
}
SCALAR_VALID = Validator(SCALAR_SCHEMA)
OUTCOMES = []


def scalar_version(base, rng):
    for _ in range(100):
        doc = copy.deepcopy(base)
        for _ in range(rng.randint(1, 3)):
            key = rng.choice(["unique", "short", "pair"])
            arr = doc[key]
            op = rng.random()
            if op < 0.6 and arr:                       # change an item in place
                i = rng.randrange(len(arr))
                arr[i] = rng.randint(0, 9) if key != "short" else rng.choice("pqrstuvw")
            elif op < 0.8:
                arr.append(rng.randint(0, 9) if key != "short" else rng.choice("pqrstuvw"))
            elif arr:
                arr.pop(rng.randrange(len(arr)))
        if SCALAR_VALID.is_valid(doc):
            return doc
    return base


@pytest.mark.parametrize("seed", range(300))
def test_conflicting_array_scalars_keep_values_only_where_valid(seed):
    rng = random.Random(seed)
    base = {"unique": rng.sample(range(10), 3), "short": ["a", "b"], "pair": [1, 2]}
    versions = [scalar_version(base, rng) for _ in range(rng.randint(2, 4))]
    m = SchemaMerger(SCALAR_SCHEMA)
    m.set_base(base)
    for v in versions:
        m.add_version(v)
    result = m.merge_report()
    assert SCALAR_VALID.is_valid(result.document)
    OUTCOMES.extend(c.resolution for c in result.conflicts if c.kind == "value")


def test_scalar_fuzzing_exercised_both_outcomes():
    assert any(r.startswith("kept every value") for r in OUTCOMES)
    assert any("would violate the schema" in r for r in OUTCOMES)
