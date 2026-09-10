"""Offline (zero-DB, zero-network) tests for pg-readonly-setup/scripts/preflight.sh.

Every test drives the real script against a throwaway consuming-project
layout in tmp_path and asserts on its stdout JSON and exit code. No real
Postgres, no mock psql needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT_SH = REPO_ROOT / "pg-readonly-setup" / "scripts" / "preflight.sh"

REAL_PASSWORD_MARKER = "z9REAL7SECRET1PASSWORD5MARKER3"


def _config_lines(**overrides: str | None) -> str:
    fields: dict[str, str | None] = {
        "SCHEMAS": "public",
        "DB_HOST": "CHANGE_ME",
        "DB_PORT": "5432",
        "DB_NAME": "CHANGE_ME",
        "DB_USER": "llm_readonly",
        "DB_PASSWORD": "CHANGE_ME",
    }
    fields.update(overrides)
    # A None override omits the line entirely — the T74 target shape for tunnel
    # mode, where DB_HOST/DB_PORT are derived rather than supplied.
    return "\n".join(f"{k}={v}" for k, v in fields.items() if v is not None) + "\n"


@pytest.fixture()
def project(tmp_path: Path):
    """A throwaway git-initialized consuming-project layout with a .gitignore
    that covers .dbmeta/db-readonly/ and .dbmeta/.dbllm.env. Returns (root, env)."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dbmeta").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text(".dbmeta/.dbllm.env\n.dbmeta/db-readonly/\n")

    env = dict(os.environ)
    env["ROOT_DIR"] = str(root)
    env.pop("CLAUDE_PROJECT_DIR", None)
    return root, env


def _run(env: dict, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PREFLIGHT_SH)], env=env, capture_output=True, text=True, timeout=timeout
    )


def _write_config(root: Path, **overrides: str) -> None:
    (root / ".dbmeta" / ".dbllm.env").write_text(_config_lines(**overrides))


def _minimal_path_without(*omit: str) -> str:
    bin_dir = Path(
        subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip()
    )
    tools = [
        "bash", "sh", "git", "sed", "awk", "grep", "cut", "cat", "mkdir", "rm",
        "mv", "cp", "chmod", "mktemp", "tr", "head", "dirname", "basename",
        "find", "xargs", "readlink", "stat", "env", "printf", "python3", "wc",
        "psql",
    ]
    for tool in tools:
        if tool in omit:
            continue
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    return str(bin_dir)


# =============================================================================
# Fresh repo
# =============================================================================


def test_fresh_repo_exit0_stage_config_no_blockers(project):
    root, env = project

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["stage"] == "config"
    assert data["blockers"] == []
    assert data["checks"]["config_file"]["status"] == "skip"
    assert data["checks"]["git_repo"]["status"] == "ok"
    assert data["checks"]["gitignore_setup_sql"]["status"] == "ok"
    assert data["checks"]["gitignore_config"]["status"] == "ok"


def test_output_is_a_single_valid_json_object(project):
    root, env = project

    result = _run(env)

    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    assert set(data.keys()) == {"checks", "stage", "blockers"}


# =============================================================================
# Dependency missing
# =============================================================================


def test_missing_psql_still_exit0_one_pass_full_picture(project):
    root, env = project
    env = dict(env)
    env["PATH"] = _minimal_path_without("psql")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["dep_psql"]["status"] == "fail"
    assert "dep_psql" in data["blockers"]
    assert data["stage"] == "deps"
    assert data["checks"]["dep_python3"]["status"] == "ok"
    assert data["checks"]["git_repo"]["status"] == "ok"
    assert data["checks"]["config_file"]["status"] == "skip"


def test_missing_python3_still_exit0(project):
    root, env = project
    env = dict(env)
    env["PATH"] = _minimal_path_without("python3")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["dep_python3"]["status"] == "fail"
    assert "dep_python3" in data["blockers"]
    assert data["stage"] == "deps"


# =============================================================================
# Non-git repo
# =============================================================================


def test_non_git_repo_fails_and_blocks(tmp_path: Path):
    root = tmp_path / "not_a_repo"
    root.mkdir()
    env = dict(os.environ)
    env["ROOT_DIR"] = str(root)
    env.pop("CLAUDE_PROJECT_DIR", None)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["git_repo"]["status"] == "fail"
    assert "git_repo" in data["blockers"]
    assert data["stage"] == "gitignore"


# =============================================================================
# Config file with placeholder fields -> stage config
# =============================================================================


def test_config_with_placeholders_stage_config(project):
    root, env = project
    _write_config(root)  # defaults have CHANGE_ME for host/db/password

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["config_file"]["status"] == "ok"
    assert data["checks"]["config_fields"]["status"] == "ok"
    assert "host=placeholder" in data["checks"]["config_fields"]["detail"]
    assert "password=placeholder" in data["checks"]["config_fields"]["detail"]
    assert data["stage"] == "config"


# =============================================================================
# setup.sql in-flight
# =============================================================================


def test_setup_sql_inflight_detected_stage_provision(project):
    root, env = project
    _write_config(
        root,
        DB_HOST="127.0.0.1",
        DB_PORT="5432",
        DB_NAME="appdb",
        DB_PASSWORD="alreadyGeneratedRealPassword123",
    )
    build_dir = root / ".dbmeta" / "db-readonly"
    build_dir.mkdir(parents=True)
    (build_dir / "setup.sql").write_text("-- placeholder setup.sql\n")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["setup_sql_inflight"]["status"] == "ok"
    assert data["stage"] == "provision"


def test_setup_sql_absent_reports_skip(project):
    root, env = project
    _write_config(root)

    result = _run(env)

    data = json.loads(result.stdout)
    assert data["checks"]["setup_sql_inflight"]["status"] == "skip"


# =============================================================================
# Password zero-leak + three-state
# =============================================================================


def test_password_never_leaks_and_reports_set(project):
    root, env = project
    _write_config(
        root,
        DB_HOST="127.0.0.1",
        DB_PORT="5432",
        DB_NAME="appdb",
        DB_PASSWORD=REAL_PASSWORD_MARKER,
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert REAL_PASSWORD_MARKER not in result.stdout
    assert REAL_PASSWORD_MARKER not in result.stderr
    data = json.loads(result.stdout)
    assert "password=set" in data["checks"]["config_fields"]["detail"]
    for check in data["checks"].values():
        assert REAL_PASSWORD_MARKER not in check["detail"]


def test_placeholder_password_reports_placeholder_state(project):
    root, env = project
    _write_config(root, DB_HOST="127.0.0.1", DB_PORT="5432", DB_NAME="appdb")

    result = _run(env)

    data = json.loads(result.stdout)
    assert "password=placeholder" in data["checks"]["config_fields"]["detail"]
    assert data["stage"] == "provision"


def test_real_conn_placeholder_password_no_setup_sql_stage_provision(project):
    root, env = project
    _write_config(root, DB_HOST="127.0.0.1", DB_PORT="5432", DB_NAME="appdb")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["stage"] == "provision"
    assert data["blockers"] == []
    assert "password=placeholder" in data["checks"]["config_fields"]["detail"]


def test_empty_password_stage_config(project):
    root, env = project
    _write_config(root, DB_HOST="127.0.0.1", DB_PORT="5432", DB_NAME="appdb", DB_PASSWORD="")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["stage"] == "config"


def test_stale_setup_sql_detail_mentions_older_than_config(project):
    root, env = project
    _write_config(
        root,
        DB_HOST="127.0.0.1",
        DB_PORT="5432",
        DB_NAME="appdb",
        DB_PASSWORD="alreadyGeneratedRealPassword123",
    )
    config_path = root / ".dbmeta" / ".dbllm.env"
    build_dir = root / ".dbmeta" / "db-readonly"
    build_dir.mkdir(parents=True)
    setup_sql_path = build_dir / "setup.sql"
    setup_sql_path.write_text("-- placeholder setup.sql\n")

    # Explicitly spread mtimes >=2s apart: filesystem timestamp granularity
    # (often 1s) means sequential creation alone can yield equal mtimes,
    # which would make the `-ot` comparison silently false. os.utime pins
    # both times unambiguously instead of depending on wall-clock creation
    # order.
    now = __import__("time").time()
    os.utime(setup_sql_path, (now - 10, now - 10))
    os.utime(config_path, (now, now))

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["setup_sql_inflight"]["status"] == "ok"
    assert "早于当前配置" in data["checks"]["setup_sql_inflight"]["detail"]
    assert data["stage"] == "provision"


def test_absent_config_reports_skip_not_fail(project):
    root, env = project
    # Config file not created at all.

    result = _run(env)

    data = json.loads(result.stdout)
    assert data["checks"]["config_file"]["status"] == "skip"
    assert data["blockers"] == []
    assert data["stage"] == "config"


# =============================================================================
# gitignore predicate agreement (preflight and ro-generate use same check)
# =============================================================================


def test_gitignore_predicate_agrees_with_ro_generate_same_fixture(project):
    root, env = project
    (root / ".gitignore").write_text(".dbmeta/db-readonly/\n")  # config NOT covered
    _write_config(root)

    pf_result = _run(env)
    pf_data = json.loads(pf_result.stdout)
    assert pf_data["checks"]["gitignore_config"]["status"] == "fail"
    assert "gitignore_config" in pf_data["blockers"]

    ro_generate_env = dict(env)
    rog_result = subprocess.run(
        [str(REPO_ROOT / "shared" / "ro-generate.sh")],
        env=ro_generate_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rog_result.returncode != 0


def test_gitignore_predicate_agrees_when_covered(project):
    root, env = project
    _write_config(root)

    pf_result = _run(env)
    pf_data = json.loads(pf_result.stdout)
    assert pf_data["checks"]["gitignore_config"]["status"] == "ok"
    assert pf_data["checks"]["gitignore_setup_sql"]["status"] == "ok"

    rog_result = subprocess.run(
        [str(REPO_ROOT / "shared" / "ro-generate.sh")],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rog_result.returncode == 0, rog_result.stderr


# =============================================================================
# A2: wizard-created config file undergoes ro-generate password replacement
# =============================================================================


def test_a2_wizard_precreated_config_replaced_by_ro_generate(project):
    """The init wizard copies the template as .dbllm.env with CHANGE_ME
    password. After the user fills in host/port/db, ro-generate.sh's REPLACE
    branch must replace the password in-place."""
    root, env = project
    _write_config(
        root,
        DB_HOST="10.0.0.42",
        DB_PORT="5433",
        DB_NAME="appdb_prod",
        DB_USER="llm_readonly",
        DB_PASSWORD="CHANGE_ME",
    )
    config = root / ".dbmeta" / ".dbllm.env"

    result = subprocess.run(
        [str(REPO_ROOT / "shared" / "ro-generate.sh")],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "已替换" in result.stderr

    fields: dict[str, str] = {}
    for line in config.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            fields[k] = v

    assert fields["DB_HOST"] == "10.0.0.42"
    assert fields["DB_PORT"] == "5433"
    assert fields["DB_NAME"] == "appdb_prod"
    assert fields["DB_USER"] == "llm_readonly"
    assert fields["DB_PASSWORD"] != "CHANGE_ME"
    assert len(fields["DB_PASSWORD"]) >= 32
    assert fields["DB_PASSWORD"].isalnum()


# =============================================================================
# Zero-connection sanity
# =============================================================================


def test_runs_to_completion_quickly_no_hang(project):
    root, env = project
    result = _run(env, timeout=10)
    assert result.returncode == 0, result.stderr


# =============================================================================
# SSH tunnel preflight checks (REQ-IN-7): ssh_fields + dep_sshpass
# =============================================================================


def test_tunnel_not_configured_ssh_checks_skip_stage_unaffected(project):
    root, env = project
    _write_config(
        root, DB_HOST="127.0.0.1", DB_PORT="5432", DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890"
    )  # no SSH_* keys at all

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["ssh_fields"]["status"] == "skip"
    assert data["checks"]["dep_sshpass"]["status"] == "skip"
    assert "ssh_fields" not in data["blockers"]
    assert "dep_sshpass" not in data["blockers"]
    # tunnel disabled must not perturb the otherwise-fully-configured stage
    # (no .dbmeta/ schema dirs yet -> collect, same as without SSH_* at all).
    assert data["stage"] == "collect"


def test_password_mode_missing_sshpass_fails_blocking_stage_deps(project):
    root, env = project
    env = dict(env)
    env["PATH"] = _minimal_path_without("sshpass")
    _write_config(
        root,
        DB_HOST="localhost",
        DB_PORT="15432",
        DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890",
        SSH_HOST="jump.example.com",
        SSH_REMOTE_HOST="10.0.0.5",
        SSH_PASSWORD="jumppw",
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["dep_sshpass"]["status"] == "fail"
    assert "dep_sshpass" in data["blockers"]
    assert data["stage"] == "deps"
    # ssh_fields itself is otherwise fully consistent (fail is dep_sshpass's job).
    assert data["checks"]["ssh_fields"]["status"] == "ok"


def test_cert_mode_missing_sshpass_is_skip_not_blocking(project):
    root, env = project
    key_file = root / "id_rsa"
    key_file.write_text("fake-key-contents\n")
    env = dict(env)
    env["PATH"] = _minimal_path_without("sshpass")
    _write_config(
        root,
        DB_HOST="localhost",
        DB_PORT="15432",
        DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890",
        SSH_HOST="jump.example.com",
        SSH_REMOTE_HOST="10.0.0.5",
        SSH_KEY_FILE=str(key_file),
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["dep_sshpass"]["status"] == "skip"
    assert "dep_sshpass" not in data["blockers"]
    assert data["checks"]["ssh_fields"]["status"] == "ok"
    assert data["stage"] == "collect"


def test_tunnel_enabled_no_auth_blocks_stage_config(project):
    root, env = project
    _write_config(
        root,
        DB_HOST="localhost",
        DB_PORT="15432",
        DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890",
        SSH_HOST="jump.example.com",
        SSH_REMOTE_HOST="10.0.0.5",
        # neither SSH_KEY_FILE nor SSH_PASSWORD set
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["checks"]["ssh_fields"]["status"] == "fail"
    assert "ssh_auth=absent" in data["checks"]["ssh_fields"]["detail"]
    assert "ssh_fields" in data["blockers"]
    assert data["stage"] == "config"
    # not password mode -> dep_sshpass is not the blocker here.
    assert data["checks"]["dep_sshpass"]["status"] == "skip"


def test_tunnel_enabled_missing_remote_host_blocks_stage_config(project):
    root, env = project
    _write_config(
        root,
        DB_HOST="localhost",
        DB_PORT="15432",
        DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890",
        SSH_HOST="jump.example.com",
        SSH_PASSWORD="jumppw",
        # SSH_REMOTE_HOST omitted
    )

    result = _run(env)

    data = json.loads(result.stdout)
    assert data["checks"]["ssh_fields"]["status"] == "fail"
    assert "ssh_fields" in data["blockers"]
    assert data["stage"] == "config"


def test_tunnel_enabled_stale_db_host_and_port_are_not_blockers(project):
    """T74: preflight used to mirror config.sh's two cross-field validations
    (DB_HOST must be loopback, DB_PORT must equal SSH_LOCAL_PORT). Both fields
    are now derived from the tunnel and never read, so an existing project
    carrying stale values must not be blocked — otherwise preflight would stop
    a configuration config.sh itself is happy to run."""
    root, env = project
    _write_config(
        root,
        DB_HOST="10.0.0.5",   # not loopback
        DB_PORT="5432",       # disagrees with SSH_LOCAL_PORT
        DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890",
        SSH_HOST="jump.example.com",
        SSH_REMOTE_HOST="10.0.0.5",
        SSH_PASSWORD="jumppw",
    )

    result = _run(env)

    data = json.loads(result.stdout)
    assert data["checks"]["ssh_fields"]["status"] == "ok"
    assert "host_port_consistent" not in data["checks"]["ssh_fields"]["detail"]
    assert "ssh_fields" not in data["blockers"]
    assert data["stage"] != "config"


def test_tunnel_enabled_without_db_host_or_port_advances_past_config(project):
    """The target shape: tunnel mode need not carry DB_HOST/DB_PORT at all.
    Requiring them to be `set` would strand such a project at stage=config."""
    root, env = project
    _write_config(
        root,
        DB_HOST=None,
        DB_PORT=None,
        DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890",
        SSH_HOST="jump.example.com",
        SSH_REMOTE_HOST="10.0.0.5",
        SSH_PASSWORD="jumppw",
    )

    result = _run(env)

    data = json.loads(result.stdout)
    assert data["stage"] != "config", data["checks"]
    assert "ssh_fields" not in data["blockers"]


def test_direct_mode_without_db_host_or_port_stays_at_config(project):
    """The other half of the branch: with no tunnel there is nothing to derive
    from, so the address fields still gate stage=config."""
    root, env = project
    _write_config(
        root,
        DB_HOST=None,
        DB_PORT=None,
        DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890",
    )

    result = _run(env)

    data = json.loads(result.stdout)
    assert data["stage"] == "config"


def test_absent_config_ssh_checks_skip(project):
    root, env = project
    # .dbllm.env not created at all.

    result = _run(env)

    data = json.loads(result.stdout)
    assert data["checks"]["ssh_fields"]["status"] == "skip"
    assert data["checks"]["dep_sshpass"]["status"] == "skip"


def test_ssh_password_never_leaks(project):
    root, env = project
    _write_config(
        root,
        DB_HOST="localhost",
        DB_PORT="15432",
        DB_NAME="appdb",
        DB_PASSWORD="realpassword1234567890",
        SSH_HOST="jump.example.com",
        SSH_REMOTE_HOST="10.0.0.5",
        SSH_PASSWORD=REAL_PASSWORD_MARKER,
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert REAL_PASSWORD_MARKER not in result.stdout
    assert REAL_PASSWORD_MARKER not in result.stderr
    data = json.loads(result.stdout)
    for check in data["checks"].values():
        assert REAL_PASSWORD_MARKER not in check["detail"]
