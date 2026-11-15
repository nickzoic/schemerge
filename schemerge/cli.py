"""Command line interface: ``schemerge BASE VERSION... [-s SCHEMA] [-o OUTPUT]``.

Reads the base Document and one or more modified versions from JSON files,
merges them, and writes the merged Document as JSON.  Without ``--schema``
the boolean schema ``true`` is used: it is valid in the metaschema and
accepts every Document, so no schema constraint ever applies.

Exit status: 0 on success; 1 with ``--strict`` when conflicts had to be
resolved or schema violations were bubbled up (the merged Document is still
written); 2 for usage errors and unusable input (unreadable or invalid JSON,
a schema outside the supported subset, a Document not valid in the schema).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from typing import Any, Optional, Sequence

from . import __version__
from .merger import SchemaMerger
from .validator import DocumentInvalidError, SchemaError

#: The most permissive schema the metaschema allows: every Document is valid.
PERMISSIVE_SCHEMA = True

EXIT_OK, EXIT_CONFLICTS, EXIT_ERROR = 0, 1, 2


class InputError(Exception):
    """A file could not be read or does not contain usable JSON."""


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _no_duplicate_keys(pairs: list) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate object key {key!r}")
        obj[key] = value
    return obj


def parse_json(text: str, source: str) -> Any:
    """Parse strict JSON: no NaN/Infinity and no duplicate object keys."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise InputError(f"{source}: invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})") from None
    except (ValueError, RecursionError) as exc:
        raise InputError(f"{source}: invalid JSON: {exc}") from None


def load_json(path: str, stdin=None) -> Any:
    """Load a JSON file; ``-`` reads standard input."""
    if path == "-":
        stream = stdin if stdin is not None else sys.stdin
        return parse_json(stream.read(), "<stdin>")
    try:
        with open(path, encoding="utf-8-sig") as handle:  # tolerate a UTF-8 byte-order mark
            text = handle.read()
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError as exc:
        raise InputError(f"{path}: not UTF-8 text ({exc.reason})") from None
    return parse_json(text, path)


def dump_json(document: Any, indent: Optional[int], sort_keys: bool) -> str:
    if indent is None:
        text = json.dumps(document, ensure_ascii=False, sort_keys=sort_keys, separators=(",", ":"))
    else:
        text = json.dumps(document, ensure_ascii=False, sort_keys=sort_keys, indent=indent)
    return text + "\n"


def write_text(path: str, text: str, stdout=None) -> None:
    """Write ``text`` to ``path`` atomically (``-`` writes standard output)."""
    if path == "-":
        (stdout if stdout is not None else sys.stdout).write(text)
        return
    directory = os.path.dirname(os.path.abspath(path))
    try:
        fd, tmp = tempfile.mkstemp(prefix=".schemerge-", suffix=".tmp", dir=directory)
    except OSError as exc:
        raise InputError(f"{path}: cannot write: {exc.strerror or exc}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if os.path.exists(path):
            try:
                os.chmod(tmp, os.stat(path).st_mode & 0o7777)
            except OSError:
                pass
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise InputError(f"{path}: cannot write: {exc.strerror or exc}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemerge",
        description="Merge modified versions of a JSON document into one document that is "
                    "valid in a JSON Schema.",
        epilog="Later versions take priority when versions genuinely conflict. "
               "Exit status: 0 success; 1 with --strict when conflicts were resolved or schema "
               "violations bubbled up; 2 for errors. Use '-' to read one input from standard input.",
    )
    parser.add_argument("base", metavar="BASE", help="the initial document (JSON file)")
    parser.add_argument("versions", metavar="VERSION", nargs="+",
                        help="modified versions of BASE (JSON files), lowest priority first")
    parser.add_argument("-s", "--schema", metavar="SCHEMA",
                        help="JSON Schema the documents must be valid in (default: the "
                             "permissive schema 'true', which accepts any document)")
    parser.add_argument("-o", "--output", metavar="OUTPUT", default="-",
                        help="write the merged document here (default: standard output)")
    parser.add_argument("--report", metavar="FILE",
                        help="write a JSON report of conflicts and bubbled-up violations")
    parser.add_argument("--on-delete-conflict", choices=("keep", "delete"), default="keep",
                        help="an element removed by one version but changed by another is kept "
                             "(default) or deleted")
    parser.add_argument("--on-value-conflict", choices=("keep", "latest"), default="keep",
                        help="a scalar array item changed differently by several versions keeps "
                             "every new value where the schema allows (default), or only the latest")
    parser.add_argument("--similarity-threshold", metavar="X", type=_fraction, default=0.5,
                        help="minimum similarity (0..1) for matching modified array items (default 0.5)")
    layout = parser.add_mutually_exclusive_group()
    # default None (not 2): argparse ignores an exclusive option whose value *is* its default
    layout.add_argument("--indent", metavar="N", type=_indent, default=None,
                        help="indentation of the output (default 2)")
    layout.add_argument("--compact", action="store_true", help="write the output on one line")
    parser.add_argument("--sort-keys", action="store_true", help="sort object keys in the output")
    parser.add_argument("--strict", action="store_true",
                        help="exit with status 1 if any conflict was resolved or violation bubbled up")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not summarise conflicts on stderr")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _fraction(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def _indent(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def _summary(result, names: list[str]) -> list[str]:
    """Human-readable lines, naming versions by their file names."""
    def name(text: str) -> str:
        return re.sub(r"\bversion (\d+)\b",
                      lambda m: names[int(m.group(1))] if int(m.group(1)) < len(names) else m.group(0),
                      text)

    lines = []
    if result.conflicts or result.bubbled:
        lines.append(f"merged {len(names)} version(s): {len(result.conflicts)} conflict(s) resolved, "
                     f"{len(result.bubbled)} schema violation(s) bubbled up")
    for c in result.conflicts:
        who = ", ".join(names[v] if v < len(names) else str(v) for v in c.versions) or "-"
        lines.append(f"  {c.kind} conflict at {c.location or '(root)'} [{who}]: {name(c.resolution)}")
    for b in result.bubbled:
        lines.append(f"  violation at {b.origin or '(root)'} bubbled up to {b.path or '(root)'}: "
                     f"replaced with the element from {name(b.resolution)}")
        lines.extend(f"      {issue}" for issue in b.issues)
    return lines


def main(argv: Optional[Sequence[str]] = None, stdin=None, stdout=None, stderr=None) -> int:
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    def fail(message: str) -> int:
        stderr.write(f"schemerge: error: {message}\n")
        return EXIT_ERROR

    inputs = [args.base] + list(args.versions) + ([args.schema] if args.schema else [])
    if inputs.count("-") > 1:
        return fail("standard input ('-') can be used for at most one input")

    try:
        schema = load_json(args.schema, stdin) if args.schema else PERMISSIVE_SCHEMA
        base = load_json(args.base, stdin)
        versions = [load_json(path, stdin) for path in args.versions]
    except InputError as exc:
        return fail(str(exc))

    try:
        merger = SchemaMerger(schema, on_delete_conflict=args.on_delete_conflict,
                              on_value_conflict=args.on_value_conflict,
                              similarity_threshold=args.similarity_threshold)
    except SchemaError as exc:
        return fail(f"{args.schema}: {exc}")
    except TypeError as exc:  # pragma: no cover - parse_json only yields JSON values
        return fail(str(exc))

    def invalid(path: str, exc: DocumentInvalidError) -> int:
        shown = exc.issues[:20]
        lines = "".join(f"\n  {issue}" for issue in shown)
        more = f"\n  ... and {len(exc.issues) - len(shown)} more" if len(exc.issues) > len(shown) else ""
        where = f"the schema {args.schema}" if args.schema else "the schema"
        return fail(f"{'<stdin>' if path == '-' else path}: not valid in {where}:{lines}{more}")

    try:
        merger.set_base(base)
    except DocumentInvalidError as exc:
        return invalid(args.base, exc)
    for path, document in zip(args.versions, versions):
        try:
            merger.add_version(document)
        except DocumentInvalidError as exc:
            return invalid(path, exc)

    result = merger.merge_report()
    indent = None if args.compact else (2 if args.indent is None else args.indent)
    try:
        write_text(args.output, dump_json(result.document, indent, args.sort_keys), stdout)
        if args.report:
            # version numbers in the report index into "versions"
            report = dict(result.to_json(), base=args.base, versions=list(args.versions),
                          schema=args.schema)
            report.pop("document")
            write_text(args.report, dump_json(report, 2, False), stdout)
    except InputError as exc:
        return fail(str(exc))

    if not args.quiet:
        names = ["<stdin>" if p == "-" else p for p in args.versions]
        for line in _summary(result, names):
            stderr.write(f"{line}\n" if line.startswith(" ") else f"schemerge: {line}\n")
    if args.strict and (result.conflicts or result.bubbled):
        return EXIT_CONFLICTS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
