"""Tests for pg-query-ro/scripts/pg-query-ro.sh (Task 5, tasks.md 5.1-5.3,
design.md DD-5, specs/query-ro REQ-QR-1..QR-3, and skill-completeness-gaps
design.md Part 2).

Two halves live in this file:

1. Migrated from tests/test_ro_session_shell.py (10 `test_query_ro_*`
   functions + `_run_query_ro` + `project`): those drive pg-query-ro.sh
   through the SAME real shared/ro-session.sh + mocked-psql chain the
   ro-session tests use, since pg-query-ro.sh delegates guard/session/psql
   work to ro-session.sh.

2. New skill-completeness-gaps Part 2 coverage (8 dimensions): these mock
   ONLY shared/ro-session.sh, via the `RO_SESSION_SH_OVERRIDE` env var
   pg-query-ro.sh already reads (pg-query-ro.sh:29) — ro_guard.py (filename
   derivation) and shared/config.sh stay real. This isolates "did
   pg-query-ro.sh's OWN skill-layer logic (arg parsing / .dbmeta gate /
   filename dedup / .meta write / preview / gitignore warn / exit-code
   handling) do the right thing" from ro-session.sh's own internal
   guard/psql behavior, which tests/test_ro_session_shell.py already covers
   (41 cases) and this file deliberately does not re-test (design.md Part 2
   "不测什么").
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PG_QUERY_RO_SH = REPO_ROOT / "pg-query-ro" / "scripts" / "pg-query-ro.sh"

sys.path.insert(0, str(REPO_ROOT / "shared"))
import ro_guard  # noqa: E402


# =============================================================================
# Part 1: migrated verbatim from tests/test_ro_session_shell.py:632-910
# (mocked psql, real ro-session.sh + real ro_guard.py + real config.sh)
#
# MOCK_PSQL and the `project` fixture are shared with
# tests/test_ro_session_shell.py and live in tests/conftest.py.
# =============================================================================


def _run_query_ro(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PG_QUERY_RO_SH), *args], env=env, capture_output=True, text=True, timeout=30
    )


def test_query_ro_missing_dbmeta_rejects_before_any_connection(project, tmp_path):
    root, env, _write_config = project
    shutil.rmtree(root / ".dbmeta")
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run_query_ro(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    assert "/pg-dict" in result.stderr
    assert not marker.exists()


def test_query_ro_multi_statement_rejected(project, tmp_path):
    root, env, _write_config = project
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run_query_ro(env, "--sql", "SELECT 1; SELECT 2")

    assert result.returncode == 3, result.stderr
    assert not marker.exists()


def test_query_ro_normal_query_writes_csv_and_meta(project, tmp_path):
    root, env, _write_config = project
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text("id\n1\n2\n3\n4\n5\n")
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)

    result = _run_query_ro(env, "--sql", "SELECT id FROM auth.users ORDER BY id")

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    result_path = Path(lines[0])
    assert result_path.exists()
    assert result_path.parent == root / "build" / "pg-query-ro"
    assert result_path.suffix == ".csv"
    assert result_path.read_text() == "id\n1\n2\n3\n4\n5\n"

    meta_path = Path(str(result_path) + ".meta")
    assert meta_path.exists()
    meta = dict(line.split(": ", 1) for line in meta_path.read_text().splitlines() if ": " in line)
    assert meta["rows"] == "5"
    assert meta["truncated"] == "false"
    assert meta["format"] == "csv"
    assert "sql" in meta and "elapsed_ms" in meta and "limit" in meta


def test_query_ro_preview_exactly_twenty_lines(project, tmp_path):
    root, env, _write_config = project
    rows = "\n".join(str(i) for i in range(1, 20))  # 19 data rows
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text(f"id\n{rows}\n")
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)

    result = _run_query_ro(env, "--sql", "SELECT id FROM t", "--no-limit")

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    # lines[0] is the file path; the preview block is everything after it.
    preview = lines[1:]
    assert len(preview) == 20, preview
    assert preview[0] == "id"
    assert "truncated" not in preview[-1]


def test_query_ro_truncation_reported_in_meta_and_preview(project, tmp_path):
    root, env, _write_config = project
    rows = "\n".join(str(i) for i in range(1, 251))  # 250 data rows
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text(f"id\n{rows}\n")
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)

    result = _run_query_ro(env, "--sql", "SELECT id FROM t", "--limit", "200")

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    result_path = Path(lines[0])
    assert len(result_path.read_text().splitlines()) == 201  # header + 200
    meta = dict(
        line.split(": ", 1) for line in Path(str(result_path) + ".meta").read_text().splitlines() if ": " in line
    )
    assert meta["truncated"] == "true"
    assert meta["rows"] == "200"
    assert lines[-1] == "… (truncated, see .meta)"


def test_query_ro_duplicate_same_second_appends_suffix(project, tmp_path):
    root, env, _write_config = project
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text("id\n1\n")
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)

    fixed_date_bin = tmp_path / "datebin"
    fixed_date_bin.mkdir()
    date_stub = fixed_date_bin / "date"
    date_stub.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-u" && "$2" == "+%Y%m%dT%H%M%SZ" ]]; then\n'
        '  echo "20250101T000000Z"\n'
        "else\n"
        '  command -p date "$@"\n'
        "fi\n"
    )
    date_stub.chmod(date_stub.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = f"{fixed_date_bin}:{env['PATH']}"

    result1 = _run_query_ro(env, "--sql", "SELECT id FROM t")
    result2 = _run_query_ro(env, "--sql", "SELECT id FROM t")

    assert result1.returncode == 0, result1.stderr
    assert result2.returncode == 0, result2.stderr
    path1 = Path(result1.stdout.splitlines()[0])
    path2 = Path(result2.stdout.splitlines()[0])
    assert path1 != path2
    assert path1.exists() and path2.exists()
    assert path2.stem == path1.stem + "-2"
    assert path1.read_text() == path2.read_text() == "id\n1\n"


def test_query_ro_explain_writes_txt_extension(project, tmp_path):
    root, env, _write_config = project
    text_file = tmp_path / "explain-output.txt"
    text_file.write_text("Seq Scan on widgets  (cost=0.00..1.01 rows=1 width=4)\n")
    env["DBLLM_TEST_TEXT_FILE"] = str(text_file)

    result = _run_query_ro(env, "--sql", "EXPLAIN SELECT 1")

    assert result.returncode == 0, result.stderr
    result_path = Path(result.stdout.splitlines()[0])
    assert result_path.suffix == ".txt"
    assert result_path.read_text() == "Seq Scan on widgets  (cost=0.00..1.01 rows=1 width=4)\n"
    meta = dict(
        line.split(": ", 1) for line in Path(str(result_path) + ".meta").read_text().splitlines() if ": " in line
    )
    assert meta["format"] == "text"


def test_query_ro_multiline_sql_meta_stays_six_lines(project, tmp_path):
    # Regression: multi-line formatted SQL (CTEs, multi-column SELECTs) is a
    # common, legitimate query shape and passes shared/ro_guard.py unchanged
    # (it only rejects top-level semicolons and backslash meta-commands, not
    # embedded newlines). Before the fix, pg-query-ro.sh wrote SQL_TEXT
    # verbatim into `sql: <value>` in .meta, so an embedded newline split the
    # value across lines and shifted rows/truncated/limit/elapsed_ms/format
    # out of their fixed positions, breaking the 6-line key:value contract
    # SKILL.md promises to consumers that parse .meta by line.
    import re

    root, env, _write_config = project
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text("id\n1\n")
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)

    multiline_sql = "SELECT id,\n  name\nFROM t\nWHERE id = 1"
    result = _run_query_ro(env, "--sql", multiline_sql)

    assert result.returncode == 0, result.stderr
    result_path = Path(result.stdout.splitlines()[0])
    meta_path = Path(str(result_path) + ".meta")
    meta_lines = meta_path.read_text().splitlines()

    assert len(meta_lines) == 6, meta_lines
    key_pattern = re.compile(r"^(sql|rows|truncated|limit|elapsed_ms|format): ")
    for line in meta_lines:
        assert key_pattern.match(line), meta_lines

    sql_value = meta_lines[0][len("sql: ") :]
    # Reverse the escaping the same way pg-query-ro.sh applies it (backslash
    # first, then \n / \r) to recover the original multi-line SQL verbatim.
    restored = re.sub(
        r"\\(\\|n|r)",
        lambda m: {"\\": "\\", "n": "\n", "r": "\r"}[m.group(1)],
        sql_value,
    )
    assert restored == multiline_sql


# =============================================================================
# code-review fixes: cr-fix-query-path (A1 TOCTOU, C1 unenumerated exit code,
# BE1 needs-human redaction gap)
# =============================================================================


def test_query_ro_concurrent_same_second_no_overwrite(project, tmp_path):
    """A1 regression: pg-query-ro.sh derives its result filename from
    <UTC-second>-<sha8-of-SQL>, and used to pick the name with a plain
    `[[ -e ]]` existence check followed later by a separate write — a TOCTOU
    window in which two processes racing on the same second with the same SQL
    (sha8 is a pure function of the SQL text, see shared/ro_guard.py) could
    both pass the existence check for the same candidate name before either
    had written it, then clobber/interleave each other's output while both
    still exit 0. This repo's own workflow fans out parallel LLM sub-agents
    running diagnostic queries, so this is not a low-probability edge case.

    Freezes `date` so both processes compute the identical <UTC-second>
    component, and uses the identical SQL text so ro_guard's sha8 is
    identical too — the only thing that can still keep them apart is the
    (now atomic, set -C-based) name-claim loop in pg-query-ro.sh.
    """
    root, env, _write_config = project
    csv_file_a = tmp_path / "rows-a.csv"
    csv_file_a.write_text("id\nAAA\n")
    csv_file_b = tmp_path / "rows-b.csv"
    csv_file_b.write_text("id\nBBB\n")

    fixed_date_bin = tmp_path / "datebin-concurrent"
    fixed_date_bin.mkdir()
    date_stub = fixed_date_bin / "date"
    date_stub.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-u" && "$2" == "+%Y%m%dT%H%M%SZ" ]]; then\n'
        '  echo "20250101T000000Z"\n'
        "else\n"
        '  command -p date "$@"\n'
        "fi\n"
    )
    date_stub.chmod(date_stub.stat().st_mode | stat.S_IEXEC)

    env_a = dict(env)
    env_a["PATH"] = f"{fixed_date_bin}:{env['PATH']}"
    env_a["DBLLM_TEST_CSV_FILE"] = str(csv_file_a)
    env_b = dict(env)
    env_b["PATH"] = f"{fixed_date_bin}:{env['PATH']}"
    env_b["DBLLM_TEST_CSV_FILE"] = str(csv_file_b)

    results: dict[str, subprocess.CompletedProcess] = {}

    def run_a() -> None:
        results["a"] = _run_query_ro(env_a, "--sql", "SELECT id FROM t")

    def run_b() -> None:
        results["b"] = _run_query_ro(env_b, "--sql", "SELECT id FROM t")

    ta = threading.Thread(target=run_a)
    tb = threading.Thread(target=run_b)
    ta.start()
    tb.start()
    ta.join(timeout=30)
    tb.join(timeout=30)

    result_a, result_b = results["a"], results["b"]
    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr
    path_a = Path(result_a.stdout.splitlines()[0])
    path_b = Path(result_b.stdout.splitlines()[0])
    assert path_a != path_b, "并发同秒同 SQL 必须落到不同文件，不能互相踩踏"
    assert path_a.exists() and path_b.exists()
    # Each writer's file must contain only its own content — an interleaved
    # or silently-overwritten file would show the wrong marker (or both).
    content_a = path_a.read_text()
    content_b = path_b.read_text()
    assert content_a == "id\nAAA\n", content_a
    assert content_b == "id\nBBB\n", content_b


def test_query_ro_unenumerated_ro_session_exit_code_fails_loud(project, tmp_path):
    """C1 regression: pg-query-ro.sh only explicitly handled ro-session.sh
    exit codes 1/2/3, so any other code (e.g. 143 — killed by SIGTERM, which
    can leave a partially-written COPY result on disk) used to fall through
    to the success branch: .meta written, path echoed, exit 0, with
    RO_ROWS/RO_TRUNCATED silently defaulting to 0/false because the
    RO_ROWS=/RO_TRUNCATED= trailer ro-session.sh prints on a clean finish
    never got printed. An unenumerated non-zero code MUST be treated as
    failure, not success."""
    stub = tmp_path / "ro-session-killed.sh"
    stub.write_text("#!/bin/bash\nexit 143\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    root, env, _write_config = project
    env["RO_SESSION_SH_OVERRIDE"] = str(stub)

    result = _run_query_ro(env, "--sql", "SELECT 1")

    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "143" in result.stderr
    assert result.stdout == "", "未枚举退出码不得走成功路径打印结果路径"
    result_dir = root / "build" / "pg-query-ro"
    assert not list(result_dir.glob("*.meta")), "未枚举退出码不得写 .meta（那意味着标了 success）"


# =============================================================================
# Part 2: skill-completeness-gaps Part 2 — 8 new dimensions, mocking ONLY
# shared/ro-session.sh via RO_SESSION_SH_OVERRIDE. ro_guard.py and
# shared/config.sh stay real (design.md Part 2 mock 策略).
# =============================================================================

# Controllable mock ro-session.sh. Reads the `--out FILE` arg it was invoked
# with and writes MOCK_RS_CONTENT there (mirroring what real ro-session.sh
# does — write the query result to --out); prints the RO_ROWS=/RO_TRUNCATED=
# stderr trailer pg-query-ro.sh parses on success (`grep -o 'RO_ROWS=...'`,
# pg-query-ro.sh:280-281) unless suppressed; exits MOCK_RS_EXIT (default 0).
MOCK_RO_SESSION = r"""#!/bin/bash
if [[ -n "${MOCK_RS_MARKER:-}" ]]; then
    echo called >> "${MOCK_RS_MARKER}"
fi

OUT=""
prev=""
for arg in "$@"; do
    if [[ "${prev}" == "--out" ]]; then
        OUT="${arg}"
    fi
    prev="${arg}"
done

if [[ -n "${OUT}" ]]; then
    printf '%s' "${MOCK_RS_CONTENT-id
1
}" > "${OUT}"
fi

if [[ "${MOCK_RS_NO_TRAILER:-0}" != "1" ]]; then
    echo "RO_ROWS=${MOCK_RS_ROWS:-1}" >&2
    echo "RO_TRUNCATED=${MOCK_RS_TRUNCATED:-false}" >&2
fi
if [[ -n "${MOCK_RS_EXTRA_STDERR:-}" ]]; then
    echo "${MOCK_RS_EXTRA_STDERR}" >&2
fi

exit "${MOCK_RS_EXIT:-0}"
"""


@pytest.fixture()
def qro_project(tmp_path: Path):
    """A minimal consuming-project layout for pg-query-ro.sh's OWN skill-
    layer logic: `.dbmeta/.dbllm.env` with DB_* fields + `.dbmeta/`
    + a mock shared/ro-session.sh wired via RO_SESSION_SH_OVERRIDE. Returns
    (root_dir, env)."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dbmeta").mkdir()
    (root / ".dbmeta" / ".dbllm.env").write_text(
        "DB_HOST=127.0.0.1\n"
        "DB_PORT=6432\n"
        "DB_NAME=testdb\n"
        "DB_USER=llm_readonly\n"
        "DB_PASSWORD=testpass\n"
    )

    mock_rs = tmp_path / "mock-ro-session.sh"
    mock_rs.write_text(MOCK_RO_SESSION)
    mock_rs.chmod(mock_rs.stat().st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env["ROOT_DIR"] = str(root)
    env["RO_SESSION_SH_OVERRIDE"] = str(mock_rs)
    env.pop("USE_TEST_ENV", None)

    return root, env


# --- dimension 1: argument parsing ------------------------------------------


def test_query_ro_missing_sql_rejected(qro_project):
    _root, env = qro_project
    result = _run_query_ro(env)
    assert result.returncode == 1, result.stderr


def test_query_ro_limit_and_no_limit_mutually_exclusive(qro_project):
    _root, env = qro_project
    result = _run_query_ro(env, "--sql", "SELECT 1", "--limit", "10", "--no-limit")
    assert result.returncode == 1, result.stderr


def test_query_ro_invalid_format_rejected(qro_project):
    _root, env = qro_project
    result = _run_query_ro(env, "--sql", "SELECT 1", "--format", "yaml")
    assert result.returncode == 1, result.stderr


def test_query_ro_unknown_argument_rejected(qro_project):
    _root, env = qro_project
    result = _run_query_ro(env, "--sql", "SELECT 1", "--bogus-flag")
    assert result.returncode == 1, result.stderr


# --- dimension 3: .meta escaping (six-line contract already covered by the
# migrated test_query_ro_multiline_sql_meta_stays_six_lines above; this adds
# the backslash-before-newline escaping order the brief calls out separately)
# -----------------------------------------------------------------------------


def test_query_ro_meta_escapes_backslash_before_newline(qro_project):
    # pg-query-ro.sh:297-299 escapes backslash FIRST, then \n/\r — reversed
    # order would turn a literal backslash adjacent to the \n it introduces
    # into an ambiguous sequence when a consumer un-escapes it. Use a SQL
    # literal with both a real embedded newline and a real embedded
    # backslash to exercise the ordering, not just presence of a backslash.
    root, env = qro_project
    sql = "SELECT 'a\\b' AS x,\n  1 AS y"

    result = _run_query_ro(env, "--sql", sql)

    assert result.returncode == 0, result.stderr
    result_path = Path(result.stdout.splitlines()[0])
    meta_lines = Path(str(result_path) + ".meta").read_text().splitlines()
    assert len(meta_lines) == 6, meta_lines
    sql_value = meta_lines[0][len("sql: ") :]
    # Expected escaping applied in the SAME order the script applies it:
    # backslash -> \\ first, THEN embedded newline -> \n.
    expected = sql.replace("\\", "\\\\").replace("\n", "\\n")
    assert sql_value == expected, (sql_value, expected)


# --- dimension 5: filename dedup, constructed via a pre-existing file (not
# by racing two real invocations against the wall clock) -------------------


def test_query_ro_dedup_appends_suffix_when_target_already_exists(qro_project):
    root, env = qro_project

    # Freeze `date` so the base filename's <UTC-second> component is
    # predictable, then derive the sha8 component the SAME way
    # pg-query-ro.sh does (via the real shared/ro_guard.py guard judgment,
    # design.md Part 2 mock 策略: ro_guard.py stays real) so we can construct
    # the exact collision path ourselves instead of depending on two
    # invocations landing in the same real wall-clock second.
    fixed_date_dir = root.parent / "datebin-dedup"
    fixed_date_dir.mkdir()
    date_stub = fixed_date_dir / "date"
    date_stub.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-u" && "$2" == "+%Y%m%dT%H%M%SZ" ]]; then\n'
        '  echo "20250101T000000Z"\n'
        "else\n"
        '  command -p date "$@"\n'
        "fi\n"
    )
    date_stub.chmod(date_stub.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = f"{fixed_date_dir}:{env['PATH']}"

    sql = "SELECT 1"
    guarded = ro_guard.evaluate(sql, limit=200, no_limit=False)
    assert guarded["ok"], guarded
    sha8 = guarded["sha8"]

    result_dir = root / "build" / "pg-query-ro"
    result_dir.mkdir(parents=True, exist_ok=True)
    pre_existing = result_dir / f"20250101T000000Z-{sha8}.csv"
    pre_existing.write_text("id\npretend-existing\n")

    result = _run_query_ro(env, "--sql", sql)

    assert result.returncode == 0, result.stderr
    result_path = Path(result.stdout.splitlines()[0])
    assert result_path.name == f"20250101T000000Z-{sha8}-2.csv", result_path.name
    # The pre-existing file must be untouched — the whole point of dedup.
    assert pre_existing.read_text() == "id\npretend-existing\n"


# --- dimension 6: build/ gitignore three-state warn -------------------------


def test_query_ro_build_dir_not_ignored_warns(qro_project):
    root, env = qro_project
    # "未忽略" branch needs a real git repo — `git check-ignore` returns
    # rc=1 (not ignored, no error) only inside one; outside a repo it
    # returns rc>=2, which is the OTHER branch this dimension tests below.
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    result = _run_query_ro(env, "--sql", "SELECT 1")

    assert "未被 git ignore" in result.stderr, result.stderr


def test_query_ro_build_dir_git_unavailable_warns_degraded(qro_project):
    root, env = qro_project
    # No `git init` here — root stays a non-git directory, so
    # `git -C root check-ignore` exits >=2 ("not a git repository"),
    # exercising the degraded-warn branch (pg-query-ro.sh:121-123)
    # distinctly from the "not ignored" branch above.

    result = _run_query_ro(env, "--sql", "SELECT 1")

    assert "无法判定" in result.stderr, result.stderr
    assert "未被 git ignore" not in result.stderr, result.stderr


# --- dimension 8: non-standard ro-session.sh exit codes ---------------------


@pytest.mark.parametrize("exit_code", [5, 130])
def test_query_ro_nonstandard_exit_code_not_success(qro_project, exit_code):
    root, env = qro_project
    env["MOCK_RS_EXIT"] = str(exit_code)

    result = _run_query_ro(env, "--sql", "SELECT 1")

    assert result.returncode != 0, (exit_code, result.returncode, result.stdout, result.stderr)
    assert str(exit_code) in result.stderr, result.stderr
    result_dir = root / "build" / "pg-query-ro"
    assert not list(result_dir.glob("*.meta")), "未枚举退出码不得写 .meta（那意味着标了 success）"
