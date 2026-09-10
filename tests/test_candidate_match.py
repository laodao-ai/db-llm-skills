"""Unit tests for pg-sql-check/scripts/candidate_match.py (add-pg-sql-check
Task 5, design.md "候选名补全", specs/sql-check/spec.md REQ-SC-6,
task5-brief.md). Pure-function + `.dbmeta/` fixture-tree tests, no DB, no
subprocess for the ranking logic itself; a thin CLI smoke test covers
`main()`/argv wiring separately."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pg-sql-check" / "scripts"))

import candidate_match  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATE_MATCH_PY = REPO_ROOT / "pg-sql-check" / "scripts" / "candidate_match.py"


# ---------------------------------------------------------------------------
# normalized_similarity / levenshtein — pure math, no filesystem
# ---------------------------------------------------------------------------


def test_similarity_identical_is_one():
    assert candidate_match.normalized_similarity("email", "email") == 1.0


def test_similarity_case_insensitive():
    assert candidate_match.normalized_similarity("Email", "email") == 1.0


def test_similarity_one_substitution_out_of_five():
    # "email" -> "emial" is a transposition = 2 substitutions under plain
    # Levenshtein (no transposition operation): distance 2, max(len)=5,
    # similarity = 1 - 2/5 = 0.6 — exactly the REQ-SC-6 threshold boundary.
    assert candidate_match.normalized_similarity("email", "emial") == pytest.approx(0.6)


def test_similarity_completely_different_below_threshold():
    sim = candidate_match.normalized_similarity("users", "account_master")
    assert sim < candidate_match.THRESHOLD


# ---------------------------------------------------------------------------
# build_index — .dbmeta/ fixture tree scanning
# ---------------------------------------------------------------------------


def make_dbmeta(tmp_path: Path) -> Path:
    """A minimal two-schema .dbmeta/ fixture: one schema with a table (with
    a NOT NULL column and a plain column) and a view, one extra schema with
    one table — enough to exercise cross-schema global matching and the
    tables-vs-views-vs-columns distinction."""
    root = tmp_path / ".dbmeta"
    root.mkdir()
    (root / "README.md").write_text("not a schema\n", encoding="utf-8")
    (root / "_gaps.md").write_text("not a schema either\n", encoding="utf-8")

    app = root / "app"
    (app / "tables").mkdir(parents=True)
    (app / "views").mkdir(parents=True)
    (app / "tables" / "users.sql").write_text(
        "-- app.users 表\n"
        "-- pg-dict:table:users:start\n"
        "CREATE TABLE app.users (\n"
        "  id bigint NOT NULL,\n"
        "  email text NOT NULL,\n"
        "  created_at timestamp with time zone DEFAULT now() NOT NULL\n"
        ");\n"
        "-- pg-dict:table:users:end\n",
        encoding="utf-8",
    )
    (app / "views" / "active_users.sql").write_text(
        "-- pg-dict:view:active_users:start\n"
        "CREATE VIEW app.active_users AS\n"
        " SELECT id, email FROM app.users;\n"
        "-- pg-dict:view:active_users:end\n",
        encoding="utf-8",
    )

    sales = root / "sales"
    (sales / "tables").mkdir(parents=True)
    (sales / "tables" / "orders.sql").write_text(
        "-- pg-dict:table:orders:start\n"
        "CREATE TABLE sales.orders (\n"
        "  id bigint NOT NULL,\n"
        "  user_id bigint NOT NULL\n"
        ");\n"
        "-- pg-dict:table:orders:end\n",
        encoding="utf-8",
    )
    return root


def test_build_index_skips_non_schema_entries(tmp_path):
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    assert sorted(index["schemas"]) == ["app", "sales"]


def test_build_index_collects_tables_and_views(tmp_path):
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    pairs = {(t["schema"], t["table"]) for t in index["tables"]}
    assert ("app", "users") in pairs
    assert ("app", "active_users") in pairs  # view counts as a relation
    assert ("sales", "orders") in pairs


def test_build_index_parses_table_columns(tmp_path):
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    app_users_cols = {
        c["column"] for c in index["columns"] if c["schema"] == "app" and c["table"] == "users"
    }
    assert app_users_cols == {"id", "email", "created_at"}


def test_build_index_does_not_parse_view_columns(tmp_path):
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    view_cols = [c for c in index["columns"] if c["table"] == "active_users"]
    assert view_cols == []  # documented simplification (module docstring)


def test_build_index_missing_dbmeta_returns_empty(tmp_path):
    index = candidate_match.build_index(tmp_path / "nope")
    assert index == {"schemas": [], "tables": [], "columns": []}


# ---------------------------------------------------------------------------
# rank_column_candidates / rank_table_candidates — end-to-end against the
# fixture tree (task5-brief.md "离线用例（.dbmeta/ fixture -> 候选集一致性）")
# ---------------------------------------------------------------------------


def test_column_typo_within_threshold_gets_candidate(tmp_path):
    # NOTE: spec.md's own illustrative Scenario ("含 user_email 而真实列为
    # email") is numerically BELOW the 0.6 threshold under literal
    # normalized Levenshtein (distance 5, max_len 10, similarity 0.5) — see
    # impl-report "票外发现". This test instead exercises a same-flavor
    # typo that the literal, spec-mandated algorithm actually surfaces.
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    ranked = candidate_match.rank_column_candidates("emailz", index)
    names = [name for name, _ in ranked]
    assert "app.users.email" in names


def test_column_candidates_qualified_and_global(tmp_path):
    """Global match (Non-Goal: no SQL parsing to scope down which tables are
    involved) — a column-name typo can surface a candidate from a schema the
    query never mentioned."""
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    ranked = candidate_match.rank_column_candidates("useer_id", index)
    names = [name for name, _ in ranked]
    assert "sales.orders.user_id" in names


def test_wrong_table_name_gets_candidate(tmp_path):
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    ranked = candidate_match.rank_table_candidates("orderz", index)
    names = [name for name, _ in ranked]
    assert "sales.orders" in names


def test_wrong_schema_name_gets_candidate(tmp_path):
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    ranked = candidate_match.rank_table_candidates("saless", index)
    names = [name for name, _ in ranked]
    assert "sales" in names


def test_no_candidate_above_threshold_is_empty_list(tmp_path):
    """spec.md Scenario「无候选达阈值时显式说明」: the ranking function itself
    returns an empty list (not a fabricated low-confidence guess) — the
    caller is responsible for rendering that as an explicit "无候选" message
    rather than a bare empty list (pg-sql-check.sh integration, not this
    module's job)."""
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    ranked = candidate_match.rank_table_candidates("account_master", index)
    assert ranked == []


def test_max_five_candidates(tmp_path):
    root = tmp_path / ".dbmeta"
    (root / "s" / "tables").mkdir(parents=True)
    # Six columns, all within edit distance 1 of "col" at length 4 (sim=0.75).
    body = "CREATE TABLE s.t (\n" + ",\n".join(
        f"  co{i} text" for i in range(6)
    ) + "\n);\n"
    (root / "s" / "tables" / "t.sql").write_text(body, encoding="utf-8")
    index = candidate_match.build_index(root)
    ranked = candidate_match.rank_column_candidates("col", index)
    assert len(ranked) <= candidate_match.MAX_CANDIDATES


def test_ranking_is_deterministic_across_repeated_calls(tmp_path):
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    first = candidate_match.rank_column_candidates("useer_id", index)
    second = candidate_match.rank_column_candidates("useer_id", index)
    assert first == second


def test_ranking_sorted_by_similarity_descending(tmp_path):
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    ranked = candidate_match.rank_column_candidates("id", index)
    sims = [sim for _, sim in ranked]
    assert sims == sorted(sims, reverse=True)


def test_column_bare_name_used_for_similarity_not_qualified_string(tmp_path):
    """A bare 5-char typo must not be sunk below threshold just because the
    qualified candidate string ("app.users.email", 16 chars) is much longer
    — similarity MUST be computed against the bare column name."""
    root = make_dbmeta(tmp_path)
    index = candidate_match.build_index(root)
    ranked = candidate_match.rank_column_candidates("emial", index)
    names = [name for name, _ in ranked]
    assert "app.users.email" in names


# ---------------------------------------------------------------------------
# CLI (main()) smoke test — argv -> stdout JSON contract
# ---------------------------------------------------------------------------


def test_cli_emits_json_with_expected_shape(tmp_path):
    root = make_dbmeta(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(CANDIDATE_MATCH_PY),
            "--dbmeta-root",
            str(root),
            "--mode",
            "column",
            "--identifier",
            "emailz",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "column"
    assert payload["identifier"] == "emailz"
    assert payload["source"] == ".dbmeta/"
    names = [c["name"] for c in payload["candidates"]]
    assert "app.users.email" in names


def test_cli_empty_candidates_is_explicit_empty_list_not_missing_key(tmp_path):
    root = make_dbmeta(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(CANDIDATE_MATCH_PY),
            "--dbmeta-root",
            str(root),
            "--mode",
            "table",
            "--identifier",
            "account_master",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["candidates"] == []
