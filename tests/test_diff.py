"""Encoding differences: what operations are produced, and exact round-trips."""
import json
import random

import pytest

from _helpers import mutate, random_json
from schemerge import Diff, DiffError, apply_diff, compute_diff, json_equal


def ops(base, version):
    diff = compute_diff(base, version)
    assert json_equal(apply_diff(base, diff), version)
    return diff.ops


def op_names(base, version):
    return sorted(o["op"] for o in ops(base, version))


# -- the shape of individual diffs -------------------------------------------

def test_identical_documents_have_an_empty_diff():
    doc = {"a": [1, {"b": None}], "c": "x"}
    assert ops(doc, doc) == []


def test_scalar_change_is_a_set():
    assert ops({"a": 1}, {"a": 2}) == [{"op": "set", "path": "/a", "value": 2}]


def test_scalar_type_change_is_still_a_set():
    assert ops({"a": 1}, {"a": "1"}) == [{"op": "set", "path": "/a", "value": "1"}]


def test_true_and_one_are_different_values():
    assert ops({"a": True}, {"a": 1}) == [{"op": "set", "path": "/a", "value": 1}]
    # in an array, a scalar of another type is a different item
    assert op_names([True], [1]) == ["add", "remove"]


def test_one_and_one_point_zero_are_equal():
    assert ops({"a": 1}, {"a": 1.0}) == []


def test_key_order_is_irrelevant():
    assert ops({"a": 1, "b": 2}, {"b": 2, "a": 1}) == []


def test_add_and_remove_keys():
    assert ops({"a": 1}, {"b": {"c": [1]}}) == [
        {"op": "remove", "path": "/a"},
        {"op": "add", "id": "+1", "parent": "", "key": "b", "value": {"c": [1]}},
    ]


def test_array_append_is_anchored_after_last_item():
    assert ops([1, 2], [1, 2, 3]) == [{"op": "add", "id": "+1", "parent": "", "after": "/1", "value": 3}]


def test_array_prepend_is_anchored_at_start():
    assert ops([1, 2], [0, 1, 2]) == [{"op": "add", "id": "+1", "parent": "", "after": None, "value": 0}]


def test_array_delete():
    assert ops(["a", "b", "c"], ["a", "c"]) == [{"op": "remove", "path": "/1"}]


def test_reorder_uses_moves_not_add_remove():
    assert op_names([1, 2, 3, 4], [4, 1, 2, 3]) == ["move"]
    assert op_names(["a", "b", "c"], ["c", "b", "a"]) == ["move", "move"]


def test_modified_array_item_is_matched_by_similarity():
    base = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    version = [{"id": 1, "name": "A"}, {"id": 2, "name": "b"}]
    assert ops(base, version) == [{"op": "set", "path": "/0/name", "value": "A"}]


def test_reordered_and_modified_item_is_one_move_plus_edits():
    base = [{"id": 0, "tags": ["a"], "note": "n"}, {"id": 4, "tags": ["b", "c"]}]
    version = [{"id": 4, "tags": ["b", "c"], "kind": "x"}, {"id": 0, "tags": ["a", "z"], "note": "n"}]
    assert op_names(base, version) == ["add", "add", "move"]


def test_reordered_and_modified_item_without_identical_children():
    base = [{"id": 1, "name": "a", "n": 1}, {"id": 2, "name": "b", "n": 1}]
    version = [{"id": 2, "name": "b", "n": 9}, {"id": 1, "name": "a", "n": 1}]
    assert op_names(base, version) == ["move", "set"]


def test_scalars_are_not_paired_across_positions():
    assert op_names([1, 2, 3], [3, 4]) == ["add", "remove", "remove"]


def test_changed_scalar_in_array_is_a_set():
    assert ops([1, 2, 3], [1, 5, 3]) == [{"op": "set", "path": "/1", "value": 5}]


def test_container_move_between_parents_is_one_move():
    base = {"todo": [{"t": "x", "n": 1}], "done": []}
    version = {"todo": [], "done": [{"t": "x", "n": 1}]}
    assert ops(base, version) == [{"op": "move", "path": "/todo/0", "parent": "/done", "after": None}]


def test_emptied_container_stays_and_its_item_moves():
    base = {"todo": [{"t": "x"}, {"t": "y"}], "done": []}
    version = {"todo": [{"t": "y"}], "done": [{"t": "x"}]}
    assert ops(base, version) == [{"op": "move", "path": "/todo/0", "parent": "/done", "after": None}]


def test_rename_over_an_existing_key_is_a_move():
    assert op_names({"a": {"v": 1}, "b": {"v": 2}}, {"b": {"v": 1}}) == ["move", "remove"]


def test_key_rename_is_a_move():
    assert ops({"old": {"x": 1}}, {"new": {"x": 1}}) == [
        {"op": "move", "path": "/old", "parent": "", "key": "new"}]


def test_swapped_values_are_moves_not_rewrites():
    base = {"a": {"v": 1}, "b": {"v": 2}}
    assert op_names(base, {"a": {"v": 2}, "b": {"v": 1}}) == ["move", "move"]


def test_edit_in_place_is_not_mistaken_for_a_move():
    base = {"p": {"pos": [0, 0]}, "q": {"pos": [1, 1]}}
    version = {"p": {"pos": [1, 1]}, "q": {"pos": [1, 1]}}
    assert op_names(base, version) == ["set", "set"]


def test_wrapping_creates_a_skeleton_and_moves_into_it():
    base = {"cfg": {"host": "h", "port": 1}}
    version = {"cfg": {"server": {"host": "h", "port": 1}}}
    assert ops(base, version) == [
        {"op": "add", "id": "+1", "parent": "", "key": "cfg", "value": {}},
        {"op": "move", "path": "/cfg", "parent": "+1", "key": "server"},
    ]


def test_wrapping_a_container_moves_it_whole():
    base = {"items": [{"a": 1}]}
    version = {"wrapper": {"items": [{"a": 1}]}}
    assert ops(base, version) == [
        {"op": "add", "id": "+1", "parent": "", "key": "wrapper", "value": {}},
        {"op": "move", "path": "/items", "parent": "+1", "key": "items"},
    ]


def test_wrapping_the_whole_document():
    base = {"a": [1, 2]}
    version = [{"a": [1, 2]}]
    assert ops(base, version) == [
        {"op": "add", "id": "+1", "parent": None, "value": []},
        {"op": "move", "path": "", "parent": "+1", "after": None},
    ]


def test_container_type_change_is_remove_and_add():
    assert ops({"a": [1]}, {"a": {"x": 1}}) == [
        {"op": "remove", "path": "/a"},
        {"op": "add", "id": "+1", "parent": "", "key": "a", "value": {"x": 1}},
    ]


def test_root_replacement():
    assert ops({"a": 1}, [1, 2]) == [
        {"op": "remove", "path": ""},
        {"op": "add", "id": "+1", "parent": None, "value": [1, 2]},
    ]


def test_unwrapping_moves_a_child_to_the_root():
    assert ops({"data": {"x": [1]}}, {"x": [1]}) == [
        {"op": "remove", "path": "/data"},
        {"op": "move", "path": "/data/x", "parent": "", "key": "x"},
    ]


def test_pointer_escaping_in_keys():
    base = {"a/b": {"~c": 1}}
    assert ops(base, {"a/b": {"~c": 2}}) == [{"op": "set", "path": "/a~1b/~0c", "value": 2}]


def test_scalar_root():
    assert ops("x", "y") == [{"op": "set", "path": "", "value": "y"}]


# -- encoding ------------------------------------------------------------------

def test_diff_is_json_serializable_and_round_trips():
    base = {"a": [1, {"b": 2}], "c": {"d": None}}
    version = {"a": [{"b": 3}, 1, 4], "e": {"d": None}}
    diff = compute_diff(base, version)
    text = json.dumps(diff.to_json())
    again = Diff.from_json(json.loads(text))
    assert again == diff
    assert json_equal(apply_diff(base, json.loads(text)), version)


def test_summary_counts_operations():
    diff = compute_diff([1, 2, 3], [3, 1, 9])
    assert sum(diff.summary().values()) == len(diff)
    assert set(diff.summary()) <= {"remove", "move", "add", "set"}


@pytest.mark.parametrize("bad_ops,message", [
    ([{"op": "remove", "path": "/nope"}], "no element"),
    ([{"op": "set", "path": "", "value": 1}], "non-scalar"),
    ([{"op": "set", "path": "/a", "value": [1]}], "scalar value"),
    ([{"op": "add", "id": "x", "parent": "", "key": "k", "value": 1}], "malformed"),
    ([{"op": "add", "id": "+1", "parent": "/zz", "key": "k", "value": 1}], "unknown parent"),
    ([{"op": "add", "id": "+1", "parent": "", "after": None, "value": 1}], "non-array"),
    ([{"op": "add", "id": "+1", "parent": "/l", "after": "/l/7", "value": 1}], "anchor"),
    ([{"op": "explode"}], "unknown operation"),
    ([{"op": "remove", "path": ""}], "exactly one new root"),
])
def test_malformed_diffs_are_rejected(bad_ops, message):
    with pytest.raises(DiffError) as exc:
        apply_diff({"a": 1, "l": [0]}, bad_ops)
    assert message in str(exc.value)


def test_from_json_requires_format_marker():
    with pytest.raises(DiffError):
        Diff.from_json({"ops": []})


# -- round-trips ---------------------------------------------------------------

PAIRS = [
    ({}, {"a": 1}),
    ([], [[], {}, [[]]]),
    ({"a": {"b": {"c": [1, 2, 3]}}}, {"a": {"b": {"c": [3, 2, 1, 0]}}}),
    ([1, 1, 1, 2], [2, 1, 1]),
    ([[1, 2], [3, 4]], [[3, 4], [1, 2], [1, 2]]),
    ({"x": [{"k": 1}, {"k": 2}]}, {"y": [{"k": 2}], "x": [{"k": 1, "z": 0}]}),
    ({"a": 1, "b": [1, 2]}, [1, 2]),
    (None, {"a": None}),
    ({"a": {"b": [{"c": 1}]}}, {"a": {"b": [[{"c": 1}]]}}),
    ({"a": [{"x": 1}, {"y": 2}]}, {"a": [{"y": 2, "w": [{"x": 1}]}]}),
]


@pytest.mark.parametrize("base,version", PAIRS)
def test_round_trip(base, version):
    assert json_equal(apply_diff(base, compute_diff(base, version)), version)


@pytest.mark.parametrize("seed", range(400))
def test_round_trip_fuzz(seed):
    rng = random.Random(seed)
    base = {"root": random_json(rng, 4), "more": random_json(rng, 3)}
    version = mutate(base, rng, steps=rng.randint(1, 6))
    diff = compute_diff(base, version)
    assert json_equal(apply_diff(base, Diff.from_json(json.loads(json.dumps(diff.to_json())))), version)
