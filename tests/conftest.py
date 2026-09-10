"""Shared pytest fixtures for the shell-layer test suite.

Extracted from tests/test_ro_session_shell.py and
tests/test_pg_query_ro_shell.py, where MOCK_PSQL and the `project` fixture
used to be duplicated verbatim (code-review Standards-axis finding,
skill-completeness-gaps Task 2 fix). pytest auto-discovers this file for
every test module under tests/.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

MOCK_PSQL = r"""#!/bin/bash
# Controllable mock psql for the shell-layer test suite — never connects to
# a real database. Behavior selected by DBLLM_TEST_MODE (default: success).
if [[ -n "${DBLLM_TEST_MARKER:-}" ]]; then
    echo called >> "${DBLLM_TEST_MARKER}"
fi
if [[ -n "${DBLLM_TEST_ARGV_DUMP:-}" ]]; then
    printf '%s\n' "$@" > "${DBLLM_TEST_ARGV_DUMP}"
fi

script_file=""
prev=""
for arg in "$@"; do
    if [[ "${prev}" == "-f" ]]; then
        script_file="${arg}"
    fi
    prev="${arg}"
done

if [[ -n "${script_file}" && -n "${DBLLM_TEST_SCRIPT_DUMP:-}" ]]; then
    cp "${script_file}" "${DBLLM_TEST_SCRIPT_DUMP}"
fi

if [[ -n "${DBLLM_TEST_CRED_DUMP:-}" ]]; then
    {
        printf 'PGHOST=%s\n' "${PGHOST-<unset>}"
        printf 'PGPORT=%s\n' "${PGPORT-<unset>}"
        printf 'PGDATABASE=%s\n' "${PGDATABASE-<unset>}"
        printf 'PGUSER=%s\n' "${PGUSER-<unset>}"
        printf 'PGPASSWORD=%s\n' "${PGPASSWORD-<unset>}"
    } > "${DBLLM_TEST_CRED_DUMP}"
fi

mode="${DBLLM_TEST_MODE:-success}"

case "${mode}" in
    mismatch)
        echo RO_ROLE_MISMATCH
        exit 2
        ;;
    conn_refused)
        echo "psql: error: connection to server at \"${PGHOST}\" (${PGHOST}), port ${PGPORT} failed: FATAL: password authentication failed for user \"${PGUSER}\" (secret=${PGPASSWORD})" >&2
        exit 2
        ;;
    sasl_auth_failed)
        echo "psql: error: connection to server at \"${PGHOST}\" (${PGHOST}), port ${PGPORT} failed: FATAL:  SASL authentication failed" >&2
        exit 2
        ;;
    conn_limit)
        echo "psql: error: FATAL: sorry, too many clients already (SQLSTATE 53300) host=${PGHOST} user=${PGUSER}" >&2
        exit 2
        ;;
    sql_error)
        echo "psql:${script_file}:9: ERROR: some sql error host=${PGHOST}" >&2
        exit 3
        ;;
    success)
        if grep -q 'COPY (' "${script_file}" 2>/dev/null; then
            if [[ -n "${DBLLM_TEST_CSV_FILE:-}" ]]; then
                cat "${DBLLM_TEST_CSV_FILE}"
            else
                printf 'col\n1\n'
            fi
        else
            if [[ -n "${DBLLM_TEST_TEXT_FILE:-}" ]]; then
                cat "${DBLLM_TEST_TEXT_FILE}"
            else
                printf 'line1\n'
            fi
        fi
        exit 0
        ;;
    *)
        echo "unknown DBLLM_TEST_MODE=${mode}" >&2
        exit 1
        ;;
esac
"""


@pytest.fixture()
def project(tmp_path: Path):
    """A throwaway consuming-project layout: .dbllm.env (single config+credential
    file) + a mock `psql` shim first on PATH. Returns (root_dir, env, write_config)."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dbmeta").mkdir()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mock_psql_path = bin_dir / "psql"
    mock_psql_path.write_text(MOCK_PSQL)
    mock_psql_path.chmod(mock_psql_path.stat().st_mode | stat.S_IEXEC)

    def write_config(
        *,
        db_host: str = "127.0.0.1",
        db_port: str = "6432",
        db_name: str = "basedb",
        db_user: str = "llm_readonly",
        db_password: str = "ropass",
        extra_lines: list[str] | None = None,
    ) -> None:
        lines = [
            f"DB_HOST={db_host}",
            f"DB_PORT={db_port}",
            f"DB_NAME={db_name}",
            f"DB_USER={db_user}",
            f"DB_PASSWORD={db_password}",
        ]
        if extra_lines:
            lines.extend(extra_lines)
        (root / ".dbmeta" / ".dbllm.env").write_text("\n".join(lines) + "\n")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ROOT_DIR"] = str(root)
    env.pop("USE_TEST_ENV", None)

    write_config()  # sane default

    return root, env, write_config
