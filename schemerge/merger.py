"""The public entry point: :class:`SchemaMerger`."""
from __future__ import annotations

import copy
from typing import Any, Optional, Union

from .diff import Diff, VersionView, compute_diff
from .jsonutil import check_json
from .merge import KEEP, MergeResult, merge_diffs
from .tree import IndexedDoc
from .validator import Validator, check_schema


class SchemaMerger:
    """Schema-aware structural merge of JSON Documents.

    >>> m = SchemaMerger({"type": "object"})
    >>> m.set_base({"a": 1, "b": 1})
    >>> _ = m.add_version({"a": 2, "b": 1})
    >>> _ = m.add_version({"a": 1, "b": 3})
    >>> m.merge()
    {'a': 2, 'b': 3}

    Parameters
    ----------
    schema:
        A Schema S in the subset described by :data:`schemerge.METASCHEMA`.
        It is checked on construction (:class:`SchemaError` if unsupported).
    on_delete_conflict:
        ``"keep"`` (default): an Element removed by one version but changed
        inside by another is kept.  ``"delete"``: the removal wins.
    on_value_conflict:
        ``"keep"`` (default): a Scalar array item set to different values by
        several versions is replaced by all of the new values (a deletion plus
        insertions) wherever the schema allows it; elsewhere, and for object
        members and the root, the latest version's value is used.
        ``"latest"``: the latest version's value is always used.
    similarity_threshold:
        Minimum similarity (0..1) for two non-identical array items to be
        considered the same Element when diffing.
    """

    def __init__(self, schema: Any, *, on_delete_conflict: str = KEEP,
                 on_value_conflict: str = KEEP, similarity_threshold: float = 0.5):
        check_json(schema, "schema")
        self.schema = copy.deepcopy(schema)
        self.validator: Validator = check_schema(self.schema)
        if on_delete_conflict not in ("keep", "delete"):
            raise ValueError("on_delete_conflict must be 'keep' or 'delete'")
        self.on_delete_conflict = on_delete_conflict
        if on_value_conflict not in ("keep", "latest"):
            raise ValueError("on_value_conflict must be 'keep' or 'latest'")
        self.on_value_conflict = on_value_conflict
        self.similarity_threshold = similarity_threshold
        self._base: Optional[IndexedDoc] = None
        self._diffs: list[Diff] = []

    # -- inputs -------------------------------------------------------------
    def set_base(self, document: Any) -> None:
        """Accept the initial Document (must be valid in S).  Clears any versions."""
        check_json(document, "base document")
        self.validator.validate(document, "base document")
        self._base = IndexedDoc(copy.deepcopy(document))
        self._diffs = []

    def add_version(self, document: Any) -> Diff:
        """Accept a modified version of the base (valid in S); return its encoded diff."""
        base = self._require_base()
        check_json(document, "version document")
        self.validator.validate(document, f"version {len(self._diffs)}")
        diff = compute_diff(base, IndexedDoc(copy.deepcopy(document), base.interner),
                            self.similarity_threshold)
        self._diffs.append(diff)
        return diff

    def add_diff(self, diff: Union[Diff, dict]) -> Diff:
        """Accept an already encoded diff (a :class:`Diff` or its ``to_json()`` form).

        The diff is replayed against the base and the resulting Document must be
        valid in S.
        """
        base = self._require_base()
        if not isinstance(diff, Diff):
            diff = Diff.from_json(diff)
        else:
            diff = Diff(copy.deepcopy(diff.ops))
        document = VersionView(base, diff.ops).document()
        self.validator.validate(document, f"version {len(self._diffs)}")
        self._diffs.append(diff)
        return diff

    # -- output -------------------------------------------------------------
    def merge(self) -> Any:
        """Combine all encoded diffs into a new Document that is valid in S."""
        return self.merge_report().document

    def merge_report(self) -> MergeResult:
        """Like :meth:`merge` but also returns the conflicts and bubbled-up violations."""
        base = self._require_base()
        return merge_diffs(base, self._diffs, self.validator, self.on_delete_conflict,
                           self.on_value_conflict)

    # -- introspection --------------------------------------------------------
    @property
    def base(self) -> Any:
        return copy.deepcopy(self._require_base().doc)

    @property
    def diffs(self) -> list[Diff]:
        return [Diff(copy.deepcopy(d.ops)) for d in self._diffs]

    def version(self, index: int) -> Any:
        """Rebuild version ``index`` from the base and its encoded diff."""
        return VersionView(self._require_base(), self._diffs[index].ops).document()

    def validate(self, document: Any) -> list:
        """Schema issues of ``document`` (empty list when valid)."""
        return self.validator.errors(document)

    def _require_base(self) -> IndexedDoc:
        if self._base is None:
            raise RuntimeError("call set_base() before adding versions or merging")
        return self._base
