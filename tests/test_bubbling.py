"""Changes that would violate the schema are bubbled up to parent Elements.

When the fine-grained merge breaks S, the offending Element is replaced
wholesale by one coherent version of it (latest version first, then the
base).  If no version of that Element fixes the problem, its parent is tried,
and so on up to the root, where the complete version Documents (each valid)
are the candidates.
"""
from _helpers import kinds, run_merge


def bubbles(result):
    return [(b.origin, b.path, b.resolution) for b in result.bubbled]


# -- array constraints ---------------------------------------------------------

TAGS = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"},
                                                  "maxItems": 2, "uniqueItems": True}}}


def test_max_items_exceeded_by_combined_appends():
    result = run_merge(TAGS, {"tags": ["a"]}, {"tags": ["a", "b"]}, {"tags": ["a", "c"]})
    assert result.document == {"tags": ["a", "c"]}
    assert bubbles(result) == [("/tags", "/tags", "version 1")]
    assert "more than 2" in result.bubbled[0].issues[0]


def test_unique_items_violated_by_concurrent_insertions():
    schema = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    result = run_merge(schema, ["a", "b"], ["a", "b", "x"], ["x", "a", "b"])
    assert result.document == ["x", "a", "b"]
    assert bubbles(result) == [("", "", "version 1")]


def test_identical_insertions_do_not_violate_unique_items():
    schema = {"type": "array", "uniqueItems": True}
    result = run_merge(schema, ["a", "b"], ["a", "b", "x"], ["a", "b", "x"])
    assert result.document == ["a", "b", "x"]
    assert result.bubbled == []


def test_unique_items_violated_by_edits_to_different_items():
    schema = {"type": "object", "properties": {
        "people": {"type": "array", "uniqueItems": True,
                   "items": {"type": "object", "properties": {"id": {"type": "integer"}}}}}}
    base = {"people": [{"id": 1}, {"id": 2}]}
    result = run_merge(schema, base, {"people": [{"id": 3}, {"id": 2}]}, {"people": [{"id": 1}, {"id": 3}]})
    assert result.document == {"people": [{"id": 1}, {"id": 3}]}
    assert bubbles(result)[0][1] == "/people"


def test_min_items_violated_by_combined_removals():
    schema = {"type": "object", "properties": {"owners": {"type": "array", "minItems": 1}}}
    result = run_merge(schema, {"owners": ["ann", "bob"]}, {"owners": ["bob"]}, {"owners": ["ann"]})
    assert result.document == {"owners": ["ann"]}
    assert bubbles(result) == [("/owners", "/owners", "version 1")]


# -- object constraints ----------------------------------------------------------

def test_max_properties_exceeded_by_combined_additions():
    schema = {"type": "object", "properties": {"limits": {"type": "object", "maxProperties": 2}}}
    base = {"limits": {"cpu": 1}}
    result = run_merge(schema, base, {"limits": {"cpu": 1, "mem": 2}}, {"limits": {"cpu": 1, "disk": 3}})
    assert result.document == {"limits": {"cpu": 1, "disk": 3}}
    assert bubbles(result) == [("/limits", "/limits", "version 1")]


def test_one_of_contact_method():
    schema = {"type": "object", "properties": {"contact": {
        "type": "object",
        "properties": {"email": {"type": "string"}, "phone": {"type": "string"}},
        "additionalProperties": False,
        "oneOf": [{"required": ["email"]}, {"required": ["phone"]}]}}}
    base = {"contact": {"email": "a@x"}}
    v1 = {"contact": {"phone": "555"}}      # switch to phone
    v2 = {"contact": {"email": "b@x"}}      # update email
    result = run_merge(schema, base, v1, v2)
    # the email edit rescues the removed email, giving both -> oneOf fails -> bubble
    assert result.document == {"contact": {"email": "b@x"}}
    assert bubbles(result) == [("/contact", "/contact", "version 1")]
    assert "delete-vs-edit" in kinds(result)


def test_discriminated_union_change_of_variant():
    shape = {"oneOf": [
        {"type": "object", "properties": {"kind": {"const": "circle"}, "radius": {"type": "number"}},
         "required": ["kind", "radius"], "additionalProperties": False},
        {"type": "object", "properties": {"kind": {"const": "square"}, "side": {"type": "number"}},
         "required": ["kind", "side"], "additionalProperties": False}]}
    schema = {"type": "object", "properties": {"shape": shape, "name": {"type": "string"}}}
    base = {"name": "s", "shape": {"kind": "circle", "radius": 1}}
    v1 = {"name": "s", "shape": {"kind": "square", "side": 2}}
    v2 = {"name": "t", "shape": {"kind": "circle", "radius": 5}}
    result = run_merge(schema, base, v1, v2)
    assert result.document == {"name": "t", "shape": {"kind": "circle", "radius": 5}}
    assert bubbles(result) == [("/shape", "/shape", "version 1")]


def test_discriminated_union_variant_change_by_the_later_version():
    shape = {"oneOf": [
        {"type": "object", "properties": {"kind": {"const": "circle"}, "radius": {"type": "number"}},
         "required": ["kind", "radius"], "additionalProperties": False},
        {"type": "object", "properties": {"kind": {"const": "square"}, "side": {"type": "number"}},
         "required": ["kind", "side"], "additionalProperties": False}]}
    schema = {"type": "object", "properties": {"shape": shape}}
    base = {"shape": {"kind": "circle", "radius": 1}}
    result = run_merge(schema, base, {"shape": {"kind": "circle", "radius": 5}},
                       {"shape": {"kind": "square", "side": 2}})
    assert result.document == {"shape": {"kind": "square", "side": 2}}


def test_dependent_required_bubbles_to_the_root():
    schema = {"type": "object",
              "properties": {"card": {"type": "string"}, "billing": {"type": "string"}, "name": {"type": "string"}},
              "dependentRequired": {"card": ["billing"]}}
    base = {"name": "n", "card": "1111", "billing": "addr"}
    v1 = {"name": "n", "card": "2222", "billing": "addr"}   # change the card
    v2 = {"name": "m"}                                       # drop card + billing, rename
    result = run_merge(schema, base, v1, v2)
    # the card edit rescues "card" but "billing" is gone -> invalid at the root
    assert result.document == {"name": "m"}
    assert bubbles(result) == [("", "", "version 1")]


def test_additional_properties_false_after_rename():
    schema = {"type": "object", "properties": {"cfg": {
        "type": "object", "properties": {"colour": {"type": "string"}, "size": {"type": "integer"}},
        "additionalProperties": False}}}
    base = {"cfg": {"colour": "red", "size": 1}}
    v1 = {"cfg": {"colour": "red", "size": 2}}
    v2 = {"cfg": {"colour": "blue", "size": 1}}
    result = run_merge(schema, base, v1, v2)
    assert result.document == {"cfg": {"colour": "blue", "size": 2}}
    assert result.bubbled == []


# -- escalation: the Element's own candidates do not help, its parent's do ------

PUBLISHING = {
    "type": "object",
    "properties": {
        "drafts": {"type": "array", "items": {"$ref": "#/$defs/draft"}},
        "published": {"type": "array", "items": {"$ref": "#/$defs/published"}},
    },
    "$defs": {
        "draft": {"type": "object", "required": ["title"], "additionalProperties": False,
                  "properties": {"title": {"type": "string"}, "status": {"enum": ["draft", "review"]}}},
        "published": {"type": "object", "required": ["title"], "additionalProperties": False,
                      "properties": {"title": {"type": "string"}, "status": {"const": "live"},
                                     "date": {"type": "string"}}},
    },
}


def test_violation_bubbles_from_new_member_to_its_moved_parent():
    base = {"drafts": [{"title": "A"}], "published": []}
    v1 = {"drafts": [], "published": [{"title": "A"}]}                  # publish A
    v2 = {"drafts": [{"title": "A", "status": "review"}], "published": []}  # mark A for review
    result = run_merge(PUBLISHING, base, v1, v2)
    assert result.document == {"drafts": [], "published": [{"title": "A"}]}
    assert bubbles(result) == [("/published/0/status", "/published/0", "version 0")]


def test_edit_valid_at_old_location_but_not_at_new_one():
    schema = {"type": "object", "properties": {
        "drafts": {"type": "array", "items": {"type": "object", "required": ["title"]}},
        "published": {"type": "array", "items": {"type": "object", "required": ["title", "date"]}}}}
    base = {"drafts": [{"title": "A", "date": "d"}], "published": []}
    v1 = {"drafts": [], "published": [{"title": "A", "date": "d"}]}   # publish A
    v2 = {"drafts": [{"title": "A"}], "published": []}                # drop the date
    result = run_merge(schema, base, v1, v2)
    # the removal follows A to "published", where a date is required
    assert result.document == {"drafts": [], "published": [{"title": "A", "date": "d"}]}
    assert bubbles(result) == [("/published/0", "/published/0", "version 0")]


def test_move_with_edits_is_remove_plus_add_and_keeps_both():
    base = {"drafts": [], "published": [{"title": "A", "date": "d"}]}
    v1 = {"drafts": [], "published": [{"title": "A", "date": "e"}]}
    v2 = {"drafts": [{"title": "A", "status": "draft"}], "published": []}  # unpublish *and* edit
    result = run_merge(PUBLISHING, base, v1, v2)
    assert result.document == {"drafts": [{"title": "A", "status": "draft"}],
                               "published": [{"title": "A", "date": "e"}]}
    assert result.bubbled == []


def test_bubbling_climbs_more_than_one_level():
    item = lambda status: {"type": "object", "required": ["title"], "additionalProperties": False,
                           "properties": {"title": {"type": "string"},
                                          "meta": {"type": "object", "properties": {"status": status}}}}
    schema = {"type": "object", "properties": {
        "drafts": {"type": "array", "items": item({"enum": ["draft", "review"]})},
        "published": {"type": "array", "items": item({"const": "live"})}}}
    base = {"drafts": [{"title": "A"}], "published": []}
    v1 = {"drafts": [], "published": [{"title": "A"}]}
    v2 = {"drafts": [{"title": "A", "meta": {"status": "review"}}], "published": []}
    result = run_merge(schema, base, v1, v2)
    # /published/0/meta/status is inside v2's new "meta" object; that object's only
    # candidate is itself, so the violation climbs to the moved item /published/0
    assert result.document == {"drafts": [], "published": [{"title": "A"}]}
    assert bubbles(result) == [("/published/0/meta/status", "/published/0", "version 0")]


def test_bubbling_in_a_recursive_schema():
    tree = {"$ref": "#/$defs/node", "$defs": {"node": {
        "type": "object", "required": ["name"], "additionalProperties": False,
        "properties": {"name": {"type": "string"},
                       "children": {"type": "array", "maxItems": 2, "items": {"$ref": "#/$defs/node"}}}}}}
    base = {"name": "root", "children": [{"name": "x", "children": [{"name": "y"}]}]}
    v1 = {"name": "root", "children": [{"name": "x", "children": [{"name": "y"}, {"name": "z1"}]}]}
    v2 = {"name": "ROOT", "children": [{"name": "x", "children": [{"name": "y"}, {"name": "z2"}]}]}
    result = run_merge(tree, base, v1, v2)
    assert result.document == {"name": "ROOT", "children": [{"name": "x", "children": [{"name": "y"}, {"name": "z2"}]}]}
    assert bubbles(result) == [("/children/0/children", "/children/0/children", "version 1")]


def test_recursive_schema_move_between_subtrees_and_edit():
    tree = {"$ref": "#/$defs/node", "$defs": {"node": {
        "type": "object", "required": ["name"],
        "properties": {"name": {"type": "string"}, "children": {"type": "array", "items": {"$ref": "#/$defs/node"}}}}}}
    base = {"name": "r", "children": [{"name": "a", "children": [{"name": "leaf", "children": []}]},
                                      {"name": "b", "children": []}]}
    v1 = {"name": "r", "children": [{"name": "a", "children": []},
                                    {"name": "b", "children": [{"name": "leaf", "children": []}]}]}
    v2 = {"name": "r", "children": [{"name": "a", "children": [{"name": "LEAF", "children": []}]},
                                    {"name": "b", "children": []}]}
    result = run_merge(tree, base, v1, v2)
    assert result.document["children"][1]["children"] == [{"name": "LEAF", "children": []}]
    assert result.document["children"][0]["children"] == []
    assert result.bubbled == []


# -- general guarantees -------------------------------------------------------------

def test_no_bubbling_when_the_merge_is_already_valid():
    result = run_merge(TAGS, {"tags": ["a"]}, {"tags": ["a", "b"]}, {"tags": []})
    assert result.document == {"tags": ["b"]}
    assert result.bubbled == []


def test_bubbling_prefers_later_versions_and_falls_back_to_base():
    schema = {"type": "object", "properties": {"pair": {"type": "array", "minItems": 2, "maxItems": 2}}}
    base = {"pair": [1, 2]}
    v1 = {"pair": [1, 3]}
    v2 = {"pair": [4, 2]}
    result = run_merge(schema, base, v1, v2)
    assert result.document == {"pair": [4, 3]}  # compatible edits: no bubbling needed
    result = run_merge(schema, base, {"pair": [2, 1]}, {"pair": [1, 2]})
    assert result.document == {"pair": [2, 1]}


def test_delete_policy_and_bubbling_together():
    schema = {"type": "object", "properties": {"opts": {"type": "object", "required": ["mode"],
                                                        "properties": {"mode": {"enum": ["a", "b"]}}}}}
    base = {"opts": {"mode": "a", "x": 1}}
    v1 = {"opts": {"mode": "b"}}
    v2 = {"opts": {"mode": "a", "x": 2}}
    result = run_merge(schema, base, v1, v2, on_delete_conflict="delete")
    assert result.document == {"opts": {"mode": "b"}}
    result = run_merge(schema, base, v1, v2, on_delete_conflict="keep")
    assert result.document == {"opts": {"mode": "b", "x": 2}}
