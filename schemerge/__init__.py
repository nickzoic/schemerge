"""schemerge - schema-aware structural diff and N-way merge of JSON documents."""
from .diff import Diff, DiffError, apply_diff, compute_diff
from .jsonutil import json_equal
from .merge import Bubble, Conflict, MergeResult, merge_diffs
from .merger import SchemaMerger
from .validator import (METASCHEMA, DocumentInvalidError, Issue, SchemaError,
                        Validator, check_schema)

__version__ = "1.0.0"

__all__ = [
    "SchemaMerger", "Diff", "DiffError", "apply_diff", "compute_diff", "merge_diffs",
    "MergeResult", "Conflict", "Bubble", "Validator", "Issue", "check_schema",
    "METASCHEMA", "SchemaError", "DocumentInvalidError", "json_equal",
]
