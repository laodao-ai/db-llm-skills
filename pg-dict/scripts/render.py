#!/usr/bin/env python3
"""Render/merge .dbmeta/<schema>/{tables,views,functions}/<name>.sql executable DDL
files (schema README/root README/_relations.md/_gaps.md remain markdown) from
db-collect v1 metadata JSON.

Reads a `{"collect_version": 1, "schemas": [...]}` document from stdin (produced by
shared/db-collect.sh via psql -At -X -f shared/db-collect.sql) and writes/merges one
`.sql` file per database object (one object == one file, EXCEPT function files
which hold one managed block per overload — openspec/changes/dbmeta-knowledge-base/
design.md D-A/D-B), plus a whole-repo .dbmeta/_gaps.md gap report. (Object files were
markdown through dbmeta-ddl-files Task 3; Task 4 switched them to executable `.sql`
via SQL_SYNTAX — design.md DD-6.)

Task-2 scope (design.md 切片建议 #2 / impl-reports/task2-brief.md) on top of Task 1's
skeleton:
  - Table files render the FULL body: columns, indexes, constraints (NOT NULL
    excluded — already excluded at the SQL layer), triggers (with a resolved link to
    the function file the trigger calls, D-D), a reltuples row-count-estimate note,
    and the partition-child fold summary/detail (D-A / spec "分区子表折叠渲染").
  - View files are rendered for the first time: definition SQL, column list (sourced
    from db-collect's new `views[].columns[]`, D-N), COMMENT.
  - Function files are rendered for the first time: one managed block PER OVERLOAD
    (`pg-dict:fn:<name>(<identity_args>):start/end`), each with signature, return
    type, language, COMMENT, full source (fenced with a backtick run longer than the
    longest backtick run inside the source itself), and a reverse "被以下触发器引用"
    list built from every top-level table's triggers across the whole collect result.
  - `_relations.md` / `_collect.json` / README index files remain out of scope
    (Task 3).

Managed-block merge contract (dbmeta spec "生成件与手写件边界" / "一文件一块的孤立与
删除语义"; pg-dict spec "需求:托管块再生与手写注记保留"):
  - A table/view file holds exactly one managed block: `<!-- pg-dict:<kind>:<name>
    :start -->` ... `<!-- pg-dict:<kind>:<name>:end -->` (kind is "table"/"view"). A
    function file holds ONE block PER OVERLOAD — `pg-dict:fn:<name>(<identity_args>)`
    — the file itself is still one-file-per-object-NAME, but the object-name-to-file
    mapping is many-blocks-to-one-file for functions specifically (the sole
    "one-file-multiple-blocks" case, per D-B). Block content is fully rewritten on
    every regen.
  - Text outside a managed block is hand-maintained and MUST be preserved verbatim,
    in place — EXCEPT the file's own auto-generated leading header (H1 title +
    immediately-following HTML-comment explanation), which is never itself treated
    as hand-written annotation.
  - When an object disappears from the collect result: if the file has no
    non-empty, non-HTML-comment text outside its managed block(s), the file is
    deleted (and its parent directory removed once empty). Otherwise the managed
    block(s) are removed and a one-line orphan banner is inserted at the top of the
    file (right after the header, if present) — idempotently, never duplicated. For
    a function file, this whole-file-removal path only triggers when EVERY overload
    of that function name is gone; a single overload disappearing while siblings
    remain just drops that one block (no banner — the file still legitimately
    represents a live object).
  - A whole schema disappearing from the collect result (D-L) is handled the same
    way, recursively, across every `tables/`/`views/`/`functions/` file plus the
    schema's own `_collect.json`/`README.md` (pure generated files, deleted
    outright) — even though this ticket does not itself produce those two files
    yet, the collapse logic already handles them for forward compatibility.
  - Object/schema names and managed-block identifiers MUST match the D-M
    identifier contract (`^[A-Za-z0-9_.]+$`, no "--"); for functions this also
    covers `identity_args` (the parenthesized part of the block ident), since a
    literal "--" there would just as surely break the HTML-comment delimiter. A
    violation is fail-loud BEFORE any file is written or deleted (dbmeta spec
    「目录契约」场景:非法标识符 fail-loud).
  - Re-running with unchanged metadata against its own prior output MUST be
    byte-identical.
  - .dbmeta/_gaps.md has no managed blocks — it is a deterministic, full-file
    rewrite listing tables/columns/functions missing a COMMENT plus a
    sensitive-column-name warning section. It MUST NOT contain any field that
    varies run-to-run (no collected_at, no reltuples).

Usage: shared/db-collect.sh | python3 render.py --dbmeta-dir dbmeta
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Managed-block identifier contract (D-B / D-M)
# ---------------------------------------------------------------------------

# The full block identifier (e.g. "table:users", "view:v_active_users",
# "fn:fn_audit(p_id bigint)") sits between "pg-dict:" and ":start -->"/":end -->".
# It is captured as one opaque, non-greedy blob rather than split on ':' — unlike
# the old single-schema-multi-table layout, the identifier itself now legitimately
# contains ':' (the "<kind>:<name>" separator), so a `[^:\n]+` exclusion (as the
# pre-rewrite BLOCK_RE used) would no longer match it.
BLOCK_RE = re.compile(
    r"<!-- pg-dict:(?P<ident>.+?):start -->\n"
    r"(?P<body>.*?)"
    r"<!-- pg-dict:(?P=ident):end -->\n?",
    re.DOTALL,
)

COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

ORPHAN_BANNER = (
    "<!-- pg-dict: 孤立注记（原挂靠对象已从数据库删除，人工确认是否仍需保留） -->\n"
)

# ---------------------------------------------------------------------------
# DD-1 (design.md): BlockSyntax — the managed-block marker syntax abstracted
# into a small data class so object files (.sql, no SQL parsing — block-
# external text is only ever handled line-wise) and README-style files (.md,
# HTML-comment markers) share ONE parse/wrap/merge/_has_annotation/
# process_removed_object_file implementation instead of two copies that would
# inevitably drift. `comment_re` for SQL_SYNTAX matches the empty string
# everywhere (`.sub()` is then a no-op) rather than stripping anything — DD-2:
# in a .sql file a "--" line IS the only way to write hand-written text, so it
# must count as annotation, unlike an HTML comment in a .md file.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockSyntax:
    start_fmt: str
    end_fmt: str
    block_re: re.Pattern
    comment_re: re.Pattern
    orphan_banner: str


MD_SYNTAX = BlockSyntax(
    start_fmt="<!-- pg-dict:{ident}:start -->\n",
    end_fmt="<!-- pg-dict:{ident}:end -->\n",
    block_re=BLOCK_RE,
    comment_re=COMMENT_RE,
    orphan_banner=ORPHAN_BANNER,
)

SQL_BLOCK_RE = re.compile(
    r"^-- pg-dict:(?P<ident>.+?):start\n"
    r"(?P<body>.*?)"
    r"^-- pg-dict:(?P=ident):end\n?",
    re.DOTALL | re.MULTILINE,
)
# [spec-review-amendment Q2] the `^` anchors above require every real block
# marker to sit at the START of a line (re.MULTILINE makes `^` match right
# after any `\n`, not just at string start) — a line produced by
# `_comment_lines` prefixing an embedded `-- pg-dict:...` marker line with
# `-- ` (yielding `-- -- pg-dict:...`) no longer matches, since that text does
# NOT begin the line. wrap_block()/merge_blocks() always place real markers
# immediately after a `\n` (header text and every block's start/end format
# string end in `\n`), so this tightening changes nothing for legitimate
# managed-block boundaries — only for a marker-shaped line embedded INSIDE a
# comment body, which validate_managed_marker_collisions below now also
# rejects at the source before any such line could reach here.

# Matches the empty string at every position — `.sub("", text)` leaves `text`
# byte-for-byte unchanged (DD-1: "comment_re 为空匹配").
SQL_COMMENT_RE = re.compile(r"")

SQL_ORPHAN_BANNER = (
    "-- pg-dict: 孤立注记（原挂靠对象已从数据库删除，人工确认是否仍需保留）\n"
)

SQL_SYNTAX = BlockSyntax(
    start_fmt="-- pg-dict:{ident}:start\n",
    end_fmt="-- pg-dict:{ident}:end\n",
    block_re=SQL_BLOCK_RE,
    comment_re=SQL_COMMENT_RE,
    orphan_banner=SQL_ORPHAN_BANNER,
)


def _managed_marker_line_pattern(fmt: str) -> str:
    """[impl-review-fix] V1 (code-review Important, outside-voice): build a regex
    alternative matching any line shaped like `fmt` (SQL_SYNTAX.start_fmt or
    .end_fmt) for ANY ident — derived from the format string itself (via a
    placeholder substituted post-escape) so this can never drift from
    SQL_BLOCK_RE's own delimiter shape (MUST NOT hand-copy the format string a
    second time)."""
    placeholder = "\x00IDENT\x00"
    literal = fmt.format(ident=placeholder).rstrip("\n")
    return re.escape(literal).replace(re.escape(placeholder), ".+")


# [impl-review-fix] V1: matches a line with the exact shape of an SQL_SYNTAX
# managed-block marker (start OR end, any ident). Used to reject a function/view
# `definition` whose source text happens to contain such a line — that line
# would be mistaken by SQL_BLOCK_RE for the block's own boundary on the NEXT
# re-generation, corrupting the parse. re.MULTILINE anchors "^"/"$" per line.
MANAGED_MARKER_LINE_RE = re.compile(
    r"^(?:"
    + _managed_marker_line_pattern(SQL_SYNTAX.start_fmt)
    + r"|"
    + _managed_marker_line_pattern(SQL_SYNTAX.end_fmt)
    + r")$",
    re.MULTILINE,
)

# D-M: object names (table/view/function) and schema names MUST match this. The
# character class deliberately excludes "-", so a "--" sequence (which would
# prematurely close an HTML comment and corrupt the managed-block delimiters) can
# never pass — no separate "--" containment check is needed (T33).
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.]+$")

OBJECT_LABELS = {"table": "表", "view": "视图", "fn": "函数"}
KIND_DIR_NAMES = {"table": "tables", "view": "views", "fn": "functions"}

# D-D: extract the called function's (optionally schema-qualified) name from a
# trigger's `pg_get_triggerdef` text, e.g. "... EXECUTE FUNCTION auth.fn_audit()".
TRIGGER_FN_RE = re.compile(
    r"EXECUTE (?:FUNCTION|PROCEDURE) ((?:[a-z_][a-z0-9_]*\.)?[a-z_][a-z0-9_]*)\("
)

# DD-3 rule 5 [spec-review-amendment]: pg_trigger.tgenabled -> the ALTER TABLE
# statement render_table_ddl emits right after CREATE TRIGGER when a trigger's
# enabled state isn't the default 'O'. `{table}` is the already-quote_ident'd
# `schema.table`, `{name}` the already-quote_ident'd trigger name.
TRIGGER_ENABLE_STATE_STMT = {
    "D": "ALTER TABLE {table} DISABLE TRIGGER {name};",
    "R": "ALTER TABLE {table} ENABLE REPLICA TRIGGER {name};",
    "A": "ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name};",
}

# Sensitive column-name / "already protected" comment judgement (.dbmeta/pg-dict
# spec's 敏感列名 warning, unchanged from the pre-rewrite implementation).
SENSITIVE_NAME_RE = re.compile(
    r"password|passwd|pwd|token|secret|mobile|phone|email|id_card|idcard",
    re.IGNORECASE,
)
SENSITIVE_SAFE_RE = re.compile(
    r"脱敏|密文|哈希|加密|hash|encrypt",
    re.IGNORECASE,
)


class CollectFormatError(Exception):
    """Raised when the stdin document is not a valid collect_version==1 document,
    OR when an object/schema name violates the D-M identifier contract.

    Carries the three problem/cause/fix lines main() prints to stderr verbatim —
    callers MUST NOT write or delete any .dbmeta/ file when this is raised.
    """

    def __init__(self, problem: str, cause: str, fix: str) -> None:
        self.problem = problem
        self.cause = cause
        self.fix = fix
        super().__init__(problem)


# ---------------------------------------------------------------------------
# DD-5 (design.md, Q1 拍板 2026-08-29): quote_ident — a verbatim port of PG's own
# quote_identifier() (src/backend/utils/adt/ruleutils.c, shared by pg_get_*def /
# pg_dump / the SQL quote_ident() builtin). Bare iff the name matches
# ^[a-z_][a-z0-9_]*$ (digit-leading MUST be quoted) AND is not one of the 164
# words PG itself insists on quoting even though some of them (the "C" —
# col-name — catcode) are technically legal bare column names; render follows
# PG's own emitted-DDL convention rather than the more permissive grammar rule,
# since the goal is DDL that reads the same as what PG itself would print.
#
# PG_KEYWORDS_QUOTED source (真库 PG 18.0, 2026-08-29, 只读凭据连接的 dev DB):
#   SELECT word FROM pg_get_keywords() WHERE catcode <> 'U' ORDER BY word;
# 164 words total — R (reserved) 78 + T (type/func-name) 23 + C (col-name) 63.
# `catcode = 'U'` (unreserved) words are deliberately EXCLUDED — those never
# need quoting anywhere, per PG's own keyword catalog.
# ---------------------------------------------------------------------------

PG_KEYWORDS_QUOTED: frozenset[str] = frozenset(
    {
        "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric",
        "authorization", "between", "bigint", "binary", "bit", "boolean", "both", "case",
        "cast", "char", "character", "check", "coalesce", "collate", "collation", "column",
        "concurrently", "constraint", "create", "cross", "current_catalog", "current_date",
        "current_role", "current_schema", "current_time", "current_timestamp",
        "current_user", "dec", "decimal", "default", "deferrable", "desc", "distinct", "do",
        "else", "end", "except", "exists", "extract", "false", "fetch", "float", "for",
        "foreign", "freeze", "from", "full", "grant", "greatest", "group", "grouping",
        "having", "ilike", "in", "initially", "inner", "inout", "int", "integer",
        "intersect", "interval", "into", "is", "isnull", "join", "json", "json_array",
        "json_arrayagg", "json_exists", "json_object", "json_objectagg", "json_query",
        "json_scalar", "json_serialize", "json_table", "json_value", "lateral", "leading",
        "least", "left", "like", "limit", "localtime", "localtimestamp", "merge_action",
        "national", "natural", "nchar", "none", "normalize", "not", "notnull", "null",
        "nullif", "numeric", "offset", "on", "only", "or", "order", "out", "outer",
        "overlaps", "overlay", "placing", "position", "precision", "primary", "real",
        "references", "returning", "right", "row", "select", "session_user", "setof",
        "similar", "smallint", "some", "substring", "symmetric", "system_user", "table",
        "tablesample", "then", "time", "timestamp", "to", "trailing", "treat", "trim",
        "true", "union", "unique", "user", "using", "values", "varchar", "variadic",
        "verbose", "when", "where", "window", "with", "xmlattributes", "xmlconcat",
        "xmlelement", "xmlexists", "xmlforest", "xmlnamespaces", "xmlparse", "xmlpi",
        "xmlroot", "xmlserialize", "xmltable",
    }
)

_BARE_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# [T42] Shape of the `name` segment of a views[].options element (the part before
# the first `=`, or the whole element when it carries no `=`). pg_class.reloptions
# only ever holds registry-validated storage-parameter names — identifier
# characters plus an optional namespace dot (`toast.autovacuum_enabled`) — so
# anything else (empty, spaces, `)`/`;`/`--`, ...) can only come from a
# hand-edited or tampered collect JSON. render_view_ddl emits this segment
# UNQUOTED (see its docstring), which is exactly why the shape is pinned here
# in _validate_schemas before any file is written.
_RELOPTION_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")


def quote_ident(name: str) -> str:
    """PG quote_identifier(): return `name` bare iff it matches
    `^[a-z_][a-z0-9_]*$` and is not in PG_KEYWORDS_QUOTED; otherwise wrap in
    double quotes with any embedded `"` doubled. Applies to every identifier
    component render itself assembles (schema/table/column/sequence-segment/
    constraint/trigger/function/view name) — NEVER to text that already came
    out of pg_get_constraintdef/pg_get_indexdef/pg_get_triggerdef/
    pg_get_functiondef/pg_get_viewdef, which PG has already quoted as needed
    (DD-5)."""
    if _BARE_IDENT_RE.match(name) and name not in PG_KEYWORDS_QUOTED:
        return name
    return '"' + name.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    """SQL string literal for a COMMENT ON ... IS '<esc>' / reloption / FDW
    option value — embedded `'` doubled per SQL's own escaping rule (DD-3 rule
    6). Newlines are left untouched; they are legal inside a single-quoted PG
    string literal.

    relation-ddl-equivalence T2.1 (decision-memo C5): when `value` contains a
    backslash, this returns an `E'...'` extended-string literal with both `\\`
    and `'` doubled, instead of a plain `'...'` literal with only `'` doubled.
    The two forms are semantically equivalent regardless of the session's
    `standard_conforming_strings` setting: under `on` (PG's default since 9.1)
    a plain `'...'` literal already treats `\\` as a literal backslash, so
    doubling it there would be WRONG; under `off` a plain literal would treat
    `\\` as an escape introducer, corrupting the value. `E'...'` always parses
    `\\` as an escape introducer regardless of `standard_conforming_strings`,
    so doubling every literal `\\` (plus every `'`) is unconditionally correct
    for it in both modes — hence the conditional dispatch: no backslash means
    no ambiguity exists and the plain form is used unchanged (byte-identical
    output for every value seen before this change)."""
    if "\\" in value:
        return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
    return "'" + value.replace("'", "''") + "'"


def _comment_lines(text: str) -> list[str]:
    """relation-ddl-equivalence T2.1 (decision-memo, TG-17 `--` 单行注释 收口):
    split `text` on any line ending (LF/CR/CRLF) and prefix EVERY resulting
    line with `-- ` — the single choke point through which catalog text that
    might itself contain embedded newlines is allowed to land inside a `--`
    comment. Without this, a raw `f"-- {text}"` splice would let an embedded
    newline in `text` end the comment early, turning the remainder of `text`
    into live (potentially injected) SQL. An empty string still splits to one
    empty segment, so this returns `["-- "]` rather than `[]` — callers that
    unconditionally `lines.extend(...)` a single logical comment line always
    get at least one line back. A `text` with no newline at all returns
    exactly `["-- " + text]` — byte-identical to the `f"-- {text}"` call sites
    this replaces, so every pre-existing single-line comment output is
    unchanged (2.2)."""
    return ["-- " + line for line in re.split(r"\r\n|\r|\n", text)]


def _warn(problem: str, cause: str, fix: str) -> None:
    """Non-fatal problem/cause/fix warning to stderr — same three-line shape as
    CollectFormatError's fatal path (main()'s except block), but regen
    continues. Used by load_confirmed() (DD-12/REQ-RI-5): a human-maintained
    file's format errors MUST NOT block the rest of regen."""
    print(f"problem: {problem}", file=sys.stderr)
    print(f"cause: {cause}", file=sys.stderr)
    print(f"fix: {fix}", file=sys.stderr)


# A column default matching this shape references a sequence by regclass
# literal (BIGSERIAL/SERIAL/`nextval(...)` columns — memo C4: the only kind of
# default this repo's tables carry; identity/generated columns are absent).
NEXTVAL_DEFAULT_RE = re.compile(r"^nextval\('(?P<seq>[^']+)'::regclass\)$")


def sequence_names_from_defaults(columns: list[dict], schema: str) -> list[str]:
    """DD-3 rule 1 / DD-5 [spec-review-amendment]: scan `columns` in order for a
    `nextval('<seq>'::regclass)` default, and return the ordered, de-duplicated
    (first occurrence wins) list of `CREATE SEQUENCE IF NOT EXISTS` targets —
    each one already schema-qualified-and-quote_ident'd, ready to place
    directly after `CREATE SEQUENCE IF NOT EXISTS `.

    The captured `<seq>` MAY already carry a schema qualifier (this repo's real
    `pg_get_expr` output always does, per .dbmeta/auth/_collect.json — memo C4)
    — such a capture MUST NOT be re-prefixed with `schema` (that would produce
    `auth.auth.users_id_seq`). Split on the FIRST "." when present; an
    unqualified capture is qualified with `schema`. Either way each of the two
    segments is quote_ident'd SEPARATELY — a dotted capture is never treated as
    one opaque identifier. Any `"` PG itself already embedded in the captured
    text is stripped first, since quote_ident() re-derives quoting from
    scratch rather than trusting the source's own quoting decision."""
    seen: set[str] = set()
    result: list[str] = []
    for col in columns:
        default = col.get("default") or ""
        m = NEXTVAL_DEFAULT_RE.match(default)
        if not m:
            continue
        raw = m.group("seq").replace('"', "")
        if "." in raw:
            seq_schema, seq_name = raw.split(".", 1)
        else:
            seq_schema, seq_name = schema, raw
        qualified = f"{quote_ident(seq_schema)}.{quote_ident(seq_name)}"
        if qualified not in seen:
            seen.add(qualified)
            result.append(qualified)
    return result


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


# T32: pg_class.reltuples is a sampling estimate that drifts with every
# autovacuum/ANALYZE, so rendering the exact number would make every table
# .sql and schema README diff on regen with zero schema change. Only its order
# of magnitude carries information a reader acts on ("is this a lookup table or
# a fact table"), so that is all that gets rendered — a rendered value changes
# only when the table's scale actually crosses a bucket boundary.
RELTUPLES_BUCKETS: tuple[tuple[int, str], ...] = (  # (exclusive upper bound, label)
    (1, "0"),
    (100, "<100"),
    (1_000, "百级"),
    (10_000, "千级"),
    (100_000, "万级"),
    (1_000_000, "十万级"),
    (10_000_000, "百万级"),
    (100_000_000, "千万级"),
    (1_000_000_000, "亿级"),
)
RELTUPLES_TOP_LABEL = "十亿级+"


def format_reltuples(value) -> str:
    """Order-of-magnitude bucket label for a reltuples value (see
    RELTUPLES_BUCKETS). Missing/invalid/negative input renders as "0" — the same
    thing PG reports for a never-ANALYZEd table, and the note next to it already
    says that 0 does not mean empty."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        n = 0
    for upper, label in RELTUPLES_BUCKETS:
        if n < upper:
            return label
    return RELTUPLES_TOP_LABEL


# ---------------------------------------------------------------------------
# Partition-child grouping (kept verbatim per design.md "保留 group_children")
# ---------------------------------------------------------------------------


def group_children(tables: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Split a schema's table list into (parent-level tables, children-by-parent-name).

    A table with a non-empty `partition_of` is a partition child and MUST NOT get
    its own file — it folds into its parent's file instead. Tables without a
    `partition_of` key at all (pre-v1 fixtures / non-partitioned tables) are
    treated as ordinary top-level tables via `.get()`.
    """
    parents: list[dict] = []
    children_by_parent: dict[str, list[dict]] = {}
    for t in tables:
        parent_name = t.get("partition_of")
        if parent_name:
            children_by_parent.setdefault(parent_name, []).append(t)
        else:
            parents.append(t)
    return parents, children_by_parent


ORPHAN_PARTITION_NOTE = (
    "分区父表 `{parent}` 不在本 schema / 为中间分区，按独立表渲染"
)


def resolve_orphan_children(
    parents: list[dict], children_by_parent: dict[str, list[dict]]
) -> dict[str, str]:
    """A child whose `partition_of` name is not itself a top-level table in this
    schema — either a cross-schema partition parent, or an intermediate partition
    level that was itself folded away into ITS OWN parent's block (multi-level
    partitioning) — would otherwise be silently dropped: it gets neither its own
    file nor a fold-in anywhere. Promotes such children back to parent-level
    tables (mutates `children_by_parent` in place, popping the orphan keys) so
    they always render as an independent file and participate in gaps.

    Returns {table_name: orphan_note} for the promoted tables — rendered as a
    `--`-comment note inside the table's DDL by render_table_ddl().
    """
    parent_names = {p["name"] for p in parents}
    orphan_keys = [k for k in children_by_parent if k not in parent_names]
    notes: dict[str, str] = {}
    for key in orphan_keys:
        for child in children_by_parent.pop(key):
            parents.append(child)
            notes[child["name"]] = ORPHAN_PARTITION_NOTE.format(parent=key)
    return notes


def render_partition_children_lines(children: list[dict]) -> list[str]:
    """Render the "分区子表（N）：…" summary line plus per-child detail lines for any
    child owning a non-inherited index, a locally-defined constraint, non-empty
    storage parameters/tablespace, a non-heap access method, or foreign-table
    kind (spec DD-4 + relation-ddl-equivalence T2.4/[spec-review-amendment Q1]:
    「仅当子表拥有非父表下推的自有索引或本地约束…」的单列门放宽到这五种形态任一
    命中). [Task 3, tasks.md 3.2] Every NON-BLANK line carries its own `-- `
    prefix (via `_comment_lines`, T2.2) — this content lands inside an
    executable .sql object file's DDL body (post Task 4 wiring), where a
    continuation line without its own `--` would stop being a comment at all.
    Blank separator lines are left bare (harmless inside a .sql file, and
    unchanged from the pre-existing spacing to minimize diff against callers
    still on the markdown path). A child's column-level `fdw_options` never
    appears here — child column definitions themselves never fold in (D13/
    DD-4), so neither does their per-column FDW options detail."""
    if not children:
        return []
    ordered = sorted(children, key=lambda c: c["name"])
    names = [c["name"] for c in ordered]
    lines = list(_comment_lines(f"分区子表（{len(names)}）：{', '.join(names)}"))
    lines.append("")
    for c in ordered:
        own_indexes = [idx for idx in c.get("indexes", []) if not idx.get("inherited_from")]
        local_constraints = [con for con in c.get("constraints", []) if con.get("is_local")]
        options = c.get("options") or []
        tablespace = c.get("tablespace")
        am = c.get("access_method")
        is_foreign = c.get("kind") == "foreign_table"
        if not (
            own_indexes
            or local_constraints
            or options
            or tablespace
            or (am and am != "heap")
            or is_foreign
        ):
            continue
        lines.extend(_comment_lines(f"`{c['name']}`"))
        for idx in sorted(own_indexes, key=lambda i: i["name"]):
            lines.extend(_comment_lines(f"  自有索引 `{idx['name']}`: `{idx['definition']}`"))
        for con in sorted(local_constraints, key=lambda c: c["name"]):
            lines.extend(_comment_lines(f"  本地约束 `{con['name']}`: `{con['definition']}`"))
        if options:
            lines.extend(_comment_lines(f"  存储参数 `{', '.join(options)}`"))
        if tablespace:
            lines.extend(_comment_lines(f"  表空间 `{tablespace}`"))
        if am and am != "heap":
            lines.extend(_comment_lines(f"  访问方法 `{am}`"))
        if is_foreign:
            foreign = c.get("foreign") or {}
            server_line = f"  外部表 SERVER `{foreign.get('server')}`"
            foreign_options = foreign.get("options") or []
            if foreign_options:
                server_line += f" OPTIONS ({_render_fdw_options_items(foreign_options)})"
            lines.extend(_comment_lines(server_line))
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# D-M identifier validation — MUST run before any filesystem write/delete
# ---------------------------------------------------------------------------


def _is_pure_dot_string(value: str) -> bool:
    """[impl-review-fix] F-D: True for "." / ".." / "..." / etc — any non-empty
    string made up ENTIRELY of dots. IDENTIFIER_RE (`^[A-Za-z0-9_.]+$`) allows "."
    as a character (needed for e.g. schema-qualified-looking names elsewhere), which
    means it also matches "." and ".." verbatim — and a schema/object name of ".."
    turned straight into a path segment (`dbmeta_dir / ".." / "tables"`) escapes
    .dbmeta/ entirely and can overwrite files in the parent directory (reproduced:
    it clobbered the repo root's README.md). Rejecting pure-dot strings up front is
    the primary defense; `_assert_inside` below is the belt-and-suspenders check
    against the resulting filesystem path itself."""
    return bool(value) and set(value) == {"."}


def _validate_identifier(schema: str, name: str) -> None:
    if (
        not IDENTIFIER_RE.match(schema)
        or not IDENTIFIER_RE.match(name)
        or _is_pure_dot_string(schema)
        or _is_pure_dot_string(name)
    ):
        raise CollectFormatError(
            f"对象名 `{schema}.{name}` 含 dbmeta 文件名/托管块不支持的字符",
            "PG 允许引号标识符或注释分隔序列，dbmeta 文件布局按名原样落盘；"
            "纯点串（`.`/`..`）会被当作路径穿越片段，逃逸出 .dbmeta/ 目录",
            "重命名该对象，或在 dbmeta 能力升版时引入可逆编码",
        )


def _assert_inside(dbmeta_dir: Path, candidate: Path) -> None:
    """[impl-review-fix] F-D: belt-and-suspenders path-containment check, run
    BEFORE any write/delete touches `candidate`. `_validate_identifier`'s pure-dot
    rejection is the primary defense; this catches anything that slips past it
    (e.g. a future identifier rule change) by resolving the actual filesystem path
    and asserting it stays inside dbmeta_dir. Not just for schema="..": a resolved
    escape could also come from an object name containing "." segments once joined
    onto a directory (kind_dir / f"{name}.md")."""
    dbmeta_resolved = dbmeta_dir.resolve()
    candidate_resolved = candidate.resolve()
    if not candidate_resolved.is_relative_to(dbmeta_resolved):
        raise CollectFormatError(
            f"计算出的写盘路径 `{candidate}` 解析后落在 .dbmeta/ 目录之外",
            "schema/对象名里的路径片段（如 `..`）在拼接为文件路径后发生了目录穿越",
            "重命名该 schema/对象，或在 dbmeta 能力升版时引入可逆编码；本次运行不写不删任何文件",
        )


def _validate_function_ident(schema: str, name: str, identity_args: str) -> None:
    """D-M also covers the parenthesized identity_args of a function's managed-block
    ident (`pg-dict:fn:<name>(<identity_args>)`) — a literal "--" there would just
    as surely prematurely close the HTML comment as one in the name itself."""
    _validate_identifier(schema, name)
    if "--" in identity_args:
        raise CollectFormatError(
            f"函数托管块名 `pg-dict:fn:{name}({identity_args})` 含 `--`，会提前闭合 HTML 注释",
            "PG 参数类型/名可能包含连字符组合，但 dbmeta 托管块分隔符依赖 `-->` 不被提前闭合",
            "重命名该参数或类型别名，或在 dbmeta 能力升版时引入可逆编码",
        )
    # B6: identity_args is spliced into the marker line itself
    # (`-- pg-dict:fn:<name>(<identity_args>):start`), so a line break inside it
    # (a quoted type name containing "\n") would split the marker across two
    # physical lines and SQL_BLOCK_RE could never re-find the block on regen.
    if "\n" in identity_args or "\r" in identity_args:
        raise CollectFormatError(
            f"函数托管块名 `pg-dict:fn:{name}({identity_args!r})` 含换行，会把托管块标记行拆成两行",
            "参数类型名（quoted identifier）含字面换行，而托管块标记必须是单独一整行",
            "重命名该参数类型，或在 dbmeta 能力升版时引入可逆编码",
        )


def validate_identifiers(schemas: list[dict], dbmeta_dir: Path) -> None:
    """D-M: every schema name and every table/view/function name (plus, for
    functions, their identity_args) MUST match `^[A-Za-z0-9_.]+$` (the class has
    no "-", so "--" is excluded by construction). Table names include partition
    children folded into their parent's block (T42) — they only become text, but
    the docstring's "every table name" claim must hold literally. Runs across ALL
    schemas/objects before `run()` performs any write or delete (dbmeta spec
    目录契约 场景:非法标识符 fail-loud: ".dbmeta/ 无任何文件变化").

    T43: within one schema, names of the same kind that differ only by case
    (`Users` vs `users`) would map to ONE file on a case-insensitive filesystem
    (macOS APFS default) and the later one would silently clobber the earlier —
    rejected here as a fail-loud regardless of the host filesystem, so the
    outcome doesn't depend on where render runs.

    [impl-review-fix] F-D: also takes `dbmeta_dir` now so every schema's would-be
    directory path (`dbmeta_dir / schema`) can be asserted (`_assert_inside`) to
    still resolve inside .dbmeta/ — belt-and-suspenders on top of the pure-dot
    identifier rejection, before any write/delete happens anywhere in `run()`."""
    for schema_obj in schemas:
        schema = schema_obj["schema"]
        _validate_identifier(schema, schema)
        _assert_inside(dbmeta_dir, dbmeta_dir / schema)

        tables = schema_obj.get("tables", [])
        parents, children_by_parent = group_children(tables)
        # relation-ddl-equivalence T2.3: every child's `partition_of` value MUST
        # also pass the D-M identifier shape — it lands as text in the parent's
        # folded-child comment lines (T2.2) and, for an orphan child promoted by
        # resolve_orphan_children below, in the `ORPHAN_PARTITION_NOTE` comment
        # too. Checked BEFORE resolve_orphan_children pops entries out of
        # `children_by_parent` so this loop still sees every child regardless of
        # whether it ends up folded or promoted to an independent file.
        for cs in children_by_parent.values():
            for c in cs:
                _validate_identifier(schema, c["partition_of"])
        resolve_orphan_children(parents, children_by_parent)
        for t in parents:
            _validate_identifier(schema, t["name"])
        for cs in children_by_parent.values():
            for c in cs:
                _validate_identifier(schema, c["name"])
        _reject_case_collisions(schema, "table", [t["name"] for t in parents])

        views = schema_obj.get("views") or []
        for v in views:
            _validate_identifier(schema, v.get("name", ""))
        _reject_case_collisions(schema, "view", [v.get("name", "") for v in views])

        functions = schema_obj.get("functions") or []
        for f in functions:
            _validate_function_ident(schema, f.get("name", ""), f.get("identity_args") or "")
        _reject_case_collisions(schema, "fn", sorted({f.get("name", "") for f in functions}))


def validate_function_definitions(schemas: list[dict]) -> None:
    """DD-4 (design.md, spec-review-amendment): `functions[].definition`
    (`pg_get_functiondef` output) MUST be present — a collect JSON produced by a
    pre-DD-8 db-collect.sql lacks this key entirely, and rendering executable SQL
    DDL from just `source` (prosrc) would silently omit the function's own `CREATE
    OR REPLACE FUNCTION ...` shell. This runs as a full pre-flight batch across
    EVERY schema's EVERY function — same as validate_identifiers — BEFORE run()
    performs any filesystem write/delete, so a later schema's stale collect
    output can't leave an earlier schema half-written (this check MUST NOT be
    inlined into a per-schema render path for that reason)."""
    for schema_obj in schemas:
        schema = schema_obj["schema"]
        for f in schema_obj.get("functions") or []:
            if not f.get("definition"):
                name = f.get("name", "")
                identity_args = f.get("identity_args") or ""
                raise CollectFormatError(
                    f"函数 {schema}.{name}({identity_args}) 缺少 definition 键，"
                    "无法生成可执行 DDL",
                    "collect JSON 来自旧版 shared/db-collect.sql（尚未采集"
                    " pg_get_functiondef）",
                    "重跑 shared/db-collect.sh 重新采集后再执行 /pg-dict",
                )


def validate_managed_marker_collisions(schemas: list[dict]) -> None:
    """[impl-review-fix] V1 (code-review Important, outside-voice; design.md
    DD-4 discipline): a function's or view's own `definition` (pg_get_
    functiondef/pg_get_viewdef output) MUST NOT contain a line shaped like an
    SQL_SYNTAX managed-block marker (`-- pg-dict:<ident>:start/end`, any ident)
    — render_function_ddl/render_view_ddl write `definition` VERBATIM into the
    object file's managed-block body, and SQL_BLOCK_RE's non-greedy match would
    then treat that embedded line as the block's real boundary on the NEXT
    re-generation, silently corrupting the parse (definition cut apart mid-body,
    trailing content misfiled as permanent human annotation). Runs as a full
    pre-flight batch across EVERY schema's EVERY function/view — same as
    validate_identifiers/validate_function_definitions — BEFORE run() performs
    any filesystem write/delete (this check MUST NOT be inlined into a
    per-object render path, for the same half-written-schema reason those two
    checks document).

    [spec-review-amendment Q2] (design.md TG-17「托管块标记伪造」row): extended
    beyond function/view `definition` to every OTHER catalog string this
    change lets land verbatim (possibly split across several `_comment_lines`
    lines) inside a managed block or a folded-child comment: table/column/
    view/view-column/function `comment`, `tables[].foreign.server` and each
    `foreign.options[]` element, each `columns[].fdw_options[]` element, each
    table/view `indexes[].definition`, table `constraints[].definition` and
    `triggers[].definition`, and each `options[]` element (tables[], views[],
    and either's `indexes[].options[]`) — a value embedding a line shaped like
    `-- pg-dict:<ident>:end` would render harmlessly on THIS generation but be
    mistaken by SQL_BLOCK_RE for the block's own end marker on the NEXT one,
    silently splitting the file. Uses the same shared `_reject_marker_line`
    helper (cause/fix text reused verbatim from the original function/view
    check above) for every field.

    B6 (issues cleanup r3): also covers the pre-existing fields that reach the
    block through `quote_ident` (table/view/index `tablespace`, `access_method`)
    or verbatim (`partition_key`, table/view `columns[].type`, table
    `columns[].default`). Function `identity_args` is the one field that lands
    in the marker line ITSELF rather than the body, so its newline check lives
    in `_validate_function_ident` instead."""
    for schema_obj in schemas:
        schema = schema_obj["schema"]
        for f in schema_obj.get("functions") or []:
            match = MANAGED_MARKER_LINE_RE.search(f.get("definition") or "")
            if match:
                name = f.get("name", "")
                identity_args = f.get("identity_args") or ""
                raise CollectFormatError(
                    f"函数 {schema}.{name}({identity_args}) 的定义源码含托管块标记行"
                    f"「{match.group(0)}」，再生时会破坏托管块切分",
                    "对象源码中出现与 pg-dict 托管块同形的 -- pg-dict:<ident>:start/end 注释行",
                    "在数据库中修改该对象源码，去掉或改写该注释行后重新采集",
                )
        for v in schema_obj.get("views") or []:
            match = MANAGED_MARKER_LINE_RE.search(v.get("definition") or "")
            if match:
                name = v.get("name", "")
                raise CollectFormatError(
                    f"视图 {schema}.{name} 的定义源码含托管块标记行"
                    f"「{match.group(0)}」，再生时会破坏托管块切分",
                    "对象源码中出现与 pg-dict 托管块同形的 -- pg-dict:<ident>:start/end 注释行",
                    "在数据库中修改该对象源码，去掉或改写该注释行后重新采集",
                )

        # [spec-review-amendment Q2] function comment.
        for f in schema_obj.get("functions") or []:
            name = f.get("name", "")
            identity_args = f.get("identity_args") or ""
            _reject_marker_line(f"{schema}.{name}({identity_args})", f.get("comment"), "comment")

        for t in schema_obj.get("tables") or []:
            if not isinstance(t, dict):
                continue
            tname = t.get("name", "")
            table_owner = f"{schema}.{tname}"
            _reject_marker_line(table_owner, t.get("comment"), "comment")
            # access_method / tablespace ride into the block raw via quote_ident
            # (a quoted identifier may legally contain "\n"); partition_key
            # (pg_get_partkeydef) and columns[].type / columns[].default
            # (format_type / pg_get_expr — a string-literal default can carry a
            # literal newline) are spliced in verbatim. Any of them could land a
            # forged marker at a physical line start (issue B6).
            _reject_marker_line(table_owner, t.get("access_method"), "access_method")
            _reject_marker_line(table_owner, t.get("tablespace"), "tablespace")
            _reject_marker_line(table_owner, t.get("partition_key"), "partition_key")
            for col in t.get("columns") or []:
                if not isinstance(col, dict):
                    continue
                col_owner = f"{table_owner}.{col.get('name', '')}"
                _reject_marker_line(col_owner, col.get("comment"), "comment")
                _reject_marker_line(col_owner, col.get("type"), "type")
                _reject_marker_line(col_owner, col.get("default"), "default")
                for oidx, opt in enumerate(col.get("fdw_options") or []):
                    _reject_marker_line(col_owner, opt, f"fdw_options[{oidx}]")
            foreign = t.get("foreign")
            if isinstance(foreign, dict):
                _reject_marker_line(table_owner, foreign.get("server"), "foreign.server")
                for oidx, opt in enumerate(foreign.get("options") or []):
                    _reject_marker_line(table_owner, opt, f"foreign.options[{oidx}]")
            for oidx, opt in enumerate(t.get("options") or []):
                _reject_marker_line(table_owner, opt, f"options[{oidx}]")
            for idx_entry in t.get("indexes") or []:
                if isinstance(idx_entry, dict):
                    idx_owner = f"{table_owner}.{idx_entry.get('name', '')}"
                    _reject_marker_line(idx_owner, idx_entry.get("definition"), "definition")
                    # B6: also covers the constraint-backing index, whose
                    # tablespace is looked up from this same indexes[] entry.
                    _reject_marker_line(idx_owner, idx_entry.get("tablespace"), "tablespace")
                    for oidx, opt in enumerate(idx_entry.get("options") or []):
                        _reject_marker_line(idx_owner, opt, f"options[{oidx}]")
            for con in t.get("constraints") or []:
                if isinstance(con, dict):
                    con_owner = f"{table_owner}.{con.get('name', '')}"
                    _reject_marker_line(con_owner, con.get("definition"), "definition")
            for trg in t.get("triggers") or []:
                if isinstance(trg, dict):
                    trg_owner = f"{table_owner}.{trg.get('name', '')}"
                    _reject_marker_line(trg_owner, trg.get("definition"), "definition")

        for v in schema_obj.get("views") or []:
            if not isinstance(v, dict):
                continue
            vname = v.get("name", "")
            view_owner = f"{schema}.{vname}"
            _reject_marker_line(view_owner, v.get("comment"), "comment")
            # See table loop above: access_method / tablespace (quote_ident) and
            # columns[].type (verbatim) are marker-checked here too (issue B6).
            _reject_marker_line(view_owner, v.get("access_method"), "access_method")
            _reject_marker_line(view_owner, v.get("tablespace"), "tablespace")
            for col in v.get("columns") or []:
                if isinstance(col, dict):
                    col_owner = f"{view_owner}.{col.get('name', '')}"
                    _reject_marker_line(col_owner, col.get("comment"), "comment")
                    _reject_marker_line(col_owner, col.get("type"), "type")
            for oidx, opt in enumerate(v.get("options") or []):
                _reject_marker_line(view_owner, opt, f"options[{oidx}]")
            for idx_entry in v.get("indexes") or []:
                if isinstance(idx_entry, dict):
                    idx_owner = f"{view_owner}.{idx_entry.get('name', '')}"
                    _reject_marker_line(idx_owner, idx_entry.get("definition"), "definition")
                    _reject_marker_line(idx_owner, idx_entry.get("tablespace"), "tablespace")
                    for oidx, opt in enumerate(idx_entry.get("options") or []):
                        _reject_marker_line(idx_owner, opt, f"options[{oidx}]")


def _reject_marker_line(owner: str, text: object, field: str) -> None:
    """[spec-review-amendment Q2] shared collision check used by
    `validate_managed_marker_collisions` for every field beyond function/view
    `definition`: raises the same three-line `CollectFormatError` as the
    original check (cause/fix reused verbatim) when `text` (typed `object` on
    purpose, T52: any non-str value — e.g. None — is treated as absent and
    skipped) contains a line shaped like a managed-block marker."""
    if not isinstance(text, str):
        return
    match = MANAGED_MARKER_LINE_RE.search(text)
    if match:
        raise CollectFormatError(
            f"{owner} 的 {field} 含托管块标记行「{match.group(0)}」，再生时会破坏托管块切分",
            "对象源码中出现与 pg-dict 托管块同形的 -- pg-dict:<ident>:start/end 注释行",
            "在数据库中修改该对象源码，去掉或改写该注释行后重新采集",
        )


def _reject_case_collisions(schema: str, kind: str, names: list[str]) -> None:
    """T43: fail-loud when two distinct names of one kind fold to the same
    lower-cased form — they'd share one `<name>.md` on a case-insensitive
    filesystem. `names` for functions is already de-duplicated (one file per
    function name, overloads share it)."""
    by_lower: dict[str, str] = {}
    for name in names:
        other = by_lower.setdefault(name.lower(), name)
        if other != name:
            raise CollectFormatError(
                f"{schema} 内 {OBJECT_LABELS[kind]} `{other}` 与 `{name}` 仅大小写不同",
                "dbmeta 一对象一文件、按名落盘；大小写不敏感文件系统（macOS APFS 默认）上"
                "两者会写到同一文件，后者静默覆盖前者",
                "重命名其中一个对象（本仓命名规范为全小写）",
            )


# ---------------------------------------------------------------------------
# Per-object file header / managed-block merge
# ---------------------------------------------------------------------------


SQL_HEADER_BASE_TMPL = (
    "-- {schema}.{name} {label}\n"
    "-- 本文件由 pg-dict skill 自动生成。托管块（-- pg-dict:<ident>:start/end）"
    "内容会在再生时整体重写；块外文本由人工维护，再生时逐字保留，人工注记 MUST "
    "写成 -- 注释行，否则本文件不可执行。\n"
)

SQL_HEADER_TABLE_EXTRA = (
    "-- 表 DDL 由 pg_catalog 拼装（不含 collation/所有者/权限），完整"
    "重建以 pg_dump 为准；触发器引用的函数在 ../functions/ 下，单文件不保证整库"
    "重放顺序。\n"
)

SQL_HEADER_FN_EXTRA = (
    "-- 函数文件按重载分块：每个重载（identity_args 不同）各占一个托管块"
    "（-- pg-dict:<ident>:start/end），本文件可能含多个块。\n"
)


def object_file_header(kind: str, schema: str, name: str, syntax: BlockSyntax) -> str:
    """The auto-generated leading header for an object file. This exact prefix is
    exempted from the "block-external text == hand-written annotation" judgement
    (D-C). Functions get a distinct explanation since a function file holds one
    block PER OVERLOAD rather than a single `<kind>:<name>` block.

    DD-7: under SQL_SYNTAX the header is a fixed `--`-comment prefix — its first
    two lines are IDENTICAL text across kinds (bar the 表|视图|函数 label on line
    1); tables and functions each get one extra explanatory line, views get none.
    Unlike the MD_SYNTAX header below, the SQL header's managed-block mention
    uses the literal placeholder text `<ident>` rather than this object's actual
    block identifier (design.md DD-7 gives this as fixed, verbatim text)."""
    label = OBJECT_LABELS[kind]
    if syntax is SQL_SYNTAX:
        header = SQL_HEADER_BASE_TMPL.format(schema=schema, name=name, label=label)
        if kind == "table":
            header += SQL_HEADER_TABLE_EXTRA
        elif kind == "fn":
            header += SQL_HEADER_FN_EXTRA
        return header

    if kind == "fn":
        explanation = (
            "<!-- 本文件由 pg-dict skill 自动生成。函数每个重载各占一个托管块"
            f"（`<!-- pg-dict:fn:{name}(<identity_args>):start/end -->`），内容会在"
            "再生时整体重写；块外文本由人工维护，再生时逐字保留。 -->\n\n"
        )
    else:
        explanation = (
            "<!-- 本文件由 pg-dict skill 自动生成。托管块"
            f"（`<!-- pg-dict:{kind}:{name}:start/end -->`）内容会在再生时整体重写；"
            "块外文本由人工维护，再生时逐字保留。 -->\n\n"
        )
    return f"# `{schema}.{name}` {label}\n\n" + explanation


def block_ident(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def wrap_block(ident: str, body: str, syntax: BlockSyntax) -> str:
    return syntax.start_fmt.format(ident=ident) + body + syntax.end_fmt.format(ident=ident)


def parse_segments(text: str, syntax: BlockSyntax) -> list[tuple]:
    """Split file text into ordered ('text', str) / ('block', ident, body)
    segments, per `syntax`'s block marker regex (DD-1)."""
    segments: list[tuple] = []
    pos = 0
    for m in syntax.block_re.finditer(text):
        if m.start() > pos:
            segments.append(("text", text[pos : m.start()]))
        segments.append(("block", m.group("ident"), m.group("body")))
        pos = m.end()
    if pos < len(text):
        segments.append(("text", text[pos:]))
    return segments


def merge_blocks(
    existing_text: str | None,
    header: str,
    blocks: list[tuple[str, str]],
    syntax: BlockSyntax,
) -> str:
    """Merge the given `[(ident, body), ...]` — every block that IS present after
    this run — into `existing_text`, preserving all block-external text verbatim.
    Creates a fresh file (header + all blocks, in the given order) when
    `existing_text` is None. A block whose ident is found in `existing_text` but is
    NOT in `blocks` (an overload that disappeared while siblings remain, e.g.) is
    simply dropped — no orphan banner: the file as a whole still represents a live
    object, so D-C's file-level orphan handling doesn't apply here (that path is
    process_removed_object_file(), used when the WHOLE object/file is gone). A
    block present in `blocks` but not found in `existing_text` (new file, or a
    manually-edited file that dropped a block) is appended at the end rather than
    silently lost.

    Single-block callers (table/view files) just pass a one-element `blocks` list —
    this is a generalization of what used to be merge_single_block()."""
    present_idents = {ident for ident, _ in blocks}
    body_by_ident = dict(blocks)

    if existing_text is None:
        return header + "".join(wrap_block(ident, body, syntax) for ident, body in blocks)

    segments = parse_segments(existing_text, syntax)
    out: list[str] = []
    seen: set[str] = set()
    for seg in segments:
        if seg[0] == "block":
            ident = seg[1]
            if ident in present_idents:
                out.append(wrap_block(ident, body_by_ident[ident], syntax))
                seen.add(ident)
            # else: this block's object/overload is no longer present — dropped.
        else:
            out.append(seg[1])

    missing = [(ident, body) for ident, body in blocks if ident not in seen]
    if not missing:
        return "".join(out)

    # T36: keep the on-disk block order consistent with `blocks` order (for
    # function files: identity_args-sorted overloads, same as a fresh render).
    # A missing block is inserted right before the first KEPT block that follows
    # it in `blocks`; when no kept block follows, it is appended at the end.
    order = {ident: i for i, (ident, _) in enumerate(blocks)}
    kept_idents = [ident for ident in order if ident in seen]
    text_blocks: dict[str, list[str]] = {}  # kept ident -> missing blocks to insert before it
    tail: list[str] = []
    for ident, body in missing:
        successor = next((k for k in kept_idents if order[k] > order[ident]), None)
        if successor is None:
            tail.append(wrap_block(ident, body, syntax))
        else:
            text_blocks.setdefault(successor, []).append(wrap_block(ident, body, syntax))

    result: list[str] = []
    for seg in segments:
        if seg[0] == "block":
            ident = seg[1]
            if ident not in present_idents:
                continue
            result.extend(text_blocks.get(ident, []))
            result.append(wrap_block(ident, body_by_ident[ident], syntax))
        else:
            result.append(seg[1])
    if tail:
        prefix = "".join(result)
        if prefix and not prefix.endswith("\n"):
            result.append("\n")
        result.extend(tail)
    return "".join(result)


def _strip_header_prefix(text: str, header: str) -> str:
    return text[len(header) :] if text.startswith(header) else text


def _has_annotation(text: str, syntax: BlockSyntax) -> bool:
    """True if `text` contains any non-empty, non-comment content, per `syntax`'s
    definition of "comment" (DD-2: MD_SYNTAX strips HTML comments before judging;
    SQL_SYNTAX's comment_re strips nothing, so ANY non-blank line — "--" included
    — counts, since a "--" line is the only way to write hand-written text in an
    executable .sql file)."""
    return bool(syntax.comment_re.sub("", text).strip())


def process_removed_object_file(path: Path, header: str, syntax: BlockSyntax) -> bool:
    """D-C: the object this file was generated for is no longer in the collect
    result (for a function file: EVERY overload is gone). Returns True iff the file
    was deleted from disk."""
    existing = path.read_text(encoding="utf-8")
    segments = parse_segments(existing, syntax)
    remaining = "".join(seg[1] for seg in segments if seg[0] == "text")
    checked = _strip_header_prefix(remaining, header)
    # DD-2 逐字："去掉文件头固定前缀与孤立横幅后" — the banner itself MUST NOT be
    # mistaken for hand-written annotation. Under MD_SYNTAX this is already a
    # no-op in practice (the banner is an HTML comment, stripped by comment_re
    # inside _has_annotation), but under SQL_SYNTAX comment_re is a no-op regex
    # (DD-1) so the banner's `--` line would otherwise count as annotation,
    # permanently blocking deletion of a file that already carries the banner.
    checked = checked.replace(syntax.orphan_banner, "", 1)

    if not _has_annotation(checked, syntax):
        path.unlink()
        return True

    if syntax.orphan_banner not in remaining:
        if remaining.startswith(header):
            remaining = header + syntax.orphan_banner + remaining[len(header) :]
        else:
            remaining = syntax.orphan_banner + remaining

    if remaining != existing:
        path.write_text(remaining, encoding="utf-8")
    return False


# ---------------------------------------------------------------------------
# D-D: trigger -> function resolution / reverse index
# ---------------------------------------------------------------------------


def parse_trigger_function_name(definition: str) -> str | None:
    """Extract the (optionally schema-qualified) function name a trigger calls
    from its `pg_get_triggerdef` text. Returns None if the definition doesn't match
    the expected `EXECUTE FUNCTION|PROCEDURE name(` shape (defensive — every real
    trigger definition PG emits does match, but a malformed/fabricated fixture
    shouldn't crash rendering)."""
    m = TRIGGER_FN_RE.search(definition)
    return m.group(1) if m else None


def split_trigger_fn_ref(raw: str, default_schema: str) -> tuple[str, str]:
    """Split a (possibly schema-qualified) function reference captured by
    TRIGGER_FN_RE into (schema, name); an unqualified reference resolves against
    the trigger's own table's schema (D-D)."""
    if "." in raw:
        schema, name = raw.split(".", 1)
        return schema, name
    return default_schema, raw


def function_file_link(from_schema: str, target_schema: str, fn_name: str, ext: str) -> str:
    """D-D relative-link rule: same schema -> `../functions/<fn><ext>`; cross
    schema -> `../../<schema>/functions/<fn><ext>` (both relative to a
    `tables/<t><ext>` file, which is one directory level below the schema
    root, same as `functions/`). `ext` is required (T30: the old `.md` default
    had no production caller left after DD-6 made object files `.sql`); the
    only production call site (render_table_ddl via resolve_trigger_link)
    passes `ext=".sql"`."""
    if target_schema == from_schema:
        return f"../functions/{fn_name}{ext}"
    return f"../../{target_schema}/functions/{fn_name}{ext}"


def resolve_trigger_link(
    schema: str,
    definition: str,
    func_names_index: dict[str, dict[str, list[str]]],
    ext: str,
) -> tuple[str | None, str | None]:
    """Resolve a trigger's `definition` to (fn_name, link) for rendering inside a
    table's trigger list. `link` is None when the target function isn't in the
    collect set (extension/system function, per spec "扩展函数不链接") — the
    caller then renders only the bare name. `fn_name` is None only when the
    trigger definition itself couldn't be parsed (defensive fallback). `ext` is
    threaded straight through to function_file_link (required — see its
    docstring)."""
    raw = parse_trigger_function_name(definition)
    if not raw:
        return None, None
    target_schema, fn_name = split_trigger_fn_ref(raw, schema)
    candidates = func_names_index.get(target_schema, {}).get(fn_name)
    if not candidates:
        return fn_name, None
    return fn_name, function_file_link(schema, target_schema, fn_name, ext=ext)


def _build_func_names_index(schemas: list[dict]) -> dict[str, dict[str, list[str]]]:
    """{schema: {function_name: [identity_args, ...]}} across the whole collect
    result — used both to decide whether a trigger's called function is "in the
    collect set" (D-D link-or-name-only) and to resolve which specific overload a
    trigger's reverse-index entry attaches to."""
    index: dict[str, dict[str, list[str]]] = {}
    for schema_obj in schemas:
        schema = schema_obj["schema"]
        per_schema = index.setdefault(schema, {})
        for f in schema_obj.get("functions") or []:
            name = f.get("name")
            if not name:
                continue
            per_schema.setdefault(name, []).append(f.get("identity_args") or "")
    return index


def _build_trigger_refs(
    schemas: list[dict], func_names_index: dict[str, dict[str, list[str]]]
) -> dict[tuple[str, str, str], list[str]]:
    """{(schema, fn_name, identity_args): [trigger display strings]} — the reverse
    index a function file's "被以下触发器引用" section reads from. Only scans
    TOP-LEVEL tables (post partition-orphan-resolution, same set that actually gets
    its own rendered file) — a partition child folded into its parent isn't
    independently rendered, so its own triggers (clones of the parent's, tgparentid
    non-null) don't participate here either (matches the fact the fold-in summary
    doesn't render triggers, only self-owned indexes/constraints per DD-4).

    A trigger function is, by PG's own trigger-mechanism constraint, always
    zero-argument — so `identity_args == ""` is picked when available among the
    matching name's overloads; if not (a fixture/edge case with no "" overload),
    falls back to attaching the reference to every overload sharing that name
    (rare, and never happens with real PG-collected data — simplification per the
    project's "don't perfect low-probability edges" principle)."""
    refs: dict[tuple[str, str, str], list[str]] = {}
    for schema_obj in schemas:
        schema = schema_obj["schema"]
        tables = schema_obj.get("tables", [])
        parents, children_by_parent = group_children(tables)
        resolve_orphan_children(parents, children_by_parent)
        for t in parents:
            for trg in t.get("triggers", []) or []:
                definition = trg.get("definition") or ""
                raw = parse_trigger_function_name(definition)
                if not raw:
                    continue
                target_schema, fn_name = split_trigger_fn_ref(raw, schema)
                candidates = func_names_index.get(target_schema, {}).get(fn_name)
                if not candidates:
                    continue
                chosen = [""] if "" in candidates else candidates
                display = f"`{schema}.{t['name']}.{trg['name']}`"
                for identity_args in chosen:
                    refs.setdefault((target_schema, fn_name, identity_args), []).append(display)
    return refs


# ---------------------------------------------------------------------------
# Table file rendering (full body: columns/indexes/constraints/triggers/reltuples/
# partition fold — spec DD-1/DD-4, ADDED "约束、触发器、函数与视图渲染")
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task 3 (design.md DD-3 / memo D8): table DDL rendering — pure function,
# collect table dict -> executable SQL text (the managed-block BODY only; the
# surrounding file header / `-- pg-dict:table:<name>:start/end` wrapper is
# object_file_header()/wrap_block()'s job, wired up at the Task-4 call site —
# render_table_ddl itself does no file I/O and knows nothing about headers or
# block markers).
#
# Fixed six-statement order (memo D8), all pass-through `definition`/
# `pg_get_*def` text used VERBATIM (DD-5 — no re-processing, no SQL parsing):
#   1. CREATE SEQUENCE IF NOT EXISTS <seq>;   (sequence_names_from_defaults)
#   2. CREATE TABLE <schema>.<name> (...)[ PARTITION BY <key>];
#   3. ALTER TABLE ... ADD CONSTRAINT <con> <definition>;   (every contype)
#   4. CREATE INDEX ... ;  (skips indexes backed by a p/u/x constraint, and
#      indexes inherited from a partition parent)
#   5. -- 触发器函数：<link-or-name>\n<trigger definition>;[\nALTER TABLE ...
#      {DISABLE|ENABLE REPLICA|ENABLE ALWAYS} TRIGGER <name>;]
#   6. COMMENT ON TABLE/COLUMN ... IS '<esc>';
# plus `--`-comment notes (reltuples estimate, orphan/fallback note,
# partition-child fold summary) that carry no executable meaning. A table with
# `kind == "foreign_table"` instead renders `CREATE FOREIGN TABLE ... SERVER
# ...[ OPTIONS (...)]` in place of steps 1-2 (relation-ddl-equivalence T2.5) —
# steps 3/5/6 still apply unchanged, and step 4 is naturally empty.
# ---------------------------------------------------------------------------


def _render_reloptions_items(options: list | None) -> str:
    """Shared by render_table_ddl and render_view_ddl (relation-ddl-equivalence
    T2.1): reloptions[] -> the comma-joined `key='value'` (or bare `key` when
    the item carries no `=`) item list, WITHOUT any surrounding `WITH (...)` /
    `SET (...)` wrapper — callers splice this into whichever wrapper their
    statement needs (CREATE ... WITH (...), or ALTER INDEX ... SET (...)).
    Each item's `name` segment is emitted UNQUOTED/unescaped on purpose (same
    reasoning as the view-reloptions-ddl D1/D2 docstring below): reloptions
    names are validated against `_RELOPTION_NAME_RE` in `_validate_schemas`
    (B5) before any file is written, so a tampered element cannot reach here.
    Returns "" for an empty/None list — callers only wrap non-empty results."""
    items = []
    for opt in options or []:
        key, sep, value = opt.partition("=")
        items.append(f"{key}={sql_literal(value)}" if sep else key)
    return ", ".join(items)


def _render_fdw_options_items(options: list | None) -> str:
    """relation-ddl-equivalence T2.1/T2.5: FDW `options`-shaped list (a table's
    `foreign.options` / a foreign-table column's `fdw_options`) -> the
    comma-joined `key 'value'` item list, WITHOUT any surrounding `OPTIONS
    (...)` wrapper — callers splice this into whichever `OPTIONS (...)` clause
    their statement needs (column-level, or the table's `SERVER ... OPTIONS
    (...)`). Each item is split on its FIRST `=` (same convention PG's own
    `untransformRelOptions` uses to store these in the catalog); `key` is
    emitted through `quote_ident` (unlike `_render_reloptions_items`'s bare
    reloptions names, an FDW option key has no namespace-dot exception to
    protect and is safest always quoted-when-needed) and `value` through
    `sql_literal`. `_validate_schemas` (B5, [spec-review-amendment]) guarantees
    every element contains `=` with a non-empty `key` segment before any
    render call reaches here, so `.partition("=")`'s `sep` is always truthy —
    there is no "bare key, no value" branch (unlike reloptions, where a bare
    `key` with no `=` is legal). Returns "" for an empty/None list — callers
    only wrap non-empty results."""
    items = []
    for opt in options or []:
        key, _sep, value = opt.partition("=")
        items.append(f"{quote_ident(key)} {sql_literal(value)}")
    return ", ".join(items)


def _render_column_defs(columns: list[dict], with_fdw: bool) -> list[str]:
    """relation-ddl-equivalence-r2 Task2 fix1: shared column-definition-line
    builder for `render_table_ddl`'s foreign and non-foreign branches — both
    build `  <quoted name> <type> [OPTIONS (...)] [DEFAULT ...] [NOT NULL]`
    lines identically; the only divergence is the foreign branch's
    column-level `OPTIONS (...)` clause (from `fdw_options`), spliced between
    the type and DEFAULT/NOT NULL. `with_fdw=False` skips that clause
    entirely (a column's `fdw_options`, if present, is ignored) so the
    non-foreign branch's output is unaffected by this extraction."""
    col_lines = []
    for col in columns:
        parts = [quote_ident(col["name"]), col["type"]]
        if with_fdw:
            fdw_options = col.get("fdw_options") or []
            if fdw_options:
                parts.append(f"OPTIONS ({_render_fdw_options_items(fdw_options)})")
        default = col.get("default")
        if default:
            parts.append(f"DEFAULT {default}")
        if not col.get("nullable"):
            parts.append("NOT NULL")
        col_lines.append("  " + " ".join(parts))
    return col_lines


def _render_index_tablespace_alter(qschema: str, index_name: str, tablespace) -> str | None:
    """relation-ddl-equivalence T2.1/T2.2: an index's `tablespace` (non-empty
    str) round-trips as a standalone `ALTER INDEX ... SET TABLESPACE ...;`
    statement immediately after the statement that creates/implies the index
    (a `CREATE INDEX ...;` line, or an `ALTER TABLE ... ADD CONSTRAINT` for a
    p/u/x-constraint-backed index whose own `pg_get_constraintdef()` text
    carries no `USING INDEX TABLESPACE` — design.md Decisions/Risks). Returns
    None (render nothing) when `tablespace` is None/empty — `qschema` MUST
    already be `quote_ident`-ed by the caller (both call sites already hold
    that value)."""
    if not tablespace:
        return None
    return f"ALTER INDEX {qschema}.{quote_ident(index_name)} SET TABLESPACE {quote_ident(tablespace)};"


def render_table_ddl(
    schema: str,
    table: dict,
    children: list[dict] | None,
    orphan_note: str | None,
    func_names_index: dict[str, dict[str, list[str]]],
) -> str:
    lines: list[str] = []

    if orphan_note:
        lines.extend(_comment_lines(orphan_note))

    reltuples_note = (
        "-- 行数估计（pg_class.reltuples 估计值，按数量级分档；新建表在 ANALYZE 前恒为 0，"
        "不代表真实为空"
    )
    if table.get("kind") == "partitioned_table":
        reltuples_note += "；父表自身不持有数据行，不代表分区总行数"
    reltuples_note += f"）：{format_reltuples(table.get('reltuples'))}"
    lines.append(reltuples_note)

    lines.append("")

    qschema = quote_ident(schema)
    qname = f"{qschema}.{quote_ident(table['name'])}"

    columns = table.get("columns", [])
    is_foreign = table.get("kind") == "foreign_table"

    if is_foreign:
        # relation-ddl-equivalence T2.5 (design.md「数据流图」/decision-memo):
        # an external-table's file has NO CREATE SEQUENCE / PARTITION BY /
        # USING / WITH / TABLESPACE section — those PG storage/partitioning
        # concepts don't apply to `CREATE FOREIGN TABLE`. Column-level
        # `fdw_options` (attfdwoptions) round-trips as an `OPTIONS (...)`
        # clause between the column's type and its DEFAULT/NOT NULL, and the
        # statement itself carries `SERVER <name>[ OPTIONS (...)]` in place of
        # everything a plain CREATE TABLE would append after the column list.
        foreign = table.get("foreign") or {}
        col_lines = _render_column_defs(columns, with_fdw=True)
        create_table = f"CREATE FOREIGN TABLE {qname} (\n" + ",\n".join(col_lines) + "\n)"
        create_table += f" SERVER {quote_ident(foreign.get('server'))}"
        foreign_options = foreign.get("options") or []
        if foreign_options:
            create_table += f" OPTIONS ({_render_fdw_options_items(foreign_options)})"
        create_table += ";"
        lines.append(create_table)
        lines.append("")
    else:
        # 1. CREATE SEQUENCE IF NOT EXISTS ...;
        seqs = sequence_names_from_defaults(columns, schema)
        for seq in seqs:
            lines.append(f"CREATE SEQUENCE IF NOT EXISTS {seq};")
        if seqs:
            lines.append("")

        # 2. CREATE TABLE ... (+ PARTITION BY + USING + WITH + TABLESPACE)
        col_lines = _render_column_defs(columns, with_fdw=False)
        create_table = f"CREATE TABLE {qname} (\n" + ",\n".join(col_lines) + "\n)"
        partition_key = table.get("partition_key")
        if partition_key:
            create_table += f" PARTITION BY {partition_key}"
        access_method = table.get("access_method")
        if access_method and access_method != "heap":
            create_table += f" USING {quote_ident(access_method)}"
        table_options = table.get("options") or []
        if table_options:
            create_table += f" WITH ({_render_reloptions_items(table_options)})"
        table_tablespace = table.get("tablespace")
        if table_tablespace:
            create_table += f" TABLESPACE {quote_ident(table_tablespace)}"
        create_table += ";"
        lines.append(create_table)
        lines.append("")

    # 3. ALTER TABLE ... ADD CONSTRAINT ... (every contype, sorted by name)
    # [spec-review-amendment] p/u/x-constraint-backed indexes never get their
    # own CREATE INDEX (below) — pg_get_constraintdef() carries no
    # USING INDEX TABLESPACE, so a same-named indexes[] entry's tablespace/
    # options round-trip as ALTER INDEX lines right after ADD CONSTRAINT.
    all_indexes_by_name = {
        idx["name"]: idx for idx in (table.get("indexes", []) or [])
    }
    constraints = table.get("constraints", []) or []
    puex_names = {c["name"] for c in constraints if c.get("type") in ("p", "u", "x")}
    if constraints:
        for con in sorted(constraints, key=lambda c: c["name"]):
            lines.append(
                f"ALTER TABLE {qname} ADD CONSTRAINT {quote_ident(con['name'])} "
                f"{con['definition']};"
            )
            if con.get("type") in ("p", "u", "x"):
                backing = all_indexes_by_name.get(con["name"])
                if backing:
                    ts_line = _render_index_tablespace_alter(
                        qschema, con["name"], backing.get("tablespace")
                    )
                    if ts_line:
                        lines.append(ts_line)
                    backing_options = backing.get("options") or []
                    if backing_options:
                        lines.append(
                            f"ALTER INDEX {qschema}.{quote_ident(con['name'])} "
                            f"SET ({_render_reloptions_items(backing_options)});"
                        )
        lines.append("")

    # 4. CREATE INDEX ... (skip p/u/x-constraint-backed + inherited indexes)
    indexes = [
        idx
        for idx in (table.get("indexes", []) or [])
        if not idx.get("inherited_from") and idx["name"] not in puex_names
    ]
    if indexes:
        for idx in sorted(indexes, key=lambda i: i["name"]):
            lines.append(f"{idx['definition']};")
            ts_line = _render_index_tablespace_alter(qschema, idx["name"], idx.get("tablespace"))
            if ts_line:
                lines.append(ts_line)
        lines.append("")

    # 5. CREATE TRIGGER ... (+ enable-state ALTER when enabled != 'O')
    triggers = table.get("triggers", []) or []
    if triggers:
        for trg in sorted(triggers, key=lambda t: t["name"]):
            definition = trg.get("definition") or ""
            fn_name, link = resolve_trigger_link(schema, definition, func_names_index, ext=".sql")
            if fn_name and link:
                fn_display = link
            elif fn_name:
                fn_display = fn_name
            else:
                fn_display = "（未能解析调用函数）"
            lines.extend(_comment_lines(f"触发器函数：{fn_display}"))
            lines.append(f"{definition};")
            enabled = trg.get("enabled")
            stmt = TRIGGER_ENABLE_STATE_STMT.get(enabled) if enabled != "O" else None
            if stmt:
                lines.append(stmt.format(table=qname, name=quote_ident(trg["name"])))
        lines.append("")

    # 6. COMMENT ON TABLE / COMMENT ON COLUMN
    comment_lines: list[str] = []
    table_comment = (table.get("comment") or "").strip()
    if table_comment:
        comment_lines.append(f"COMMENT ON TABLE {qname} IS {sql_literal(table_comment)};")
    for col in columns:
        col_comment = (col.get("comment") or "").strip()
        if col_comment:
            comment_lines.append(
                f"COMMENT ON COLUMN {qname}.{quote_ident(col['name'])} IS "
                f"{sql_literal(col_comment)};"
            )
    if comment_lines:
        lines.extend(comment_lines)
        lines.append("")

    lines.extend(render_partition_children_lines(children or []))

    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# View file rendering (definition SQL + columns + COMMENT — D-N, ADDED "约束、
# 触发器、函数与视图渲染")
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task 3 (design.md DD-4): view DDL rendering — pure function, collect view
# dict -> executable SQL text (managed-block body only, same division of
# responsibility as render_table_ddl above). `view["definition"]`
# (`pg_get_viewdef`) is used VERBATIM — real-DB check (2026-08-29, read-only creds against dev DB):
# `pg_get_viewdef('pg_catalog.pg_tables'::regclass, true)` already ends in
# `);` — a trailing `;` is PART of pg_get_viewdef's own output, so render MUST
# NOT append a second one (unlike render_function_ddl below, where
# pg_get_functiondef's real output has NO trailing `;`).
# ---------------------------------------------------------------------------


def render_view_ddl(schema: str, view: dict) -> str:
    """Render one view / materialized view file body: `CREATE [MATERIALIZED]
    VIEW <q> [WITH (...)] AS` + `definition` + COMMENT ON lines.

    `options` (view-reloptions-ddl D1/D2): reloptions round-trip as a
    `WITH (...)` clause between the qualified name and `AS` — WITH sits in the
    CREATE header, before AS, so it never touches `pg_get_viewdef`'s own
    output (`definition`, which for a view already ends in its own trailing
    `;` — see the module comment above). Each `options[]` item's `name`
    segment is emitted UNQUOTED/unescaped on purpose: reloptions names are
    validated against PG's own storage-parameter registry (not user input),
    and some legitimately contain a namespace dot (e.g. `toast.*`) — passing
    that through `quote_ident` would double-quote the dot and break the
    syntax PG expects. The shape of that segment is pinned by
    `_RELOPTION_NAME_RE` in `_validate_schemas` (B5, T42) before any file is
    written, so a tampered element cannot reach this unquoted splice.

    relation-ddl-equivalence T2.2: `tablespace` round-trips the same way as
    `options` — a ` TABLESPACE <ts>` clause between `with_clause` and ` AS`.
    For `kind == "materialized_view"` with `populated is False`, `definition`
    is `rstrip()`-ed then `rstrip(";")`-ed (stripping ONLY the trailing `;`
    that's part of `pg_get_viewdef`'s own output, and only in this branch —
    every other byte of `definition` is untouched, DD-5) and a `WITH NO
    DATA;` line is appended; the view's own `indexes[]` (sorted by `name`)
    then each render as `<definition>;` (+ an `ALTER INDEX ... SET
    TABLESPACE ...;` line when that index's `tablespace` is non-empty — same
    reasoning as render_table_ddl: `pg_get_indexdef` puts `WHERE` last, so an
    inline `TABLESPACE` after it would be a syntax error). A plain `view`
    (kind == "view") ignores `populated`/`indexes` entirely — a materialized
    view can never itself hold indexes it isn't materialized_view.

    relation-ddl-equivalence T2.6 (decision-memo C8): `access_method` rounds
    trips as a ` USING <am>` clause between `qname` and `with_clause` — BEFORE
    `WITH (...)`, matching `CREATE MATERIALIZED VIEW`'s own grammar order
    (`... USING method WITH (...) AS query`). Only emitted when
    `kind == "materialized_view"` and `access_method` is a non-empty value
    other than `"heap"` — a plain VIEW has no storage AM (PG guarantees
    `relam = 0` → null for plain views, same reasoning as the `tablespace`
    gate above) and the common `"heap"` case renders byte-identically to
    before this change."""
    name = view.get("name", "")
    is_matview = view.get("kind") == "materialized_view"
    view_kind_sql = "MATERIALIZED VIEW" if is_matview else "VIEW"
    definition = (view.get("definition") or "").rstrip("\n")
    comment = (view.get("comment") or "").strip()
    qschema = quote_ident(schema)
    qname = f"{qschema}.{quote_ident(name)}"

    access_method = view.get("access_method")
    using_clause = (
        f" USING {quote_ident(access_method)}"
        if (is_matview and access_method and access_method != "heap")
        else ""
    )

    options = view.get("options") or []
    with_clause = f" WITH ({_render_reloptions_items(options)})" if options else ""

    tablespace = view.get("tablespace")
    # T46: TABLESPACE only applies to materialized views — a plain VIEW never
    # carries storage (PG guarantees reltablespace=0 → null for plain views).
    # Gate on is_matview so a stale/hand-edited {kind:view, tablespace:X} JSON
    # renders output unchanged for views (design invariant「kind=view ⇒ 输出不变」)
    # instead of emitting illegal `CREATE VIEW ... TABLESPACE`.
    tablespace_clause = (
        f" TABLESPACE {quote_ident(tablespace)}" if (is_matview and tablespace) else ""
    )

    not_populated = is_matview and view.get("populated") is False
    body_lines: list[str] = []
    if not_populated:
        body_lines.append(definition.rstrip().rstrip(";"))
        body_lines.append("WITH NO DATA;")
    else:
        body_lines.append(definition)

    if is_matview:
        for idx in sorted(view.get("indexes") or [], key=lambda i: i["name"]):
            body_lines.append(f"{idx['definition']};")
            ts_line = _render_index_tablespace_alter(qschema, idx["name"], idx.get("tablespace"))
            if ts_line:
                body_lines.append(ts_line)

    lines = [
        f"CREATE {view_kind_sql} {qname}{using_clause}{with_clause}{tablespace_clause} AS",
        *body_lines,
        "",
    ]

    comment_lines: list[str] = []
    if comment:
        comment_lines.append(f"COMMENT ON {view_kind_sql} {qname} IS {sql_literal(comment)};")
    for col in view.get("columns", []) or []:
        col_comment = (col.get("comment") or "").strip()
        if col_comment:
            comment_lines.append(
                f"COMMENT ON COLUMN {qname}.{quote_ident(col['name'])} IS "
                f"{sql_literal(col_comment)};"
            )
    lines.extend(comment_lines)

    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Function file rendering (per-overload block: `definition` verbatim + COMMENT
# + reverse-trigger-index comment — D-D/D-M).
#
# Task 3/4 (design.md DD-4): function DDL rendering — pure function, collect
# function dict -> executable SQL text (managed-block body only — one call
# per overload, the one-block-per-overload split _render_schema_functions
# wires up via _sync_object_dir). `func["definition"]` (`pg_get_functiondef`,
# DD-8) is used VERBATIM as the `CREATE OR REPLACE FUNCTION ...
# $function$...$function$` shell — the validate_function_definitions()
# pre-flight (run() batch, before any write) guarantees this key is
# present/non-empty before render_function_ddl is ever called, so no
# fallback-to-`source` path exists here (DD-4: MUST NOT).
# Real-DB check (2026-08-29, read-only creds against dev DB): `pg_get_functiondef(...)` output ends
# in `$function$\n` with NO trailing `;` — render appends one (unlike
# render_view_ddl above, where pg_get_viewdef's own output already ends in
# `;`). A `.sql` file has no markdown-fence concept, so render_function_ddl
# has no counterpart to the old fence-sizing helper.
# ---------------------------------------------------------------------------


def render_function_ddl(schema: str, func: dict, trigger_refs: list[str]) -> str:
    name = func.get("name", "")
    identity_args = func.get("identity_args") or ""
    definition = (func.get("definition") or "").rstrip("\n")
    comment = (func.get("comment") or "").strip()

    lines = [definition + ";", ""]

    if comment:
        lines.append(
            f"COMMENT ON FUNCTION {quote_ident(schema)}.{quote_ident(name)}"
            f"({identity_args}) IS {sql_literal(comment)};"
        )
        lines.append("")

    lines.append("-- 被以下触发器引用：")
    if trigger_refs:
        for d in sorted(set(trigger_refs)):
            lines.extend(_comment_lines(d))
    else:
        lines.append("-- （无）")

    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Per-schema table/view/function rendering
# ---------------------------------------------------------------------------


class SyncResult(NamedTuple):
    """T29: bookkeeping returned by every per-schema sync path (_sync_object_dir
    and its three _render_schema_* wrappers, plus _collapse_schema_dir) so run()
    merges results by field name rather than by tuple position. Every field is a
    list of str paths; a path appears in at most one of written/deleted/unchanged.
    `legacy_md` (DD-6) lists pre-.sql `*.md` object files that were neither read
    nor deleted — reported only."""

    written: list[str]
    deleted: list[str]
    unchanged: list[str]
    legacy_md: list[str]


def _sync_object_dir(
    dbmeta_dir: Path,
    schema: str,
    kind: str,
    dir_name: str,
    current_names: set[str],
    render_blocks: Callable[[str], list[tuple[str, str]]],
) -> SyncResult:
    """Shared D-C directory-sync skeleton for one schema's tables/views/functions
    dir: reconcile removed objects (process_removed_object_file), prune an empty
    dir, ensure the dir exists when there's anything to write, then merge_blocks
    each current object via `render_blocks(name)` (one object == one file, except
    functions where it returns one block per overload). Callers own only "how to
    split/render a single object's block(s)".

    DD-6 (design.md/task4-brief): object files are now `.sql` (SQL_SYNTAX) — the
    current-object glob/read/write/delete set is exclusively `*.sql`. Any `*.md`
    still sitting in the same dir (leftover from the pre-.sql skill version) is
    NEVER read or deleted here; it is only collected into the returned `legacy_md`
    list for run()'s summary + stderr notice — human migrates it by hand."""
    written: list[str] = []
    deleted: list[str] = []
    unchanged: list[str] = []

    obj_dir = dbmeta_dir / schema / dir_name
    # [impl-review-fix] F-E: same symlink rejection as the top-level .dbmeta/ scan in
    # run(), one level down — obj_dir.exists() alone follows a symlink transparently,
    # so without this a symlinked tables/views/functions dir would get its target
    # written into by the merge loop below.
    if obj_dir.is_symlink():
        raise CollectFormatError(
            f".dbmeta/{schema}/{dir_name} 是符号链接",
            "render 的写入/收敛删除不跟随链接、也不能静默跳过",
            "人工核实后移除该链接或改为真实目录",
        )
    existing_names: set[str] = set()
    legacy_md: list[str] = []
    if obj_dir.exists():
        existing_names = {p.stem for p in obj_dir.glob("*.sql")}
        legacy_md = [str(p) for p in obj_dir.glob("*.md")]

    removed_names = existing_names - current_names
    for name in sorted(removed_names):
        path = obj_dir / f"{name}.sql"
        header = object_file_header(kind, schema, name, SQL_SYNTAX)
        if process_removed_object_file(path, header, SQL_SYNTAX):
            deleted.append(str(path))

    if obj_dir.exists() and not any(obj_dir.iterdir()):
        obj_dir.rmdir()

    if current_names:
        obj_dir.mkdir(parents=True, exist_ok=True)

    for name in sorted(current_names):
        path = obj_dir / f"{name}.sql"
        header = object_file_header(kind, schema, name, SQL_SYNTAX)
        # T35: an object that was removed (file kept as 孤立注记) and has now
        # reappeared — the banner's claim "已从数据库删除" is no longer true, so it
        # is dropped before merging the live block back in.
        w, u = _sync_whole_file(
            path,
            lambda existing, h=header, n=name: merge_blocks(
                existing.replace(SQL_ORPHAN_BANNER, "") if existing else existing,
                h,
                render_blocks(n),
                SQL_SYNTAX,
            ),
        )
        written.extend(w)
        unchanged.extend(u)

    return SyncResult(written, deleted, unchanged, legacy_md)


def _sync_whole_file(
    path: Path, compute: Callable[[str | None], str], *, mkdir_parent: bool = False
) -> tuple[list[str], list[str]]:
    """T40: shared "read old -> compute new -> write only if different" skeleton.
    `compute(existing_text_or_None)` returns the full new content. Returns
    (written, unchanged), each holding at most `[str(path)]`."""
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    new_content = compute(existing)
    if new_content == existing:
        return [], [str(path)]
    if mkdir_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")
    return [str(path)], []


def _render_schema_tables(
    dbmeta_dir: Path,
    schema: str,
    tables: list[dict],
    func_names_index: dict[str, dict[str, list[str]]],
) -> SyncResult:
    parents, children_by_parent = group_children(tables)
    orphan_notes = resolve_orphan_children(parents, children_by_parent)
    parents_by_name = {t["name"]: t for t in parents}
    current_names = set(parents_by_name)

    def render_blocks(name: str) -> list[tuple[str, str]]:
        t = parents_by_name[name]
        body = render_table_ddl(
            schema, t, children_by_parent.get(name, []), orphan_notes.get(name), func_names_index
        )
        return [(block_ident("table", name), body)]

    return _sync_object_dir(dbmeta_dir, schema, "table", "tables", current_names, render_blocks)


def _render_schema_views(
    dbmeta_dir: Path, schema: str, views: list[dict]
) -> SyncResult:
    views_by_name = {v["name"]: v for v in views}
    current_names = set(views_by_name)

    def render_blocks(name: str) -> list[tuple[str, str]]:
        return [(block_ident("view", name), render_view_ddl(schema, views_by_name[name]))]

    return _sync_object_dir(dbmeta_dir, schema, "view", "views", current_names, render_blocks)


def _render_schema_functions(
    dbmeta_dir: Path,
    schema: str,
    functions: list[dict],
    trigger_refs: dict[tuple[str, str, str], list[str]],
) -> SyncResult:
    by_name: dict[str, list[dict]] = {}
    for f in functions:
        by_name.setdefault(f["name"], []).append(f)
    current_names = set(by_name)

    def render_blocks(name: str) -> list[tuple[str, str]]:
        overloads = sorted(by_name[name], key=lambda f: f.get("identity_args") or "")
        blocks: list[tuple[str, str]] = []
        for f in overloads:
            identity_args = f.get("identity_args") or ""
            ident = f"fn:{name}({identity_args})"
            refs = trigger_refs.get((schema, name, identity_args), [])
            blocks.append((ident, render_function_ddl(schema, f, refs)))
        return blocks

    return _sync_object_dir(dbmeta_dir, schema, "fn", "functions", current_names, render_blocks)


# ---------------------------------------------------------------------------
# D-L: schema-level disappearance collapse
# ---------------------------------------------------------------------------


def _collapse_schema_dir(schema_dir: Path) -> SyncResult:
    """A schema present on disk (.dbmeta/<schema>/) but absent from this run's
    collect result. `_collect.json` (a pure generated file, D-E — no
    block/hand-written-annotation concept) is deleted outright.

    [impl-review-fix] F-C: `README.md` is NOT a pure generated file — dbmeta spec
    DBM-2 gives it the same managed-block contract (托管块内再生整体重写，块外
    逐字保留) as every other object file, and design.md D-L's original "delete it
    outright" wording undersold that. It now goes through the same
    process_removed_object_file D-C path as table/view/function files, using the
    exact header `_sync_schema_readme`'s write side uses (`schema_readme_header`)
    — a README with block-external hand-written annotation survives (with an
    orphan banner) instead of being silently unlinked; one with none is deleted as
    before. Every object file under tables/views/functions/ is processed per D-C;
    empty directories are cleaned up afterward (if README.md survived, the schema
    dir is non-empty and the trailing rmdir is skipped — no exception either way,
    since it's guarded by the `not any(schema_dir.iterdir())` check below)."""
    schema = schema_dir.name
    deleted: list[str] = []
    legacy_md: list[str] = []

    # [impl-review-fix] F-E: scan every subdirectory this function is about to
    # touch for symlinks BEFORE any deletion happens below (README/_collect.json
    # unlink and the tables/views/functions object-file deletion loop are not
    # atomic as a whole) — detecting a symlink only right before touching it would
    # still leave already-processed earlier entries deleted by the time the
    # symlink is found.
    for kind_dir_name in KIND_DIR_NAMES.values():
        kind_dir = schema_dir / kind_dir_name
        if kind_dir.is_symlink():
            raise CollectFormatError(
                f".dbmeta/{schema}/{kind_dir_name} 是符号链接",
                "render 的写入/收敛删除不跟随链接、也不能静默跳过",
                "人工核实后移除该链接或改为真实目录",
            )

    collect_json = schema_dir / "_collect.json"
    if collect_json.exists():
        collect_json.unlink()
        deleted.append(str(collect_json))

    readme = schema_dir / "README.md"
    if readme.exists():
        header = schema_readme_header(schema)
        if process_removed_object_file(readme, header, MD_SYNTAX):
            deleted.append(str(readme))

    for kind, kind_dir_name in KIND_DIR_NAMES.items():
        kind_dir = schema_dir / kind_dir_name
        if not kind_dir.exists():
            continue
        # DD-6: object files are `.sql` now — a stray `.md` here (pre-.sql skill
        # version) is left untouched, same "only report, never read/delete" rule
        # as _sync_object_dir's legacy_md. T28: it is also reported through the
        # same `legacy_md` channel, so a schema that vanished from the DB can't
        # silently hide its leftover .md files from run()'s summary.
        legacy_md.extend(str(p) for p in kind_dir.glob("*.md"))
        for path in sorted(kind_dir.glob("*.sql")):
            name = path.stem
            header = object_file_header(kind, schema, name, SQL_SYNTAX)
            if process_removed_object_file(path, header, SQL_SYNTAX):
                deleted.append(str(path))
        if kind_dir.exists() and not any(kind_dir.iterdir()):
            kind_dir.rmdir()

    if schema_dir.exists() and not any(schema_dir.iterdir()):
        schema_dir.rmdir()
        # [DD-13] the directory itself vanished with nothing else left to report
        # (e.g. a legacy empty `backfill/` placeholder that never held any
        # managed file) — report the directory path so callers/output summaries
        # don't silently drop a real disk change from `deleted[]`.
        deleted.append(str(schema_dir))

    return SyncResult([], deleted, [], legacy_md)


# ---------------------------------------------------------------------------
# D-H: schema README index (managed block `pg-dict:index`, one per schema dir)
# ---------------------------------------------------------------------------

SCHEMA_README_HEADER_TMPL = (
    "# `{schema}` schema 数据字典索引\n\n"
    "<!-- 本文件由 pg-dict skill 自动生成。托管块（`<!-- pg-dict:index:start/end"
    " -->`）内容会在再生时整体重写；块外文本由人工维护，再生时逐字保留。 -->\n\n"
)


def schema_readme_header(schema: str) -> str:
    return SCHEMA_README_HEADER_TMPL.format(schema=schema)


def render_schema_readme_body(
    schema: str,
    parents: list[dict],
    children_by_parent: dict[str, list[dict]],
    views: list[dict],
    functions: list[dict],
) -> str:
    lines: list[str] = ["### 表", ""]
    lines.append("| 名 | COMMENT | 行数估计 | 分区子表数 |")
    lines.append("|---|---|---|---|")
    for t in sorted(parents, key=lambda t: t["name"]):
        n_children = len(children_by_parent.get(t["name"], []))
        lines.append(
            "| {name} | {comment} | {reltuples} | {n_children} |".format(
                name=_escape_cell(t["name"]),
                comment=_escape_cell((t.get("comment") or "").strip()),
                reltuples=format_reltuples(t.get("reltuples")),
                n_children=n_children,
            )
        )
    lines.append("")

    lines.append("### 视图")
    lines.append("")
    if views:
        lines.append("| 名 | COMMENT |")
        lines.append("|---|---|")
        for v in sorted(views, key=lambda v: v["name"]):
            lines.append(
                "| {name} | {comment} |".format(
                    name=_escape_cell(v["name"]),
                    comment=_escape_cell((v.get("comment") or "").strip()),
                )
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("### 函数")
    lines.append("")
    if functions:
        for f in sorted(functions, key=lambda f: (f["name"], f.get("identity_args") or "")):
            identity_args = f.get("identity_args") or ""
            result_type = f.get("result_type") or ""
            lines.append(f"- `{f['name']}({identity_args})` → `{result_type}`")
    else:
        lines.append("（无）")
    lines.append("")

    return "\n".join(lines) + "\n"


def _sync_schema_readme(
    dbmeta_dir: Path,
    schema: str,
    parents: list[dict],
    children_by_parent: dict[str, list[dict]],
    views: list[dict],
    functions: list[dict],
) -> tuple[list[str], list[str]]:
    path = dbmeta_dir / schema / "README.md"
    header = schema_readme_header(schema)
    body = render_schema_readme_body(schema, parents, children_by_parent, views, functions)
    return _sync_whole_file(
        path,
        lambda existing: merge_blocks(existing, header, [("index", body)], MD_SYNTAX),
        mkdir_parent=True,
    )


# ---------------------------------------------------------------------------
# D-G/D-H: root README index (managed block `pg-dict:index`, fixed reading-order
# note before it)
# ---------------------------------------------------------------------------

ROOT_README_HEADER = (
    "# dbmeta 数据字典\n\n"
    "<!-- 本文件由 pg-dict skill 自动生成。托管块（`<!-- pg-dict:index:start/end"
    " -->`）内容会在再生时整体重写；块外文本由人工维护，再生时逐字保留。 -->\n\n"
    "阅读顺序：先读本 README.md，再读对应 `<schema>/README.md`，最后按需读具体"
    "对象文件；表/列之间的 join 依据看 `_relations.md`；查询侧通用过滤规则看"
    "`rules.md`。\n\n"
)


def root_readme_header() -> str:
    return ROOT_README_HEADER


def render_root_readme_body(
    schema_summaries: list[dict], out_of_scope_schemas: list[str] | None = None
) -> str:
    """`schema_summaries` entries: {schema, n_tables, n_views, n_functions,
    n_gaps}. `out_of_scope_schemas` — schema dirs on
    disk but excluded from this run because `requested_schemas` narrowed scope
    — get one row each, name suffixed "（未覆盖）", all four count columns "—"
    (their on-disk `_collect.json`, if any, is stale and MUST NOT be read to
    fill these in). `None` (the pre-change default) or an empty list add no
    rows — byte-for-byte identical to before this parameter existed."""
    lines = [
        "| schema | 表数 | 视图 | 函数 | 缺注释 |",
        "|---|---|---|---|---|",
    ]
    for s in sorted(schema_summaries, key=lambda s: s["schema"]):
        lines.append(
            "| {schema} | {n_tables} | {n_views} | {n_functions} | {n_gaps} |".format(
                schema=_escape_cell(s["schema"]),
                n_tables=s["n_tables"],
                n_views=s["n_views"],
                n_functions=s["n_functions"],
                n_gaps=s["n_gaps"],
            )
        )
    for schema in sorted(out_of_scope_schemas or []):
        lines.append(f"| {_escape_cell(schema)}（未覆盖） | — | — | — | — |")
    lines.append("")
    return "\n".join(lines) + "\n"


def _sync_root_readme(
    dbmeta_dir: Path,
    schema_summaries: list[dict],
    out_of_scope_schemas: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    path = dbmeta_dir / "README.md"
    header = root_readme_header()
    body = render_root_readme_body(schema_summaries, out_of_scope_schemas)
    return _sync_whole_file(
        path, lambda existing: merge_blocks(existing, header, [("index", body)], MD_SYNTAX)
    )


# ---------------------------------------------------------------------------
# D-F: derived-semantics parsing constants (逻辑关联/枚举, applied to COLUMN
# comments only — matches openspec/rules/database.md's "逻辑关联在 COMMENT ON
# COLUMN 中说明" convention and every existing usage in this repo, memo C4).
# Sensitive-column judgement reuses SENSITIVE_NAME_RE/SENSITIVE_SAFE_RE above.
# ---------------------------------------------------------------------------

# 逻辑关联 <schema>.<table>.<column> — three-segment, fully-qualified target.
# "每列可多条": findall (not just the first match) picks up every occurrence in
# one comment.
LOGICAL_RELATION_RE = re.compile(
    r"逻辑关联\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)"
)

# <integer>=<text> enum entries, e.g. "0=待处理". design.md D-F's own text is
# self-contradictory ("连续匹配 ≥2 项才视为枚举" vs "单个 0=未设置 之类也收，阈值
# 1") — task3-brief.md flags this and instructs taking the simplest reading:
# threshold 1 (a single "N=text" occurrence already counts as an enum entry).
ENUM_ENTRY_RE = re.compile(r"(-?\d+)\s*=\s*([^,;，；/\s)）]+)")


def _iter_columns(schema_obj: dict):
    """Yield (table_name, column_dict) for every TOP-LEVEL table's column in this
    schema — folded partition children are excluded (same `folded_names` logic as
    collect_gaps(), so a child inheriting its parent's COMMENT verbatim doesn't
    produce a duplicate relation/enum/sensitive entry alongside the parent)."""
    tables = schema_obj.get("tables", [])
    parents, children_by_parent = group_children(tables)
    resolve_orphan_children(parents, children_by_parent)
    for t in parents:
        for col in t.get("columns", []):
            yield t["name"], col


def collect_relations(schemas: list[dict]) -> dict[str, list]:
    """Scan every schema's top-level-table column COMMENTs for the three D-F
    conventions. Returns a dict of raw entries (not yet rendered) so
    render_relations_report() stays a pure formatting step:
      - 'logical': [(target_key, source_line), ...] — target_key is
        "<schema>.<table>" (grouping key), source_line is the full
        "src.schema.table.col → tgt.schema.table.col" display string.
      - 'enums': [(qualified_col, [(value, meaning), ...]), ...]
      - 'sensitive': [(qualified_col, declared_safe: bool), ...]
    """
    logical: list[tuple[str, str]] = []
    enums: list[tuple[str, list[tuple[str, str]]]] = []
    sensitive: list[tuple[str, bool]] = []

    for schema_obj in schemas:
        schema = schema_obj["schema"]
        for table_name, col in _iter_columns(schema_obj):
            comment = (col.get("comment") or "").strip()
            col_name = col["name"]
            qualified_col = f"{schema}.{table_name}.{col_name}"

            for tgt_schema, tgt_table, tgt_col in LOGICAL_RELATION_RE.findall(comment):
                target_key = f"{tgt_schema}.{tgt_table}"
                target_col = f"{tgt_schema}.{tgt_table}.{tgt_col}"
                logical.append((target_key, f"{qualified_col} → {target_col}"))

            enum_entries = ENUM_ENTRY_RE.findall(comment)
            if enum_entries:
                enums.append((qualified_col, enum_entries))

            if SENSITIVE_NAME_RE.search(col_name):
                declared_safe = bool(SENSITIVE_SAFE_RE.search(comment))
                sensitive.append((qualified_col, declared_safe))

    return {"logical": logical, "enums": enums, "sensitive": sensitive}


def render_mermaid_er_diagram(pairs: list[tuple[str, str]]) -> str:
    """DD-7/REQ-RD-1,2,3,4: given fully-qualified (source_column, target_column)
    pairs (each `schema.table.column`), render a Mermaid `erDiagram` fence with
    one line per distinct (source_table, target_table) pair. The referenced
    target table is the "one" side, the column-holding source table is the
    "many" side — `[spec-review-amendment]` DD-7 corrected direction:
    `<target_table> ||--o{ <source_table> : "<source_column>"`. Multiple
    columns pointing at the same table pair collapse to a single line labelled
    with the lexicographically smallest source column name (REQ-RD-3). Table
    names use `<schema>_<table>` (`.` -> `_`, REQ-RD-2). Lines sorted by
    (source_table, target_table) (REQ-RD-3). Empty input -> "" — no fence at
    all (REQ-RD-4)."""
    if not pairs:
        return ""

    labels_by_pair: dict[tuple[str, str], list[str]] = {}
    for source, target in pairs:
        source_table, _, source_col = source.rpartition(".")
        target_table = target.rpartition(".")[0]
        labels_by_pair.setdefault((source_table, target_table), []).append(source_col)

    # B1: node ids come from schema.table with `.`->`_`, so `a_b.c` and `a.b_c`
    # both fold to `a_b_c` — two distinct tables silently merged into one diagram
    # entity (dedup above keys on the original dot string, so only the render
    # layer collides). Detect any node id claimed by >1 distinct table and fail
    # loud rather than emit a silently-wrong ER diagram.
    node_id_owner: dict[str, str] = {}
    lines = ["erDiagram"]
    for source_table, target_table in sorted(labels_by_pair):
        label = min(labels_by_pair[(source_table, target_table)])
        source_id = source_table.replace(".", "_")
        target_id = target_table.replace(".", "_")
        for node_id, table in ((source_id, source_table), (target_id, target_table)):
            prior = node_id_owner.setdefault(node_id, table)
            if prior != table:
                raise CollectFormatError(
                    f"Mermaid 节点 id `{node_id}` 被两张不同的表 `{prior}` 与 `{table}` 争用",
                    "schema.table 生成节点 id 时把 `.` 替换成 `_`，"
                    "使 `a_b.c` 与 `a.b_c` 这类不同表折叠成同一 id，会在 ER 图中被静默合并",
                    "重命名其中一张表以消除下划线歧义，或在 dbmeta 能力升版时引入可逆编码",
                )
        lines.append(f'    {target_id} ||--o{{ {source_id} : "{label}"')
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def render_relations_report(
    relations: dict[str, list],
    requested_schemas: list[str] | None = None,
    confirmed: list[dict] | None = None,
) -> str:
    """Render .dbmeta/_relations.md — no managed blocks, full-file deterministic
    rewrite (mirrors render_gaps_report()'s shape/rules): three sections, sorted
    deterministically so re-runs with unchanged input are byte-identical.
    `requested_schemas` (None = full scan) adds a one-line coverage-scope
    declaration when non-null; None leaves the file byte-for-byte identical to
    before this parameter existed. `confirmed` (DD-7/REQ-RD-1, `[spec-review-
    amendment]`) is `load_confirmed()`'s output — entries with `ignore: true`
    excluded — merged with the COMMENT-annotated `logical` pairs (deduped by
    (source, target)) to become the data source for BOTH the Mermaid erDiagram
    fence AND the "逻辑关联" text list below it (brief Task 4: 文字列表数据源
    同步扩展为 merge 后合集)."""
    confirmed = confirmed or []

    merged_pairs: set[tuple[str, str]] = set()
    for _target_key, source_line in relations["logical"]:
        source, _, target = source_line.partition(" → ")
        merged_pairs.add((source, target))
    for entry in confirmed:
        if entry.get("ignore"):
            continue
        merged_pairs.add((entry["source"], entry["target"]))

    lines = [
        "# 数据字典派生语义",
        "",
        "<!-- 本文件由 pg-dict skill 自动生成，勿手改；内容仅来自 COMMENT 中符合"
        "约定格式的片段机械派生，不代表模型推测。 -->",
        "",
    ]
    lines.extend(_scope_declaration_lines(requested_schemas))
    lines.extend([
        "## 逻辑关联",
        "",
    ])
    if merged_pairs:
        by_target: dict[str, set[str]] = {}
        for source, target in merged_pairs:
            target_key = target.rpartition(".")[0]
            by_target.setdefault(target_key, set()).add(f"{source} → {target}")

        mermaid = render_mermaid_er_diagram(sorted(merged_pairs))
        lines.append(mermaid)
        lines.append("")
        for target_key in sorted(by_target):
            lines.append(f"### {target_key}")
            lines.append("")
            for source_line in sorted(by_target[target_key]):
                lines.append(f"- `{source_line}`")
            lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    lines.append("## 枚举取值")
    lines.append("")
    if relations["enums"]:
        for qualified_col, entries in sorted(set(
            (col, tuple(sorted(set(e)))) for col, e in relations["enums"]
        )):
            lines.append(f"### `{qualified_col}`")
            lines.append("")
            lines.append("| 值 | 含义 |")
            lines.append("|---|---|")
            for value, meaning in sorted(entries, key=lambda e: int(e[0])):
                lines.append(f"| {_escape_cell(value)} | {_escape_cell(meaning)} |")
            lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    lines.append("## 敏感列")
    lines.append("")
    if relations["sensitive"]:
        for qualified_col, declared_safe in sorted(set(relations["sensitive"])):
            suffix = "（已声明脱敏）" if declared_safe else ""
            lines.append(f"- `{qualified_col}`{suffix}")
        lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Relation inference (design.md DD-1..DD-5/DD-11/DD-12, specs/relation-inference
# REQ-RI-1..5/7 — Task 2 scope). DD-1: lives in render.py, same layer as
# collect_relations()/render_relations_report() above, no new module.
# ---------------------------------------------------------------------------


def _top_level_tables(schemas: list[dict]) -> list[tuple[str, dict]]:
    """Yield (schema_name, table_dict) for every TOP-LEVEL table across ALL
    schemas — partition children folded/promoted exactly like _iter_columns()
    does per-schema (group_children + resolve_orphan_children), so the column-
    name/COMMENT inference signals never see a partition child as its own
    candidate source or target."""
    result: list[tuple[str, dict]] = []
    for schema_obj in schemas:
        schema = schema_obj["schema"]
        tables = schema_obj.get("tables", [])
        parents, children_by_parent = group_children(tables)
        resolve_orphan_children(parents, children_by_parent)
        for t in parents:
            result.append((schema, t))
    return result


def infer_column_name_candidates(schemas: list[dict]) -> list[dict]:
    """DD-2/REQ-RI-1: for every top-level table column whose name ends in
    `_id`, strip the suffix to get `stem`, then look up a top-level table named
    exactly `stem` (priority 1) or, only if none exists, `stem + "s"` (priority
    2) across ALL schemas — a same-name table in multiple schemas each yields
    an independent candidate. Target column is the matched table's `id` column
    if it has one, else a same-named column as the source, else no candidate.
    Filters the degenerate self-match case (source == target verbatim) that
    arises when a table's own pluralized name equals its stem and it lacks an
    `id` column (REQ-RI-1 "自引用不推导")."""
    top = _top_level_tables(schemas)
    tables_by_name: dict[str, list[tuple[str, dict]]] = {}
    for schema, t in top:
        tables_by_name.setdefault(t["name"], []).append((schema, t))

    candidates: list[dict] = []
    for schema, t in top:
        table_name = t["name"]
        for col in t.get("columns", []):
            col_name = col["name"]
            if not col_name.endswith("_id"):
                continue
            stem = col_name[:-3]
            matches = tables_by_name.get(stem) or tables_by_name.get(stem + "s") or []
            for tgt_schema, tgt_table in matches:
                tgt_cols = {c["name"] for c in tgt_table.get("columns", [])}
                if "id" in tgt_cols:
                    target_col = "id"
                elif col_name in tgt_cols:
                    target_col = col_name
                else:
                    continue
                source = f"{schema}.{table_name}.{col_name}"
                target = f"{tgt_schema}.{tgt_table['name']}.{target_col}"
                if source == target:
                    continue
                candidates.append({"source": source, "target": target, "signal": "column_name"})
    return candidates


# DD-3 rule 1: fully-qualified schema.table reference inside a COMMENT.
_COMMENT_QUALIFIED_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")
# DD-3 rule 2: comment tokenization for bare table-name matching.
_COMMENT_TOKEN_RE = re.compile(r"[a-z_][a-z0-9_]*")


def infer_comment_ref_candidates(schemas: list[dict]) -> list[dict]:
    """DD-3/REQ-RI-2: scan every top-level table column's COMMENT (with any
    already-annotated `逻辑关联 ...` spans stripped first) for table-name
    references. A fully-qualified `<schema>.<table>` reference is validated
    against the actual collect result; a bare table-name token only counts
    when that table name is unique across ALL schemas (ambiguous otherwise —
    REQ-RI-2 "裸表名多 schema 歧义不命中"). Target column is fixed at `id`
    (DD-3: COMMENT references conventionally point at the primary key, never
    validated against the target table's actual columns)."""
    top = _top_level_tables(schemas)
    tables_by_schema: dict[str, set[str]] = {}
    owners_by_table_name: dict[str, set[str]] = {}
    for schema, t in top:
        tables_by_schema.setdefault(schema, set()).add(t["name"])
        owners_by_table_name.setdefault(t["name"], set()).add(schema)

    candidates: list[dict] = []
    for schema_obj in schemas:
        schema = schema_obj["schema"]
        for table_name, col in _iter_columns(schema_obj):
            comment = (col.get("comment") or "").strip()
            if not comment:
                continue
            cleaned = LOGICAL_RELATION_RE.sub("", comment)
            source = f"{schema}.{table_name}.{col['name']}"
            seen_targets: set[str] = set()

            for ref_schema, ref_table in _COMMENT_QUALIFIED_RE.findall(cleaned):
                if ref_table in tables_by_schema.get(ref_schema, ()):
                    target = f"{ref_schema}.{ref_table}.id"
                    if source == target:
                        continue
                    if target not in seen_targets:
                        seen_targets.add(target)
                        candidates.append(
                            {"source": source, "target": target, "signal": "comment_ref"}
                        )

            for token in _COMMENT_TOKEN_RE.findall(cleaned):
                owners = owners_by_table_name.get(token)
                if owners and len(owners) == 1:
                    (owner_schema,) = owners
                    target = f"{owner_schema}.{token}.id"
                    if source == target:
                        continue
                    if target not in seen_targets:
                        seen_targets.add(target)
                        candidates.append(
                            {"source": source, "target": target, "signal": "comment_ref"}
                        )
    return candidates


def _annotated_relation_pairs(schemas: list[dict]) -> set[tuple[str, str]]:
    """(source, target) fully-qualified column pairs already annotated via the
    `逻辑关联` COMMENT convention (LOGICAL_RELATION_RE) — REQ-RI-3 excludes
    these from candidates. Mirrors collect_relations()'s COMMENT scan but
    keeps the raw column-pair shape candidates need (collect_relations()
    groups by target table for _relations.md display, a different shape)."""
    pairs: set[tuple[str, str]] = set()
    for schema_obj in schemas:
        schema = schema_obj["schema"]
        for table_name, col in _iter_columns(schema_obj):
            comment = (col.get("comment") or "").strip()
            source = f"{schema}.{table_name}.{col['name']}"
            for tgt_schema, tgt_table, tgt_col in LOGICAL_RELATION_RE.findall(comment):
                pairs.add((source, f"{tgt_schema}.{tgt_table}.{tgt_col}"))
    return pairs


def build_candidates(schemas: list[dict], confirmed: list[dict]) -> list[dict]:
    """DD-4/REQ-RI-3: merge the two inference signals, deduping by (source,
    target) with column_name taking priority over comment_ref, then exclude
    pairs already in `confirmed` (load_confirmed() output — ignore:true
    entries included, since ignored candidates MUST NOT resurface) and pairs
    already annotated via COMMENT `逻辑关联`. Sorted by (source, target) —
    `source` is DD-4's contractual sort key; `target` is an additional,
    spec-compatible tie-breaker that makes multi-target-per-source output
    deterministic regardless of collect JSON schema ordering (REQ-RI-7)."""
    merged: dict[tuple[str, str], dict] = {}
    for c in infer_column_name_candidates(schemas):
        merged.setdefault((c["source"], c["target"]), c)
    for c in infer_comment_ref_candidates(schemas):
        merged.setdefault((c["source"], c["target"]), c)

    excluded = {(e["source"], e["target"]) for e in confirmed}
    excluded |= _annotated_relation_pairs(schemas)

    result = [v for k, v in merged.items() if k not in excluded]
    result.sort(key=lambda c: (c["source"], c["target"]))
    return result


def render_candidates_yaml(candidates: list[dict]) -> str:
    """REQ-RI-4/DD-4/DD-11: full-file deterministic rewrite of
    `_relations.candidates.yaml` — top-of-file "勿手改" comment, then either
    `[]` (no candidates) or one `- source:/  target:/  signal:` block per
    candidate, in the order already sorted by build_candidates()."""
    lines = [
        "# .dbmeta/_relations.candidates.yaml",
        "# 本文件由 pg-dict 每次再生时全量重写，勿手改。",
        "# 确认的条目请移入 _relations.confirmed.yaml。",
    ]
    if not candidates:
        lines.append("[]")
    else:
        for c in candidates:
            lines.append(f"- source: {c['source']}")
            lines.append(f"  target: {c['target']}")
            lines.append(f"  signal: {c['signal']}")
    return "\n".join(lines) + "\n"


def _sync_candidates(dbmeta_dir: Path, candidates: list[dict]) -> tuple[list[str], list[str]]:
    """Write/refresh .dbmeta/_relations.candidates.yaml. Returns (written,
    unchanged), each holding at most the one path."""
    path = dbmeta_dir / "_relations.candidates.yaml"
    content = render_candidates_yaml(candidates)
    return _sync_whole_file(path, lambda _existing: content, mkdir_parent=True)


def _column_comment_index(schemas: list[dict]) -> dict[str, str]:
    """Maps `schema.table.column` (top-level tables only — same partition-
    folded set every other inference/candidate helper above reads from, via
    `_top_level_tables()`) to that column's current COMMENT (empty string if
    none). Used by render_pending_sql() (DD-6/REQ-RI-6) both to detect a
    confirmed entry's source column going stale (absent from this collect
    result) and to read the existing COMMENT text to append onto."""
    index: dict[str, str] = {}
    for schema, t in _top_level_tables(schemas):
        table_name = t["name"]
        for col in t.get("columns", []):
            index[f"{schema}.{table_name}.{col['name']}"] = (col.get("comment") or "").strip()
    return index


def render_pending_sql(schemas: list[dict], confirmed: list[dict]) -> str:
    """DD-6/REQ-RI-6: full-file deterministic rewrite of
    `.dbmeta/_relations.pending.sql` — human-executable `COMMENT ON COLUMN`
    statements appending confirmed `逻辑关联 <target>` annotations onto each
    source column's existing COMMENT (read from the collect result, not from
    _relations.confirmed.yaml). `ignore: true` entries are excluded first.
    Grouped by source (`[spec-review-amendment]` — one column, one statement,
    all not-yet-annotated targets appended in target-lexicographic order in a
    single call; generating one statement per target would have each later
    COMMENT overwrite the earlier one, silently losing confirmed relations).
    A source column absent from this collect result (renamed/dropped) is
    skipped with a stderr problem/cause/fix warning — regen continues, never
    raises (mirrors load_confirmed()'s non-fail-loud contract for this same
    human-maintained file). A source whose confirmed targets are ALL already
    present in its COMMENT is omitted entirely (no-op statement)."""
    column_comments = _column_comment_index(schemas)

    targets_by_source: dict[str, list[str]] = {}
    for entry in confirmed:
        if entry.get("ignore"):
            continue
        targets_by_source.setdefault(entry["source"], []).append(entry["target"])

    statements: list[str] = []
    for source in sorted(targets_by_source):
        if source not in column_comments:
            _warn(
                f"_relations.confirmed.yaml 中 source={source!r} 在本次 collect 结果中不存在",
                "该列可能已被删除或改名，或 schema/表名拼写有误",
                "从 _relations.confirmed.yaml 中移除该条目，或核实并修正列名后重跑",
            )
            continue
        existing_comment = column_comments[source]
        already_targets = {".".join(m) for m in LOGICAL_RELATION_RE.findall(existing_comment)}
        new_targets = sorted(set(targets_by_source[source]) - already_targets)
        if not new_targets:
            continue
        appended = " ".join(f"逻辑关联 {t}" for t in new_targets)
        full_comment = f"{existing_comment} {appended}" if existing_comment else appended
        schema, table, column = source.split(".")
        qualified = (
            f"{quote_ident(schema)}.{quote_ident(table)}.{quote_ident(column)}"
        )
        statements.append(
            f"COMMENT ON COLUMN {qualified} IS {sql_literal(full_comment)};"
        )

    lines = [
        "-- .dbmeta/_relations.pending.sql",
        "-- 本文件由 pg-dict 每次再生时全量重写。",
        "-- 执行本文件会把 _relations.confirmed.yaml 中的关系写回 DB COMMENT。",
        "-- 可选操作——执行后下次再生会从 COMMENT 采集到这些关系。",
    ]
    lines.extend(statements)
    return "\n".join(lines) + "\n"


def _sync_pending_sql(
    dbmeta_dir: Path, schemas: list[dict], confirmed: list[dict]
) -> tuple[list[str], list[str]]:
    """Write/refresh .dbmeta/_relations.pending.sql. Returns (written,
    unchanged), each holding at most the one path."""
    path = dbmeta_dir / "_relations.pending.sql"
    content = render_pending_sql(schemas, confirmed)
    return _sync_whole_file(path, lambda _existing: content, mkdir_parent=True)


# ---------------------------------------------------------------------------
# DD-11: minimal hand-written YAML-subset reader for _relations.confirmed.yaml.
# Supports ONLY: leading `#` comment lines (top of file, before the array
# body), blank lines, and either a literal `[]` empty array or a sequence of
# `- key: value` / `  key: value` array-item entries whose values match
# IDENTIFIER_RE's charset (`^[A-Za-z0-9_.]+$`). Anything else (quoted strings,
# multi-line scalars, nested mappings/sequences, flow style `[]`/`{}` mixed
# with entries, anchors, inline comments) is a format error -> None, so the
# caller (load_confirmed) can apply REQ-RI-5's "file-level error -> treat as
# empty + warn" contract without depending on a real YAML library (D6: no
# pyyaml dependency anywhere in this repo).
# ---------------------------------------------------------------------------

_YAML_ITEM_RE = re.compile(r"^- ([A-Za-z_][A-Za-z0-9_]*): ([A-Za-z0-9_.]+)$")
_YAML_CONT_RE = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_]*): ([A-Za-z0-9_.]+)$")


def _parse_relations_yaml_subset(text: str) -> list[dict] | None:
    entries: list[dict] = []
    current: dict | None = None
    entry_seen = False
    empty_literal_seen = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("#"):
            if entry_seen or empty_literal_seen:
                return None  # comments only allowed before the array body
            continue
        if line.strip() == "[]":
            if entry_seen or empty_literal_seen:
                return None
            empty_literal_seen = True
            continue
        m_item = _YAML_ITEM_RE.match(line)
        if m_item:
            if empty_literal_seen:
                return None
            if current is not None:
                entries.append(current)
            current = {m_item.group(1): m_item.group(2)}
            entry_seen = True
            continue
        m_cont = _YAML_CONT_RE.match(line)
        if m_cont and current is not None:
            key = m_cont.group(1)
            if key in current:
                return None  # T6: duplicate key within one entry -> file-level fail-soft
            current[key] = m_cont.group(2)
            continue
        return None  # unsupported shape: quotes/multi-line/nesting/flow/anchors/...

    if current is not None:
        entries.append(current)
    return entries


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _is_three_segment_identifier(value: str) -> bool:
    """source/target MUST be a fully-qualified `schema.table.column` path —
    exactly 3 non-empty dot-separated segments, each matching the D-M
    identifier charset (rejects e.g. `a..b`, which IDENTIFIER_RE alone would
    let through since it treats `.` as an ordinary allowed character)."""
    parts = value.split(".")
    return len(parts) == 3 and all(_SEGMENT_RE.match(p) for p in parts)


def load_confirmed(dbmeta_dir: Path) -> list[dict]:
    """DD-12/REQ-RI-5: read+validate `.dbmeta/_relations.confirmed.yaml`
    before any `.dbmeta/` write. Missing file -> []. File-level format error
    -> [] + stderr problem/cause/fix warning (regen continues). Per-entry
    validation failure (extra keys / missing source-target / not a 3-segment
    identifier / duplicate (source,target) pair) -> skip that entry + warn,
    other entries still returned. Never raises (not fail-loud — a human-
    maintained file's local mistakes MUST NOT block the rest of regen).
    Returned entries carry `source`/`target` (str) and, only when the literal
    value was `true`, `ignore: True` — never a literal-False key, matching
    DD-5's "ignore 缺省或任何非 true 值均视为未忽略"."""
    path = dbmeta_dir / "_relations.confirmed.yaml"
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    parsed = _parse_relations_yaml_subset(text)
    if parsed is None:
        _warn(
            "_relations.confirmed.yaml 不是受支持的 YAML 子集",
            "DD-11 只支持顶部 # 注释/空行/`- key: value` 数组条目，"
            "不支持引号字符串、多行、嵌套、流式 []/{}、锚点",
            "对照 _relations.candidates.yaml 的输出样式手工修正该文件后重跑",
        )
        return []

    valid: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, entry in enumerate(parsed, start=1):
        extra_keys = sorted(set(entry) - {"source", "target", "ignore"})
        if extra_keys:
            _warn(
                f"_relations.confirmed.yaml 第 {i} 条含不支持的键 {extra_keys}",
                "该文件每条只支持 source/target/可选 ignore 三个键",
                "删除多余键，或删除该条目",
            )
            continue
        source = entry.get("source")
        target = entry.get("target")
        if not source or not target:
            _warn(
                f"_relations.confirmed.yaml 第 {i} 条缺少 source 或 target",
                "每条须同时含 source 与 target",
                "补全该条目，或删除该条目",
            )
            continue
        if not _is_three_segment_identifier(source):
            _warn(
                f"_relations.confirmed.yaml 第 {i} 条 source={source!r} 不是合法的三段限定名",
                "source 须形如 schema.table.column，且只含字母数字下划线",
                "改成 schema.table.column 形式，或删除该条目",
            )
            continue
        if not _is_three_segment_identifier(target):
            _warn(
                f"_relations.confirmed.yaml 第 {i} 条 target={target!r} 不是合法的三段限定名",
                "target 须形如 schema.table.column，且只含字母数字下划线",
                "改成 schema.table.column 形式，或删除该条目",
            )
            continue
        pair = (source, target)
        if pair in seen_pairs:
            _warn(
                f"_relations.confirmed.yaml 中 (source, target) = {pair} 重复出现",
                "同一对关系被确认了不止一次",
                "删除重复的那一条",
            )
            continue
        seen_pairs.add(pair)
        result_entry = {"source": source, "target": target}
        if entry.get("ignore") == "true":
            result_entry["ignore"] = True
        valid.append(result_entry)
    return valid


def _sync_relations_report(
    dbmeta_dir: Path,
    schemas: list[dict],
    requested_schemas: list[str] | None = None,
    confirmed: list[dict] | None = None,
) -> tuple[list[str], list[str]]:
    """Write/refresh .dbmeta/_relations.md. Returns (written, unchanged), each
    holding at most the one path."""
    path = dbmeta_dir / "_relations.md"
    content = render_relations_report(collect_relations(schemas), requested_schemas, confirmed)
    return _sync_whole_file(path, lambda _existing: content)


# ---------------------------------------------------------------------------
# D-E: per-schema _collect.json slice (machine-fact slice, whole-file rewrite,
# no managed block)
# ---------------------------------------------------------------------------

# D-E/Q2 (拍板 A): statistics fields that vary run-to-run purely from autovacuum/
# ANALYZE noise (not from an actual schema change) and therefore MUST NOT appear
# in the slice at all — only 'reltuples' exists today, but the set is written as
# a set so a future addition doesn't require touching call sites.
STRIPPED_TABLE_STAT_FIELDS = {"reltuples"}


def strip_stat_fields(schema_obj: dict) -> dict:
    """Return a NEW schema dict (same key order, via dict-comprehension over
    `.items()`) whose `tables[]` entries have `STRIPPED_TABLE_STAT_FIELDS` keys
    removed — the original `schema_obj` (and its table dicts) is left untouched,
    since DDL rendering (render_table_ddl) still needs the real reltuples value
    for its row-count-estimate comment. Only `tables[]` carries statistics
    fields today; views/functions pass through unmodified."""
    sliced_tables = [
        {k: v for k, v in t.items() if k not in STRIPPED_TABLE_STAT_FIELDS}
        for t in schema_obj.get("tables", [])
    ]
    return {
        k: (sliced_tables if k == "tables" else v) for k, v in schema_obj.items()
    }


def render_collect_slice(schema_obj: dict) -> str:
    """D-E: `{"collect_version": 1, "schema": <stripped schema obj>}`, indent 2,
    ensure_ascii=False, trailing newline. Key order: the two top-level keys are
    fixed by dict-literal order below; `schema`'s own key order is whatever
    `strip_stat_fields` preserved from the parsed collect document (== db-collect's
    jsonb canonical key order) — determinism comes from never re-sorting."""
    wrapped = {"collect_version": 1, "schema": strip_stat_fields(schema_obj)}
    return json.dumps(wrapped, indent=2, ensure_ascii=False) + "\n"


def _sync_collect_slice(
    dbmeta_dir: Path, schema: str, schema_obj: dict
) -> tuple[list[str], list[str]]:
    """Write/refresh `.dbmeta/<schema>/_collect.json`. No managed block (whole-file
    deterministic rewrite, D-E); deletion on schema disappearance is handled
    separately by `_collapse_schema_dir` (D-L). Returns (written, unchanged) each
    holding at most the one path, mirroring the written/unchanged bookkeeping used
    elsewhere."""
    path = dbmeta_dir / schema / "_collect.json"
    content = render_collect_slice(schema_obj)
    return _sync_whole_file(path, lambda _existing: content, mkdir_parent=True)


# ---------------------------------------------------------------------------
# Gaps report (kept verbatim per design.md "保留 collect_gaps/render_gaps_report")
# ---------------------------------------------------------------------------


def collect_gaps(schema: str, schema_obj: dict, gaps: dict[str, list[str]]) -> None:
    """Accumulate gap entries for one schema's tables/columns/functions into
    `gaps` (mutated in place). Genuinely-folded partition child tables are
    excluded — a child promoted to standalone rendering by
    resolve_orphan_children() (parent not in this schema, or an intermediate
    partition level) DOES participate, same as any other table, matching the
    fact it now gets its own rendered file. Views are not checked (not in
    scope). Sensitive-column judgement per SENSITIVE_NAME_RE/SENSITIVE_SAFE_RE."""
    tables = schema_obj.get("tables", [])
    parents, children_by_parent = group_children(tables)
    resolve_orphan_children(parents, children_by_parent)
    # After resolve_orphan_children() pops orphan keys out, children_by_parent only
    # holds children genuinely folded into a same-schema top-level parent's block.
    folded_names = {c["name"] for cs in children_by_parent.values() for c in cs}

    for t in tables:
        if t["name"] in folded_names:
            continue
        qualified_table = f"{schema}.{t['name']}"
        comment = (t.get("comment") or "").strip()
        if not comment:
            gaps["tables"].append(qualified_table)
        for col in t.get("columns", []):
            col_name = col["name"]
            col_comment = (col.get("comment") or "").strip()
            qualified_col = f"{qualified_table}.{col_name}"
            if not col_comment:
                gaps["columns"].append(qualified_col)
            if SENSITIVE_NAME_RE.search(col_name) and not SENSITIVE_SAFE_RE.search(col_comment):
                gaps["sensitive"].append(qualified_col)
    for f in schema_obj.get("functions", []):
        comment = (f.get("comment") or "").strip()
        if not comment:
            # Key by name+identity_args, not just name — two overloads sharing a
            # name but differing in comment status would otherwise collide in the
            # set() dedup inside render_gaps_report, silently dropping one entry
            # / mismatching the reported count.
            identity_args = f.get("identity_args") or ""
            gaps["functions"].append(f"{schema}.{f['name']}({identity_args})")


def _scope_declaration_lines(requested_schemas: list[str] | None) -> list[str]:
    """Shared by render_gaps_report/render_relations_report:
    a coverage-scope declaration line, emitted only when `requested_schemas` is
    non-null — `None` (the pre-change default, still the overwhelmingly common
    case) returns `[]`, so a caller that always appends this list's output keeps
    byte-for-byte no-op behavior on the null path (D8 gate)."""
    if requested_schemas is None:
        return []
    scope = ", ".join(f"`{s}`" for s in sorted(requested_schemas)) or "（无）"
    return [f"> 本次采集范围限定为：{scope}；范围外的 schema 不在本文件统计之列。", ""]


def render_gaps_report(gaps: dict[str, list[str]], requested_schemas: list[str] | None = None) -> str:
    """Render .dbmeta/_gaps.md — no managed blocks, full-file deterministic
    rewrite, MUST NOT contain any run-varying field. `requested_schemas` (None =
    full scan) adds a one-line coverage-scope declaration when non-null; None
    leaves the file byte-for-byte identical to before this parameter existed."""

    def section(title: str, entries: list[str]) -> list[str]:
        out = [f"## {title}", ""]
        items = sorted(set(entries))
        if items:
            out.extend(f"- `{e}`" for e in items)
        else:
            out.append("（无）")
        out.append("")
        return out

    lines = [
        "# 数据字典缺口清单",
        "",
        "<!-- 本文件由 pg-dict skill 自动生成，勿手改；本文件是待补注释的清单，"
        "列入敏感 warning 不代表该字段实际无保护。 -->",
        "",
    ]
    lines.extend(_scope_declaration_lines(requested_schemas))
    lines.extend(section("缺表注释", gaps["tables"]))
    lines.extend(section("缺列注释", gaps["columns"]))
    lines.extend(section("缺函数注释", gaps["functions"]))
    lines.extend(section("敏感列名 warning", gaps["sensitive"]))
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Top-level v1 document parsing/validation (kept verbatim)
# ---------------------------------------------------------------------------


def parse_collect_document(raw: str) -> dict:
    """Parse+validate the top-level v1 collect document. Raises
    CollectFormatError (never a bare exception) on any structural problem —
    callers must not write any .dbmeta/ file when this is raised."""
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectFormatError(
            "无法解析 stdin 上的元数据 JSON",
            str(exc),
            "确认输入是 shared/db-collect.sh 的 v1 输出，没有混入其它输出（如 NOTICE、额外 SELECT）",
        ) from exc

    is_v1 = isinstance(metadata, dict) and metadata.get("collect_version") == 1
    if not is_v1:
        kind = type(metadata).__name__
        version = metadata.get("collect_version") if isinstance(metadata, dict) else None
        raise CollectFormatError(
            "元数据 JSON 顶层不是 collect_version==1 的对象",
            f"收到 {kind}/collect_version={version}（期望 shared/db-collect.sh 的 v1 输出）",
            "直接跑 shared/db-collect.sh --out /tmp/c.json 核对顶层字段；版本不符时升级 pg-dict skill",
        )

    if not isinstance(metadata.get("schemas"), list):
        raise CollectFormatError(
            "元数据 JSON 顶层不是 collect_version==1 的对象",
            f"收到 dict/collect_version=1，但 'schemas' 字段不是数组（{type(metadata.get('schemas')).__name__}）",
            "直接跑 shared/db-collect.sh --out /tmp/c.json 核对顶层字段；版本不符时升级 pg-dict skill",
        )

    return metadata


def _validate_schemas(schemas: list) -> None:
    """Validate every schema/table entry BEFORE run() writes anything, so a
    malformed schema[N] (N>0) can never let schemas[0..N-1] already be written to
    disk first ("no file written on invalid input")."""
    for idx, schema_obj in enumerate(schemas):
        if not isinstance(schema_obj, dict):
            raise CollectFormatError(
                "元数据 JSON 的 schemas[] 元素不是合法的 schema 对象",
                f"schemas[{idx}] 不是对象（{type(schema_obj).__name__}）",
                "直接跑 shared/db-collect.sh --out /tmp/c.json 核对 schemas[] 结构；版本不符时升级 pg-dict skill",
            )
        schema_name = schema_obj.get("schema")
        if not isinstance(schema_name, str) or not schema_name:
            raise CollectFormatError(
                "元数据 JSON 的 schema 对象缺少合法的 schema 字段",
                f"schemas[{idx}] 的 'schema' 字段不是非空字符串（{type(schema_name).__name__}）",
                "直接跑 shared/db-collect.sh --out /tmp/c.json 核对 schemas[] 结构；版本不符时升级 pg-dict skill",
            )
        tables = schema_obj.get("tables", [])
        if not isinstance(tables, list):
            raise CollectFormatError(
                "元数据 JSON 的 schema 对象 tables 不是数组",
                f"schemas[{idx}]（schema={schema_name}）的 'tables' 字段不是数组（{type(tables).__name__}）",
                "直接跑 shared/db-collect.sh --out /tmp/c.json 核对 tables[] 结构；版本不符时升级 pg-dict skill",
            )
        for tidx, t in enumerate(tables):
            if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t.get("name"):
                raise CollectFormatError(
                    "元数据 JSON 的表对象缺少合法的 name 字段",
                    f"schemas[{idx}].tables[{tidx}]（schema={schema_name}）不是含非空 'name' 字符串的对象",
                    "直接跑 shared/db-collect.sh --out /tmp/c.json 核对 tables[] 结构；版本不符时升级 pg-dict skill",
                )
            columns = t.get("columns", [])
            if not isinstance(columns, list):
                raise CollectFormatError(
                    "元数据 JSON 的表对象 columns 不是数组",
                    f"schemas[{idx}].tables[{tidx}].name={t['name']}（schema={schema_name}）"
                    f"的 'columns' 字段不是数组（{type(columns).__name__}）",
                    "直接跑 shared/db-collect.sh --out /tmp/c.json 核对 columns[] 结构；版本不符时升级 pg-dict skill",
                )
            # [impl-review-fix] A1 (code-review Important, antagonist mirror A):
            # render_table_ddl hard-subscripts col["name"]/col["type"] — a column
            # dict missing either would otherwise reach that subscript
            # unvalidated, raising a bare KeyError mid-run AFTER earlier
            # schemas/tables already wrote to disk. Checked here, in the same
            # full pre-flight batch as every other structural check in this
            # function, before run() performs any filesystem write/delete.
            # (B5: constraints/indexes/triggers[].name and views[].columns[].name
            # are hard-subscripted too — see _validate_named_entries below.)
            for cidx, col in enumerate(columns):
                if not isinstance(col, dict):
                    raise CollectFormatError(
                        "元数据 JSON 的列对象不是合法结构",
                        f"schemas[{idx}].tables[{tidx}].columns[{cidx}]"
                        f"（schema={schema_name}, table={t['name']}）不是对象（{type(col).__name__}）",
                        "重跑 shared/db-collect.sh 重新采集",
                    )
                col_name = col.get("name")
                col_label = col_name if isinstance(col_name, str) and col_name else f"columns[{cidx}]"
                for key in ("name", "type"):
                    val = col.get(key)
                    if not isinstance(val, str) or not val:
                        raise CollectFormatError(
                            f"{schema_name}.{t['name']}.{col_label} 缺少合法的 {key} 字段",
                            "collect JSON 结构不完整（旧版或手工编辑）",
                            "重跑 shared/db-collect.sh 重新采集",
                        )
                # relation-ddl-equivalence T2.7 [spec-review-amendment]:
                # tables[].columns[].fdw_options — render_table_ddl's foreign_
                # table branch hard-iterates and `.partition("=")`s each
                # element (via `_render_fdw_options_items`) into a column-level
                # `OPTIONS (...)` clause; shape shared with tables[].foreign.
                # options below (`_validate_kv_options_list`).
                _validate_kv_options_list(
                    f"{schema_name}.{t['name']}.{col_label}",
                    col.get("fdw_options"),
                    f"schemas[{idx}].tables[{tidx}].columns[{cidx}]",
                    "fdw_options",
                )
            # B5: render_table_ddl hard-subscripts constraints[].name /
            # indexes[].name / triggers[].name (sort keys + DDL statements);
            # indexes[].definition additionally (relation-ddl-equivalence
            # T2.4 — see _validate_named_entries' key == "indexes" branch).
            for sub_key in ("constraints", "indexes", "triggers"):
                _validate_named_entries(
                    f"{schema_name}.{t['name']}", sub_key, t.get(sub_key),
                    f"schemas[{idx}].tables[{tidx}]",
                )
            # relation-ddl-equivalence T2.4: tables[].options/tablespace and
            # tables[].indexes[].options/tablespace — render_table_ddl splices
            # all four into CREATE TABLE / ALTER INDEX statements (T2.1).
            table_owner = f"{schema_name}.{t['name']}"
            _validate_reloptions_list(
                table_owner, t.get("options"), f"schemas[{idx}].tables[{tidx}]"
            )
            _validate_tablespace_shape(
                table_owner, t.get("tablespace"), f"schemas[{idx}].tables[{tidx}]"
            )
            # relation-ddl-equivalence T2.7: tables[].access_method —
            # render_table_ddl's non-foreign branch splices it (quote_ident'd)
            # into a ` USING <am>` clause.
            _validate_access_method_shape(
                table_owner, t.get("access_method"), f"schemas[{idx}].tables[{tidx}]"
            )
            # relation-ddl-equivalence T2.7 [spec-review-amendment]:
            # tables[].foreign — render_table_ddl's foreign_table branch
            # hard-subscripts foreign["server"] and hard-iterates
            # foreign["options"]; kind == "foreign_table" with foreign
            # missing/null is rejected outright (D8 fail-loud), matching the
            # existing validate_function_definitions precedent.
            _validate_foreign_shape(table_owner, t, f"schemas[{idx}].tables[{tidx}]")
            for iidx, index_entry in enumerate(t.get("indexes") or []):
                if isinstance(index_entry, dict):
                    index_owner = f"{table_owner}.{index_entry.get('name', '')}"
                    index_location = f"schemas[{idx}].tables[{tidx}].indexes[{iidx}]"
                    _validate_reloptions_list(index_owner, index_entry.get("options"), index_location)
                    _validate_tablespace_shape(
                        index_owner, index_entry.get("tablespace"), index_location
                    )
        for key in ("functions", "views"):
            val = schema_obj.get(key)
            if val is not None and not isinstance(val, list):
                raise CollectFormatError(
                    f"元数据 JSON 的 schema 对象 {key} 不是数组",
                    f"schemas[{idx}]（schema={schema_name}）的 '{key}' 字段不是数组（{type(val).__name__}）",
                    "直接跑 shared/db-collect.sh --out /tmp/c.json 核对结构；版本不符时升级 pg-dict skill",
                )
        # B5: render_view_ddl hard-subscripts views[].columns[].name (COMMENT ON
        # COLUMN) and, for materialized views, views[].indexes[].name/definition
        # (relation-ddl-equivalence T2.4 — same depth as tables[].indexes[]);
        # views[].name itself is already covered by validate_identifiers.
        # `options`/`tablespace`/`populated` (view-reloptions-ddl D1,
        # [spec-review-amendment] Q1/Q2 拍板; relation-ddl-equivalence T2.4):
        # render_view_ddl hard-iterates/splices all of these — see
        # _validate_reloptions_list / _validate_tablespace_shape below for the
        # shared shape checks (also reused by tables[]/*.indexes[] above).
        for vidx, v in enumerate(schema_obj.get("views") or []):
            if isinstance(v, dict):
                view_owner = f"{schema_name}.{v.get('name', '')}"
                view_location = f"schemas[{idx}].views[{vidx}]"
                _validate_named_entries(view_owner, "columns", v.get("columns"), view_location)
                _validate_named_entries(view_owner, "indexes", v.get("indexes"), view_location)
                _validate_reloptions_list(view_owner, v.get("options"), view_location)
                _validate_tablespace_shape(view_owner, v.get("tablespace"), view_location)
                # relation-ddl-equivalence T2.7: views[].access_method —
                # render_view_ddl's materialized-view branch splices it into a
                # ` USING <am>` clause the same way tables[] does above.
                _validate_access_method_shape(view_owner, v.get("access_method"), view_location)
                populated = v.get("populated")
                if populated is not None and not isinstance(populated, bool):
                    raise CollectFormatError(
                        f"{view_owner} 的 populated 形状不合法：应为布尔 / 实得 "
                        f"{type(populated).__name__}",
                        "collect JSON 结构不完整（旧版或手工编辑）",
                        "重跑 shared/db-collect.sh 重新采集",
                    )
                for iidx, index_entry in enumerate(v.get("indexes") or []):
                    if isinstance(index_entry, dict):
                        index_owner = f"{view_owner}.{index_entry.get('name', '')}"
                        index_location = f"schemas[{idx}].views[{vidx}].indexes[{iidx}]"
                        _validate_reloptions_list(
                            index_owner, index_entry.get("options"), index_location
                        )
                        _validate_tablespace_shape(
                            index_owner, index_entry.get("tablespace"), index_location
                        )


def _validate_reloptions_list(owner: str, options, location: str) -> None:
    """relation-ddl-equivalence T2.1/T2.4 extraction: shape check for a
    reloptions-shaped `options[]` list — shared by tables[].options,
    views[].options (view-reloptions-ddl D1/[spec-review-amendment] Q1),
    tables[].indexes[].options and views[].indexes[].options
    ([spec-review-amendment] Q2 拍板). MUST be absent/None, or a list of
    strings each shaped `key` or `key=value` with `key` matching
    `_RELOPTION_NAME_RE` — render_table_ddl/render_view_ddl (via
    `_render_reloptions_items`) hard-iterate and `.partition("=")` each
    element, then splice the `key` segment UNQUOTED into `WITH (...)` /
    `SET (...)`: an empty/malformed `key` would otherwise yield invalid SQL
    (`WITH ()`) or splice extra statements into a written file ([T42])."""
    if options is None:
        return
    if not isinstance(options, list):
        raise CollectFormatError(
            f"元数据 JSON 的 {owner} 的 options 不是数组",
            f"{location} 的 'options' 字段不是数组（{type(options).__name__}）",
            "重跑 shared/db-collect.sh 重新采集",
        )
    for oidx, opt in enumerate(options):
        if not isinstance(opt, str):
            raise CollectFormatError(
                f"{owner} 的 options[{oidx}] 不是字符串",
                "collect JSON 结构不完整（旧版或手工编辑）",
                "重跑 shared/db-collect.sh 重新采集",
            )
        if not _RELOPTION_NAME_RE.match(opt.partition("=")[0]):
            raise CollectFormatError(
                f"{owner} 的 options[{oidx}] 不是合法的存储参数形态",
                f"元素 {opt!r} 的 name 段为空或含 [A-Za-z0-9_.] 以外的字符——"
                "pg_class.reloptions 不会产出这种元素（手工编辑或篡改的 collect JSON）",
                "重跑 shared/db-collect.sh 重新采集",
            )


def _validate_access_method_shape(owner: str, access_method, location: str) -> None:
    """relation-ddl-equivalence T2.7: `access_method` (tables[]/views[]) MUST
    be absent/None or a non-empty str — render_table_ddl/render_view_ddl
    splice it through `quote_ident()` into a ` USING <am>` clause; an empty
    string would silently render a zero-length identifier (`USING ""`,
    invalid SQL written to disk). Same shape rule as `_validate_tablespace_
    shape` (kept as a separate function since its `owner`/message use
    `access_method` wording, not `tablespace`)."""
    if access_method is None:
        return
    if isinstance(access_method, str) and access_method:
        return
    detail = (
        repr(access_method) if isinstance(access_method, str) else type(access_method).__name__
    )
    raise CollectFormatError(
        f"{owner} 的 access_method 形状不合法：应为 null 或非空字符串 / 实得 {detail}",
        f"{location} 的 'access_method' 字段不合法（collect JSON 结构不完整——旧版或手工编辑）",
        "重跑 shared/db-collect.sh 重新采集",
    )


def _validate_kv_options_list(owner: str, options, location: str, field: str) -> None:
    """relation-ddl-equivalence T2.7 [spec-review-amendment]: shape check for a
    `name=value`-shaped options list — shared by `tables[].foreign.options`
    and `tables[].columns[].fdw_options`. MUST be absent/None or a list of
    strings each containing `=` with a non-empty segment before it (PG's own
    `untransformRelOptions` guarantees this catalog shape for both
    `pg_foreign_table.ftoptions` and `pg_attribute.attfdwoptions` — value MAY
    be empty). `render_table_ddl`/`_render_fdw_options_items` hard-
    `.partition("=")` and `quote_ident()` the `key` segment of each element —
    a missing/empty `key` would otherwise splice a malformed or zero-length
    identifier into an `OPTIONS (...)` clause written to disk."""
    if options is None:
        return
    if not isinstance(options, list):
        raise CollectFormatError(
            f"元数据 JSON 的 {owner} 的 {field} 不是数组",
            f"{location} 的 '{field}' 字段不是数组（{type(options).__name__}）",
            "重跑 shared/db-collect.sh 重新采集",
        )
    for oidx, opt in enumerate(options):
        if not isinstance(opt, str):
            raise CollectFormatError(
                f"{owner} 的 {field}[{oidx}] 不是字符串",
                "collect JSON 结构不完整（旧版或手工编辑）",
                "重跑 shared/db-collect.sh 重新采集",
            )
        key, sep, _value = opt.partition("=")
        if not sep or not key:
            raise CollectFormatError(
                f"{owner} 的 {field}[{oidx}] 不是合法的 name=value 形态",
                f"元素 {opt!r} 缺少 '=' 或 '=' 前为空——PG untransformRelOptions "
                "保证目录形态恒为 name=value（value 可空）（手工编辑或篡改的 collect JSON）",
                "重跑 shared/db-collect.sh 重新采集",
            )


def _validate_foreign_shape(owner: str, table: dict, location: str) -> None:
    """relation-ddl-equivalence T2.7 (design.md D8 fail-loud precedent):
    `tables[].foreign` MUST be absent/None (unless `kind == "foreign_table"`,
    in which case it MUST be present) or a dict with a non-empty str
    `server` and an `options` list shaped per `_validate_kv_options_list`.
    `render_table_ddl`'s foreign_table branch hard-subscripts
    `foreign["server"]` — a table declared `kind == "foreign_table"` with no
    `foreign` dict would otherwise reach that subscript unvalidated mid-run,
    same class of bug `validate_function_definitions`'s DD-8 check already
    guards against for functions."""
    foreign = table.get("foreign")
    if foreign is None:
        if table.get("kind") == "foreign_table":
            raise CollectFormatError(
                f"{owner} 是外部表但缺少 foreign 字段（server/options）",
                f"{location} 的 'foreign' 字段缺失或为 null，但 kind == foreign_table",
                "重跑 shared/db-collect.sh 重新采集",
            )
        return
    if not isinstance(foreign, dict):
        raise CollectFormatError(
            f"{owner} 的 foreign 形状不合法：应为 null 或对象 / 实得 {type(foreign).__name__}",
            f"{location} 的 'foreign' 字段不合法（collect JSON 结构不完整——旧版或手工编辑）",
            "重跑 shared/db-collect.sh 重新采集",
        )
    server = foreign.get("server")
    if not isinstance(server, str) or not server:
        detail = repr(server) if isinstance(server, str) else type(server).__name__
        raise CollectFormatError(
            f"{owner} 的 foreign.server 形状不合法：应为非空字符串 / 实得 {detail}",
            f"{location} 的 'foreign.server' 字段不合法（collect JSON 结构不完整——旧版或手工编辑）",
            "重跑 shared/db-collect.sh 重新采集",
        )
    _validate_kv_options_list(owner, foreign.get("options"), location, "foreign.options")


def _validate_tablespace_shape(owner: str, tablespace, location: str) -> None:
    """relation-ddl-equivalence T2.4: `tablespace` (tables[]/views[]/either's
    indexes[]) MUST be absent/None or a non-empty str — render_table_ddl /
    render_view_ddl splice it through `quote_ident()` into a `TABLESPACE`/
    `ALTER INDEX ... SET TABLESPACE` clause; an empty string would silently
    render a zero-length identifier (`TABLESPACE ""`, invalid SQL written to
    disk). `location` is spliced into the `cause` line for machine-readable
    locatability, symmetric with `_validate_reloptions_list` (T44)."""
    if tablespace is None:
        return
    if isinstance(tablespace, str) and tablespace:
        return
    detail = repr(tablespace) if isinstance(tablespace, str) else type(tablespace).__name__
    raise CollectFormatError(
        f"{owner} 的 tablespace 形状不合法：应为 null 或非空字符串 / 实得 {detail}",
        f"{location} 的 'tablespace' 字段不合法（collect JSON 结构不完整——旧版或手工编辑）",
        "重跑 shared/db-collect.sh 重新采集",
    )


def _validate_named_entries(owner: str, key: str, entries, location: str) -> None:
    """B5: `entries` (an optional sub-list such as tables[].constraints) MUST be
    absent/None or a list of dicts each carrying a non-empty string `name` —
    every renderer sorts and subscripts these by `["name"]`, so a missing key
    would otherwise surface as a bare KeyError mid-run after earlier files were
    already written. Raises CollectFormatError with the same fix hint as A1.

    relation-ddl-equivalence T2.4: for `key == "indexes"` (tables[].indexes AND
    views[].indexes — same depth on both sides), each entry's `definition`
    MUST also be a non-empty str — render_table_ddl/render_view_ddl both
    hard-subscript `idx["definition"]` when emitting `CREATE INDEX ...;` /
    `ALTER INDEX ...;` lines."""
    if entries is None:
        return
    if not isinstance(entries, list):
        raise CollectFormatError(
            f"元数据 JSON 的 {owner} 的 {key} 不是数组",
            f"{location} 的 '{key}' 字段不是数组（{type(entries).__name__}）",
            "重跑 shared/db-collect.sh 重新采集",
        )
    for eidx, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            raise CollectFormatError(
                f"{owner} 的 {key}[{eidx}] 缺少合法的 name 字段",
                "collect JSON 结构不完整（旧版或手工编辑）",
                "重跑 shared/db-collect.sh 重新采集",
            )
        if key == "indexes" and (
            not isinstance(entry.get("definition"), str) or not entry["definition"]
        ):
            raise CollectFormatError(
                f"{owner} 的 {key}[{eidx}] 缺少合法的 definition 字段",
                "collect JSON 结构不完整（旧版或手工编辑）",
                "重跑 shared/db-collect.sh 重新采集",
            )


def _validate_requested_schemas(requested_schemas, schemas: list) -> None:
    """`requested_schemas` (top-level, added by
    shared/db-collect.sql) MUST be `null` or a deduplicated list of valid schema
    name strings, and every `schemas[].schema` MUST be a member of it — a stale/
    hand-edited/cross-version collect document that violates this could otherwise
    let render narrow the D-L convergence scope (disk ∩ requested_schemas) to
    something inconsistent with what it actually rendered, silently leaving a
    schema directory that's neither refreshed nor recognized as out-of-scope.
    Called from run() AFTER `_validate_schemas` (so every schemas[].schema is
    already known to be a non-empty string) and BEFORE any filesystem mutation —
    same "no file written on invalid input" contract as every other pre-flight
    check here. `None` (missing/explicit null) is the common case and returns
    immediately: full-scan semantics, byte-for-byte unchanged from before this
    field existed."""
    if requested_schemas is None:
        return
    if not isinstance(requested_schemas, list):
        raise CollectFormatError(
            "元数据 JSON 的 requested_schemas 字段既不是 null 也不是数组",
            f"'requested_schemas' 字段类型是 {type(requested_schemas).__name__}"
            "（期望 list[str] 或 null）",
            "直接跑 shared/db-collect.sh --out /tmp/c.json 核对顶层字段；版本不符时升级 pg-dict skill",
        )
    seen: set[str] = set()
    for idx, name in enumerate(requested_schemas):
        if (
            not isinstance(name, str)
            or not name
            or not IDENTIFIER_RE.match(name)
            or _is_pure_dot_string(name)
        ):
            raise CollectFormatError(
                "元数据 JSON 的 requested_schemas 含非法 schema 名",
                f"requested_schemas[{idx}]={name!r} 不是合法的 schema 标识符",
                "核对 .dbllm.env 的 SCHEMAS 声明或 shared/db-collect.sh 的 --schema 参数",
            )
        if name in seen:
            raise CollectFormatError(
                "元数据 JSON 的 requested_schemas 含重复 schema 名",
                f"requested_schemas 中 {name!r} 出现多次",
                "核对 .dbllm.env 的 SCHEMAS 声明或 shared/db-collect.sh 的 --schema 参数，去除重复项",
            )
        seen.add(name)

    schema_names = {s["schema"] for s in schemas}
    extra = schema_names - seen
    if extra:
        raise CollectFormatError(
            "元数据 JSON 的 schemas[] 含 requested_schemas 范围外的 schema",
            f"schemas[].schema 中 {sorted(extra)} 不在 requested_schemas={sorted(seen)} 之内",
            "核对 shared/db-collect.sql 的范围过滤是否与 requested_schemas 一致；"
            "跨版本 producer 或手工编辑的 collect JSON 不能绕过范围保证",
        )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def run(raw: str, dbmeta_dir: Path) -> dict:
    """Top-level entry point, independent of argparse/stdin plumbing so
    end-to-end fixtures can call it directly. Parses+validates the v1 collect
    document (incl. D-M identifier contract), collapses any schema no longer in
    the collect result (D-L), merges each schema's table/view/function files,
    writes .dbmeta/_gaps.md, and returns the same summary dict main() prints as
    stdout JSON. Raises CollectFormatError on invalid input — caller MUST NOT
    have written or deleted any file in that case (all validation runs before any
    filesystem mutation)."""
    metadata = parse_collect_document(raw)
    schemas = metadata["schemas"]
    _validate_schemas(schemas)
    requested_schemas = metadata.get("requested_schemas")
    _validate_requested_schemas(requested_schemas, schemas)
    validate_identifiers(schemas, dbmeta_dir)
    validate_function_definitions(schemas)
    validate_managed_marker_collisions(schemas)

    # DD-12/REQ-RI-5: load_confirmed() MUST run before any .dbmeta/ write —
    # alongside the validate_* calls above, before the mkdir below. A missing
    # file (first run, dbmeta_dir not created yet) reads as [] without error.
    confirmed_relations = load_confirmed(dbmeta_dir)

    dbmeta_dir.mkdir(parents=True, exist_ok=True)

    # [impl-review-fix] F-E: reject any symlink directly under .dbmeta/ BEFORE the
    # D-L collapse loop below touches anything — the loop's `_collapse_schema_dir`
    # walks and deletes files inside what it assumes is a real directory; if
    # .dbmeta/<x> is actually a symlink to somewhere outside the repo, that walk
    # deletes the LINK TARGET's files, and the trailing `rmdir()` then raises
    # NotADirectoryError (reproduced: it deleted files under a temp dir the symlink
    # pointed at, then crashed on rmdir). Scanning ALL entries up front (rather
    # than checking one entry right before processing it) matters because the loop
    # deletes as it goes — a later symlink must not be reached only after earlier
    # entries already got deleted.
    for entry in sorted(dbmeta_dir.iterdir()):
        if entry.is_symlink():
            raise CollectFormatError(
                f".dbmeta/ 下发现符号链接 {entry.name}",
                "render 的收敛删除不跟随链接、也不能静默跳过",
                "人工核实后移除该链接或改为真实目录",
            )

    written: list[str] = []
    deleted: list[str] = []
    unchanged: list[str] = []
    legacy_md: list[str] = []

    def absorb(res: SyncResult) -> None:
        # T29: merge one sync path's bookkeeping into run()'s totals by field name.
        written.extend(res.written)
        deleted.extend(res.deleted)
        unchanged.extend(res.unchanged)
        legacy_md.extend(res.legacy_md)

    gaps: dict[str, list[str]] = {"tables": [], "columns": [], "functions": [], "sensitive": []}
    schema_summaries: list[dict] = []

    current_schema_names = {s["schema"] for s in schemas}
    # requested_scope is None for full-scan collects
    # (unchanged behavior); non-None narrows the D-L convergence domain from
    # "disk" to "disk ∩ requested_schemas" — a directory outside the requested
    # scope is left untouched (not deleted, not treated as stale) even though
    # it's also absent from this run's collect result, because that absence is
    # explained by scope, not by the schema having been dropped from the DB.
    requested_scope = set(requested_schemas) if requested_schemas is not None else None
    out_of_scope_schemas: list[str] = []

    # D-L: any directory on disk not present in this run's collect result gets
    # fully collapsed before we touch the schemas that ARE present, so a
    # stale/renamed schema directory can't accidentally interleave collapse and
    # write ordering in a way that matters (it doesn't — the two sets are
    # disjoint by construction — but collapsing first keeps the written/deleted
    # bookkeeping easy to reason about). DD-13: a legacy `backfill/` placeholder
    # (pre-dates this contract; COMMENT-backfill now belongs to a separate
    # db-comment skill) gets no special case here — it converges through this
    # exact same path as any other unrecognized directory: managed files (if any
    # ever existed under it) would be cleaned, hand-written files survive, and
    # an empty directory is removed.
    for entry in sorted(dbmeta_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in current_schema_names:
            continue
        if requested_scope is not None and entry.name not in requested_scope:
            out_of_scope_schemas.append(entry.name)
            continue
        absorb(_collapse_schema_dir(entry))

    # D-D: built once across the WHOLE collect result (a trigger in one schema may
    # call a function in another) before any table/function file is rendered.
    func_names_index = _build_func_names_index(schemas)
    trigger_refs = _build_trigger_refs(schemas, func_names_index)

    for schema_obj in schemas:
        schema = schema_obj["schema"]
        tables = schema_obj.get("tables", [])
        views = schema_obj.get("views") or []
        functions = schema_obj.get("functions") or []

        absorb(_render_schema_tables(dbmeta_dir, schema, tables, func_names_index))
        absorb(_render_schema_views(dbmeta_dir, schema, views))
        absorb(_render_schema_functions(dbmeta_dir, schema, functions, trigger_refs))

        w, u = _sync_collect_slice(dbmeta_dir, schema, schema_obj)
        written.extend(w)
        unchanged.extend(u)

        readme_parents, readme_children_by_parent = group_children(tables)
        resolve_orphan_children(readme_parents, readme_children_by_parent)
        w, u = _sync_schema_readme(
            dbmeta_dir, schema, readme_parents, readme_children_by_parent, views, functions
        )
        written.extend(w)
        unchanged.extend(u)

        schema_gaps: dict[str, list[str]] = {
            "tables": [], "columns": [], "functions": [], "sensitive": []
        }
        collect_gaps(schema, schema_obj, schema_gaps)
        for key in gaps:
            gaps[key].extend(schema_gaps[key])

        schema_summaries.append(
            {
                "schema": schema,
                "n_tables": len(readme_parents),
                "n_views": len(views),
                "n_functions": len({f["name"] for f in functions}),
                # "缺注释" = missing-COMMENT count only (tables+columns+functions);
                # the sensitive-name warning is a separate concern, not a comment
                # gap, and lives only in _gaps.md/_relations.md.
                "n_gaps": (
                    len(schema_gaps["tables"])
                    + len(schema_gaps["columns"])
                    + len(schema_gaps["functions"])
                ),
            }
        )

    w, u = _sync_root_readme(dbmeta_dir, schema_summaries, sorted(out_of_scope_schemas))
    written.extend(w)
    unchanged.extend(u)

    w, u = _sync_candidates(dbmeta_dir, build_candidates(schemas, confirmed_relations))
    written.extend(w)
    unchanged.extend(u)

    w, u = _sync_pending_sql(dbmeta_dir, schemas, confirmed_relations)
    written.extend(w)
    unchanged.extend(u)

    w, u = _sync_relations_report(dbmeta_dir, schemas, requested_schemas, confirmed_relations)
    written.extend(w)
    unchanged.extend(u)

    gaps_path = dbmeta_dir / "_gaps.md"
    gaps_content = render_gaps_report(gaps, requested_schemas)
    existing_gaps = gaps_path.read_text(encoding="utf-8") if gaps_path.exists() else None
    if gaps_content != existing_gaps:
        gaps_path.write_text(gaps_content, encoding="utf-8")
        written.append(str(gaps_path))
    else:
        unchanged.append(str(gaps_path))

    # DD-6: a leftover *.md object file (pre-.sql skill version) is never read or
    # deleted — only reported, once, sorted by path, so a human can migrate its
    # hand-written annotation into the corresponding .sql file and git rm it.
    legacy_md.sort()
    if legacy_md:
        print(
            f"[pg-dict] 发现旧格式对象文件 {len(legacy_md)} 个（已改为 .sql，旧 .md "
            f"未读未删）：{', '.join(legacy_md)}；请人工把 .md 里的注记搬到同名 .sql "
            "后 git rm 这些 .md",
            file=sys.stderr,
        )

    return {
        "written": written,
        "deleted": deleted,
        "unchanged": unchanged,
        "schemas": [s["schema"] for s in schemas],
        "gaps": {
            "tables": len(gaps["tables"]),
            "columns": len(gaps["columns"]),
            "functions": len(gaps["functions"]),
            "sensitive": len(gaps["sensitive"]),
        },
        "legacy_md": legacy_md,
        # requested_schemas: the top-level collect JSON
        # field verbatim (None = full scan). out_of_scope_schemas: on-disk schema
        # dirs excluded from this run's D-L convergence because they fall outside
        # requested_schemas (always [] when requested_schemas is None). pg-dict.sh
        # surfaces both in its closing summary.
        "requested_schemas": requested_schemas,
        "out_of_scope_schemas": sorted(out_of_scope_schemas),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dbmeta-dir", required=True, help="output directory, e.g. dbmeta")
    args = ap.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("problem: stdin 上没有收到元数据（db-collect.sh 输出为空）", file=sys.stderr)
        print("cause: shared/db-collect.sh 无输出，或管道上游已失败", file=sys.stderr)
        print("fix: 直接跑 shared/db-collect.sh 看输出与报错", file=sys.stderr)
        return 1

    try:
        result = run(raw, Path(args.dbmeta_dir))
    except CollectFormatError as exc:
        print(f"problem: {exc.problem}", file=sys.stderr)
        print(f"cause: {exc.cause}", file=sys.stderr)
        print(f"fix: {exc.fix}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
