#!/usr/bin/env python3
"""candidate_match: global fuzzy match of `.dbmeta/` schema.table.column
triples, for pg-sql-check's candidate-name completion (add-pg-sql-check
Task 5, design.md "候选名补全" / decision-memo C9-C10,
specs/sql-check/spec.md REQ-SC-6, tasks.md group 5.2-5.4).

Scope (REQ-SC-6, design.md Non-Goals): this ONLY covers the two classes
PostgreSQL itself never gives a HINT for — ① a semantically made-up column
name (42703 with no HINT) and ② an undefined table/schema (42P01). The
caller (pg-sql-check.sh) is responsible for: checking whether PG already
gave a HINT (if so, this module MUST NOT be invoked — REQ-SC-6 "PG 已给
HINT 时不取代"), extracting the bare identifier text from the PG error
message (C10: a SELECT-shaped 42703 message carries only the column name,
never the table), and deciding which mode (`column` vs `table`) applies
from the SQLSTATE. This module does not parse user SQL at all (Non-Goal) —
it only reads the `.dbmeta/` file tree that `/pg-dict` already generated
and fuzzy-matches one bare identifier against it.

Match algorithm (spec.md REQ-SC-6 "匹配判据 MUST 可验证"): normalized edit
distance (Levenshtein distance / max(len(a), len(b))) >= 0.6 threshold,
sorted by similarity descending, capped at 5. Deterministic: the same
identifier against the same `.dbmeta/` tree always yields the same ordered
candidate list (ties broken by candidate name, ascending) — required so the
stdout summary and the JSON artifact (task 4) can assert identical
candidate sets from two independent invocations (spec.md "双出一致性").

Data source restriction (design.md 组件清单 / Non-Goals): column candidates
are parsed ONLY from `<schema>/tables/*.sql` files' `CREATE TABLE` column
list (render.py's `_render_column_defs` emits exactly one column per line,
`"  <quoted name> <type> ..."`, joined by ",\n" — pg-dict/scripts/render.py
:1292-1313). View files (`<schema>/views/*.sql`) are NOT parsed for their
column list: `render_view_ddl` (render.py:1511) only round-trips
`pg_get_viewdef`'s SELECT-list verbatim (arbitrary expressions/aliases, not
reliably parseable) plus whatever per-column COMMENTs happen to exist — an
unreliable, partial column source. This is a deliberate simplification
(CLAUDE.md 通则④): view columns overwhelmingly re-expose columns of their
underlying base tables, which the table-column index already covers; a
full view-definition parser is complex and fragile for a fallback feature.
Views DO still contribute their own name as a table/schema candidate
(`rank_table_candidates`), since a view is itself a valid relation target
for a 42P01 "undefined relation" error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

THRESHOLD = 0.6
MAX_CANDIDATES = 5

# render.py's `quote_ident` only wraps an identifier in double quotes when
# it needs escaping (reserved word / uppercase / special chars) — bare
# identifiers are emitted unquoted. Match either shape and always return the
# unquoted identifier text.
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE\s+\S+\s*\(\n(.*?)\n\)", re.DOTALL)
# ASCII-only by design: [A-Za-z_][A-Za-z0-9_$]* does not match non-ASCII
# identifiers (e.g. Chinese column names). A non-ASCII column line simply
# fails this match and is silently skipped when building the index — it
# degrades to "no candidates" for that column, it does not raise or crash.
_COL_LINE_RE = re.compile(r'^"?([A-Za-z_][A-Za-z0-9_$]*)"?\s+\S')


def _levenshtein(a: str, b: str) -> int:
    """Standard O(len(a)*len(b)) edit distance, single-row DP."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * lb
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                prev[j] + 1,  # deletion
                cur[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = cur
    return prev[lb]


def normalized_similarity(a: str, b: str) -> float:
    """1 - levenshtein(a, b) / max(len(a), len(b)), case-insensitive (PG
    identifiers are case-folded to lowercase unless quoted, and typos rarely
    differ only in case — comparing case-insensitively avoids losing an
    otherwise-exact match to a stray-case difference). Both-empty is a
    degenerate case (never hit: identifiers extracted from a real error
    message and every `.dbmeta/` entry are non-empty) defined as similarity
    1.0 for total-function safety rather than raising."""
    a_l, b_l = a.lower(), b.lower()
    max_len = max(len(a_l), len(b_l))
    if max_len == 0:
        return 1.0
    return 1.0 - (_levenshtein(a_l, b_l) / max_len)


def _parse_table_columns(sql_text: str) -> list[str]:
    """Extract column names from one rendered table file's `CREATE TABLE
    (...)` block. Assumes render.py's current one-column-per-line output
    shape (module docstring) — this repo's own generator, not arbitrary
    hand-written DDL, so the assumption is load-bearing but stable."""
    match = _CREATE_TABLE_RE.search(sql_text)
    if not match:
        return []
    columns: list[str] = []
    for raw_line in match.group(1).split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line = line.rstrip(",")
        col_match = _COL_LINE_RE.match(line)
        if col_match:
            columns.append(col_match.group(1))
    return columns


def build_index(dbmeta_root: Path) -> dict:
    """Scan `.dbmeta/<schema>/{tables,views}/*.sql` once into an in-memory
    index: schema names, (schema, table) pairs (tables AND views — both are
    valid 42P01 relation targets), and (schema, table, column) triples
    (tables only, see module docstring). Schema-tree entries prefixed `_`
    or `.` (`_gaps.md`, `_relations.*`, `.dbllm.env.example`, `README.md`
    at the tree root — none of these are schema directories) are skipped by
    virtue of the `is_dir()` + prefix check below; a stray non-directory
    file directly under `.dbmeta/` is likewise never treated as a schema."""
    schemas: list[str] = []
    tables: list[dict] = []
    columns: list[dict] = []

    if not dbmeta_root.is_dir():
        return {"schemas": schemas, "tables": tables, "columns": columns}

    for schema_dir in sorted(p for p in dbmeta_root.iterdir() if p.is_dir()):
        name = schema_dir.name
        if name.startswith("_") or name.startswith("."):
            continue
        schemas.append(name)

        tables_dir = schema_dir / "tables"
        if tables_dir.is_dir():
            for sql_file in sorted(tables_dir.glob("*.sql")):
                table = sql_file.stem
                tables.append({"schema": name, "table": table})
                try:
                    text = sql_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                for col in _parse_table_columns(text):
                    columns.append({"schema": name, "table": table, "column": col})

        views_dir = schema_dir / "views"
        if views_dir.is_dir():
            for sql_file in sorted(views_dir.glob("*.sql")):
                tables.append({"schema": name, "table": sql_file.stem})

    return {"schemas": schemas, "tables": tables, "columns": columns}


def _finalize(scored: list[tuple[str, float]], limit: int) -> list[tuple[str, float]]:
    """Dedupe by candidate string (keep the max score seen), then sort by
    (similarity desc, candidate name asc) so the same identifier against the
    same index is byte-for-byte reproducible run to run (spec.md "恒得同一
    候选集合") — a plain dict/set iteration order would not guarantee this
    across candidates that tie on score."""
    best: dict[str, float] = {}
    for cand, sim in scored:
        if cand not in best or sim > best[cand]:
            best[cand] = sim
    ordered = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:limit]


def rank_column_candidates(
    identifier: str,
    index: dict,
    threshold: float = THRESHOLD,
    limit: int = MAX_CANDIDATES,
) -> list[tuple[str, float]]:
    """Rank every `.dbmeta/`-known column against `identifier`'s bare local
    name (text after the last `.`, if any — the extracted identifier is
    already just a bare column name per C10, but a defensive strip costs
    nothing). Similarity is computed against the BARE column name (not the
    fully-qualified `schema.table.column` string) — comparing the bare
    3-10 char identifier against a 20+ char qualified string would sink
    almost every real typo below threshold on length alone. The returned
    candidate label IS fully qualified, since the same bare column name can
    legitimately exist in more than one table across a global match."""
    local = identifier.rsplit(".", 1)[-1]
    scored: list[tuple[str, float]] = []
    for col in index["columns"]:
        sim = normalized_similarity(local, col["column"])
        if sim >= threshold:
            qualified = f'{col["schema"]}.{col["table"]}.{col["column"]}'
            scored.append((qualified, sim))
    return _finalize(scored, limit)


def rank_table_candidates(
    identifier: str,
    index: dict,
    threshold: float = THRESHOLD,
    limit: int = MAX_CANDIDATES,
) -> list[tuple[str, float]]:
    """Same idea as `rank_column_candidates`, for 42P01 (undefined relation
    or schema). Ranks against both the known (schema, table) pairs — bare
    table name compared, `schema.table` returned — and the known schema
    names themselves (bare schema name compared AND returned, unqualified),
    since PG never gives a HINT for either an undefined table or an
    undefined schema and the same SQLSTATE covers both."""
    local = identifier.rsplit(".", 1)[-1]
    scored: list[tuple[str, float]] = []
    for tbl in index["tables"]:
        sim = normalized_similarity(local, tbl["table"])
        if sim >= threshold:
            qualified = f'{tbl["schema"]}.{tbl["table"]}'
            scored.append((qualified, sim))
    for schema in index["schemas"]:
        sim = normalized_similarity(local, schema)
        if sim >= threshold:
            scored.append((schema, sim))
    return _finalize(scored, limit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fuzzy-match one bare identifier against the .dbmeta/ data "
            "dictionary (pg-sql-check candidate-name completion, REQ-SC-6)."
        )
    )
    parser.add_argument("--dbmeta-root", required=True, help="path to the .dbmeta/ directory")
    parser.add_argument("--mode", required=True, choices=["column", "table"])
    parser.add_argument("--identifier", required=True, help="bare identifier extracted from the PG error message")
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="json (default, machine-consumable / test contract) or text "
        "(a ready-to-embed human-readable block — used by pg-sql-check.sh "
        "so the caller does not need a JSON parser in bash)",
    )
    args = parser.parse_args(argv)

    index = build_index(Path(args.dbmeta_root))
    if args.mode == "column":
        ranked = rank_column_candidates(args.identifier, index)
    else:
        ranked = rank_table_candidates(args.identifier, index)

    candidates = [{"name": name, "similarity": round(sim, 4)} for name, sim in ranked]

    if args.format == "json":
        result = {
            "identifier": args.identifier,
            "mode": args.mode,
            "candidates": candidates,
            "source": ".dbmeta/",
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # --format text (spec.md REQ-SC-6 "无候选达阈值时 MUST 显式呈现「无候选」
    # 并仅说明该表/列不存在，MUST NOT 输出空列表而不加说明"):
    if not candidates:
        print("无候选 —— 该标识符与 .dbmeta/ 字典中任何已知标识符的相似度均低于阈值")
    else:
        for cand in candidates:
            print(f'  - {cand["name"]}（相似度 {cand["similarity"]:.2f}）')
        print("候选名取自 .dbmeta/，可能滞后于活库，必要时重跑 /pg-dict")
    return 0


if __name__ == "__main__":
    sys.exit(main())
