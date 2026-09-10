"""Tests for pg-sql-check/scripts/pg-sql-check.sh (add-pg-sql-check Task 3+4,
design.md "退出码契约" / "数据流图" / "失败模式表" / "可观测性", tasks.md
group 3-4, specs/sql-check/spec.md REQ-SC-1..SC-5).

Task 3 (S3) verified the offline, SQLSTATE -> exit-code fan-out end to end.
Task 4 (S4, this revision) adds coverage for: the stdout diagnostic summary,
position translation back to the user's original SQL coordinates, the JSON
artifact under build/pg-sql-check/, and the contract snapshot's
null-vs-[]-vs-missing distinction for result_types. All of it is verified
offline by mocking ONLY shared/ro-session.sh (via the RO_SESSION_SH_OVERRIDE
env var pg-sql-check.sh reads, the same pattern tests/test_pg_query_ro_shell.py
Part 2 uses for its sibling skill) — the JSON writer's own filename-derivation
guard call goes to the REAL shared/ro_guard.py (a pure, DB-free stdlib
script), the same thing pg-query-ro.sh's own tests rely on.

Real psql's verbose-mode SQLSTATE/LINE/caret/HINT line shapes were confirmed
empirically against a real local PostgreSQL 18.6 instance before writing the
parsers (see impl-reports); they are not re-derived here. The caret's column
is measured against the FULL printed "LINE n: <stmt>" text (prefix included),
and pg_prepared_statements.parameter_types/result_types render in PG's
standard braced array-of-regtype text form (e.g. "{integer,text}", "{}") —
both confirmed against real PostgreSQL, not assumed.

Does NOT test: ro_guard.py's own judgment (covered by test_ro_guard.py),
ro-session.sh's own --prepare injection-script shape (covered by
test_ro_session_shell.py), or candidate-name fuzzy matching (S5, not yet
implemented — the "candidates" field is always null in this revision).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PG_SQL_CHECK_SH = REPO_ROOT / "pg-sql-check" / "scripts" / "pg-sql-check.sh"

# Controllable mock shared/ro-session.sh. pg-sql-check.sh calls it exactly
# twice per invocation that gets past the .dbmeta/ gate: once WITHOUT
# --prepare (the "SHOW server_version_num" version gate) and once WITH
# --prepare (the real validation). This mock branches on the presence of
# --prepare in argv and answers each call independently via env vars.
MOCK_RO_SESSION = r"""#!/bin/bash
if [[ -n "${MOCK_RS_MARKER:-}" ]]; then
    echo called >> "${MOCK_RS_MARKER}"
fi

IS_PREPARE=0
for arg in "$@"; do
    if [[ "${arg}" == "--prepare" ]]; then
        IS_PREPARE=1
    fi
done

if [[ "${IS_PREPARE}" == "1" ]]; then
    if [[ -n "${MOCK_PREPARE_STDOUT:-}" ]]; then
        printf '%s' "${MOCK_PREPARE_STDOUT}"
    fi
    if [[ -n "${MOCK_PREPARE_STDERR:-}" ]]; then
        printf '%s' "${MOCK_PREPARE_STDERR}" >&2
    fi
    exit "${MOCK_PREPARE_RC:-0}"
else
    if [[ -n "${MOCK_VERSION_STDOUT:-}" ]]; then
        printf '%s' "${MOCK_VERSION_STDOUT}"
    else
        printf '%s\n' "${MOCK_VERSION_NUM:-180006}"
    fi
    if [[ -n "${MOCK_VERSION_STDERR:-}" ]]; then
        printf '%s' "${MOCK_VERSION_STDERR}" >&2
    fi
    exit "${MOCK_VERSION_RC:-0}"
fi
"""


@pytest.fixture()
def sc_project(tmp_path: Path):
    """A minimal consuming-project layout: `.dbmeta/` (existence gate only —
    pg-sql-check.sh does not read .dbllm.env itself, it delegates all
    connection handling to ro-session.sh) + a mock shared/ro-session.sh
    wired via RO_SESSION_SH_OVERRIDE. Returns (root_dir, env)."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dbmeta").mkdir()

    mock_rs = tmp_path / "mock-ro-session.sh"
    mock_rs.write_text(MOCK_RO_SESSION)
    mock_rs.chmod(mock_rs.stat().st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env["ROOT_DIR"] = str(root)
    env["RO_SESSION_SH_OVERRIDE"] = str(mock_rs)
    env.pop("USE_TEST_ENV", None)

    return root, env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PG_SQL_CHECK_SH), *args], env=env, capture_output=True, text=True, timeout=30
    )


# --- argument parsing / .dbmeta gate ----------------------------------------


def test_missing_sql_rejected(sc_project):
    _root, env = sc_project
    result = _run(env)
    assert result.returncode == 1, result.stderr


def test_unknown_argument_rejected(sc_project):
    _root, env = sc_project
    result = _run(env, "--sql", "SELECT 1", "--bogus")
    assert result.returncode == 1, result.stderr


def test_missing_dbmeta_rejects_before_any_ro_session_call(sc_project, tmp_path):
    root, env = sc_project
    (root / ".dbmeta").rmdir()
    marker = tmp_path / "rs-called.marker"
    env["MOCK_RS_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT foo FROM bar")

    assert result.returncode == 2, result.stderr
    assert "/pg-dict" in result.stderr
    assert not marker.exists()


# --- PG version gate (REQ-SC-2) ---------------------------------------------


def test_pg_version_below_16_fails_loud(sc_project):
    _root, env = sc_project
    env["MOCK_VERSION_NUM"] = "150004"  # PG 15.4

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 1, result.stderr
    assert "16" in result.stderr


def test_pg_version_16_passes_the_gate(sc_project):
    _root, env = sc_project
    env["MOCK_VERSION_NUM"] = "160003"
    env["MOCK_PREPARE_STDOUT"] = "|\n"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 0, result.stderr


def test_unparseable_version_output_fails_loud(sc_project):
    _root, env = sc_project
    env["MOCK_VERSION_STDOUT"] = "not-a-number\n"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 1, result.stderr


def test_version_check_needs_human_forwarded(sc_project, tmp_path):
    root, env = sc_project
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    needs_human.parent.mkdir(parents=True)
    needs_human.write_text("# 只读会话不可用\n")
    env["MOCK_VERSION_RC"] = "2"
    env["MOCK_VERSION_STDOUT"] = str(needs_human) + "\n"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    assert result.stdout.strip() == str(needs_human)


# --- delegate rc passthrough (2 / 3) ----------------------------------------


def test_prepare_needs_human_forwarded(sc_project, tmp_path):
    root, env = sc_project
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    needs_human.parent.mkdir(parents=True)
    needs_human.write_text("# 只读会话不可用\n")
    env["MOCK_PREPARE_RC"] = "2"
    env["MOCK_PREPARE_STDOUT"] = str(needs_human) + "\n"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    assert result.stdout.strip() == str(needs_human)


def test_guard_rejection_passthrough(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "3"
    env["MOCK_PREPARE_STDERR"] = "[FAIL] problem: 只读会话拒绝执行该语句（reason=multi-statement）\n"

    result = _run(env, "--sql", "SELECT 1; DROP TABLE t")

    assert result.returncode == 3, result.stderr
    assert "multi-statement" in result.stderr


def test_unenumerated_prepare_exit_code_fails_loud(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "137"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 1, result.stderr
    assert "137" in result.stderr


# --- success / transaction-pool topology gate -------------------------------


def test_success_prints_snapshot(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_STDOUT"] = "int4|text\n"

    result = _run(env, "--sql", "SELECT id, name FROM users")

    assert result.returncode == 0, result.stderr
    assert "int4|text" in result.stdout


def test_success_null_snapshot_row_is_not_empty_output(sc_project):
    # A write statement's read-back row (both columns NULL) prints as a
    # bare "|" line under psql -A -t — non-empty bytes, must NOT be
    # confused with the true empty-output transaction-pool symptom below.
    _root, env = sc_project
    env["MOCK_PREPARE_STDOUT"] = "|\n"

    result = _run(env, "--sql", "UPDATE users SET name = 'x'")

    assert result.returncode == 0, result.stderr


def test_empty_snapshot_on_rc0_is_transaction_pool_fail_loud(sc_project):
    # design.md failure-mode table: rc=0 with an empty snapshot MUST NOT be
    # reported as "passed" — it is PgBouncer transaction-pool statement loss.
    _root, env = sc_project
    env["MOCK_PREPARE_STDOUT"] = ""
    env["MOCK_PREPARE_RC"] = "0"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 1, result.stderr
    assert "transaction" in result.stderr.lower() or "PgBouncer" in result.stderr


# --- SQLSTATE extraction + exit-code fan-out (REQ-SC-3 / REQ-SC-4) ----------

VERBOSE_42703 = (
    'psql:/tmp/x.sql:14: ERROR:  42703: column "foo" does not exist\n'
    "LINE 1: PREPARE _p AS SELECT foo FROM bar\n"
    "                             ^\n"
)


def test_42703_undefined_column_maps_to_exit_4(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = VERBOSE_42703

    result = _run(env, "--sql", "SELECT foo FROM bar")

    assert result.returncode == 4, result.stderr
    assert "42703" in result.stderr


def test_42p01_undefined_table_maps_to_exit_4(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42P01: relation "nope" does not exist\n'

    result = _run(env, "--sql", "SELECT * FROM nope")

    assert result.returncode == 4, result.stderr
    assert "42P01" in result.stderr


def test_42883_undefined_operator_maps_to_exit_4(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = "ERROR:  42883: operator does not exist: text + integer\n"

    result = _run(env, "--sql", "SELECT 'a' + 1")

    assert result.returncode == 4, result.stderr


def test_42601_ddl_and_typo_share_unified_message_and_exit_4(sc_project):
    root, env = sc_project

    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42601: syntax error at or near "CREATE"\n'
    ddl_result = _run(env, "--sql", "CREATE INDEX idx ON t(c)")

    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42601: syntax error at or near "SELET"\n'
    typo_result = _run(env, "--sql", "SELET id FROM t")

    assert ddl_result.returncode == 4, ddl_result.stderr
    assert typo_result.returncode == 4, typo_result.stderr
    # Same unified diagnostic text (modulo the verbatim PG message appended),
    # and MUST NOT name a specific (unimplemented) skill.
    assert "pg-migrate-verify" not in ddl_result.stderr
    assert "roadmap" in ddl_result.stderr.lower() or "skills-roadmap" in ddl_result.stderr
    assert "roadmap" in typo_result.stderr.lower() or "skills-roadmap" in typo_result.stderr


def test_42p18_indeterminate_datatype_maps_to_exit_1_not_4(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = "ERROR:  42P18: could not determine data type of parameter $1\n"

    result = _run(env, "--sql", "SELECT $1 IS NULL")

    assert result.returncode == 1, result.stderr
    assert "cast" in result.stderr.lower() or "42P18" in result.stderr


def test_non_class_42_sqlstate_is_hard_error(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = "ERROR:  53300: too many connections\n"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 1, result.stderr


def test_unextractable_sqlstate_fails_loud_not_guessed(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = "some garbled psql output with no recognizable SQLSTATE\n"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 1, result.stderr


# --- 42501 secondary triage (Global Constraints) ----------------------------


def test_42501_schema_in_dbmeta_scope_points_to_readonly_setup(sc_project):
    root, env = sc_project
    (root / ".dbmeta" / "sales").mkdir()
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42501: permission denied for schema sales\n'

    result = _run(env, "--sql", "SELECT * FROM sales.orders")

    assert result.returncode == 2, result.stderr
    assert "/pg-readonly-setup" in result.stderr


def test_42501_schema_outside_dbmeta_scope_points_to_schemas_config(sc_project):
    root, env = sc_project
    # sales/ is NOT under .dbmeta/ — not onboarded.
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42501: permission denied for schema sales\n'

    result = _run(env, "--sql", "SELECT * FROM sales.orders")

    assert result.returncode == 2, result.stderr
    assert "SCHEMAS" in result.stderr
    assert "/pg-readonly-setup" not in result.stderr or "SCHEMAS" in result.stderr


def test_42501_table_level_denial_without_schema_name_fails_loud(sc_project):
    # Table-level denial messages don't include the schema name (design.md
    # C10) — the schema membership cannot be determined, MUST NOT guess.
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42501: permission denied for table orders\n'

    result = _run(env, "--sql", "SELECT * FROM orders")

    assert result.returncode == 1, result.stderr


def test_42501_lc_messages_permission_gap_reported_distinctly(sc_project):
    # Off-ticket finding: lc_messages' GUC context is `superuser` — a
    # genuinely non-superuser read-only role gets 42501 on the tool's OWN
    # `SET LOCAL lc_messages='C'` statement, not on the user's SQL. MUST be
    # distinguished from a real schema/table permission gap.
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = (
        'ERROR:  42501: permission denied to set parameter "lc_messages"\n'
    )

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    assert "lc_messages" in result.stderr
    assert "GRANT SET ON PARAMETER" in result.stderr


# --- diagnostic double-output: JSON artifact + stdout summary (Task 4, ------
# REQ-SC-4 / REQ-SC-5) ---------------------------------------------------


def _json_files(root: Path) -> list[Path]:
    return sorted((root / "build" / "pg-sql-check").glob("*.json"))


def _only_json(root: Path) -> dict:
    files = _json_files(root)
    assert len(files) == 1, f"expected exactly one JSON artifact, got {files}"
    return json.loads(files[0].read_text())


def test_json_artifact_written_on_validation_failure(sc_project):
    root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = VERBOSE_42703

    result = _run(env, "--sql", "SELECT foo FROM bar")

    assert result.returncode == 4, result.stderr
    doc = _only_json(root)
    assert doc["sqlstate"] == "42703"
    assert "does not exist" in doc["message"]
    assert doc["hint"] is None
    assert doc["candidates"] is None
    assert doc["parameter_types"] is None
    assert doc["result_types"] is None


def test_position_converted_to_user_original_coordinates(sc_project):
    # VERBOSE_42703's injected statement is `PREPARE _p AS SELECT foo FROM
    # bar` — PostgreSQL's caret lands on column 30 of the printed
    # "LINE 1: PREPARE _p AS SELECT foo FROM bar" line (verified against a
    # real PG 18.6 sample). Subtracting "LINE 1: " (8 chars) gives statement
    # column 22; subtracting "PREPARE _p AS " (14 chars) gives user column
    # 8 — the 'f' of "foo" in the user's original "SELECT foo FROM bar".
    root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = VERBOSE_42703

    result = _run(env, "--sql", "SELECT foo FROM bar")

    assert result.returncode == 4, result.stderr
    assert "第 1 行第 8 列" in result.stdout
    assert "已换算回用户原文坐标" in result.stdout
    doc = _only_json(root)
    assert doc["position"] == {"line": 1, "column": 8}


def test_position_absent_when_pg_gives_no_line_info(sc_project):
    # 42501 (and most non-syntax/semantic SQLSTATEs) never carry a LINE/caret
    # — position MUST be null, not a guessed value.
    root, env = sc_project
    (root / ".dbmeta" / "sales").mkdir()
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42501: permission denied for schema sales\n'

    result = _run(env, "--sql", "SELECT * FROM sales.orders")

    assert result.returncode == 2, result.stderr
    doc = _only_json(root)
    assert doc["position"] is None


def test_hint_extracted_into_stdout_and_json(sc_project):
    root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = (
        'psql:/tmp/x.sql:14: ERROR:  42703: column "emial" does not exist\n'
        "LINE 1: PREPARE _p AS SELECT emial FROM users\n"
        "                             ^\n"
        'HINT:  Perhaps you meant to reference the column "users.email".\n'
    )

    result = _run(env, "--sql", "SELECT emial FROM users")

    assert result.returncode == 4, result.stderr
    assert 'Perhaps you meant to reference the column "users.email".' in result.stdout
    doc = _only_json(root)
    assert doc["hint"] == 'Perhaps you meant to reference the column "users.email".'


def test_dual_output_sqlstate_consistency(sc_project):
    # REQ-SC-4: stdout and the JSON artifact MUST report the same SQLSTATE
    # from the same judgment.
    root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42883: operator does not exist: text + integer\n'

    result = _run(env, "--sql", "SELECT 'a' + 1")

    assert result.returncode == 4, result.stderr
    doc = _only_json(root)
    assert doc["sqlstate"] == "42883"
    assert "42883" in result.stdout


def test_42501_secondary_triage_also_writes_json(sc_project):
    root, env = sc_project
    (root / ".dbmeta" / "sales").mkdir()
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42501: permission denied for schema sales\n'

    result = _run(env, "--sql", "SELECT * FROM sales.orders")

    assert result.returncode == 2, result.stderr
    doc = _only_json(root)
    assert doc["sqlstate"] == "42501"


# --- contract snapshot: null vs [] vs missing (Task 4, REQ-SC-5) -----------


def test_success_read_statement_snapshot_json(sc_project):
    # pg_prepared_statements.parameter_types/result_types render in PG's
    # standard braced array-of-regtype text form (confirmed against a real
    # PostgreSQL instance) — NOT the bare "int4|text" shorthand some earlier
    # (Task 3) tests use as an arbitrary "there is a non-empty snapshot"
    # placeholder; those tests only assert on the raw pass-through, never on
    # parsed JSON content, so the two fixture shapes coexist without conflict.
    root, env = sc_project
    env["MOCK_PREPARE_STDOUT"] = "{integer,text}|{text}\n"

    result = _run(env, "--sql", "SELECT id, email FROM app.users WHERE id > $1 AND email = $2")

    assert result.returncode == 0, result.stderr
    doc = _only_json(root)
    assert doc["parameter_types"] == ["integer", "text"]
    assert doc["result_types"] == ["text"]
    assert doc["sqlstate"] is None


def test_success_write_statement_result_types_is_null_not_empty_list(sc_project):
    # design.md / REQ-SC-5: a DML statement's result_types is SQL NULL per
    # PostgreSQL's own docs — the JSON field MUST be `null`, MUST NOT be `[]`,
    # MUST NOT be a missing key. psql -A -t (unaligned) renders NULL as an
    # empty string, which is what a real UPDATE/INSERT/DELETE read-back
    # produces for that column.
    root, env = sc_project
    env["MOCK_PREPARE_STDOUT"] = "{text,integer}|\n"

    result = _run(env, "--sql", "UPDATE app.users SET email = $1 WHERE id = $2")

    assert result.returncode == 0, result.stderr
    doc = _only_json(root)
    assert doc["parameter_types"] == ["text", "integer"]
    assert "result_types" in doc
    assert doc["result_types"] is None


def test_success_zero_param_statement_parameter_types_is_empty_list(sc_project):
    root, env = sc_project
    env["MOCK_PREPARE_STDOUT"] = "{}|{integer}\n"

    result = _run(env, "--sql", "SELECT id FROM app.users")

    assert result.returncode == 0, result.stderr
    doc = _only_json(root)
    assert doc["parameter_types"] == []
    assert doc["result_types"] == ["integer"]


# --- concurrent same-second, same-SQL calls do not collide (Task 4, -------
# REQ-SC-4 "同一秒同一条 SQL 并发也不互相覆盖") -------------------------------


def test_json_artifact_name_collision_retried_with_suffix(sc_project, tmp_path):
    # Simulate two same-second, same-SQL invocations by pre-claiming the
    # exact filename the second (real) invocation will compute, via a fixed
    # `date` shim on PATH ahead of the real one — deterministic instead of a
    # timing-dependent race.
    root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = VERBOSE_42703

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_date = fake_bin / "date"
    fake_date.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-u" ]]; then echo 20260909T000000Z; else /bin/date "$@"; fi\n'
    )
    fake_date.chmod(fake_date.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    # Compute the real sha8 the script itself would derive, to pre-claim the
    # exact colliding filename ahead of time.
    guard = subprocess.run(
        ["python3", str(REPO_ROOT / "shared" / "ro_guard.py"), "guard", "--prepare", "--no-limit"],
        input="SELECT foo FROM bar",
        capture_output=True,
        text=True,
    )
    sha8 = json.loads(guard.stdout)["sha8"]
    result_dir = root / "build" / "pg-sql-check"
    result_dir.mkdir(parents=True)
    (result_dir / f"20260909T000000Z-{sha8}.json").write_text('{"pre-existing": true}\n')

    result = _run(env, "--sql", "SELECT foo FROM bar")

    assert result.returncode == 4, result.stderr
    suffixed = result_dir / f"20260909T000000Z-{sha8}-2.json"
    assert suffixed.exists()
    doc = json.loads(suffixed.read_text())
    assert doc["sqlstate"] == "42703"
    # The pre-existing file MUST be left untouched (never overwritten).
    pre_existing = json.loads((result_dir / f"20260909T000000Z-{sha8}.json").read_text())
    assert pre_existing == {"pre-existing": True}

# --- candidate-name completion wiring (Task 5, REQ-SC-6) ---


def test_42703_identifier_extraction_failure_degrades_without_changing_exit_code(sc_project):
    # spec.md「非英文 locale 下降级而非静默」: message shape PG never
    # actually produces for 42703 (no quoted column name at all) simulates
    # extraction failure — MUST degrade to "no candidate name", MUST NOT
    # change the exit code or silently fail.
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = "ERROR:  42703: 未知の列\n"

    result = _run(env, "--sql", "SELECT foo FROM bar")

    assert result.returncode == 4, result.stderr
    assert "无法从错误消息中提取列名" in result.stderr



def test_42703_with_pg_hint_uses_hint_not_local_candidates(sc_project):
    # REQ-SC-6: PostgreSQL already gave a HINT -> present it as-is, MUST NOT
    # invoke local candidate matching at all.
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = (
        'ERROR:  42703: column "emial" does not exist\n'
        'HINT:  Perhaps you meant to reference the column "users.email".\n'
    )

    result = _run(env, "--sql", "SELECT emial FROM users")

    assert result.returncode == 4, result.stderr
    assert 'Perhaps you meant to reference the column "users.email".' in result.stderr
    assert "候选" not in result.stderr



def test_42703_without_hint_reports_no_candidates_explicitly(sc_project):
    # sc_project's .dbmeta/ is empty -> no column can reach the similarity
    # threshold -> MUST explicitly say "no candidates", never a silent empty
    # list and never a dump of every known identifier (spec.md Scenario).
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = VERBOSE_42703

    result = _run(env, "--sql", "SELECT foo FROM bar")

    assert result.returncode == 4, result.stderr
    assert "无候选" in result.stderr



def test_42703_without_hint_surfaces_real_candidate(sc_project_with_dict):
    _root, env = sc_project_with_dict
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = (
        'ERROR:  42703: column "emial" does not exist\n'
        "LINE 1: PREPARE _p AS SELECT emial FROM users\n"
        "                             ^\n"
    )

    result = _run(env, "--sql", "SELECT emial FROM users")

    assert result.returncode == 4, result.stderr
    assert "dbllm_e2e.users.email" in result.stderr
    assert ".dbmeta/" in result.stderr
    assert "/pg-dict" in result.stderr



def test_42p01_identifier_extraction_failure_degrades_without_changing_exit_code(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = "ERROR:  42P01: 未知の関係\n"

    result = _run(env, "--sql", "SELECT * FROM nope")

    assert result.returncode == 4, result.stderr
    assert "无法从错误消息中提取标识符" in result.stderr


@pytest.fixture()
def sc_project_with_dict(sc_project):
    """sc_project plus a small populated .dbmeta/ tree, for end-to-end
    wiring tests that need an actual candidate to be found (as opposed to
    the "no candidates" tests above, which rely on the fixture's .dbmeta/
    being empty)."""
    root, env = sc_project
    tables_dir = root / ".dbmeta" / "dbllm_e2e" / "tables"
    tables_dir.mkdir(parents=True)
    (tables_dir / "users.sql").write_text(
        "-- pg-dict:table:users:start\n"
        "CREATE TABLE dbllm_e2e.users (\n"
        "  id bigint NOT NULL,\n"
        "  email text NOT NULL\n"
        ");\n"
        "-- pg-dict:table:users:end\n",
        encoding="utf-8",
    )
    return root, env



def test_42p01_without_candidates_reports_no_candidates_explicitly(sc_project):
    _root, env = sc_project
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42P01: relation "nope" does not exist\n'

    result = _run(env, "--sql", "SELECT * FROM nope")

    assert result.returncode == 4, result.stderr
    assert "无候选" in result.stderr



def test_42p01_without_hint_surfaces_real_candidate(sc_project_with_dict):
    _root, env = sc_project_with_dict
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = 'ERROR:  42P01: relation "userz" does not exist\n'

    result = _run(env, "--sql", "SELECT id FROM userz")

    assert result.returncode == 4, result.stderr
    assert "dbllm_e2e.users" in result.stderr



def test_dual_output_consistency_stdout_vs_repeated_invocation(sc_project_with_dict):
    # spec.md「双出一致性」: the same SQLSTATE/candidate-set must come back
    # identically across independent invocations of the same underlying
    # error against the same .dbmeta/ tree (task 4's JSON artifact write is
    # out of this ticket's scope, but the determinism it depends on is
    # candidate_match.py's job and is verified here at the process level).
    _root, env = sc_project_with_dict
    env["MOCK_PREPARE_RC"] = "1"
    env["MOCK_PREPARE_STDERR"] = (
        'ERROR:  42703: column "emial" does not exist\n'
    )

    first = _run(env, "--sql", "SELECT emial FROM users")
    second = _run(env, "--sql", "SELECT emial FROM users")

    assert first.returncode == second.returncode == 4
    assert "dbllm_e2e.users.email" in first.stderr
    assert "dbllm_e2e.users.email" in second.stderr

