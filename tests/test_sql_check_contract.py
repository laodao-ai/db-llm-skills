"""Real-PG contract test for pg-sql-check (add-pg-sql-check Task 6, brief
`openspec/changes/add-pg-sql-check/impl-reports/task6-brief.md`,
specs/sql-check/spec.md REQ-SC-2 / REQ-SC-5 / REQ-SC-7).

Three load-bearing assertions, each pinned to a spec Scenario:

1. 只读角色下写语句校验（REQ-SC-2 Scenario「只读角色下校验写语句」）——PREPARE-based
   validation gives the SAME class of diagnosis (undefined column, 42703) for a write
   statement under a role that holds only USAGE+SELECT, never a permission error. This
   is the empirical proof that PostgreSQL's ACL check happens at executor start (never
   reached here — nothing is EXECUTEd), not at PREPARE/analyze time.
2. 契约快照（REQ-SC-5 Scenario「读语句的契约快照」/「写语句的契约快照」）——the JSON
   artifact's parameter_types/result_types reflect PostgreSQL's real type inference for
   a passing statement, with the null-vs-[]-vs-missing distinction preserved.
3. 校验后库内零变更（REQ-SC-7 Scenario「校验后库内无变更」）——validating a
   schema-valid INSERT under the read-only role leaves the table's data untouched
   (the row is never persisted).

2026-09-10 阶段 0 角色分离(split-db-llm-from-pg-ops task1)：DBLLM_TEST_PGUSER/PGPASSWORD
now resolve to the test-privilege role `dbllm_test` (tests/provision-test-db.sql §①b),
NOT the database owner `dbllm` — the owner keeps only its runtime identity and this repo's
tests no longer connect as it (test-db-provisioning R「契约测试以测试角色连接」). The
`dbllm_e2e` fixture this file reuses is now owned by `dbllm_test` too (§③), so the
verification queries below work unchanged.

Zero privileged SQL: this file never issues CREATE ROLE/CREATE USER/GRANT/REVOKE/
ALTER ROLE (CLAUDE.md「特权 SQL 边界」) and never mutates any object — it only
invokes pg-sql-check.sh (which itself only PREPAREs + ROLLBACKs, REQ-SC-7) and runs
read-only verification queries with the test-privilege role's credentials.

Fixture reuse, not creation: this test does NOT create its own throwaway schema.
It reuses the `dbllm_e2e` schema (tables `users`/`orders`) that
`tests/provision-test-db.sql` §③ provisions as a standing fixture, and the
`llm_readonly` read-only role that `/pg-readonly-setup` provisions against it
(same pair `run-e2e-smoke.sh` already uses via DBLLM_TEST_RO_USER/PASSWORD) —
provisioning a role is the one thing this repo's tests MUST NOT do themselves
(见 CLAUDE.md「特权 SQL 边界」), so a real read-only role can only be reused, not
minted per test run. If that standing fixture/role isn't there yet, this test
fails loud with a pointer to provision-test-db.sql / /pg-readonly-setup rather
than silently skipping (same fail-not-skip convention as
test_db_collect_contract.py).

Env contract (MUST NOT read generic PGHOST/PGPORT/... or RO_USER/RO_PASSWORD from
anywhere else — only these 7):
  DBLLM_TEST_PGHOST      DBLLM_TEST_PGPORT      DBLLM_TEST_PGDATABASE
  DBLLM_TEST_PGUSER      DBLLM_TEST_PGPASSWORD  (test-privilege role `dbllm_test`,
                                                        verification-only queries below)
  DBLLM_TEST_RO_USER     DBLLM_TEST_RO_PASSWORD (read-only role — the role
                                                        pg-sql-check.sh actually runs as)

Missing any of them is a hard Fail (not Skip), same reasoning as
test_db_collect_contract.py's module docstring.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PG_SQL_CHECK_SH = REPO_ROOT / "pg-sql-check" / "scripts" / "pg-sql-check.sh"

FIXTURE_SCHEMA = "dbllm_e2e"
FIXTURE_TABLE = f"{FIXTURE_SCHEMA}.users"

REQUIRED_ENV = [
    "DBLLM_TEST_PGHOST",
    "DBLLM_TEST_PGPORT",
    "DBLLM_TEST_PGDATABASE",
    "DBLLM_TEST_PGUSER",
    "DBLLM_TEST_PGPASSWORD",
    "DBLLM_TEST_RO_USER",
    "DBLLM_TEST_RO_PASSWORD",
]


def _require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.fail(
            "problem: 缺少契约测试专属环境变量 " + ", ".join(missing) + "\n"
            "cause: pg-sql-check 契约测试 MUST 只认 DBLLM_TEST_PG{HOST,PORT,DATABASE,"
            "USER,PASSWORD} + DBLLM_TEST_RO_{USER,PASSWORD} 这 7 个，不读通用 PGHOST 等\n"
            "fix: 在 tests/.env.test 补全这 7 个变量后重跑（DBLLM_TEST_RO_* 与 "
            "run-e2e-smoke.sh 共用同一对只读凭据，见 tests/.env.test.example）"
        )
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _priv_psql_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PGHOST"] = os.environ["DBLLM_TEST_PGHOST"]
    env["PGPORT"] = os.environ["DBLLM_TEST_PGPORT"]
    env["PGDATABASE"] = os.environ["DBLLM_TEST_PGDATABASE"]
    env["PGUSER"] = os.environ["DBLLM_TEST_PGUSER"]
    env["PGPASSWORD"] = os.environ["DBLLM_TEST_PGPASSWORD"]
    env["PGCONNECT_TIMEOUT"] = "10"
    env.pop("DATABASE_URL", None)
    return env


def _priv_psql(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["psql", "-At", "-X", "-q", "-v", "ON_ERROR_STOP=1", *args]
    result = subprocess.run(cmd, env=_priv_psql_env(), capture_output=True, text=True, timeout=30)
    if check and result.returncode != 0:
        pytest.fail(
            f"problem: 校验查询失败（{' '.join(args)}）\n"
            f"cause: 退出码 {result.returncode}\nstderr: {result.stderr}\n"
            "fix: 核对 DBLLM_TEST_PG* 指向的库是否可达"
        )
    return result


@pytest.fixture(scope="module", autouse=True)
def _require_standing_fixture():
    """Fail loud (not skip) if the standing dbllm_e2e fixture / read-only
    role this test reuses isn't provisioned yet — pointing at the two exact
    commands that provision it, rather than a generic "DB unreachable"."""
    _require_env()
    exists = _priv_psql(
        "-c",
        f"SELECT 1 FROM information_schema.tables "
        f"WHERE table_schema = '{FIXTURE_SCHEMA}' AND table_name = 'users'",
    )
    if not exists.stdout.strip():
        pytest.fail(
            f"problem: 标准 fixture {FIXTURE_TABLE} 不存在\n"
            "cause: 本测试复用 tests/provision-test-db.sql §③ 建的常驻 fixture，"
            "不自建自删（避免每次重新走一遍 /pg-readonly-setup 供给）\n"
            "fix: 人工执行 tests/provision-test-db.sql，再对 dbllm_e2e/dbllm_e2e_ext "
            "跑一遍 /pg-readonly-setup 供给只读角色后重跑本测试"
        )
    yield


@pytest.fixture()
def sc_root(tmp_path: Path) -> Path:
    """A throwaway consuming-project ROOT_DIR pointed at the real test DB
    through the DBLLM_TEST_RO_* read-only role — never the test-privilege role."""
    root = tmp_path / "project"
    (root / ".dbmeta" / FIXTURE_SCHEMA).mkdir(parents=True)
    config = "\n".join(
        [
            f"DB_HOST={os.environ['DBLLM_TEST_PGHOST']}",
            f"DB_PORT={os.environ['DBLLM_TEST_PGPORT']}",
            f"DB_NAME={os.environ['DBLLM_TEST_PGDATABASE']}",
            f"DB_USER={os.environ['DBLLM_TEST_RO_USER']}",
            f"DB_PASSWORD={os.environ['DBLLM_TEST_RO_PASSWORD']}",
            "",
        ]
    )
    (root / ".dbmeta" / ".dbllm.env").write_text(config)
    return root


def _run_check(root: Path, sql: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ROOT_DIR"] = str(root)
    result = subprocess.run(
        [str(PG_SQL_CHECK_SH), "--sql", sql],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result


def _latest_diag_json(root: Path) -> dict:
    result_dir = root / "build" / "pg-sql-check"
    files = sorted(result_dir.glob("*.json"))
    assert files, f"未在 {result_dir} 下找到诊断 JSON 产物"
    return json.loads(files[-1].read_text())


# ---------------------------------------------------------------------------
# Assertion 1 (REQ-SC-2): 只读角色下写语句校验 —— PREPARE 不要求执行期权限，
# 写语句拿到与读语句同等的 schema 级校验（错列名 -> 42703），不是权限错误 -> 42501。
# ---------------------------------------------------------------------------
def test_write_statement_under_readonly_role_gets_schema_diagnosis_not_permission_error(
    sc_root: Path,
):
    result = _run_check(
        sc_root, "INSERT INTO dbllm_e2e.users (id, bogus_col) VALUES ($1, $2)"
    )
    assert result.returncode == 4, (
        "预期退出码 4（校验不通过，SQLSTATE class 42），"
        f"实际 {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    doc = _latest_diag_json(sc_root)
    assert doc["sqlstate"] == "42703", (
        "只读角色（仅 USAGE+SELECT，无 INSERT 授权）下 PREPARE 一条写语句，"
        "报出的必须是未定义列错误（42703），而非权限不足（42501）——"
        f"证明 PREPARE 从不在分析期做执行期 ACL 检查。实际: {doc}"
    )


# ---------------------------------------------------------------------------
# Assertion 2 (REQ-SC-5): 契约快照 —— parameter_types / result_types 存在且类型正确，
# 且保留 null（DML 无结果列）与非空列表的区分。
# ---------------------------------------------------------------------------
def test_contract_snapshot_reflects_real_type_inference(sc_root: Path):
    # 读语句：非空 parameter_types 与非空 result_types。
    read_result = _run_check(
        sc_root, "SELECT id, email FROM dbllm_e2e.users WHERE id = $1"
    )
    assert read_result.returncode == 0, (
        f"预期校验通过\nstdout:\n{read_result.stdout}\nstderr:\n{read_result.stderr}"
    )
    read_doc = _latest_diag_json(sc_root)
    assert read_doc["parameter_types"] == ["bigint"]
    assert read_doc["result_types"] == ["bigint", "text"]

    # 写语句：非空 parameter_types，result_types 必须是 JSON null（不是 []，也不是缺字段）。
    write_result = _run_check(
        sc_root, "UPDATE dbllm_e2e.users SET email = $1 WHERE id = $2"
    )
    assert write_result.returncode == 0, (
        f"预期校验通过\nstdout:\n{write_result.stdout}\nstderr:\n{write_result.stderr}"
    )
    write_doc = _latest_diag_json(sc_root)
    assert write_doc["parameter_types"] == ["text", "bigint"]
    assert "result_types" in write_doc
    assert write_doc["result_types"] is None


# ---------------------------------------------------------------------------
# Assertion 3 (REQ-SC-7): 校验后库内零变更 —— 一条 schema 合法、若真执行会插入
# 一行的 INSERT，校验后该行必须不存在，行数必须不变。
# ---------------------------------------------------------------------------
def test_zero_db_change_after_validating_a_schema_valid_write_statement(sc_root: Path):
    probe_id = 987654321
    before_count = _priv_psql(
        "-c", f"SELECT count(*) FROM {FIXTURE_TABLE}"
    ).stdout.strip()
    before_row = _priv_psql(
        "-c", f"SELECT 1 FROM {FIXTURE_TABLE} WHERE id = {probe_id}"
    ).stdout.strip()
    assert before_row == "", "探针 id 在测试开始前就已存在，测试前提被破坏，换一个探针 id"

    result = _run_check(
        sc_root,
        f"INSERT INTO dbllm_e2e.users (id, email) VALUES ({probe_id}, 'probe@example.com')",
    )
    assert result.returncode == 0, (
        "该语句对真实 schema 合法，预期校验通过（即使只读角色本身没有 INSERT 授权 —— "
        f"PREPARE 不检查执行期权限）\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    after_count = _priv_psql(
        "-c", f"SELECT count(*) FROM {FIXTURE_TABLE}"
    ).stdout.strip()
    after_row = _priv_psql(
        "-c", f"SELECT 1 FROM {FIXTURE_TABLE} WHERE id = {probe_id}"
    ).stdout.strip()

    assert after_count == before_count, "校验后表行数发生了变化——本应零写入"
    assert after_row == "", "校验后探针行竟然存在——本应从未被 EXECUTE"
