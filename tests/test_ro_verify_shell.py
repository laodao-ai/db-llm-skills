"""Mock tests for shared/ro-verify.sh (Task 3, tasks.md 3.3, design.md 「六面审计」,
specs/ro-provision/spec.md REQ-RP-5, REQ-RP-3).

No real Postgres needed: a controllable mock `psql` binary sits first on PATH.
It never connects to anything. Each of ro-verify.sh's audit queries pipes
distinctive SQL text to psql's stdin via `-f -` (never `-c`) — the mock
classifies each call by sniffing a unique substring and answers with rows
controlled by MOCK_* env vars (empty/all-false by default, i.e. a clean role).
A second mock stands in for shared/ro-session.sh (substituted via
RO_SESSION_SH_OVERRIDE, the same testability seam readonly-setup.sh already
establishes for this exact delegation) to control the full-link probe outcome
independently of the audit.

This isolates "did ro-verify.sh compose the right SQL, classify findings into
fail-closed/report-only correctly, and produce the right exit code / redacted
needs-human.md" from "does PG's has_*_privilege family actually work the way
the design assumes". The latter has NO automated anchor in this repo on
purpose: exercising it end to end needs a role to audit, and creating one is
privileged SQL this repo MUST NOT execute (CLAUDE.md 「特权 SQL 边界」). The
predicate semantics were verified against PostgreSQL's own docs §9.27.2 at
design time (decision-memo C2), and a human sees the real thing on any real
onboarding.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RO_VERIFY_SH = REPO_ROOT / "shared" / "ro-verify.sh"

MOCK_PASSWORD = "s3cr3t-marker-do-not-leak"

MOCK_PSQL = r"""#!/bin/bash
# Mock psql for tests/test_ro_verify_shell.py. Never connects to a real
# database. Classifies ro-verify.sh's sequential ro_audit_sql calls by
# sniffing distinctive substrings in the SQL piped to stdin (every call site
# uses `-f -`). Dumps its inherited PG* env + full stdin to files when the
# corresponding DBLLM_TEST_*_DUMP env var is set, for assertions.

stdin_content="$(cat)"

if [[ -n "${DBLLM_TEST_CRED_DUMP:-}" ]]; then
    {
        printf 'PGHOST=%s\n' "${PGHOST-<unset>}"
        printf 'PGPORT=%s\n' "${PGPORT-<unset>}"
        printf 'PGDATABASE=%s\n' "${PGDATABASE-<unset>}"
        printf 'PGUSER=%s\n' "${PGUSER-<unset>}"
        printf 'PGPASSWORD=%s\n' "${PGPASSWORD-<unset>}"
    } >> "${DBLLM_TEST_CRED_DUMP}"
fi

query_id="OTHER"
case "${stdin_content}" in
    *"SELECT current_user"*) query_id="CURRENT_USER" ;;
    *"ORDER BY n.nspname;"*) query_id="INCLUSION" ;;
    *"'SUPERUSER'"*) query_id="ROLE_FLAGS" ;;
    *"pg_auth_members m"*) query_id="MEMBERSHIP" ;;
    *"registrants AS"*) query_id="OWNER_GAP" ;;
    *"pg_default_acl a"*) query_id="DEFACL" ;;
    *"datname <> current_database()"*) query_id="CLUSTER_CONNECT" ;;
    *"prosecdef"*) query_id="SECDEF" ;;
    *"dblink%"*) query_id="ESCAPE" ;;
    *"pg_get_userbyid(c.relowner) = :"*) query_id="OWNERSHIP" ;;
    *"has_table_privilege(:'ro_role', c.oid, 'SELECT')"*) query_id="OUT_SELECT" ;;
    *"has_schema_privilege(:'ro_role', n.oid, 'USAGE')"*"scope_csv"*) query_id="OUT_USAGE" ;;
    *"has_sequence_privilege(:'ro_role', c.oid, 'USAGE')"*) query_id="SEQ_PRIV" ;;
    *"has_table_privilege(:'ro_role', c.oid, 'INSERT')"*) query_id="TABLE_PRIV" ;;
    *"'CREATE', has_database_privilege"*) query_id="DB_CREATE" ;;
    *"has_schema_privilege(:'ro_role', n.oid, 'CREATE')"*) query_id="SCHEMA_CREATE" ;;
esac

if [[ -n "${DBLLM_TEST_STDIN_DUMP:-}" && "${query_id}" == "${DBLLM_TEST_STDIN_DUMP_QUERY:-ROLE_FLAGS}" ]]; then
    printf '%s' "${stdin_content}" > "${DBLLM_TEST_STDIN_DUMP}"
fi

if [[ "${query_id}" == "${DBLLM_TEST_FAIL_QUERY:-}" ]]; then
    echo "mock psql: simulated failure for ${query_id}" >&2
    exit 3
fi

case "${query_id}" in
    CURRENT_USER) printf '%s\n' "${MOCK_CURRENT_USER:-llm_readonly}" ;;
    INCLUSION) printf '%s\n' "${MOCK_INCLUSION:-public}" ;;
    ROLE_FLAGS)
        printf 'SUPERUSER|%s\n' "${MOCK_SUPERUSER:-f}"
        printf 'CREATEDB|%s\n' "${MOCK_CREATEDB:-f}"
        printf 'BYPASSRLS|%s\n' "${MOCK_BYPASSRLS:-f}"
        printf 'REPLICATION|%s\n' "${MOCK_REPLICATION:-f}"
        printf 'CREATEROLE|%s\n' "${MOCK_CREATEROLE:-f}"
        ;;
    MEMBERSHIP) [[ -n "${MOCK_MEMBERSHIP:-}" ]] && printf '%s\n' "${MOCK_MEMBERSHIP}" ;;
    SCHEMA_CREATE) [[ -n "${MOCK_SCHEMA_CREATE:-}" ]] && printf '%s\n' "${MOCK_SCHEMA_CREATE}" ;;
    DB_CREATE)
        printf 'CREATE|%s|%s\n' "${MOCK_DB_CREATE_RO:-f}" "${MOCK_DB_CREATE_PUB:-f}"
        printf 'TEMP|%s|%s\n' "${MOCK_DB_TEMP_RO:-f}" "${MOCK_DB_TEMP_PUB:-f}"
        ;;
    TABLE_PRIV) [[ -n "${MOCK_TABLE_PRIV:-}" ]] && printf '%s\n' "${MOCK_TABLE_PRIV}" ;;
    SEQ_PRIV) [[ -n "${MOCK_SEQ_PRIV:-}" ]] && printf '%s\n' "${MOCK_SEQ_PRIV}" ;;
    OUT_USAGE) [[ -n "${MOCK_OUT_USAGE:-}" ]] && printf '%s\n' "${MOCK_OUT_USAGE}" ;;
    OUT_SELECT) [[ -n "${MOCK_OUT_SELECT:-}" ]] && printf '%s\n' "${MOCK_OUT_SELECT}" ;;
    OWNERSHIP) [[ -n "${MOCK_OWNERSHIP:-}" ]] && printf '%s\n' "${MOCK_OWNERSHIP}" ;;
    ESCAPE) [[ -n "${MOCK_ESCAPE:-}" ]] && printf '%s\n' "${MOCK_ESCAPE}" ;;
    SECDEF) [[ -n "${MOCK_SECDEF:-}" ]] && printf '%s\n' "${MOCK_SECDEF}" ;;
    DEFACL) [[ -n "${MOCK_DEFACL:-}" ]] && printf '%s\n' "${MOCK_DEFACL}" ;;
    CLUSTER_CONNECT) [[ -n "${MOCK_CLUSTER_CONNECT:-}" ]] && printf '%s\n' "${MOCK_CLUSTER_CONNECT}" ;;
    OWNER_GAP) [[ -n "${MOCK_OWNER_GAP:-}" ]] && printf '%s\n' "${MOCK_OWNER_GAP}" ;;
esac
exit 0
"""

MOCK_RO_SESSION = r"""#!/bin/bash
# Stand-in for shared/ro-session.sh, substituted via RO_SESSION_SH_OVERRIDE.
# Records that it was invoked (for assertions) and exits with MOCK_PROBE_RC.
if [[ -n "${DBLLM_TEST_PROBE_MARKER:-}" ]]; then
    printf '%s\n' "$@" > "${DBLLM_TEST_PROBE_MARKER}"
fi
exit "${MOCK_PROBE_RC:-0}"
"""


@pytest.fixture()
def project(tmp_path: Path):
    """Throwaway consuming-project layout: single .dbllm.env + a mock psql
    shim + a mock ro-session.sh stub."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dbmeta").mkdir()

    (root / ".dbmeta" / ".dbllm.env").write_text(
        "SCHEMAS=public\n"
        f"DB_HOST=127.0.0.1\nDB_PORT=6432\nDB_NAME=appdb\nDB_USER=llm_readonly\nDB_PASSWORD={MOCK_PASSWORD}\n"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mock_psql_path = bin_dir / "psql"
    mock_psql_path.write_text(MOCK_PSQL)
    mock_psql_path.chmod(mock_psql_path.stat().st_mode | stat.S_IEXEC)

    mock_ro_session_path = tmp_path / "mock-ro-session.sh"
    mock_ro_session_path.write_text(MOCK_RO_SESSION)
    mock_ro_session_path.chmod(mock_ro_session_path.stat().st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ROOT_DIR"] = str(root)
    env["RO_SESSION_SH_OVERRIDE"] = str(mock_ro_session_path)
    return root, env


def _run_verify(root: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(RO_VERIFY_SH)], cwd=root, env=env, capture_output=True, text=True, timeout=30
    )


def _needs_human(root: Path) -> Path:
    return root / ".dbmeta" / "db-readonly" / "needs-human.md"


class TestCleanRoleAllGreen:
    def test_clean_role_reaches_probe_and_exit0(self, project):
        root, env = project
        result = _run_verify(root, env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "六面审计通过" in result.stderr
        assert "只读通道可用" in result.stderr
        assert not _needs_human(root).exists()

    def test_probe_actually_invoked_with_expected_args(self, project):
        root, env = project
        marker = root / "probe-marker.txt"
        env = dict(env)
        env["DBLLM_TEST_PROBE_MARKER"] = str(marker)
        result = _run_verify(root, env)
        assert result.returncode == 0
        assert marker.exists()
        assert marker.read_text().strip() == "--sql\nSELECT 1"


class TestPublicWritePrivilegeDetected:
    def test_public_insert_on_table_is_fail_closed(self, project):
        root, env = project
        env = dict(env)
        # tbl | ro:INS,UPD,DEL,TRUNC,REF,TRIG | pub:INS,UPD,DEL,TRUNC,REF,TRIG
        env["MOCK_TABLE_PRIV"] = "auth.users|f|f|f|f|f|f|t|f|f|f|f|f"
        result = _run_verify(root, env)
        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "只读通道可用" not in result.stdout + result.stderr
        needs_human = _needs_human(root)
        assert needs_human.exists()
        content = needs_human.read_text()
        assert "REVOKE INSERT ON auth.users FROM PUBLIC;" in content
        assert MOCK_PASSWORD not in content

    def test_role_direct_write_grant_is_also_fail_closed(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_TABLE_PRIV"] = "auth.users|t|f|f|f|f|f|f|f|f|f|f|f"
        result = _run_verify(root, env)
        assert result.returncode == 2
        content = _needs_human(root).read_text()
        assert "REVOKE INSERT ON auth.users FROM \"llm_readonly\";" in content


class TestSecurityDefinerScopeSplit:
    def test_out_of_scope_definer_fail_closed(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_SECDEF"] = "admin.leak()|admin"
        result = _run_verify(root, env)
        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        content = _needs_human(root).read_text()
        assert "admin.leak" in content
        assert "REVOKE EXECUTE" in content
        assert "FROM PUBLIC" in content

    def test_in_scope_definer_report_only(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_SECDEF"] = "public.leak()|public"
        result = _run_verify(root, env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert not _needs_human(root).exists()
        assert "public.leak" in result.stderr
        assert "仅报告" in result.stderr


class TestOutOfScopeDefaultAclDrift:
    def test_out_of_scope_default_acl_fail_closed(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_DEFACL"] = "admin|dba_owner|r"
        result = _run_verify(root, env)
        assert result.returncode == 2
        content = _needs_human(root).read_text()
        assert "ALTER DEFAULT PRIVILEGES" in content
        assert "admin" in content

    def test_in_scope_default_acl_report_only(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_DEFACL"] = "public|dba_owner|r"
        result = _run_verify(root, env)
        assert result.returncode == 0
        assert not _needs_human(root).exists()
        assert "dba_owner" in result.stderr


class TestAlwaysReportOnlyFindings:
    def test_cluster_connect_never_blocks(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_CLUSTER_CONNECT"] = "other_db"
        result = _run_verify(root, env)
        assert result.returncode == 0
        assert not _needs_human(root).exists()
        assert "other_db" in result.stderr

    def test_owner_gap_never_blocks(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_OWNER_GAP"] = "public|some_other_owner"
        result = _run_verify(root, env)
        assert result.returncode == 0
        assert not _needs_human(root).exists()
        assert "some_other_owner" in result.stderr


class TestRoleFlagsAndMembership:
    def test_superuser_flag_fail_closed(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_SUPERUSER"] = "t"
        result = _run_verify(root, env)
        assert result.returncode == 2
        content = _needs_human(root).read_text()
        assert "ALTER ROLE \"llm_readonly\" NOSUPERUSER;" in content

    def test_membership_fail_closed(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_MEMBERSHIP"] = "app_rw"
        result = _run_verify(root, env)
        assert result.returncode == 2
        content = _needs_human(root).read_text()
        assert 'REVOKE "app_rw" FROM "llm_readonly";' in content


class TestProbeFailureExitCode:
    def test_probe_failure_is_exit1_not_exit2(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        result = _run_verify(root, env)
        assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
        # Mechanically distinct from an audit hit — no needs-human.md written
        # by ro-verify.sh itself (that's exit 2's job only).
        assert not _needs_human(root).exists()
        assert "只读通道可用" not in result.stdout + result.stderr

    def test_probe_failure_stdout_points_to_userlist_fragment(self, project):
        """REQ-RP-3: on probe failure, stdout MUST point at the
        userlist-fragment.txt path (not merely mention it on stderr via the
        human-readable fail() hint). Capture stdout and stderr separately so
        this cannot pass on a stderr-only mention."""
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        result = _run_verify(root, env)
        assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
        expected_path = str(root / ".dbmeta" / "db-readonly" / "userlist-fragment.txt")
        stdout_lines = result.stdout.splitlines()
        assert any(line == expected_path for line in stdout_lines), (
            f"expected a line exactly equal to {expected_path!r} in stdout, "
            f"got stdout={result.stdout!r}"
        )
        assert MOCK_PASSWORD not in result.stdout
        assert MOCK_PASSWORD not in result.stderr

    def test_probe_failure_stderr_gives_discriminator_command_and_branches(self, project):
        """Task 2 (tasks.md 2.4/2.6): probe-failure stderr must lead with the
        discriminator command, branch on auth_query/auth_file, and give a
        third sentence for the undeterminable case -- regardless of whether
        userlist-fragment.txt happens to exist."""
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        result = _run_verify(root, env)
        assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "grep -E '^(auth_type|auth_file|auth_query)'" in result.stderr
        assert "auth_query" in result.stderr
        assert "auth_file" in result.stderr
        expected_path = str(root / ".dbmeta" / "db-readonly" / "userlist-fragment.txt")
        assert expected_path in result.stderr
        assert "确认生效的认证后端" in result.stderr


class TestStatementTimeoutOverride:
    def test_every_audit_session_sets_statement_timeout(self, project):
        root, env = project
        dump = root / "stdin-dump.txt"
        env = dict(env)
        env["DBLLM_TEST_STDIN_DUMP"] = str(dump)
        env["DBLLM_TEST_STDIN_DUMP_QUERY"] = "ROLE_FLAGS"
        result = _run_verify(root, env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert dump.exists()
        content = dump.read_text()
        assert content.strip().startswith("SET statement_timeout = '60s';")


class TestOnlyReadOnlyCredentialsUsed:
    def test_every_connection_uses_ro_credentials(self, project):
        root, env = project
        dump = root / "cred-dump.txt"
        env = dict(env)
        env["DBLLM_TEST_CRED_DUMP"] = str(dump)
        result = _run_verify(root, env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert dump.exists()
        lines = [line for line in dump.read_text().splitlines() if line.startswith("PGUSER=")]
        assert lines, "expected at least one audit connection"
        assert all(line == "PGUSER=llm_readonly" for line in lines)


class TestNeedsHumanStdoutPath:
    def test_fail_closed_exit2_stdout_points_to_needs_human(self, project):
        """Same stdout-path convention as readonly-setup.sh's
        NEEDS_HUMAN_FILE printf — Task 4's orchestrator relies on stdout
        (not stderr) to pick up the path. Capture stdout/stderr separately."""
        root, env = project
        env = dict(env)
        env["MOCK_SUPERUSER"] = "t"
        result = _run_verify(root, env)
        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        expected_path = str(root / ".dbmeta" / "db-readonly" / "needs-human.md")
        stdout_lines = result.stdout.splitlines()
        assert any(line == expected_path for line in stdout_lines), (
            f"expected a line exactly equal to {expected_path!r} in stdout, "
            f"got stdout={result.stdout!r}"
        )
        assert MOCK_PASSWORD not in result.stdout
        assert MOCK_PASSWORD not in result.stderr


class TestAuditConnectionFailurePropagates:
    def test_admin_sql_style_failure_is_exit1(self, project):
        """If the RO connection itself can't run a query (e.g. role not yet
        provisioned by the DBA), ro-verify.sh must fail loud with exit 1 —
        same bucket as probe failure, MUST NOT be exit 2 (that's reserved for
        an actual audit finding)."""
        root, env = project
        env = dict(env)
        env["DBLLM_TEST_FAIL_QUERY"] = "ROLE_FLAGS"
        result = _run_verify(root, env)
        assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert not _needs_human(root).exists()


# --- [impl-review-fix] regressions for the code-review round -----------------


class TestAuditedIdentityIsConnectedIdentity:
    """Face 0: every has_*_privilege(:'ro_role', ...) face audits the RO_ROLE
    string from .dbllm.env, which is NOT sourced from this connection. If
    it drifts from the user the connection actually authenticated as, the six
    faces audit some other role and report on an identity nobody is using."""

    def test_current_user_mismatch_is_fail_closed(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_CURRENT_USER"] = "some_other_role"
        result = _run_verify(root, env)
        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert _needs_human(root).exists()
        body = _needs_human(root).read_text()
        assert "llm_readonly" in body
        assert "some_other_role" in body
        assert "六面审计通过" not in result.stderr
        assert MOCK_PASSWORD not in body

    def test_mismatch_stops_before_the_probe(self, project):
        root, env = project
        marker = root / "probe-marker.txt"
        env = dict(env)
        env["MOCK_CURRENT_USER"] = "some_other_role"
        env["DBLLM_TEST_PROBE_MARKER"] = str(marker)
        result = _run_verify(root, env)
        assert result.returncode == 2
        assert not marker.exists(), "probe MUST NOT run once identity is known bad"


class TestUnresolvableCredentialsHonourExit2Contract:
    def test_placeholder_credentials_writes_needs_human(self, project):
        """The header contract reserves exit 2 for 'fail-closed, and
        .dbmeta/db-readonly/needs-human.md explains it'. A credential-resolution
        failure (CHANGE_ME placeholders) returns 2 from db_llm_export_pg_env,
        so it MUST produce the file too rather than exiting 2 with nothing on disk."""
        root, env = project
        (root / ".dbmeta" / ".dbllm.env").write_text(
            "SCHEMAS=public\nDB_HOST=CHANGE_ME\nDB_PORT=5432\nDB_NAME=CHANGE_ME\n"
            "DB_USER=llm_readonly\nDB_PASSWORD=CHANGE_ME\n"
        )
        result = _run_verify(root, env)
        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert _needs_human(root).exists()
        assert ".dbllm.env" in _needs_human(root).read_text()


class TestRemediationSqlTargetsTheActualGrantee:
    """The escape / out-of-scope-SECURITY-DEFINER predicates fire on
    has_function_privilege(RO_ROLE, ...), which a grant made directly TO the
    role satisfies just as much as one to PUBLIC. A 'FROM PUBLIC'-only fix
    would leave that direct grant in place and verify would never converge."""

    def test_escape_function_fix_revokes_from_role_too(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_ESCAPE"] = "function:public.dblink_exec(text)"
        result = _run_verify(root, env)
        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        body = _needs_human(root).read_text()
        assert 'REVOKE EXECUTE ON FUNCTION public.dblink_exec(text) FROM PUBLIC, "llm_readonly";' in body

    def test_fdw_server_fix_revokes_from_role_too(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_ESCAPE"] = "fdw_server:remote_srv"
        result = _run_verify(root, env)
        assert result.returncode == 2
        body = _needs_human(root).read_text()
        assert 'REVOKE USAGE ON FOREIGN SERVER "remote_srv" FROM PUBLIC, "llm_readonly";' in body

    def test_out_of_scope_definer_fix_revokes_from_role_too(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_SECDEF"] = "billing.escalate()|billing"
        result = _run_verify(root, env)
        assert result.returncode == 2
        body = _needs_human(root).read_text()
        assert 'REVOKE EXECUTE ON FUNCTION billing.escalate() FROM PUBLIC, "llm_readonly";' in body


class TestFunctionSignaturesComeFromRegprocedure:
    """Object names must reach the DBA-executed REVOKE statements through
    PostgreSQL's own quoting (`oid::regprocedure`), never raw
    `nspname || '.' || proname` concatenation."""

    def _dump_sql(self, root, env, query_id):
        dump = root / f"{query_id.lower()}-sql.txt"
        env = dict(env)
        env["DBLLM_TEST_STDIN_DUMP"] = str(dump)
        env["DBLLM_TEST_STDIN_DUMP_QUERY"] = query_id
        _run_verify(root, env)
        assert dump.exists(), f"{query_id} query never ran"
        return dump.read_text()

    def test_escape_query_uses_regprocedure(self, project):
        root, env = project
        sql = self._dump_sql(root, env, "ESCAPE")
        assert "oid::regprocedure::text" in sql
        assert "p.proname || '('" not in sql

    def test_secdef_query_uses_regprocedure(self, project):
        root, env = project
        sql = self._dump_sql(root, env, "SECDEF")
        assert "oid::regprocedure::text" in sql
        assert "p.proname || '('" not in sql


class TestOwnerGapAdviceIsActionable:
    def test_owner_gap_names_the_statement_the_owner_must_run(self, project):
        """`ALTER DEFAULT PRIVILEGES FOR ROLE current_user` in the generated
        setup.sql only ever registers the executing DBA's own future objects,
        so 'rerun the provisioning script' can never close another owner's
        gap — the advice MUST name that owner's own statement instead."""
        root, env = project
        env = dict(env)
        env["MOCK_OWNER_GAP"] = "public|appuser"
        result = _run_verify(root, env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "或重跑供给" not in result.stderr
        assert (
            'ALTER DEFAULT PRIVILEGES FOR ROLE "appuser" IN SCHEMA "public" '
            'GRANT SELECT ON TABLES TO "llm_readonly";' in result.stderr
        )


class TestPgFactoryDefaultsAreReportOnly:
    """PG 建库/建 schema 时把 TEMPORARY 与 public 的 USAGE 授给 PUBLIC（datacl 的 `=Tc`、
    nspacl 的 `=U`）。对这两项 fail-closed 会在**任何默认配置的集群**上假阳，而唯一整改
    手段只能 `FROM PUBLIC`、影响该库所有用户——PG 没有针对单角色的反向撤销。故降 report-only，
    与附加检查项 (b) 的 cluster CONNECT 同档同因。"""

    def test_db_temp_never_blocks(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_DB_TEMP_RO"] = "t"
        env["MOCK_DB_TEMP_PUB"] = "t"
        result = _run_verify(root, env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert not _needs_human(root).exists()
        assert "可建临时表" in result.stderr
        assert "仅报告，不阻断" in result.stderr

    def test_db_create_still_fail_closed(self, project):
        """同一面里 CREATE 不受影响——它不是 PG 出厂缺省，是真实越权。"""
        root, env = project
        env = dict(env)
        env["MOCK_DB_CREATE_RO"] = "t"
        result = _run_verify(root, env)
        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "上角色持有 CREATE" in _needs_human(root).read_text()

    def test_out_of_scope_public_schema_usage_never_blocks(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_OUT_USAGE"] = "public|t|t"
        result = _run_verify(root, env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert not _needs_human(root).exists()
        assert "范围外 schema public 上有 USAGE" in result.stderr

    def test_out_of_scope_business_schema_usage_still_fail_closed(self, project):
        """只有 `public` 是缺省豁免；别的范围外 schema 有 USAGE 仍是真实残留。"""
        root, env = project
        env = dict(env)
        env["MOCK_OUT_USAGE"] = "billing|t|f"
        result = _run_verify(root, env)
        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "范围外 schema billing 上角色仍持有 USAGE" in _needs_human(root).read_text()
