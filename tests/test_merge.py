"""Merge semantics without schema constraints (the schema is ``true``).

Version order matters only for genuine conflicts: later versions win.
"""
import pytest

from _helpers import PERMISSIVE as S, kinds, merged, run_merge


# -- basics ------------------------------------------------------------------

def test_no_versions_returns_the_base():
    base = {"a": [1, 2], "b": {"c": None}}
    assert merged(S, base) == base


def test_single_version_is_reproduced_exactly():
    base = {"a": [1, 2, {"x": 1}], "b": {"c": None}}
    version = {"a": [{"x": 2}, 1, 3], "d": {"c": None}}
    assert merged(S, base, version) == version


def test_identical_versions_merge_without_conflicts():
    base = {"a": 1, "l": [1]}
    v = {"a": 2, "l": [1, 2], "n": {"k": True}}
    result = run_merge(S, base, v, v, v)
    assert result.document == v
    assert result.conflicts == []


def test_unchanged_versions_change_nothing():
    base = {"a": 1}
    v = {"a": 2}
    assert merged(S, base, base, v, base) == v


# -- objects -------------------------------------------------------------------

def test_edits_to_different_keys_combine():
    base = {"name": "app", "version": 1, "debug": False}
    assert merged(S, base, {**base, "version": 2}, {**base, "debug": True}) == \
        {"name": "app", "version": 2, "debug": True}


def test_additions_of_different_keys_combine():
    assert merged(S, {}, {"a": 1}, {"b": 2}, {"c": 3}) == {"a": 1, "b": 2, "c": 3}


def test_deep_independent_edits_combine():
    base = {"x": {"y": {"z": 1, "w": 1}}, "q": [{"r": 1}]}
    v1 = {"x": {"y": {"z": 2, "w": 1}}, "q": [{"r": 1}]}
    v2 = {"x": {"y": {"z": 1, "w": 3}}, "q": [{"r": 4}]}
    assert merged(S, base, v1, v2) == {"x": {"y": {"z": 2, "w": 3}}, "q": [{"r": 4}]}


def test_same_change_in_two_versions_is_not_a_conflict():
    result = run_merge(S, {"a": 1}, {"a": 5}, {"a": 5})
    assert result.document == {"a": 5}
    assert result.conflicts == []


def test_conflicting_scalar_edits_later_version_wins():
    result = run_merge(S, {"a": 1}, {"a": 2}, {"a": 3})
    assert result.document == {"a": 3}
    assert kinds(result) == ["value"]
    assert result.conflicts[0].versions == [0, 1]
    assert result.conflicts[0].location == "/a"


def test_removal_of_untouched_key():
    assert merged(S, {"a": 1, "b": 2}, {"a": 1}, {"a": 1, "b": 2, "c": 3}) == {"a": 1, "c": 3}


def test_same_key_added_with_different_objects_is_combined():
    result = run_merge(S, {}, {"meta": {"author": "ann", "tags": {"x": 1}}},
                       {"meta": {"date": "2026", "tags": {"y": 2}}})
    assert result.document == {"meta": {"author": "ann", "date": "2026", "tags": {"x": 1, "y": 2}}}
    assert kinds(result) == ["key-collision"]


def test_same_key_added_with_different_scalars_later_wins():
    result = run_merge(S, {}, {"k": 1}, {"k": 2})
    assert result.document == {"k": 2}
    assert kinds(result) == ["key-collision"]


def test_same_key_added_with_equal_values_is_not_a_conflict():
    result = run_merge(S, {}, {"k": [1, 2]}, {"k": [1, 2]})
    assert result.document == {"k": [1, 2]}
    assert result.conflicts == []


def test_scalar_type_change_combined_with_unrelated_edit():
    assert merged(S, {"a": 1, "b": 1}, {"a": "one", "b": 1}, {"a": 1, "b": 2}) == {"a": "one", "b": 2}


# -- removal versus edit ----------------------------------------------------------

def test_removed_object_edited_elsewhere_is_kept_by_default():
    base = {"user": {"name": "a", "email": "x"}, "n": 1}
    v1 = {"n": 1}                                     # removes user
    v2 = {"user": {"name": "a", "email": "y"}, "n": 1}  # edits inside user
    result = run_merge(S, base, v1, v2)
    assert result.document == {"user": {"name": "a", "email": "y"}, "n": 1}
    assert kinds(result) == ["delete-vs-edit"]
    assert "kept" in result.conflicts[0].resolution


def test_removed_object_edited_elsewhere_with_delete_policy():
    base = {"user": {"name": "a", "email": "x"}, "n": 1}
    result = run_merge(S, base, {"n": 1}, {"user": {"name": "a", "email": "y"}, "n": 1},
                       on_delete_conflict="delete")
    assert result.document == {"n": 1}
    assert kinds(result) == ["delete-vs-edit"]
    assert "removed" in result.conflicts[0].resolution


def test_removal_is_rescued_by_an_insertion_deep_inside():
    base = {"a": {"b": {"c": []}}}
    assert merged(S, base, {}, {"a": {"b": {"c": [1]}}}) == {"a": {"b": {"c": [1]}}}


def test_removal_of_parent_and_child_by_different_versions():
    base = {"a": {"b": 1, "c": 2}, "z": 0}
    assert merged(S, base, {"z": 0}, {"a": {"c": 2}, "z": 0}) == {"z": 0}


def test_container_replaced_by_scalar_versus_edit_inside_later_wins():
    base = {"a": {"x": 1}}
    assert merged(S, base, {"a": 5}, {"a": {"x": 2}}) == {"a": {"x": 2}}
    assert merged(S, base, {"a": {"x": 2}}, {"a": 5}) == {"a": 5}


# -- moves and renames: edits follow the Element ----------------------------------

def test_rename_key_and_edit_inside():
    base = {"settings": {"theme": "dark", "size": 10}}
    v1 = {"preferences": {"theme": "dark", "size": 10}}   # rename
    v2 = {"settings": {"theme": "light", "size": 10}}     # edit at old name
    assert merged(S, base, v1, v2) == {"preferences": {"theme": "light", "size": 10}}


def test_move_between_objects_and_edit_inside():
    base = {"draft": {"post": {"title": "t", "body": "b"}}, "live": {}}
    v1 = {"draft": {}, "live": {"post": {"title": "t", "body": "b"}}}
    v2 = {"draft": {"post": {"title": "T!", "body": "b"}}, "live": {}}
    assert merged(S, base, v1, v2) == {"draft": {}, "live": {"post": {"title": "T!", "body": "b"}}}


def test_order_of_versions_does_not_matter_for_move_plus_edit():
    base = {"draft": {"post": {"title": "t", "body": "b"}}, "live": {}}
    v1 = {"draft": {}, "live": {"post": {"title": "t", "body": "b"}}}
    v2 = {"draft": {"post": {"title": "T!", "body": "b"}}, "live": {}}
    assert merged(S, base, v2, v1) == merged(S, base, v1, v2)


def test_wrap_and_edit_inside():
    base = {"host": "h", "port": 80, "opts": {"tls": False}}
    v1 = {"server": {"host": "h", "port": 80, "opts": {"tls": False}}}
    v2 = {"host": "h", "port": 80, "opts": {"tls": True}}
    assert merged(S, base, v1, v2)["server"]["opts"] == {"tls": True}


def test_deleted_container_but_child_moved_out_survives():
    base = {"old": {"keep": {"v": 1}, "junk": 1}, "new": {}}
    v1 = {"new": {}}                                   # delete old entirely
    v2 = {"old": {"junk": 1}, "new": {"keep": {"v": 1}}}  # move keep out of old
    assert merged(S, base, v1, v2) == {"new": {"keep": {"v": 1}}}


def test_moving_into_a_deleted_container_rescues_it():
    base = {"box": {"x": 1}, "item": {"y": 2}}
    v1 = {"item": {"y": 2}}                     # delete box
    v2 = {"box": {"x": 1, "item": {"y": 2}}}     # move item into box
    result = run_merge(S, base, v1, v2)
    assert result.document == {"box": {"x": 1, "item": {"y": 2}}}


def test_moves_that_would_create_a_cycle_are_broken():
    base = {"a": {"name": "A"}, "b": {"name": "B"}}
    v1 = {"b": {"name": "B", "a": {"name": "A"}}}   # a into b
    v2 = {"a": {"name": "A", "b": {"name": "B"}}}   # b into a
    result = run_merge(S, base, v1, v2)
    assert result.document == {"a": {"name": "A", "b": {"name": "B"}}}
    assert "cycle" in kinds(result)


def test_conflicting_destinations_later_wins():
    base = {"x": {"k": [1]}, "p": {}, "q": {}}
    v1 = {"p": {"x": {"k": [1]}}, "q": {}}
    v2 = {"p": {}, "q": {"x": {"k": [1]}}}
    result = run_merge(S, base, v1, v2)
    assert result.document == {"p": {}, "q": {"x": {"k": [1]}}}
    assert kinds(result) == ["move"]


def test_moved_and_deleted_keep_policy():
    base = {"a": {"v": [1]}, "dst": {}}
    v1 = {"dst": {"a": {"v": [1]}}}   # move a
    v2 = {"dst": {}}                  # delete a
    assert merged(S, base, v1, v2) == {"dst": {"a": {"v": [1]}}}
    assert merged(S, base, v1, v2, on_delete_conflict="delete") == {"dst": {}}


# -- arrays ----------------------------------------------------------------------

def test_appends_from_both_versions_are_kept_in_version_order():
    assert merged(S, [1, 2], [1, 2, 3], [1, 2, 4]) == [1, 2, 3, 4]


def test_prepends_from_both_versions():
    assert merged(S, ["m"], ["a", "m"], ["b", "m"]) == ["a", "b", "m"]


def test_insertions_at_different_places():
    assert merged(S, [1, 2, 3], [1, "x", 2, 3], [1, 2, "y", 3]) == [1, "x", 2, "y", 3]


def test_runs_of_insertions_stay_together():
    assert merged(S, ["s", "e"], ["s", 1, 2, 3, "e"], ["s", "a", "b", "e"]) == ["s", 1, 2, 3, "a", "b", "e"]


def test_identical_insertions_at_the_same_place_are_kept_once():
    result = run_merge(S, ["a"], ["a", "b"], ["a", "b"])
    assert result.document == ["a", "b"]


def test_deleting_different_items():
    assert merged(S, [1, 2, 3, 4], [1, 3, 4], [1, 2, 3]) == [1, 3]


def test_deleting_the_same_item():
    assert merged(S, ["a", "b", "c"], ["a", "c"], ["a", "c"]) == ["a", "c"]


def test_insertion_after_a_deleted_item_is_kept():
    assert merged(S, ["a", "b", "c"], ["a", "c"], ["a", "b", "new", "c"]) == ["a", "new", "c"]


def test_modified_item_is_not_lost_when_deleted_elsewhere():
    base = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
    v1 = [{"id": 2, "v": "b"}]
    v2 = [{"id": 1, "v": "A"}, {"id": 2, "v": "b"}]
    assert merged(S, base, v1, v2) == [{"id": 1, "v": "A"}, {"id": 2, "v": "b"}]
    assert merged(S, base, v1, v2, on_delete_conflict="delete") == [{"id": 2, "v": "b"}]


def test_reorder_and_edit_same_item():
    base = [{"id": 1, "t": "a"}, {"id": 2, "t": "b"}, {"id": 3, "t": "c"}]
    v1 = [base[1], base[2], base[0]]                          # move first to end
    v2 = [{"id": 1, "t": "A"}, base[1], base[2]]               # edit first
    assert merged(S, base, v1, v2) == [{"id": 2, "t": "b"}, {"id": 3, "t": "c"}, {"id": 1, "t": "A"}]


def test_reorder_and_append():
    # the appended item stays at the end even though its predecessor moved
    assert merged(S, [1, 2, 3], [3, 1, 2], [1, 2, 3, 4]) == [3, 1, 2, 4]


def test_insertion_follows_its_successor_when_predecessor_moved():
    assert merged(S, ["a", "b", "c"], ["b", "c", "a"], ["a", "x", "b", "c"]) == ["x", "b", "c", "a"]


def test_sort_and_insert_in_the_middle():
    assert merged(S, ["c", "a", "b"], ["a", "b", "c"], ["c", "a", "x", "b"]) == ["a", "x", "b", "c"]


def test_reverse_and_edit():
    base = [{"n": 1, "v": 0}, {"n": 2, "v": 0}, {"n": 3, "v": 0}]
    v1 = list(reversed(base))
    v2 = [{"n": 1, "v": 0}, {"n": 2, "v": 9}, {"n": 3, "v": 0}]
    assert merged(S, base, v1, v2) == [{"n": 3, "v": 0}, {"n": 2, "v": 9}, {"n": 1, "v": 0}]


def test_independent_reorders_combine():
    # v1 moves "a" after "c"; v2 moves "b" to the front: both intents hold
    assert merged(S, ["a", "b", "c"], ["b", "c", "a"], ["b", "a", "c"]) == ["b", "c", "a"]


def test_conflicting_moves_of_the_same_item_later_wins():
    # both versions move "a": v0 puts it after "c", v1 after "d"
    result = run_merge(S, ["a", "b", "c", "d"], ["b", "c", "a", "d"], ["b", "c", "d", "a"])
    assert result.document == ["b", "c", "d", "a"]
    assert kinds(result) == ["move"]


def test_scalar_edit_and_deletion_of_neighbours():
    assert merged(S, [10, 20, 30], [10, 25, 30], [20, 30]) == [25, 30]


def test_field_edits_in_the_same_array_item_combine():
    base = {"rows": [{"id": 1, "a": 0, "b": 0}]}
    v1 = {"rows": [{"id": 1, "a": 1, "b": 0}]}
    v2 = {"rows": [{"id": 1, "a": 0, "b": 2}]}
    assert merged(S, base, v1, v2) == {"rows": [{"id": 1, "a": 1, "b": 2}]}


def test_move_between_arrays_and_edit():
    base = {"todo": [{"task": "x", "p": 1}, {"task": "y", "p": 1}], "done": []}
    v1 = {"todo": [{"task": "y", "p": 1}], "done": [{"task": "x", "p": 1}]}
    v2 = {"todo": [{"task": "x", "p": 5}, {"task": "y", "p": 1}], "done": []}
    assert merged(S, base, v1, v2) == {"todo": [{"task": "y", "p": 1}], "done": [{"task": "x", "p": 5}]}


def test_move_between_arrays_and_insertion_into_source():
    base = {"todo": [{"task": "x"}], "done": []}
    v1 = {"todo": [], "done": [{"task": "x"}]}
    v2 = {"todo": [{"task": "x"}, {"task": "new"}], "done": []}
    assert merged(S, base, v1, v2) == {"todo": [{"task": "new"}], "done": [{"task": "x"}]}


def test_move_from_array_to_object_member():
    base = {"list": [{"k": 1}, {"k": 2}], "pinned": None}
    v1 = {"list": [{"k": 2}], "pinned": None, "top": {"k": 1}}
    v2 = {"list": [{"k": 1, "extra": True}, {"k": 2}], "pinned": None}
    assert merged(S, base, v1, v2) == {"list": [{"k": 2}], "pinned": None, "top": {"k": 1, "extra": True}}


def test_nested_arrays():
    base = [[1, 2], [3, 4]]
    assert merged(S, base, [[1, 2, 5], [3, 4]], [[1, 2], [0, 3, 4]]) == [[1, 2, 5], [0, 3, 4]]


def test_three_versions_mixed_operations():
    base = {"items": ["a", "b", "c"], "meta": {"n": 3}}
    v1 = {"items": ["a", "b", "c", "d"], "meta": {"n": 4}}
    v2 = {"items": ["c", "a", "b"], "meta": {"n": 3}}
    v3 = {"items": ["a", "c"], "meta": {"n": 3, "note": "b removed"}}
    result = run_merge(S, base, v1, v2, v3)
    assert result.document == {"items": ["c", "a", "d"], "meta": {"n": 4, "note": "b removed"}}


# -- the root ------------------------------------------------------------------

def test_root_replacement_versus_edit_later_wins():
    assert merged(S, {"a": 1}, ["list"], {"a": 2}) == {"a": 2}
    result = run_merge(S, {"a": 1}, {"a": 2}, ["list"])
    assert result.document == ["list"]
    assert "root" in kinds(result)


def test_wrapping_moves_scalar_siblings_along():
    base = {"cfg": {"host": "h", "port": 80, "opts": {"tls": False}}}
    v1 = {"cfg": {"server": {"host": "h", "port": 80, "opts": {"tls": False}}}}
    v2 = {"cfg": {"host": "example.org", "port": 80, "opts": {"tls": True}}}
    assert merged(S, base, v1, v2) == {"cfg": {"server": {"host": "example.org", "port": 80, "opts": {"tls": True}}}}


def test_wrapping_the_whole_document_and_editing_inside():
    base = {"x": 1, "y": [1]}
    v1 = {"data": {"x": 1, "y": [1]}, "version": 2}
    v2 = {"x": 5, "y": [1, 2]}
    assert merged(S, base, v1, v2) == {"data": {"x": 5, "y": [1, 2]}, "version": 2}


def test_scalar_documents():
    assert merged(S, 1, 2) == 2
    assert merged(S, "a", "b", "c") == "c"


def test_result_does_not_alias_inputs():
    base = {"a": {"b": [1]}}
    v1 = {"a": {"b": [1]}, "n": {"m": [2]}}
    out = merged(S, base, v1)
    out["a"]["b"].append(99)
    out["n"]["m"].append(99)
    assert base == {"a": {"b": [1]}} and v1 == {"a": {"b": [1]}, "n": {"m": [2]}}


@pytest.mark.parametrize("policy", ["keep", "delete"])
def test_policies_agree_when_there_is_no_delete_conflict(policy):
    base = {"a": [1, 2], "b": {"c": 1}}
    assert merged(S, base, {"a": [1, 2, 3], "b": {"c": 1}}, {"a": [2], "b": {"c": 2}},
                  on_delete_conflict=policy) == {"a": [2, 3], "b": {"c": 2}}


# -- regressions found by fuzzing ------------------------------------------------

def test_identical_wrapping_in_two_versions_is_unified():
    base = {"r": [[], "x", [{}, 1], []]}
    v = {"r": [[[], "x", [{}, 1], []]], "g": False}
    result = run_merge(S, base, v, v)
    assert result.document == v
    assert result.conflicts == []


def test_identical_new_skeletons_with_identical_children_are_unified():
    base = {"items": [{"a": 1}, {"b": 2}]}
    v = {"items": [], "groups": [{"members": [{"a": 1}, {"b": 2}], "label": "g"}]}
    assert merged(S, base, v, v, v) == v


def test_agreed_move_with_identical_insertion_after_it():
    base = {"a": {"b": [9, [0]]}, "l": [True, "y"]}
    v = {"a": {"b": [9]}, "l": [[0], 1.5, True, "y"]}
    assert merged(S, base, v, v) == v


def test_reordered_and_modified_item_with_concurrent_reorder():
    base = [{"id": 0, "tags": ["t4"]}, {"id": 4, "tags": ["t1", "t2"]}]
    v1 = [base[1], base[0]]
    v2 = [{"id": 4, "tags": ["t1", "t2"], "kind": "x"}, {"id": 0, "tags": ["t4", "t3"]}]
    assert merged(S, base, v1, v2) == v2
