"""Unit tests for shared/ro_guard.py (design.md DD-2, tasks.md 2.2, REQ-RS-4 /
REQ-RS-6). Pure-function tests, no DB, no subprocess — evaluate()/truncate_csv()/
truncate_lines() are called directly."""
import csv
import hashlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

import ro_guard  # noqa: E402


# ---------------------------------------------------------------------------
# multi-statement (REQ-RS-4)
# ---------------------------------------------------------------------------


def test_multi_statement_rejected():
    result = ro_guard.evaluate(
        "SELECT 1; COMMIT; INSERT INTO t VALUES (1)", limit=200, no_limit=False
    )
    assert result["ok"] is False
    assert result["reason"] == "multi-statement"


def test_semicolon_inside_line_comment_not_multi_statement():
    result = ro_guard.evaluate("SELECT 'a;b' AS x -- c;d", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["reason"] is None
    assert result["kind"] == "select"


def test_semicolon_inside_dollar_quote_not_multi_statement():
    result = ro_guard.evaluate("SELECT $q$x;y$q$", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["kind"] == "select"


def test_trailing_semicolon_allowed_and_stripped():
    result = ro_guard.evaluate("SELECT 1;", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["sql"] == "SELECT 1"


# ---------------------------------------------------------------------------
# meta-command (REQ-RS-4 [spec-review-amendment])
# ---------------------------------------------------------------------------


def test_meta_command_bang_rejected():
    result = ro_guard.evaluate("SELECT 1\n\\! id", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "meta-command"


def test_meta_command_o_rejected():
    result = ro_guard.evaluate("SELECT 1\n\\o /tmp/x", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "meta-command"


def test_backslash_inside_string_literal_allowed():
    result = ro_guard.evaluate("SELECT 'a\\b'", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["reason"] is None
    assert result["kind"] == "select"


# ---------------------------------------------------------------------------
# whitelist / set_config (REQ-RS-4)
# ---------------------------------------------------------------------------


def test_set_config_rejected_case_insensitive():
    result = ro_guard.evaluate(
        "SELECT SET_CONFIG('statement_timeout','0',true), pg_sleep(600)",
        limit=200,
        no_limit=False,
    )
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_set_config_double_quoted_rejected():
    # PG double-quoted identifiers are never case-folded, so `"set_config"` is
    # the exact same built-in function as bare `set_config` — this is the
    # bypass form: _sanitize() blanks the quoted identifier out of the text
    # _SET_CONFIG_RE searches, so without the dedicated quoted-form check this
    # would incorrectly pass through as ok=True (verified red before the fix:
    # the bare-only check at the old line 192 does not see anything inside a
    # `"..."` span, since that span is fully blanked to spaces).
    result = ro_guard.evaluate(
        'SELECT "set_config"(\'statement_timeout\',\'0\',true)',
        limit=200,
        no_limit=False,
    )
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_set_config_double_quoted_schema_qualified_rejected():
    result = ro_guard.evaluate(
        'SELECT pg_catalog."set_config"(\'statement_timeout\',\'0\',true)',
        limit=200,
        no_limit=False,
    )
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_set_config_uppercase_quoted_identifier_not_matched():
    # "SET_CONFIG" (quoted, uppercase) names a *different* identifier from the
    # built-in set_config — PG never folds quoted-identifier case, so this is
    # not the same function and must not be treated as the bypass. (It would
    # in practice fail at the DB with "function does not exist"; the guard's
    # job is only to not falsely block it here.)
    result = ro_guard.evaluate(
        "SELECT \"SET_CONFIG\"('x','0',true)", limit=200, no_limit=False
    )
    assert result["ok"] is True


def test_double_quoted_identifier_not_set_config_still_allowed():
    # A plain double-quoted column identifier that has nothing to do with
    # set_config must not be caught by the new quoted-form check (no
    # over-blocking / false positive).
    result = ro_guard.evaluate('SELECT "some_col" FROM t', limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["kind"] == "select"


def test_values_rejected():
    result = ro_guard.evaluate("VALUES (1)", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_do_block_rejected():
    result = ro_guard.evaluate("DO $$ BEGIN END $$", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_begin_rejected():
    result = ro_guard.evaluate("BEGIN", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_lowercase_and_leading_comment_do_not_affect_verdict():
    result = ro_guard.evaluate("-- comment\n select 1", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["reason"] is None
    assert result["kind"] == "select"


def test_with_wraps_and_tolerates_embedded_string_semicolon():
    result = ro_guard.evaluate(
        "WITH a AS (SELECT 'x;y' AS z) SELECT z FROM a", limit=9, no_limit=False
    )
    assert result["ok"] is True
    assert result["kind"] == "with"
    assert result["wrapped"] == (
        "SELECT * FROM (WITH a AS (SELECT 'x;y' AS z) SELECT z FROM a\n) _q LIMIT 10"
    )


# ---------------------------------------------------------------------------
# --limit validation (REQ-RS-4 [spec-review-amendment])
# ---------------------------------------------------------------------------


def test_bad_limit_zero():
    result = ro_guard.evaluate("SELECT 1", limit=0, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "bad-limit"


def test_bad_limit_negative():
    result = ro_guard.evaluate("SELECT 1", limit=-5, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "bad-limit"


def test_bad_limit_non_numeric():
    result = ro_guard.evaluate("SELECT 1", limit="abc", no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "bad-limit"


# ---------------------------------------------------------------------------
# wrapping (REQ-RS-6)
# ---------------------------------------------------------------------------


def test_select_wrapped_with_limit_plus_one():
    result = ro_guard.evaluate("SELECT * FROM t", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["wrapped"] == "SELECT * FROM (SELECT * FROM t\n) _q LIMIT 201"


def test_select_with_trailing_line_comment_wraps_on_its_own_line():
    # Before the fix, `wrapped` was `f"SELECT * FROM ({core}) _q LIMIT {n}"` —
    # a trailing `--` line comment in `core` (no newline after it, since core
    # is the raw single-statement text) would swallow `) _q LIMIT N` on the
    # same line, leaving an unclosed paren (a real psql syntax error, not a
    # guard bypass — verified red before the fix by asserting the suffix used
    # to land on the comment's own line).
    result = ro_guard.evaluate(
        "SELECT id FROM users -- 注释", limit=200, no_limit=False
    )
    assert result["ok"] is True
    wrapped = result["wrapped"]
    # the `) _q LIMIT 201` suffix must be on a line the `--` comment can't reach
    comment_line, _, rest = wrapped.partition("\n")
    assert comment_line == "SELECT * FROM (SELECT id FROM users -- 注释"
    assert rest == ") _q LIMIT 201"
    # and the wrapped text must itself be free of any unclosed-paren defect:
    # every char after the last un-commented `--` is on a separate line from
    # the closing `)`.
    assert ") _q LIMIT" not in comment_line


def test_no_limit_not_wrapped():
    result = ro_guard.evaluate("SELECT * FROM t", limit=None, no_limit=True)
    assert result["ok"] is True
    assert result["wrapped"] == "SELECT * FROM t"


def test_explain_not_wrapped_even_with_limit():
    result = ro_guard.evaluate("EXPLAIN SELECT 1", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["kind"] == "explain"
    assert result["wrapped"] == "EXPLAIN SELECT 1"


def test_show_not_wrapped():
    result = ro_guard.evaluate("SHOW statement_timeout", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["kind"] == "show"
    assert result["wrapped"] == "SHOW statement_timeout"


# ---------------------------------------------------------------------------
# sha8 (DD-2)
# ---------------------------------------------------------------------------


def test_sha8_is_stable_sha256_prefix():
    result = ro_guard.evaluate("SELECT 1", limit=200, no_limit=False)
    expected = hashlib.sha256(b"SELECT 1").hexdigest()[:8]
    assert result["sha8"] == expected
    # calling again with the same input reproduces the same sha8
    again = ro_guard.evaluate("SELECT 1", limit=200, no_limit=False)
    assert again["sha8"] == expected


def test_sha8_differs_for_different_sql():
    a = ro_guard.evaluate("SELECT 1", limit=200, no_limit=False)
    b = ro_guard.evaluate("SELECT 2", limit=200, no_limit=False)
    assert a["sha8"] != b["sha8"]


# ---------------------------------------------------------------------------
# truncate (record-level, REQ-RS-6 [spec-review-amendment])
# ---------------------------------------------------------------------------


def test_truncate_csv_keeps_whole_records_across_embedded_newlines():
    # 3 data records, the second one has an embedded newline in a quoted field —
    # a physical-line truncation (e.g. `head -N`) would cut it mid-record.
    csv_text = (
        'id,note\r\n'
        '1,"first"\r\n'
        '2,"line one\nline two"\r\n'
        '3,"third"\r\n'
    )
    instream = io.StringIO(csv_text, newline="")
    reader = csv.reader(instream)
    outstream = io.StringIO(newline="")
    writer = csv.writer(outstream, lineterminator="\n")

    rows_written, truncated = ro_guard.truncate_csv(reader, writer, 2)

    assert rows_written == 2
    assert truncated is True

    # what we wrote back must itself parse as exactly 2 data records + header
    outstream.seek(0)
    parsed = list(csv.reader(io.StringIO(outstream.getvalue(), newline="")))
    assert parsed == [
        ["id", "note"],
        ["1", "first"],
        ["2", "line one\nline two"],
    ]


def test_truncate_csv_not_truncated_when_within_limit():
    csv_text = "id\r\n1\r\n2\r\n"
    instream = io.StringIO(csv_text, newline="")
    reader = csv.reader(instream)
    outstream = io.StringIO(newline="")
    writer = csv.writer(outstream, lineterminator="\n")

    rows_written, truncated = ro_guard.truncate_csv(reader, writer, 2)

    assert rows_written == 2
    assert truncated is False


def test_truncate_lines_by_line_count():
    text_lines = ["Seq Scan on t\n", "  Filter: x\n", "Planning Time: 0.1ms\n"]
    outstream = io.StringIO()

    rows_written, truncated = ro_guard.truncate_lines(iter(text_lines), outstream, 2)

    assert rows_written == 2
    assert truncated is True
    assert outstream.getvalue() == "Seq Scan on t\n  Filter: x\n"


# ---------------------------------------------------------------------------
# T9: EXPLAIN ANALYZE detection
# ---------------------------------------------------------------------------


def test_explain_analyze_legacy_rejected():
    result = ro_guard.evaluate("EXPLAIN ANALYZE SELECT 1", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"
    assert result["kind"] == "explain"


def test_explain_analyze_modern_parenthesized_rejected():
    result = ro_guard.evaluate(
        "EXPLAIN (ANALYZE true, COSTS false) SELECT 1", limit=200, no_limit=False
    )
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_explain_analyze_on_variant_rejected():
    result = ro_guard.evaluate(
        "EXPLAIN (ANALYZE on) SELECT 1", limit=200, no_limit=False
    )
    assert result["ok"] is False


def test_explain_without_analyze_allowed():
    result = ro_guard.evaluate("EXPLAIN SELECT 1", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["kind"] == "explain"


def test_explain_costs_only_allowed():
    result = ro_guard.evaluate(
        "EXPLAIN (COSTS true) SELECT 1", limit=200, no_limit=False
    )
    assert result["ok"] is True


def test_explain_analyze_false_allowed():
    """EXPLAIN (ANALYZE false) does NOT execute the statement — safe.

    Every PG spelling of an explicit false, so the reject rule cannot be
    satisfied by pattern-matching the word ANALYZE alone.
    """
    for sql in (
        "EXPLAIN (ANALYZE false) SELECT 1",
        "EXPLAIN (ANALYZE off) SELECT 1",
        "EXPLAIN (ANALYZE 0) SELECT 1",
        "EXPLAIN (ANALYZE no) SELECT 1",
        "EXPLAIN (ANALYZE false, BUFFERS) SELECT 1",
    ):
        result = ro_guard.evaluate(sql, limit=200, no_limit=False)
        assert result["ok"] is True, sql


def test_explain_analyze_omitted_boolean_rejected():
    """PG's grammar is `ANALYZE [ boolean ]` and an omitted boolean means TRUE,
    so each of these executes the statement. Cases are derived from that grammar
    rather than from the guard's regex — the pre-fix regex demanded a truthy
    literal and let every one of them through (B10).
    """
    for sql in (
        "EXPLAIN (ANALYZE) SELECT 1",
        "EXPLAIN (ANALYZE, BUFFERS) SELECT 1",
        "EXPLAIN (BUFFERS, ANALYZE) SELECT 1",
        "EXPLAIN (COSTS false, ANALYZE) SELECT 1",
    ):
        result = ro_guard.evaluate(sql, limit=200, no_limit=False)
        assert result["ok"] is False, sql
        assert result["reason"] == "not-whitelisted", sql
        assert result["kind"] == "explain", sql


def test_explain_analyze_without_whitespace_rejected():
    """`EXPLAIN(...)` is valid SQL — no whitespace is required before the paren,
    which the pre-fix `\\bEXPLAIN\\s+` missed independently of the boolean (B10).
    """
    for sql in (
        "EXPLAIN(ANALYZE TRUE) SELECT 1",
        "EXPLAIN(ANALYZE) SELECT 1",
        "EXPLAIN(ANALYZE, BUFFERS) SELECT 1",
    ):
        result = ro_guard.evaluate(sql, limit=200, no_limit=False)
        assert result["ok"] is False, sql
        assert result["reason"] == "not-whitelisted", sql


# ---------------------------------------------------------------------------
# T9: E-string \x27 handling
# ---------------------------------------------------------------------------


def test_e_string_backslash_quote_not_string_end():
    """E'...\\'...' — backslash-escaped quote inside E-string must not end the
    string literal prematurely. Before the fix, _sanitize treated \\' as the
    closing quote, leaving a dangling quote that could misparse subsequent SQL."""
    result = ro_guard.evaluate(r"SELECT E'it\x27s fine'", limit=200, no_limit=False)
    assert result["ok"] is True
    assert result["kind"] == "select"


def test_e_string_backslash_escape_sequences():
    result = ro_guard.evaluate(r"SELECT E'\n\t\\'", limit=200, no_limit=False)
    assert result["ok"] is True


def test_plain_string_not_affected_by_e_string_logic():
    """A plain '...' string must still use '' escaping, not backslash."""
    result = ro_guard.evaluate("SELECT 'it''s fine'", limit=200, no_limit=False)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# T9: --limit upper bound (bigint max)
# ---------------------------------------------------------------------------


def test_limit_exceeding_bigint_max_rejected():
    result = ro_guard.evaluate("SELECT 1", limit=2**63, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "bad-limit"


def test_limit_at_bigint_max_allowed():
    result = ro_guard.evaluate("SELECT 1", limit=2**63 - 1, no_limit=False)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# T10: adversarial tests — writable CTE, nested dollar-quote, empty input
# ---------------------------------------------------------------------------


def test_writable_cte_passes_guard_delegated_to_pg():
    """WITH ... DELETE is a writable CTE — the guard intentionally lets it through
    (ok=True) because the guard only checks syntax shape, not write intent. PG's
    READ ONLY transaction mode (0A000) blocks the actual execution. This test
    documents that delegation contract and guards against future regressions that
    might accidentally reject writable CTEs at the guard level."""
    result = ro_guard.evaluate(
        "WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d",
        limit=200,
        no_limit=False,
    )
    assert result["ok"] is True
    assert result["kind"] == "with"


def test_nested_different_tag_dollar_quotes():
    """$a$...$b$;$b$...$a$ — the inner $b$...$b$ pair is consumed first (tags
    must match), so the top-level ; between $b$ and $a$ is still inside the
    outer $a$...$a$ span and must not be seen as a statement separator."""
    result = ro_guard.evaluate(
        "SELECT $a$outer $b$;inner$b$ still outer$a$", limit=200, no_limit=False
    )
    assert result["ok"] is True
    assert result["kind"] == "select"


def test_empty_input_rejected():
    result = ro_guard.evaluate("", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_pure_comment_input_rejected():
    result = ro_guard.evaluate("-- just a comment", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_block_comment_only_rejected():
    result = ro_guard.evaluate("/* nothing here */", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


# ---------------------------------------------------------------------------
# T11: lexical-boundary confusions in _sanitize (spec-review add-pg-sql-check,
# findings M1/M2 — both were live guard bypasses, each reproduced before the fix)
# ---------------------------------------------------------------------------


def test_dollar_in_identifier_does_not_open_dollar_quote():
    """`a$tag$` is ONE identifier (PG allows `$` in an identifier's non-first
    position), so the `;` after it is a real top-level terminator and the `\\! id`
    behind it is a real psql meta-command. Treating the `$tag$` as a dollar-quote
    opener masked both, and the payload was judged ok=True — a psql-level command
    execution path, since ro-session.sh feeds the accepted text to `psql -f`."""
    result = ro_guard.evaluate(
        "SELECT 1 AS a$tag$;\n\\! id\n$tag$", limit=200, no_limit=False
    )
    assert result["ok"] is False
    assert result["reason"] == "multi-statement"


def test_dollar_quote_after_ident_char_variants_rejected():
    """Same confusion via other identifier-continuation chars before the `$`."""
    for sql in (
        "SELECT 1 AS x_$t$;\n\\! id\n$t$",
        "SELECT 1 AS a$$;\n\\! id\n$$",
    ):
        result = ro_guard.evaluate(sql, limit=200, no_limit=False)
        assert result["ok"] is False, sql
        assert result["reason"] == "multi-statement", sql


def test_typed_literal_e_suffix_is_not_an_e_string():
    """`date'...'` is PG's documented `type 'string'` syntax (allowed for every
    type). The trailing `e` of `date` is NOT an E-string prefix, so `\\` is not
    special and the quote closes where PG closes it. Misreading it as E'...' made
    the scanner run past the real closing quote and blank the top-level `;`."""
    result = ro_guard.evaluate(
        r"SELECT date'2024-01-01\' ; DROP TABLE secrets -- '",
        limit=200,
        no_limit=False,
    )
    assert result["ok"] is False
    assert result["reason"] == "multi-statement"


def test_typed_literal_e_suffix_does_not_hide_set_config():
    """Same root cause defeating the set_config rule as well: both structural
    checks read the same poisoned sanitized text."""
    result = ro_guard.evaluate(
        r"SELECT date'x\' ; SELECT set_config('statement_timeout','0',true), pg_sleep(600) -- '",
        limit=200,
        no_limit=False,
    )
    assert result["ok"] is False
    assert result["reason"] == "multi-statement"


# ---------------------------------------------------------------------------
# Task 1 (add-pg-sql-check): mode="prepare" — ADR-0007 / REQ-RS-4 amendment.
# Structural checks (multi-statement / meta-command / set_config) are
# unchanged across modes; only the first-keyword whitelist (and the EXPLAIN
# ANALYZE special-case) is skipped in prepare mode, since PG's own PREPARE
# grammar is the whitelist there.
# ---------------------------------------------------------------------------


def test_default_mode_is_query_values_still_rejected():
    # Regression: evaluate() with no `mode` kwarg at all must behave exactly
    # as before this change — VALUES/DO/BEGIN remain not-whitelisted.
    result = ro_guard.evaluate("VALUES (1)", limit=200, no_limit=False)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_query_mode_explicit_values_do_begin_still_rejected():
    for sql in ("VALUES (1)", "DO $$ BEGIN END $$", "BEGIN"):
        result = ro_guard.evaluate(sql, limit=200, no_limit=True, mode="query")
        assert result["ok"] is False, sql
        assert result["reason"] == "not-whitelisted", sql


def test_prepare_mode_insert_allowed():
    result = ro_guard.evaluate(
        "INSERT INTO t (x) VALUES (1)", limit=None, no_limit=True, mode="prepare"
    )
    assert result["ok"] is True
    assert result["reason"] is None
    assert result["kind"] == "insert"
    assert result["sql"] == "INSERT INTO t (x) VALUES (1)"


def test_prepare_mode_update_allowed():
    result = ro_guard.evaluate(
        "UPDATE t SET x = 1 WHERE id = 1", limit=None, no_limit=True, mode="prepare"
    )
    assert result["ok"] is True
    assert result["kind"] == "update"


def test_prepare_mode_delete_allowed():
    result = ro_guard.evaluate(
        "DELETE FROM t WHERE id = 1", limit=None, no_limit=True, mode="prepare"
    )
    assert result["ok"] is True
    assert result["kind"] == "delete"


def test_prepare_mode_merge_allowed():
    result = ro_guard.evaluate(
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DO NOTHING",
        limit=None,
        no_limit=True,
        mode="prepare",
    )
    assert result["ok"] is True
    assert result["kind"] == "merge"


def test_prepare_mode_values_allowed():
    result = ro_guard.evaluate("VALUES (1)", limit=None, no_limit=True, mode="prepare")
    assert result["ok"] is True
    assert result["kind"] == "values"


def test_prepare_mode_multi_statement_rejected():
    result = ro_guard.evaluate(
        "INSERT INTO t VALUES (1); DROP TABLE t", limit=None, no_limit=True, mode="prepare"
    )
    assert result["ok"] is False
    assert result["reason"] == "multi-statement"


def test_prepare_mode_meta_command_rejected():
    result = ro_guard.evaluate(
        "INSERT INTO t VALUES (1)\n\\! id", limit=None, no_limit=True, mode="prepare"
    )
    assert result["ok"] is False
    assert result["reason"] == "meta-command"


def test_prepare_mode_set_config_rejected():
    result = ro_guard.evaluate(
        "INSERT INTO t (x) VALUES (set_config('statement_timeout','0',true))",
        limit=None,
        no_limit=True,
        mode="prepare",
    )
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_guard_cli_prepare_flag_wires_to_prepare_mode(monkeypatch, capsys):
    # CLI-level check that `guard --prepare` reaches evaluate(mode="prepare"):
    # a bare DELETE (rejected in default/query mode) must be accepted when
    # --prepare is passed. in-process (main()) per this file's no-subprocess
    # scope, not a subprocess call.
    monkeypatch.setattr(sys, "stdin", io.StringIO("DELETE FROM t WHERE id = 1"))
    rc = ro_guard.main(["guard", "--no-limit", "--prepare"])
    assert rc == 0
    out = capsys.readouterr().out
    import json

    result = json.loads(out)
    assert result["ok"] is True
    assert result["kind"] == "delete"


def test_guard_cli_without_prepare_flag_keeps_query_mode(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("DELETE FROM t WHERE id = 1"))
    rc = ro_guard.main(["guard", "--no-limit"])
    assert rc == 0
    out = capsys.readouterr().out
    import json

    result = json.loads(out)
    assert result["ok"] is False
    assert result["reason"] == "not-whitelisted"


def test_standalone_e_string_still_recognized_after_fix():
    """The boundary check must not break the real E'...' form, including when the
    `E` sits at position 0 of the scanned text and after a non-identifier char."""
    for sql in (r"SELECT E'it\'s fine'", r"SELECT (E'a\'b')", r"SELECT e'x\'y'"):
        result = ro_guard.evaluate(sql, limit=200, no_limit=False)
        assert result["ok"] is True, sql
        assert result["kind"] == "select", sql
