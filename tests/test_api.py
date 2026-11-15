"""The SchemaMerger class: inputs, validation, encoded diffs, reports."""
import json

import pytest

from schemerge import (Diff, DocumentInvalidError, MergeResult, SchemaError, SchemaMerger,
                       apply_diff)

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}, "tags": {"type": "array"}},
          "required": ["n"]}


def make(base={"n": 1, "tags": []}, **kw):
    m = SchemaMerger(SCHEMA, **kw)
    m.set_base(base)
    return m


def test_full_workflow():
    m = make()
    d1 = m.add_version({"n": 2, "tags": []})
    d2 = m.add_version({"n": 1, "tags": ["x"]})
    assert isinstance(d1, Diff) and isinstance(d2, Diff)
    assert m.merge() == {"n": 2, "tags": ["x"]}


def test_schema_is_checked_in_the_constructor():
    with pytest.raises(SchemaError):
        SchemaMerger({"if": {}})


def test_schema_must_be_json():
    with pytest.raises(TypeError):
        SchemaMerger({"type": ("object",)})


def test_invalid_base_is_rejected():
    m = SchemaMerger(SCHEMA)
    with pytest.raises(DocumentInvalidError) as exc:
        m.set_base({"tags": []})
    assert "missing required property 'n'" in str(exc.value)
    assert exc.value.issues[0].keyword == "required"


def test_invalid_version_is_rejected_and_not_stored():
    m = make()
    with pytest.raises(DocumentInvalidError):
        m.add_version({"n": "two"})
    assert m.diffs == []


def test_non_json_documents_are_rejected():
    m = SchemaMerger(True)
    with pytest.raises(TypeError):
        m.set_base({"a": {1, 2}})
    with pytest.raises(TypeError):
        m.set_base({1: "int key"})
    with pytest.raises(TypeError):
        m.set_base([float("nan")])


def test_versions_require_a_base():
    m = SchemaMerger(SCHEMA)
    with pytest.raises(RuntimeError):
        m.add_version({"n": 1})
    with pytest.raises(RuntimeError):
        m.merge()


def test_set_base_resets_versions():
    m = make()
    m.add_version({"n": 5})
    m.set_base({"n": 7})
    assert m.diffs == []
    assert m.merge() == {"n": 7}


def test_inputs_are_copied():
    base = {"n": 1, "tags": ["a"]}
    version = {"n": 1, "tags": ["a", "b"]}
    m = make(base)
    m.add_version(version)
    base["tags"].clear()
    version["tags"].append("zzz")
    assert m.merge() == {"n": 1, "tags": ["a", "b"]}


def test_encoded_diffs_can_be_serialised_and_replayed():
    m = make()
    m.add_version({"n": 3, "tags": ["q"]})
    wire = json.dumps([d.to_json() for d in m.diffs])
    other = make()
    for payload in json.loads(wire):
        other.add_diff(payload)
    assert other.merge() == m.merge() == {"n": 3, "tags": ["q"]}


def test_add_diff_validates_the_resulting_document():
    m = make()
    bad = Diff([{"op": "set", "path": "/n", "value": "not an int"}])
    with pytest.raises(DocumentInvalidError):
        m.add_diff(bad)


def test_version_is_rebuilt_from_its_diff():
    m = make()
    m.add_version({"n": 9, "tags": [1, 2]})
    assert m.version(0) == {"n": 9, "tags": [1, 2]}
    assert apply_diff(m.base, m.diffs[0]) == m.version(0)


def test_merge_report_contents():
    m = make()
    m.add_version({"n": 2, "tags": []})
    m.add_version({"n": 3, "tags": []})
    report = m.merge_report()
    assert isinstance(report, MergeResult)
    assert report.document == {"n": 3, "tags": []}
    assert [c.kind for c in report.conflicts] == ["value"]
    as_json = json.loads(json.dumps(report.to_json()))
    assert as_json["conflicts"][0]["versions"] == [0, 1]


def test_merge_is_repeatable_and_incremental():
    m = make()
    m.add_version({"n": 1, "tags": ["a"]})
    first = m.merge()
    assert m.merge() == first
    m.add_version({"n": 1, "tags": ["b"]})
    assert m.merge() == {"n": 1, "tags": ["a", "b"]}


def test_validate_helper():
    m = make()
    assert m.validate({"n": 1}) == []
    assert [i.keyword for i in m.validate({})] == ["required"]


def test_bad_policy_is_rejected():
    with pytest.raises(ValueError):
        SchemaMerger(SCHEMA, on_delete_conflict="maybe")


def test_similarity_threshold_controls_matching():
    base = [{"a": 1, "b": 1, "c": 1}]
    version = [{"a": 1, "b": 2, "c": 2}]
    strict = SchemaMerger(True, similarity_threshold=0.95)
    strict.set_base(base)
    loose = SchemaMerger(True, similarity_threshold=0.5)
    loose.set_base(base)
    assert strict.add_version(version).summary() == {"remove": 1, "add": 1}
    assert loose.add_version(version).summary() == {"set": 2}


def test_docstring_examples():
    import doctest

    import schemerge.merger
    failures, attempted = doctest.testmod(schemerge.merger)
    assert attempted > 0 and failures == 0


def test_readme_quick_start():
    schema = {
        "type": "object",
        "properties": {
            "todo": {"type": "array", "items": {"$ref": "#/$defs/task"}},
            "done": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        },
        "$defs": {"task": {"type": "object", "required": ["title"],
                           "properties": {"title": {"type": "string"}, "prio": {"type": "integer"}}}},
    }
    merger = SchemaMerger(schema)
    merger.set_base({"todo": [{"title": "write", "prio": 1}], "done": []})
    d1 = merger.add_version({"todo": [], "done": [{"title": "write", "prio": 1}]})
    merger.add_version({"todo": [{"title": "write", "prio": 5}], "done": []})
    assert d1.to_json()["ops"][0]["op"] == "move"
    assert merger.merge() == {"todo": [], "done": [{"title": "write", "prio": 5}]}
