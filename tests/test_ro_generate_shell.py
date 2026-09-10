"""Offline (zero-DB) tests for shared/ro-generate.sh — the pure-text generator.

No real Postgres, no mock psql: ro-generate.sh makes ZERO database connections
by design (it only ever reads/writes local files: .dbllm.env,
.dbmeta/db-readonly/setup.sql, .dbmeta/db-readonly/userlist-fragment.txt). Every
test here drives the real script against a throwaway consuming-project layout
in tmp_path and asserts on the files it produced (or, for the fail-loud paths,
did NOT produce).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RO_GENERATE_SH = REPO_ROOT / "shared" / "ro-generate.sh"


@pytest.fixture()
def project(tmp_path: Path):
    """A throwaway, git-initialized consuming-project layout with a
    .dbllm.env containing DB_* fields. Returns (root, env, write_config)."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dbmeta").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text(".dbmeta/.dbllm.env\n.dbmeta/db-readonly/\n")

    def write_config(schemas: str = "", db_user: str = "llm_readonly") -> None:
        lines = [
            f"SCHEMAS={schemas}",
            "DB_HOST=CHANGE_ME",
            "DB_PORT=5432",
            "DB_NAME=CHANGE_ME",
            f"DB_USER={db_user}",
            "DB_PASSWORD=CHANGE_ME",
        ]
        (root / ".dbmeta" / ".dbllm.env").write_text("\n".join(lines) + "\n")

    write_config()

    env = dict(os.environ)
    env["ROOT_DIR"] = str(root)
    env.pop("CLAUDE_PROJECT_DIR", None)

    return root, env, write_config


def _run(env: dict[str, str], *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(RO_GENERATE_SH), *extra_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _config_path(root: Path) -> Path:
    return root / ".dbmeta" / ".dbllm.env"


def _setup_sql_path(root: Path) -> Path:
    return root / ".dbmeta" / "db-readonly" / "setup.sql"


def _fragment_path(root: Path) -> Path:
    return root / ".dbmeta" / "db-readonly" / "userlist-fragment.txt"


def _perm(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k] = v
    return out


# --- Determinism / structural snapshot assertions ---------------------------


def test_replace_generates_setup_sql_and_updates_password(project):
    root, env, write_config = project
    write_config(schemas="auth,shop")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _setup_sql_path(root).is_file()
    assert _fragment_path(root).is_file()
    assert _perm(_setup_sql_path(root)) == "0o600"
    assert _perm(_fragment_path(root)) == "0o600"

    fields = _parse_env_file(_config_path(root))
    assert fields["DB_PASSWORD"] != "CHANGE_ME"
    assert len(fields["DB_PASSWORD"]) >= 32


def test_setup_sql_first_line_is_on_error_stop(project):
    root, env, write_config = project
    write_config(schemas="auth")
    _run(env)

    first_line = _setup_sql_path(root).read_text().splitlines()[0]
    assert first_line == r"\set ON_ERROR_STOP on"


def test_setup_sql_is_a_single_do_block(project):
    root, env, write_config = project
    write_config(schemas="auth")
    _run(env)

    sql = _setup_sql_path(root).read_text()
    assert "DO $do$" in sql
    # [Task 2] The DO block itself still ends with `$do$;` -- only a trailing
    # commented ALTER ROLE (Task 2 tail) follows it in the file.
    assert "\n$do$;\n" in sql
    assert sql.count("DO $do$") == 1


def test_setup_sql_contains_dangerous_role_audit_section(project):
    root, env, write_config = project
    write_config(schemas="auth")
    _run(env)

    sql = _setup_sql_path(root).read_text()
    assert "pre-execution dangerous-role audit" in sql
    assert "RAISE EXCEPTION" in sql
    assert "rolsuper OR rolcreatedb OR rolbypassrls OR rolreplication" in sql
    assert "pg_auth_members" in sql
    assert "has_table_privilege" in sql
    assert "FROM information_schema.role_table_grants" not in sql


def test_setup_sql_contains_idempotent_create_role_and_convergence(project):
    root, env, write_config = project
    write_config(schemas="auth")
    _run(env)

    sql = _setup_sql_path(root).read_text()
    assert "CREATE ROLE %I LOGIN PASSWORD %L" in sql
    assert "NOCREATEROLE NOINHERIT CONNECTION LIMIT 5" in sql
    assert "statement_timeout" in sql
    assert "idle_in_transaction_session_timeout" in sql
    assert "ALTER DEFAULT PRIVILEGES" in sql


def test_setup_sql_grants_set_on_lc_messages(project):
    # ADR-0007 / add-pg-sql-check task 8: lc_messages has GUC context=superuser,
    # so ro-session.sh --prepare's `SET LOCAL lc_messages='C'` 42501s for any
    # non-superuser read-only role unless this (non-privilege-escalation,
    # PG 15+) grant is present in the generated setup.sql.
    root, env, write_config = project
    write_config(schemas="auth")
    _run(env)

    sql = _setup_sql_path(root).read_text()
    assert "GRANT SET ON PARAMETER lc_messages TO %I" in sql


def test_setup_sql_resolves_scope_at_execution_time_when_schemas_unset(project):
    root, env, write_config = project
    write_config(schemas="")
    _run(env)

    sql = _setup_sql_path(root).read_text()
    assert "explicit_scope    CONSTANT boolean := false;" in sql
    assert "pg_namespace" in sql
    assert "pg_depend" in sql


def test_setup_sql_embeds_explicit_schema_list_when_declared(project):
    root, env, write_config = project
    write_config(schemas="auth,shop")
    _run(env)

    sql = _setup_sql_path(root).read_text()
    assert "explicit_scope    CONSTANT boolean := true;" in sql
    assert "ARRAY['auth','shop']::name[]" in sql


def test_snapshot_determinism_same_inputs_same_bytes(project):
    root, env, write_config = project
    write_config(schemas="auth,shop", db_user="my_ro")
    r1 = _run(env)
    assert r1.returncode == 0, r1.stderr
    bytes1 = _setup_sql_path(root).read_bytes()

    r2 = _run(env)  # rerun: password now non-CHANGE_ME -> reused, not rotated
    assert r2.returncode == 0, r2.stderr
    bytes2 = _setup_sql_path(root).read_bytes()

    assert bytes1 == bytes2


# --- Password three-state semantics ------------------------------------------


def test_change_me_password_generates_random_alnum_len_32(project):
    root, env, write_config = project
    write_config(schemas="auth")
    result = _run(env)
    assert result.returncode == 0, result.stderr

    fields = _parse_env_file(_config_path(root))
    pw = fields["DB_PASSWORD"]
    assert len(pw) >= 32
    assert pw.isalnum()
    assert pw not in result.stdout
    assert pw not in result.stderr


def test_replace_preserves_other_fields_and_comments(project):
    root, env, _write_config = project
    config = _config_path(root)
    config.write_text(
        "# hand-edited comment\n"
        "SCHEMAS=auth\n"
        "DB_HOST=custom.example.com\n"
        "DB_PORT=6432\n"
        "DB_NAME=mydb\n"
        "DB_USER=llm_readonly\n"
        "DB_PASSWORD=CHANGE_ME\n"
    )

    result = _run(env)
    assert result.returncode == 0, result.stderr

    text = config.read_text()
    assert "# hand-edited comment" in text
    assert "DB_HOST=custom.example.com" in text
    assert "DB_PORT=6432" in text
    assert "DB_NAME=mydb" in text
    assert "DB_USER=llm_readonly" in text
    assert "SCHEMAS=auth" in text

    fields = _parse_env_file(config)
    assert fields["DB_PASSWORD"] != "CHANGE_ME"
    assert len(fields["DB_PASSWORD"]) >= 32


def test_existing_usable_password_is_reused_not_rotated(project):
    root, env, _write_config = project
    config = _config_path(root)
    config.write_text(
        "SCHEMAS=auth\n"
        "DB_HOST=h\nDB_PORT=5432\nDB_NAME=d\nDB_USER=llm_readonly\n"
        "DB_PASSWORD=alreadyUsablePassword123456789012\n"
    )
    before_bytes = config.read_bytes()

    result = _run(env)
    assert result.returncode == 0, result.stderr

    after_bytes = config.read_bytes()
    assert before_bytes == after_bytes

    sql = _setup_sql_path(root).read_text()
    assert "alreadyUsablePassword123456789012" in sql


def test_reused_password_with_unsafe_chars_fails_loud(project):
    root, env, _write_config = project
    config = _config_path(root)
    config.write_text(
        "SCHEMAS=auth\n"
        "DB_HOST=h\nDB_PORT=5432\nDB_NAME=d\nDB_USER=llm_readonly\n"
        "DB_PASSWORD=\"abc'def0123456789012345678901234\"\n"
    )

    result = _run(env)

    assert result.returncode != 0
    assert not _setup_sql_path(root).exists()
    assert not _fragment_path(root).exists()


# --- Identifier validation ---------------------------------------------------


def test_illegal_db_user_fails_loud_no_script_produced(project):
    root, env, write_config = project
    write_config(schemas="auth", db_user="bad;role")

    result = _run(env)

    assert result.returncode != 0
    assert not (root / ".dbmeta" / "db-readonly").exists()


def test_illegal_schema_fails_loud_no_script_produced(project):
    root, env, write_config = project
    write_config(schemas="auth,dro'p")

    result = _run(env)

    assert result.returncode != 0
    assert not (root / ".dbmeta" / "db-readonly").exists()


# --- git-ignore guard ---------------------------------------------------------


def test_config_not_ignored_fails_closed(project):
    root, env, write_config = project
    write_config(schemas="auth")
    (root / ".gitignore").write_text(".dbmeta/db-readonly/\n")  # config NOT ignored

    result = _run(env)

    assert result.returncode != 0
    assert not (root / ".dbmeta" / "db-readonly").exists()


def test_config_not_ignored_bypass_flag_succeeds(project):
    root, env, write_config = project
    write_config(schemas="auth")
    (root / ".gitignore").write_text(".dbmeta/db-readonly/\n")

    result = _run(env, "--allow-unignored")

    assert result.returncode == 0, result.stderr
    assert _setup_sql_path(root).exists()


def test_unignored_setup_sql_fails_closed(project):
    root, env, write_config = project
    write_config(schemas="auth")
    (root / ".gitignore").write_text(".dbmeta/.dbllm.env\n")  # db-readonly/ NOT ignored

    result = _run(env)

    assert result.returncode != 0
    assert not (root / ".dbmeta" / "db-readonly").exists()
    assert "git-ignore" in result.stderr


def test_unignored_build_products_bypass_flag_succeeds(project):
    root, env, write_config = project
    write_config(schemas="auth")
    (root / ".gitignore").write_text(".dbmeta/.dbllm.env\n")

    result = _run(env, "--allow-unignored")

    assert result.returncode == 0, result.stderr
    assert _setup_sql_path(root).exists()
    assert _fragment_path(root).exists()


# --- Zero stdout/stderr/argv password leakage --------------------------------


def test_password_never_in_stdout_or_stderr(project):
    root, env, write_config = project
    write_config(schemas="auth", db_user="llm_readonly")

    result = _run(env)
    assert result.returncode == 0, result.stderr

    fields = _parse_env_file(_config_path(root))
    pw = fields["DB_PASSWORD"]
    assert pw not in result.stdout
    assert pw not in result.stderr


# --- [impl-review-fix] regressions ------------------------------------------


def test_reused_password_with_double_quote_fails_loud(project):
    root, env, _write_config = project
    config = _config_path(root)
    config.write_text(
        "SCHEMAS=auth\n"
        'DB_HOST=h\nDB_PORT=5432\nDB_NAME=d\nDB_USER=llm_readonly\n'
        '''DB_PASSWORD='abc"def'\n'''
    )

    result = _run(env)

    assert result.returncode != 0
    assert not _setup_sql_path(root).exists()
    assert not _fragment_path(root).exists()


def test_role_convergence_includes_login(project):
    root, env, write_config = project
    write_config(schemas="auth", db_user="llm_readonly")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    sql = _setup_sql_path(root).read_text()
    assert "ALTER ROLE %I LOGIN NOCREATEROLE NOINHERIT CONNECTION LIMIT 5" in sql


# --- Task 2: existing-role NOTICE + commented ALTER ROLE ---------------------


def test_setup_sql_existing_role_notice_and_commented_alter_role(project):
    import re

    root, env, write_config = project
    write_config(schemas="auth")
    result = _run(env)
    assert result.returncode == 0, result.stderr

    sql = _setup_sql_path(root).read_text()
    assert "RAISE NOTICE" in sql
    assert "已存在" in sql
    assert "密码未改动" in sql

    # NOTICE line itself must not embed the password literal.
    fields = _parse_env_file(_config_path(root))
    pw = fields["DB_PASSWORD"]
    notice_lines = [line for line in sql.splitlines() if "RAISE NOTICE" in line]
    assert notice_lines
    for line in notice_lines:
        assert pw not in line

    alter_lines = [
        line
        for line in sql.splitlines()
        if re.match(r'^-- ALTER ROLE "[A-Za-z0-9_]+" PASSWORD \'', line)
    ]
    assert len(alter_lines) == 1, sql
    assert pw in alter_lines[0]
    assert "ALTER ROLE '" not in sql
    assert sql.count("DO $do$") == 1


# --- Task 2: userlist-fragment.txt first-line applicability comment ----------


def test_fragment_first_line_is_semicolon_comment(project):
    root, env, write_config = project
    write_config(schemas="auth")
    result = _run(env)
    assert result.returncode == 0, result.stderr

    lines = _fragment_path(root).read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(";")
    assert "auth_file" in lines[0]
    assert "auth_query" in lines[0]

    fields = _parse_env_file(_config_path(root))
    pw = fields["DB_PASSWORD"]
    assert lines[1] == f'"llm_readonly" "{pw}"'


# --- Task 2: unsafe reused password fail-loud shared-rotation hint -----------


def test_unsafe_reused_password_hint_mentions_shared_rotation(project):
    root, env, _write_config = project
    config = _config_path(root)
    config.write_text(
        "SCHEMAS=auth\n"
        "DB_HOST=h\nDB_PORT=5432\nDB_NAME=d\nDB_USER=llm_readonly\n"
        "DB_PASSWORD=\"abc'def0123456789012345678901234\"\n"
    )

    result = _run(env)

    assert result.returncode != 0
    assert "共享" in result.stderr
    assert "轮换" in result.stderr
    assert "协调" in result.stderr


# --- T23: cross-file password consistency ------------------------------------


def test_concurrent_rewrite_of_config_file_fails_loud(project, tmp_path):
    """T23 regression: the consistency re-read detects concurrent password
    overwrites. Simulated deterministically via a PATH-injected mktemp shim."""
    root, env, _write_config = project
    config = root / ".dbmeta" / ".dbllm.env"

    real_mktemp = shutil.which("mktemp")
    assert real_mktemp, "mktemp 不在 PATH 中"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "mktemp"
    shim.write_text(
        "#!/bin/bash\n"
        f'if [[ "$*" == *".setup.sql"* ]]; then\n'
        f'    /usr/bin/sed -i "" -e "s/^DB_PASSWORD=.*/DB_PASSWORD=CONCURRENTLY_REPLACED/" "{config}"\n'
        "fi\n"
        f'exec "{real_mktemp}" "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = dict(env)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(
        [str(RO_GENERATE_SH)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "并发" in result.stderr or "不一致" in result.stderr, result.stderr
    assert "CONCURRENTLY_REPLACED" not in result.stderr
    assert "CONCURRENTLY_REPLACED" not in result.stdout


def test_no_concurrent_rewrite_still_succeeds(project, tmp_path):
    """T23 counterpart: the consistency re-read MUST NOT fire on the ordinary
    path — a plain first run still exits 0."""
    root, env, _write_config = project

    result = subprocess.run(
        [str(RO_GENERATE_SH)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    config_text = (root / ".dbmeta" / ".dbllm.env").read_text()
    password = [
        line.split("=", 1)[1]
        for line in config_text.splitlines()
        if line.startswith("DB_PASSWORD=")
    ][0]
    setup_sql = (root / ".dbmeta" / "db-readonly" / "setup.sql").read_text()
    fragment = (root / ".dbmeta" / "db-readonly" / "userlist-fragment.txt").read_text()
    assert password in setup_sql
    assert password in fragment
