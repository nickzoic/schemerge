"""The metaschema: which schemas are accepted, and that S is checked against it."""
import pytest

from _helpers import reference_jsonschema
from schemerge import METASCHEMA, SchemaError, SchemaMerger, Validator, check_schema

ACCEPTED = [
    True,
    False,
    {},
    {"type": "object", "properties": {"a": {"type": ["string", "null"]}}, "required": ["a"]},
    {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "t", "description": "d",
     "$comment": "c", "default": {}, "examples": [{}], "type": "object"},
    {"type": "array", "items": {"type": "integer", "minimum": 0, "multipleOf": 2},
     "minItems": 1, "maxItems": 10, "uniqueItems": True},
    {"type": "string", "minLength": 1, "maxLength": 5, "pattern": "^[a-z]+$"},
    {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1.5},
    {"enum": [1, "a", None, [1], {"x": 1}]},
    {"const": {"nested": [True]}},
    {"anyOf": [{"type": "string"}, {"type": "null"}]},
    {"oneOf": [{"required": ["a"]}, {"required": ["b"]}]},
    {"allOf": [{"type": "object"}, {"maxProperties": 3}], "not": {"required": ["forbidden"]}},
    {"patternProperties": {"^x-": True}, "additionalProperties": False, "minProperties": 0},
    {"dependentRequired": {"a": ["b", "c"]}},
    {"$defs": {"node": {"type": "object", "properties": {"kids": {"type": "array", "items": {"$ref": "#/$defs/node"}}}}},
     "$ref": "#/$defs/node"},
    {"definitions": {"x": {"type": "integer"}}, "properties": {"a": {"$ref": "#/definitions/x"}}},
    {"properties": {"a/b": {"type": "integer"}, "c": {"$ref": "#/properties/a~1b"}}},
    {"items": {"$ref": "#"}},
]

REJECTED = [
    ({"if": {"type": "string"}, "then": {"minLength": 1}}, "unexpected property 'if'"),
    ({"type": "array", "prefixItems": [{}]}, "prefixItems"),
    ({"contains": {"type": "string"}}, "contains"),
    ({"propertyNames": {"maxLength": 3}}, "propertyNames"),
    ({"unevaluatedProperties": False}, "unevaluatedProperties"),
    ({"format": "email"}, "format"),
    ({"$ref": "other.json#/defs/x"}, "$ref"),
    ({"$ref": "https://example.com/schema"}, "$ref"),
    ({"items": [{"type": "string"}]}, "items"),                 # tuple form of items
    ({"type": "decimal"}, "type"),
    ({"type": []}, "type"),
    ({"minItems": -1}, "minItems"),
    ({"maxLength": 1.5}, "maxLength"),
    ({"multipleOf": 0}, "multipleOf"),
    ({"required": ["a", "a"]}, "required"),
    ({"anyOf": []}, "anyOf"),
    ({"properties": {"a": {"if": {}}}}, "/properties/a"),         # nested subschemas are checked
    ({"$defs": {"x": {"then": {}}}}, "/$defs/x"),
    ("not a schema", "expected object or boolean"),
    (42, "expected object or boolean"),
]


@pytest.mark.parametrize("schema", ACCEPTED)
def test_supported_schemas_are_accepted(schema):
    check_schema(schema)
    SchemaMerger(schema)


@pytest.mark.parametrize("schema,fragment", REJECTED)
def test_unsupported_schemas_are_rejected(schema, fragment):
    with pytest.raises(SchemaError) as exc:
        SchemaMerger(schema)
    assert fragment in str(exc.value)


@pytest.mark.parametrize("schema,problem", [
    ({"$ref": "#/$defs/missing"}, "unresolvable"),
    ({"properties": {"a": {}}, "items": {"$ref": "#/properties"}}, "does not point at a subschema"),
    ({"pattern": "("}, "invalid regular expression"),
    ({"patternProperties": {"[": {}}}, "invalid regular expression"),
    ({"$ref": "#anchor"}, "$ref"),
])
def test_semantic_schema_checks(schema, problem):
    with pytest.raises(SchemaError) as exc:
        check_schema(schema)
    assert problem in str(exc.value)


def test_metaschema_validates_itself():
    check_schema(METASCHEMA)
    assert Validator(METASCHEMA).is_valid(METASCHEMA)


def test_metaschema_is_a_valid_draft_2020_12_schema():
    jsonschema = reference_jsonschema()
    jsonschema.Draft202012Validator.check_schema(METASCHEMA)


@pytest.mark.parametrize("schema", ACCEPTED + [s for s, _ in REJECTED])
def test_metaschema_verdict_matches_reference_implementation(schema):
    # jsonschema <= 4.17 used a RefResolver that treats every dict containing a
    # "$id" key as a schema - including the metaschema's `properties` map, which
    # must list "$id" as an allowed keyword - and crashes with
    # "TypeError: unhashable type: 'dict'" when resolving "#/$defs/...".
    jsonschema = reference_jsonschema((4, 18), "older RefResolver cannot resolve $refs in this metaschema")
    ours = Validator(METASCHEMA).is_valid(schema)
    theirs = jsonschema.Draft202012Validator(METASCHEMA).is_valid(schema)
    assert ours == theirs


def test_accepted_schemas_are_valid_draft_2020_12():
    jsonschema = reference_jsonschema()
    for schema in ACCEPTED:
        jsonschema.Draft202012Validator.check_schema(schema)
