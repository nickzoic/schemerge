"""The ``schemerge`` command line tool."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from schemerge import METASCHEMA, Validator, check_schema
from schemerge.cli import EXIT_CONFLICTS, EXIT_ERROR, EXIT_OK, PERMISSIVE_SCHEMA, main

ROOT = Path(__file__).resolve().parent.parent

BASE = {"todo": [{"title": "write", "prio": 1}], "done": [], "tags": ["a", "b"]}
V1 = {"todo": [], "done": [{"title": "write", "prio": 1}], "tags": ["a", "X"]}       # move + change tag
V2 = {"todo": [{"title": "write", "prio": 5}], "done": [], "tags": ["a", "Y"]}       # edit + change tag
MERGED = {"todo": [], "done": [{"title": "write", "prio": 5}], "tags": ["a", "X", "Y"]}


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def run(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = main([str(a) for a in argv], stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def files(tmp_path):
    return tmp_path, write(tmp_path / "base.json", BASE), write(tmp_path / "v1.json", V1), \
        write(tmp_path / "v2.json", V2)


# -- the default (permissive) schema ---------------------------------------------

def test_permissive_default_schema_is_in_the_metaschema_and_accepts_everything():
    check_schema(PERMISSIVE_SCHEMA)
    assert Validator(METASCHEMA).is_valid(PERMISSIVE_SCHEMA)
    for doc in (None, 0, "x", [], {}, [{"a": [1, {"b": None}]}], {"if": {"then": 1}}):
        assert Validator(PERMISSIVE_SCHEMA).is_valid(doc)


def test_merge_to_stdout_without_a_schema(files):
    _, base, v1, v2 = files
    code, out, err = run([base, v1, v2])
    assert code == EXIT_OK
    assert json.loads(out) == MERGED
    assert out.endswith("\n")
    assert "value conflict at /tags/1" in err


def test_any_documents_merge_without_a_schema(tmp_path):
    base = write(tmp_path / "b.json", [1, "two", None])
    v1 = write(tmp_path / "1.json", [1, "two", None, {"x": True}])
    v2 = write(tmp_path / "2.json", [0, 1, "two", None])
    code, out, _ = run([base, v1, v2])
    assert code == EXIT_OK and json.loads(out) == [0, 1, "two", None, {"x": True}]


def test_scalar_documents(tmp_path):
    base, v1 = write(tmp_path / "b.json", "old"), write(tmp_path / "v.json", "new")
    assert json.loads(run([base, v1])[1]) == "new"


# -- output ----------------------------------------------------------------------

def test_output_file(files):
    tmp, base, v1, v2 = files
    out_path = tmp / "merged.json"
    code, out, _ = run([base, v1, v2, "-o", out_path])
    assert code == EXIT_OK and out == ""
    text = out_path.read_text(encoding="utf-8")
    assert json.loads(text) == MERGED and text.endswith("\n")
    assert '\n  "todo"' in text                                  # indent 2 by default


def test_output_can_overwrite_an_input(files):
    _, base, v1, v2 = files
    assert run([base, v1, v2, "--output", base])[0] == EXIT_OK
    assert json.loads(Path(base).read_text()) == MERGED


def test_existing_output_permissions_are_kept(files):
    tmp, base, v1, v2 = files
    out_path = tmp / "merged.json"
    out_path.write_text("{}")
    os.chmod(out_path, 0o640)
    run([base, v1, v2, "-o", out_path])
    assert (os.stat(out_path).st_mode & 0o777) == 0o640


def test_no_temporary_files_are_left_behind(files):
    tmp, base, v1, v2 = files
    run([base, v1, v2, "-o", tmp / "merged.json"])
    assert not [p for p in os.listdir(tmp) if p.startswith(".schemerge-")]


def test_unwritable_output_is_an_error(files):
    tmp, base, v1, v2 = files
    code, _, err = run([base, v1, v2, "-o", tmp / "missing-dir" / "out.json"])
    assert code == EXIT_ERROR and "cannot write" in err


@pytest.mark.parametrize("options,check", [
    (["--compact"], lambda t: t.count("\n") == 1 and ", " not in t),
    (["--indent", "4"], lambda t: '\n    "todo"' in t),
    (["--indent", "0"], lambda t: '\n"todo"' in t),
    (["--sort-keys", "--compact"], lambda t: t.index('"done"') < t.index('"tags"') < t.index('"todo"')),
])
def test_output_layout(files, options, check):
    _, base, v1, v2 = files
    code, out, _ = run([base, v1, v2, *options])
    assert code == EXIT_OK and check(out) and json.loads(out) == MERGED


def test_non_ascii_text_is_written_as_utf8(tmp_path):
    base = write(tmp_path / "b.json", {"name": "café"})
    v1 = write(tmp_path / "v.json", {"name": "café ☕"})
    out_path = tmp_path / "o.json"
    run([base, v1, "-o", out_path])
    assert "café ☕" in out_path.read_text(encoding="utf-8")


def test_compact_and_indent_are_exclusive(files):
    _, base, v1, _ = files
    with pytest.raises(SystemExit) as exc:
        run([base, v1, "--compact", "--indent", "2"])
    assert exc.value.code == EXIT_ERROR


# -- schema ----------------------------------------------------------------------

def test_schema_constraints_are_enforced(files):
    tmp, base, v1, v2 = files
    schema = write(tmp / "s.json", {"type": "object", "properties": {"tags": {"type": "array", "maxItems": 2}}})
    code, out, err = run(["-s", schema, base, v1, v2])
    assert code == EXIT_OK
    assert json.loads(out)["tags"] == ["a", "Y"]
    assert "value from" in err and "v2.json used" in err


def test_bubbling_is_reported(tmp_path):
    schema = write(tmp_path / "s.json", {"type": "array", "uniqueItems": True})
    base = write(tmp_path / "b.json", ["a", "b"])
    v1 = write(tmp_path / "1.json", ["a", "b", "x"])
    v2 = write(tmp_path / "2.json", ["x", "a", "b"])
    code, out, err = run(["--schema", schema, base, v1, v2])
    assert code == EXIT_OK and json.loads(out) == ["x", "a", "b"]
    assert "bubbled up to (root)" in err and "2.json" in err and "duplicate item" in err


def test_schema_outside_the_subset_is_rejected(files):
    tmp, base, v1, _ = files
    schema = write(tmp / "s.json", {"if": {"type": "object"}})
    code, out, err = run(["-s", schema, base, v1])
    assert code == EXIT_ERROR and out == ""
    assert "s.json" in err and "unexpected property 'if'" in err


def test_document_not_valid_in_the_schema(files):
    tmp, base, v1, v2 = files
    schema = write(tmp / "s.json", {"type": "object", "properties": {"done": {"maxItems": 0}}})
    out_path = tmp / "o.json"
    code, _, err = run(["-s", schema, base, v1, v2, "-o", out_path])
    assert code == EXIT_ERROR
    assert "v1.json: not valid in the schema" in err and "/done" in err
    assert not out_path.exists()


def test_invalid_base(files):
    tmp, base, v1, _ = files
    schema = write(tmp / "s.json", {"required": ["missing"]})
    code, _, err = run(["-s", schema, base, v1])
    assert code == EXIT_ERROR and "base.json: not valid" in err and "'missing'" in err


# -- policies --------------------------------------------------------------------

def test_value_conflict_latest(files):
    _, base, v1, v2 = files
    _, out, _ = run([base, v1, v2, "--on-value-conflict", "latest"])
    assert json.loads(out)["tags"] == ["a", "Y"]


def test_delete_conflict_policies(tmp_path):
    base = write(tmp_path / "b.json", {"user": {"name": "a"}, "n": 1})
    v1 = write(tmp_path / "1.json", {"n": 1})
    v2 = write(tmp_path / "2.json", {"user": {"name": "b"}, "n": 1})
    assert json.loads(run([base, v1, v2])[1]) == {"user": {"name": "b"}, "n": 1}
    assert json.loads(run([base, v1, v2, "--on-delete-conflict", "delete"])[1]) == {"n": 1}


def test_similarity_threshold(tmp_path):
    base = write(tmp_path / "b.json", [{"a": 1, "b": 1, "c": 1}])
    v1 = write(tmp_path / "1.json", [{"a": 1, "b": 2, "c": 2}])
    v2 = write(tmp_path / "2.json", [{"a": 9, "b": 1, "c": 1}])
    assert json.loads(run([base, v1, v2])[1]) == [{"a": 9, "b": 2, "c": 2}]
    assert len(json.loads(run([base, v1, v2, "--similarity-threshold", "0.95"])[1])) == 2


@pytest.mark.parametrize("value", ["1.5", "-0.1", "half"])
def test_bad_similarity_threshold(files, value):
    _, base, v1, _ = files
    with pytest.raises(SystemExit) as exc:
        run([base, v1, "--similarity-threshold", value])
    assert exc.value.code == EXIT_ERROR


# -- strictness, quietness and the report ----------------------------------------

def test_strict_exit_status(files):
    tmp, base, v1, v2 = files
    out_path = tmp / "o.json"
    code, _, _ = run([base, v1, v2, "--strict", "-o", out_path])
    assert code == EXIT_CONFLICTS
    assert json.loads(out_path.read_text()) == MERGED          # still written
    assert run([base, v1, "--strict"])[0] == EXIT_OK           # nothing to resolve


def test_quiet(files):
    _, base, v1, v2 = files
    code, out, err = run([base, v1, v2, "-q"])
    assert code == EXIT_OK and err == "" and json.loads(out) == MERGED


def test_no_summary_without_conflicts(files):
    _, base, v1, _ = files
    assert run([base, v1])[2] == ""


def test_report_file(files):
    tmp, base, v1, v2 = files
    report_path = tmp / "report.json"
    code, out, _ = run([base, v1, v2, "--report", report_path])
    report = json.loads(report_path.read_text())
    assert code == EXIT_OK and json.loads(out) == MERGED
    assert set(report) == {"base", "versions", "schema", "conflicts", "bubbled"}
    assert report["versions"] == [v1, v2] and report["schema"] is None
    [conflict] = report["conflicts"]
    assert conflict["kind"] == "value" and conflict["versions"] == [0, 1]


# -- input handling --------------------------------------------------------------

def test_base_from_stdin(files):
    _, _, v1, v2 = files
    code, out, _ = run(["-", v1, v2], stdin=json.dumps(BASE))
    assert code == EXIT_OK and json.loads(out) == MERGED


def test_version_from_stdin_is_named_in_messages(files):
    _, base, v1, _ = files
    code, _, err = run([base, v1, "-"], stdin=json.dumps(V2))
    assert code == EXIT_OK and "<stdin>" in err


def test_stdin_only_once(files):
    _, base, _, _ = files
    code, _, err = run([base, "-", "-"], stdin="{}")
    assert code == EXIT_ERROR and "at most one" in err


@pytest.mark.parametrize("content,message", [
    ('{"a": }', "invalid JSON: Expecting value"),
    ('{"a": NaN}', "NaN is not valid JSON"),
    ('[Infinity]', "Infinity is not valid JSON"),
    ('{"a": 1, "a": 2}', "duplicate object key 'a'"),
    ('', "invalid JSON"),
    ('{} {}', "Extra data"),
])
def test_bad_json(tmp_path, files, content, message):
    _, _, v1, _ = files
    bad = tmp_path / "bad.json"
    bad.write_text(content)
    code, out, err = run([bad, v1])
    assert code == EXIT_ERROR and out == ""
    assert "bad.json" in err and message in err


def test_missing_file(files):
    tmp, _, v1, _ = files
    code, _, err = run([tmp / "nope.json", v1])
    assert code == EXIT_ERROR and "nope.json" in err and "No such file" in err


def test_non_utf8_file(tmp_path, files):
    _, _, v1, _ = files
    bad = tmp_path / "latin1.json"
    bad.write_bytes('{"a": "café"}'.encode("latin-1"))
    code, _, err = run([bad, v1])
    assert code == EXIT_ERROR and "not UTF-8" in err


def test_utf8_byte_order_mark_is_accepted(tmp_path, files):
    _, _, v1, _ = files
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(BASE).encode())
    assert run([bom, v1])[0] == EXIT_OK


def test_a_version_is_required(files):
    _, base, _, _ = files
    with pytest.raises(SystemExit) as exc:
        run([base])
    assert exc.value.code == EXIT_ERROR


# -- running the program -----------------------------------------------------------

def _python(*args, **kw):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, **kw)


def test_python_dash_m(files):
    tmp, base, v1, v2 = files
    proc = _python("-m", "schemerge", base, v1, v2, "-o", str(tmp / "o.json"))
    assert proc.returncode == EXIT_OK, proc.stderr
    assert json.loads((tmp / "o.json").read_text()) == MERGED


def test_python_dash_m_exit_status_and_stdin(files):
    _, _, v1, v2 = files
    proc = _python("-m", "schemerge", "-", v1, v2, "--strict", input=json.dumps(BASE))
    assert proc.returncode == EXIT_CONFLICTS and json.loads(proc.stdout) == MERGED


def test_version_and_help():
    proc = _python("-m", "schemerge", "--version")
    assert proc.returncode == 0 and proc.stdout.startswith("schemerge ")
    proc = _python("-m", "schemerge", "--help")
    assert proc.returncode == 0 and "--schema" in proc.stdout and "permissive" in proc.stdout


def test_console_script_is_declared():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'schemerge = "schemerge.cli:main"' in text
