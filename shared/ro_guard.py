#!/usr/bin/env python3
"""shared/ro_guard.py — ro-session single-statement / whitelist / limit-wrap guard.

Pure Python stdlib, no psql dependency (design.md DD-2, tasks.md 2.1, REQ-RS-4 /
REQ-RS-6). Consumed by shared/ro-session.sh (a separate ticket, T-session) as a
subprocess: `ro_guard.py guard` reads raw SQL text from stdin and prints a JSON
verdict to stdout; `ro_guard.py truncate` reads a psql `COPY ... CSV HEADER` (or
plain-text EXPLAIN/SHOW) stream from stdin and re-emits it truncated to N records/
lines.

Guard verdict JSON schema (DD-2):
    {
      "ok": bool,
      "reason": str | null,   # null when ok=True; else one of
                               # multi-statement / meta-command / not-whitelisted /
                               # bad-limit
      "kind": "select"|"with"|"explain"|"show"|null,
                               # prepare-mode additions (DML rewritten to a
                               # PREPARE'd statement, ro-session prepare path):
                               # "insert"|"update"|"delete"|"merge"|"values"
      "sql": <trimmed, trailing-semicolon-stripped single statement> | null,
      "wrapped": <statement actually sent to PG> | null,
      "sha8": <first 8 hex chars of sha256(sql)> | null,
    }

`ro_guard.py` itself never encodes the guard verdict in its process exit code —
translating ok=False into "exit 3" is ro-session.sh's job (DD-3). This keeps the
guard a pure judgment function: same input always produces the same JSON, exit
code 0 unless invoked wrong (missing/invalid CLI args).

Scanner: a single left-to-right pass builds a "sanitized" same-length copy of the
input where every character that lives inside a `--` line comment, a `/* */`
block comment (nesting-aware, since PG nests them), a '...'-quoted string (''
escape), a "..."-quoted identifier ("" escape), or a $tag$...$tag$ dollar-quoted
string is replaced with a literal space. Top-level characters (SQL code) are left
untouched. Because the mask is same-length, positions line up 1:1 with the
original text, so both regex matching (first token, `set_config` identifier) and
slicing (isolating the first statement at a top-level `;`) can be done against
the sanitized text and applied back to the original.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from typing import IO, Iterable

WHITELIST_KEYWORDS = {"SELECT", "WITH", "EXPLAIN", "SHOW"}
_LIMIT_UPPER_BOUND = 2**63 - 1  # PG bigint max

_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
# Characters that can continue (not start) a PG identifier: letters, digits, `_`
# and `$`. Used to reject two lexical-boundary confusions in _sanitize() — an
# `E`/`e` that is merely the last letter of a longer token, and a `$` that is
# part of an identifier rather than opening a dollar-quote. Both misreadings let
# the scanner mask a span PG would not, hiding real top-level `;` / `\`.
_IDENT_CONT_RE = re.compile(r"[A-Za-z0-9_$]")
_SET_CONFIG_RE = re.compile(r"(?i)\bset_config\s*\(")
# PG double-quoted identifiers are case-sensitive and never case-folded, so the
# quoted spelling of the built-in function is exactly `"set_config"` (lowercase
# only — `"SET_CONFIG"` names a different, unrelated identifier). This regex is
# intentionally case-sensitive (no re.I), unlike _SET_CONFIG_RE above.
_SET_CONFIG_QUOTED_RE = re.compile(r'"set_config"\s*\(')
_FIRST_TOKEN_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)")
# EXPLAIN option variants that trigger write-side analysis (ANALYZE executes the
# statement, which could mutate data via writable CTEs under a non-RO transaction).
_EXPLAIN_ANALYZE_RE = re.compile(
    r"(?i)\bEXPLAIN\s*"                  # `\s*`: `EXPLAIN(...)` needs no whitespace
    r"(?:"
    r"ANALYZE\b"                         # legacy: EXPLAIN ANALYZE <stmt>
    r"|"
    # modern: EXPLAIN ( ... ANALYZE [ boolean ] ... ). PG's grammar makes the
    # boolean optional and an omitted one means TRUE, so the test is "not an
    # explicit false" rather than "followed by a truthy literal".
    r"\([^)]*\bANALYZE\b(?!\s+(?:false|off|0|no)\b)"
    r")"
)


def _sanitize(sql: str, *, keep_double_quoted_idents: bool = False) -> str:
    """Return a same-length copy of `sql` with everything outside top-level SQL
    code (comments, string/identifier literals, dollar-quoted bodies) blanked to
    spaces. Top-level code characters are preserved verbatim, including their
    original positions.

    `keep_double_quoted_idents=True` is a narrow variant used only to detect the
    double-quoted spelling of set_config (`"set_config"(...)`): it leaves the
    contents of `"..."` quoted-identifier spans (including the quotes) intact
    instead of blanking them, while every other span (comments, '...' string
    literals, dollar-quoted bodies) is still blanked exactly as in the default
    mode. This does not change the default (False) behavior used everywhere
    else."""
    n = len(sql)
    out = [" "] * n
    i = 0
    while i < n:
        c = sql[i]

        # -- line comment: runs to end of line (exclusive of the newline itself,
        # which stays masked as a space too — irrelevant, it's whitespace either
        # way).
        if c == "-" and sql[i + 1 : i + 2] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue

        # /* block comment */, nesting-aware (PG nests these).
        if c == "/" and sql[i + 1 : i + 2] == "*":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if sql[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif sql[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            i = j
            continue

        # '...' string literal (or E'...' with backslash escaping).
        if c == "'":
            # The `E` prefix only makes this an escape string when it stands
            # alone. In `date'2024-01-01'` the `e` is the last letter of the type
            # name (PG's documented `type 'string'` syntax, allowed for *every*
            # type), so PG reads a plain literal where `\` is NOT special. Taking
            # it for E'...' made the scanner treat `\'` as escaped, run past the
            # real closing quote, and blank out the top-level `;` / `set_config`
            # that followed — defeating both the single-statement and the
            # set_config rule at once.
            is_e_string = (
                i > 0
                and sql[i - 1 : i] in ("E", "e")
                and out[i - 1] != " "
                and not _IDENT_CONT_RE.match(sql[i - 2 : i - 1] or " ")
            )
            j = i + 1
            while j < n:
                if is_e_string and sql[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if sql[j] == "'":
                    if sql[j + 1 : j + 2] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            if is_e_string:
                out[i - 1] = " "
            i = j
            continue

        # "..." quoted identifier, "" is an escaped quote.
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if sql[j + 1 : j + 2] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            if keep_double_quoted_idents:
                out[i:j] = sql[i:j]
            i = j
            continue

        # $tag$...$tag$ dollar-quoted string (tag may be empty: $$...$$).
        # A `$` only opens one when it cannot be read as part of an identifier:
        # PG allows `$` in an identifier's non-first position, so `a$tag$` is one
        # identifier and a `;` after it is a real top-level terminator. Masking
        # from that `$` hid the `;` and any psql meta-command behind it.
        # Deliberately conservative: after a digit (`1$tag$`) PG would open a
        # dollar-quote but we do not, which can only over-reject, never under-.
        if c == "$" and not _IDENT_CONT_RE.match(sql[i - 1 : i] if i > 0 else " "):
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                j = n if end == -1 else end + len(tag)
                i = j
                continue

        out[i] = c
        i += 1

    return "".join(out)


def _parse_positive_int(raw: str | int | None) -> int | None:
    """Return `raw` as a positive int, or None if it isn't one (covers bad
    strings like "abc" as well as zero/negative values and values exceeding
    bigint max — reason=bad-limit for all of them, per REQ-RS-4)."""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    if value <= 0 or value > _LIMIT_UPPER_BOUND:
        return None
    return value


def _reject(reason: str, *, kind: str | None = None, sql: str | None = None) -> dict:
    sha8 = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:8] if sql else None
    return {
        "ok": False,
        "reason": reason,
        "kind": kind,
        "sql": sql,
        "wrapped": None,
        "sha8": sha8,
    }


def evaluate(
    sql: str, *, limit: str | int | None, no_limit: bool = False, mode: str = "query"
) -> dict:
    """Judge a single raw SQL text against the ro-session guard (REQ-RS-4 /
    REQ-RS-6). Pure function: same inputs always produce the same JSON-able
    dict. `limit` is the resolved --limit value (already RO_DEFAULT_LIMIT-
    substituted by the caller if the user didn't pass --limit); ignored when
    no_limit=True.

    `mode` (ADR-0007, add-pg-sql-check REQ-RS-4 amendment):
      - "query" (default): unchanged behavior — first keyword must be in
        WHITELIST_KEYWORDS, and EXPLAIN ANALYZE is rejected (checks 4/5 below).
      - "prepare": the pg-sql-check `PREPARE`-based path. Checks 1-3 (single
        statement / no meta-command / no set_config) still apply unchanged —
        those are what keeps a user statement from escaping the `PREPARE
        <name> AS <sql>` payload position (design.md TG-27). Checks 4
        (EXPLAIN ANALYZE) and 5 (keyword whitelist) are skipped: PG's own
        PREPARE grammar only accepts SELECT/INSERT/UPDATE/DELETE/MERGE/VALUES
        (never EXPLAIN, never DDL), so letting PG's grammar reject anything
        else is the whole point of ADR-0007 — this file MUST NOT grow a
        second, self-maintained keyword list to duplicate that. `kind` still
        reports the lowercased first keyword (e.g. "insert"/"update"/
        "delete"/"merge"/"values") for diagnostics.
    """
    sanitized = _sanitize(sql)

    # 1. multi-statement: a top-level ';' that isn't followed by only
    #    whitespace/masked-comment content is a second statement.
    first_semi = sanitized.find(";")
    if first_semi != -1:
        remainder = sanitized[first_semi + 1 :]
        if remainder.strip():
            return _reject("multi-statement")

    core_span = sanitized if first_semi == -1 else sanitized[:first_semi]

    # 2. meta-command: a bare backslash at top level (outside quotes/comments)
    #    — the statement is executed via `psql -f`, so a leading `\!`/`\o`/`\i`/
    #    etc. would be interpreted as a psql meta-command, not SQL.
    if "\\" in core_span:
        return _reject("meta-command")

    # 3. set_config(...) at top level can rewrite the SET LOCAL statement_timeout
    #    ro-session relies on — the one built-in that can unwind that guard from
    #    inside an otherwise-whitelisted statement. Checked in both its bare form
    #    (set_config(...), case-insensitive — PG folds unquoted identifiers to
    #    lowercase) and its double-quoted form ("set_config"(...), case-sensitive
    #    — PG never folds quoted identifiers) since both name the same built-in
    #    function and REQ-RS-4 bars set_config regardless of spelling.
    if _SET_CONFIG_RE.search(core_span):
        return _reject("not-whitelisted")
    quoted_core_span = _sanitize(sql, keep_double_quoted_idents=True)
    quoted_core_span = quoted_core_span if first_semi == -1 else quoted_core_span[:first_semi]
    if _SET_CONFIG_QUOTED_RE.search(quoted_core_span):
        return _reject("not-whitelisted")

    # 4. EXPLAIN ANALYZE — executes the statement (can mutate via writable CTEs).
    #    Skipped in prepare mode: PREPARE's grammar never accepts EXPLAIN at
    #    all, so PG itself rejects it (ADR-0007 — no self-built keyword list).
    if mode != "prepare" and _EXPLAIN_ANALYZE_RE.search(core_span):
        core = (sql if first_semi == -1 else sql[:first_semi]).strip()
        return _reject("not-whitelisted", kind="explain", sql=core or None)

    # 5. first keyword must be in the whitelist — skipped in prepare mode
    #    (ADR-0007: PREPARE's own grammar is the whitelist there). `kind`
    #    still reports the lowercased first keyword in both modes.
    m = _FIRST_TOKEN_RE.match(core_span)
    first_token = m.group(1).upper() if m else ""
    if mode != "prepare" and first_token not in WHITELIST_KEYWORDS:
        core = (sql if first_semi == -1 else sql[:first_semi]).strip()
        return _reject("not-whitelisted", sql=core or None)
    kind = first_token.lower()

    core = (sql if first_semi == -1 else sql[:first_semi]).strip()

    # 5. --limit / RO_DEFAULT_LIMIT must be a positive integer unless --no-limit.
    limit_value: int | None = None
    if not no_limit:
        limit_value = _parse_positive_int(limit)
        if limit_value is None:
            return _reject("bad-limit", kind=kind, sql=core)

    wrapped = core
    if kind in ("select", "with") and limit_value is not None:
        # A trailing unterminated `--` line comment inside `core` (e.g. the
        # user's query ends `... -- note`) would otherwise swallow the
        # `) _q LIMIT N` suffix if it were appended on the same line, leaving
        # an unclosed paren. The newline before `)` puts that suffix on its
        # own line so any such comment only eats its own line.
        wrapped = f"SELECT * FROM ({core}\n) _q LIMIT {limit_value + 1}"

    sha8 = hashlib.sha256(core.encode("utf-8")).hexdigest()[:8]
    return {
        "ok": True,
        "reason": None,
        "kind": kind,
        "sql": core,
        "wrapped": wrapped,
        "sha8": sha8,
    }


def truncate_csv(instream: Iterable[list], outstream: "csv._writer", limit: int) -> tuple[int, bool]:
    """Copy CSV records from `instream` (a csv.reader-like iterable of rows,
    header row first) to `outstream` (a csv.writer), keeping the header plus at
    most `limit` data records. Truncates on record boundaries, not physical
    lines, so a quoted multi-line field is never split mid-record. Returns
    (rows_written, truncated)."""
    rows = iter(instream)
    try:
        header = next(rows)
    except StopIteration:
        return 0, False
    outstream.writerow(header)

    rows_written = 0
    truncated = False
    for row in rows:
        if rows_written < limit:
            outstream.writerow(row)
            rows_written += 1
        else:
            truncated = True
            break
    return rows_written, truncated


def truncate_lines(instream: Iterable[str], outstream: IO[str], limit: int) -> tuple[int, bool]:
    """Copy plain-text lines from `instream` to `outstream`, keeping at most
    `limit` lines (EXPLAIN/SHOW output has no CSV header/record structure).
    Returns (rows_written, truncated)."""
    rows_written = 0
    truncated = False
    for line in instream:
        if rows_written < limit:
            outstream.write(line)
            rows_written += 1
        else:
            truncated = True
            break
    return rows_written, truncated


def _cmd_guard(args: argparse.Namespace) -> int:
    sql = sys.stdin.read()
    mode = "prepare" if args.prepare else "query"
    result = evaluate(sql, limit=args.limit, no_limit=args.no_limit, mode=mode)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _cmd_truncate(args: argparse.Namespace) -> int:
    limit = _parse_positive_int(args.limit)
    if limit is None:
        print(f"ro_guard.py: truncate --limit must be a positive integer, got {args.limit!r}", file=sys.stderr)
        return 2

    if args.format == "text":
        rows_written, truncated = truncate_lines(sys.stdin, sys.stdout, limit)
    else:
        try:
            sys.stdin.reconfigure(newline="")
        except AttributeError:
            pass
        try:
            sys.stdout.reconfigure(newline="")
        except AttributeError:
            pass
        reader = csv.reader(sys.stdin)
        writer = csv.writer(sys.stdout, lineterminator="\n")
        rows_written, truncated = truncate_csv(reader, writer, limit)

    print(f"RO_TRUNCATED={'true' if truncated else 'false'}", file=sys.stderr)
    print(f"RO_ROWS={rows_written}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ro_guard.py")
    sub = parser.add_subparsers(dest="command", required=True)

    guard_p = sub.add_parser("guard", help="judge a single SQL statement read from stdin")
    limit_group = guard_p.add_mutually_exclusive_group(required=True)
    limit_group.add_argument("--limit", help="row-cap N (positive integer) for select/with wrapping")
    limit_group.add_argument("--no-limit", action="store_true", help="do not wrap with a LIMIT")
    guard_p.add_argument(
        "--prepare",
        action="store_true",
        help=(
            "judge in prepare mode (ADR-0007): skip the first-keyword whitelist "
            "and the EXPLAIN ANALYZE check, since PG's own PREPARE grammar only "
            "accepts SELECT/INSERT/UPDATE/DELETE/MERGE/VALUES; the single-"
            "statement / meta-command / set_config checks still apply"
        ),
    )
    guard_p.set_defaults(func=_cmd_guard)

    truncate_p = sub.add_parser("truncate", help="truncate a result stream read from stdin to N records/lines")
    truncate_p.add_argument("--limit", required=True, help="max records (csv) or lines (text) to keep")
    truncate_p.add_argument("--format", choices=("csv", "text"), default="csv")
    truncate_p.set_defaults(func=_cmd_truncate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
