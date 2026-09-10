"""Mock tests for pg-readonly-setup/scripts/readonly-setup.sh's five-step
orchestration (ro-only-credential-architecture Task 4, tasks.md 4.1/4.3,
specs/ro-provision/spec.md REQ-RP-4).

No real Postgres and no real shared/ro-generate.sh / shared/ro-verify.sh
needed: both are substituted via RO_GENERATE_SH_OVERRIDE / RO_VERIFY_SH_OVERRIDE
(the same testability seam pattern the P1 script established for
RO_PROVISION_SH_OVERRIDE / RO_SESSION_SH_OVERRIDE) with tiny stub scripts that
just exit with a controlled code. Step ③'s direct role-readiness probe is
substituted via RO_PROBE_PSQL_OVERRIDE with a mock `psql` binary that exits
with a controlled code and stderr text (mirroring PG's actual FATAL message
shapes for "role does not exist" vs "password authentication failed").

This isolates "did the orchestration stop at the right step, with the right
exit code and the right needs-human.md content" from "does ro-generate.sh /
ro-verify.sh actually work" (covered separately by
tests/test_ro_generate_shell.py and tests/test_ro_verify_shell.py).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
READONLY_SETUP_SH = REPO_ROOT / "pg-readonly-setup" / "scripts" / "readonly-setup.sh"
CONFIG_TEMPLATE = REPO_ROOT / "shared" / ".dbllm.env.example"

MOCK_STUB = r"""#!/bin/bash
# Generic stand-in for shared/ro-generate.sh or shared/ro-verify.sh. Exits
# with $MOCK_RC (default 0) and prints $MOCK_STDERR_MSG to stderr if set.
[[ -n "${MOCK_STDERR_MSG:-}" ]] && echo "${MOCK_STDERR_MSG}" >&2
exit "${MOCK_RC:-0}"
"""

MOCK_PROBE_PSQL = r"""#!/bin/bash
# Stand-in for the real `psql` binary used by readonly-setup.sh's own
# step ③ direct role-readiness probe (`psql -c 'SELECT 1'`). Exits with
# $MOCK_PROBE_RC (default 0) and prints $MOCK_PROBE_STDERR to stderr.
[[ -n "${MOCK_PROBE_STDERR:-}" ]] && printf '%s\n' "${MOCK_PROBE_STDERR}" >&2
exit "${MOCK_PROBE_RC:-0}"
"""


def _write_mock(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture()
def project(tmp_path: Path):
    """Throwaway consuming-project layout with a fully-usable .dbllm.env
    (host/port/db already filled — the "past step ③'s placeholder check"
    state most tests start from), plus mock generate/verify/psql-probe stubs."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dbmeta").mkdir()

    (root / ".dbmeta" / ".dbllm.env").write_text(
        "SCHEMAS=public\n"
        "DB_HOST=127.0.0.1\nDB_PORT=6432\nDB_NAME=appdb\n"
        "DB_USER=llm_readonly\nDB_PASSWORD=real-password\n"
    )

    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    mock_generate = _write_mock(stub_dir / "ro-generate.sh", MOCK_STUB)
    mock_verify = _write_mock(stub_dir / "ro-verify.sh", MOCK_STUB)
    mock_probe_psql = _write_mock(stub_dir / "psql", MOCK_PROBE_PSQL)

    env = dict(os.environ)
    env["ROOT_DIR"] = str(root)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env["RO_GENERATE_SH_OVERRIDE"] = str(mock_generate)
    env["RO_VERIFY_SH_OVERRIDE"] = str(mock_verify)
    env["RO_PROBE_PSQL_OVERRIDE"] = str(mock_probe_psql)
    return root, env


def _run(env: dict, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(READONLY_SETUP_SH)], cwd=root, env=env, capture_output=True, text=True, timeout=30
    )


def _needs_human(root: Path) -> Path:
    return root / ".dbmeta" / "db-readonly" / "needs-human.md"


# =============================================================================
# ① config
# =============================================================================


class TestConfigMissingScaffolds:
    def test_missing_config_scaffolds_from_template_and_exits1(self, tmp_path: Path):
        root = tmp_path / "fresh_project"
        root.mkdir()
        env = dict(os.environ)
        env["ROOT_DIR"] = str(root)
        env.pop("CLAUDE_PROJECT_DIR", None)

        result = _run(env, root)

        assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
        cfg = root / ".dbmeta" / ".dbllm.env"
        assert cfg.is_file(), "应从模版自举出配置文件"
        assert cfg.read_text() == CONFIG_TEMPLATE.read_text()
        assert not _needs_human(root).exists()


class TestPlaceholderCredentialsExitsTwo:
    def test_placeholder_credentials_writes_needs_human_exit2(self, project):
        root, env = project
        (root / ".dbmeta" / ".dbllm.env").write_text(
            "SCHEMAS=public\nDB_HOST=CHANGE_ME\nDB_PORT=5432\nDB_NAME=CHANGE_ME\n"
            "DB_USER=llm_readonly\nDB_PASSWORD=CHANGE_ME\n"
        )

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        nh = _needs_human(root)
        assert nh.exists()
        content = nh.read_text()
        assert "DB_HOST" in content or "占位值" in content
        assert result.stdout.strip() == str(nh)


class TestLegacyEnvFileStaysExit1NoNeedsHuman:
    def test_legacy_env_file_key_is_ignored(self, project):
        """Legacy ENV_FILE keys are simply ignored as unknown keys in the new
        architecture. This test just verifies the config still loads."""
        root, env = project
        (root / ".dbmeta" / ".dbllm.env").write_text(
            "SCHEMAS=public\nENV_FILE=hack/.env\n"
            "DB_HOST=127.0.0.1\nDB_PORT=6432\nDB_NAME=appdb\n"
            "DB_USER=llm_readonly\nDB_PASSWORD=real-password\n"
        )

        result = _run(env, root)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


# =============================================================================
# ② generate
# =============================================================================


class TestGenerateFailurePropagates:
    def test_generate_exit1_propagates_as_is(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_RC"] = "1"
        env["MOCK_STDERR_MSG"] = "mock generate: simulated failure"

        result = _run(env, root)

        assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "mock generate: simulated failure" in result.stderr
        assert not _needs_human(root).exists()


# =============================================================================
# ③ role readiness probe
# =============================================================================


class TestPlaceholderHostBlocksBeforeProbe:
    def test_change_me_host_writes_needs_human_exit2_without_probing(self, project):
        root, env = project
        (root / ".dbmeta" / ".dbllm.env").write_text(
            "SCHEMAS=public\nDB_HOST=CHANGE_ME\nDB_PORT=CHANGE_ME\nDB_NAME=CHANGE_ME\n"
            "DB_USER=llm_readonly\nDB_PASSWORD=real-password\n"
        )
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"  # would fail if actually invoked

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        nh = _needs_human(root)
        assert nh.exists()
        content = nh.read_text()
        assert "DB_HOST" in content or "占位值" in content


class TestRoleNotYetCreatedBranchA:
    def test_role_does_not_exist_gives_dba_execute_guidance(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        env["MOCK_PROBE_STDERR"] = 'psql: error: FATAL:  role "llm_readonly" does not exist'

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        content = _needs_human(root).read_text()
        assert "尚未在数据库中创建" in content
        assert "psql -f .dbmeta/db-readonly/setup.sql" in content
        assert "DBA" in content

    def test_no_such_user_gives_dba_execute_guidance(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        env["MOCK_PROBE_STDERR"] = "psql: error: connection to server at \"h\" failed: FATAL:  no such user"

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        content = _needs_human(root).read_text()
        assert "尚未在数据库中创建" in content
        assert "setup.sql" in content


class TestDatabaseDoesNotExistFallsToCatchAll:
    def test_database_does_not_exist_falls_to_catch_all(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        env["MOCK_PROBE_STDERR"] = 'psql: error: FATAL:  database "typo_db" does not exist'

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        content = _needs_human(root).read_text()
        assert "核对" in content
        assert "DB_NAME" in content
        assert "尚未在数据库中创建" not in content


class TestPasswordDriftBranchB:
    def test_password_auth_failed_gives_alter_role_guidance(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        env["MOCK_PROBE_STDERR"] = 'psql: error: FATAL:  password authentication failed for user "llm_readonly"'

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        content = _needs_human(root).read_text()
        assert "认证被拒" in content
        assert "ALTER ROLE" in content
        assert "确认独占再执行" in content  # shared-rotation warning
        assert "real-password" not in content  # MUST NOT leak the plaintext password

    def test_sasl_auth_failed_gives_four_way_guidance(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        env["MOCK_PROBE_STDERR"] = (
            'psql: error: connection to server at "h", port 6432 failed: '
            "FATAL:  SASL authentication failed"
        )

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        content = _needs_human(root).read_text()
        assert "认证被拒" in content
        assert "pg_roles" in content
        assert "setup.sql" in content
        assert "DB_PASSWORD" in content
        assert "DB_USER" in content
        assert "ALTER ROLE" in content
        assert "userlist-fragment.txt" in content
        assert content.index("ALTER ROLE") > content.index("DB_USER")
        assert "核对" not in content
        assert "网络不可达" not in content


class TestGenericConnectionFailureFallback:
    def test_unrecognized_probe_failure_still_exit2_generic_guidance(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_PROBE_RC"] = "1"
        env["MOCK_PROBE_STDERR"] = "psql: error: could not connect to server: Connection refused"

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        content = _needs_human(root).read_text()
        assert "非「角色不存在」或「认证被拒」" in content


# =============================================================================
# ④ verify
# =============================================================================


class TestVerifyAuditFindingPropagatesExit2:
    def test_verify_exit2_propagates_without_overwriting_its_own_needs_human(self, project):
        root, env = project
        env = dict(env)
        env["MOCK_RC"] = "0"  # applies to generate; overridden per-script below

        # Give generate and verify DIFFERENT exit codes via two distinct stub
        # scripts (MOCK_STUB keys off a single MOCK_RC, so verify needs its
        # own stub instance with its own env var name).
        verify_stub = Path(env["RO_VERIFY_SH_OVERRIDE"])
        verify_stub.write_text(
            "#!/bin/bash\n"
            "mkdir -p .dbmeta/db-readonly\n"
            "printf 'verify findings\\n' > .dbmeta/db-readonly/needs-human.md\n"
            "printf '%s\\n' .dbmeta/db-readonly/needs-human.md\n"
            "exit 2\n"
        )
        verify_stub.chmod(verify_stub.stat().st_mode | stat.S_IEXEC)

        result = _run(env, root)

        assert result.returncode == 2, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert _needs_human(root).read_text() == "verify findings\n"


class TestVerifyProbeFailurePropagatesExit1:
    def test_verify_exit1_propagates(self, project):
        root, env = project
        env = dict(env)
        verify_stub = Path(env["RO_VERIFY_SH_OVERRIDE"])
        verify_stub.write_text("#!/bin/bash\necho 'verify probe failed' >&2\nexit 1\n")
        verify_stub.chmod(verify_stub.stat().st_mode | stat.S_IEXEC)

        result = _run(env, root)

        assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "verify probe failed" in result.stderr


# =============================================================================
# ⑤ done
# =============================================================================


class TestAllStepsGreenExitZero:
    def test_full_happy_path_exit0(self, project):
        root, env = project

        result = _run(env, root)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "只读通道可用" in result.stdout + result.stderr
        assert not _needs_human(root).exists()
