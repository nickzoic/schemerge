# schemerge

[![tests](https://github.com/nickzoic/schemerge/actions/workflows/tests.yml/badge.svg)](https://github.com/nickzoic/schemerge/actions/workflows/tests.yml)

Schema-aware structural diff and N-way merge of JSON documents.

Given a JSON Schema **S**, an initial Document valid in S, and any number of
modified versions (each valid in S), `schemerge` encodes each version's
differences from the initial Document and combines them into one new Document
that is **guaranteed to be valid in S**.

To make this possible, schema S must be valid in the included
[metaschema](schemerge/metaschema.json) which is itself
valid in, and a subset of, the [JSON schema metaschema](https://json-schema.org/specification).

Elements may be inserted, modified,
deleted or moved anywhere in the Document; edits follow Elements to wherever
they were moved, and changes that would break the schema are bubbled up to
parent Elements.

No runtime dependencies (Python ≥ 3.9). `pytest` and, optionally,
`jsonschema` ≥ 4.18 (used only to cross-check the validator) are needed for
the tests.

## Origin

This is an idea I've been working on on-and-off for literally years which I ended
up handing to Claude as an exercise to see how that worked, and to my great
surprise it worked very quickly.

The inital prompt was:

> A Document is a deserialized JSON document.
> An Element is an Object, Array or Scalar in a Document.
> A Schema is a Document which is a valid JSON schema.
> Write a Python package containing a class which can take a Schema S in its constructor.
> Write a method to accept an initial Document which is valid in S.
> Write a method to accept further documents, also valid in S, which are modified versions of the initial document and encode their differences from it.
> Write a method to combine the encoded differences into a new document which is valid in S.
> Elements may have been inserted, modifed, deleted or moved within the Document.
> Differences may be reordered to ensure that information isn't lost.
> Changes which would result in schema violations should be "bubbled up" to parent Elements to ensure the output is still valid in S.
> Only a subset of JSON Schema needs to be supported. External references do not need to be supported.
> Write a metaschema which describes the subset of JSON schema for which this code will work.  The schema S should be checked against this metaschema.
> Write many test cases illustrating the range of situations this code is able to address.

... and I later added:

> When a scalar element is changed in multiple differences treat that as a deletion and insertion and keep both new values though, where that is allowed by the schema.

## License

[CC0](LICENSE)

## Quick start

```python
from schemerge import SchemaMerger

schema = {
    "type": "object",
    "properties": {
        "todo": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "done": {"type": "array", "items": {"$ref": "#/$defs/task"}},
    },
    "$defs": {"task": {"type": "object", "required": ["title"],
                       "properties": {"title": {"type": "string"}, "prio": {"type": "integer"}}}},
}

merger = SchemaMerger(schema)                       # S is checked against the metaschema
merger.set_base({"todo": [{"title": "write", "prio": 1}], "done": []})

d1 = merger.add_version({"todo": [], "done": [{"title": "write", "prio": 1}]})   # moved
d2 = merger.add_version({"todo": [{"title": "write", "prio": 5}], "done": []})   # edited
print(d1.to_json())   # {'format': 'schemerge-diff/1', 'ops': [{'op': 'move', 'path': '/todo/0', ...}]}

merger.merge()        # {'todo': [], 'done': [{'title': 'write', 'prio': 5}]}
```

`merge_report()` returns a `MergeResult` with the `document`, the `conflicts`
that were resolved and the schema violations that were `bubbled` up.

## Command line

Installing the package (`pip install .`) provides a `schemerge` command;
`python -m schemerge` works too.

```
schemerge BASE VERSION [VERSION ...] [-s SCHEMA] [-o OUTPUT] [options]
```

`BASE` and each `VERSION` are JSON files (`-` reads one of them from standard
input). Versions are listed lowest priority first. The merged document is
written as JSON to `OUTPUT` (atomically, so a failed run never leaves a
partial file) or to standard output. Without `--schema` the boolean schema
`true` is used: it is valid in the metaschema and accepts every document.

```console
$ schemerge base.json alice.json bob.json -o merged.json
schemerge: merged 2 version(s): 1 conflict(s) resolved, 0 schema violation(s) bubbled up
  value conflict at /tags/1 [alice.json, bob.json]: kept every value: the item was deleted and each new value inserted ["X", "Y"]

$ schemerge -s schema.json base.json alice.json bob.json --compact --strict
{"todo":[],"done":[{"title":"write","prio":5}],"tags":["a","Y"]}
schemerge: merged 2 version(s): 1 conflict(s) resolved, 0 schema violation(s) bubbled up
  value conflict at /tags/1 [alice.json, bob.json]: value from bob.json used (keeping every value would violate the schema)
```

| Option | |
|---|---|
| `-s`, `--schema SCHEMA` | JSON Schema (in the supported subset) every input must be valid in. |
| `-o`, `--output OUTPUT` | Where to write the merged document (default: standard output). |
| `--report FILE` | Also write a JSON report of the conflicts and bubbled-up violations. |
| `--on-delete-conflict keep\|delete` | Element removed by one version but changed by another (default `keep`). |
| `--on-value-conflict keep\|latest` | Scalar array item changed differently by several versions (default `keep`). |
| `--similarity-threshold X` | Minimum similarity (0..1) for matching modified array items (default 0.5). |
| `--indent N`, `--compact`, `--sort-keys` | Output layout (default: indent 2, keys in merge order). |
| `--strict` | Exit with status 1 if anything had to be resolved (the output is still written). |
| `-q`, `--quiet` | No summary on standard error. |

Input must be strict JSON in UTF-8: `NaN`/`Infinity` and duplicate object keys
are rejected. Exit status is 0 on success, 1 with `--strict` when conflicts or
violations were resolved, and 2 for errors (unreadable or invalid JSON, a
schema outside the subset, an input not valid in the schema); errors name the
offending file and list the schema issues.

## API

| | |
|---|---|
| `SchemaMerger(schema, on_delete_conflict="keep", on_value_conflict="keep", similarity_threshold=0.5)` | Checks `schema` against the metaschema (`SchemaError` if it is outside the subset). |
| `set_base(document)` | Accepts the initial Document (`DocumentInvalidError` unless valid in S). Clears versions. |
| `add_version(document) -> Diff` | Accepts a modified Document valid in S; encodes and stores its differences. |
| `add_diff(diff)` | Accepts an already encoded diff (a `Diff` or its JSON form); the replayed Document must be valid in S. |
| `merge() -> document` | Combines all stored diffs into a new Document valid in S. |
| `merge_report() -> MergeResult` | Same, plus `conflicts` and `bubbled`. |
| `version(i)`, `diffs`, `base`, `validate(doc)` | Introspection helpers. |

Lower-level functions: `compute_diff(base, version)`, `apply_diff(base, diff)`,
`merge_diffs(base, diffs, validator=None, on_delete_conflict="keep", on_value_conflict="keep")`,
`check_schema(schema)`, `Validator(schema).errors(doc)`.

## Encoded differences

A diff is a JSON-serializable list of operations. Base Elements are named by
their JSON Pointer *in the base Document*; new Elements get ids `+1`, `+2`, ….
Because operations address Elements by identity rather than position, an edit
made at an Element's old location can be replayed at its new location.

| Operation | Meaning |
|---|---|
| `{"op": "remove", "path": P}` | Base Element P (and its descendants not moved elsewhere) is gone. |
| `{"op": "set", "path": P, "value": V}` | Base Scalar P has a new value (including a new type). |
| `{"op": "move", "path": P, <loc>}` | Base Element P (with its subtree) now lives at `<loc>`. |
| `{"op": "add", "id": "+N", <loc>, "value": V}` | New Element. An empty container that later operations use as a parent is a *skeleton* (e.g. a wrapper around moved Elements). |

`<loc>` is `"parent": null` (the root), `"parent": R, "key": K` (member of an
Object) or `"parent": R, "after": A` (in an Array right after sibling A;
`null` = first). `apply_diff(base, compute_diff(base, v)) == v` holds exactly;
the test-suite checks it on hundreds of random documents.

**How Elements are matched.** Roots, then object members by key and array
items by a longest common subsequence of structural hashes; identical items
that changed order; identical non-empty containers anywhere (moves, renames,
swaps, wrapping and unwrapping), along with the same-key siblings that
travelled with them; containers most of whose children came from one
container (moved *and* modified); finally, remaining array items by
similarity (`similarity_threshold`).

## Merge semantics

Versions are combined in the order they were added; **later versions win
genuine conflicts**. Differences are not replayed in sequence but resolved per
Element, which is how information is preserved:

* **Edits follow moves.** A version that edits inside an Element and another
  that moves or renames it combine: the edit lands at the new location.
* **Moves beat deletes.** Moving an Element out of a container that another
  version deletes saves the Element.
* **Edits vs. removals.** With `on_delete_conflict="keep"` (default), an
  Element removed by one version but edited, reordered, moved into, or
  inserted into by another version is kept. With `"delete"` the removal wins.
* **Scalars changed differently by several versions.** With
  `on_value_conflict="keep"` (default), a Scalar *array item* is treated as a
  deletion plus insertions: every distinct new value is kept, in version
  order, where the old item was (`["a","b","c"]` changed to `X` and to `Y`
  gives `["a","X","Y","c"]`). Each such expansion is kept only if it adds no
  schema violation (it would break `maxItems`, `uniqueItems`, a combinator on
  the array, …); expansions are tried in document order, so an earlier one
  can use up room a later one needed. When an expansion is refused, and for
  object members and the root, which hold a single value, the later version's
  value wins. `on_value_conflict="latest"` always uses the later value.
* **Objects.** Different keys combine. Two versions adding the same key with
  different objects → the objects are combined recursively.
* **Arrays.** Base order, adjusted by each version's reorders; insertions go
  after their nearest surviving predecessor (or before their successor if the
  predecessor was reordered by another version), concurrent insertions at the
  same place are kept in version order, and identical concurrent insertions
  are kept once.
* **Identical changes** in several versions (including identical new wrapper
  structures) are unified, so merging a version with itself reproduces it.
* **Cycles** (one version moves A into B, another B into A) are broken by
  dropping the earliest version's move.

Each resolved disagreement is reported as a `Conflict(kind, location,
versions, resolution)` with kind `value`, `move`, `delete-vs-edit`,
`key-collision`, `root` or `cycle`.

## Bubbling up schema violations

After combining, the result is validated against S. While it is invalid:

1. Take the deepest violation (the validator reports each issue at the
   Element at fault — e.g. `maxItems` at the Array, `required` at the Object,
   `oneOf` at the Element the combinator applies to).
2. Replace that Element wholesale by the same Element as it appears in one of
   the versions (latest first) or the base, accepting the first candidate that
   removes every violation inside it without adding violations elsewhere.
3. If no candidate works, move to the **parent Element** and repeat.
4. At the root the candidates are the complete version Documents, each valid
   in S, so the loop always terminates with a valid Document.

Value expansions (above) are decided before this step, so they never cause
bubbling themselves.

Examples covered by the tests: two versions appending past `maxItems`;
concurrent edits creating duplicates under `uniqueItems`; removals going below
`minItems`; a rescued property violating `oneOf` (email vs. phone); a
discriminated union whose variant changed in one version while another edited
the old variant; `dependentRequired` broken at the root; an Element moved into
a region of the schema where another version's edit is not allowed, escalating
from a new member to the moved Element.

## Supported JSON Schema subset

The subset is defined by [`schemerge/metaschema.json`](schemerge/metaschema.json)
(itself written in the subset, so it validates against itself and is a valid
draft 2020-12 schema). Every S is validated against it, and additionally every
`$ref` must resolve to a subschema and every regular expression must compile.

* Core: `$schema`, `$id`, `$comment`, `$defs`, `definitions`, local `$ref`
  (`#` or `#/json/pointer`, recursion allowed), boolean schemas.
* Annotations (ignored): `title`, `description`, `default`, `examples`,
  `deprecated`, `readOnly`, `writeOnly`.
* Any type: `type` (name or array of names), `enum`, `const`.
* Numbers: `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` (numeric
  form), `multipleOf`.
* Strings: `minLength`, `maxLength`, `pattern`.
* Arrays: `items` (single schema), `minItems`, `maxItems`, `uniqueItems`.
* Objects: `properties`, `patternProperties`, `additionalProperties`,
  `required`, `dependentRequired`, `minProperties`, `maxProperties`.
* Combinators: `allOf`, `anyOf`, `oneOf`, `not`.

Not supported (rejected by the metaschema): external references, anchors,
`if`/`then`/`else`, `prefixItems`/tuple `items`, `contains`, `propertyNames`,
`dependentSchemas`, `unevaluated*`, `$dynamicRef`, `format`.

Notes: `pattern` uses Python's `re` (identical to ECMA-262 for common
patterns); `multipleOf` uses a small relative tolerance, so `0.3` is a
multiple of `0.1`; JSON equality is used throughout (`1 == 1.0`, `1 != true`,
object key order is irrelevant).

## Limitations

* An Element that was moved *and* changed so much that none of its children
  survived identically is seen as removed + added; with the default policy both
  copies are kept.
* Scalars are only recognised as moved when they move with an identical
  container sibling or within the same Array.
* A Scalar array item whose JSON *type* changed (e.g. `2` → `"two"`) is
  encoded as a removal plus an insertion, so its new value is placed like an
  insertion (just before the item) rather than in version order among the
  kept values.
* When versions genuinely conflict, one side's change is necessarily dropped;
  every such case is listed in `MergeResult.conflicts` / `bubbled`.
* Diffing uses an LCS per Array (quadratic in the length of changed regions).

## Tests

```
pip install "pytest>=7" "jsonschema>=4.18"
pytest
```

GitHub Actions ([`.github/workflows/tests.yml`](.github/workflows/tests.yml))
runs the suite on every push and pull request, on Python 3.9 to 3.14, then
installs the package and smoke-tests the `schemerge` command. A separate job
runs without `jsonschema` to check that the cross-checks skip cleanly.

The cross-checks against `jsonschema` are skipped if it is not installed. The
metaschema cross-check is skipped on jsonschema ≤ 4.17: its legacy
`RefResolver` treats any dict containing a `"$id"` key as a schema, including
the metaschema's `properties` map (which must list `$id` as an allowed
keyword), and fails with `TypeError: unhashable type: 'dict'`.

About 2,300 test cases: validator keyword semantics (cross-checked
against `jsonschema`), accepted and rejected schemas, the shape of individual
diffs and random diff round-trips, more than fifty hand-written merge
scenarios, schema-bubbling scenarios, value-conflict scenarios, API behaviour, and randomised merge
properties (single-version reproduction, idempotence, disjoint edits combine
exactly, validity under a constrained schema for both delete policies, and
validity when conflicting array values are kept under array constraints),
and the command line tool.
