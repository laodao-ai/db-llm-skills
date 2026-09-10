"""Offline tests for setup.sh：装哪 5 个 skill、装成什么形态、碰不碰别人的东西。

setup.sh 只对这 5 个名字做判定与写入，宿主目录下其它任何东西一律不动——姊妹仓
pg-ops 与本仓共用同一对宿主目录，这是两仓互不干扰的唯一依据（design F8）。
Windows 拷贝的所有权标记是 `.db-llm`，内含安装时的 HEAD sha。

No real ~/.claude or ~/.codex touched: every test runs setup.sh with HOME
pointed at a throwaway tmp_path (TARGET_DIRS is derived from $HOME, no
override variable). The Windows branch is exercised by shadowing `uname` on
PATH with a fake binary that reports a Windows-shaped uname -s string
(MINGW64_NT-...), the same mocking pattern this repo already uses for
psql/ssh in tests/test_ssh_tunnel_shell.py.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = REPO_ROOT / "setup.sh"

# 5 个新名，顺序与 setup.sh 安装循环一致（只读通道 4 个业务 skill + upgrade）。
NEW_SKILLS = [
    "pg-readonly-setup",
    "pg-dict",
    "pg-query-ro",
    "pg-sql-check",
    "db-llm-upgrade",
]
HOSTS = [".claude/skills", ".codex/skills"]


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run_setup(home: Path, extra_path: str | None = None, stdin_data: str | None = None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    if extra_path:
        env["PATH"] = f"{extra_path}:{env['PATH']}"
    kwargs = dict(cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30)
    if stdin_data is not None:
        return subprocess.run(["bash", str(SETUP_SH)], input=stdin_data, **kwargs)
    # No stdin at all -> inherits a real (non-tty in test harness) stdin.
    return subprocess.run(
        ["bash", str(SETUP_SH)], stdin=subprocess.DEVNULL, **kwargs
    )


class TestNewNameLinkedUnix:
    """① 五个 skill 在两个宿主目录各建一条指向本仓的软链。"""

    def test_symlinks_created_in_both_hosts(self, tmp_path: Path):
        home = tmp_path / "home"
        home.mkdir()
        for host in HOSTS:
            (home / host).mkdir(parents=True)

        result = _run_setup(home)
        assert result.returncode == 0, result.stderr

        for host in HOSTS:
            dest = home / host
            for skill in NEW_SKILLS:
                target = dest / skill
                assert target.is_symlink(), f"{target} should be a symlink"
                assert os.readlink(target) == str(REPO_ROOT / skill)


class TestForeignSymlinkPreserved:
    """② 别的仓装的软链原样不动 —— setup.sh 只碰 NEW_SKILLS 这五个名字。"""

    def test_symlink_to_another_repo_is_not_touched(self, tmp_path: Path):
        home = tmp_path / "home"
        other_repo = tmp_path / "other-repo" / "some-other-skill"
        other_repo.mkdir(parents=True)
        for host in HOSTS:
            dest = home / host
            dest.mkdir(parents=True)
            (dest / "some-other-skill").symlink_to(other_repo)

        result = _run_setup(home)
        assert result.returncode == 0, result.stderr

        for host in HOSTS:
            link = home / host / "some-other-skill"
            assert link.is_symlink()
            assert os.readlink(link) == str(other_repo)


class TestIdempotentRerun:
    """③ 幂等重跑 — running twice in a row is a clean no-op the second time."""

    def test_second_run_is_a_no_op_success(self, tmp_path: Path):
        home = tmp_path / "home"
        home.mkdir()

        first = _run_setup(home)
        assert first.returncode == 0, first.stderr

        second = _run_setup(home)
        assert second.returncode == 0, second.stderr
        assert "全部完成" in second.stdout
        for host in HOSTS:
            dest = home / host
            for skill in NEW_SKILLS:
                target = dest / skill
                assert target.is_symlink()
                assert os.readlink(target) == str(REPO_ROOT / skill)


class TestForeignRealDirectoryFailLoud:
    """④ 非自属实体目录 fail-loud 三行文案（INST-6）—— problem / cause / fix，退出非 0，
    绝不覆盖或递归删除不认识的目录。
    """

    def test_real_directory_not_owned_fails_loud_with_three_lines(self, tmp_path: Path):
        home = tmp_path / "home"
        home.mkdir()
        dest = home / HOSTS[0]
        dest.mkdir(parents=True)
        foreign_dir = dest / "pg-dict"
        foreign_dir.mkdir()
        (foreign_dir / "some-unrelated-file.txt").write_text("not ours\n")

        result = _run_setup(home)

        assert result.returncode != 0
        assert "problem:" in result.stderr
        assert "cause:" in result.stderr
        assert "fix:" in result.stderr
        fail_lines = [line for line in result.stderr.splitlines() if "[FAIL]" in line]
        assert len(fail_lines) == 3, f"expected exactly 3 [FAIL] lines, got: {fail_lines}"
        # 目录本身没被动过（不覆盖 / 不删）
        assert (foreign_dir / "some-unrelated-file.txt").exists()


class TestWindowsSameNameMarkerRefreshed:
    """⑤ Windows 自属拷贝重跑即更新：带 `.db-llm` 标记的同名 `pg-dict/` 被 rm -rf
    重拷（陈旧文件不残留），`shared/` 走合并拷贝分支（design F9），两者 marker 都
    刷成当前 HEAD。
    """

    def test_owned_copies_are_recopied_with_refreshed_marker(self, tmp_path: Path):
        home = tmp_path / "home"
        home.mkdir()
        bin_dir = tmp_path / "fake-bin"
        bin_dir.mkdir()
        _write_exec(bin_dir / "uname", "#!/bin/bash\necho 'MINGW64_NT-10.0'\n")

        for host in HOSTS:
            dest = home / host
            dest.mkdir(parents=True)
            for name in ("pg-dict", "shared"):
                copy_dir = dest / name
                copy_dir.mkdir()
                (copy_dir / ".db-llm").write_text("deadbeef\n")
                (copy_dir / "stale-file.txt").write_text("from the old copy\n")

        result = _run_setup(home, extra_path=str(bin_dir), stdin_data="")
        assert result.returncode == 0, result.stderr

        for host in HOSTS:
            dest = home / host
            copy_dir = dest / "pg-dict"
            assert copy_dir.is_dir()
            assert (copy_dir / ".db-llm").exists(), f"{copy_dir} should carry the marker"
            assert (copy_dir / ".db-llm").read_text() != "deadbeef\n", (
                f"{copy_dir} marker should have been refreshed to the current HEAD"
            )
            assert not (copy_dir / "stale-file.txt").exists(), (
                f"{copy_dir} should have been rm -rf'd and recopied fresh, not merged"
            )
            assert (dest / "pg-dict" / "SKILL.md").exists()
            # shared/ 是合并拷贝分支（design F9）：逐文件覆盖同名，不 rm -rf 清空目标目录，
            # 所以旧文件与旧 marker 会与新内容共存，跟 pg-dict/ 的整目录重拷行为不同。
            shared_dir = dest / "shared"
            assert shared_dir.is_dir()
            assert (shared_dir / ".db-llm").exists(), "shared/ 合并拷贝也应写入当前 marker"
            assert (shared_dir / "config.sh").exists(), "shared/ 合并拷贝应带上 db-llm 自己的脚本"


class TestPgOpsUpgradeSymlinkNeverTouched:
    """⑥ 姊妹仓 pg-ops 装的 `pg-ops-upgrade` 软链，跑 db-llm 的 setup.sh 后原样存活。
    两仓共用同一对宿主目录，谁也不许动对方的软链（design F8）。
    """

    def test_pg_ops_upgrade_symlink_survives_db_llm_setup(self, tmp_path: Path):
        home = tmp_path / "home"
        home.mkdir()
        pg_ops_root = home / ".skills" / "pg-ops-skills"
        for host in HOSTS:
            dest = home / host
            dest.mkdir(parents=True)
            (dest / "pg-ops-upgrade").symlink_to(pg_ops_root / "pg-ops-upgrade")

        result = _run_setup(home)
        assert result.returncode == 0, result.stderr

        for host in HOSTS:
            link = home / host / "pg-ops-upgrade"
            assert link.is_symlink(), "pg-ops-upgrade symlink must survive db-llm's setup.sh"
            assert os.readlink(link) == str(pg_ops_root / "pg-ops-upgrade")
