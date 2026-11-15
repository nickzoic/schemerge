"""Combining the encoded differences of several versions into one Document.

Overview
========
1. **Collect** every version's operations, keyed by Element identity.  New
   Elements that several versions created identically (same parent, same
   key or array anchor, and the same literal value or same kind of skeleton
   container) are unified into one Element.
2. **Reorder** instead of replaying operations in sequence: an Element's final
   *location* (from moves), *value* (from sets) and *existence* (from removes)
   are resolved independently, so an edit made at an Element's old location
   follows it to its new location, and moving an Element out of a container
   that another version deletes saves it.
3. **Removal vs edit** (``on_delete_conflict``): with ``"keep"`` (default) a
   removed Element survives when another version edited, moved or inserted
   anything inside it, so no information is lost; with ``"delete"`` the
   removal wins.
4. **Cycles** created by moves from different versions (A into B, B into A)
   are broken by dropping the move from the earliest version.
5. **Build** the tree from the root.  Elements competing for the same object
   key are resolved by priority (later versions win; concurrently added
   objects are combined).  Array order is the base order, adjusted by each
   version's reorders, with insertions placed after their nearest surviving
   predecessor and kept in version order; identical concurrent insertions at
   the same place are kept once.
   A Scalar *array item* set to different values by several versions is
   treated as a deletion plus insertions: every distinct new value is kept,
   in version order, where the old item was - provided that introduces no
   schema violation (``on_value_conflict="keep"``, the default).  Otherwise,
   and for object members and the root, which hold a single value, the
   latest version's value is used.
6. **Bubble up**: while the result violates the schema, take the deepest
   violation and replace the offending Element wholesale with the same Element
   as it appears in one of the versions (latest first) or the base.  If no
   candidate fixes it without breaking anything else, move to the parent
   Element and try again.  At the root the candidates are the complete version
   Documents, each valid by construction, so the result is always valid.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from .diff import Diff, Loc, VersionView
from .jsonutil import OBJECT, canonical, get_at, is_under, set_at, to_pointer
from .tree import IndexedDoc
from .validator import Validator

KEEP = "keep"
DELETE = "delete"
LATEST = "latest"


@dataclass
class Conflict:
    """Two or more versions disagreed; ``resolution`` says what was done.

    ``location`` is the base JSON Pointer of the Element concerned, or, for
    ``key-collision`` / ``root`` conflicts, the pointer in the merged result.
    New Elements are identified as ``v<version>+<n>``.
    """

    kind: str
    location: str
    versions: list
    resolution: str

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Bubble:
    """A schema violation detected at ``origin`` and resolved at ``path``."""

    origin: str
    path: str
    issues: list
    resolution: str

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class MergeResult:
    document: Any
    conflicts: list = field(default_factory=list)
    bubbled: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"document": self.document,
                "conflicts": [c.to_json() for c in self.conflicts],
                "bubbled": [b.to_json() for b in self.bubbled]}


def _who(j: int) -> str:
    return "base" if j < 0 else f"version {j}"


def _deep_union(low: Any, high: Any) -> Any:
    if isinstance(low, dict) and isinstance(high, dict):
        out = dict(low)
        for k, v in high.items():
            out[k] = _deep_union(low[k], v) if k in low else v
        return out
    return high


class _Merge:
    def __init__(self, base: IndexedDoc, diffs: list, validator: Optional[Validator], policy: str,
                 value_policy: str = KEEP):
        if policy not in (KEEP, DELETE):
            raise ValueError(f"on_delete_conflict must be {KEEP!r} or {DELETE!r}")
        if value_policy not in (KEEP, LATEST):
            raise ValueError(f"on_value_conflict must be {KEEP!r} or {LATEST!r}")
        self.value_policy = value_policy
        self.expand: set[str] = set()                      # array scalars whose values are all kept
        self.exp_info: dict[str, tuple] = {}               # ref -> (array path, index, count, winner offset)
        self.B = base
        self.validator = validator
        self.policy = policy
        self.views = [VersionView(base, d.ops if isinstance(d, Diff) else d, ns=f"v{j}")
                      for j, d in enumerate(diffs)]
        self.n = len(self.views)
        self.conflicts: list[Conflict] = []
        self.bubbled: list[Bubble] = []
        self.aliases: dict[str, list[str]] = defaultdict(list)

    def _conflict(self, kind, location, versions, resolution) -> None:
        vs = sorted({v for v in versions if v >= 0})
        self.conflicts.append(Conflict(kind, location, vs, resolution))

    # -- 1. collect -------------------------------------------------------
    def _c(self, ref: Optional[str]) -> Optional[str]:
        """Canonical ref: unified new Elements map to their representative."""
        return self.canon.get(ref, ref) if ref is not None else None

    def _tloc(self, loc: Loc) -> Loc:
        return Loc(self._c(loc.parent), loc.key, self._c(loc.after), loc.in_array)

    def _local(self, j: int, r: str) -> Optional[str]:
        """The ref version ``j`` uses for canonical Element ``r`` (None if absent)."""
        if r in self.creators:
            return self.local[r].get(j)
        return r

    def _collect(self) -> None:
        self.move_cands: dict[str, list[tuple[int, Loc]]] = {}
        self.sets: dict[str, list[tuple[int, Any]]] = defaultdict(list)
        self.removes: dict[str, set[int]] = defaultdict(set)
        self.new: dict[str, tuple[int, Loc]] = {}          # representative -> (first creator, loc)
        self.creators: dict[str, list[int]] = {}           # representative -> versions
        self.local: dict[str, dict[int, str]] = {}         # representative -> {version: own ref}
        self.canon: dict[str, str] = {}
        signatures: dict[tuple, str] = {}
        moves: dict[str, list] = defaultdict(list)
        for j, view in enumerate(self.views):
            for r, (op, loc) in view.adds.items():   # op order: parents before children
                loc = self._tloc(loc)
                if view.is_literal(r):
                    shape = ("literal", canonical(op["value"]))
                else:
                    shape = ("skeleton", "object" if isinstance(op["value"], dict) else "array")
                sig = (loc.parent, loc.key, loc.after, loc.in_array, shape)
                rep = signatures.get(sig)
                if rep is not None and j not in self.creators[rep]:
                    self.canon[r] = rep
                    self.creators[rep].append(j)
                    self.local[rep][j] = r
                    continue
                signatures[sig] = r
                self.new[r] = (j, loc)
                self.creators[r] = [j]
                self.local[r] = {j: r}
        for j, view in enumerate(self.views):
            for r, (_, loc) in view.moves.items():
                moves[r].append((j, self._tloc(loc)))
            for r, value in view.sets.items():
                self.sets[r].append((j, value))
            for r in view.removed:
                self.removes[r].add(j)
        for r, cands in moves.items():
            cands.sort(key=lambda t: -t[0])
            self.move_cands[r] = cands
            if len({(l.parent, l.key, l.after) for _, l in cands}) > 1:
                self._conflict("move", r, [j for j, _ in cands],
                               f"placement from {_who(cands[0][0])} used")

    # -- 2. touches ---------------------------------------------------------
    def _touches(self) -> None:
        """Base Elements each version edited inside (used to rescue removals)."""
        self.touched: list[set[str]] = []
        for view in self.views:
            t: set[str] = set()
            for r in view.sets:
                t.update(self.B.ancestors_and_self(r))
            for r, (_, loc) in list(view.moves.items()) + list(view.adds.items()):
                parent = loc.parent
                while parent is not None and parent in view.adds:
                    parent = view.adds[parent][1].parent
                if parent is not None:
                    t.update(self.B.ancestors_and_self(parent))
            self.touched.append(t)

    def _toucher_versions(self, r: str) -> list[int]:
        removers = self.removes.get(r, set())
        return [k for k in range(self.n) if k not in removers and r in self.touched[k]]

    # -- 3. removals --------------------------------------------------------
    def _deaths(self) -> None:
        self.dead: set[str] = set()
        for r, removers in self.removes.items():
            edits = sorted(set(self._toucher_versions(r)) | {j for j, _ in self.move_cands.get(r, [])})
            who = ", ".join(_who(k) for k in edits)
            if self.policy == KEEP and edits:
                self._conflict("delete-vs-edit", r, list(removers) + edits,
                               f"kept: removal overridden because of changes by {who}")
            else:
                self.dead.add(r)
                if edits:
                    self._conflict("delete-vs-edit", r, list(removers) + edits,
                                   f"removed: changes by {who} discarded")

    # -- locations ------------------------------------------------------------
    def _loc_of(self, r: str) -> Loc:
        if r in self.new:
            return self.new[r][1]
        cands = self.move_cands.get(r)
        if cands:
            return cands[0][1]
        node = self.B.node(r)
        if node.parent is None:
            return Loc(None)
        parent = self.B.nodes[node.parent]
        if parent.kind == OBJECT:
            return Loc(parent.ref, key=node.key)
        return Loc(parent.ref, in_array=True)

    def _placer(self, r: str) -> Optional[int]:
        if r in self.new:
            return self.new[r][0]
        cands = self.move_cands.get(r)
        return cands[0][0] if cands else None

    def _order_placer(self, r: str) -> Optional[int]:
        """Version whose array order places ``r``: the earliest one agreeing with the winner."""
        if r in self.new:
            return self.new[r][0]
        cands = self.move_cands.get(r)
        if not cands:
            return None
        win = cands[0][1]
        return min(j for j, loc in cands
                   if (loc.parent, loc.key, loc.after) == (win.parent, win.key, win.after))

    def _priority(self, r: str) -> int:
        if r in self.new:
            return max(self.creators[r])
        placer = self._placer(r)
        if placer is not None:
            return placer
        return max(self._toucher_versions(r), default=-1)

    # -- 4. cycles ------------------------------------------------------------
    def _find_cycle(self) -> Optional[list[str]]:
        state: dict[str, int] = {}
        for start in [n.ref for n in self.B.nodes] + list(self.new):
            if start in state:
                continue
            path, r = [], start
            while r is not None and r not in state:
                state[r] = 1
                path.append(r)
                r = self._loc_of(r).parent
            if r is not None and state[r] == 1:
                return path[path.index(r):]
            for x in path:
                state[x] = 2
        return None

    def _break_cycles(self) -> None:
        while True:
            cycle = self._find_cycle()
            if not cycle:
                return
            movable = [r for r in cycle if self.move_cands.get(r)]
            victim = min(movable, key=lambda r: self.move_cands[r][0][0])
            j, _ = self.move_cands[victim].pop(0)
            others = [self._placer(r) for r in movable if r != victim]
            self._conflict("cycle", victim, [j] + [o for o in others if o is not None],
                           f"move by {_who(j)} dropped because it would nest an element inside itself")

    # -- 5. build -------------------------------------------------------------
    def _index_children(self) -> None:
        self.kids: dict[Optional[str], list[str]] = defaultdict(list)
        for node in self.B.nodes:
            if node.ref not in self.dead:
                self.kids[self._loc_of(node.ref).parent].append(node.ref)
        for r, (_, loc) in self.new.items():
            self.kids[loc.parent].append(r)

    def _order_key(self, r: str):
        placer = self._order_placer(r)
        if placer is None:
            return (0, self.B.by_ref[r], 0)
        return (1, placer, self.views[placer].seq[r])

    def _literal_key(self, r: str):
        if r not in self.new:
            return None
        view = self.views[self.new[r][0]]
        return canonical(view.adds[r][0]["value"]) if view.is_literal(r) else None

    def _vseq(self, j: int, r: str) -> Optional[list[str]]:
        """Version ``j``'s order of the items of array ``r`` (canonical refs)."""
        view, lr = self.views[j], self._local(j, r)
        if lr is None or not view.present(lr):
            return None
        return [self._c(x) for x in view.array_items(lr)]

    @staticmethod
    def _neighbours(vseq: list[str], r: str, seq: list[str]):
        """Nearest predecessor and successor of ``r`` in a version that survive in ``seq``."""
        present = set(seq)
        i = vseq.index(r) if r in vseq else len(vseq)
        pred = next((vseq[k] for k in range(i - 1, -1, -1) if vseq[k] in present), None)
        succ = next((vseq[k] for k in range(i + 1, len(vseq)) if vseq[k] in present), None)
        return pred, succ

    @classmethod
    def _anchor_pos(cls, vseq: list[str], r: str, seq: list[str]) -> int:
        pred, _ = cls._neighbours(vseq, r, seq)
        return 0 if pred is None else seq.index(pred) + 1

    def _array_order(self, r: str, members: list[str]) -> list[str]:
        mset = set(members)
        repositioned: dict[str, int] = {}
        if r in self.new:
            # a new array: its first creator's order, then other creators' insertions
            seq = [c for c in (self._vseq(self.new[r][0], r) or []) if c in mset]
            natives = set(seq)
        else:
            node = self.B.node(r)
            seq = [self.B.nodes[c].ref for c in node.children if self.B.nodes[c].ref in mset]
            natives = set(seq)
            # (a) reorders of native items, earlier versions first so later ones win
            for j, view in enumerate(self.views):
                if not view.present(r):
                    continue
                vseq = None
                for c, (_, loc) in view.moves.items():
                    if c in natives and loc.parent == r and any(jj == j for jj, _ in self.move_cands.get(c, [])):
                        if vseq is None:
                            vseq = self._vseq(j, r)
                        seq.remove(c)
                        seq.insert(self._anchor_pos(vseq, c, seq), c)
                        repositioned[c] = j
        # (b) insertions (new items and items moved in from elsewhere)
        inserted_by: dict[str, int] = {}
        for j, view in enumerate(self.views):
            lr = self._local(j, r)
            if lr is None or not view.present(lr):
                continue
            vseq = None
            for c in (self._c(x) for x in view.placed.get(lr, [])):
                if c in natives or c not in mset or c in inserted_by or self._order_placer(c) != j:
                    continue
                if vseq is None:
                    vseq = self._vseq(j, r)
                pred, succ = self._neighbours(vseq, c, seq)
                lit, dup = self._literal_key(c), None
                # If the predecessor was reordered by another version, anchor on the
                # successor (or the end) instead so the insertion is not dragged along.
                if (pred is not None and repositioned.get(pred, j) != j
                        and (succ is None or repositioned.get(succ, j) == j)):
                    pos = len(seq) if succ is None else seq.index(succ)
                    k = pos - 1
                    while k >= 0 and seq[k] in inserted_by and inserted_by[seq[k]] < j:
                        if lit is not None and self._literal_key(seq[k]) == lit:
                            dup = seq[k]
                            break
                        k -= 1
                else:
                    pos = 0 if pred is None else seq.index(pred) + 1
                    while pos < len(seq) and seq[pos] in inserted_by and inserted_by[seq[pos]] < j:
                        if lit is not None and self._literal_key(seq[pos]) == lit:
                            dup = seq[pos]
                            break
                        pos += 1
                if dup is not None:
                    self.aliases[dup].append(c)  # identical concurrent insertion: keep once
                    continue
                seq.insert(pos, c)
                inserted_by[c] = j
        placed = set(seq)
        return seq + [m for m in members if m not in placed and m not in self._aliased()]

    def _aliased(self) -> set[str]:
        return {a for lst in self.aliases.values() for a in lst}

    def _value_choices(self, r: str):
        """For a base Scalar set to different values: (values in version order, winner offset)."""
        if r in self.new or self.B.node(r).kind != "scalar":
            return None
        cands = sorted(self.sets.get(r, []), key=lambda t: t[0])
        if len({canonical(v) for _, v in cands}) < 2:
            return None
        values, keys = [], []
        for _, v in cands:
            c = canonical(v)
            if c not in keys:
                keys.append(c)
                values.append(v)
        return values, keys.index(canonical(cands[-1][1]))

    def _build(self, r: str, path: tuple, pm: dict) -> Any:
        pm[path] = [r] + self.aliases.get(r, [])
        if r in self.new:
            view = self.views[self.new[r][0]]
            if view.is_literal(r):
                return copy.deepcopy(view.adds[r][0]["value"])
            kind = view.kind(r)
        else:
            node = self.B.node(r)
            kind = node.kind
            if kind == "scalar":
                cands = sorted(self.sets.get(r, []), key=lambda t: -t[0])
                if not cands:
                    return node.value
                if len({canonical(v) for _, v in cands}) > 1:
                    why = ""
                    if self.value_policy == KEEP:
                        why = (" (keeping every value would violate the schema)" if r in self.exp_info
                               else " (only one value fits here)")
                    self._conflict("value", r, [j for j, _ in cands], f"value from {_who(cands[0][0])} used{why}")
                return copy.deepcopy(cands[0][1])
        members = [m for m in self.kids.get(r, []) if m not in self.dead]
        if kind == OBJECT:
            groups: dict[str, list[str]] = {}
            for m in sorted(members, key=self._order_key):
                groups.setdefault(self._loc_of(m).key, []).append(m)
            return {key: self._resolve(cands, path + (key,), pm) for key, cands in groups.items()}
        order = self._array_order(r, members)
        out: list = []
        for m in order:
            choice = self._value_choices(m) if self.value_policy == KEEP else None
            if choice is not None:
                values, winner = choice
                if m in self.expand:
                    # deletion of the old item plus one insertion per distinct new value
                    self.exp_info[m] = (path, len(out), len(values), winner)
                    for v in values:
                        pm[path + (len(out),)] = [m]
                        out.append(copy.deepcopy(v))
                    versions = [j for j, _ in self.sets[m]]
                    self._conflict("value", m, versions,
                                   "kept every value: the item was deleted and each new value inserted "
                                   + json.dumps(values))
                    continue
                self.exp_info[m] = (path, len(out), 1, 0)
            out.append(self._build(m, path + (len(out),), pm))
        return out

    def _resolve(self, cands: list[str], path: tuple, pm: dict) -> Any:
        """Build the Element for a slot several Elements compete for."""
        if len(cands) == 1:
            return self._build(cands[0], path, pm)
        ranked = sorted(cands, key=self._priority, reverse=True)
        built = []
        for m in ranked:
            mark = len(self.conflicts)
            tpm: dict = {}
            value = self._build(m, path, tpm)
            built.append((m, value, tpm, self.conflicts[mark:]))
            del self.conflicts[mark:]
        where = to_pointer(path)
        prios = [self._priority(m) for m in ranked]
        winner, value, tpm, cf = built[0]
        if len({canonical(v) for _, v, _, _ in built}) == 1:
            pm.update(tpm)
            self.conflicts.extend(cf)
            pm[path] = [m for m, _, _, _ in built]
            return value
        if all(m in self.new and self.views[self.new[m][0]].is_literal(m) and isinstance(v, dict)
               for m, v, _, _ in built):
            merged: Any = {}
            for _, v, _, _ in reversed(built):
                merged = _deep_union(merged, v)
            pm[path] = [m for m, _, _, _ in built]
            self._conflict("key-collision", where, prios, "objects added concurrently were combined")
            return merged
        pm.update(tpm)
        self.conflicts.extend(cf)
        kind = "root" if path == () else "key-collision"
        self._conflict(kind, where, prios, f"kept the element from {_who(prios[0])}")
        return value

    # -- 6. schema enforcement --------------------------------------------------
    def _candidates(self, p: tuple, pm: dict):
        if p == ():
            for j in reversed(range(self.n)):
                yield _who(j), self.views[j].document()
            yield "base", copy.deepcopy(self.B.doc)
            return
        for r in pm.get(p, []):
            if r in self.new:
                for j in sorted(self.creators[r], reverse=True):
                    yield _who(j), self.views[j].value(self.local[r][j])
                continue
            for j in reversed(range(self.n)):
                if self.views[j].present(r):
                    yield _who(j), self.views[j].value(r)
            yield "base", copy.deepcopy(self.B.node(r).value)

    def _enforce(self, doc: Any, pm: dict) -> Any:
        validator = self.validator
        if validator is None:
            return doc
        issues = validator.errors(doc)
        handled: set[tuple] = set()
        while issues:
            origin = max(issues, key=lambda i: len(i.path))
            p = tuple(origin.path)
            while p and (p not in pm or p in handled):
                p = p[:-1]
            here = [str(i) for i in issues if is_under(i.path, p)]
            outside = {(i.path, i.keyword) for i in issues if not is_under(i.path, p)}
            seen = {canonical(get_at(doc, p))}
            chosen = None
            for label, cand in self._candidates(p, pm):
                c = canonical(cand)
                if c in seen:
                    continue
                seen.add(c)
                trial = set_at(doc, p, cand)
                new = validator.errors(trial)
                if any(is_under(i.path, p) for i in new):
                    continue
                if not {(i.path, i.keyword) for i in new} <= outside:
                    continue
                chosen = (label, trial, new)
                break
            if chosen is None:
                if p == ():
                    raise RuntimeError("no valid document found; the inputs cannot all have been valid")
                handled.add(p)
                continue
            label, doc, issues = chosen
            handled.add(p)
            for q in [q for q in pm if len(q) > len(p) and q[:len(p)] == p]:
                del pm[q]
            self.bubbled.append(Bubble(to_pointer(origin.path), to_pointer(p), here, label))
        return doc

    # -- driver -----------------------------------------------------------------
    def _build_document(self, expand) -> tuple[Any, dict]:
        """Build the merged tree, keeping every value of the array scalars in ``expand``."""
        del self.conflicts[self._build_mark:]
        self.aliases = defaultdict(list)
        self.expand = set(expand)
        self.exp_info = {}
        pm: dict = {}
        roots = [m for m in self.kids.get(None, []) if m not in self.dead]
        if roots:
            return self._resolve(roots, (), pm), pm
        self._conflict("root", "", [], "every candidate root was removed; base document kept")
        return copy.deepcopy(self.B.doc), {(): [""]}

    @staticmethod
    def _expansion_ok(before: list, after: list, info: tuple) -> bool:
        """True if expanding one item introduced no schema violation.

        Issue paths of the expanded document are mapped back to the document
        before expansion (later items shift by ``count - 1``); any issue on an
        inserted item, or one that did not exist before, rejects the expansion.
        """
        array, start, count, winner = info
        old = {(i.path, i.keyword) for i in before}
        n = len(array)
        for issue in after:
            p = tuple(issue.path)
            if len(p) > n and p[:n] == array:
                k = p[n]
                if start <= k < start + count:
                    if k != start + winner:
                        return False
                    k = start
                elif k >= start + count:
                    k -= count - 1
                p = array + (k,) + p[n + 1:]
            if (p, issue.keyword) not in old:
                return False
        return True

    def _choose_expansions(self) -> tuple[Any, dict]:
        doc, pm = self._build_document(())
        candidates = list(self.exp_info)
        if self.value_policy != KEEP or not candidates:
            return doc, pm
        if self.validator is None:
            return self._build_document(candidates)
        everything, epm = self._build_document(candidates)
        if not self.validator.errors(everything):
            return everything, epm
        # Greedily, in document order: keep an expansion only if it adds no violation.
        enabled: list[str] = []
        doc, pm = self._build_document(enabled)
        issues = self.validator.errors(doc)
        for r in candidates:
            trial, tpm = self._build_document(enabled + [r])
            info = self.exp_info.get(r)
            if info is None or info[2] == 1:
                continue
            trial_issues = self.validator.errors(trial)
            if self._expansion_ok(issues, trial_issues, info):
                enabled.append(r)
                issues = trial_issues
        return self._build_document(enabled)

    def run(self) -> MergeResult:
        self._collect()
        self._touches()
        self._deaths()
        self._break_cycles()
        self._index_children()
        self._build_mark = len(self.conflicts)
        doc, pm = self._choose_expansions()
        doc = self._enforce(doc, pm)
        return MergeResult(doc, self.conflicts, self.bubbled)


def merge_diffs(base: Any, diffs: Iterable, validator: Optional[Validator] = None,
                on_delete_conflict: str = KEEP, on_value_conflict: str = KEEP) -> MergeResult:
    """Merge encoded diffs (earliest first; later versions have priority) onto ``base``."""
    if not isinstance(base, IndexedDoc):
        base = IndexedDoc(base)
    return _Merge(base, list(diffs), validator, on_delete_conflict, on_value_conflict).run()
