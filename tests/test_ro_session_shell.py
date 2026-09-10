"""Integration tests for shared/ro-session.sh (design.md DD-3, tasks.md 2.3-2.4,
specs/ro-session/spec.md REQ-RS-2..RS-6).

No real Postgres needed: a controllable mock `psql` binary is put first on
PATH. It never connects to anything — its behavior (success / current_user
mismatch / connection refused / connection-limit / a plain SQL error) is
selected via the DBLLM_TEST_MODE env var, and it dumps its argv, the `-f`
script file content, and its inherited PG* credential env vars to files the
test can assert on afterward. This isolates "did the shell layer do the right
thing before/around psql" from "does psql/PG actually enforce READ ONLY /
statement_timeout / ACLs" — the latter needs a provisioned read-only role,
whose creation is privileged SQL this repo MUST NOT execute (CLAUDE.md
「特权 SQL 边界」), so it has no automated anchor here by design.
"""

from __future__ import annotations

import csv
import io
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RO_SESSION_SH = REPO_ROOT / "shared" / "ro-session.sh"


def _run(env: dict[str, str], *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(RO_SESSION_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        input=input_text,
    )


# --- REQ-RS-4: guard runs before any credential read / connection ----------


def test_multi_statement_rejected_before_any_connection(project, tmp_path):
    root, env, write_config = project
    # Write config with CHANGE_ME placeholders — if the guard did NOT run
    # first, credential resolution would fail with exit 2 (CHANGE_ME),
    # not exit 3 (guard rejection). Getting exit 3 proves the guard ran first.
    write_config(db_host="CHANGE_ME", db_name="CHANGE_ME")
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1; COMMIT; INSERT INTO t VALUES (1)")

    assert result.returncode == 3, result.stderr
    assert "multi-statement" in result.stderr
    assert not marker.exists(), "psql must not be invoked when the guard rejects"


def test_guard_rejects_before_config_file_is_parsed_at_all(project, tmp_path):
    # A stronger ordering check than the CHANGE_ME test above: write a config
    # file that db_llm_load_config itself cannot parse at all (a line with
    # no '=' — a hard parse error, exit 1, independent of any credential
    # value). If the guard ran AFTER db_llm_load_config (the pre-fix
    # order), this malformed file would be read and fail first with exit 1.
    # Getting exit 3 (the guard's own verdict) proves the config file was
    # never even opened/parsed before the guard decided.
    root, env, _write_config = project
    (root / ".dbmeta" / ".dbllm.env").write_text("this line has no equals sign\n")
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "DELETE FROM t")

    assert result.returncode == 3, result.stderr
    assert "not-whitelisted" in result.stderr
    assert not marker.exists()


def test_meta_command_rejected(project, tmp_path):
    root, env, _write_config = project
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1\n\\! id")

    assert result.returncode == 3, result.stderr
    assert "meta-command" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("limit_value", ["0", "-5", "abc"])
def test_bad_limit_rejected(project, tmp_path, limit_value):
    root, env, _write_config = project
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1", "--limit", limit_value)

    assert result.returncode == 3, result.stderr
    assert "bad-limit" in result.stderr
    assert not marker.exists()


def test_not_whitelisted_statement_rejected(project, tmp_path):
    root, env, _write_config = project
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "DELETE FROM t")

    assert result.returncode == 3, result.stderr
    assert "not-whitelisted" in result.stderr
    assert not marker.exists()


# --- REQ-RS-7: independent single-file credential resolution ----------------


def test_ro_env_file_independent_resolution(project, tmp_path):
    """All five connection values come from .dbllm.env directly."""
    root, env, _write_config = project
    dump = tmp_path / "cred-dump.env"
    env["DBLLM_TEST_CRED_DUMP"] = str(dump)

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 0, result.stderr
    dumped = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert dumped["PGHOST"] == "127.0.0.1"
    assert dumped["PGPORT"] == "6432"
    assert dumped["PGDATABASE"] == "basedb"
    assert dumped["PGUSER"] == "llm_readonly"
    assert dumped["PGPASSWORD"] == "ropass"


def test_missing_host_field_fails_loud(project, tmp_path):
    """Config with DB_HOST absent fails loud via needs-human (exit 2)."""
    root, env, write_config = project
    (root / ".dbmeta" / ".dbllm.env").write_text(
        "DB_USER=llm_readonly\nDB_PASSWORD=ropass\nDB_PORT=5432\nDB_NAME=testdb\n"
    )
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    assert needs_human.exists()
    assert not marker.exists()


def test_change_me_placeholder_triggers_needs_human(project, tmp_path):
    """CHANGE_ME placeholder in DB_HOST triggers exit 2 (needs-human)."""
    root, env, write_config = project
    write_config(db_host="CHANGE_ME")
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    assert result.stdout.strip() == str(needs_human)
    assert not marker.exists()


def test_use_test_env_not_recognized(project, tmp_path):
    """USE_TEST_ENV is simply ignored — no error, session runs normally."""
    root, env, _write_config = project
    env["USE_TEST_ENV"] = "1"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 0, result.stderr
    assert "TEST_ENV_FILE" not in result.stderr


# --- fail-closed exits ------------------------------------------------------


def test_missing_config_file_fails_loud(project, tmp_path):
    """Missing .dbllm.env fails at config load (exit 1)."""
    root, env, _write_config = project
    (root / ".dbmeta" / ".dbllm.env").unlink()
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 1, result.stderr
    assert ".dbllm.env" in result.stderr
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    assert not needs_human.exists()
    assert not marker.exists()


def test_unknown_keys_silently_ignored(project, tmp_path):
    """Unknown keys like ENV_FILE are silently ignored — session runs normally."""
    root, env, write_config = project
    write_config(extra_lines=["ENV_FILE=hack/.env"])
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 0, result.stderr
    assert marker.read_text().count("called") == 1


def test_current_user_mismatch_writes_needs_human(project, tmp_path):
    root, env, _write_config = project
    env["DBLLM_TEST_MODE"] = "mismatch"
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    assert result.stdout.strip() == str(needs_human)
    # Credentials WERE resolved (mismatch is discovered inside the single
    # connection) — psql is invoked exactly once.
    assert marker.read_text().count("called") == 1
    content = needs_human.read_text()
    assert "llm_readonly" in content  # RO_ROLE
    assert "llm_readonly" in content  # PGUSER from the RO overlay in this fixture


def test_successful_result_containing_marker_text_not_misclassified(project, tmp_path):
    # Guard against a false-positive: a rc=0 query result that happens to
    # contain the literal text "RO_ROLE_MISMATCH" as ordinary data (e.g. a
    # text column value) must NOT be treated as the role-mismatch marker.
    # The marker is only trustworthy when psql exits 2 (see \quit 2 right
    # after the \echo in the generated script) — rc=0 output is query data.
    root, env, _write_config = project
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text("label\nRO_ROLE_MISMATCH\n")
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT * FROM widgets")

    assert result.returncode == 0, result.stderr
    assert marker.read_text().count("called") == 1
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    assert not needs_human.exists()
    reader = csv.reader(io.StringIO(result.stdout))
    rows = list(reader)
    assert rows == [["label"], ["RO_ROLE_MISMATCH"]]


def test_connection_refused_auth_failure_redacted(project, tmp_path):
    root, env, _write_config = project
    env["DBLLM_TEST_MODE"] = "conn_refused"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    content = needs_human.read_text()
    assert "ropass" not in content, "raw RO password must never reach needs-human.md"
    assert "[REDACTED]" in content
    assert "认证失败" in content or "无法连接" in content


def test_sasl_auth_failed_routes_to_auth_or_unreachable_guidance(project, tmp_path):
    root, env, _write_config = project
    env["DBLLM_TEST_MODE"] = "sasl_auth_failed"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    content = needs_human.read_text()
    assert "认证失败或无法连接" in content
    assert "SQL 错误" not in content


def test_connection_limit_exceeded_distinct_cause(project, tmp_path):
    root, env, _write_config = project
    env["DBLLM_TEST_MODE"] = "conn_limit"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    content = needs_human.read_text()
    assert "53300" in content
    assert "稍后重试" in content or "并发" in content


def test_plain_sql_error_exits_1_not_2(project, tmp_path):
    root, env, _write_config = project
    env["DBLLM_TEST_MODE"] = "sql_error"

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 1, result.stderr
    assert "[REDACTED]" in result.stderr or "127.0.0.1" not in result.stderr


# --- REQ-RS-5 / generated script shape --------------------------------------


def test_generated_script_has_readonly_transaction_and_copy_wrap(project, tmp_path):
    root, env, _write_config = project
    script_dump = tmp_path / "script.sql"
    argv_dump = tmp_path / "argv.txt"
    env["DBLLM_TEST_SCRIPT_DUMP"] = str(script_dump)
    env["DBLLM_TEST_ARGV_DUMP"] = str(argv_dump)

    result = _run(env, "--sql", "SELECT * FROM widgets")

    assert result.returncode == 0, result.stderr
    script = script_dump.read_text()
    assert "\\gset" in script
    assert "SET TRANSACTION READ ONLY;" in script
    assert "SET LOCAL statement_timeout = '30s';" in script
    assert "COPY (SELECT * FROM (" in script
    assert "LIMIT 201" in script  # RO_DEFAULT_LIMIT=200 (unset in config) -> N+1
    assert "ROLLBACK;" in script

    argv_lines = argv_dump.read_text().splitlines()
    assert "-A" in argv_lines
    assert "-t" in argv_lines


def test_no_limit_skips_wrap(project, tmp_path):
    root, env, _write_config = project
    script_dump = tmp_path / "script.sql"
    env["DBLLM_TEST_SCRIPT_DUMP"] = str(script_dump)

    result = _run(env, "--sql", "SELECT * FROM widgets", "--no-limit")

    assert result.returncode == 0, result.stderr
    script = script_dump.read_text()
    assert "COPY (SELECT * FROM widgets) TO STDOUT CSV HEADER;" in script
    assert "LIMIT" not in script


def test_explain_with_csv_forces_text_and_warns(project, tmp_path):
    root, env, _write_config = project
    script_dump = tmp_path / "script.sql"
    text_file = tmp_path / "explain-output.txt"
    text_file.write_text("Seq Scan on widgets  (cost=0.00..1.01 rows=1 width=4)\n")
    env["DBLLM_TEST_SCRIPT_DUMP"] = str(script_dump)
    env["DBLLM_TEST_TEXT_FILE"] = str(text_file)

    result = _run(env, "--sql", "EXPLAIN SELECT 1", "--format", "csv")

    assert result.returncode == 0, result.stderr
    assert "不支持 csv" in result.stderr
    script = script_dump.read_text()
    assert "COPY (" not in script
    assert "EXPLAIN SELECT 1;" in script
    assert result.stdout == "Seq Scan on widgets  (cost=0.00..1.01 rows=1 width=4)\n"


def test_mock_psql_invoked_exactly_once_on_success(project, tmp_path):
    root, env, _write_config = project
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 0, result.stderr
    assert marker.read_text().count("called") == 1


# --- REQ-RS-6: truncation ----------------------------------------------------


def test_csv_truncation_preserves_multiline_record(project, tmp_path):
    root, env, _write_config = project
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text('id,note\n1,foo\n2,"multi\nline"\n3,bar\n')
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)

    result = _run(env, "--sql", "SELECT * FROM widgets", "--limit", "2")

    assert result.returncode == 0, result.stderr
    assert "RO_TRUNCATED=true" in result.stderr
    assert "RO_ROWS=2" in result.stderr
    reader = csv.reader(io.StringIO(result.stdout))
    rows = list(reader)
    assert rows[0] == ["id", "note"]
    assert rows[1] == ["1", "foo"]
    assert rows[2] == ["2", "multi\nline"]
    assert len(rows) == 3  # header + exactly 2 data records, no partial 3rd


def test_no_limit_returns_all_rows_untruncated(project, tmp_path):
    root, env, _write_config = project
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text("id\n1\n2\n3\n")
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)

    result = _run(env, "--sql", "SELECT * FROM widgets", "--no-limit")

    assert result.returncode == 0, result.stderr
    assert "RO_TRUNCATED=false" in result.stderr
    reader = csv.reader(io.StringIO(result.stdout))
    rows = list(reader)
    assert rows == [["id"], ["1"], ["2"], ["3"]]


def test_out_file_receives_truncated_output(project, tmp_path):
    root, env, _write_config = project
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text("id\n1\n2\n")
    env["DBLLM_TEST_CSV_FILE"] = str(csv_file)
    out_file = tmp_path / "out" / "result.csv"

    result = _run(env, "--sql", "SELECT * FROM widgets", "--out", str(out_file))

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert out_file.read_text() == "id\n1\n2\n"


# =============================================================================
# pg-readonly-setup/scripts/readonly-setup.sh's own five-step orchestration
# (config bootstrap / generate / role-readiness probe / verify) is covered by
# its own dedicated module, tests/test_readonly_setup_shell.py
# (ro-only-credential-architecture Task 4, tasks.md 4.3) — the old orchestration
# test block that used to live here (RO_PROVISION_SH_OVERRIDE-based, testing
# the now-deleted shared/ro-provision.sh's placeholder-generation-inside-the-
# orchestrator flow) is superseded, not just stale: readonly-setup.sh's
# step ② no longer generates the RO_ENV_FILE placeholder itself — that moved
# into shared/ro-generate.sh (REQ-RP-4/REQ-RP-6) — so those scenarios don't
# even describe the current architecture's step boundaries.
# =============================================================================


def test_ro_session_needs_human_redacts_secret_in_ro_env_file_message(project, tmp_path):
    """Redaction regression: CHANGE_ME branch writes needs-human.md; secrets in
    the environment MUST NOT leak into it."""
    root, env, write_config = project
    secret = "TOPSECRET-pw-9f3e"
    write_config(db_host="CHANGE_ME")
    env["PGPASSWORD"] = secret

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 2, result.stderr
    needs_human = root / ".dbmeta" / "db-readonly" / "needs-human.md"
    assert needs_human.exists()
    body = needs_human.read_text()
    assert secret not in body, body


# --- config file is PARSED, never sourced -----------------------------------


def test_config_command_substitution_is_not_executed(project, tmp_path):
    """Config file is data, not code. A command substitution MUST NOT run."""
    root, env, write_config = project
    pwned = tmp_path / "PWNED"
    (root / ".dbmeta" / ".dbllm.env").write_text(
        "DB_HOST=127.0.0.1\n"
        "DB_PORT=6432\n"
        "DB_NAME=basedb\n"
        "DB_USER=llm_readonly\n"
        "DB_PASSWORD=ropass\n"
        f"EVIL=$(touch {pwned})\n"
    )
    cred_dump = tmp_path / "creds.txt"
    env["DBLLM_TEST_CRED_DUMP"] = str(cred_dump)

    result = _run(env, "--sql", "SELECT 1")

    assert not pwned.exists(), "config 里的命令替换被执行了"
    assert result.returncode == 0, result.stderr
    dumped = dict(
        line.split("=", 1) for line in cred_dump.read_text().splitlines() if "=" in line
    )
    assert dumped["PGHOST"] == "127.0.0.1"
    assert dumped["PGPASSWORD"] == "ropass"


def test_unknown_keys_in_config_are_ignored(project, tmp_path):
    """Unknown keys in .dbllm.env are silently ignored — forward-compat."""
    root, env, write_config = project
    write_config(extra_lines=["SOME_FUTURE_KEY=value"])
    cred_dump = tmp_path / "creds.txt"
    env["DBLLM_TEST_CRED_DUMP"] = str(cred_dump)

    result = _run(env, "--sql", "SELECT 1")

    assert result.returncode == 0, result.stderr


# --- REQ-RS-9: --prepare mode (add-pg-sql-check, ADR-0007) ------------------


def test_prepare_mode_generates_prepare_script_in_order(project, tmp_path):
    root, env, _write_config = project
    script_dump = tmp_path / "script.sql"
    env["DBLLM_TEST_SCRIPT_DUMP"] = str(script_dump)

    result = _run(env, "--sql", "SELECT * FROM widgets", "--prepare")

    assert result.returncode == 0, result.stderr
    lines = script_dump.read_text().splitlines()

    assert lines[0] == "\\set ON_ERROR_STOP on"
    assert lines[1] == "\\set VERBOSITY verbose"  # prepare-only

    prepare_idx = next(i for i, l in enumerate(lines) if l.startswith("PREPARE "))
    assert lines[prepare_idx] == f"PREPARE {lines[prepare_idx].split()[1]} AS SELECT * FROM widgets"
    # terminating ';' MUST be on its own line (REQ-RS-9: a trailing line
    # comment in the user SQL must not swallow it)
    assert lines[prepare_idx + 1] == ";"

    readback_idx = next(
        i for i, l in enumerate(lines) if l.startswith("SELECT parameter_types, result_types")
    )
    assert readback_idx > prepare_idx + 1
    dealloc_idx = next(i for i, l in enumerate(lines) if l.startswith("DEALLOCATE "))
    assert dealloc_idx > readback_idx
    assert lines[dealloc_idx + 1] == "ROLLBACK;"

    assert "SET LOCAL lc_messages = 'C';" in lines  # prepare-only
    assert "SET TRANSACTION READ ONLY;" in lines
    assert "SET LOCAL statement_timeout = '30s';" in lines

    # equivalent to --no-limit: WRAPPED is the user statement's original
    # text, never LIMIT-wrapped or COPY-wrapped
    script = "\n".join(lines)
    assert "LIMIT" not in script
    assert "COPY (" not in script


def test_prepare_mode_name_is_random_across_invocations(project, tmp_path):
    root, env, _write_config = project

    script1 = tmp_path / "script1.sql"
    env["DBLLM_TEST_SCRIPT_DUMP"] = str(script1)
    r1 = _run(env, "--sql", "SELECT 1", "--prepare")
    assert r1.returncode == 0, r1.stderr

    script2 = tmp_path / "script2.sql"
    env["DBLLM_TEST_SCRIPT_DUMP"] = str(script2)
    r2 = _run(env, "--sql", "SELECT 1", "--prepare")
    assert r2.returncode == 0, r2.stderr

    name1 = next(l for l in script1.read_text().splitlines() if l.startswith("PREPARE ")).split()[1]
    name2 = next(l for l in script2.read_text().splitlines() if l.startswith("PREPARE ")).split()[1]
    assert name1 != name2
    assert name1.startswith("_pgsc_")
    assert name2.startswith("_pgsc_")


def test_prepare_mode_skips_keyword_whitelist(project, tmp_path):
    # In default mode, an INSERT is rejected as not-whitelisted (guard
    # mode="query"). --prepare relies on PG's own PREPARE grammar instead
    # (ADR-0007: no second, self-maintained keyword list) — the guard here
    # only still enforces single-statement/no meta-command/no set_config.
    root, env, _write_config = project
    script_dump = tmp_path / "script.sql"
    env["DBLLM_TEST_SCRIPT_DUMP"] = str(script_dump)

    result = _run(env, "--sql", "INSERT INTO widgets(id) VALUES (1)", "--prepare")

    assert result.returncode == 0, result.stderr
    assert "PREPARE " in script_dump.read_text()


def test_prepare_mode_still_rejects_multi_statement(project, tmp_path):
    root, env, _write_config = project
    marker = tmp_path / "psql-called.marker"
    env["DBLLM_TEST_MARKER"] = str(marker)

    result = _run(env, "--sql", "SELECT 1; DROP TABLE widgets", "--prepare")

    assert result.returncode == 3, result.stderr
    assert "multi-statement" in result.stderr
    assert not marker.exists()


def test_prepare_mode_rejects_explicit_limit(project, tmp_path):
    root, env, _write_config = project

    result = _run(env, "--sql", "SELECT 1", "--prepare", "--limit", "10")

    assert result.returncode == 1, result.stderr
    assert "--prepare" in result.stderr


def test_prepare_mode_output_is_passthrough_not_truncated(project, tmp_path):
    # Prepare mode's payload is one parameter_types/result_types row, never a
    # result set — it MUST bypass ro_guard.py's truncate step entirely.
    root, env, _write_config = project
    text_file = tmp_path / "raw.txt"
    text_file.write_text("int4|text\n")
    env["DBLLM_TEST_TEXT_FILE"] = str(text_file)

    result = _run(env, "--sql", "SELECT 1", "--prepare")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "int4|text\n"


def test_prepare_mode_out_file_receives_raw_output(project, tmp_path):
    root, env, _write_config = project
    text_file = tmp_path / "raw.txt"
    text_file.write_text("int4|text\n")
    env["DBLLM_TEST_TEXT_FILE"] = str(text_file)
    out_file = tmp_path / "out" / "result.txt"

    result = _run(env, "--sql", "SELECT 1", "--prepare", "--out", str(out_file))

    assert result.returncode == 0, result.stderr
    assert out_file.read_text() == "int4|text\n"


def test_default_mode_script_unaffected_by_prepare_additions(project, tmp_path):
    # Regression pin (REQ-RS-9): the default path MUST remain byte-identical
    # — no VERBOSITY verbose / lc_messages / PREPARE leaking in when
    # --prepare is not passed.
    root, env, _write_config = project
    script_dump = tmp_path / "script.sql"
    env["DBLLM_TEST_SCRIPT_DUMP"] = str(script_dump)

    result = _run(env, "--sql", "SELECT * FROM widgets")

    assert result.returncode == 0, result.stderr
    script = script_dump.read_text()
    assert "VERBOSITY" not in script
    assert "lc_messages" not in script
    assert "PREPARE " not in script
