"""Engine-level tests for shared/ssh-tunnel.sh's `ensure` subcommand (ssh-tunnel-auto
Task 1, tasks.md Task 1, design.md 「引擎 `ensure` 与 `start` 的复用方式」 /
「失败清理只信本次 spawn 的 pid」, specs/ssh-tunnel/spec.md REQ-ST-5 / REQ-ST-3).

No real SSH/network needed: a controllable mock `ssh` binary sits first on PATH.
It never connects to a real jump host. Selected via MOCK_SSH_MODE:
  - listen  : binds+listens on 127.0.0.1:$SSH_LOCAL_PORT and blocks forever,
              simulating a tunnel that came up successfully.
  - exit255 : exits immediately with status 255, simulating an auth/connect
              failure the real `ssh -N -L ... -o ExitOnForwardFailure=yes`
              would surface the same way.
  - hang    : sleeps without ever listening, simulating a stuck handshake
              (unreachable jump host / firewall black-hole).
Every invocation dumps its argv to MOCK_SSH_ARGV_DUMP for assertions.

This isolates "did shared/ssh-tunnel.sh's `ensure` compose the right spawn,
poll port_listening() correctly, and clean up only the pid it spawned" from
"does a real ssh -L actually tunnel traffic" — the latter has no automated
anchor in this repo (no real jump host in CI); a human verifies it once via
`bash shared/ssh-tunnel.sh start/status` against a real target.

Every SSH_LOCAL_PORT used here is dynamically allocated (tests run in parallel /
repeat) and every backgrounded process this test file itself starts (the
python "already listening" helper) is a pytest fixture: yields, then kills.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import stat
import subprocess
import shutil
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_TUNNEL_SH = REPO_ROOT / "shared" / "ssh-tunnel.sh"

MOCK_SSH = r"""#!/bin/bash
# Mock ssh for tests/test_ssh_tunnel_shell.py. Never connects to a real jump
# host. Records its argv, then behaves per MOCK_SSH_MODE (default: listen).
if [[ -n "${MOCK_SSH_ARGV_DUMP:-}" ]]; then
    printf '%s\n' "$@" > "${MOCK_SSH_ARGV_DUMP}"
fi

mode="${MOCK_SSH_MODE:-listen}"

case "${mode}" in
    exit255)
        exit 255
        ;;
    hang)
        exec sleep 300
        ;;
    listen)
        exec python3 -c '
import os, socket, time
port = int(os.environ["SSH_LOCAL_PORT"])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(1)
while True:
    time.sleep(3600)
'
        ;;
    *)
        echo "unknown MOCK_SSH_MODE=${mode}" >&2
        exit 1
        ;;
esac
"""

LISTENER_SNIPPET = """
import os, socket, time
port = int(os.environ["LISTEN_PORT"])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(1)
while True:
    time.sleep(3600)
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture()
def bin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    mock_ssh = d / "ssh"
    mock_ssh.write_text(MOCK_SSH)
    mock_ssh.chmod(mock_ssh.stat().st_mode | stat.S_IEXEC)
    return d


@pytest.fixture()
def base_env(bin_dir: Path, tmp_path: Path):
    key_file = tmp_path / "dummy_key"
    key_file.write_text("not-a-real-key")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SSH_HOST"] = "jump.example.invalid"
    env["SSH_REMOTE_HOST"] = "internal-db.example.invalid"
    env["SSH_KEY_FILE"] = str(key_file)
    env["MOCK_SSH_ARGV_DUMP"] = str(tmp_path / "ssh-argv.txt")
    return env


@pytest.fixture()
def real_listener(tmp_path: Path):
    """Starts a real (non-mock-ssh) process that binds+listens on a
    dynamically allocated port, simulating "the tunnel is already up" /
    "something else is squatting the port" — independent of the engine
    under test. Managed by this fixture: yields the port, kills on teardown.
    """
    procs: list[subprocess.Popen] = []

    def _start() -> int:
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-c", LISTENER_SNIPPET],
            env={**os.environ, "LISTEN_PORT": str(port)},
        )
        procs.append(proc)
        _wait_until(lambda: _port_open(port))
        return port

    yield _start

    for p in procs:
        p.kill()
        p.wait(timeout=5)


def _run(cmd: str, env: dict, port: int, timeout: float = 15.0, extra_env: dict | None = None):
    """Runs shared/ssh-tunnel.sh, redirecting stdout/stderr to real files rather
    than OS pipes. A successful `ensure` intentionally leaves a background
    tunnel process running past the wrapper's own exit (that's the point —
    the tunnel stays up); that process still inherits the wrapper's stdout/
    stderr fds. Reading through a pipe (subprocess's capture_output) would
    block waiting for EOF, i.e. for every fd holder to close it, which the
    backgrounded process never does. Redirecting to a file sidesteps that —
    exactly the capture shape design.md's db_llm_ensure_tunnel itself uses
    ("MUST 把引擎的 stdout+stderr 一并捕获到临时文件"), so this is also the
    more production-faithful way to invoke it, not merely a test workaround.
    """
    import tempfile

    full_env = dict(env)
    full_env["SSH_LOCAL_PORT"] = str(port)
    if extra_env:
        full_env.update(extra_env)
    with tempfile.TemporaryDirectory() as d:
        out_path = Path(d) / "stdout.txt"
        err_path = Path(d) / "stderr.txt"
        with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
            proc = subprocess.run(
                ["bash", str(SSH_TUNNEL_SH), cmd],
                env=full_env,
                stdout=out_f,
                stderr=err_f,
                timeout=timeout,
            )
        stdout = out_path.read_text()
        stderr = err_path.read_text()
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _kill_pid_from_stdout(stdout: str) -> None:
    m = re.search(r"PID (\d+)", stdout)
    if not m:
        return
    pid = int(m.group(1))
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class TestEnsureReuse:
    def test_ensure_reuses_listening_port_without_spawning(self, base_env, real_listener, tmp_path):
        port = real_listener()

        result = _run("ensure", base_env, port)

        assert result.returncode == 0
        assert result.stdout == "已就绪（复用）\n"
        assert not (tmp_path / "ssh-argv.txt").exists()

    def test_start_on_already_listening_port_is_unchanged(self, base_env, real_listener):
        port = real_listener()

        result = _run("start", base_env, port)

        assert result.returncode == 1
        expected = (
            f"警告：本地端口 {port} 已被占用，可能隧道已在运行\n"
            f"  使用 '{SSH_TUNNEL_SH} stop' 先停止旧隧道，或 '{SSH_TUNNEL_SH} status' 查看详情\n"
        )
        assert result.stdout == expected


class TestEnsureStart:
    def test_ensure_starts_tunnel_and_waits_for_port(self, base_env):
        port = _free_port()
        pid = None
        try:
            result = _run(
                "ensure",
                base_env,
                port,
                extra_env={"MOCK_SSH_MODE": "listen", "SSH_TUNNEL_ENSURE_TIMEOUT": "5"},
            )

            assert result.returncode == 0
            m = re.fullmatch(r"已启动 \(PID (\d+)\)\n", result.stdout)
            assert m, f"unexpected stdout: {result.stdout!r}"
            pid = int(m.group(1))
            assert _port_open(port)

            argv_dump = Path(base_env["MOCK_SSH_ARGV_DUMP"]).read_text()
            assert f"{port}:internal-db.example.invalid:{port}" in argv_dump
            assert "jump.example.invalid" in argv_dump

            pid_file = Path(f"/tmp/ssh-tunnel-{port}.pid")
            assert pid_file.read_text().strip() == str(pid)
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            Path(f"/tmp/ssh-tunnel-{port}.pid").unlink(missing_ok=True)


class TestEnsureFailureCleanup:
    def test_ensure_ssh_exits_immediately_reports_failure_no_pid_file(self, base_env):
        port = _free_port()

        result = _run(
            "ensure",
            base_env,
            port,
            extra_env={"MOCK_SSH_MODE": "exit255", "SSH_TUNNEL_ENSURE_TIMEOUT": "3"},
        )

        assert result.returncode == 1
        assert "[FAIL]" in result.stderr
        assert "problem:" in result.stderr
        assert "cause:" in result.stderr
        assert f"bash {SSH_TUNNEL_SH} status" in result.stderr
        assert not _port_open(port)
        assert not Path(f"/tmp/ssh-tunnel-{port}.pid").exists()

    def test_ensure_hang_times_out_and_kills_orphan(self, base_env):
        port = _free_port()

        started = time.monotonic()
        result = _run(
            "ensure",
            base_env,
            port,
            timeout=10,
            extra_env={"MOCK_SSH_MODE": "hang", "SSH_TUNNEL_ENSURE_TIMEOUT": "1"},
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 1
        assert elapsed <= 5, f"ensure took {elapsed}s, expected to bail out around the 1s timeout"
        assert "[FAIL]" in result.stderr
        assert not _port_open(port)
        assert not Path(f"/tmp/ssh-tunnel-{port}.pid").exists()

        # the mock ssh's argv was recorded, proving it really was spawned
        # (and therefore had to be killed, not merely never-started)
        assert Path(base_env["MOCK_SSH_ARGV_DUMP"]).exists()


# ═══════════════════════════════════════════════════════════════════════════
# Config-segment tests (Task 2) — SSH_* parsing, validation, redaction
# ═══════════════════════════════════════════════════════════════════════════

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG_SH = REPO_ROOT / "shared" / "config.sh"

# A scripted stand-in for shared/ssh-tunnel.sh's `ensure` subcommand (Task 1,
# not yet landed in this worktree). Records the env vars db_llm_ensure_
# tunnel is required to pass it (SSH_HOST/SSH_PORT/SSH_USER/SSH_LOCAL_PORT/
# SSH_REMOTE_HOST/SSH_REMOTE_PORT/SSH_KEY_FILE/SSH_PASSWORD) when
# DBLLM_TEST_ENGINE_ENV_DUMP is set, and writes a marker file
# (DBLLM_TEST_ENGINE_CALLED) so tests can assert the engine was (or was
# NOT) invoked at all. Behavior controlled by DBLLM_TEST_ENGINE_MODE:
#   success        -> stdout "已就绪（复用）", exit 0
#   fail_with_host -> stderr containing the literal SSH_HOST value, exit 1
#   fail_with_pw   -> stderr containing the literal SSH_PASSWORD value, exit 1
MOCK_ENGINE = r"""#!/bin/bash
if [[ -n "${DBLLM_TEST_ENGINE_CALLED:-}" ]]; then
    echo called >> "${DBLLM_TEST_ENGINE_CALLED}"
fi
if [[ -n "${DBLLM_TEST_ENGINE_ENV_DUMP:-}" ]]; then
    {
        printf 'SSH_HOST=%s\n' "${SSH_HOST-<unset>}"
        printf 'SSH_PORT=%s\n' "${SSH_PORT-<unset>}"
        printf 'SSH_USER=%s\n' "${SSH_USER-<unset>}"
        printf 'SSH_LOCAL_PORT=%s\n' "${SSH_LOCAL_PORT-<unset>}"
        printf 'SSH_REMOTE_HOST=%s\n' "${SSH_REMOTE_HOST-<unset>}"
        printf 'SSH_REMOTE_PORT=%s\n' "${SSH_REMOTE_PORT-<unset>}"
        printf 'SSH_KEY_FILE=%s\n' "${SSH_KEY_FILE-<unset>}"
        printf 'SSH_PASSWORD=%s\n' "${SSH_PASSWORD-<unset>}"
    } > "${DBLLM_TEST_ENGINE_ENV_DUMP}"
fi

if [[ "${1:-}" != "ensure" ]]; then
    echo "mock engine: unsupported subcommand ${1:-}" >&2
    exit 9
fi

mode="${DBLLM_TEST_ENGINE_MODE:-success}"
case "${mode}" in
    success)
        echo "已就绪（复用）"
        exit 0
        ;;
    fail_with_host)
        echo "SSH 隧道 10s 内未就绪：${SSH_HOST}" >&2
        exit 1
        ;;
    fail_with_pw)
        echo "认证失败，密码=${SSH_PASSWORD}" >&2
        exit 1
        ;;
    *)
        echo "mock engine: unknown DBLLM_TEST_ENGINE_MODE=${mode}" >&2
        exit 9
        ;;
esac
"""

# Dumps its full inherited environment — used to prove SSH_PASSWORD/SSHPASS
# do not survive into a psql-shaped child of config.sh's own process.
MOCK_PSQL_ENV_DUMP = r"""#!/bin/bash
env > "${DBLLM_TEST_PSQL_ENVIRON_DUMP}"
exit 0
"""


@dataclass
class TunnelProject:
    env: dict[str, str]
    write_config: Callable[..., None]
    run: Callable[..., subprocess.CompletedProcess]
    config_file: Path
    shared_dir: Path
    key_file: Path
    home_dir: Path


@pytest.fixture()
def tunnel_project(tmp_path: Path) -> TunnelProject:
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    shutil.copy(REAL_CONFIG_SH, shared_dir / "config.sh")

    engine_path = shared_dir / "ssh-tunnel.sh"
    engine_path.write_text(MOCK_ENGINE)
    engine_path.chmod(engine_path.stat().st_mode | stat.S_IEXEC)

    home_dir = tmp_path / "home"
    key_file = home_dir / ".ssh" / "id_test"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("dummy-private-key\n")

    config_file = tmp_path / ".dbllm.env"

    def write_config(
        *,
        db_host: str | None = "localhost",
        db_port: str | None = "15432",
        ssh_host: str | None = "jump.example.com",
        ssh_remote_host: str | None = "10.0.0.5",
        ssh_key_file: str | None = str(key_file),
        ssh_password: str | None = None,
        extra_lines: list[str] | None = None,
    ) -> None:
        # db_host/db_port default to values a pre-T74 consuming project would
        # have written; pass None to omit the line entirely (the T74 target
        # shape for tunnel mode, where both are derived rather than supplied).
        lines = []
        if db_host is not None:
            lines.append(f"DB_HOST={db_host}")
        if db_port is not None:
            lines.append(f"DB_PORT={db_port}")
        lines += [
            "DB_NAME=basedb",
            "DB_USER=llm_readonly",
            "DB_PASSWORD=ropass",
        ]
        if ssh_host is not None:
            lines.append(f"SSH_HOST={ssh_host}")
        if ssh_remote_host is not None:
            lines.append(f"SSH_REMOTE_HOST={ssh_remote_host}")
        if ssh_key_file is not None:
            lines.append(f"SSH_KEY_FILE={ssh_key_file}")
        if ssh_password is not None:
            lines.append(f"SSH_PASSWORD={ssh_password}")
        if extra_lines:
            lines.extend(extra_lines)
        config_file.write_text("\n".join(lines) + "\n")

    write_config()

    env = dict(os.environ)
    env["HOME"] = str(home_dir)

    def run(
        run_env: dict[str, str],
        *,
        dump_ssh_vars: Path | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        dump_snippet = ""
        if dump_ssh_vars is not None:
            dump_snippet = textwrap.dedent(
                f'''
                {{
                    printf 'DBS_SSH_HOST=%s\\n' "${{DBS_SSH_HOST-<unset>}}"
                    printf 'DBS_SSH_PORT=%s\\n' "${{DBS_SSH_PORT-<unset>}}"
                    printf 'DBS_SSH_USER=%s\\n' "${{DBS_SSH_USER-<unset>}}"
                    printf 'DBS_SSH_LOCAL_PORT=%s\\n' "${{DBS_SSH_LOCAL_PORT-<unset>}}"
                    printf 'DBS_SSH_REMOTE_HOST=%s\\n' "${{DBS_SSH_REMOTE_HOST-<unset>}}"
                    printf 'DBS_SSH_REMOTE_PORT=%s\\n' "${{DBS_SSH_REMOTE_PORT-<unset>}}"
                    printf 'DBS_SSH_KEY_FILE=%s\\n' "${{DBS_SSH_KEY_FILE-<unset>}}"
                    printf 'DBS_SSH_PASSWORD=%s\\n' "${{DBS_SSH_PASSWORD-<unset>}}"
                }} > "{dump_ssh_vars}"
                '''
            )
        script = textwrap.dedent(
            f'''
            set -u
            source "{shared_dir / "config.sh"}"
            db_llm_load_config "{config_file}" || exit 10
            {dump_snippet}
            db_llm_ensure_tunnel
            exit $?
            '''
        )
        return subprocess.run(
            ["bash", "-c", script],
            env=run_env,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return TunnelProject(
        env=env,
        write_config=write_config,
        run=run,
        config_file=config_file,
        shared_dir=shared_dir,
        key_file=key_file,
        home_dir=home_dir,
    )


def _dump_to_dict(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


# --- REQ-ST-1: parsing, defaults, ~/ expansion ------------------------------


def test_ssh_fields_parsed_with_defaults(tunnel_project: TunnelProject, tmp_path: Path):
    """Only SSH_HOST/SSH_REMOTE_HOST/SSH_KEY_FILE set -> the other four
    SSH_* keys fall back to their documented defaults."""
    tp = tunnel_project
    dump = tmp_path / "ssh_vars.env"
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "success"

    result = tp.run(tp.env, dump_ssh_vars=dump)

    assert result.returncode == 0, result.stderr
    dumped = _dump_to_dict(dump)
    assert dumped["DBS_SSH_HOST"] == "jump.example.com"
    assert dumped["DBS_SSH_PORT"] == "22"
    assert dumped["DBS_SSH_USER"] == "root"
    assert dumped["DBS_SSH_LOCAL_PORT"] == "15432"
    assert dumped["DBS_SSH_REMOTE_HOST"] == "10.0.0.5"
    assert dumped["DBS_SSH_REMOTE_PORT"] == "5432"


def test_ssh_fields_explicit_overrides_respected(tunnel_project: TunnelProject, tmp_path: Path):
    tp = tunnel_project
    tp.write_config(
        db_port="16432",
        extra_lines=["SSH_PORT=2222", "SSH_USER=deploy", "SSH_LOCAL_PORT=16432", "SSH_REMOTE_PORT=6543"],
    )
    dump = tmp_path / "ssh_vars.env"
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "success"

    result = tp.run(tp.env, dump_ssh_vars=dump)

    assert result.returncode == 0, result.stderr
    dumped = _dump_to_dict(dump)
    assert dumped["DBS_SSH_PORT"] == "2222"
    assert dumped["DBS_SSH_USER"] == "deploy"
    assert dumped["DBS_SSH_LOCAL_PORT"] == "16432"
    assert dumped["DBS_SSH_REMOTE_PORT"] == "6543"


def test_ssh_key_file_tilde_expansion(tunnel_project: TunnelProject, tmp_path: Path):
    """`~/` is a literal prefix substitution to $HOME, not shell expansion."""
    tp = tunnel_project
    tp.write_config(ssh_key_file="~/.ssh/id_test")
    dump = tmp_path / "ssh_vars.env"
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "success"

    result = tp.run(tp.env, dump_ssh_vars=dump)

    assert result.returncode == 0, result.stderr
    dumped = _dump_to_dict(dump)
    assert dumped["DBS_SSH_KEY_FILE"] == str(tp.key_file)


# --- REQ-ST-1: enablement gate / zero side effects when disabled -----------


def test_disabled_when_ssh_host_absent(tunnel_project: TunnelProject, tmp_path: Path):
    tp = tunnel_project
    tp.write_config(ssh_host=None, ssh_remote_host=None, ssh_key_file=None)
    called_marker = tmp_path / "engine-called.marker"
    tp.env["DBLLM_TEST_ENGINE_CALLED"] = str(called_marker)

    result = tp.run(tp.env)

    assert result.returncode == 0, result.stderr
    assert not called_marker.exists(), "engine must not be invoked when tunnel mode is disabled"


def test_disabled_when_ssh_host_is_change_me(tunnel_project: TunnelProject, tmp_path: Path):
    tp = tunnel_project
    tp.write_config(ssh_host="CHANGE_ME", ssh_remote_host=None, ssh_key_file=None)
    called_marker = tmp_path / "engine-called.marker"
    tp.env["DBLLM_TEST_ENGINE_CALLED"] = str(called_marker)

    result = tp.run(tp.env)

    assert result.returncode == 0, result.stderr
    assert not called_marker.exists()


# --- REQ-ST-2: structural validation ----------------------------------------


def test_fails_when_remote_host_empty(tunnel_project: TunnelProject):
    """SSH_HOST set but SSH_REMOTE_HOST empty -> rc 1. This is the direct
    replacement for the brief's requested (but, at this checkpoint, not yet
    wireable) ro-session.sh-driven negative case — see module docstring."""
    tp = tunnel_project
    tp.write_config(ssh_remote_host=None)

    result = tp.run(tp.env)

    assert result.returncode == 1
    assert "SSH_REMOTE_HOST 为空" in result.stderr
    assert "SSH_REMOTE_HOST=" in result.stderr


def _export_pg_env(tp: TunnelProject) -> subprocess.CompletedProcess:
    """Run db_llm_export_pg_env and print the PGHOST/PGPORT it settled on."""
    script = textwrap.dedent(
        f'''
        set -u
        source "{tp.shared_dir / "config.sh"}"
        db_llm_load_config "{tp.config_file}" || exit 10
        db_llm_export_pg_env || exit $?
        printf 'PGHOST=%s\\nPGPORT=%s\\n' "${{PGHOST}}" "${{PGPORT}}"
        '''
    )
    return subprocess.run(
        ["bash", "-c", script], env=tp.env,
        capture_output=True, text=True, timeout=30,
    )


def test_tunnel_mode_does_not_need_db_host_or_db_port(tunnel_project: TunnelProject):
    """T74 target shape: in tunnel mode both values are fully determined by the
    tunnel, so the config need not carry them at all."""
    tp = tunnel_project
    tp.write_config(db_host=None, db_port=None)

    assert tp.run(tp.env).returncode == 0

    exported = _export_pg_env(tp)
    assert exported.returncode == 0, exported.stderr
    assert "PGHOST=localhost" in exported.stdout
    assert "PGPORT=15432" in exported.stdout  # SSH_LOCAL_PORT


def test_tunnel_mode_ignores_supplied_db_host_and_db_port(tunnel_project: TunnelProject):
    """Backward compatibility, and the easiest thing to get wrong here: existing
    consuming projects still carry these two lines — sometimes disagreeing with
    the tunnel, since the old cross-field validation is what used to catch that.
    Upgrading MUST NOT start failing on them; they are simply not read, and the
    derived values win."""
    tp = tunnel_project
    tp.write_config(db_host="10.0.0.9", db_port="9999")

    result = tp.run(tp.env)
    assert result.returncode == 0, result.stderr

    exported = _export_pg_env(tp)
    assert exported.returncode == 0, exported.stderr
    assert "PGHOST=localhost" in exported.stdout
    assert "PGPORT=15432" in exported.stdout


def test_direct_mode_still_requires_db_host_and_db_port(tunnel_project: TunnelProject):
    """The other half of the branch: with no SSH_HOST there is no tunnel to
    derive from, so these two stay required."""
    tp = tunnel_project
    tp.write_config(db_host=None, db_port=None, ssh_host=None, ssh_remote_host=None)

    exported = _export_pg_env(tp)

    assert exported.returncode == 2
    assert "缺少必需的连接字段" in exported.stderr


def test_direct_mode_uses_supplied_db_host_and_db_port(tunnel_project: TunnelProject):
    tp = tunnel_project
    tp.write_config(
        db_host="10.0.0.9", db_port="5433", ssh_host=None, ssh_remote_host=None
    )

    exported = _export_pg_env(tp)

    assert exported.returncode == 0, exported.stderr
    assert "PGHOST=10.0.0.9" in exported.stdout
    assert "PGPORT=5433" in exported.stdout


@pytest.mark.parametrize(
    "key,value",
    [
        ("SSH_PORT", "70000"),
        ("SSH_PORT", "0"),
        ("SSH_REMOTE_PORT", "abc"),
    ],
)
def test_fails_when_jump_or_remote_port_out_of_range(tunnel_project: TunnelProject, key: str, value: str):
    tp = tunnel_project
    tp.write_config(extra_lines=[f"{key}={value}"])

    result = tp.run(tp.env)

    assert result.returncode == 1
    assert f"{key}={value} 不是 1..65535 的整数" in result.stderr


def test_fails_when_local_port_out_of_range(tunnel_project: TunnelProject):
    """SSH_LOCAL_PORT is also DB_PORT's consistency partner — set both to the
    same out-of-range value so the consistency check (which runs first) does
    not mask the integer check."""
    tp = tunnel_project
    tp.write_config(db_port="abc", extra_lines=["SSH_LOCAL_PORT=abc"])

    result = tp.run(tp.env)

    assert result.returncode == 1
    assert "SSH_LOCAL_PORT=abc 不是 1..65535 的整数" in result.stderr


def test_fails_when_auth_missing(tunnel_project: TunnelProject):
    tp = tunnel_project
    tp.write_config(ssh_key_file=None, ssh_password=None)

    result = tp.run(tp.env)

    assert result.returncode == 1
    assert "未配置认证方式" in result.stderr
    assert "SSH_KEY_FILE" in result.stderr
    assert "SSH_PASSWORD" in result.stderr


# --- REQ-ST-2: environment validation ---------------------------------------


def test_fails_when_cert_file_missing(tunnel_project: TunnelProject, tmp_path: Path):
    tp = tunnel_project
    missing_key = tmp_path / "nonexistent_key"
    tp.write_config(ssh_key_file=str(missing_key))

    result = tp.run(tp.env)

    assert result.returncode == 1
    assert f"证书文件不存在：{missing_key}" in result.stderr


def test_fails_when_password_mode_missing_sshpass(tunnel_project: TunnelProject):
    tp = tunnel_project
    tp.write_config(ssh_key_file=None, ssh_password="tunnelpw")
    tp.env["PATH"] = "/usr/bin:/bin"  # deterministically excludes sshpass

    result = tp.run(tp.env)

    assert result.returncode == 1
    assert "密码模式需要 sshpass" in result.stderr


def test_cert_takes_priority_over_password_in_env_validation(tunnel_project: TunnelProject):
    """When both SSH_KEY_FILE and SSH_PASSWORD are set, only the cert file's
    existence is checked — sshpass need not be installed. (The actual ssh
    argv-level cert-vs-password precedence lives in the real engine's
    init_connect_params, already covered by Task 1's own tests; this asserts
    the config.sh-level half of that contract: env validation does not
    demand sshpass when a valid cert is present.)"""
    tp = tunnel_project
    tp.write_config(ssh_key_file=str(tp.key_file), ssh_password="tunnelpw")
    tp.env["PATH"] = "/usr/bin:/bin"  # excludes sshpass
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "success"

    result = tp.run(tp.env)

    assert result.returncode == 0, result.stderr


# --- REQ-ST-3/REQ-ST-4: engine call contract + redaction --------------------


def test_engine_receives_mapped_env_vars(tunnel_project: TunnelProject, tmp_path: Path):
    tp = tunnel_project
    dump = tmp_path / "engine_env.dump"
    tp.env["DBLLM_TEST_ENGINE_ENV_DUMP"] = str(dump)
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "success"

    result = tp.run(tp.env)

    assert result.returncode == 0, result.stderr
    dumped = _dump_to_dict(dump)
    assert dumped["SSH_HOST"] == "jump.example.com"
    assert dumped["SSH_PORT"] == "22"
    assert dumped["SSH_USER"] == "root"
    assert dumped["SSH_LOCAL_PORT"] == "15432"
    assert dumped["SSH_REMOTE_HOST"] == "10.0.0.5"
    assert dumped["SSH_REMOTE_PORT"] == "5432"
    assert dumped["SSH_KEY_FILE"] == str(tp.key_file)


def test_engine_called_with_absolute_path_regardless_of_cwd(tunnel_project: TunnelProject, tmp_path: Path):
    """DBS_SHARED_DIR is resolved from config.sh's own BASH_SOURCE at source
    time, so the engine is found even when the caller's CWD is unrelated."""
    tp = tunnel_project
    unrelated_cwd = tmp_path / "somewhere-else"
    unrelated_cwd.mkdir()
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "success"

    result = tp.run(tp.env, cwd=unrelated_cwd)

    assert result.returncode == 0, result.stderr


def test_engine_failure_forwards_redacted_host(tunnel_project: TunnelProject):
    tp = tunnel_project
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "fail_with_host"

    result = tp.run(tp.env)

    assert result.returncode == 1
    assert "jump.example.com" not in result.stderr
    assert "[REDACTED]" in result.stderr


def test_engine_failure_forwards_redacted_password_with_escape_chars(tunnel_project: TunnelProject):
    tp = tunnel_project
    secret = r"p\a&ss/word"
    tp.write_config(ssh_key_file=None, ssh_password=secret)
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "fail_with_pw"

    result = tp.run(tp.env)

    assert result.returncode == 1
    assert secret not in result.stderr
    assert "[REDACTED]" in result.stderr


def test_password_not_exported_into_caller_process_or_psql_child(tunnel_project: TunnelProject, tmp_path: Path):
    """SSH_PASSWORD must reach the engine only via the variable-prefix
    single-command form — never `export`ed into config.sh's own process,
    so a later psql child spawned by the same process never inherits it."""
    tp = tunnel_project
    tp.write_config(ssh_key_file=None, ssh_password="tunnelpw")
    tp.env["DBLLM_TEST_ENGINE_MODE"] = "success"

    mock_psql = tmp_path / "mock_psql.sh"
    mock_psql.write_text(MOCK_PSQL_ENV_DUMP)
    mock_psql.chmod(mock_psql.stat().st_mode | stat.S_IEXEC)
    psql_env_dump = tmp_path / "psql_environ.dump"
    tp.env["DBLLM_TEST_PSQL_ENVIRON_DUMP"] = str(psql_env_dump)

    script = textwrap.dedent(
        f'''
        set -u
        source "{tp.shared_dir / "config.sh"}"
        db_llm_load_config "{tp.config_file}" || exit 10
        db_llm_ensure_tunnel || exit 20
        if env | grep -q '^SSH_PASSWORD='; then echo LEAK_SSH_PASSWORD_OWN_ENV; fi
        if env | grep -q '^SSHPASS='; then echo LEAK_SSHPASS_OWN_ENV; fi
        "{mock_psql}"
        '''
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env=tp.env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "LEAK_SSH_PASSWORD_OWN_ENV" not in result.stdout
    assert "LEAK_SSHPASS_OWN_ENV" not in result.stdout
    psql_environ = psql_env_dump.read_text()
    assert "SSH_PASSWORD=" not in psql_environ
    assert "SSHPASS=" not in psql_environ


# --- REQ-ST-4: db_llm_redact / db_llm_redact_one direct unit tests ----


def _run_config_snippet(shared_dir: Path, snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    script = f'source "{shared_dir / "config.sh"}"\n{snippet}'
    return subprocess.run(
        ["bash", "-c", script],
        env=env if env is not None else dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("secret", [r"p\a&ss/word", "simplepw", r"back\slash"])
def test_redact_one_handles_escape_characters(tunnel_project: TunnelProject, secret: str):
    tp = tunnel_project
    text = f"connection failed: password={secret} host=unchanged"
    result = _run_config_snippet(
        tp.shared_dir,
        f'db_llm_redact_one {textwrap_quote(text)} {textwrap_quote(secret)}',
    )
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert "host=unchanged" in result.stdout


def test_redact_scrubs_ssh_password_and_host(tunnel_project: TunnelProject):
    tp = tunnel_project
    env = dict(os.environ)
    env["DBS_SSH_PASSWORD"] = "tunnelpw"
    env["DBS_SSH_HOST"] = "jump.example.com"
    text = "auth failed for tunnelpw against jump.example.com"
    result = _run_config_snippet(
        tp.shared_dir,
        f'db_llm_redact {textwrap_quote(text)}',
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "tunnelpw" not in result.stdout
    assert "jump.example.com" not in result.stdout
    assert result.stdout.count("[REDACTED]") == 2


def textwrap_quote(value: str) -> str:
    """Shell-quote a literal string for embedding into a bash -c script."""
    return "'" + value.replace("'", "'\\''") + "'"


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end tests (Task 3, tasks.md 3.1-3.3, design.md D6, specs/ssh-tunnel/
# spec.md REQ-ST-3) — db_llm_export_pg_env's own db_llm_ensure_tunnel
# call, driven through the REAL connection entry scripts against the REAL
# shared/ssh-tunnel.sh engine (this file's MOCK_SSH stands in only for the
# `ssh` binary the engine spawns) plus a mock `psql`. No real network/DB.
#
# Why this is a stronger anchor than calling db_llm_ensure_tunnel
# directly (as the Config-segment tests above already do): it proves the
# wiring at the one call site this task actually edits
# (db_llm_export_pg_env in shared/config.sh) is reached by walking the
# real, unmodified entry scripts end to end — not by asserting the helper
# function's contract in isolation.
# ═══════════════════════════════════════════════════════════════════════════

DB_COLLECT_SH = REPO_ROOT / "shared" / "db-collect.sh"
RO_SESSION_SH = REPO_ROOT / "shared" / "ro-session.sh"
RO_VERIFY_SH = REPO_ROOT / "shared" / "ro-verify.sh"
READONLY_SETUP_SH = REPO_ROOT / "pg-readonly-setup" / "scripts" / "readonly-setup.sh"

# Mock psql for the e2e tests below. Records the PGHOST/PGPORT it was
# invoked with (proving they reflect the tunnel's local forwarded port, not
# .dbllm.env's raw DB_HOST/DB_PORT) and, separately, that it was invoked
# at all — then exits 0 with no output. Every one of the four entry
# scripts' probe/query calls treats a clean, zero-output psql run as
# success (none of the e2e assertions below depend on psql's stdout
# content).
MOCK_PSQL_E2E = r"""#!/bin/bash
if [[ -n "${DBLLM_TEST_PSQL_CALLED:-}" ]]; then
    echo called >> "${DBLLM_TEST_PSQL_CALLED}"
fi
if [[ -n "${DBLLM_TEST_PSQL_ENV_DUMP:-}" ]]; then
    {
        printf 'PGHOST=%s\n' "${PGHOST-<unset>}"
        printf 'PGPORT=%s\n' "${PGPORT-<unset>}"
    } >> "${DBLLM_TEST_PSQL_ENV_DUMP}"
fi
exit 0
"""

# Generic stand-in for shared/ro-generate.sh / shared/ro-verify.sh /
# shared/ro-session.sh when a test needs to sidestep that script's own
# unrelated logic (already covered by its own dedicated test file) and
# isolate just the tunnel-wiring question.
MOCK_STUB_EXIT0 = "#!/bin/bash\nexit 0\n"


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@dataclass
class E2EProject:
    root: Path
    env: dict[str, str]
    local_port: int


@pytest.fixture()
def e2e_project(tmp_path: Path) -> E2EProject:
    """A throwaway consuming-project layout in SSH-tunnel mode: a
    .dbmeta/.dbllm.env whose DB_HOST/DB_PORT already point at the local
    forwarded port (localhost:<local_port>, matching SSH_LOCAL_PORT — the
    same "already-consistent" precondition db_llm_ensure_tunnel's own
    structural validation requires), plus a PATH pre-loaded with a mock
    `ssh` (this file's MOCK_SSH, `listen` mode by default — binds+listens
    on the real forwarded port, simulating a jump host that came up) and a
    mock `psql` (MOCK_PSQL_E2E)."""
    root = tmp_path / "e2e-project"
    (root / ".dbmeta").mkdir(parents=True)

    key_file = tmp_path / "e2e-key"
    key_file.write_text("dummy-private-key\n")

    bin_dir = tmp_path / "e2e-bin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "ssh", MOCK_SSH)
    _write_exec(bin_dir / "psql", MOCK_PSQL_E2E)

    local_port = _free_port()
    (root / ".dbmeta" / ".dbllm.env").write_text(
        "SCHEMAS=public\n"
        f"DB_HOST=localhost\nDB_PORT={local_port}\nDB_NAME=appdb\n"
        "DB_USER=llm_readonly\nDB_PASSWORD=e2e-marker-pw\n"
        "SSH_HOST=jump.example.com\n"
        "SSH_REMOTE_HOST=10.0.0.5\n"
        f"SSH_LOCAL_PORT={local_port}\n"
        f"SSH_KEY_FILE={key_file}\n"
    )

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ROOT_DIR"] = str(root)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env["MOCK_SSH_ARGV_DUMP"] = str(tmp_path / "ssh-argv.txt")
    env["SSH_TUNNEL_ENSURE_TIMEOUT"] = "5"
    return E2EProject(root=root, env=env, local_port=local_port)


def _kill_if_set(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _assert_auto_started(stderr: str) -> int:
    """Asserts the real engine's "已启动 (PID N)" success line made it
    through db_llm_ensure_tunnel's forwarding into the caller's stderr,
    and returns the spawned PID for teardown. Only db-collect.sh forwards
    db_llm_export_pg_env's stderr in real time on the success path — see
    _pid_from_pidfile for the entry scripts (ro-session.sh/ro-verify.sh/
    readonly-setup.sh) that instead capture it to a tempfile and only
    surface it on failure."""
    m = re.search(r"已启动 \(PID (\d+)\)", stderr)
    assert m, f"expected auto-start message in stderr, got: {stderr!r}"
    return int(m.group(1))


def _pid_from_pidfile(local_port: int) -> int | None:
    """Reads the real engine's own /tmp/ssh-tunnel-<port>.pid (shared/
    ssh-tunnel.sh:25) — the auto-start proof to use for entry scripts that
    swallow db_llm_export_pg_env's success-path stderr (see
    _assert_auto_started's docstring), since a swallowed message is not
    "no tunnel started", just "not surfaced"."""
    pid_file = Path(f"/tmp/ssh-tunnel-{local_port}.pid")
    if not pid_file.exists():
        return None
    return int(pid_file.read_text().strip())


class TestE2EDbCollectAutoTunnel:
    """shared/db-collect.sh — REQ-ST-3's primary named entry point."""

    def test_first_call_starts_tunnel_and_reaches_psql_with_local_port(self, e2e_project: E2EProject):
        p = e2e_project
        psql_dump = p.root / "psql-env.dump"
        env = dict(p.env)
        env["DBLLM_TEST_PSQL_ENV_DUMP"] = str(psql_dump)

        pid = None
        try:
            result = subprocess.run(
                ["bash", str(DB_COLLECT_SH)],
                cwd=p.root, env=env, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
            pid = _assert_auto_started(result.stderr)

            argv_dump = Path(env["MOCK_SSH_ARGV_DUMP"]).read_text()
            assert "jump.example.com" in argv_dump
            assert f"{p.local_port}:10.0.0.5:5432" in argv_dump

            dumped = psql_dump.read_text()
            assert "PGHOST=localhost" in dumped
            assert f"PGPORT={p.local_port}" in dumped
        finally:
            _kill_if_set(pid)

    def test_second_call_reuses_without_spawning_ssh_again(self, e2e_project: E2EProject):
        p = e2e_project
        env = dict(p.env)

        pid = None
        try:
            first = subprocess.run(
                ["bash", str(DB_COLLECT_SH)],
                cwd=p.root, env=env, capture_output=True, text=True, timeout=30,
            )
            assert first.returncode == 0, f"stdout={first.stdout}\nstderr={first.stderr}"
            pid = _assert_auto_started(first.stderr)
            argv_dump_path = Path(env["MOCK_SSH_ARGV_DUMP"])
            first_argv = argv_dump_path.read_text()

            second = subprocess.run(
                ["bash", str(DB_COLLECT_SH)],
                cwd=p.root, env=env, capture_output=True, text=True, timeout=30,
            )
            assert second.returncode == 0, f"stdout={second.stdout}\nstderr={second.stderr}"
            assert "已就绪（复用）" in second.stderr
            assert "已启动" not in second.stderr
            # The mock ssh was never invoked a second time -> its argv dump
            # (overwritten on every invocation) is byte-identical.
            assert argv_dump_path.read_text() == first_argv
        finally:
            _kill_if_set(pid)


class TestE2EJumpFailure:
    def test_jump_failure_exits_nonzero_and_psql_never_called(self, e2e_project: E2EProject):
        p = e2e_project
        env = dict(p.env)
        env["MOCK_SSH_MODE"] = "exit255"
        psql_called = p.root / "psql-called.marker"
        env["DBLLM_TEST_PSQL_CALLED"] = str(psql_called)

        result = subprocess.run(
            ["bash", str(DB_COLLECT_SH)],
            cwd=p.root, env=env, capture_output=True, text=True, timeout=30,
        )

        assert result.returncode != 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert not psql_called.exists(), "psql must never be invoked when the tunnel fails to come up"


class TestE2ESharedEntryPointsAutoTunnel:
    """One "not yet listening -> auto starts" case per remaining connection
    entry script (db-collect.sh already has dedicated coverage above),
    proving REQ-ST-3's "all four entry points share this behavior" — each
    sidesteps that script's own unrelated logic (already covered by its own
    dedicated test file) via its existing *_OVERRIDE testability seam, to
    isolate just the tunnel-wiring question."""

    def test_ro_session_sh_auto_starts_tunnel(self, e2e_project: E2EProject):
        p = e2e_project
        env = dict(p.env)
        psql_dump = p.root / "psql-env.dump"
        env["DBLLM_TEST_PSQL_ENV_DUMP"] = str(psql_dump)

        pid = None
        try:
            result = subprocess.run(
                ["bash", str(RO_SESSION_SH), "--sql", "SELECT 1"],
                cwd=p.root, env=env, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
            # ro-session.sh captures db_llm_export_pg_env's stderr to a
            # tempfile and only surfaces it on failure (shared/ro-session.sh
            # ~line 229) — so the auto-start proof here is the mock ssh's
            # own argv dump + the real engine's pidfile, not stderr text.
            argv_dump = Path(env["MOCK_SSH_ARGV_DUMP"]).read_text()
            assert "jump.example.com" in argv_dump
            assert f"{p.local_port}:10.0.0.5:5432" in argv_dump
            pid = _pid_from_pidfile(p.local_port)
            assert pid is not None, "expected the real engine's pidfile to exist after auto-start"

            dumped = psql_dump.read_text()
            assert "PGHOST=localhost" in dumped
            assert f"PGPORT={p.local_port}" in dumped
        finally:
            _kill_if_set(pid)

    def test_ro_verify_sh_auto_starts_tunnel(self, e2e_project: E2EProject):
        p = e2e_project
        env = dict(p.env)
        psql_dump = p.root / "psql-env.dump"
        env["DBLLM_TEST_PSQL_ENV_DUMP"] = str(psql_dump)
        # ro-verify.sh's own six-face audit query shapes/parsing and its
        # ro-session.sh delegation are tests/test_ro_verify_shell.py's job;
        # stub the delegation out so a downstream parsing quirk in the mock
        # psql's empty responses can't mask this test's only question: did
        # the tunnel come up before ro-verify.sh's first real psql call.
        # (ro-verify.sh also captures db_llm_export_pg_env's stderr to a
        # tempfile on success — same swallowing as ro-session.sh above.)
        mock_ro_session = _write_exec(p.root / "mock-ro-session.sh", MOCK_STUB_EXIT0)
        env["RO_SESSION_SH_OVERRIDE"] = str(mock_ro_session)

        pid = None
        try:
            subprocess.run(
                ["bash", str(RO_VERIFY_SH)],
                cwd=p.root, env=env, capture_output=True, text=True, timeout=30,
            )
            argv_dump = Path(env["MOCK_SSH_ARGV_DUMP"]).read_text()
            assert "jump.example.com" in argv_dump
            assert f"{p.local_port}:10.0.0.5:5432" in argv_dump
            pid = _pid_from_pidfile(p.local_port)
            assert pid is not None, "expected the real engine's pidfile to exist after auto-start"

            dumped = psql_dump.read_text()
            assert "PGHOST=localhost" in dumped
            assert f"PGPORT={p.local_port}" in dumped
        finally:
            _kill_if_set(pid)

    def test_db_readonly_setup_sh_auto_starts_tunnel(self, e2e_project: E2EProject):
        p = e2e_project
        env = dict(p.env)
        psql_dump = p.root / "psql-env.dump"
        env["DBLLM_TEST_PSQL_ENV_DUMP"] = str(psql_dump)
        # Steps ②/④ (generate/verify) are orthogonal to tunnel wiring and
        # already covered by tests/test_ro_generate_shell.py /
        # tests/test_ro_verify_shell.py — stub them so only step ③'s
        # db_llm_export_pg_env call (this task's edit) is exercised.
        # Step ③'s own probe uses plain `psql` (no override in the real
        # script), which resolves via PATH to the mock installed above.
        # (Also captures db_llm_export_pg_env's stderr on success — same
        # swallowing as ro-session.sh/ro-verify.sh above.)
        stub_generate = _write_exec(p.root / "mock-ro-generate.sh", MOCK_STUB_EXIT0)
        stub_verify = _write_exec(p.root / "mock-ro-verify.sh", MOCK_STUB_EXIT0)
        env["RO_GENERATE_SH_OVERRIDE"] = str(stub_generate)
        env["RO_VERIFY_SH_OVERRIDE"] = str(stub_verify)

        pid = None
        try:
            result = subprocess.run(
                ["bash", str(READONLY_SETUP_SH)],
                cwd=p.root, env=env, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
            argv_dump = Path(env["MOCK_SSH_ARGV_DUMP"]).read_text()
            assert "jump.example.com" in argv_dump
            assert f"{p.local_port}:10.0.0.5:5432" in argv_dump
            pid = _pid_from_pidfile(p.local_port)
            assert pid is not None, "expected the real engine's pidfile to exist after auto-start"

            dumped = psql_dump.read_text()
            assert "PGHOST=localhost" in dumped
            assert f"PGPORT={p.local_port}" in dumped
        finally:
            _kill_if_set(pid)


# The ssh-tunnel change's "six scripts untouched" anchor lived here. It asserted
# `git diff HEAD` was empty for those files — which proves a fact about that one
# change's diff, but as a standing test it reads as "these files may never be
# edited again". Its own comment already said the promise "was about that
# specific change's own diff, not a permanent freeze"; even so it was worked
# around once (ro-session.sh removed from the list for add-pg-sql-check) rather
# than retired, and it blocked the next legitimate edit too (db-collect.sh, B9).
# The change is archived and its verify-report.md records the anchor as ✅ — that
# historical fact is what git history is for, so the test is gone rather than
# whittled down a third time.


# ═══════════════════════════════════════════════════════════════════════════
# Static regression guard (Task 5) — no stale path references remain
# ═══════════════════════════════════════════════════════════════════════════


def test_no_stale_tests_ssh_tunnel_sh_path_references():
    """`tests/ssh-tunnel.sh` MUST NOT appear anywhere the file could be
    invoked or documented from — the engine now lives at
    shared/ssh-tunnel.sh (this test file's own SSH_TUNNEL_SH constant)."""
    stale = "tests/ssh-tunnel.sh"
    candidates: list[Path] = []
    candidates.extend(REPO_ROOT.glob("*.sh"))
    candidates.extend(REPO_ROOT.glob("tests/*.sh"))
    candidates.extend(REPO_ROOT.glob("tests/*.example"))
    candidates.extend(REPO_ROOT.glob("shared/*.sh"))
    candidates.extend(REPO_ROOT.glob("shared/*.example"))
    candidates.extend(REPO_ROOT.glob("openspec/architecture/*.md"))

    offenders = []
    for path in candidates:
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        if stale in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == [], (
        f"stale '{stale}' reference(s) found in: {offenders} "
        "(engine moved to shared/ssh-tunnel.sh)"
    )
