"""The subset validator: keyword semantics and where issues are reported."""
import pytest

from _helpers import reference_jsonschema
from schemerge import Validator
from schemerge.validator import Issue

CASES = [
    # type
    ({"type": "integer"}, 3, True),
    ({"type": "integer"}, 3.0, True),
    ({"type": "integer"}, 3.5, False),
    ({"type": "integer"}, True, False),
    ({"type": "number"}, False, False),
    ({"type": "boolean"}, 0, False),
    ({"type": "null"}, None, True),
    ({"type": ["string", "null"]}, None, True),
    ({"type": ["string", "null"]}, 1, False),
    ({"type": "array"}, {}, False),
    ({"type": "object"}, [], False),
    # enum / const use JSON equality (1 == 1.0, 1 != true)
    ({"enum": [1, "a"]}, 1.0, True),
    ({"enum": [1, "a"]}, True, False),
    ({"enum": [{"a": [1]}]}, {"a": [1]}, True),
    ({"const": False}, 0, False),
    ({"const": None}, None, True),
    # numbers
    ({"minimum": 2}, 2, True),
    ({"exclusiveMinimum": 2}, 2, False),
    ({"maximum": 2}, 2.5, False),
    ({"exclusiveMaximum": 2}, 1.9, True),
    ({"multipleOf": 3}, 9, True),
    ({"multipleOf": 0.1}, 0.3, True),
    ({"multipleOf": 3}, 10, False),
    ({"minimum": 5}, "abc", True),          # numeric keywords ignore strings
    # strings
    ({"minLength": 2}, "a", False),
    ({"maxLength": 2}, "ab", True),
    ({"pattern": "^[a-z]+$"}, "abc", True),
    ({"pattern": "b"}, "abc", True),         # unanchored search
    ({"pattern": "^b"}, "abc", False),
    # arrays
    ({"items": {"type": "string"}}, ["a", 1], False),
    ({"minItems": 1}, [], False),
    ({"maxItems": 1}, [1, 2], False),
    ({"uniqueItems": True}, [1, 1.0], False),
    ({"uniqueItems": True}, [1, True], True),
    ({"uniqueItems": True}, [{"a": 1, "b": 2}, {"b": 2, "a": 1}], False),
    # objects
    ({"required": ["a"]}, {"b": 1}, False),
    ({"properties": {"a": {"type": "string"}}}, {"a": 1}, False),
    ({"additionalProperties": False, "properties": {"a": True}}, {"a": 1, "b": 2}, False),
    ({"additionalProperties": {"type": "integer"}}, {"x": 1, "y": 2}, True),
    ({"patternProperties": {"^x_": {"type": "integer"}}, "additionalProperties": False}, {"x_a": 1}, True),
    ({"patternProperties": {"^x_": {"type": "integer"}}, "additionalProperties": False}, {"y": 1}, False),
    ({"dependentRequired": {"card": ["billing"]}}, {"card": 1}, False),
    ({"dependentRequired": {"card": ["billing"]}}, {"billing": 1}, True),
    ({"minProperties": 1}, {}, False),
    ({"maxProperties": 1}, {"a": 1, "b": 2}, False),
    # combinators
    ({"allOf": [{"type": "integer"}, {"minimum": 3}]}, 2, False),
    ({"anyOf": [{"type": "integer"}, {"type": "string"}]}, "s", True),
    ({"anyOf": [{"type": "integer"}, {"type": "string"}]}, None, False),
    ({"oneOf": [{"type": "integer"}, {"type": "number"}]}, 1, False),
    ({"oneOf": [{"type": "integer"}, {"type": "number"}]}, 1.5, True),
    ({"not": {"type": "null"}}, None, False),
    # boolean schemas
    (True, {"anything": [1]}, True),
    (False, None, False),
    ({"properties": {"a": False}}, {"a": 1}, False),
    # $ref, including recursion
    ({"$defs": {"pos": {"minimum": 0}}, "$ref": "#/$defs/pos"}, -1, False),
    ({"definitions": {"pos": {"minimum": 0}}, "items": {"$ref": "#/definitions/pos"}}, [1, 2], True),
    ({"type": "object", "properties": {"kids": {"type": "array", "items": {"$ref": "#"}}},
      "required": ["name"]}, {"name": 1, "kids": [{"name": 2, "kids": [{}]}]}, False),
]


@pytest.mark.parametrize("schema,instance,valid", CASES)
def test_keyword_semantics(schema, instance, valid):
    assert Validator(schema).is_valid(instance) is valid


# Intentional deviation: multipleOf uses a relative tolerance, so 0.3 is a
# multiple of 0.1 here, whereas plain float division (jsonschema) says it is not.
DEVIATIONS = {(repr({"multipleOf": 0.1}), repr(0.3))}


@pytest.mark.parametrize("schema,instance,valid", CASES)
def test_agrees_with_reference_implementation(schema, instance, valid):
    jsonschema = reference_jsonschema()
    if (repr(schema), repr(instance)) in DEVIATIONS:
        pytest.skip("documented deviation")
    reference = jsonschema.Draft202012Validator(schema)
    assert reference.is_valid(instance) is valid


def _paths(schema, instance):
    return sorted((i.path, i.keyword) for i in Validator(schema).errors(instance))


def test_issue_paths_point_at_the_element_at_fault():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            "shape": {"oneOf": [{"required": ["r"]}, {"required": ["s"]}]},
        },
        "required": ["tags"],
        "additionalProperties": False,
    }
    doc = {"tags": ["a", 1, "c"], "shape": {}, "extra": 0}
    assert _paths(schema, doc) == sorted([
        (("tags", 1), "type"),
        (("tags",), "maxItems"),
        (("shape",), "oneOf"),
        ((), "additionalProperties"),
    ])


def test_missing_required_reported_on_object():
    assert _paths({"properties": {"o": {"required": ["x"]}}}, {"o": {}}) == [(("o",), "required")]


def test_recursive_ref_without_progress_terminates():
    schema = {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}
    assert Validator(schema).is_valid(1)


def test_issue_str_and_pointer():
    issue = Issue(("a/b", 0), "type", "bad")
    assert issue.pointer == "/a~1b/0"
    assert str(issue) == "/a~1b/0: bad"
    assert str(Issue((), "x", "root problem")) == "(root): root problem"


def test_large_integers_do_not_overflow():
    assert Validator({"type": "integer"}).is_valid(10 ** 400)
