"""A Scalar changed differently by several versions.

As an array item it is treated as a deletion plus insertions: every distinct
new value is kept, in version order, where the old item was - as long as the
schema allows it.  An object member or the root holds one value, so there the
latest version's value is used.
"""
import pytest

from _helpers import PERMISSIVE as S, merged, run_merge


def value_conflicts(result):
    return [c for c in result.conflicts if c.kind == "value"]


# -- array items: keep every value -------------------------------------------------

def test_both_new_values_are_kept_in_version_order():
    result = run_merge(S, ["a", "b", "c"], ["a", "X", "c"], ["a", "Y", "c"])
    assert result.document == ["a", "X", "Y", "c"]
    [conflict] = value_conflicts(result)
    assert conflict.location == "/1" and conflict.versions == [0, 1]
    assert "kept every value" in conflict.resolution


def test_three_versions_with_a_repeated_value():
    assert merged(S, ["a", "b", "c"], ["a", "X", "c"], ["a", "Y", "c"], ["a", "X", "c"]) == \
        ["a", "X", "Y", "c"]


def test_values_of_different_types_are_all_kept():
    # A change of JSON type inside an array is already an insertion in the diff
    # ("two" and None), so those land just before the changed item (2 -> 2.5).
    assert merged(S, [1, 2, 3], [1, "two", 3], [1, 2.5, 3], [1, None, 3]) == [1, "two", None, 2.5, 3]


def test_only_one_version_changing_is_not_a_conflict():
    result = run_merge(S, ["a", "b"], ["a", "X"], ["a", "b", "c"])
    assert result.document == ["a", "X", "c"]
    assert value_conflicts(result) == []


def test_same_new_value_in_every_version_is_kept_once():
    result = run_merge(S, ["a", "b"], ["a", "X"], ["a", "X"])
    assert result.document == ["a", "X"]
    assert result.conflicts == []


def test_nested_array_item():
    base = {"matrix": [[1, 2], [3, 4]]}
    assert merged(S, base, {"matrix": [[1, 20], [3, 4]]}, {"matrix": [[1, 21], [3, 4]]}) == \
        {"matrix": [[1, 20, 21], [3, 4]]}


def test_kept_values_follow_a_reorder_by_another_version():
    base = ["a", "b", "c"]
    v0 = ["b", "a", "c"]            # b moved to the front
    v1 = ["a", "X", "c"]
    v2 = ["a", "Y", "c"]
    assert merged(S, base, v0, v1, v2) == ["X", "Y", "a", "c"]


def test_kept_values_survive_a_removal_by_another_version():
    base = ["a", "b", "c"]
    assert merged(S, base, ["a", "c"], ["a", "X", "c"], ["a", "Y", "c"]) == ["a", "X", "Y", "c"]


def test_neighbouring_insertions_stay_in_place():
    base = ["a", "b", "c"]
    v0 = ["new", "a", "X", "c"]     # insert at the start and change b
    v1 = ["a", "Y", "c", "end"]     # change b and append
    assert merged(S, base, v0, v1) == ["new", "a", "X", "Y", "c", "end"]


def test_insertion_after_the_changed_item_follows_all_its_values():
    # ["a","b","c"] -> ["a","new","X","c"] reads as: b became "new", then X inserted
    assert merged(S, ["a", "b", "c"], ["a", "new", "X", "c"], ["a", "Y", "c"]) == ["a", "new", "Y", "X", "c"]


def test_several_conflicts_in_one_array():
    base = [1, 2, 3, 4]
    assert merged(S, base, [10, 2, 30, 4], [11, 2, 31, 4]) == [10, 11, 2, 30, 31, 4]


# -- where only one value fits -------------------------------------------------------

def test_object_member_uses_the_latest_value():
    result = run_merge(S, {"a": 1}, {"a": 2}, {"a": 3})
    assert result.document == {"a": 3}
    [conflict] = value_conflicts(result)
    assert "only one value fits here" in conflict.resolution


def test_scalar_inside_an_array_item_object_uses_the_latest_value():
    base = [{"id": 1, "v": "a"}]
    assert merged(S, base, [{"id": 1, "v": "b"}], [{"id": 1, "v": "c"}]) == [{"id": 1, "v": "c"}]


def test_scalar_root_uses_the_latest_value():
    assert merged(S, "a", "b", "c") == "c"


# -- where the schema forbids keeping every value ------------------------------------

def test_max_items_prevents_keeping_both():
    schema = {"type": "array", "maxItems": 2}
    result = run_merge(schema, ["a", "b"], ["a", "X"], ["a", "Y"])
    assert result.document == ["a", "Y"]
    [conflict] = value_conflicts(result)
    assert "would violate the schema" in conflict.resolution
    assert result.bubbled == []


def test_unique_items_prevents_keeping_a_duplicate():
    schema = {"type": "array", "uniqueItems": True}
    # v0 changes b to "x"; v1 changes b to "x2" and c to "x"
    result = run_merge(schema, ["a", "b", "c"], ["a", "x", "c"], ["a", "x2", "x"])
    assert result.document == ["a", "x2", "x"]


def test_unique_items_allows_distinct_values():
    schema = {"type": "array", "uniqueItems": True}
    assert merged(schema, ["a", "b", "c"], ["a", "x", "c"], ["a", "y", "c"]) == ["a", "x", "y", "c"]


def test_combinator_on_the_parent_prevents_keeping_both():
    schema = {"type": "object", "properties": {"point": {
        "type": "array", "items": {"type": "number"},
        "oneOf": [{"minItems": 2, "maxItems": 2}, {"minItems": 3, "maxItems": 3}]}}}
    base = {"point": [0, 0]}
    result = run_merge(schema, base, {"point": [1, 0]}, {"point": [2, 0]})
    assert result.document == {"point": [1, 2, 0]}          # 3 items: still one of the shapes
    result = run_merge(schema, {"point": [0, 0, 0]}, {"point": [1, 0, 0]}, {"point": [2, 0, 0]})
    assert result.document == {"point": [2, 0, 0]}          # 4 items would match neither


def test_expansions_are_accepted_one_at_a_time_until_the_limit():
    schema = {"type": "array", "maxItems": 4}
    result = run_merge(schema, ["a", "b", "c"], ["a", "X", "Z"], ["a", "Y", "W"])
    # keeping both values of "b" fits (4 items); then "c" would make 5
    assert result.document == ["a", "X", "Y", "W"]
    resolutions = sorted(c.resolution.split(":")[0] for c in value_conflicts(result))
    assert resolutions == ["kept every value", "value from version 1 used (keeping every value would violate the schema)"]


def test_item_level_constraints_are_unaffected():
    schema = {"type": "array", "items": {"type": "string", "maxLength": 3}}
    assert merged(schema, ["aa", "bb"], ["aa", "xx"], ["aa", "yyy"]) == ["aa", "xx", "yyy"]


def test_other_violations_still_bubble_up():
    schema = {"type": "object", "properties": {
        "tags": {"type": "array", "maxItems": 3},
        "n": {"type": "integer"}}}
    base = {"tags": ["a", "b"], "n": 0}
    result = run_merge(schema, base, {"tags": ["a", "X", "c"], "n": 0}, {"tags": ["a", "Y", "d"], "n": 1})
    # appends c and d already exceed maxItems, so the expansion is not the cause
    assert len(result.document["tags"]) <= 3
    assert result.bubbled


# -- the option ----------------------------------------------------------------------

def test_latest_policy_keeps_only_the_last_value():
    result = run_merge(S, ["a", "b", "c"], ["a", "X", "c"], ["a", "Y", "c"], on_value_conflict="latest")
    assert result.document == ["a", "Y", "c"]
    [conflict] = value_conflicts(result)
    assert conflict.resolution == "value from version 1 used"


def test_bad_value_policy_is_rejected():
    from schemerge import SchemaMerger
    with pytest.raises(ValueError):
        SchemaMerger(S, on_value_conflict="both")
