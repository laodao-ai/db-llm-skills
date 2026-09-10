"""Shell-layer scope propagation tests for shared/db-collect.sh's schemas_csv /
--schema three-way priority (CLI > SCHEMAS in .dbllm.env > full DB) — the
top half of the `requested_schemas` data-flow chain documented in design.md's
「数据流图」. The bottom half (render.py narrowing D-L convergence from the
`requested_schemas` field) is covered by
pg-dict/scripts/render_test.py::RequestedSchemasScopeTests.

No real Postgres needed: a mock `psql` binary is put first on PATH. It doesn't
connect to anything — it just inspects its own argv for the `-v
schemas_csv=...` flag shared/db-collect.sh passed it, reproduces
db-collect.sql's `CASE WHEN :'schemas_csv' = '' THEN NULL ELSE
to_jsonb(string_to_array(...)) END` logic in miniature, and prints a minimal
collect_version=1 JSON document to stdout — exactly what shared/db-collect.sh
expects psql to have produced. This isolates "did the shell layer compute and
pass the right schemas_csv" from "does the SQL layer render it correctly"
(covered separately by tests/test_db_collect_contract.py against a real DB).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_COLLECT_SH = REPO_ROOT / "shared" / "db-collect.sh"

MOCK_PSQL = r"""#!/bin/bash
# Mock psql for tests/test_scope_shell.py — does not connect to any database.
# Finds the `-v schemas_csv=...` flag among its argv and reproduces
# db-collect.sql's CASE-based requested_schemas derivation.
#
# When DBLLM_TEST_CRED_DUMP is set, also dumps the PG*/DATABASE_URL env vars
# db-collect.sh's db_llm_export_pg_env call is supposed to have exported
# by the time psql runs — lets test_db_collect_exports_pg_env_for_mock_psql
# assert on the shell-layer credential-resolution path (DD-1), which the
# schemas_csv-only mock above never touched.
if [[ -n "${DBLLM_TEST_CRED_DUMP:-}" ]]; then
    {
        printf 'PGHOST=%s\n' "${PGHOST-<unset>}"
        printf 'PGPORT=%s\n' "${PGPORT-<unset>}"
        printf 'PGDATABASE=%s\n' "${PGDATABASE-<unset>}"
        printf 'PGUSER=%s\n' "${PGUSER-<unset>}"
        printf 'PGPASSWORD=%s\n' "${PGPASSWORD-<unset>}"
        printf 'PGCONNECT_TIMEOUT=%s\n' "${PGCONNECT_TIMEOUT-<unset>}"
        printf 'DATABASE_URL=%s\n' "${DATABASE_URL-<unset>}"
    } > "${DBLLM_TEST_CRED_DUMP}"
fi

schemas_csv=""
prev=""
for arg in "$@"; do
    if [[ "${prev}" == "-v" && "${arg}" == schemas_csv=* ]]; then
        schemas_csv="${arg#schemas_csv=}"
    fi
    prev="${arg}"
done

if [[ -z "${schemas_csv}" ]]; then
    requested_schemas="null"
else
    IFS=',' read -ra parts <<< "${schemas_csv}"
    joined=""
    for p in "${parts[@]}"; do
        [[ -n "${joined}" ]] && joined+=","
        joined+="\"${p}\""
    done
    requested_schemas="[${joined}]"
fi

printf '{"collect_version": 1, "requested_schemas": %s, "schemas": []}\n' "${requested_schemas}"
"""


@pytest.fixture()
def project(tmp_path: Path):
    """A throwaway consuming-project layout: .dbllm.env + a mock `psql` shim
    placed first on PATH. Returns (root_dir, env, write_config) where
    write_config(schemas=None) rewrites .dbllm.env's SCHEMAS line."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dbmeta").mkdir()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mock_psql_path = bin_dir / "psql"
    mock_psql_path.write_text(MOCK_PSQL)
    mock_psql_path.chmod(mock_psql_path.stat().st_mode | stat.S_IEXEC)

    def write_config(schemas: str = "") -> None:
        (root / ".dbmeta" / ".dbllm.env").write_text(
            f"SCHEMAS={schemas}\n"
            "DB_HOST=127.0.0.1\n"
            "DB_PORT=6432\n"
            "DB_NAME=x\n"
            "DB_USER=x\n"
            "DB_PASSWORD=x\n"
        )

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ROOT_DIR"] = str(root)

    return root, env, write_config


def _run_collect(env: dict[str, str], *extra_args: str) -> dict:
    result = subprocess.run(
        [str(DB_COLLECT_SH), *extra_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"shared/db-collect.sh 非零退出\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout)


def test_cli_schema_flag_wins_over_schemas_config(project):
    _root, env, write_config = project
    write_config(schemas="x,y")  # .dbllm.env declares SCHEMAS=x,y

    doc = _run_collect(env, "--schema", "a", "--schema", "b")

    assert doc["requested_schemas"] == ["a", "b"]


def test_schemas_config_used_when_no_cli_flag(project):
    _root, env, write_config = project
    write_config(schemas="p,q")

    doc = _run_collect(env)

    assert doc["requested_schemas"] == ["p", "q"]


def test_unset_scope_means_full_scan(project):
    _root, env, write_config = project
    write_config(schemas="")  # SCHEMAS declared but empty == unset

    doc = _run_collect(env)

    assert doc["requested_schemas"] is None


def test_db_collect_exports_pg_env_for_mock_psql(project, tmp_path):
    """Credential-resolution guard: shared/db-collect.sh resolves credentials by
    calling shared/config.sh's db_llm_export_pg_env, which reads DB_HOST/
    DB_PORT/DB_NAME/DB_USER/DB_PASSWORD directly from .dbllm.env. This
    asserts the shell layer exports the correct PG* values and unsets
    DATABASE_URL."""
    _root, env, write_config = project
    write_config(schemas="")

    dump_file = tmp_path / "cred-dump.env"
    env["DBLLM_TEST_CRED_DUMP"] = str(dump_file)
    env["DATABASE_URL"] = "postgres://should-be-unset/db"

    _run_collect(env)

    dumped = dict(
        line.split("=", 1) for line in dump_file.read_text().splitlines() if "=" in line
    )
    assert dumped["PGHOST"] == "127.0.0.1"
    assert dumped["PGPORT"] == "6432"
    assert dumped["PGDATABASE"] == "x"
    assert dumped["PGUSER"] == "x"
    assert dumped["PGPASSWORD"] == "x"
    assert dumped["PGCONNECT_TIMEOUT"] == "10"
    assert dumped["DATABASE_URL"] == "<unset>"
