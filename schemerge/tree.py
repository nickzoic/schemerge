"""A flat, indexed view of a Document used by the diff and merge engines."""
from __future__ import annotations

from typing import Any, Optional

from .jsonutil import ARRAY, OBJECT, SCALAR, canonical, element_kind, escape_token


class Node:
    """One Element of an indexed Document."""

    __slots__ = ("idx", "ref", "path", "kind", "value", "parent", "key",
                 "children", "keymap", "hid", "size")

    def __init__(self, idx, ref, path, kind, value, parent, key):
        self.idx: int = idx
        self.ref: str = ref              # JSON pointer of the Element in its Document
        self.path: tuple = path
        self.kind: str = kind            # object / array / scalar
        self.value: Any = value
        self.parent: Optional[int] = parent
        self.key: Any = key              # key (str) or index (int) in the parent
        self.children: list[int] = []
        self.keymap: dict[str, int] = {}
        self.hid: int = -1               # structural hash id: equal iff JSON-equal
        self.size: int = 1               # number of Elements in the subtree

    def __repr__(self) -> str:
        return f"Node({self.ref or '(root)'}, {self.kind})"


class IndexedDoc:
    """Nodes in pre-order (a subtree is the contiguous range ``idx .. idx+size``).

    ``interner`` maps structural keys to small integers; sharing one interner
    between two IndexedDocs makes their ``hid`` values comparable.
    """

    def __init__(self, document: Any, interner: Optional[dict] = None):
        self.doc = document
        self.interner = interner if interner is not None else {}
        self.nodes: list[Node] = []
        self.by_ref: dict[str, int] = {}
        self._index()

    def node(self, ref: str) -> Optional[Node]:
        idx = self.by_ref.get(ref)
        return None if idx is None else self.nodes[idx]

    def parent_ref(self, ref: str) -> Optional[str]:
        node = self.node(ref)
        if node is None or node.parent is None:
            return None
        return self.nodes[node.parent].ref

    def ancestors_and_self(self, ref: str):
        idx = self.by_ref.get(ref)
        while idx is not None:
            yield self.nodes[idx].ref
            idx = self.nodes[idx].parent

    def subtree(self, idx: int) -> range:
        return range(idx, idx + self.nodes[idx].size)

    def _index(self) -> None:
        stack = [(self.doc, None, None, (), "")]
        while stack:
            value, parent, key, path, ref = stack.pop()
            idx = len(self.nodes)
            node = Node(idx, ref, path, element_kind(value), value, parent, key)
            self.nodes.append(node)
            self.by_ref[ref] = idx
            if parent is not None:
                p = self.nodes[parent]
                p.children.append(idx)
                if p.kind == OBJECT:
                    p.keymap[key] = idx
            if node.kind == OBJECT:
                items = list(value.items())
                for k, v in reversed(items):
                    stack.append((v, idx, k, path + (k,), ref + "/" + escape_token(k)))
            elif node.kind == ARRAY:
                for i in range(len(value) - 1, -1, -1):
                    stack.append((value[i], idx, i, path + (i,), ref + "/" + str(i)))
        # Bottom-up: sizes and structural hash ids.
        intern = self.interner
        for node in reversed(self.nodes):
            if node.kind == SCALAR:
                key = ("s", canonical(node.value))
            elif node.kind == ARRAY:
                key = ("a", tuple(self.nodes[c].hid for c in node.children))
            else:
                key = ("o", tuple(sorted((self.nodes[c].key, self.nodes[c].hid) for c in node.children)))
            node.hid = intern.setdefault(key, len(intern))
            node.size = 1 + sum(self.nodes[c].size for c in node.children)
