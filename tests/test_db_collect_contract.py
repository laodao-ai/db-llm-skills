"""Structural contract test for shared/db-collect.sql's collect_version=1 JSON
output — PLUS relation-inference/relation-diagram integration assertions fed
by the same fixture schema.

Scope: the structural assertions stay purely structural — no project-specific
expectations. The new integration assertions below ARE allowed to reference
`dbllm_fixture`-specific table/column names because they exist precisely to
exercise render.py's relation-inference engine (infer_column_name_candidates /
infer_comment_ref_candidates / build_candidates / render_pending_sql /
render_relations_report) against a real, versioned schema — see
tests/fixtures/dbllm_fixture.sql for the full "pattern dictionary" this
schema documents.

This test is this repo's own self-contained guard: it points at an
already-running Postgres via DBLLM_TEST_PG* env vars (no docker, no
dependency on any consuming project's config) and
creates/drops its own throwaway fixture schema (`dbllm_fixture`) from
tests/fixtures/dbllm_fixture.sql.

角色分离：DBLLM_TEST_PGUSER/PGPASSWORD
resolve to the test-privilege role `dbllm_test` (tests/provision-test-db.sql §①b) —
NOT the database owner `dbllm`, which this repo's tests no longer connect as
(test-db-provisioning R「契约测试以测试角色连接」). This module already connected via the
generic `DBLLM_TEST_PGUSER` name for everything (fixture build/drop, collect, DDL
roundtrip), so no code change was needed here beyond this note — only the *value* behind
that env var changed, in tests/.env.test.example.

Env contract (MUST NOT read generic PGHOST/PGPORT/... — only these five):
  DBLLM_TEST_PGHOST
  DBLLM_TEST_PGPORT
  DBLLM_TEST_PGDATABASE
  DBLLM_TEST_PGUSER
  DBLLM_TEST_PGPASSWORD

Missing any of them is a hard Fail (not Skip, not a silent no-op) — a contract
guard that can quietly not run is worse than no guard (same reasoning as the
Go test it replaces, design.md failure-mode table: "pytest 无可用 PG" ->
"Fail 而非 Skip -- 引擎仓契约测试是唯一守护"). The `_fixture_lifecycle` module
fixture below fails via the same _pg_env() helper, so a missing env var fails
every test in this module (not a silent skip) exactly as before this file was
restructured to a module-scoped fixture load.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COLLECT_SQL = REPO_ROOT / "shared" / "db-collect.sql"
FIXTURE_SQL = REPO_ROOT / "tests" / "fixtures" / "dbllm_fixture.sql"

sys.path.insert(0, str(REPO_ROOT / "pg-dict" / "scripts"))
import render  # noqa: E402

REQUIRED_ENV = [
    "DBLLM_TEST_PGHOST",
    "DBLLM_TEST_PGPORT",
    "DBLLM_TEST_PGDATABASE",
    "DBLLM_TEST_PGUSER",
    "DBLLM_TEST_PGPASSWORD",
]

FIXTURE_SCHEMA = "dbllm_fixture"
FIXTURE_SCHEMA_EXT = "dbllm_fixture_ext"
# 契约测试 fixture 会建的全部 schema（第二个用于跨 schema 关系推导覆盖）。
ALL_FIXTURE_SCHEMAS = (FIXTURE_SCHEMA, FIXTURE_SCHEMA_EXT)


def _pg_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.fail(
            "problem: 缺少契约测试专属环境变量 " + ", ".join(missing) + "\n"
            "cause: 契约测试 MUST 只认 DBLLM_TEST_PG{HOST,PORT,DATABASE,USER,PASSWORD}，"
            "不读通用 PGHOST 等（防 shell 已导出值误命中，见 design.md M25 拍板）\n"
            "fix: 导出这 5 个变量指向一个已有的可写测试库后重跑"
        )
    env = dict(os.environ)
    env["PGHOST"] = os.environ["DBLLM_TEST_PGHOST"]
    env["PGPORT"] = os.environ["DBLLM_TEST_PGPORT"]
    env["PGDATABASE"] = os.environ["DBLLM_TEST_PGDATABASE"]
    env["PGUSER"] = os.environ["DBLLM_TEST_PGUSER"]
    env["PGPASSWORD"] = os.environ["DBLLM_TEST_PGPASSWORD"]
    env["PGCONNECT_TIMEOUT"] = "10"
    env.pop("DATABASE_URL", None)
    return env


def _psql(env: dict[str, str], *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["psql", "-At", "-X", "-q", "-v", "ON_ERROR_STOP=1", *args]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    if check and result.returncode != 0:
        pytest.fail(
            f"problem: psql 调用失败（{' '.join(args)}）\n"
            f"cause: 退出码 {result.returncode}\n"
            f"stderr: {result.stderr}\n"
            "fix: 核对 DBLLM_TEST_PG* 指向的库是否可写、可达"
        )
    return result


# Module-scoped fixture lifecycle (tasks.md Task 1.5: "setup_module 用 psql -f
# 加载 fixture、teardown_module DROP SCHEMA ... CASCADE") — loaded once, shared
# read-only by every test function below (none of them mutate the schema).
#
# NOTE (Task 5 双轴审 Important fix): this used to be plain xunit
# setup_module()/teardown_module(). Under pytest's xunit-style semantics, if
# setup_module() raises partway through (e.g. `psql -f FIXTURE_SQL` fails
# after creating some but not all objects), teardown_module() is NEVER
# called -- the half-created dbllm_fixture schema is left behind, and the
# next run's "schema 已存在" guard then fails loud requiring manual cleanup.
# A pytest fixture with try/finally around `yield` closes that gap: whatever
# happens between fixture setup and the `yield`, once we reach `yield` the
# `finally` block is guaranteed to run on teardown; and if the load itself
# raises, we drop the (possibly partial) schema before re-raising instead of
# leaving it behind. The pre-existing-schema fail-loud guard is unchanged.
_ENV: dict[str, str] = {}


def _drop_all_fixture_schemas(env: dict[str, str]) -> None:
    """Drop every schema the fixture creates (best-effort, no error check)."""
    for schema in ALL_FIXTURE_SCHEMAS:
        _psql(env, "-c", f"DROP SCHEMA IF EXISTS {schema} CASCADE", check=False)


@pytest.fixture(scope="module", autouse=True)
def _fixture_lifecycle():
    global _ENV
    _ENV = _pg_env()

    quoted = ", ".join(f"'{s}'" for s in ALL_FIXTURE_SCHEMAS)
    exists = _psql(
        _ENV,
        "-c",
        f"SELECT nspname FROM pg_namespace WHERE nspname IN ({quoted})",
    )
    if exists.stdout.strip():
        already = exists.stdout.strip().replace("\n", ", ")
        pytest.fail(
            f"problem: fixture schema 已存在于目标库：{already}\n"
            "cause: 契约测试的 fixture schema MUST 是自建自删的一次性对象，"
            "不能覆盖/借用已存在的同名 schema（可能是他人手工创建的残留）\n"
            "fix: 手工核对来源，确认可删后手动 `DROP SCHEMA <name> CASCADE` 再重跑本测试"
        )

    try:
        _psql(_ENV, "-f", str(FIXTURE_SQL))
    except BaseException:
        # setup failed partway through -- drop whatever got created (across BOTH
        # schemas) so the next run's pre-existing-schema guard above doesn't trip
        # on our own leftovers (pytest.fail() raises a BaseException, not
        # Exception, so this must catch BaseException to see it).
        _drop_all_fixture_schemas(_ENV)
        raise

    try:
        yield
    finally:
        for schema in ALL_FIXTURE_SCHEMAS:
            teardown = _psql(_ENV, "-c", f"DROP SCHEMA {schema} CASCADE", check=False)
            if teardown.returncode != 0:
                print(
                    "\n[WARN] contract test teardown 失败，请手工执行:\n"
                    f"  DROP SCHEMA {schema} CASCADE;\n"
                    f"stderr: {teardown.stderr}"
                )


def _collect_raw(pg_env: dict[str, str], schemas_csv: str = FIXTURE_SCHEMA) -> str:
    result = _psql(
        pg_env,
        "-v",
        f"schemas_csv={schemas_csv}",
        "-c",
        "SET statement_timeout = '60s'",
        "-c",
        "SET lock_timeout = '10s'",
        "-f",
        str(COLLECT_SQL),
    )
    return result.stdout


def _collect(pg_env: dict[str, str]) -> dict:
    return json.loads(_collect_raw(pg_env))


# Minor fix (Task 5 双轴审): the structural + relation-inference tests below
# are all read-only over the same collect output (none of them mutate the
# fixture schema or depend on collect ordering relative to each other), so a
# single module-scoped collect is shared instead of every test function
# re-running its own `psql -f db-collect.sql` against the live database.
# `raw` stays a fixture (not just a plain module attribute) so tests that
# feed it straight into render.run() (which reads the string, not a dict)
# keep doing so without a redundant json.dumps/json.loads round-trip.
@pytest.fixture(scope="module")
def collect_raw(_fixture_lifecycle) -> str:
    return _collect_raw(_ENV)


@pytest.fixture(scope="module")
def collect_doc(collect_raw) -> dict:
    return json.loads(collect_raw)


# ---------------------------------------------------------------------------
# Original structural contract assertions (unchanged in substance — only the
# fixture-loading mechanism moved from a per-function inline-DDL fixture to
# the module-scoped tests/fixtures/dbllm_fixture.sql load above).
# ---------------------------------------------------------------------------


def test_collect_version_is_1(collect_doc):
    doc = collect_doc
    assert doc["collect_version"] == 1


def test_requested_schemas_reflects_the_scope_filter(collect_doc):
    doc = collect_doc
    assert doc["requested_schemas"] == [FIXTURE_SCHEMA]


def test_schema_has_tables_views_functions_as_lists(collect_doc):
    doc = collect_doc
    schemas = {s["schema"]: s for s in doc["schemas"]}
    assert FIXTURE_SCHEMA in schemas
    schema = schemas[FIXTURE_SCHEMA]
    for key in ("tables", "views", "functions"):
        assert key in schema, f"schema 必须含 {key} 键"
        assert isinstance(schema[key], list), f"{key} 必须是数组"
    # users, categories, products, orders, order_items, payments, audit_logs,
    # events, events_2026_08 == 9 tables (see tests/fixtures/dbllm_fixture.sql)
    assert len(schema["tables"]) >= 9
    assert len(schema["views"]) >= 1
    assert len(schema["functions"]) >= 1


def test_every_table_has_the_four_subset_keys_as_lists(collect_doc):
    doc = collect_doc
    schemas = {s["schema"]: s for s in doc["schemas"]}
    schema = schemas[FIXTURE_SCHEMA]
    for table in schema["tables"]:
        for key in ("columns", "indexes", "constraints", "triggers"):
            assert key in table, f"{table['name']}.{key} 键缺失"
            assert isinstance(table[key], list), f"{table['name']}.{key} 必须是数组"


def test_partitioned_table_partition_of_points_to_parent_in_same_result(collect_doc):
    doc = collect_doc
    schemas = {s["schema"]: s for s in doc["schemas"]}
    schema = schemas[FIXTURE_SCHEMA]
    tables_by_name = {t["name"]: t for t in schema["tables"]}

    parent = tables_by_name["events"]
    child = tables_by_name["events_2026_08"]

    assert parent.get("partition_of") is None
    assert child["partition_of"] == "events"
    # 分区父表在结果内 -> partition_of 指向的名字必须能在同一 schema 的
    # tables[] 里解析回一个真实条目（不是悬空引用）。
    assert child["partition_of"] in tables_by_name


def test_view_has_columns_list(collect_doc):
    doc = collect_doc
    schemas = {s["schema"]: s for s in doc["schemas"]}
    schema = schemas[FIXTURE_SCHEMA]
    view = next(v for v in schema["views"] if v["name"] == "active_users")
    assert isinstance(view["columns"], list)
    assert len(view["columns"]) == 2


def test_function_has_definition_and_source(collect_doc):
    doc = collect_doc
    schemas = {s["schema"]: s for s in doc["schemas"]}
    schema = schemas[FIXTURE_SCHEMA]
    fn = next(f for f in schema["functions"] if f["name"] == "plain_fn")
    assert isinstance(fn["definition"], str) and fn["definition"].startswith(
        "CREATE OR REPLACE FUNCTION"
    )
    assert "source" in fn


# ---------------------------------------------------------------------------
# Relation-inference / relation-diagram integration assertions
# (relation-inference-and-diagram change, Task 5 / tasks.md Task 1.5).
#
# Expected pairs below are derived independently from design.md DD-2/DD-3 and
# specs/relation-inference/spec.md REQ-RI-1/2/3 (and specs/relation-diagram
# REQ-RD-1/2) applied by hand to tests/fixtures/dbllm_fixture.sql's DDL —
# NOT by running render.py and copying its output (that would be a
# tautological/implementation-coupled assertion, exactly what the TDD
# contract for this ticket forbids).
# ---------------------------------------------------------------------------


def test_candidates_include_column_name_inference(collect_doc):
    doc = collect_doc
    candidates = render.build_candidates(doc["schemas"], confirmed=[])
    pairs = {(c["source"], c["target"]) for c in candidates}
    # DD-2: orders.user_id, stem "user" + "s" == "users", users has "id".
    assert (
        "dbllm_fixture.orders.user_id",
        "dbllm_fixture.users.id",
    ) in pairs


def test_candidates_include_comment_ref_inference(collect_doc):
    doc = collect_doc
    candidates = render.build_candidates(doc["schemas"], confirmed=[])
    pairs = {(c["source"], c["target"]) for c in candidates}
    # DD-3 rule 1: audit_logs.ref_id's COMMENT fully-qualifies
    # "dbllm_fixture.orders"; target column fixed at "id".
    assert (
        "dbllm_fixture.audit_logs.ref_id",
        "dbllm_fixture.orders.id",
    ) in pairs


def test_candidates_exclude_already_annotated_pair(collect_doc):
    doc = collect_doc
    candidates = render.build_candidates(doc["schemas"], confirmed=[])
    pairs = {(c["source"], c["target"]) for c in candidates}
    # payments.order_id's COMMENT already carries 逻辑关联 dbllm_fixture.orders.id
    # (REQ-RI-3) -- MUST NOT resurface even though the column-name signal alone
    # would otherwise match it (same shape as order_items.order_id -> orders.id).
    assert (
        "dbllm_fixture.payments.order_id",
        "dbllm_fixture.orders.id",
    ) not in pairs


def test_categories_self_reference_handled_correctly(collect_doc):
    doc = collect_doc
    candidates = render.build_candidates(doc["schemas"], confirmed=[])
    pairs = {(c["source"], c["target"]) for c in candidates}
    # DD-2's stem rule (exact / "+s" only) cannot match categories.parent_id
    # back to "categories" (irregular plural) -- the self-reference is
    # produced instead via DD-3's bare-table-name COMMENT signal ("categories"
    # is a unique table name across all schemas). This is the "自引用处理
    # 正确" acceptance case: source != target (parent_id vs id), so it must
    # NOT be caught by the column-name signal's degenerate self-loop filter.
    assert (
        "dbllm_fixture.categories.parent_id",
        "dbllm_fixture.categories.id",
    ) in pairs


def test_created_by_user_id_not_inferred(collect_doc):
    doc = collect_doc
    candidates = render.build_candidates(doc["schemas"], confirmed=[])
    sources = {c["source"] for c in candidates}
    # DD-2: stem = "created_by_user_id"[:-3] = "created_by_user" -- no table
    # named "created_by_user" or "created_by_users" exists -> no candidate at
    # all for this column, from either signal.
    assert "dbllm_fixture.orders.created_by_user_id" not in sources


def test_updated_by_not_considered_by_column_name_signal(collect_doc):
    doc = collect_doc
    candidates = render.infer_column_name_candidates(doc["schemas"])
    sources = {c["source"] for c in candidates}
    # DD-2's column-name signal only ever looks at columns ending in "_id" --
    # "updated_by" does not, so it can never appear as a source here.
    assert "dbllm_fixture.orders.updated_by" not in sources


def test_relations_md_contains_mermaid_for_annotated_pair(collect_raw, tmp_path):
    raw = collect_raw
    dbmeta_dir = tmp_path / ".dbmeta"
    render.run(raw, dbmeta_dir)

    content = (dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
    assert "```mermaid" in content
    assert "erDiagram" in content
    # REQ-RD-2: target ("one" side, orders) on the left, source ("many" side,
    # payments) on the right, schema.table -> schema_table.
    assert (
        'dbllm_fixture_orders ||--o{ dbllm_fixture_payments : "order_id"'
        in content
    )


def test_pending_sql_reflects_confirmed_entry(collect_raw, tmp_path):
    dbmeta_dir = tmp_path / ".dbmeta"
    dbmeta_dir.mkdir(parents=True)
    (dbmeta_dir / "_relations.confirmed.yaml").write_text(
        "# .dbmeta/_relations.confirmed.yaml\n"
        "- source: dbllm_fixture.orders.user_id\n"
        "  target: dbllm_fixture.users.id\n",
        encoding="utf-8",
    )

    render.run(collect_raw, dbmeta_dir)

    content = (dbmeta_dir / "_relations.pending.sql").read_text(encoding="utf-8")
    # orders.user_id carries no existing COMMENT (see fixture SQL) -- DD-6's
    # append rule degenerates to just the new 逻辑关联 annotation, no leading
    # existing-comment text or separating space.
    assert (
        "COMMENT ON COLUMN dbllm_fixture.orders.user_id "
        "IS '逻辑关联 dbllm_fixture.users.id';" in content
    )


def test_regen_is_idempotent_across_relation_files(collect_raw, tmp_path):
    dbmeta_dir = tmp_path / ".dbmeta"
    dbmeta_dir.mkdir(parents=True)
    (dbmeta_dir / "_relations.confirmed.yaml").write_text(
        "# .dbmeta/_relations.confirmed.yaml\n"
        "- source: dbllm_fixture.orders.user_id\n"
        "  target: dbllm_fixture.users.id\n",
        encoding="utf-8",
    )

    raw = collect_raw
    render.run(raw, dbmeta_dir)

    relation_files = [
        "_relations.candidates.yaml",
        "_relations.pending.sql",
        "_relations.md",
    ]
    first_pass = {name: (dbmeta_dir / name).read_bytes() for name in relation_files}

    render.run(raw, dbmeta_dir)

    second_pass = {name: (dbmeta_dir / name).read_bytes() for name in relation_files}
    for name in relation_files:
        assert first_pass[name] == second_pass[name], f"{name} 两次 regen 不是逐字节一致"


# ---------------------------------------------------------------------------
# Extended object-shape coverage (added when formalizing the devenv test
# strategy): three real-catalog shapes that render_test.py exercises only with
# synthetic JSON, and that the base fixture above did not exercise against a
# live pg_catalog -- so the collect->render contract for them was previously
# unverified end-to-end (both layers green could still let a real-shape bug
# through). See tests/fixtures/dbllm_fixture.sql's fmt / recent_orders /
# dbllm_fixture_ext blocks.
# ---------------------------------------------------------------------------


def test_materialized_view_collected_with_kind(collect_doc):
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    mv = next(v for v in schema["views"] if v["name"] == "recent_orders")
    # db-collect.sql: views[].kind == 'materialized_view' for relkind='m'
    # (render_view_ddl then emits MATERIALIZED VIEW instead of VIEW).
    assert mv["kind"] == "materialized_view"
    assert isinstance(mv["columns"], list) and len(mv["columns"]) == 3


def test_view_options_sorted_and_empty_default(collect_doc):
    # view-reloptions-ddl T1: views[].options is pg_class.reloptions verbatim,
    # one "name=value" string per element, sorted by element text -- and this
    # MUST be independent of the WITH-clause's written order (guarded_users is
    # written as security_barrier, check_option; the sorted output flips that).
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    views_by_name = {v["name"]: v for v in schema["views"]}
    guarded = views_by_name["guarded_users"]
    assert guarded["options"] == ["check_option=cascaded", "security_barrier=true"]
    # Views/matviews with no storage options set -> [] (never null, never a
    # missing key).
    assert views_by_name["active_users"]["options"] == []
    # recent_orders (relation-ddl-equivalence-r2 T1): WITH (toast.autovacuum_enabled
    # = false) lands in the materialized view's toast relation's own reloptions,
    # merged into options with a "toast." prefix (decision-memo C1/D4).
    assert views_by_name["recent_orders"]["options"] == ["toast.autovacuum_enabled=false"]
    # Adding a key does not bump collect_version (design.md Global Constraint).
    assert doc["collect_version"] == 1


def test_table_options_sorted_and_empty_default(collect_doc):
    # relation-ddl-equivalence T1: tables[].options is pg_class.reloptions
    # verbatim, same shape as views[].options -- sorted by element text,
    # independent of the WITH-clause's written order (tuned_counters is
    # written as fillfactor, autovacuum_enabled; sorted output flips that).
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    tables_by_name = {t["name"]: t for t in schema["tables"]}
    tuned = tables_by_name["tuned_counters"]
    assert tuned["options"] == ["autovacuum_enabled=false", "fillfactor=70"]
    # Ordinary tables with no storage params set -> [] (never null, never a
    # missing key); a partitioned parent table has [] too (PG allows no
    # storage params on a partition parent).
    assert tables_by_name["users"]["options"] == []
    assert tables_by_name["events"]["options"] == []
    assert doc["collect_version"] == 1


def test_toast_reloptions_merged_into_options_with_prefix(collect_doc):
    # relation-ddl-equivalence-r2 T1 (tasks.md 1.1/1.5): tables[].options /
    # views[].options is the main relation's reloptions UNION the relation's
    # own toast relation's reloptions (pg_class.reltoastrelid), each toast
    # element prefixed "toast.", the whole set sorted by element text.
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    tables_by_name = {t["name"]: t for t in schema["tables"]}
    views_by_name = {v["name"]: v for v in schema["views"]}

    # toasted_notes: only a toast.* param -> options is exactly that one item.
    assert tables_by_name["toasted_notes"]["options"] == ["toast.autovacuum_enabled=false"]
    # events_2026_08: a partition child's OWN reloptions (no toast relation on
    # this table -> no toast.* item), same shape as any other table.options.
    assert tables_by_name["events_2026_08"]["options"] == ["fillfactor=60"]
    # tuned_counters: has main-relation reloptions but NO toast relation (no
    # toastable column) -> options unchanged, no toast.* item leaks in.
    assert tables_by_name["tuned_counters"]["options"] == [
        "autovacuum_enabled=false",
        "fillfactor=70",
    ]
    # recent_orders (materialized view): toast.* item only, verified precisely
    # in test_view_options_sorted_and_empty_default above; re-asserted here to
    # keep every "toast merge" anchor colocated.
    assert views_by_name["recent_orders"]["options"] == ["toast.autovacuum_enabled=false"]
    assert doc["collect_version"] == 1


def test_access_method_present_and_null_shape(collect_doc):
    # relation-ddl-equivalence-r2 T1 (tasks.md 1.2/1.5): tables[]/views[].
    # access_method is pg_am.amname (relam=0 -> null). The fixture DB has no
    # non-heap access method available (decision-memo C4), so every ordinary
    # table / partition child / materialized view is "heap"; the partition
    # PARENT and every ordinary view have no explicit AM -> null.
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    tables_by_name = {t["name"]: t for t in schema["tables"]}
    views_by_name = {v["name"]: v for v in schema["views"]}

    for name in ("users", "tuned_counters", "toasted_notes", "events_2026_08"):
        assert tables_by_name[name]["access_method"] == "heap", name
    # Partition parent tables never carry an explicit AM (relam=0).
    assert tables_by_name["events"]["access_method"] is None
    for name in ("recent_orders", "stale_orders"):
        assert views_by_name[name]["access_method"] == "heap", name
    for name in ("active_users", "guarded_users"):
        assert views_by_name[name]["access_method"] is None, name
    assert doc["collect_version"] == 1


def test_foreign_and_fdw_options_null_for_non_fdw_objects(collect_doc):
    # relation-ddl-equivalence-r2 T1 (tasks.md 1.3/1.5): the fixture DB has no
    # FDW (decision-memo C4) -- every table's "foreign" key is null and every
    # column's "fdw_options" key is []. This also proves the new foreign /
    # fdw_options subqueries are syntactically valid against a real catalog
    # (tasks.md 4.2's "全部表 foreign is None 证明语法可执行" anchor).
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    for table in schema["tables"]:
        assert "foreign" in table, f"{table['name']}.foreign 键缺失"
        assert table["foreign"] is None, table["name"]
        for col in table["columns"]:
            assert "fdw_options" in col, f"{table['name']}.{col['name']}.fdw_options 键缺失"
            assert col["fdw_options"] == [], f"{table['name']}.{col['name']}"
    assert doc["collect_version"] == 1


def test_index_options_on_constraint_backed_and_plain_index(collect_doc):
    # relation-ddl-equivalence T1 [spec-review-amendment Q2 拍板]: index-level
    # reloptions, same shape, on both a constraint-backed index (PRIMARY KEY
    # WITH (fillfactor=90)) and a plain non-unique index (no options -> []).
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    tuned = next(t for t in schema["tables"] if t["name"] == "tuned_counters")
    indexes_by_name = {i["name"]: i for i in tuned["indexes"]}
    assert indexes_by_name["tuned_counters_pkey"]["options"] == ["fillfactor=90"]
    assert indexes_by_name["tuned_counters_hits_idx"]["options"] == []


def test_materialized_view_populated_and_indexes(collect_doc):
    # relation-ddl-equivalence T1: views[].populated is relispopulated
    # verbatim; views[].indexes is the same shape as tables[].indexes minus
    # inherited_from, [] for ordinary views (matviews can't be partitions).
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    views_by_name = {v["name"]: v for v in schema["views"]}

    stale = views_by_name["stale_orders"]
    assert stale["populated"] is False
    assert len(stale["indexes"]) == 1
    idx = stale["indexes"][0]
    assert idx["name"] == "stale_orders_id_key"
    assert idx["definition"].startswith(
        "CREATE UNIQUE INDEX stale_orders_id_key ON dbllm_fixture.stale_orders"
    )

    recent = views_by_name["recent_orders"]
    assert recent["populated"] is True
    assert recent["indexes"] == []

    # Any ordinary (non-materialized) view: populated is always true, indexes
    # always [] -- exercised on active_users (no options set) here.
    active = views_by_name["active_users"]
    assert active["populated"] is True
    assert active["indexes"] == []


def test_tablespace_key_present_and_null_on_default_tablespace(collect_doc):
    # relation-ddl-equivalence T1: reltablespace=0 (database default, the only
    # tablespace the contract test account/DB have) -> tablespace is None on
    # every table, view, and index entry (never a missing key).
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    for table in schema["tables"]:
        assert "tablespace" in table, f"{table['name']}.tablespace 键缺失"
        assert table["tablespace"] is None
        for idx in table["indexes"]:
            assert "tablespace" in idx, f"{table['name']}.indexes[{idx['name']}].tablespace 键缺失"
            assert idx["tablespace"] is None
    for view in schema["views"]:
        assert "tablespace" in view, f"{view['name']}.tablespace 键缺失"
        assert view["tablespace"] is None
        for idx in view["indexes"]:
            assert "tablespace" in idx, f"{view['name']}.indexes[{idx['name']}].tablespace 键缺失"
            assert idx["tablespace"] is None
    assert doc["collect_version"] == 1


def test_function_overloads_collected_as_separate_entries(collect_doc):
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    # fmt has two overloads (integer / text) -> real pg_catalog emits each as a
    # separate pg_proc row, so functions[] MUST carry two entries named "fmt"
    # (render.py then renders them as one file with one block per overload).
    fmt_overloads = [f for f in schema["functions"] if f["name"] == "fmt"]
    assert len(fmt_overloads) == 2
    for fn in fmt_overloads:
        assert isinstance(fn["definition"], str) and fn["definition"].startswith(
            "CREATE OR REPLACE FUNCTION"
        )


def test_cross_schema_comment_ref_inference():
    # Collect BOTH fixture schemas so the cross-schema COMMENT reference can
    # resolve (the default single-schema collect_raw never sees widgets).
    doc = json.loads(
        _collect_raw(_ENV, schemas_csv=f"{FIXTURE_SCHEMA},{FIXTURE_SCHEMA_EXT}")
    )
    candidates = render.build_candidates(doc["schemas"], confirmed=[])
    pairs = {(c["source"], c["target"]) for c in candidates}
    # DD-3: widgets.order_id's COMMENT fully-qualifies "dbllm_fixture.orders"
    # -- a table in the OTHER schema; target column fixed at "id". This only
    # resolves because the collect scope spans both schemas.
    assert (
        "dbllm_fixture_ext.widgets.order_id",
        "dbllm_fixture.orders.id",
    ) in pairs


# ---------------------------------------------------------------------------
# Table-subset shapes the base fixture never exercised against a live catalog
# (spec-review cold-lens HIGH): secondary/partial indexes, CHECK constraints,
# triggers, and inherited (non-local) constraints on a partition child. Without
# these, db-collect.sql's pg_get_indexdef / contype='c' / pg_get_triggerdef /
# conislocal branches ran on no matching data, and
# test_every_table_has_the_four_subset_keys_as_lists passed trivially on empty
# lists (a false green). These live on the `metrics` table + the `events` CHECK.
# ---------------------------------------------------------------------------


def test_secondary_and_partial_index_collected(collect_doc):
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    metrics = {t["name"]: t for t in schema["tables"]}["metrics"]
    idx_defs = [i.get("definition", "") for i in metrics["indexes"]]
    # 二级唯一索引：pg_get_indexdef 出的 definition 是真的 UNIQUE INDEX
    assert any("metrics_name_key" in d and "UNIQUE INDEX" in d for d in idx_defs), \
        f"二级唯一索引未采集或定义不对：{idx_defs}"
    # 部分索引：WHERE 谓词必须在 definition 里
    assert any("WHERE" in d for d in idx_defs), f"部分索引的 WHERE 谓词未采集：{idx_defs}"


def test_check_constraint_collected(collect_doc):
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    metrics = {t["name"]: t for t in schema["tables"]}["metrics"]
    checks = [c for c in metrics["constraints"] if c.get("type") == "c"]
    assert checks, f"CHECK 约束 (contype='c') 未采集：{metrics['constraints']}"
    # metrics 上的 CHECK 是本地定义的 -> is_local True
    assert all(c.get("is_local") for c in checks)


def test_invalid_index_is_not_collected(_fixture_lifecycle):
    """[T45⑤] pg_index.indisvalid=false (a failed CREATE INDEX CONCURRENTLY
    leftover) must not be collected — replaying it as DDL would recreate an
    index the source database itself cannot use.

    The fixture SQL loads under ON_ERROR_STOP, so the invalid index is built
    here: a unique index built CONCURRENTLY against duplicate rows fails and
    leaves the index behind with indisvalid=false. Scratch table + index are
    dropped in `finally` so the module-scoped fixture stays untouched."""
    table = f"{FIXTURE_SCHEMA}.invalid_idx_probe"
    index = "invalid_idx_probe_dup_uidx"
    try:
        _psql(_ENV, "-c", f"CREATE TABLE {table} (v int)")
        _psql(_ENV, "-c", f"INSERT INTO {table} VALUES (1), (1)")
        # CONCURRENTLY cannot run inside a transaction block; `-c` with a
        # single statement is autocommit, so this is fine. Expected to fail.
        failed = _psql(
            _ENV, "-c",
            f"CREATE UNIQUE INDEX CONCURRENTLY {index} ON {table} (v)",
            check=False,
        )
        assert failed.returncode != 0, "唯一索引撞重复值本应失败（用于制造 invalid 索引）"
        probe = _psql(
            _ENV, "-c",
            f"SELECT indisvalid FROM pg_index WHERE indexrelid = '{FIXTURE_SCHEMA}.{index}'::regclass",
        )
        assert probe.stdout.strip() == "f", f"制造 invalid 索引失败：indisvalid={probe.stdout!r}"

        doc = _collect(_ENV)
        schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
        tbl = {t["name"]: t for t in schema["tables"]}["invalid_idx_probe"]
        names = [i["name"] for i in tbl["indexes"]]
        assert index not in names, f"indisvalid=false 的索引被当正常索引采集：{names}"
    finally:
        _psql(_ENV, "-c", f"DROP TABLE IF EXISTS {table}", check=False)


def test_trigger_collected_with_definition(collect_doc):
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    metrics = {t["name"]: t for t in schema["tables"]}["metrics"]
    assert len(metrics["triggers"]) >= 1, "触发器未采集（tgisinternal 之外应有一条用户触发器）"
    assert "CREATE TRIGGER" in metrics["triggers"][0].get("definition", "")


def test_inherited_check_on_partition_child_marked_not_local(collect_doc):
    doc = collect_doc
    schema = {s["schema"]: s for s in doc["schemas"]}[FIXTURE_SCHEMA]
    child = {t["name"]: t for t in schema["tables"]}["events_2026_08"]
    # 分区子表从父表继承的 CHECK：is_local == False（conislocal AND coninhcount=0 => False）。
    inherited = [
        c for c in child["constraints"]
        if c.get("type") == "c" and not c.get("is_local", True)
    ]
    assert inherited, f"分区子表继承的 CHECK 未标 is_local=False（Dec-1 分支）：{child['constraints']}"


# ---------------------------------------------------------------------------
# [T41] Roundtrip lane: the generated .dbmeta/<schema>/{views,functions}/*.sql
# files are executable DDL (pg-dict spec 「字典生成内容与落点」) — prove it by
# feeding them back into the live fixture database and comparing the rebuilt
# object's catalog identity with the original. This is the automated anchor for
# the two Scenarios that previously only had a manual record (decision-memo C1):
# 「带选项视图可执行且等价」 and 「函数文件可直接执行」.
#
# Zero privileged SQL: only DROP/CREATE of objects inside the throwaway
# dbllm_fixture schema the contract account already owns (CLAUDE.md 特权 SQL
# 边界 — no CREATE ROLE / GRANT / setup.sql here, ever).
#
# These two tests are the only ones in this module that mutate the live schema
# (drop + recreate). They recreate each object from the generated file with the
# same identity, so the module-scoped collect_doc other tests read stays valid;
# the module fixture drops the whole schema afterwards regardless.
# ---------------------------------------------------------------------------

_ROUNDTRIP_VIEWS = (
    ("guarded_users", "VIEW"),            # options set: security_barrier + check_option
    ("active_users", "VIEW"),             # no options -> file has no WITH clause
    ("recent_orders", "MATERIALIZED VIEW"),
    ("stale_orders", "MATERIALIZED VIEW"),  # relation-ddl-equivalence T3.1: unpopulated + its own index
)
_ROUNDTRIP_FUNCTIONS = ("fmt", "plain_fn")  # fmt = two overloads in one file
_ROUNDTRIP_TABLES = (
    ("tuned_counters", "TABLE"),  # relation-ddl-equivalence T3.2: storage params + tablespace + indexes + constraints
    ("toasted_notes", "TABLE"),  # relation-ddl-equivalence-r2 T3.1: toast.* reloptions roundtrip
)


# relation-ddl-equivalence T3.1 [spec-review-amendment]: pg_get_indexdef never
# emits TABLESPACE, so comparing indexdef text alone is blind to an index
# living in a non-default tablespace -- pair each indexdef with its own
# COALESCE(tablespace name, '') before sorting/comparing the set.
_INDEX_DEFS_WITH_TABLESPACE_SUBQUERY = (
    "(SELECT coalesce(array_agg(pg_get_indexdef(i.indexrelid) || '|' || coalesce(ts.spcname, '')"
    " ORDER BY pg_get_indexdef(i.indexrelid))::text, '{}')"
    " FROM pg_index i"
    " LEFT JOIN pg_class ic ON ic.oid = i.indexrelid"
    " LEFT JOIN pg_tablespace ts ON ts.oid = ic.reltablespace"
    " WHERE i.indrelid = pg_class.oid)"
)


# relation-ddl-equivalence-r2 T3.1: reloptions comparison is extended from
# "this relation's own reloptions" to "this relation's own reloptions UNION
# its TOAST relation's reloptions prefixed with toast." -- the exact same
# UNION shape shared/db-collect.sql's tables[]/views[] `options` subquery uses
# (tasks.md 1.1), so the roundtrip identity check stays blind to whether a
# toast.* option survived the drop/regenerate/reload cycle only if the
# collect-side merge and this comparison agree on what counts as "the same
# set of options". `pg_class` here is the outer query's unqualified range
# var (same technique the pre-existing indexdef/tablespace subquery already
# uses to reach the outer row from inside a correlated subquery).
_RELOPTIONS_WITH_TOAST_SUBQUERY = (
    "coalesce((SELECT array_agg(o ORDER BY o)::text FROM ("
    "     SELECT o FROM unnest(reloptions) AS o"
    "     UNION ALL"
    "     SELECT 'toast.' || o FROM pg_class tc, unnest(tc.reloptions) AS o"
    "     WHERE tc.oid = pg_class.reltoastrelid"
    "   ) AS u), '{}')"
)


def _view_identity(qualified: str) -> str:
    # reloptions compared as a sorted set (逐项相等): PG stores them in CREATE
    # write order, and the generated file deliberately writes them sorted.
    # relation-ddl-equivalence T3.1: extended with relispopulated, the view's
    # own tablespace name, and the indexdef/tablespace pair set (materialized
    # view indexes) ahead of viewdef.
    # relation-ddl-equivalence-r2 T3.1: reloptions widened to the toast-union
    # set (see _RELOPTIONS_WITH_TOAST_SUBQUERY) and access_method appended.
    return _psql(
        _ENV, "-c",
        f"SELECT {_RELOPTIONS_WITH_TOAST_SUBQUERY}"
        " || E'\\n' || coalesce((SELECT amname FROM pg_am WHERE oid = NULLIF(relam, 0)), '')"
        " || E'\\n' || relispopulated::text"
        " || E'\\n' || coalesce((SELECT spcname FROM pg_tablespace WHERE oid = reltablespace), '')"
        f" || E'\\n' || {_INDEX_DEFS_WITH_TABLESPACE_SUBQUERY}"
        f" || E'\\n' || pg_get_viewdef(oid, true) FROM pg_class WHERE oid = '{qualified}'::regclass",
    ).stdout


def _table_identity(qualified: str) -> str:
    # relation-ddl-equivalence T3.2: reloptions (sorted set) + table's own
    # tablespace name + columns (name/type/attnotnull ordered by attnum,
    # NOT sorted -- column order is part of a table's identity) + the same
    # indexdef/tablespace pair set as _view_identity + a sorted
    # pg_get_constraintdef set (order-independent: constraints have no
    # meaningful ordering).
    # relation-ddl-equivalence-r2 T3.1: reloptions widened to the toast-union
    # set (see _RELOPTIONS_WITH_TOAST_SUBQUERY) and access_method appended.
    return _psql(
        _ENV, "-c",
        f"SELECT {_RELOPTIONS_WITH_TOAST_SUBQUERY}"
        " || E'\\n' || coalesce((SELECT amname FROM pg_am WHERE oid = NULLIF(relam, 0)), '')"
        " || E'\\n' || coalesce((SELECT spcname FROM pg_tablespace WHERE oid = reltablespace), '')"
        " || E'\\n' || (SELECT coalesce(array_agg("
        "     a.attname || ':' || format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull::text"
        "     ORDER BY a.attnum"
        "   )::text, '{}')"
        "   FROM pg_attribute a"
        "   WHERE a.attrelid = pg_class.oid AND a.attnum > 0 AND NOT a.attisdropped)"
        f" || E'\\n' || {_INDEX_DEFS_WITH_TABLESPACE_SUBQUERY}"
        " || E'\\n' || coalesce((SELECT array_agg(pg_get_constraintdef(c.oid) ORDER BY pg_get_constraintdef(c.oid))::text"
        "   FROM pg_constraint c WHERE c.conrelid = pg_class.oid), '{}')"
        f" FROM pg_class WHERE oid = '{qualified}'::regclass",
    ).stdout


def _function_identity(name: str) -> str:
    return _psql(
        _ENV, "-c",
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        f" WHERE n.nspname = '{FIXTURE_SCHEMA}' AND p.proname = '{name}'"
        " ORDER BY pg_get_function_identity_arguments(p.oid)",
    ).stdout


def _function_signatures(name: str) -> list[str]:
    out = _psql(
        _ENV, "-c",
        "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        f" WHERE n.nspname = '{FIXTURE_SCHEMA}' AND p.proname = '{name}'",
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def test_generated_view_files_roundtrip_into_live_db(collect_raw, tmp_path):
    dbmeta_dir = tmp_path / ".dbmeta"
    render.run(collect_raw, dbmeta_dir)
    for name, kind in _ROUNDTRIP_VIEWS:
        qualified = f"{FIXTURE_SCHEMA}.{name}"
        generated = dbmeta_dir / FIXTURE_SCHEMA / "views" / f"{name}.sql"
        assert generated.is_file(), f"生成文件缺失：{generated}"
        before = _view_identity(qualified)
        _psql(_ENV, "-c", f"DROP {kind} {qualified}")
        _psql(_ENV, "-f", str(generated))  # ON_ERROR_STOP=1: any SQL error fails here
        after = _view_identity(qualified)
        assert after == before, (
            f"{qualified} 经生成文件重建后与原对象不等价（reloptions 或 viewdef 变了）：\n"
            f"--- before ---\n{before}\n--- after ---\n{after}"
        )


def test_generated_function_files_roundtrip_into_live_db(collect_raw, tmp_path):
    dbmeta_dir = tmp_path / ".dbmeta"
    render.run(collect_raw, dbmeta_dir)
    for name in _ROUNDTRIP_FUNCTIONS:
        generated = dbmeta_dir / FIXTURE_SCHEMA / "functions" / f"{name}.sql"
        assert generated.is_file(), f"生成文件缺失：{generated}"
        before = _function_identity(name)
        signatures = _function_signatures(name)
        assert signatures, f"夹具里找不到函数 {FIXTURE_SCHEMA}.{name}"
        _psql(_ENV, "-c", "DROP FUNCTION " + ", ".join(signatures))
        _psql(_ENV, "-f", str(generated))
        after = _function_identity(name)
        assert after == before, (
            f"{FIXTURE_SCHEMA}.{name} 经生成文件重建后 pg_get_functiondef 不等价：\n"
            f"--- before ---\n{before}\n--- after ---\n{after}"
        )
        assert len(_function_signatures(name)) == len(signatures), "重载数量在重建后变化"


def test_generated_table_files_roundtrip_into_live_db(collect_raw, tmp_path):
    # relation-ddl-equivalence T3.2: same drop/regenerate/compare shape as the
    # view/function roundtrip tests above, proving the generated tables/*.sql
    # file (storage params + tablespace + constraint-backed/plain index
    # options) is executable and identity-preserving. tuned_counters is
    # deliberately dependency-free (no FK, no trigger, referenced by no view)
    # so DROP TABLE here doesn't cascade into any other fixture object.
    dbmeta_dir = tmp_path / ".dbmeta"
    render.run(collect_raw, dbmeta_dir)
    for name, kind in _ROUNDTRIP_TABLES:
        qualified = f"{FIXTURE_SCHEMA}.{name}"
        generated = dbmeta_dir / FIXTURE_SCHEMA / "tables" / f"{name}.sql"
        assert generated.is_file(), f"生成文件缺失：{generated}"
        before = _table_identity(qualified)
        _psql(_ENV, "-c", f"DROP {kind} {qualified}")
        _psql(_ENV, "-f", str(generated))  # ON_ERROR_STOP=1: any SQL error fails here
        after = _table_identity(qualified)
        assert after == before, (
            f"{qualified} 经生成文件重建后与原对象不等价（reloptions/tablespace/列/索引/约束变了）：\n"
            f"--- before ---\n{before}\n--- after ---\n{after}"
        )


def test_partition_child_options_folded_into_parent_file(collect_raw, tmp_path):
    # relation-ddl-equivalence-r2 T3.2: real-database half of the
    # render_partition_children_lines storage-param folding assertion (unit
    # half is render_test.py 2.8 ⑦) -- events_2026_08 carries WITH
    # (fillfactor=60) (tests/fixtures/dbllm_fixture.sql) and MUST fold into
    # its parent's events.sql as a one-line 存储参数 note under the child's own
    # name heading, never spill into a standalone events_2026_08.sql file or a
    # PARTITION OF clause on the parent (partition children are folded, not
    # separately rendered -- pg-dict spec 「分区子表折叠渲染」).
    dbmeta_dir = tmp_path / ".dbmeta"
    render.run(collect_raw, dbmeta_dir)
    parent_file = dbmeta_dir / FIXTURE_SCHEMA / "tables" / "events.sql"
    assert parent_file.is_file(), f"生成文件缺失：{parent_file}"
    text = parent_file.read_text()
    assert "-- `events_2026_08`" in text, "折叠子表名行缺失"
    child_heading = text.index("-- `events_2026_08`")
    remainder = text[child_heading:]
    assert "--   存储参数 `fillfactor=60`" in remainder, "折叠子表存储参数行缺失或不在子表名行之后"
    child_file = dbmeta_dir / FIXTURE_SCHEMA / "tables" / "events_2026_08.sql"
    assert not child_file.exists(), f"分区子表不应生成独立文件：{child_file}"
    assert "PARTITION OF" not in text, "父表文件不应出现 PARTITION OF（子表折叠渲染，非独立 DDL）"
