"""Structural differences between a base Document and a modified version.

Encoding
========
A :class:`Diff` is a list of JSON-serializable operations.  Every Element of the
*base* Document is identified by its JSON Pointer in the base (its "ref"); new
Elements are given ids ``"+1"``, ``"+2"``, ...  Because operations address
Elements by identity rather than by position, an edit made at an Element's old
location can later be replayed at its new location.

``{"op": "remove", "path": P}``
    Base Element ``P`` (and every descendant not moved elsewhere) is absent.
``{"op": "set", "path": P, "value": V}``
    Base Scalar ``P`` now has value ``V``.
``{"op": "move", "path": P, <location>}``
    Base Element ``P`` (with its subtree) now lives at ``<location>``.
``{"op": "add", "id": "+N", <location>, "value": V}``
    A new Element.  If ``V`` is an empty container that later operations use
    as a parent it is a *skeleton* (e.g. a wrapper around moved Elements);
    otherwise ``V`` is the complete literal value.

``<location>`` is one of
    ``"parent": null`` - the Element is the Document root;
    ``"parent": R, "key": K`` - member ``K`` of Object ``R``;
    ``"parent": R, "after": A`` - in Array ``R`` immediately after sibling
    ``A`` (``null`` means first).

Operations are emitted in pre-order of the new version, so every parent and
anchor is defined before it is referenced.
"""
from __future__ import annotations

import bisect
import copy
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Union

from .jsonutil import ARRAY, OBJECT, SCALAR, canonical, element_kind, is_number, json_type
from .tree import IndexedDoc

FORMAT = "schemerge-diff/1"


class DiffError(ValueError):
    """An encoded diff is malformed or does not fit the base Document."""


class Diff:
    """The encoded differences of one version from the base Document."""

    def __init__(self, ops: Iterable[dict]):
        self.ops: list[dict] = list(ops)

    def to_json(self) -> dict:
        return {"format": FORMAT, "ops": copy.deepcopy(self.ops)}

    @classmethod
    def from_json(cls, data: dict) -> "Diff":
        if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("ops"), list):
            raise DiffError(f"not a {FORMAT} document")
        return cls(copy.deepcopy(data["ops"]))

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for op in self.ops:
            counts[op["op"]] = counts.get(op["op"], 0) + 1
        return counts

    def __len__(self) -> int:
        return len(self.ops)

    def __iter__(self):
        return iter(self.ops)

    def __eq__(self, other) -> bool:
        return isinstance(other, Diff) and self.ops == other.ops

    def __repr__(self) -> str:
        return f"Diff({self.ops!r})"


# ---------------------------------------------------------------------------
# sequence helpers

def lcs_pairs(a: list, b: list) -> list[tuple[int, int]]:
    """Index pairs of a longest common subsequence of ``a`` and ``b``."""
    n, m = len(a), len(b)
    pre = 0
    while pre < n and pre < m and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and suf < m - pre and a[n - 1 - suf] == b[m - 1 - suf]:
        suf += 1
    a2, b2 = a[pre:n - suf], b[pre:m - suf]
    N, M = len(a2), len(b2)
    middle = []
    if N and M:
        dp = [[0] * (M + 1) for _ in range(N + 1)]
        for i in range(N - 1, -1, -1):
            row, nxt, ai = dp[i], dp[i + 1], a2[i]
            for j in range(M - 1, -1, -1):
                row[j] = nxt[j + 1] + 1 if ai == b2[j] else max(nxt[j], row[j + 1])
        i = j = 0
        while i < N and j < M:
            if a2[i] == b2[j]:
                middle.append((i + pre, j + pre))
                i += 1
                j += 1
            elif dp[i + 1][j] >= dp[i][j + 1]:
                i += 1
            else:
                j += 1
    return ([(k, k) for k in range(pre)] + middle
            + [(n - suf + k, m - suf + k) for k in range(suf)])


def lis_indices(seq: list) -> list[int]:
    """Indexes into ``seq`` of a longest strictly increasing subsequence."""
    tails: list = []
    tail_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, x in enumerate(seq):
        k = bisect.bisect_left(tails, x)
        if k == len(tails):
            tails.append(x)
            tail_idx.append(i)
        else:
            tails[k] = x
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k > 0 else -1
    out = []
    i = tail_idx[-1] if tail_idx else -1
    while i != -1:
        out.append(i)
        i = prev[i]
    return out[::-1]


def _scalar_family(value: Any) -> str:
    return "number" if is_number(value) else json_type(value)


# ---------------------------------------------------------------------------
# matching

class _Matcher:
    """Pairs Elements of the base with Elements of the version.

    Phases (repeated until nothing new is paired):

    1. top-down: roots, then object members by key and array items by an LCS
       on structural hashes;
    2. reorders: identical unmatched items of a matched array pair;
    3. moves: identical non-empty containers anywhere in the Documents, then
       siblings that travelled with them (same key / equal value under the
       new parent), then containers bottom-up: an unmatched container most of
       whose children came from one unmatched container of the same kind is
       that container, moved and modified;
    4. similarity: remaining array items in the gaps between matched items,
       paired in order when their similarity reaches the threshold; then
       leftover *containers* of the array paired regardless of order
       (reordered and modified).

    Pairing a member by key is suppressed when the two values were swapped
    with other members, or when one side is an identical copy of a container
    whose own path exists in only one of the Documents (it was moved here
    from a vanished key, or moved away to a brand-new key).  Swaps and
    renames therefore become moves instead of in-place rewrites, while a
    container that merely had its contents moved out stays where it is.
    """

    def __init__(self, B: IndexedDoc, V: IndexedDoc, threshold: float):
        self.B, self.V, self.threshold = B, V, threshold
        self.b2v: dict[int, int] = {}
        self.v2b: dict[int, int] = {}
        self.queue: deque = deque()
        self.array_pairs: list[tuple[int, int]] = []
        bpaths = {n.path: n for n in B.nodes}
        vpaths = {n.path: n for n in V.nodes}
        # out of place: content differs at the same path; vanished: path absent
        self.oop_b = {n.hid for n in B.nodes if n.children and
                      (n.path not in vpaths or vpaths[n.path].hid != n.hid)}
        self.oop_v = {n.hid for n in V.nodes if n.children and
                      (n.path not in bpaths or bpaths[n.path].hid != n.hid)}
        self.vanished_b = {n.hid for n in B.nodes if n.children and n.path not in vpaths}
        self.vanished_v = {n.hid for n in V.nodes if n.children and n.path not in bpaths}

    def run(self) -> None:
        if self.B.nodes[0].kind == self.V.nodes[0].kind:
            self.pair(0, 0)
        self.drain()
        while True:
            before = len(self.b2v)
            self.reorders()
            self.moves()
            self.similar()
            if len(self.b2v) == before:
                break

    def pair(self, b: int, v: int) -> None:
        self.b2v[b] = v
        self.v2b[v] = b
        self.queue.append((b, v))

    def drain(self) -> None:
        B, V = self.B, self.V
        while self.queue:
            b, v = self.queue.popleft()
            nb, nv = B.nodes[b], V.nodes[v]
            if nb.kind == OBJECT and nv.kind == OBJECT:
                for cb in nb.children:
                    if cb in self.b2v:
                        continue
                    cv = nv.keymap.get(B.nodes[cb].key)
                    if cv is None or cv in self.v2b or V.nodes[cv].kind != B.nodes[cb].kind:
                        continue
                    hb, hv = B.nodes[cb].hid, V.nodes[cv].hid
                    if hb != hv and ((hv in self.oop_b and hb in self.oop_v)
                                     or hv in self.vanished_b or hb in self.vanished_v):
                        continue  # swapped / moved over / moved away: let the move phase decide
                    self.pair(cb, cv)
            elif nb.kind == ARRAY and nv.kind == ARRAY:
                self.array_pairs.append((b, v))
                bc = [c for c in nb.children if c not in self.b2v]
                vc = [c for c in nv.children if c not in self.v2b]
                for i, j in lcs_pairs([B.nodes[c].hid for c in bc], [V.nodes[c].hid for c in vc]):
                    self.pair(bc[i], vc[j])

    def reorders(self) -> None:
        for b, v in list(self.array_pairs):
            avail: dict[int, deque] = defaultdict(deque)
            for c in self.B.nodes[b].children:
                if c not in self.b2v:
                    avail[self.B.nodes[c].hid].append(c)
            if not avail:
                continue
            for c in self.V.nodes[v].children:
                if c not in self.v2b:
                    q = avail.get(self.V.nodes[c].hid)
                    if q:
                        self.pair(q.popleft(), c)
        self.drain()

    @staticmethod
    def _free(doc: IndexedDoc, idx: int, matched: dict) -> bool:
        return not any(i in matched for i in doc.subtree(idx))

    def moves(self) -> None:
        B, V = self.B, self.V
        avail: dict[int, list[int]] = defaultdict(list)
        for nb in B.nodes:
            if nb.children and nb.idx not in self.b2v:
                avail[nb.hid].append(nb.idx)
        if not avail:
            return
        for nv in V.nodes:
            if nv.idx in self.v2b or not nv.children:
                continue
            cands = avail.get(nv.hid)
            if not cands or not self._free(V, nv.idx, self.v2b):
                continue
            best = None
            for b in cands:
                if not self._free(B, b, self.b2v):
                    continue
                nb = B.nodes[b]
                score = (nb.path == nv.path,
                         nb.parent is not None and self.b2v.get(nb.parent, -1) == nv.parent)
                if best is None or score > best[0]:
                    best = (score, b)
            if best is not None:
                cands.remove(best[1])
                self.pair(best[1], nv.idx)
                self.drain()
        self.sibling_moves()
        self.bottom_up()

    def bottom_up(self) -> None:
        B, V = self.B, self.V
        for nv in reversed(V.nodes):
            if nv.idx in self.v2b or not nv.children:
                continue
            counts: dict[int, int] = defaultdict(int)
            for cv in nv.children:
                cb = self.v2b.get(cv)
                if cb is not None and B.nodes[cb].parent is not None:
                    counts[B.nodes[cb].parent] += 1
            if not counts:
                continue
            pb, k = max(counts.items(), key=lambda t: (t[1], -t[0]))
            nb = B.nodes[pb]
            if pb not in self.b2v and nb.kind == nv.kind and \
                    k / max(len(nb.children), len(nv.children)) >= 0.5:
                self.pair(pb, nv.idx)
        self.drain()

    def sibling_moves(self) -> None:
        """Pair members of P and Q when a matched Element went from parent P to Q."""
        B, V = self.B, self.V
        evidence = set()
        for b, v in self.b2v.items():
            pb, pv = B.nodes[b].parent, V.nodes[v].parent
            if pb is not None and pv is not None and self.b2v.get(pb) != pv:
                evidence.add((pb, pv))
        for pb, pv in sorted(evidence):
            nb, nv = B.nodes[pb], V.nodes[pv]
            if nb.kind == OBJECT and nv.kind == OBJECT:
                for key, cb in nb.keymap.items():
                    cv = nv.keymap.get(key)
                    if (cv is not None and cb not in self.b2v and cv not in self.v2b
                            and B.nodes[cb].kind == V.nodes[cv].kind):
                        self.pair(cb, cv)
            elif nb.kind == ARRAY and nv.kind == ARRAY:
                avail: dict[int, deque] = defaultdict(deque)
                for cb in nb.children:
                    if cb not in self.b2v:
                        avail[B.nodes[cb].hid].append(cb)
                for cv in nv.children:
                    if cv not in self.v2b and avail.get(V.nodes[cv].hid):
                        self.pair(avail[V.nodes[cv].hid].popleft(), cv)
        self.drain()

    def similarity(self, b: int, v: int) -> float:
        nb, nv = self.B.nodes[b], self.V.nodes[v]
        if nb.hid == nv.hid:
            return 1.0
        if nb.kind != nv.kind:
            return 0.0
        if nb.kind == SCALAR:
            return 0.5 if _scalar_family(nb.value) == _scalar_family(nv.value) else 0.0
        if nb.kind == OBJECT:
            union = set(nb.keymap) | set(nv.keymap)
            if not union:
                return 1.0
            score = 0.0
            for k in set(nb.keymap) & set(nv.keymap):
                cb, cv = self.B.nodes[nb.keymap[k]], self.V.nodes[nv.keymap[k]]
                score += 1.0 if cb.hid == cv.hid else (0.5 if cb.kind == cv.kind else 0.25)
            return score / len(union)
        hb = [self.B.nodes[c].hid for c in nb.children]
        hv = [self.V.nodes[c].hid for c in nv.children]
        if not hb and not hv:
            return 1.0
        return 2 * len(lcs_pairs(hb, hv)) / (len(hb) + len(hv))

    def similar(self) -> None:
        for b, v in list(self.array_pairs):
            bc, vc = self.B.nodes[b].children, self.V.nodes[v].children
            vpos = {c: j for j, c in enumerate(vc)}
            matched = [(i, vpos[self.b2v[c]]) for i, c in enumerate(bc)
                       if c in self.b2v and self.b2v[c] in vpos]
            keep = lis_indices([j for _, j in matched])
            anchors = [(-1, -1)] + [matched[k] for k in keep] + [(len(bc), len(vc))]
            for (i0, j0), (i1, j1) in zip(anchors, anchors[1:]):
                gb = [bc[i] for i in range(i0 + 1, i1) if bc[i] not in self.b2v]
                gv = [vc[j] for j in range(j0 + 1, j1) if vc[j] not in self.v2b]
                ptr = 0
                for cb in gb:
                    best = None
                    for k in range(ptr, len(gv)):
                        s = self.similarity(cb, gv[k])
                        if s >= self.threshold and (best is None or s > best[0]):
                            best = (s, k)
                    if best is not None:
                        self.pair(cb, gv[best[1]])
                        ptr = best[1] + 1
        self.drain()
        # leftover containers, ignoring order (reordered *and* modified items)
        for b, v in list(self.array_pairs):
            gb = [c for c in self.B.nodes[b].children if c not in self.b2v and self.B.nodes[c].kind != SCALAR]
            gv = [c for c in self.V.nodes[v].children if c not in self.v2b and self.V.nodes[c].kind != SCALAR]
            if not gb or not gv:
                continue
            scored = sorted(((self.similarity(cb, cv), -i, -k, cb, cv)
                             for i, cb in enumerate(gb) for k, cv in enumerate(gv)), reverse=True)
            for score, _, _, cb, cv in scored:
                if score < self.threshold:
                    break
                if cb not in self.b2v and cv not in self.v2b:
                    self.pair(cb, cv)
        self.drain()


# ---------------------------------------------------------------------------
# encoding

def _encode(B: IndexedDoc, V: IndexedDoc, b2v: dict, v2b: dict) -> list[dict]:
    ops: list[dict] = []
    for nb in B.nodes:
        if nb.idx not in b2v and (nb.parent is None or nb.parent in b2v):
            ops.append({"op": "remove", "path": nb.ref})

    # Items that stay in the same array but change relative order.
    reordered: set[int] = set()
    for b, v in b2v.items():
        if B.nodes[b].kind != ARRAY:
            continue
        natives = [c for c in V.nodes[v].children if c in v2b and B.nodes[v2b[c]].parent == b]
        keep = set(lis_indices([B.nodes[v2b[c]].key for c in natives]))
        reordered.update(v2b[c] for i, c in enumerate(natives) if i not in keep)

    has_matched = [False] * len(V.nodes)
    for nv in reversed(V.nodes):
        has_matched[nv.idx] = nv.idx in v2b or any(has_matched[c] for c in nv.children)

    new_ids: dict[int, str] = {}

    def ref(v: int) -> str:
        return B.nodes[v2b[v]].ref if v in v2b else new_ids[v]

    def location(v: int) -> dict:
        nv = V.nodes[v]
        if nv.parent is None:
            return {"parent": None}
        p = V.nodes[nv.parent]
        if p.kind == OBJECT:
            return {"parent": ref(p.idx), "key": nv.key}
        prev = p.children[nv.key - 1] if nv.key else None
        return {"parent": ref(p.idx), "after": None if prev is None else ref(prev)}

    def moved(nb, nv) -> bool:
        if nv.parent is None or nb.parent is None:
            return (nv.parent is None) != (nb.parent is None)
        if v2b.get(nv.parent) != nb.parent:
            return True
        if V.nodes[nv.parent].kind == OBJECT:
            return nb.key != nv.key
        return nb.idx in reordered

    stack = [0]
    while stack:
        v = stack.pop()
        nv = V.nodes[v]
        if v in v2b:
            nb = B.nodes[v2b[v]]
            if moved(nb, nv):
                ops.append({"op": "move", "path": nb.ref, **location(v)})
            if nv.kind == SCALAR and canonical(nb.value) != canonical(nv.value):
                ops.append({"op": "set", "path": nb.ref, "value": nv.value})
            stack.extend(reversed(nv.children))
        else:
            new_ids[v] = f"+{len(new_ids) + 1}"
            if has_matched[v]:
                skeleton = {} if nv.kind == OBJECT else []
                ops.append({"op": "add", "id": new_ids[v], **location(v), "value": skeleton})
                stack.extend(reversed(nv.children))
            else:
                ops.append({"op": "add", "id": new_ids[v], **location(v), "value": copy.deepcopy(nv.value)})
    return ops


def compute_diff(base: Union[IndexedDoc, Any], version: Union[IndexedDoc, Any],
                 similarity_threshold: float = 0.5) -> Diff:
    """Encode the differences of ``version`` from ``base``."""
    if not isinstance(base, IndexedDoc):
        base = IndexedDoc(base)
    if isinstance(version, IndexedDoc):
        if version.interner is not base.interner:
            version = IndexedDoc(version.doc, base.interner)
    else:
        version = IndexedDoc(version, base.interner)
    matcher = _Matcher(base, version, similarity_threshold)
    matcher.run()
    return Diff(_encode(base, version, matcher.b2v, matcher.v2b))


# ---------------------------------------------------------------------------
# replaying a diff

@dataclass(frozen=True)
class Loc:
    """Where an Element lives: root (parent None), an object key, or an array slot."""

    parent: Optional[str]
    key: Optional[str] = None
    after: Optional[str] = None
    in_array: bool = False


class VersionView:
    """The version described by ``ops``, seen as base Elements plus new Elements.

    ``ns`` prefixes new-element ids so that views of several versions can be
    used side by side (``"+3"`` becomes e.g. ``"v1+3"``).
    """

    def __init__(self, base: IndexedDoc, ops: Iterable[dict], ns: str = ""):
        self.base = base
        self.ns = ns
        self.removed: set[str] = set()
        self.sets: dict[str, Any] = {}
        self.moves: dict[str, tuple[dict, Loc]] = {}
        self.adds: dict[str, tuple[dict, Loc]] = {}
        self.placed: dict[Optional[str], list[str]] = defaultdict(list)
        self.seq: dict[str, int] = {}
        for n, op in enumerate(ops):
            kind = op.get("op") if isinstance(op, dict) else None
            if kind == "remove":
                self._base_node(op.get("path"))
                self.removed.add(op["path"])
            elif kind == "set":
                if self._base_node(op.get("path")).kind != SCALAR:
                    raise DiffError(f"set on non-scalar {op['path']!r}")
                if "value" not in op or element_kind(op["value"]) != SCALAR:
                    raise DiffError(f"set on {op['path']!r} needs a scalar value")
                self.sets[op["path"]] = op["value"]
            elif kind in ("move", "add"):
                if kind == "move":
                    r = op.get("path")
                    self._base_node(r)
                    if r in self.moves:
                        raise DiffError(f"element {r!r} moved twice")
                else:
                    raw = op.get("id")
                    if not isinstance(raw, str) or not raw.startswith("+") or "value" not in op:
                        raise DiffError(f"malformed add operation {op!r}")
                    r = self._t(raw)
                    if r in self.adds:
                        raise DiffError(f"duplicate id {raw!r}")
                loc = self._loc_from_op(op)
                (self.moves if kind == "move" else self.adds)[r] = (op, loc)
                self.placed[loc.parent].append(r)
                self.seq[r] = n
            else:
                raise DiffError(f"unknown operation {op!r}")
        self.root = self._find_root()

    # -- construction helpers ---------------------------------------------
    def _t(self, ref: Optional[str]) -> Optional[str]:
        if ref is None:
            return None
        if not isinstance(ref, str):
            raise DiffError(f"invalid reference {ref!r}")
        return self.ns + ref if ref.startswith("+") else ref

    def _base_node(self, ref):
        node = self.base.node(ref) if isinstance(ref, str) else None
        if node is None:
            raise DiffError(f"no element {ref!r} in the base document")
        return node

    def _loc_from_op(self, op: dict) -> Loc:
        parent = self._t(op.get("parent"))
        if parent is None:
            return Loc(None)
        if parent not in self.adds and self.base.node(parent) is None:
            raise DiffError(f"unknown parent {op.get('parent')!r}")
        pkind = self.kind(parent)
        if "key" in op:
            if pkind != OBJECT or not isinstance(op["key"], str):
                raise DiffError(f"key location inside non-object {op.get('parent')!r}")
            return Loc(parent, key=op["key"])
        if "after" in op:
            if pkind != ARRAY:
                raise DiffError(f"array location inside non-array {op.get('parent')!r}")
            return Loc(parent, after=self._t(op["after"]), in_array=True)
        raise DiffError(f"operation {op!r} has no key/after")

    def _find_root(self) -> str:
        if "" not in self.removed and "" not in self.moves:
            if self.placed.get(None):
                raise DiffError("root added although the base root is kept")
            return ""
        roots = self.placed.get(None, [])
        if len(roots) != 1:
            raise DiffError("diff must define exactly one new root")
        return roots[0]

    # -- queries ------------------------------------------------------------
    def kind(self, ref: str) -> str:
        if ref in self.adds:
            return element_kind(self.adds[ref][0]["value"])
        return self._base_node(ref).kind

    def is_literal(self, ref: str) -> bool:
        """New Element whose whole value is carried by its add operation."""
        if ref not in self.adds:
            return False
        value = self.adds[ref][0]["value"]
        return not (isinstance(value, (dict, list)) and not value and self.placed.get(ref))

    def loc(self, ref: str) -> Loc:
        if ref in self.moves:
            return self.moves[ref][1]
        if ref in self.adds:
            return self.adds[ref][1]
        node = self._base_node(ref)
        if node.parent is None:
            return Loc(None)
        parent = self.base.nodes[node.parent]
        if parent.kind == OBJECT:
            return Loc(parent.ref, key=node.key)
        return Loc(parent.ref, in_array=True)

    def present(self, ref: str) -> bool:
        while True:
            if ref in self.adds or ref in self.moves:
                return True
            if ref in self.removed:
                return False
            node = self.base.node(ref)
            if node is None:
                return False
            if node.parent is None:
                return ref == self.root
            ref = self.base.nodes[node.parent].ref

    def _stay(self, ref: str) -> list[str]:
        node = self.base.node(ref)
        if node is None:
            return []
        out = []
        for c in node.children:
            cref = self.base.nodes[c].ref
            if cref not in self.moves and cref not in self.removed:
                out.append(cref)
        return out

    def object_items(self, ref: str) -> list[tuple[str, str]]:
        items = [(self.base.node(c).key, c) for c in self._stay(ref)]
        items += [(self.loc(r).key, r) for r in self.placed.get(ref, [])]
        return items

    def array_items(self, ref: str) -> list[str]:
        seq = self._stay(ref)
        for r in self.placed.get(ref, []):
            after = self.loc(r).after
            try:
                pos = 0 if after is None else seq.index(after) + 1
            except ValueError:
                raise DiffError(f"anchor {after!r} of {r!r} is not in array {ref!r}") from None
            seq.insert(pos, r)
        return seq

    def value(self, ref: str) -> Any:
        """The JSON value of an Element (base or new) in this version."""
        if self.is_literal(ref):
            return copy.deepcopy(self.adds[ref][0]["value"])
        kind = self.kind(ref)
        if kind == SCALAR:
            return copy.deepcopy(self.sets[ref]) if ref in self.sets else self.base.node(ref).value
        if kind == OBJECT:
            out = {}
            for key, child in self.object_items(ref):
                if key in out:
                    raise DiffError(f"two elements claim key {key!r} of {ref!r}")
                out[key] = self.value(child)
            return out
        return [self.value(c) for c in self.array_items(ref)]

    def document(self) -> Any:
        return self.value(self.root)


def apply_diff(base: Union[IndexedDoc, Any], diff: Union[Diff, dict, list]) -> Any:
    """Rebuild the version Document from ``base`` and its encoded ``diff``."""
    if not isinstance(base, IndexedDoc):
        base = IndexedDoc(base)
    if isinstance(diff, dict):
        diff = Diff.from_json(diff)
    ops = diff.ops if isinstance(diff, Diff) else diff
    return VersionView(base, ops).document()
