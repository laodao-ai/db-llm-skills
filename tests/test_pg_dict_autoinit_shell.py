"""Shell-layer tests for pg-dict/scripts/pg-dict.sh's auto-initialization
branches: the check of `.dbmeta/.dbllm.env` that runs before
`db_llm_load_config` is ever called.

Three of the branches ("missing" branches) MUST exit non-zero before reaching
db_llm_load_config, python3, or shared/db-collect.sh — so these tests never
touch a real database. The "normal" branch is covered indirectly: it must get
past the auto-init check and reach the point where DBMETA_DIR is reported.

No mocked psql needed here — every "missing" branch exits before
shared/db-collect.sh is ever invoked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PG_DICT_SH = REPO_ROOT / "pg-dict" / "scripts" / "pg-dict.sh"
TEMPLATE = REPO_ROOT / "shared" / ".dbllm.env.example"


@pytest.fixture()
def project(tmp_path: Path):
    """A throwaway consuming-project root with no .dbmeta/ — the "nothing
    exists yet" starting point."""
    root = tmp_path / "project"
    root.mkdir()
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    return root, env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PG_DICT_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_template_missing_fails_loud(project, tmp_path):
    """No config, no template -> fail-loud, exit non-zero, no .dbmeta/ created."""
    root, env = project
    env["DBLLM_TEMPLATE_OVERRIDE"] = str(tmp_path / "does-not-exist.template")

    result = _run(env)

    assert result.returncode != 0
    assert not (root / ".dbmeta").exists()
    assert "FAIL" in result.stdout


def test_template_autoinit_when_nothing_exists(project):
    """Neither old nor new config exists, but the install template does ->
    auto-create .dbmeta/ + copy template to .dbmeta/.dbllm.env, exit
    non-zero, prompt to fill credentials and rerun."""
    root, env = project

    result = _run(env)

    assert result.returncode != 0
    new_config = root / ".dbmeta" / ".dbllm.env"
    assert new_config.exists()
    content = new_config.read_text()
    assert content == TEMPLATE.read_text()


def test_normal_branch_passes_autoinit_check(project):
    """.dbmeta/.dbllm.env already exists -> auto-init check is a no-op."""
    root, env = project
    dbmeta = root / ".dbmeta"
    dbmeta.mkdir()
    (dbmeta / ".dbllm.env").write_text(
        "SCHEMAS=public\n"
        "DB_HOST=127.0.0.1\nDB_PORT=1\nDB_NAME=x\nDB_USER=x\nDB_PASSWORD=x\n"
    )

    result = _run(env)

    assert "未找到配置文件" not in result.stdout + result.stderr
    assert str(dbmeta) in result.stdout


def test_autoinit_mkdir_failure_fails_loud_with_diagnostics(project):
    """mkdir -p .dbmeta fails -> must fail loud with diagnostics."""
    root, env = project
    root.chmod(0o555)
    try:
        result = _run(env)
    finally:
        root.chmod(0o755)

    assert result.returncode != 0
    assert not (root / ".dbmeta").exists()
    combined = result.stdout + result.stderr
    assert "problem:" in combined
    assert "cause:" in combined
    assert "fix:" in combined
    assert "无法创建输出目录" in combined
