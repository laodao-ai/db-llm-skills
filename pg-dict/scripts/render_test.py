#!/usr/bin/env python3
"""Unit tests for .agents/skills/pg-dict/scripts/render.py — .dbmeta/ one-object-
one-file-one-block merge/delete/orphan/D-L/D-M logic (Task 1 tracer bullet).

Run: python3 render_test.py
(stdlib unittest only — no extra deps.)

Fixture groups map to openspec/changes/dbmeta-knowledge-base/impl-reports/
task1-brief.md's acceptance checklist:
  - CLI/stdout summary shape (written/deleted/unchanged/schemas/gaps)
  - single managed block per table file, merge preserves block-external text
  - object disappearance: pure-generated file deleted + empty dir cleaned;
    annotated file kept with one idempotent orphan banner; file header itself
    never counted as annotation
  - whole-schema disappearance (D-L) collapse, both pure-generated and
    annotated object files, deleted[] bookkeeping
  - D-M identifier fail-loud (bad chars / "--"), zero filesystem mutation
  - dotted table name round-trips as a literal filename/block name, idempotent
  - carried-over v1 top-level parsing / partition-fold structure / father-not-
    in-schema fallback / gaps idempotency-sensitive-overloaded-function fixtures
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render  # noqa: E402


def make_table(name: str, columns=None, comment=None, partition_of=None):
    table = {
        "name": name,
        "comment": comment,
        "columns": columns
        or [
            {"name": "id", "type": "bigint", "default": None, "nullable": False, "comment": None},
        ],
    }
    if partition_of is not None:
        table["partition_of"] = partition_of
    return table


_OMIT = object()


def v1_doc(schemas: list[dict], requested_schemas=_OMIT) -> str:
    """`requested_schemas` defaults to being omitted from the document entirely
    (design.md TG-05: "缺失 ⇒ 读作 null ⇒ 全量语义") — pass an explicit value
    (including `None`) to set the key, or the module-level `_OMIT` sentinel is
    never passed by callers so this default always omits."""
    doc = {"collect_version": 1, "schemas": schemas}
    if requested_schemas is not _OMIT:
        doc["requested_schemas"] = requested_schemas
    return json.dumps(doc)


def schema_obj(name: str, tables=None, functions=None, views=None) -> dict:
    return {
        "schema": name,
        "tables": tables or [],
        "functions": functions or [],
        "views": views or [],
    }


def make_full_table(
    name: str,
    columns=None,
    comment=None,
    partition_of=None,
    kind=None,
    reltuples=None,
    indexes=None,
    constraints=None,
    triggers=None,
    options=None,
    tablespace=None,
    access_method=None,
    foreign=None,
) -> dict:
    """Like make_table() but also accepts the Task-2 full-render fields (indexes/
    constraints/triggers/reltuples/kind) — kept as a separate helper so Task-1's
    make_table() fixtures (which never set these) stay untouched/unaffected.
    relation-ddl-equivalence T2.5: `options`/`tablespace` default to `[]`/`None`
    (byte-identical-output defaults, design.md "数据模型与生命周期").
    relation-ddl-equivalence T2.8: `access_method`/`foreign` default to
    `None`/`None` (same byte-identical-output-by-default discipline)."""
    table = make_table(name, columns=columns, comment=comment, partition_of=partition_of)
    if kind is not None:
        table["kind"] = kind
    if reltuples is not None:
        table["reltuples"] = reltuples
    table["indexes"] = indexes or []
    table["constraints"] = constraints or []
    table["triggers"] = triggers or []
    table["options"] = options if options is not None else []
    table["tablespace"] = tablespace
    table["access_method"] = access_method
    table["foreign"] = foreign
    return table


def make_function(
    name: str,
    identity_args="",
    result_type="void",
    language="plpgsql",
    comment=None,
    source="BEGIN END;",
    definition=None,
) -> dict:
    """`definition` (design.md DD-4/DD-8, added on top of Task 1's fixture) is the
    `pg_get_functiondef` output a newer db-collect.sql produces; Task 2's front-
    loaded fail-loud check requires it to be present (non-empty) on every
    function, so this defaults to a synthesized-but-plausible value here rather
    than being left absent — a test that specifically wants to exercise the
    "missing definition" fail-loud path passes `definition=""` (or deletes the
    key from the returned dict) instead."""
    if definition is None:
        definition = (
            f"CREATE OR REPLACE FUNCTION {name}({identity_args}) "
            f"RETURNS {result_type} LANGUAGE {language} AS $function$\n{source}\n$function$"
        )
    return {
        "name": name,
        "identity_args": identity_args,
        "arg_names": [],
        "result_type": result_type,
        "language": language,
        "comment": comment,
        "source": source,
        "definition": definition,
    }


def make_view(
    name: str,
    columns=None,
    comment=None,
    definition="SELECT 1",
    options=None,
    tablespace=None,
    populated=True,
    indexes=None,
    access_method=None,
) -> dict:
    """relation-ddl-equivalence T2.5: `tablespace`/`populated`/`indexes` default
    to `None`/`True`/`[]` — the byte-identical-output defaults for a normal,
    already-populated, default-tablespace, index-free (materialized) view
    (design.md "数据模型与生命周期"). relation-ddl-equivalence T2.8:
    `access_method` defaults to `None` (same discipline)."""
    return {
        "name": name,
        "kind": "view",
        "comment": comment,
        "definition": definition,
        "columns": columns
        or [
            {"position": 1, "name": "id", "type": "bigint", "nullable": False, "comment": None},
        ],
        "options": options if options is not None else [],
        "tablespace": tablespace,
        "populated": populated,
        "indexes": indexes if indexes is not None else [],
        "access_method": access_method,
    }


def make_trigger(name: str, fn_ref: str, enabled="O", inherited_from=None) -> dict:
    return {
        "name": name,
        "definition": f"CREATE TRIGGER {name} AFTER INSERT ON t FOR EACH ROW EXECUTE FUNCTION {fn_ref}()",
        "enabled": enabled,
        "inherited_from": inherited_from,
    }


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dbmeta_dir = Path(self._tmp.name) / "dbmeta"

    def tearDown(self):
        self._tmp.cleanup()

    def run_render(self, doc: str) -> dict:
        return render.run(doc, self.dbmeta_dir)


# ---------------------------------------------------------------------------
# stdout summary shape
# ---------------------------------------------------------------------------


class SummaryShapeTests(TmpDirCase):
    def test_summary_has_the_eight_required_keys(self):
        # `requested_schemas`/`out_of_scope_schemas`
        # join the summary shape on top of task4's prior six keys (`legacy_md`
        # (DD-6) joined Task 1's original five before that).
        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertEqual(
            set(result.keys()),
            {
                "written", "deleted", "unchanged", "schemas", "gaps", "legacy_md",
                "requested_schemas", "out_of_scope_schemas",
            },
        )
        self.assertEqual(result["schemas"], ["auth"])
        self.assertEqual(result["legacy_md"], [])
        self.assertIsNone(result["requested_schemas"])
        self.assertEqual(result["out_of_scope_schemas"], [])

    def test_first_run_writes_table_file_and_gaps_report(self):
        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        table_path = self.dbmeta_dir / "auth" / "tables" / "users.sql"
        gaps_path = self.dbmeta_dir / "_gaps.md"
        self.assertIn(str(table_path), result["written"])
        self.assertIn(str(gaps_path), result["written"])
        self.assertTrue(table_path.exists())
        self.assertTrue(gaps_path.exists())


# ---------------------------------------------------------------------------
# single managed block, block-external text preserved
# ---------------------------------------------------------------------------


class ManagedBlockMergeTests(TmpDirCase):
    def test_table_file_has_exactly_one_managed_block(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertEqual(content.count("-- pg-dict:table:users:start"), 1)
        self.assertEqual(content.count("-- pg-dict:table:users:end"), 1)

    def test_regen_rewrites_block_and_preserves_text_outside_it(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        path = self.dbmeta_dir / "auth" / "tables" / "users.sql"
        content = path.read_text(encoding="utf-8")
        annotated = content + "\n-- 人工注记：此表即将拆分\n"
        path.write_text(annotated, encoding="utf-8")

        changed_users = make_table(
            "users",
            columns=[
                {"name": "id", "type": "bigint", "default": None, "nullable": False, "comment": None},
                {"name": "nickname", "type": "text", "default": None, "nullable": True, "comment": None},
            ],
        )
        self.run_render(v1_doc([schema_obj("auth", tables=[changed_users])]))
        out = path.read_text(encoding="utf-8")
        self.assertIn("nickname", out)
        self.assertIn("-- 人工注记：此表即将拆分", out)

    def test_second_regen_with_unchanged_metadata_is_byte_identical(self):
        doc = v1_doc([schema_obj("auth", tables=[make_table("users"), make_table("roles")])])
        self.run_render(doc)
        path = self.dbmeta_dir / "auth" / "tables" / "users.sql"
        first = path.read_text(encoding="utf-8")
        self.run_render(doc)
        second = path.read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_new_table_appended_when_regenerating_schema(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.run_render(
            v1_doc([schema_obj("auth", tables=[make_table("users"), make_table("sessions")])])
        )
        self.assertTrue((self.dbmeta_dir / "auth" / "tables" / "sessions.sql").exists())
        self.assertTrue((self.dbmeta_dir / "auth" / "tables" / "users.sql").exists())


# ---------------------------------------------------------------------------
# object disappearance (D-C)
# ---------------------------------------------------------------------------


class ObjectRemovalTests(TmpDirCase):
    def test_pure_generated_file_deleted_and_empty_dir_cleaned(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("tmp")])]))
        table_path = self.dbmeta_dir / "auth" / "tables" / "tmp.sql"
        self.assertTrue(table_path.exists())

        result = self.run_render(v1_doc([schema_obj("auth", tables=[])]))
        self.assertFalse(table_path.exists())
        self.assertFalse(table_path.parent.exists())
        self.assertIn(str(table_path), result["deleted"])

    def test_annotated_file_kept_with_one_idempotent_orphan_banner(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("tmp")])]))
        path = self.dbmeta_dir / "auth" / "tables" / "tmp.sql"
        path.write_text(path.read_text(encoding="utf-8") + "\n-- 人工注记：保留观察\n", encoding="utf-8")

        result = self.run_render(v1_doc([schema_obj("auth", tables=[])]))
        first = path.read_text(encoding="utf-8")
        self.assertTrue(path.exists())
        self.assertNotIn(str(path), result["deleted"])
        self.assertNotIn("-- pg-dict:table:tmp:start", first)
        self.assertEqual(first.count(render.SQL_SYNTAX.orphan_banner), 1)
        self.assertIn("-- 人工注记：保留观察", first)

        # re-run again (table still absent) — byte-identical, banner not duplicated
        self.run_render(v1_doc([schema_obj("auth", tables=[])]))
        second = path.read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(second.count(render.SQL_SYNTAX.orphan_banner), 1)

    def test_reappearing_object_clears_orphan_banner_and_keeps_annotation(self):
        # T35: removed -> 孤立注记 banner; object comes back -> banner dropped,
        # hand-written text kept, managed block restored.
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("tmp")])]))
        path = self.dbmeta_dir / "auth" / "tables" / "tmp.sql"
        path.write_text(path.read_text(encoding="utf-8") + "\n-- 人工注记：保留观察\n", encoding="utf-8")
        self.run_render(v1_doc([schema_obj("auth", tables=[])]))
        self.assertIn(render.SQL_SYNTAX.orphan_banner, path.read_text(encoding="utf-8"))

        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("tmp")])]))
        content = path.read_text(encoding="utf-8")
        self.assertNotIn(render.SQL_SYNTAX.orphan_banner, content)
        self.assertIn("-- pg-dict:table:tmp:start", content)
        self.assertIn("-- 人工注记：保留观察", content)
        self.assertIn(str(path), result["written"])
        # idempotent afterwards
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("tmp")])]))
        self.assertEqual(content, path.read_text(encoding="utf-8"))

    def test_file_header_alone_does_not_trigger_orphan_banner(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("tmp")])]))
        table_path = self.dbmeta_dir / "auth" / "tables" / "tmp.sql"

        result = self.run_render(v1_doc([schema_obj("auth", tables=[])]))
        # No hand-written text ever existed outside the block (just the auto
        # header) -> deletion, not an orphan banner.
        self.assertFalse(table_path.exists())
        self.assertIn(str(table_path), result["deleted"])


# ---------------------------------------------------------------------------
# Task 4 (design.md DD-6 [spec-review-amendment] / task4-brief 4.2): a `.md`
# object file left in the same dir as the now-`.sql` files (pre-.sql skill
# version) is NEVER read or deleted — only collected into the summary's
# `legacy_md` and reported once via stderr, in the exact wording DD-6 pins.
# ---------------------------------------------------------------------------


class LegacyMdReportTests(TmpDirCase):
    def test_legacy_md_reported_untouched_and_stderr_message_matches_dd6_verbatim(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        legacy_path = self.dbmeta_dir / "auth" / "tables" / "old_notes.md"
        legacy_content = "# 手写旧文档\n\n人工内容，不应被读取或删除\n"
        legacy_path.write_text(legacy_content, encoding="utf-8")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))

        # not read (would otherwise have no effect anyway) / not deleted
        self.assertEqual(legacy_path.read_text(encoding="utf-8"), legacy_content)
        self.assertEqual(result["legacy_md"], [str(legacy_path)])
        self.assertNotIn(str(legacy_path), result["deleted"])
        self.assertEqual(
            stderr.getvalue(),
            f"[pg-dict] 发现旧格式对象文件 1 个（已改为 .sql，旧 .md 未读未删）："
            f"{legacy_path}；请人工把 .md 里的注记搬到同名 .sql 后 git rm 这些 .md\n",
        )

    def test_no_legacy_md_no_stderr_output(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertEqual(result["legacy_md"], [])
        self.assertEqual(stderr.getvalue(), "")

    def test_multiple_legacy_md_across_kinds_sorted_lexicographically_by_path(self):
        self.run_render(
            v1_doc(
                [
                    schema_obj(
                        "auth",
                        tables=[make_table("users")],
                        functions=[make_function("fn_a")],
                    )
                ]
            )
        )
        p_table = self.dbmeta_dir / "auth" / "tables" / "zzz_old.md"
        p_fn = self.dbmeta_dir / "auth" / "functions" / "aaa_old.md"
        p_table.write_text("stale", encoding="utf-8")
        p_fn.write_text("stale", encoding="utf-8")

        result = self.run_render(
            v1_doc(
                [
                    schema_obj(
                        "auth",
                        tables=[make_table("users")],
                        functions=[make_function("fn_a")],
                    )
                ]
            )
        )
        self.assertEqual(result["legacy_md"], sorted([str(p_table), str(p_fn)]))


# ---------------------------------------------------------------------------
# whole-schema disappearance (D-L)
# ---------------------------------------------------------------------------


class SchemaCollapseTests(TmpDirCase):
    def test_legacy_md_under_collapsed_schema_reported_not_deleted(self):
        """T28: a pre-.sql `*.md` object file inside a schema that vanished from
        the collect result goes through the same report-only `legacy_md` channel
        as the live _sync_object_dir path — never read, never deleted."""
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a")])]))
        legacy = self.dbmeta_dir / "tmp" / "tables" / "a.md"
        legacy.write_text("# 旧格式\n", encoding="utf-8")

        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertTrue(legacy.exists())
        self.assertEqual(legacy.read_text(encoding="utf-8"), "# 旧格式\n")
        self.assertEqual(result["legacy_md"], [str(legacy)])
        self.assertNotIn(str(legacy), result["deleted"])
        self.assertIn(str(self.dbmeta_dir / "tmp" / "tables" / "a.sql"), result["deleted"])
        # the leftover .md keeps tables/ (and thus tmp/) non-empty, so neither is rmdir'd
        self.assertTrue((self.dbmeta_dir / "tmp" / "tables").exists())

    def test_collapse_returns_sync_result(self):
        """T29: every sync path returns SyncResult so run() merges by field name."""
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a")])]))
        res = render._collapse_schema_dir(self.dbmeta_dir / "tmp")
        self.assertIsInstance(res, render.SyncResult)
        self.assertEqual(res.written, [])
        self.assertEqual(res.unchanged, [])
        self.assertIn(str(self.dbmeta_dir / "tmp" / "tables" / "a.sql"), res.deleted)

    def test_schema_fully_generated_disappears_entirely(self):
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a"), make_table("b")])]))
        schema_dir = self.dbmeta_dir / "tmp"
        self.assertTrue(schema_dir.exists())

        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertFalse(schema_dir.exists())
        self.assertIn(str(schema_dir / "tables" / "a.sql"), result["deleted"])
        self.assertIn(str(schema_dir / "tables" / "b.sql"), result["deleted"])

    def test_schema_with_annotated_object_file_keeps_that_file_but_removes_rest(self):
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a"), make_table("b")])]))
        a_path = self.dbmeta_dir / "tmp" / "tables" / "a.sql"
        a_path.write_text(a_path.read_text(encoding="utf-8") + "\n-- 人工注记：勿删\n", encoding="utf-8")

        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        b_path = self.dbmeta_dir / "tmp" / "tables" / "b.sql"
        self.assertTrue(a_path.exists())
        self.assertFalse(b_path.exists())
        self.assertIn(str(b_path), result["deleted"])
        self.assertNotIn(str(a_path), result["deleted"])
        self.assertIn(render.SQL_SYNTAX.orphan_banner, a_path.read_text(encoding="utf-8"))
        # schema dir itself must survive since a.sql is still there
        self.assertTrue((self.dbmeta_dir / "tmp").exists())

    def test_stray_collect_json_deleted_outright_on_schema_collapse(self):
        """_collect.json (D-E) has no managed-block/hand-annotation concept — any
        content is always deleted outright on schema collapse, unlike README.md
        below (F-C)."""
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a")])]))
        schema_dir = self.dbmeta_dir / "tmp"
        (schema_dir / "_collect.json").write_text("{}", encoding="utf-8")

        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertFalse(schema_dir.exists())
        self.assertIn(str(schema_dir / "_collect.json"), result["deleted"])

    # [impl-review-fix] F-C: dbmeta spec DBM-2 gives README.md the same
    # managed-block contract (block content regenerated, block-external text
    # hand-preserved) as every other dbmeta object file. The old
    # _collapse_schema_dir unconditionally unlink()'d README.md on schema
    # collapse, silently destroying any hand-written note living outside its
    # `<!-- pg-dict:index:start/end -->` block the moment the schema disappeared
    # from the collect result. These two tests pin both halves of the fixed
    # contract: annotated -> kept + orphaned; unannotated (pure generated, already
    # covered by SchemaReadmeTests.test_readme_deleted_outright_on_schema_collapse
    # below) -> deleted.
    def test_readme_with_annotation_kept_orphaned_on_schema_collapse(self):
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a")])]))
        readme_path = self.dbmeta_dir / "tmp" / "README.md"
        readme_path.write_text(
            readme_path.read_text(encoding="utf-8")
            + "\n> 人工注记：这个 schema 的迁移历史很特殊，勿删\n",
            encoding="utf-8",
        )

        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertTrue(readme_path.exists())
        self.assertNotIn(str(readme_path), result["deleted"])
        content = readme_path.read_text(encoding="utf-8")
        self.assertEqual(content.count(render.ORPHAN_BANNER), 1)
        self.assertIn("人工注记：这个 schema 的迁移历史很特殊，勿删", content)
        # schema dir itself must survive since README.md is still there — rmdir
        # must not be attempted/raise (it's guarded by an emptiness check).
        self.assertTrue((self.dbmeta_dir / "tmp").exists())
        # the pure-generated table file (no annotation) is still removed as usual
        self.assertFalse((self.dbmeta_dir / "tmp" / "tables" / "a.sql").exists())

    def test_readme_with_annotation_idempotent_across_repeated_collapse_runs(self):
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a")])]))
        readme_path = self.dbmeta_dir / "tmp" / "README.md"
        readme_path.write_text(
            readme_path.read_text(encoding="utf-8") + "\n> 勿删\n", encoding="utf-8"
        )
        other_doc = v1_doc([schema_obj("auth", tables=[make_table("users")])])
        self.run_render(other_doc)
        first = readme_path.read_text(encoding="utf-8")
        self.run_render(other_doc)
        second = readme_path.read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(second.count(render.ORPHAN_BANNER), 1)

    def test_legacy_empty_backfill_dir_removed_and_reported_in_deleted(self):
        # DD-13 / REQ-DM-3: `backfill/` is no longer a special-cased placeholder
        # — a legacy empty one converges through the exact same D-L path as any
        # other unrecognized directory (empty ⇒ removed, and reported).
        backfill_dir = self.dbmeta_dir / "backfill"
        backfill_dir.mkdir(parents=True)
        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertFalse(backfill_dir.exists())
        self.assertIn(str(backfill_dir), result["deleted"])

    def test_legacy_backfill_dir_with_handwritten_file_survives(self):
        # A legacy `backfill/` holding a hand-written file is not a recognized
        # managed file location (no _collect.json/README.md/tables|views|
        # functions kind dirs directly under it), so it is left untouched and
        # the directory is not empty ⇒ not removed.
        backfill_dir = self.dbmeta_dir / "backfill"
        backfill_dir.mkdir(parents=True)
        notes = backfill_dir / "notes.md"
        notes.write_text("keep me", encoding="utf-8")
        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertTrue(notes.exists())
        self.assertEqual(notes.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(backfill_dir.exists())
        self.assertNotIn(str(backfill_dir), result["deleted"])


# ---------------------------------------------------------------------------
# requested_schemas scope propagation
# ---------------------------------------------------------------------------


class RequestedSchemasScopeTests(TmpDirCase):
    def test_null_requested_schemas_collapses_out_of_result_schema_as_before(self):
        """Baseline (D8): omitted requested_schemas (== null) must keep the
        pre-T4 full-disk convergence behavior — a schema dir absent from the
        collect result is collapsed regardless of what it's named."""
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a")])]))
        schema_dir = self.dbmeta_dir / "tmp"
        self.assertTrue(schema_dir.exists())

        result = self.run_render(
            v1_doc([schema_obj("auth", tables=[make_table("users")])], requested_schemas=None)
        )
        self.assertFalse(schema_dir.exists())
        self.assertIn(str(schema_dir / "tables" / "a.sql"), result["deleted"])
        self.assertIsNone(result["requested_schemas"])
        self.assertEqual(result["out_of_scope_schemas"], [])

    def test_out_of_scope_disk_schema_untouched_by_scoped_run(self):
        """CLI/SCHEMAS-scoped run: a schema dir on disk but outside
        requested_schemas is left alone — not deleted, not rewritten — even
        though it's also absent from this run's (scoped) collect result."""
        self.run_render(v1_doc([schema_obj("logs", tables=[make_table("audit")])]))
        out_of_scope_dir = self.dbmeta_dir / "logs"
        before = (out_of_scope_dir / "tables" / "audit.sql").read_bytes()

        result = self.run_render(
            v1_doc(
                [schema_obj("auth", tables=[make_table("users")])],
                requested_schemas=["auth"],
            )
        )
        self.assertTrue(out_of_scope_dir.exists())
        after = (out_of_scope_dir / "tables" / "audit.sql").read_bytes()
        self.assertEqual(before, after)
        self.assertNotIn(str(out_of_scope_dir / "tables" / "audit.sql"), result["deleted"])
        self.assertEqual(result["requested_schemas"], ["auth"])
        self.assertEqual(result["out_of_scope_schemas"], ["logs"])

    def test_in_scope_disk_schema_dropped_from_result_still_collapses(self):
        """A schema WITHIN requested_schemas but absent from this run's collect
        result (e.g. genuinely DROPed) still collapses — scope narrows what
        render is even allowed to touch, it doesn't grant survivorship to a
        schema render was told to look at."""
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("old_table")])]))
        stale_dir = self.dbmeta_dir / "auth"

        result = self.run_render(
            v1_doc(
                [schema_obj("logs", tables=[make_table("audit")])],
                requested_schemas=["auth", "logs"],
            )
        )
        self.assertFalse(stale_dir.exists())
        self.assertIn(str(stale_dir / "tables" / "old_table.sql"), result["deleted"])
        self.assertEqual(result["out_of_scope_schemas"], [])

    def test_repeated_scoped_runs_are_idempotent_and_leave_out_of_scope_dir_untouched(self):
        self.run_render(v1_doc([schema_obj("logs", tables=[make_table("audit")])]))
        doc = v1_doc(
            [schema_obj("auth", tables=[make_table("users")])], requested_schemas=["auth"]
        )
        self.run_render(doc)
        before = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        result = self.run_render(doc)
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["written"], [])
        self.assertEqual(result["deleted"], [])

    def test_requested_schemas_not_null_or_list_rejected_zero_write(self):
        doc = v1_doc([schema_obj("auth", tables=[make_table("users")])], requested_schemas="auth")
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_requested_schemas_duplicate_entry_rejected_zero_write(self):
        doc = v1_doc(
            [schema_obj("auth", tables=[make_table("users")])],
            requested_schemas=["auth", "auth"],
        )
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_requested_schemas_invalid_identifier_rejected_zero_write(self):
        doc = v1_doc(
            [schema_obj("auth", tables=[make_table("users")])],
            requested_schemas=["bad--name"],
        )
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_schemas_not_subset_of_requested_schemas_rejected_zero_write(self):
        """schemas[].schema MUST be ⊆ requested_schemas — a producer/hand-edit
        bug that violates this is rejected before any write, not silently
        rendered as if the scope claim were true."""
        doc = v1_doc(
            [schema_obj("auth", tables=[make_table("users")])],
            requested_schemas=["logs"],
        )
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("requested_schemas", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_existing_dbmeta_dir_untouched_when_requested_schemas_invalid(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        before = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        doc = v1_doc(
            [schema_obj("auth", tables=[make_table("users")])],
            requested_schemas=["logs"],
        )
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class ScopeDeclarationAggregationFilesTests(TmpDirCase):
    def test_gaps_report_null_scope_byte_identical_to_no_scope_declaration(self):
        result_a = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        content_a = (self.dbmeta_dir / "_gaps.md").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as other:
            other_dbmeta = Path(other) / "dbmeta"
            render.run(
                v1_doc(
                    [schema_obj("auth", tables=[make_table("users")])], requested_schemas=None
                ),
                other_dbmeta,
            )
            content_b = (other_dbmeta / "_gaps.md").read_text(encoding="utf-8")
        self.assertEqual(content_a, content_b)
        self.assertIsNone(result_a["requested_schemas"])

    def test_gaps_report_declares_scope_when_requested_schemas_non_null(self):
        self.run_render(
            v1_doc(
                [schema_obj("auth", tables=[make_table("users")])],
                requested_schemas=["auth", "logs"],
            )
        )
        content = (self.dbmeta_dir / "_gaps.md").read_text(encoding="utf-8")
        self.assertIn("`auth`", content)
        self.assertIn("`logs`", content)

    def test_relations_report_declares_scope_when_requested_schemas_non_null(self):
        self.run_render(
            v1_doc(
                [schema_obj("auth", tables=[make_table("users")])],
                requested_schemas=["auth"],
            )
        )
        content = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.assertIn("`auth`", content)

    def test_relations_report_null_scope_byte_identical_to_no_scope_declaration(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        content_a = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as other:
            other_dbmeta = Path(other) / "dbmeta"
            render.run(
                v1_doc(
                    [schema_obj("auth", tables=[make_table("users")])], requested_schemas=None
                ),
                other_dbmeta,
            )
            content_b = (other_dbmeta / "_relations.md").read_text(encoding="utf-8")
        self.assertEqual(content_a, content_b)


class RootReadmeOutOfScopeTests(TmpDirCase):
    def test_out_of_scope_schema_listed_as_uncovered_with_dash_counts(self):
        self.run_render(v1_doc([schema_obj("logs", tables=[make_table("audit")])]))
        self.run_render(
            v1_doc(
                [schema_obj("auth", tables=[make_table("users")])],
                requested_schemas=["auth"],
            )
        )
        readme = (self.dbmeta_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("logs（未覆盖）", readme)
        # the out-of-scope row's four count columns are all "—"
        self.assertRegex(readme, r"\|\s*logs（未覆盖）\s*\|\s*—\s*\|\s*—\s*\|\s*—\s*\|\s*—\s*\|")

    def test_null_scope_root_readme_byte_identical_to_no_scope_param(self):
        result_a = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        content_a = (self.dbmeta_dir / "README.md").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as other:
            other_dbmeta = Path(other) / "dbmeta"
            render.run(
                v1_doc(
                    [schema_obj("auth", tables=[make_table("users")])], requested_schemas=None
                ),
                other_dbmeta,
            )
            content_b = (other_dbmeta / "README.md").read_text(encoding="utf-8")
        self.assertEqual(content_a, content_b)
        self.assertEqual(result_a["out_of_scope_schemas"], [])


# ---------------------------------------------------------------------------
# D-M identifier fail-loud
# ---------------------------------------------------------------------------


class IdentifierFailLoudTests(TmpDirCase):
    def test_bad_function_name_rejected_before_any_write(self):
        doc = v1_doc(
            [schema_obj("auth", tables=[make_table("users")], functions=[{"name": "bad--name"}])]
        )
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("bad--name", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_bad_table_name_rejected(self):
        doc = v1_doc([schema_obj("auth", tables=[make_table("weird name!")])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_bad_view_name_rejected(self):
        doc = v1_doc([schema_obj("auth", views=[{"name": "v--x"}])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_bad_schema_name_rejected(self):
        # T34: an illegal SCHEMA name (not just an object name) is fail-loud too.
        doc = v1_doc([schema_obj("bad--schema", tables=[make_table("users")])])
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("bad--schema", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_bad_folded_partition_child_name_rejected(self):
        # T42: a child folded into its parent's block never becomes a file name,
        # but D-M's "every table name" contract still covers it.
        root = make_table("root")
        child = make_table("weird child!", partition_of="root")
        doc = v1_doc([schema_obj("logs", tables=[root, child])])
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("weird child!", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

    # relation-ddl-equivalence T2.3/2.8⑥: a child's `partition_of` value itself
    # must pass the D-M identifier shape — it lands as raw text in the
    # parent's ORPHAN_PARTITION_NOTE comment (when the parent isn't in this
    # schema) or the folded-child comment lines.
    def test_partition_of_with_embedded_newline_rejected(self):
        child = make_table("child", partition_of="p\nq")
        doc = v1_doc([schema_obj("logs", tables=[child])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_partition_of_with_dash_rejected(self):
        child = make_table("child", partition_of="p-q")
        doc = v1_doc([schema_obj("logs", tables=[child])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_case_only_name_collision_rejected(self):
        # T43: Users vs users share one file on a case-insensitive filesystem —
        # rejected on every host so the outcome doesn't depend on the filesystem.
        doc = v1_doc([schema_obj("auth", tables=[make_table("Users"), make_table("users")])])
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("仅大小写不同", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

        fn_doc = v1_doc(
            [schema_obj("auth", functions=[make_function("fn_a"), make_function("FN_A")])]
        )
        with self.assertRaises(render.CollectFormatError):
            self.run_render(fn_doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_existing_dbmeta_dir_untouched_when_later_schema_is_invalid(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        before = {
            p: p.read_bytes()
            for p in self.dbmeta_dir.rglob("*")
            if p.is_file()
        }
        doc = v1_doc(
            [
                schema_obj("auth", tables=[make_table("users")]),
                schema_obj("bad", tables=[make_table("weird!")]),
            ]
        )
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# [impl-review-fix] F-D: path-escape via pure-dot schema/object names
# ---------------------------------------------------------------------------
#
# IDENTIFIER_RE (`^[A-Za-z0-9_.]+$`) allows "." as a character (needed for
# legitimate dotted names, see DottedTableNameTests below) — which also matched
# "." and ".." verbatim before this fix. A schema named ".." turns straight into
# `dbmeta_dir / ".." / "tables"`, a path that resolves OUTSIDE .dbmeta/ entirely;
# reproduced pre-fix as an actual write clobbering a file one directory above
# .dbmeta/. MUST fail loud before any file is written, anywhere — not just inside
# .dbmeta/.


class PathEscapeIdentifierTests(TmpDirCase):
    def test_dotdot_schema_name_rejected_before_any_write(self):
        sentinel = self.dbmeta_dir.parent / "sentinel.txt"
        sentinel.write_text("do not touch", encoding="utf-8")

        doc = v1_doc([schema_obj("..", tables=[make_table("users")])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)

        self.assertFalse(self.dbmeta_dir.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not touch")

    def test_single_dot_schema_name_rejected_before_any_write(self):
        doc = v1_doc([schema_obj(".", tables=[make_table("users")])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_dotdot_table_name_rejected_before_any_write(self):
        doc = v1_doc([schema_obj("auth", tables=[make_table("..")])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_dotdotdot_schema_name_also_rejected(self):
        """Any non-empty run of ONLY dots, not just the two canonical forms."""
        doc = v1_doc([schema_obj("...", tables=[make_table("users")])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertFalse(self.dbmeta_dir.exists())


class AssertInsidePathContainmentTests(unittest.TestCase):
    """Direct unit coverage of the belt-and-suspenders `_assert_inside` helper
    itself (called from validate_identifiers before any write/delete), independent
    of whether an identifier rule change might someday let something past
    `_validate_identifier`'s pure-dot rejection."""

    def test_path_outside_dbmeta_dir_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbmeta_dir = Path(tmp) / "dbmeta"
            dbmeta_dir.mkdir()
            outside = Path(tmp) / "outside"
            with self.assertRaises(render.CollectFormatError):
                render._assert_inside(dbmeta_dir, outside)

    def test_path_inside_dbmeta_dir_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbmeta_dir = Path(tmp) / "dbmeta"
            dbmeta_dir.mkdir()
            inside = dbmeta_dir / "schema_a"
            render._assert_inside(dbmeta_dir, inside)  # must not raise


# ---------------------------------------------------------------------------
# [impl-review-fix] F-E: .dbmeta/ entries that are symlinks MUST fail loud, never
# be followed for deletion
# ---------------------------------------------------------------------------
#
# The D-L collapse loop's `entry.is_dir()` (and, one level down, `obj_dir.exists()`
# in _sync_object_dir / `kind_dir.exists()` in _collapse_schema_dir) all follow
# symlinks transparently. If .dbmeta/<x> — or a schema's tables/views/functions
# subdirectory — is actually a symlink to a directory outside .dbmeta/, the old
# code would walk into and DELETE the link target's real files, then crash on the
# trailing rmdir() with NotADirectoryError once it tried to remove what it still
# thought was a plain empty directory. Reproduced pre-fix. Every path below MUST
# fail loud (CollectFormatError) before touching the symlink target at all.


class SymlinkGuardTests(TmpDirCase):
    def test_symlink_directly_under_dbmeta_rejected_before_any_deletion(self):
        self.dbmeta_dir.mkdir(parents=True)
        target_dir = Path(self._tmp.name) / "link_target"
        target_dir.mkdir()
        secret = target_dir / "secret.md"
        secret.write_text("do not touch", encoding="utf-8")
        (self.dbmeta_dir / "evil").symlink_to(target_dir, target_is_directory=True)

        doc = v1_doc([schema_obj("auth", tables=[make_table("users")])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)

        self.assertTrue(secret.exists())
        self.assertEqual(secret.read_text(encoding="utf-8"), "do not touch")
        self.assertFalse((self.dbmeta_dir / "auth").exists())

    def test_symlinked_object_dir_rejected_for_a_schema_still_present(self):
        """obj_dir (tables/) itself a symlink, for a schema that IS in this run's
        collect result — the _sync_object_dir path, not D-L collapse."""
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        tables_dir = self.dbmeta_dir / "auth" / "tables"
        target_dir = Path(self._tmp.name) / "link_target2"
        target_dir.mkdir()
        secret = target_dir / "secret.md"
        secret.write_text("do not touch", encoding="utf-8")
        for p in tables_dir.glob("*.sql"):
            p.unlink()
        tables_dir.rmdir()
        tables_dir.symlink_to(target_dir, target_is_directory=True)

        doc = v1_doc([schema_obj("auth", tables=[make_table("users")])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)
        self.assertTrue(secret.exists())

    def test_symlinked_kind_dir_rejected_before_any_deletion_on_schema_collapse(self):
        """kind_dir a symlink inside a schema that's about to be D-L-collapsed —
        pins the pre-scan-before-any-deletion ordering: _collect.json (deleted
        earlier in _collapse_schema_dir than the old in-loop symlink check) must
        also survive once the symlink is found."""
        self.run_render(v1_doc([schema_obj("tmp", tables=[make_table("a")])]))
        schema_dir = self.dbmeta_dir / "tmp"
        tables_dir = schema_dir / "tables"
        target_dir = Path(self._tmp.name) / "link_target3"
        target_dir.mkdir()
        secret = target_dir / "secret.md"
        secret.write_text("do not touch", encoding="utf-8")
        for p in tables_dir.glob("*.sql"):
            p.unlink()
        tables_dir.rmdir()
        tables_dir.symlink_to(target_dir, target_is_directory=True)

        collect_json = schema_dir / "_collect.json"
        self.assertTrue(collect_json.exists())

        doc = v1_doc([schema_obj("auth", tables=[make_table("users")])])
        with self.assertRaises(render.CollectFormatError):
            self.run_render(doc)

        self.assertTrue(secret.exists())
        self.assertTrue(collect_json.exists())


# ---------------------------------------------------------------------------
# dotted table name
# ---------------------------------------------------------------------------


class DottedTableNameTests(TmpDirCase):
    def test_dotted_table_name_round_trips_and_stays_idempotent(self):
        doc = v1_doc(
            [schema_obj("public", tables=[make_table("example.schema_migrations"), make_table("schema_migrations")])]
        )
        self.run_render(doc)
        path = self.dbmeta_dir / "public" / "tables" / "example.schema_migrations.sql"
        self.assertTrue(path.exists())
        first = path.read_text(encoding="utf-8")
        self.assertEqual(first.count("-- pg-dict:table:example.schema_migrations:start"), 1)

        self.run_render(doc)
        second = path.read_text(encoding="utf-8")
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# carried-over: v1 top-level parsing
# ---------------------------------------------------------------------------


class TopLevelParsingTests(unittest.TestCase):
    def test_v1_object_parses_and_yields_schemas(self):
        metadata = render.parse_collect_document(
            v1_doc([schema_obj("auth", tables=[make_table("users")])])
        )
        self.assertEqual(metadata["collect_version"], 1)
        self.assertEqual([s["schema"] for s in metadata["schemas"]], ["auth"])

    def test_bare_array_top_level_is_rejected(self):
        legacy_array = [{"schema": "auth", "tables": []}]
        with self.assertRaises(render.CollectFormatError) as ctx:
            render.parse_collect_document(json.dumps(legacy_array))
        self.assertIn("collect_version==1", ctx.exception.problem)

    def test_wrong_collect_version_is_rejected(self):
        bad = {"collect_version": 2, "schemas": []}
        with self.assertRaises(render.CollectFormatError) as ctx:
            render.parse_collect_document(json.dumps(bad))
        self.assertIn("collect_version=2", ctx.exception.cause)

    def test_second_schema_malformed_writes_nothing_not_even_the_first(self):
        doc = {
            "collect_version": 1,
            "schemas": [
                {"schema": "auth", "tables": [make_table("users")], "functions": []},
                {"tables": []},  # missing required 'schema' key
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            dbmeta_dir = Path(tmp) / "dbmeta"
            with self.assertRaises(render.CollectFormatError) as ctx:
                render.run(json.dumps(doc), dbmeta_dir)
            self.assertIn("schema", ctx.exception.cause)
            self.assertFalse(dbmeta_dir.exists())


# ---------------------------------------------------------------------------
# carried-over: partition-fold structure / father-not-in-schema fallback
# ---------------------------------------------------------------------------


class PartitionFoldStructureTests(TmpDirCase):
    def test_partition_children_do_not_get_their_own_file(self):
        parent = make_table("audit", comment="按月分区的审计日志父表")
        children = [make_table(f"audit_2026_{m:02d}", partition_of="audit") for m in range(1, 14)]
        self.run_render(v1_doc([schema_obj("logs", tables=[parent] + children)]))

        tables_dir = self.dbmeta_dir / "logs" / "tables"
        self.assertTrue((tables_dir / "audit.sql").exists())
        for c in children:
            self.assertFalse((tables_dir / f"{c['name']}.sql").exists())

    def test_old_json_without_partition_of_key_treated_as_ordinary_table(self):
        # make_table() omits the "partition_of" key entirely by default, mirroring
        # pre-v1 fixtures / non-partitioned tables — must still render normally.
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertTrue((self.dbmeta_dir / "auth" / "tables" / "users.sql").exists())

    # relation-ddl-equivalence T2.2/2.8⑥: a folded child's own-index
    # `definition` containing an embedded newline lands as TWO `-- `-prefixed
    # comment lines in the parent's file (via `_comment_lines`) rather than
    # corrupting the comment (the newline never escapes into live SQL), and
    # every other line in the file is still either a `--` comment or part of
    # an executable statement.
    def test_child_index_definition_with_newline_stays_two_comment_lines(self):
        child = make_full_table(
            "audit_2026_01",
            partition_of="audit",
            indexes=[
                {
                    "name": "a",
                    "definition": 'CREATE INDEX "a\nb" ON logs.audit_2026_01 USING btree (id)',
                }
            ],
        )
        parent = make_table("audit")
        self.run_render(v1_doc([schema_obj("logs", tables=[parent, child])]))
        text = (self.dbmeta_dir / "logs" / "tables" / "audit.sql").read_text(encoding="utf-8")
        self.assertIn('--   自有索引 `a`: `CREATE INDEX "a', text)
        self.assertIn('-- b" ON logs.audit_2026_01 USING btree (id)`', text)
        for line in text.splitlines():
            if not line.strip():
                continue
            self.assertTrue(
                line.startswith("--") or line.startswith(("CREATE", "ALTER", "COMMENT", ")", "  ")),
                f"line is neither a comment nor part of executable SQL: {line!r}",
            )


class PartitionOrphanChildTests(TmpDirCase):
    """A child whose `partition_of` name is not itself a top-level table in this
    schema (cross-schema parent, or an intermediate partition level folded into
    ITS OWN parent) must not be silently dropped — group_children()/
    resolve_orphan_children() promote it back to a standalone file."""

    def test_cross_schema_partition_parent_child_gets_own_file_and_gap(self):
        child = make_table("orphan_child", partition_of="other_parent")
        self.run_render(v1_doc([schema_obj("logs", tables=[child])]))
        child_path = self.dbmeta_dir / "logs" / "tables" / "orphan_child.sql"
        self.assertTrue(child_path.exists())
        # T38: the fallback note itself is rendered in the body (not just implied
        # by the file's existence).
        expected_note = render.ORPHAN_PARTITION_NOTE.format(parent="other_parent")
        self.assertIn(f"-- {expected_note}", child_path.read_text(encoding="utf-8"))

        gaps = {"tables": [], "columns": [], "functions": [], "sensitive": []}
        render.collect_gaps("logs", schema_obj("logs", tables=[child]), gaps)
        self.assertIn("logs.orphan_child", gaps["tables"])

    def test_three_level_partition_mid_folds_leaf_gets_own_file(self):
        root = make_table("root", comment="根分区父表")
        mid = make_table("mid", partition_of="root")
        leaf = make_table("leaf", partition_of="mid")
        self.run_render(v1_doc([schema_obj("logs", tables=[root, mid, leaf])]))

        tables_dir = self.dbmeta_dir / "logs" / "tables"
        self.assertTrue((tables_dir / "root.sql").exists())
        self.assertFalse((tables_dir / "mid.sql").exists())
        self.assertTrue((tables_dir / "leaf.sql").exists())


# ---------------------------------------------------------------------------
# carried-over: gaps idempotency / sensitive / overloaded-function
# ---------------------------------------------------------------------------


class GapsReportTests(unittest.TestCase):
    def _schema_obj(self, tables=None, functions=None):
        return schema_obj("auth", tables=tables or [], functions=functions or [])

    def test_missing_table_and_column_comments_listed(self):
        gaps = {"tables": [], "columns": [], "functions": [], "sensitive": []}
        table = make_table(
            "apps",
            columns=[
                {"name": "id", "type": "bigint", "default": None, "nullable": False, "comment": None},
                {"name": "name", "type": "text", "default": None, "nullable": False, "comment": ""},
                {"name": "enabled", "type": "integer", "default": "1", "nullable": False, "comment": None},
            ],
            comment=None,
        )
        render.collect_gaps("auth", self._schema_obj([table]), gaps)
        self.assertIn("auth.apps", gaps["tables"])
        self.assertIn("auth.apps.id", gaps["columns"])
        self.assertIn("auth.apps.name", gaps["columns"])
        self.assertIn("auth.apps.enabled", gaps["columns"])

    def test_partition_child_table_not_counted(self):
        gaps = {"tables": [], "columns": [], "functions": [], "sensitive": []}
        parent = make_table("audit", comment="父表")
        child = make_table("audit_2026_09", partition_of="audit")
        render.collect_gaps("logs", self._schema_obj([parent, child]), gaps)
        self.assertFalse(any("audit_2026_09" in e for e in gaps["tables"] + gaps["columns"]))

    def test_sensitive_column_with_encrypted_comment_not_flagged(self):
        gaps = {"tables": [], "columns": [], "functions": [], "sensitive": []}
        table = make_table(
            "apps",
            columns=[
                {"name": "app_secret", "type": "text", "default": None, "nullable": False, "comment": "应用密钥（AES-GCM 密文）"},
            ],
            comment="应用表",
        )
        render.collect_gaps("auth", self._schema_obj([table]), gaps)
        self.assertNotIn("auth.apps.app_secret", gaps["sensitive"])

    def test_sensitive_column_without_comment_is_flagged(self):
        gaps = {"tables": [], "columns": [], "functions": [], "sensitive": []}
        table = make_table(
            "users",
            columns=[
                {"name": "mobile", "type": "text", "default": None, "nullable": False, "comment": None},
            ],
            comment="用户表",
        )
        render.collect_gaps("auth", self._schema_obj([table]), gaps)
        self.assertIn("auth.users.mobile", gaps["sensitive"])

    def test_overloaded_function_gap_keeps_signature(self):
        gaps = {"tables": [], "columns": [], "functions": [], "sensitive": []}
        render.collect_gaps(
            "auth",
            self._schema_obj(
                functions=[
                    {"name": "fn_over", "identity_args": "", "comment": "无参版本，已注释"},
                    {"name": "fn_over", "identity_args": "p_id bigint", "comment": None},
                ]
            ),
            gaps,
        )
        self.assertEqual(gaps["functions"], ["auth.fn_over(p_id bigint)"])

    def test_report_has_no_run_varying_fields_and_is_idempotent(self):
        gaps = {"tables": ["auth.apps"], "columns": ["auth.apps.id"], "functions": [], "sensitive": ["auth.users.mobile"]}
        first = render.render_gaps_report(gaps)
        second = render.render_gaps_report(gaps)
        self.assertEqual(first, second)
        self.assertNotIn("collected_at", first)
        self.assertNotIn("reltuples", first)

    def test_report_sections_present(self):
        gaps = {"tables": ["auth.apps"], "columns": ["auth.apps.id"], "functions": ["auth.fn_x"], "sensitive": ["auth.users.mobile"]}
        out = render.render_gaps_report(gaps)
        self.assertIn("## 缺表注释", out)
        self.assertIn("## 缺列注释", out)
        self.assertIn("## 缺函数注释", out)
        self.assertIn("## 敏感列名 warning", out)
        self.assertIn("`auth.apps`", out)
        self.assertIn("`auth.apps.id`", out)
        self.assertIn("`auth.fn_x`", out)
        self.assertIn("`auth.users.mobile`", out)


class GapsReportEndToEndIdempotencyTests(TmpDirCase):
    def test_gaps_md_byte_identical_on_second_run_with_unchanged_schema(self):
        doc = v1_doc([schema_obj("auth", tables=[make_table("apps", comment=None)])])
        self.run_render(doc)
        gaps_path = self.dbmeta_dir / "_gaps.md"
        first = gaps_path.read_text(encoding="utf-8")
        self.run_render(doc)
        second = gaps_path.read_text(encoding="utf-8")
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# Task 2: full table body — columns/indexes/constraints/triggers/reltuples/
# partition-fold detail text
# ---------------------------------------------------------------------------


class TableFullBodyTests(TmpDirCase):
    """Task 4: run()'s end-to-end table object-file path now goes through
    render_table_ddl/SQL_SYNTAX (task3-ddl-renderers already covers
    render_table_ddl's OWN unit-level behavior exhaustively — these tests pin
    that the run()->_render_schema_tables->_sync_object_dir wiring actually
    reaches it, on a real `.sql` file on disk)."""

    def test_constraint_trigger_contype_rendered_verbatim(self):
        # T37 (superseded by the DDL cut-over): DDL has no human contype label —
        # every contype (including 't', constraint-trigger) becomes a plain
        # `ADD CONSTRAINT <name> <definition>;` using the definition verbatim.
        table = make_full_table(
            "t1",
            constraints=[{"name": "ct_x", "type": "t", "definition": "TRIGGER ct_x", "is_local": True}],
        )
        self.run_render(v1_doc([schema_obj("auth", tables=[table])]))
        content = (self.dbmeta_dir / "auth" / "tables" / "t1.sql").read_text(encoding="utf-8")
        self.assertIn("ADD CONSTRAINT ct_x TRIGGER ct_x;", content)

    def test_reltuples_note_for_ordinary_table(self):
        self.run_render(
            v1_doc([schema_obj("auth", tables=[make_full_table("users", reltuples=42)])])
        )
        content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertIn("新建表在 ANALYZE 前恒为 0，不代表真实为空", content)
        self.assertIn("按数量级分档", content)
        self.assertIn("）：<100", content)
        self.assertNotIn("父表自身不持有数据行", content)

    # T32: only the order of magnitude is rendered, so ANALYZE drift inside one
    # bucket leaves table .sql and schema README byte-identical; crossing a
    # bucket boundary (a real change of scale) is the only thing that diffs.
    def test_reltuples_drift_within_bucket_is_byte_identical(self):
        def snapshot():
            return {
                p: p.read_bytes()
                for p in (self.dbmeta_dir / "auth").rglob("*")
                if p.is_file() and p.suffix in (".sql", ".md")
            }
        self.run_render(v1_doc([schema_obj("auth", tables=[make_full_table("users", reltuples=1_200)])]))
        before = snapshot()
        result = self.run_render(
            v1_doc([schema_obj("auth", tables=[make_full_table("users", reltuples=9_800)])])
        )
        self.assertEqual(before, snapshot())
        self.assertEqual(result["written"], [])
        self.assertIn("）：千级", before[self.dbmeta_dir / "auth" / "tables" / "users.sql"].decode())

    def test_reltuples_crossing_bucket_boundary_rewrites_table_and_readme(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_full_table("users", reltuples=9_800)])]))
        result = self.run_render(
            v1_doc([schema_obj("auth", tables=[make_full_table("users", reltuples=12_000)])])
        )
        self.assertIn(str(self.dbmeta_dir / "auth" / "tables" / "users.sql"), result["written"])
        self.assertIn(str(self.dbmeta_dir / "auth" / "README.md"), result["written"])
        readme = (self.dbmeta_dir / "auth" / "README.md").read_text(encoding="utf-8")
        self.assertIn("| 万级 |", readme)

    def test_reltuples_note_for_partitioned_parent_adds_extra_clause(self):
        self.run_render(
            v1_doc(
                [
                    schema_obj(
                        "logs",
                        tables=[make_full_table("audit", kind="partitioned_table", reltuples=0)],
                    )
                ]
            )
        )
        content = (self.dbmeta_dir / "logs" / "tables" / "audit.sql").read_text(encoding="utf-8")
        self.assertIn("父表自身不持有数据行，不代表分区总行数", content)

    def test_indexes_rendered(self):
        idx = [{"name": "users_pkey", "definition": "CREATE UNIQUE INDEX users_pkey ON auth.users USING btree (id)"}]
        self.run_render(v1_doc([schema_obj("auth", tables=[make_full_table("users", indexes=idx)])]))
        content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertIn("users_pkey", content)
        self.assertIn("CREATE UNIQUE INDEX users_pkey", content)

    def test_no_indexes_renders_no_create_index_statement(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_full_table("users")])]))
        content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertNotIn("CREATE INDEX", content)
        self.assertNotIn("CREATE UNIQUE INDEX", content)

    def test_constraints_rendered_verbatim_excluding_not_null(self):
        cons = [
            {"name": "users_pkey", "type": "p", "definition": "PRIMARY KEY (id)", "is_local": True},
            {"name": "users_email_key", "type": "u", "definition": "UNIQUE (email)", "is_local": True},
        ]
        self.run_render(v1_doc([schema_obj("auth", tables=[make_full_table("users", constraints=cons)])]))
        content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertIn("ADD CONSTRAINT users_pkey PRIMARY KEY (id);", content)
        self.assertIn("ADD CONSTRAINT users_email_key UNIQUE (email);", content)

    def test_triggers_rendered_with_definition_and_no_extra_alter_when_enabled(self):
        trg = [make_trigger("trg_audit", "fn_audit", enabled="O")]
        self.run_render(v1_doc([schema_obj("auth", tables=[make_full_table("users", triggers=trg)])]))
        content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertIn("trg_audit", content)
        self.assertIn(trg[0]["definition"] + ";", content)
        self.assertNotIn("DISABLE TRIGGER", content)
        self.assertNotIn("ENABLE REPLICA TRIGGER", content)
        self.assertNotIn("ENABLE ALWAYS TRIGGER", content)

    def test_partition_fold_detail_text_rendered(self):
        parent = make_full_table("audit", comment="审计日志父表")
        children = [
            make_full_table(f"audit_2026_{m:02d}", partition_of="audit") for m in range(1, 4)
        ]
        self.run_render(v1_doc([schema_obj("logs", tables=[parent] + children)]))
        content = (self.dbmeta_dir / "logs" / "tables" / "audit.sql").read_text(encoding="utf-8")
        self.assertIn("分区子表（3）", content)
        self.assertIn("audit_2026_01", content)

    def test_partition_child_own_index_listed_under_child(self):
        parent = make_full_table("audit", comment="父表")
        own_idx = [{"name": "audit_2026_01_extra_idx", "definition": "CREATE INDEX audit_2026_01_extra_idx ON logs.audit_2026_01 (event_type)"}]
        child = make_full_table("audit_2026_01", partition_of="audit", indexes=own_idx)
        self.run_render(v1_doc([schema_obj("logs", tables=[parent, child])]))
        content = (self.dbmeta_dir / "logs" / "tables" / "audit.sql").read_text(encoding="utf-8")
        self.assertIn("audit_2026_01_extra_idx", content)
        self.assertIn("自有索引", content)

    def test_partition_child_inherited_index_not_listed_under_child(self):
        parent = make_full_table("audit", comment="父表")
        inherited_idx = [{"name": "audit_2026_01_pkey", "definition": "...", "inherited_from": "audit_pkey"}]
        child = make_full_table("audit_2026_01", partition_of="audit", indexes=inherited_idx)
        self.run_render(v1_doc([schema_obj("logs", tables=[parent, child])]))
        content = (self.dbmeta_dir / "logs" / "tables" / "audit.sql").read_text(encoding="utf-8")
        self.assertNotIn("自有索引", content)


# ---------------------------------------------------------------------------
# Task 2: view file rendering
# ---------------------------------------------------------------------------


class ViewRenderTests(TmpDirCase):
    def test_view_file_has_exactly_one_managed_block(self):
        self.run_render(v1_doc([schema_obj("auth", views=[make_view("v_active_users")])]))
        path = self.dbmeta_dir / "auth" / "views" / "v_active_users.sql"
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertEqual(content.count("-- pg-dict:view:v_active_users:start"), 1)

    def test_view_definition_and_columns_and_comment_rendered(self):
        view = make_view(
            "v_active_users",
            comment="活跃用户视图",
            definition="SELECT id, nickname FROM auth.users WHERE enabled = 1",
            columns=[
                {"position": 1, "name": "id", "type": "bigint", "nullable": False, "comment": None},
                {"position": 2, "name": "nickname", "type": "text", "nullable": True, "comment": "昵称"},
            ],
        )
        self.run_render(v1_doc([schema_obj("auth", views=[view])]))
        content = (self.dbmeta_dir / "auth" / "views" / "v_active_users.sql").read_text(encoding="utf-8")
        self.assertIn("活跃用户视图", content)
        self.assertIn("SELECT id, nickname FROM auth.users WHERE enabled = 1", content)
        self.assertIn("nickname", content)
        self.assertIn("昵称", content)

    def test_view_removed_deletes_pure_generated_file(self):
        self.run_render(v1_doc([schema_obj("auth", views=[make_view("v_tmp")])]))
        path = self.dbmeta_dir / "auth" / "views" / "v_tmp.sql"
        self.assertTrue(path.exists())
        result = self.run_render(v1_doc([schema_obj("auth", views=[])]))
        self.assertFalse(path.exists())
        self.assertIn(str(path), result["deleted"])


# ---------------------------------------------------------------------------
# Task 2: function file rendering — per-overload blocks, source fencing,
# trigger reverse-index
# ---------------------------------------------------------------------------


class FunctionRenderTests(TmpDirCase):
    """Task 4: run()'s function object-file path now goes through
    render_function_ddl/SQL_SYNTAX. (The old markdown-only "code fence widens
    for backtick-heavy source" concern — CommonMark fence sizing — has no
    executable-SQL counterpart and is dropped rather than migrated: `definition`
    is emitted verbatim regardless of its content, already pinned by
    FunctionDdlRenderTests.test_definition_verbatim_plus_semicolon.)"""

    def test_two_overloads_produce_exactly_two_blocks(self):
        funcs = [
            make_function("fn_over", identity_args=""),
            make_function("fn_over", identity_args="p_id bigint"),
        ]
        self.run_render(v1_doc([schema_obj("auth", functions=funcs)]))
        path = self.dbmeta_dir / "auth" / "functions" / "fn_over.sql"
        content = path.read_text(encoding="utf-8")
        # NB: the fixed file header also mentions the literal placeholder text
        # "pg-dict:<ident>:start/end" (DD-7) — counting bare ":start"/":end"
        # would double-count it, so match on the actual marker's trailing "\n"
        # (the placeholder is followed by "/end", never a newline, right there).
        self.assertEqual(content.count(":start\n"), 2)
        self.assertEqual(content.count(":end\n"), 2)
        self.assertIn("-- pg-dict:fn:fn_over():start", content)
        self.assertIn("-- pg-dict:fn:fn_over(p_id bigint):start", content)

    def test_removing_one_overload_removes_only_that_block(self):
        funcs = [
            make_function("fn_over", identity_args=""),
            make_function("fn_over", identity_args="p_id bigint"),
        ]
        self.run_render(v1_doc([schema_obj("auth", functions=funcs)]))
        path = self.dbmeta_dir / "auth" / "functions" / "fn_over.sql"

        remaining = [make_function("fn_over", identity_args="")]
        self.run_render(v1_doc([schema_obj("auth", functions=remaining)]))
        content = path.read_text(encoding="utf-8")
        self.assertIn("-- pg-dict:fn:fn_over():start", content)
        self.assertNotIn("-- pg-dict:fn:fn_over(p_id bigint):start", content)
        # file itself must still exist (one overload remains)
        self.assertTrue(path.exists())

    def test_new_overload_inserted_in_sorted_position_not_appended(self):
        # T36: an overload added later lands where a fresh render would put it
        # (identity_args order), so incremental and fresh renders agree.
        self.run_render(
            v1_doc([schema_obj("auth", functions=[make_function("fn_over", identity_args="p_z text")])])
        )
        path = self.dbmeta_dir / "auth" / "functions" / "fn_over.sql"
        path.write_text(path.read_text(encoding="utf-8") + "\n-- 尾注\n", encoding="utf-8")
        funcs = [
            make_function("fn_over", identity_args="p_z text"),
            make_function("fn_over", identity_args="p_a bigint"),
        ]
        self.run_render(v1_doc([schema_obj("auth", functions=funcs)]))
        content = path.read_text(encoding="utf-8")
        a = content.index("-- pg-dict:fn:fn_over(p_a bigint):start")
        z = content.index("-- pg-dict:fn:fn_over(p_z text):start")
        self.assertLess(a, z)
        self.assertTrue(content.endswith("-- 尾注\n"))
        # matches a fresh render byte-for-byte apart from the hand-written tail
        fresh_dir = Path(self._tmp.name) / "fresh"
        render.run(v1_doc([schema_obj("auth", functions=funcs)]), fresh_dir)
        fresh = (fresh_dir / "auth" / "functions" / "fn_over.sql").read_text(encoding="utf-8")
        self.assertEqual(content, fresh + "\n-- 尾注\n")

    def test_removing_all_overloads_deletes_pure_generated_file(self):
        self.run_render(v1_doc([schema_obj("auth", functions=[make_function("fn_tmp")])]))
        path = self.dbmeta_dir / "auth" / "functions" / "fn_tmp.sql"
        self.assertTrue(path.exists())
        result = self.run_render(v1_doc([schema_obj("auth", functions=[])]))
        self.assertFalse(path.exists())
        self.assertIn(str(path), result["deleted"])

    def test_function_signature_return_type_language_comment_rendered(self):
        self.run_render(
            v1_doc(
                [
                    schema_obj(
                        "auth",
                        functions=[
                            make_function(
                                "fn_hello",
                                identity_args="p_id bigint",
                                result_type="text",
                                language="plpgsql",
                                comment="返回问候语",
                            )
                        ],
                    )
                ]
            )
        )
        content = (self.dbmeta_dir / "auth" / "functions" / "fn_hello.sql").read_text(encoding="utf-8")
        self.assertIn("fn_hello(p_id bigint)", content)
        self.assertIn("RETURNS text", content)
        self.assertIn("LANGUAGE plpgsql", content)
        self.assertIn("COMMENT ON FUNCTION auth.fn_hello(p_id bigint) IS '返回问候语';", content)


# ---------------------------------------------------------------------------
# Task 2: D-D trigger -> function link + reverse index
# ---------------------------------------------------------------------------


class TriggerFunctionLinkTests(TmpDirCase):
    """Task 4: run()'s table/function object-file path now goes through
    render_table_ddl/render_function_ddl — trigger link resolution itself is
    already exhaustively unit-tested by TableDdlTriggerTests (task3), this
    class pins that run() actually threads `ext=".sql"` through to the file on
    disk (DD-3 rule 5 / task4-brief's "触发器→函数链接指向 .sql")."""

    def test_same_schema_trigger_links_to_function_and_reverse_index_lists_it(self):
        trg = [make_trigger("trg_audit", "fn_audit")]
        users = make_full_table("users", triggers=trg)
        fn = make_function("fn_audit", identity_args="")
        self.run_render(v1_doc([schema_obj("auth", tables=[users], functions=[fn])]))

        table_content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertIn("../functions/fn_audit.sql", table_content)

        fn_content = (self.dbmeta_dir / "auth" / "functions" / "fn_audit.sql").read_text(encoding="utf-8")
        self.assertIn("auth.users.trg_audit", fn_content)
        self.assertNotIn("-- 被以下触发器引用：\n-- （无）", fn_content)

    def test_cross_schema_trigger_link_path(self):
        trg = [make_trigger("trg_audit", "logs.fn_write_audit")]
        users = make_full_table("users", triggers=trg)
        fn = make_function("fn_write_audit", identity_args="")
        self.run_render(
            v1_doc(
                [
                    schema_obj("auth", tables=[users]),
                    schema_obj("logs", functions=[fn]),
                ]
            )
        )
        table_content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertIn("../../logs/functions/fn_write_audit.sql", table_content)

        fn_content = (self.dbmeta_dir / "logs" / "functions" / "fn_write_audit.sql").read_text(encoding="utf-8")
        self.assertIn("auth.users.trg_audit", fn_content)

    def test_extension_function_not_linked_only_name_written(self):
        trg = [make_trigger("trg_ext", "some_extension_fn")]
        users = make_full_table("users", triggers=trg)
        # no function with this name is in the collect set at all
        self.run_render(v1_doc([schema_obj("auth", tables=[users])]))
        content = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertIn("-- 触发器函数：some_extension_fn", content)
        self.assertNotIn("functions/some_extension_fn", content)  # no link path produced

    def test_function_without_trigger_shows_none_in_reverse_index(self):
        fn = make_function("fn_lonely", identity_args="")
        self.run_render(v1_doc([schema_obj("auth", functions=[fn])]))
        content = (self.dbmeta_dir / "auth" / "functions" / "fn_lonely.sql").read_text(encoding="utf-8")
        self.assertIn("-- 被以下触发器引用：\n-- （无）", content)


# ---------------------------------------------------------------------------
# Task 2: D-M function block-ident "--" fail-loud
# ---------------------------------------------------------------------------


class FunctionIdentifierFailLoudTests(TmpDirCase):
    def test_identity_args_containing_double_dash_rejected_before_any_write(self):
        doc = v1_doc(
            [schema_obj("auth", functions=[make_function("fn_x", identity_args="p_x text--bad")])]
        )
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("--", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_identity_args_containing_newline_rejected_before_any_write(self):
        # B6: identity_args is part of the marker line itself; a newline inside
        # it would split `-- pg-dict:fn:<name>(<args>):start` across two lines.
        doc = v1_doc(
            [schema_obj("auth", functions=[make_function("fn_x", identity_args='"a\nb"')])]
        )
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("换行", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())


# ---------------------------------------------------------------------------
# Task 2: second-regen idempotency across the full render surface
# ---------------------------------------------------------------------------


class CollectSliceTests(TmpDirCase):
    def test_collect_json_top_level_keys_and_no_reltuples(self):
        users = make_full_table("users", reltuples=42)
        self.run_render(v1_doc([schema_obj("auth", tables=[users])]))
        path = self.dbmeta_dir / "auth" / "_collect.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {"collect_version", "schema"})
        self.assertEqual(data["collect_version"], 1)
        self.assertEqual(data["schema"]["schema"], "auth")
        self.assertNotIn("collected_at", data)
        self.assertNotIn("database", data)
        table_obj = data["schema"]["tables"][0]
        self.assertNotIn("reltuples", table_obj)
        # DDL rendering still uses reltuples (only the slice strips it) — as a bucket
        sql = (self.dbmeta_dir / "auth" / "tables" / "users.sql").read_text(encoding="utf-8")
        self.assertIn("）：<100", sql)

    def test_collect_json_deterministic_serialization(self):
        users = make_full_table("users", reltuples=1)
        doc = v1_doc([schema_obj("auth", tables=[users])])
        self.run_render(doc)
        path = self.dbmeta_dir / "auth" / "_collect.json"
        raw = path.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        self.assertIn("  ", raw)  # indent 2

    def test_collect_json_byte_identical_on_unchanged_second_run(self):
        users = make_full_table("users", reltuples=1)
        doc = v1_doc([schema_obj("auth", tables=[users])])
        self.run_render(doc)
        path = self.dbmeta_dir / "auth" / "_collect.json"
        first = path.read_text(encoding="utf-8")
        # simulate ANALYZE bumping reltuples between runs — sliced doc must stay
        # byte-identical since reltuples is stripped from the slice entirely.
        users2 = make_full_table("users", reltuples=999)
        self.run_render(v1_doc([schema_obj("auth", tables=[users2])]))
        second = path.read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_collect_json_tracked_in_written_and_unchanged(self):
        users = make_full_table("users", reltuples=1)
        doc = v1_doc([schema_obj("auth", tables=[users])])
        result = self.run_render(doc)
        path = self.dbmeta_dir / "auth" / "_collect.json"
        self.assertIn(str(path), result["written"])
        result2 = self.run_render(doc)
        self.assertIn(str(path), result2["unchanged"])


class RelationsReportTests(TmpDirCase):
    def test_logical_relation_grouped_by_target(self):
        org_id = {
            "name": "org_id",
            "type": "bigint",
            "default": None,
            "nullable": True,
            "comment": "所属组织 ID（逻辑关联 auth.orgs.id）",
        }
        users = make_table("users", columns=[org_id])
        self.run_render(v1_doc([schema_obj("auth", tables=[users])]))
        relations_path = self.dbmeta_dir / "_relations.md"
        content = relations_path.read_text(encoding="utf-8")
        self.assertIn("auth.orgs.id", content)
        self.assertIn("auth.users.org_id → auth.orgs.id", content)

    def test_non_conventional_comment_produces_no_relation_entry_and_no_gap(self):
        org_id = {
            "name": "org_id",
            "type": "bigint",
            "default": None,
            "nullable": True,
            "comment": "关联组织表",
        }
        users = make_table("users", columns=[org_id])
        self.run_render(v1_doc([schema_obj("auth", tables=[users])]))
        content = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.assertNotIn("→", content)
        gaps = (self.dbmeta_dir / "_gaps.md").read_text(encoding="utf-8")
        self.assertNotIn("auth.users.org_id", gaps)

    def test_enum_values_table(self):
        status = {
            "name": "status",
            "type": "int",
            "default": None,
            "nullable": True,
            "comment": "状态：0=待处理 1=处理中 2=已关闭",
        }
        orders = make_table("orders", columns=[status])
        self.run_render(v1_doc([schema_obj("auth", tables=[orders])]))
        content = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.assertIn("待处理", content)
        self.assertIn("处理中", content)
        self.assertIn("已关闭", content)

    def test_sensitive_column_section_includes_declared_safe_annotation(self):
        secret = {
            "name": "app_secret",
            "type": "text",
            "default": None,
            "nullable": True,
            "comment": "应用密钥（AES-GCM 密文）",
        }
        mobile = {
            "name": "mobile",
            "type": "text",
            "default": None,
            "nullable": True,
            "comment": None,
        }
        apps = make_table("apps", columns=[secret, mobile])
        self.run_render(v1_doc([schema_obj("auth", tables=[apps])]))
        content = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.assertIn("auth.apps.app_secret", content)
        self.assertIn("已声明脱敏", content)
        self.assertIn("auth.apps.mobile", content)

    def test_relations_md_byte_identical_on_second_run(self):
        org_id = {
            "name": "org_id",
            "type": "bigint",
            "default": None,
            "nullable": True,
            "comment": "所属组织 ID（逻辑关联 auth.orgs.id）",
        }
        doc = v1_doc([schema_obj("auth", tables=[make_table("users", columns=[org_id])])])
        self.run_render(doc)
        first = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.run_render(doc)
        second = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_mermaid_fence_appears_before_text_list_for_annotated_relation(self):
        org_id = {
            "name": "org_id",
            "type": "bigint",
            "default": None,
            "nullable": True,
            "comment": "所属组织 ID（逻辑关联 auth.orgs.id）",
        }
        users = make_table("users", columns=[org_id])
        self.run_render(v1_doc([schema_obj("auth", tables=[users])]))
        content = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        fence_pos = content.index("```mermaid")
        heading_pos = content.index("## 逻辑关联")
        list_pos = content.index("### auth.orgs")
        self.assertLess(heading_pos, fence_pos)
        self.assertLess(fence_pos, list_pos)
        self.assertIn("erDiagram", content)
        self.assertIn('auth_orgs ||--o{ auth_users : "org_id"', content)

    def test_confirmed_relation_included_in_diagram_and_text_list(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        self.dbmeta_dir.mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "- source: auth.orders.user_id\n  target: auth.users.id\n",
            encoding="utf-8",
        )
        self.run_render(v1_doc([schema_obj("auth", tables=[orders, users])]))
        content = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.assertIn('auth_users ||--o{ auth_orders : "user_id"', content)
        self.assertIn("auth.orders.user_id → auth.users.id", content)

    def test_confirmed_ignore_true_excluded_from_diagram_and_text(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        self.dbmeta_dir.mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "- source: auth.orders.user_id\n  target: auth.users.id\n  ignore: true\n",
            encoding="utf-8",
        )
        self.run_render(v1_doc([schema_obj("auth", tables=[orders, users])]))
        content = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.assertNotIn("```mermaid", content)
        self.assertNotIn("auth.orders.user_id", content)
        self.assertIn("（无）", content)

    def test_relations_md_byte_identical_on_second_run_with_confirmed(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        self.dbmeta_dir.mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "- source: auth.orders.user_id\n  target: auth.users.id\n",
            encoding="utf-8",
        )
        doc = v1_doc([schema_obj("auth", tables=[orders, users])])
        self.run_render(doc)
        first = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.run_render(doc)
        second = (self.dbmeta_dir / "_relations.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)


def col(name: str, comment=None) -> dict:
    """Minimal column fixture for relation-inference tests (id/type/nullable
    are irrelevant to the inference functions under test)."""
    return {"name": name, "type": "bigint", "default": None, "nullable": True, "comment": comment}


class ColumnNameCandidateInferenceTests(unittest.TestCase):
    def test_orders_user_id_hits_users_id(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        schemas = [schema_obj("auth", tables=[orders, users])]
        candidates = render.infer_column_name_candidates(schemas)
        self.assertIn(
            {"source": "auth.orders.user_id", "target": "auth.users.id", "signal": "column_name"},
            candidates,
        )

    def test_cross_schema_stem_match(self):
        invoices = make_table("invoices", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        schemas = [
            schema_obj("billing", tables=[invoices]),
            schema_obj("auth", tables=[users]),
        ]
        candidates = render.infer_column_name_candidates(schemas)
        self.assertIn(
            {"source": "billing.invoices.user_id", "target": "auth.users.id", "signal": "column_name"},
            candidates,
        )

    def test_same_name_table_in_multiple_schemas_all_produced(self):
        invoices = make_table("invoices", columns=[col("id"), col("user_id")])
        schemas = [
            schema_obj("billing", tables=[invoices]),
            schema_obj("auth", tables=[make_table("users", columns=[col("id")])]),
            schema_obj("public", tables=[make_table("users", columns=[col("id")])]),
        ]
        candidates = render.infer_column_name_candidates(schemas)
        targets = {c["target"] for c in candidates if c["source"] == "billing.invoices.user_id"}
        self.assertEqual(targets, {"auth.users.id", "public.users.id"})

    def test_compound_stem_without_matching_table_produces_nothing(self):
        orders = make_table("orders", columns=[col("id"), col("created_by_user_id")])
        schemas = [schema_obj("auth", tables=[orders])]
        candidates = render.infer_column_name_candidates(schemas)
        self.assertEqual(candidates, [])

    def test_no_matching_table_produces_nothing(self):
        orders = make_table("orders", columns=[col("id"), col("widget_id")])
        schemas = [schema_obj("auth", tables=[orders])]
        self.assertEqual(render.infer_column_name_candidates(schemas), [])

    def test_self_reference_via_pluralized_own_name_is_filtered(self):
        # "users" table's own "user_id" column: stem "user" has no exact-name
        # table, but "user"+"s" == "users" matches the table ITSELF. It has no
        # "id" column, so the same-name fallback resolves target to its own
        # "user_id" column too — source == target, filtered (REQ-RI-1 自引用).
        users = make_table("users", columns=[col("user_id")])
        schemas = [schema_obj("auth", tables=[users])]
        self.assertEqual(render.infer_column_name_candidates(schemas), [])


class CommentRefCandidateInferenceTests(unittest.TestCase):
    def test_fully_qualified_reference_hits_target_id(self):
        product_id = col("product_id", comment="商品，见 catalog.products")
        items = make_table("items", columns=[col("id"), product_id])
        schemas = [
            schema_obj("orders", tables=[items]),
            schema_obj("catalog", tables=[make_table("products", columns=[col("id")])]),
        ]
        candidates = render.infer_comment_ref_candidates(schemas)
        self.assertIn(
            {
                "source": "orders.items.product_id",
                "target": "catalog.products.id",
                "signal": "comment_ref",
            },
            candidates,
        )

    def test_bare_table_name_unique_across_schemas_hits(self):
        parent_id = col("parent_id", comment="父角色，参考 roles 表")
        roles = make_table("roles", columns=[col("id"), parent_id])
        schemas = [schema_obj("auth", tables=[roles])]
        candidates = render.infer_comment_ref_candidates(schemas)
        self.assertIn(
            {"source": "auth.roles.parent_id", "target": "auth.roles.id", "signal": "comment_ref"},
            candidates,
        )

    def test_bare_table_name_ambiguous_across_schemas_does_not_hit(self):
        owner_id = col("owner_id", comment="所有者，参考 users 表")
        t = make_table("things", columns=[col("id"), owner_id])
        schemas = [
            schema_obj("auth", tables=[t, make_table("users", columns=[col("id")])]),
            schema_obj("public", tables=[make_table("users", columns=[col("id")])]),
        ]
        candidates = render.infer_comment_ref_candidates(schemas)
        self.assertEqual(
            [c for c in candidates if c["source"] == "auth.things.owner_id"], []
        )

    def test_self_reference_via_own_table_name_in_comment_is_filtered(self):
        # A table's own "id" column COMMENT mentions the table's own name
        # (bare token, unique across schemas) -> would resolve target to
        # "auth.orders.id", same as source -> self-loop, MUST be filtered
        # (relation-inference-and-diagram [impl-review-fix]: comment_ref
        # lacked the self-loop guard that column_name already has).
        id_col = col("id", comment="订单主键，见 orders 表说明")
        orders = make_table("orders", columns=[id_col])
        schemas = [schema_obj("auth", tables=[orders])]
        candidates = render.infer_comment_ref_candidates(schemas)
        self.assertEqual(
            [c for c in candidates if c["source"] == "auth.orders.id"], []
        )

    def test_self_reference_via_qualified_own_table_name_is_filtered(self):
        # Same self-loop case but via the fully-qualified schema.table form.
        id_col = col("id", comment="订单主键，见 auth.orders")
        orders = make_table("orders", columns=[id_col])
        schemas = [schema_obj("auth", tables=[orders])]
        candidates = render.infer_comment_ref_candidates(schemas)
        self.assertEqual(
            [c for c in candidates if c["source"] == "auth.orders.id"], []
        )

    def test_already_annotated_span_is_stripped_before_scanning(self):
        c = col("org_id", comment="所属组织（逻辑关联 auth.orgs.id）")
        t = make_table("units", columns=[col("id"), c])
        schemas = [schema_obj("auth", tables=[t, make_table("orgs", columns=[col("id")])])]
        candidates = render.infer_comment_ref_candidates(schemas)
        # The annotated span itself must not be re-mined as a fresh comment_ref
        # candidate for the same pair (REQ-RI-2 excludes 逻辑关联 spans).
        self.assertNotIn(
            {"source": "auth.units.org_id", "target": "auth.orgs.id", "signal": "comment_ref"},
            candidates,
        )


class CandidateDedupeExclusionTests(unittest.TestCase):
    def test_column_name_signal_wins_over_comment_ref_for_same_pair(self):
        # user_id both stem-matches "users" AND its COMMENT fully-qualifies
        # the same target — column_name signal must be the one that survives.
        user_id = col("user_id", comment="下单人，见 auth.users")
        orders = make_table("orders", columns=[col("id"), user_id])
        schemas = [
            schema_obj("auth", tables=[orders, make_table("users", columns=[col("id")])]),
        ]
        candidates = render.build_candidates(schemas, confirmed=[])
        matches = [c for c in candidates if c["source"] == "auth.orders.user_id"]
        self.assertEqual(matches, [{"source": "auth.orders.user_id", "target": "auth.users.id", "signal": "column_name"}])

    def test_confirmed_entry_excludes_candidate(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        schemas = [schema_obj("auth", tables=[orders, users])]
        confirmed = [{"source": "auth.orders.user_id", "target": "auth.users.id"}]
        candidates = render.build_candidates(schemas, confirmed)
        self.assertEqual(candidates, [])

    def test_ignored_confirmed_entry_also_excludes_candidate(self):
        orders = make_table("orders", columns=[col("id"), col("status_id")])
        statuses = make_table("statuses", columns=[col("id")])
        schemas = [schema_obj("auth", tables=[orders, statuses])]
        confirmed = [{"source": "auth.orders.status_id", "target": "auth.statuses.id", "ignore": True}]
        self.assertEqual(render.build_candidates(schemas, confirmed), [])

    def test_annotated_logical_relation_excludes_candidate(self):
        user_id = col("user_id", comment="逻辑关联 auth.users.id")
        orders = make_table("orders", columns=[col("id"), user_id])
        users = make_table("users", columns=[col("id")])
        schemas = [schema_obj("auth", tables=[orders, users])]
        self.assertEqual(render.build_candidates(schemas, confirmed=[]), [])

    def test_candidates_sorted_by_source_then_target(self):
        invoices = make_table("invoices", columns=[col("id"), col("user_id")])
        schemas = [
            schema_obj("billing", tables=[invoices]),
            schema_obj("public", tables=[make_table("users", columns=[col("id")])]),
            schema_obj("auth", tables=[make_table("users", columns=[col("id")])]),
        ]
        candidates = render.build_candidates(schemas, confirmed=[])
        self.assertEqual(
            [c["target"] for c in candidates],
            ["auth.users.id", "public.users.id"],
        )


class CandidatesYamlOutputTests(TmpDirCase):
    def test_empty_candidates_writes_bracket_array_with_header_comment(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        content = (self.dbmeta_dir / "_relations.candidates.yaml").read_text(encoding="utf-8")
        self.assertIn("勿手改", content)
        self.assertTrue(content.rstrip("\n").endswith("[]"))

    def test_candidate_entry_has_three_fields(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        self.run_render(v1_doc([schema_obj("auth", tables=[orders, users])]))
        content = (self.dbmeta_dir / "_relations.candidates.yaml").read_text(encoding="utf-8")
        self.assertIn("- source: auth.orders.user_id", content)
        self.assertIn("  target: auth.users.id", content)
        self.assertIn("  signal: column_name", content)

    def test_confirmed_entry_excluded_from_written_candidates_file(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        (self.dbmeta_dir).mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "# confirmed\n- source: auth.orders.user_id\n  target: auth.users.id\n",
            encoding="utf-8",
        )
        self.run_render(v1_doc([schema_obj("auth", tables=[orders, users])]))
        content = (self.dbmeta_dir / "_relations.candidates.yaml").read_text(encoding="utf-8")
        self.assertNotIn("auth.orders.user_id", content)

    def test_candidates_byte_identical_on_second_run_with_unchanged_confirmed(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        doc = v1_doc([schema_obj("auth", tables=[orders, users])])
        self.run_render(doc)
        first = (self.dbmeta_dir / "_relations.candidates.yaml").read_text(encoding="utf-8")
        self.run_render(doc)
        second = (self.dbmeta_dir / "_relations.candidates.yaml").read_text(encoding="utf-8")
        self.assertEqual(first, second)


class LoadConfirmedTests(TmpDirCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(render.load_confirmed(self.dbmeta_dir), [])

    def test_valid_entries_round_trip(self):
        self.dbmeta_dir.mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "# confirmed\n"
            "- source: auth.orders.user_id\n"
            "  target: auth.users.id\n"
            "- source: auth.orders.status_id\n"
            "  target: public.statuses.id\n"
            "  ignore: true\n",
            encoding="utf-8",
        )
        result = render.load_confirmed(self.dbmeta_dir)
        self.assertEqual(
            result,
            [
                {"source": "auth.orders.user_id", "target": "auth.users.id"},
                {"source": "auth.orders.status_id", "target": "public.statuses.id", "ignore": True},
            ],
        )

    def test_single_bad_entry_skipped_others_kept(self):
        self.dbmeta_dir.mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "- source: auth.orders.user_id\n"
            "  target: auth.users\n"  # missing column segment -> not 3-segment
            "- source: auth.orders.status_id\n"
            "  target: public.statuses.id\n",
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = render.load_confirmed(self.dbmeta_dir)
        self.assertEqual(
            result, [{"source": "auth.orders.status_id", "target": "public.statuses.id"}]
        )
        self.assertIn("problem:", stderr.getvalue())

    def test_file_level_format_error_yields_empty_and_warns(self):
        self.dbmeta_dir.mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "- source: 'auth.orders.user_id'\n  target: auth.users.id\n",  # quoted -> unsupported
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = render.load_confirmed(self.dbmeta_dir)
        self.assertEqual(result, [])
        self.assertIn("problem:", stderr.getvalue())

    def test_duplicate_pair_second_occurrence_skipped(self):
        self.dbmeta_dir.mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "- source: auth.orders.user_id\n"
            "  target: auth.users.id\n"
            "- source: auth.orders.user_id\n"
            "  target: auth.users.id\n",
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = render.load_confirmed(self.dbmeta_dir)
        self.assertEqual(result, [{"source": "auth.orders.user_id", "target": "auth.users.id"}])
        self.assertIn("problem:", stderr.getvalue())


class MermaidErDiagramTests(unittest.TestCase):
    """DD-7/REQ-RD-1..4: render.render_mermaid_er_diagram(pairs) ->
    Mermaid erDiagram fence, given fully-qualified (source_column,
    target_column) pairs."""

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(render.render_mermaid_er_diagram([]), "")

    def test_direction_is_target_on_the_left(self):
        content = render.render_mermaid_er_diagram(
            [("auth.orders.user_id", "auth.users.id")]
        )
        self.assertIn("```mermaid", content)
        self.assertIn("erDiagram", content)
        self.assertIn('auth_users ||--o{ auth_orders : "user_id"', content)
        self.assertTrue(content.rstrip("\n").endswith("```"))

    def test_dedup_same_table_pair_picks_lexicographically_smallest_label(self):
        content = render.render_mermaid_er_diagram(
            [
                ("auth.orders.updated_by", "auth.users.id"),
                ("auth.orders.created_by", "auth.users.id"),
            ]
        )
        self.assertEqual(content.count("||--o{"), 1)
        self.assertIn('auth_users ||--o{ auth_orders : "created_by"', content)

    def test_sorted_by_source_table_then_target_table(self):
        content = render.render_mermaid_er_diagram(
            [
                ("billing.invoices.order_id", "billing.orders.id"),
                ("auth.orders.user_id", "auth.users.id"),
            ]
        )
        auth_line = content.index("auth_users")
        billing_line = content.index("billing_orders")
        self.assertLess(auth_line, billing_line)

    def test_node_id_collision_across_distinct_tables_fails_loud(self):
        # B1: `a_b.c` and `a.b_c` both fold to node id `a_b_c`; emitting a
        # silently-merged diagram would be wrong, so fail loud instead.
        with self.assertRaises(render.CollectFormatError):
            render.render_mermaid_er_diagram(
                [
                    ("a_b.c.x", "z.t.id"),
                    ("a.b_c.y", "z.t.id"),
                ]
            )


class PendingSqlGenerationTests(unittest.TestCase):
    """REQ-RI-6/DD-6: render.render_pending_sql(schemas, confirmed) ->
    `.dbmeta/_relations.pending.sql` full-file content."""

    def test_appends_to_existing_comment(self):
        orders = make_table(
            "orders", columns=[col("id"), col("user_id", comment="下单用户")]
        )
        schemas = [
            schema_obj("auth", tables=[orders, make_table("users", columns=[col("id")])]),
        ]
        confirmed = [{"source": "auth.orders.user_id", "target": "auth.users.id"}]
        content = render.render_pending_sql(schemas, confirmed)
        self.assertIn(
            "COMMENT ON COLUMN auth.orders.user_id IS '下单用户 逻辑关联 auth.users.id';",
            content,
        )

    def test_no_existing_comment_no_leading_space(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        schemas = [
            schema_obj("auth", tables=[orders, make_table("users", columns=[col("id")])]),
        ]
        confirmed = [{"source": "auth.orders.user_id", "target": "auth.users.id"}]
        content = render.render_pending_sql(schemas, confirmed)
        self.assertIn(
            "COMMENT ON COLUMN auth.orders.user_id IS '逻辑关联 auth.users.id';",
            content,
        )

    def test_multi_target_same_source_one_statement_lexicographic(self):
        customer_id = col("customer_id", comment="下单人")
        orders = make_table("orders", columns=[col("id"), customer_id])
        schemas = [
            schema_obj(
                "shop",
                tables=[orders],
            ),
            schema_obj("auth", tables=[make_table("users", columns=[col("id")])]),
            schema_obj("crm", tables=[make_table("customers", columns=[col("id")])]),
        ]
        confirmed = [
            {"source": "shop.orders.customer_id", "target": "crm.customers.id"},
            {"source": "shop.orders.customer_id", "target": "auth.users.id"},
        ]
        content = render.render_pending_sql(schemas, confirmed)
        self.assertEqual(
            content.count("COMMENT ON COLUMN shop.orders.customer_id"),
            1,
        )
        self.assertIn(
            "COMMENT ON COLUMN shop.orders.customer_id IS "
            "'下单人 逻辑关联 auth.users.id 逻辑关联 crm.customers.id';",
            content,
        )

    def test_already_annotated_target_excluded_others_kept(self):
        user_id = col("user_id", comment="下单用户 逻辑关联 auth.users.id")
        orders = make_table("orders", columns=[col("id"), user_id])
        schemas = [
            schema_obj("shop", tables=[orders]),
            schema_obj("auth", tables=[make_table("users", columns=[col("id")])]),
            schema_obj("crm", tables=[make_table("customers", columns=[col("id")])]),
        ]
        confirmed = [
            {"source": "shop.orders.user_id", "target": "auth.users.id"},
            {"source": "shop.orders.user_id", "target": "crm.customers.id"},
        ]
        content = render.render_pending_sql(schemas, confirmed)
        self.assertIn(
            "COMMENT ON COLUMN shop.orders.user_id IS "
            "'下单用户 逻辑关联 auth.users.id 逻辑关联 crm.customers.id';",
            content,
        )

    def test_all_targets_already_annotated_source_omitted(self):
        user_id = col("user_id", comment="下单用户 逻辑关联 auth.users.id")
        orders = make_table("orders", columns=[col("id"), user_id])
        schemas = [
            schema_obj("shop", tables=[orders]),
            schema_obj("auth", tables=[make_table("users", columns=[col("id")])]),
        ]
        confirmed = [{"source": "shop.orders.user_id", "target": "auth.users.id"}]
        content = render.render_pending_sql(schemas, confirmed)
        self.assertNotIn("COMMENT ON COLUMN", content)

    def test_ignore_true_excluded(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        schemas = [
            schema_obj("auth", tables=[orders, make_table("users", columns=[col("id")])]),
        ]
        confirmed = [
            {"source": "auth.orders.user_id", "target": "auth.users.id", "ignore": True}
        ]
        content = render.render_pending_sql(schemas, confirmed)
        self.assertNotIn("COMMENT ON COLUMN", content)

    def test_reserved_word_table_quoted_and_literal_escaped(self):
        user_id = col("user_id", comment="buyer's id")
        order_table = make_table("order", columns=[col("id"), user_id])
        schemas = [
            schema_obj("shop", tables=[order_table]),
            schema_obj("auth", tables=[make_table("users", columns=[col("id")])]),
        ]
        confirmed = [{"source": "shop.order.user_id", "target": "auth.users.id"}]
        content = render.render_pending_sql(schemas, confirmed)
        self.assertIn(
            'COMMENT ON COLUMN shop."order".user_id IS '
            "'buyer''s id 逻辑关联 auth.users.id';",
            content,
        )

    def test_stale_source_skipped_with_warning(self):
        schemas = [schema_obj("auth", tables=[make_table("users", columns=[col("id")])])]
        confirmed = [{"source": "auth.orders.user_id", "target": "auth.users.id"}]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            content = render.render_pending_sql(schemas, confirmed)
        self.assertNotIn("COMMENT ON COLUMN", content)
        self.assertIn("problem:", stderr.getvalue())

    def test_no_confirmed_writes_header_only(self):
        schemas = [schema_obj("auth", tables=[make_table("users", columns=[col("id")])])]
        content = render.render_pending_sql(schemas, confirmed=[])
        self.assertNotIn("COMMENT ON COLUMN", content)
        self.assertIn(".dbmeta/_relations.pending.sql", content)


class PendingSqlEndToEndTests(TmpDirCase):
    def test_written_via_run_and_idempotent(self):
        orders = make_table("orders", columns=[col("id"), col("user_id")])
        users = make_table("users", columns=[col("id")])
        doc = v1_doc([schema_obj("auth", tables=[orders, users])])
        self.dbmeta_dir.mkdir(parents=True)
        (self.dbmeta_dir / "_relations.confirmed.yaml").write_text(
            "- source: auth.orders.user_id\n  target: auth.users.id\n",
            encoding="utf-8",
        )
        self.run_render(doc)
        path = self.dbmeta_dir / "_relations.pending.sql"
        first = path.read_text(encoding="utf-8")
        self.assertIn(
            "COMMENT ON COLUMN auth.orders.user_id IS '逻辑关联 auth.users.id';",
            first,
        )
        self.run_render(doc)
        second = path.read_text(encoding="utf-8")
        self.assertEqual(first, second)


class YamlSubsetParserTests(unittest.TestCase):
    def test_empty_array_literal(self):
        self.assertEqual(render._parse_relations_yaml_subset("# comment\n[]\n"), [])

    def test_quoted_value_is_format_error(self):
        self.assertIsNone(render._parse_relations_yaml_subset('- source: "auth.orders.user_id"\n'))

    def test_flow_style_is_format_error(self):
        self.assertIsNone(render._parse_relations_yaml_subset("[{source: a.b.c}]\n"))

    def test_nested_mapping_is_format_error(self):
        self.assertIsNone(
            render._parse_relations_yaml_subset("- source: a.b.c\n  nested:\n    x: 1\n")
        )

    def test_multiline_block_scalar_is_format_error(self):
        self.assertIsNone(render._parse_relations_yaml_subset("- source: |\n    a.b.c\n"))

    def test_value_outside_identifier_charset_is_format_error(self):
        self.assertIsNone(
            render._parse_relations_yaml_subset("- source: a b c\n  target: x.y.z\n")
        )

    def test_valid_entries_round_trip(self):
        text = (
            "# top comment\n"
            "- source: auth.orders.user_id\n"
            "  target: auth.users.id\n"
            "  signal: column_name\n"
        )
        self.assertEqual(
            render._parse_relations_yaml_subset(text),
            [{"source": "auth.orders.user_id", "target": "auth.users.id", "signal": "column_name"}],
        )

    def test_duplicate_key_within_entry_is_format_error(self):
        # T6: a repeated key inside one entry (silent last-write-wins before)
        # now trips file-level fail-soft, consistent with the per-entry contract.
        self.assertIsNone(
            render._parse_relations_yaml_subset(
                "- source: a.b.c\n  target: x.y.z\n  target: p.q.r\n"
            )
        )


class ThreeSegmentIdentifierTests(unittest.TestCase):
    """T2: _is_three_segment_identifier — exactly 3 non-empty dot-separated
    segments, each in the D-M charset. Empty-segment shapes like `a..b` (which
    IDENTIFIER_RE alone would accept, treating `.` as an ordinary char) are the
    coverage gap this pins down."""

    def test_valid_three_segment(self):
        self.assertTrue(render._is_three_segment_identifier("auth.users.id"))

    def test_empty_middle_segment_rejected(self):
        self.assertFalse(render._is_three_segment_identifier("a..b"))

    def test_empty_leading_segment_rejected(self):
        self.assertFalse(render._is_three_segment_identifier(".a.b"))

    def test_empty_trailing_segment_rejected(self):
        self.assertFalse(render._is_three_segment_identifier("a.b."))

    def test_two_segments_rejected(self):
        self.assertFalse(render._is_three_segment_identifier("a.b"))

    def test_four_segments_rejected(self):
        self.assertFalse(render._is_three_segment_identifier("a.b.c.d"))

    def test_out_of_charset_segment_rejected(self):
        self.assertFalse(render._is_three_segment_identifier("a.b c.d"))


class SchemaReadmeTests(TmpDirCase):
    def test_table_row_count_matches_top_level_table_count(self):
        t1 = make_full_table("users", reltuples=5)
        t2 = make_full_table("roles", reltuples=2)
        self.run_render(v1_doc([schema_obj("auth", tables=[t1, t2])]))
        content = (self.dbmeta_dir / "auth" / "README.md").read_text(encoding="utf-8")
        self.assertEqual(content.count("| users |") + content.count("| roles |"), 2)

    def test_partition_children_not_counted_as_top_level_rows(self):
        parent = make_full_table("audit", kind="partitioned_table", reltuples=0)
        child = make_full_table("audit_2026_08", partition_of="audit")
        self.run_render(v1_doc([schema_obj("logs", tables=[parent, child])]))
        content = (self.dbmeta_dir / "logs" / "README.md").read_text(encoding="utf-8")
        self.assertIn("| audit |", content)
        self.assertNotIn("| audit_2026_08 |", content)

    def test_views_and_function_signatures_present(self):
        view = make_view("v_active_users", comment="活跃用户")
        fn = make_function("fn_audit", identity_args="p_id bigint", comment="审计")
        self.run_render(v1_doc([schema_obj("auth", tables=[], functions=[fn], views=[view])]))
        content = (self.dbmeta_dir / "auth" / "README.md").read_text(encoding="utf-8")
        self.assertIn("v_active_users", content)
        self.assertIn("fn_audit(p_id bigint)", content)

    def test_readme_has_exactly_one_index_managed_block(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        content = (self.dbmeta_dir / "auth" / "README.md").read_text(encoding="utf-8")
        self.assertEqual(content.count("<!-- pg-dict:index:start -->"), 1)
        self.assertEqual(content.count("<!-- pg-dict:index:end -->"), 1)

    def test_readme_byte_identical_on_second_run(self):
        doc = v1_doc([schema_obj("auth", tables=[make_full_table("users", reltuples=1)])])
        self.run_render(doc)
        first = (self.dbmeta_dir / "auth" / "README.md").read_text(encoding="utf-8")
        self.run_render(doc)
        second = (self.dbmeta_dir / "auth" / "README.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_readme_deleted_outright_on_schema_collapse(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        readme_path = self.dbmeta_dir / "auth" / "README.md"
        self.assertTrue(readme_path.exists())
        result = self.run_render(v1_doc([]))
        self.assertFalse(readme_path.exists())
        self.assertIn(str(readme_path), result["deleted"])


class RootReadmeTests(TmpDirCase):
    def run_render(self, doc: str) -> dict:
        return render.run(doc, self.dbmeta_dir)

    def test_reading_order_note_precedes_managed_block(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        content = (self.dbmeta_dir / "README.md").read_text(encoding="utf-8")
        block_pos = content.index("<!-- pg-dict:index:start -->")
        self.assertLess(content.index("README"), block_pos)
        self.assertIn("_relations.md", content[:block_pos])
        self.assertIn("rules.md", content[:block_pos])

    def test_index_header_has_no_attribution_column(self):
        self.run_render(v1_doc([schema_obj("example", tables=[make_table("users")])]))
        content = (self.dbmeta_dir / "README.md").read_text(encoding="utf-8")
        header_line = [line for line in content.splitlines() if line.startswith("| schema")][0]
        self.assertEqual(header_line, "| schema | 表数 | 视图 | 函数 | 缺注释 |")

    def test_counts_and_gap_column(self):
        users = make_table(
            "users",
            columns=[{"name": "id", "type": "bigint", "default": None, "nullable": False, "comment": None}],
        )
        view = make_view("v1")
        fn = make_function("fn1")
        self.run_render(v1_doc([schema_obj("auth", tables=[users], views=[view], functions=[fn])]))
        content = (self.dbmeta_dir / "README.md").read_text(encoding="utf-8")
        row = [line for line in content.splitlines() if line.startswith("| auth")][0]
        cells = [c.strip() for c in row.strip("|").split("|")]
        # schema | 表数 | 视图 | 函数 | 缺注释
        self.assertEqual(cells[0], "auth")
        self.assertEqual(cells[1], "1")
        self.assertEqual(cells[2], "1")
        self.assertEqual(cells[3], "1")
        # gaps: users table itself + its id column + fn1, none have a COMMENT
        self.assertEqual(int(cells[4]), 3)

    def test_readme_byte_identical_on_second_run(self):
        doc = v1_doc([schema_obj("auth", tables=[make_table("users")])])
        self.run_render(doc)
        first = (self.dbmeta_dir / "README.md").read_text(encoding="utf-8")
        self.run_render(doc)
        second = (self.dbmeta_dir / "README.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)


class FullSurfaceIdempotencyTests(TmpDirCase):
    def test_table_view_function_all_byte_identical_on_second_run(self):
        trg = [make_trigger("trg_audit", "fn_audit")]
        cons = [{"name": "users_pkey", "type": "p", "definition": "PRIMARY KEY (id)", "is_local": True}]
        idx = [{"name": "users_pkey", "definition": "CREATE UNIQUE INDEX ..."}]
        users = make_full_table("users", triggers=trg, constraints=cons, indexes=idx, reltuples=7)
        fn = make_function("fn_audit", identity_args="", comment="审计触发器函数")
        view = make_view("v_users", comment="用户视图")
        doc = v1_doc([schema_obj("auth", tables=[users], functions=[fn], views=[view])])

        self.run_render(doc)
        paths = [
            self.dbmeta_dir / "auth" / "tables" / "users.sql",
            self.dbmeta_dir / "auth" / "functions" / "fn_audit.sql",
            self.dbmeta_dir / "auth" / "views" / "v_users.sql",
        ]
        first = {p: p.read_text(encoding="utf-8") for p in paths}
        self.run_render(doc)
        second = {p: p.read_text(encoding="utf-8") for p in paths}
        self.assertEqual(first, second)

    def test_full_tree_second_regen_has_empty_written_and_deleted(self):
        """Acceptance: "全树二次再生逐字节一致且无文件增删（deleted/written 为空）"
        — exercises every Task-3 surface (_collect.json/_relations.md/_gaps.md/
        both README kinds) together with the Task-2 object surfaces in one doc."""
        org_id = {
            "name": "org_id",
            "type": "bigint",
            "default": None,
            "nullable": True,
            "comment": "所属组织 ID（逻辑关联 auth.orgs.id）",
        }
        status = {
            "name": "status",
            "type": "int",
            "default": None,
            "nullable": True,
            "comment": "状态：0=待处理 1=处理中",
        }
        users = make_full_table("users", columns=[org_id, status], reltuples=3)
        orgs = make_full_table("orgs", reltuples=1)
        fn = make_function("fn_audit", comment="审计")
        view = make_view("v_users", comment="用户视图")
        doc = v1_doc(
            [schema_obj("auth", tables=[users, orgs], functions=[fn], views=[view])]
        )

        self.run_render(doc)
        result_same = self.run_render(doc)
        self.assertEqual(result_same["written"], [])
        self.assertEqual(result_same["deleted"], [])

    def run_render(self, doc: str) -> dict:
        return render.run(doc, self.dbmeta_dir)


class RulesFileUntouchedTests(TmpDirCase):
    def test_rules_md_not_created_when_absent(self):
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertFalse((self.dbmeta_dir / "rules.md").exists())

    def test_rules_md_left_byte_identical_when_hand_edited(self):
        self.dbmeta_dir.mkdir(parents=True, exist_ok=True)
        rules_path = self.dbmeta_dir / "rules.md"
        rules_content = "# 查询侧规则\n\n人工维护的内容。\n"
        rules_path.write_text(rules_content, encoding="utf-8")
        self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertEqual(rules_path.read_text(encoding="utf-8"), rules_content)


# ---------------------------------------------------------------------------
# Task 2 (design.md DD-1/DD-2/DD-4/DD-7): BlockSyntax abstraction — SQL_SYNTAX
# round-trips through the same parse/wrap/merge/_has_annotation/
# process_removed_object_file machinery as MD_SYNTAX, only the marker syntax
# differs. README call sites keep passing MD_SYNTAX (unchanged behavior,
# covered by every test class above this one, which is why those are left
# untouched rather than duplicated here).
# ---------------------------------------------------------------------------


class SqlSyntaxWrapParseMergeRoundTripTests(unittest.TestCase):
    def test_wrap_block_uses_dash_dash_markers(self):
        wrapped = render.wrap_block("table:users", "body\n", render.SQL_SYNTAX)
        self.assertEqual(
            wrapped,
            "-- pg-dict:table:users:start\nbody\n-- pg-dict:table:users:end\n",
        )

    def test_parse_segments_splits_text_and_block_on_sql_markers(self):
        text = (
            "-- hand-written note\n"
            "-- pg-dict:table:users:start\n"
            "body\n"
            "-- pg-dict:table:users:end\n"
            "-- trailing note\n"
        )
        segments = render.parse_segments(text, render.SQL_SYNTAX)
        self.assertEqual(
            segments,
            [
                ("text", "-- hand-written note\n"),
                ("block", "table:users", "body\n"),
                ("text", "-- trailing note\n"),
            ],
        )

    def test_merge_blocks_creates_fresh_sql_file(self):
        header = "-- auth.users 表\n"
        content = render.merge_blocks(None, header, [("table:users", "body\n")], render.SQL_SYNTAX)
        self.assertEqual(
            content,
            "-- auth.users 表\n-- pg-dict:table:users:start\nbody\n-- pg-dict:table:users:end\n",
        )

    def test_merge_blocks_regen_rewrites_block_and_preserves_text_outside_it_sql(self):
        header = "-- auth.users 表\n"
        first = render.merge_blocks(None, header, [("table:users", "old body\n")], render.SQL_SYNTAX)
        annotated = first + "-- 人工注记：此表即将拆分\n"
        second = render.merge_blocks(
            annotated, header, [("table:users", "new body\n")], render.SQL_SYNTAX
        )
        self.assertIn("new body\n", second)
        self.assertNotIn("old body\n", second)
        self.assertIn("-- 人工注记：此表即将拆分\n", second)

    def test_merge_blocks_second_regen_with_unchanged_body_is_byte_identical(self):
        header = "-- auth.users 表\n"
        first = render.merge_blocks(None, header, [("table:users", "body\n")], render.SQL_SYNTAX)
        second = render.merge_blocks(first, header, [("table:users", "body\n")], render.SQL_SYNTAX)
        self.assertEqual(first, second)


class SqlHasAnnotationTests(unittest.TestCase):
    def test_dash_dash_comment_line_counts_as_annotation_under_sql_syntax(self):
        # DD-2: unlike MD_SYNTAX (where an HTML comment is never annotation), a
        # bare "--" line under SQL_SYNTAX has no other way to be hand-written text
        # in an executable .sql file, so it MUST count.
        self.assertTrue(render._has_annotation("-- 人工注记\n", render.SQL_SYNTAX))

    def test_blank_text_is_not_annotation_under_sql_syntax(self):
        self.assertFalse(render._has_annotation("\n  \n", render.SQL_SYNTAX))

    def test_same_dash_dash_text_is_annotation_under_md_syntax_default_unaffected(self):
        # Sanity: MD_SYNTAX behavior (existing README/object-file contract) is
        # untouched by adding the syntax parameter — a bare HTML comment still
        # does NOT count as annotation under MD_SYNTAX.
        self.assertFalse(render._has_annotation("<!-- 说明 -->\n", render.MD_SYNTAX))


class SqlProcessRemovedObjectFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "users.sql"

    def tearDown(self):
        self._tmp.cleanup()

    def test_header_alone_does_not_trigger_orphan_banner_sql(self):
        header = render.object_file_header("table", "auth", "users", render.SQL_SYNTAX)
        self.path.write_text(
            header + render.wrap_block("table:users", "body\n", render.SQL_SYNTAX),
            encoding="utf-8",
        )
        deleted = render.process_removed_object_file(self.path, header, render.SQL_SYNTAX)
        self.assertTrue(deleted)
        self.assertFalse(self.path.exists())

    def test_dash_dash_annotation_outside_block_keeps_file_with_orphan_banner_sql(self):
        header = render.object_file_header("table", "auth", "users", render.SQL_SYNTAX)
        self.path.write_text(
            header
            + "-- 人工注记：此表历史信息\n"
            + render.wrap_block("table:users", "body\n", render.SQL_SYNTAX),
            encoding="utf-8",
        )
        deleted = render.process_removed_object_file(self.path, header, render.SQL_SYNTAX)
        self.assertFalse(deleted)
        content = self.path.read_text(encoding="utf-8")
        self.assertIn(render.SQL_SYNTAX.orphan_banner, content)
        self.assertIn("-- 人工注记：此表历史信息\n", content)
        # idempotent: running again does not duplicate the banner
        deleted_again = render.process_removed_object_file(self.path, header, render.SQL_SYNTAX)
        self.assertFalse(deleted_again)
        self.assertEqual(content.count(render.SQL_SYNTAX.orphan_banner), 1)

    def test_header_plus_orphan_banner_only_gets_deleted_sql(self):
        """DD-2 逐字：去掉文件头固定前缀与孤立横幅后若再无其它内容，MUST 判定为
        无注记 ⇒ 文件被删除。这是「文件已被标孤立、随后人工把自己的注记删掉，只剩
        header+banner」的场景——SQL 语法下 comment_re 是空匹配，横幅本身（`-- ...`
        行）不会被任何步骤剥离，若 process_removed_object_file 只剥 header 不剥
        banner，就会把横幅误判成注记，导致此文件永远无法被自动删除。"""
        header = render.object_file_header("table", "auth", "users", render.SQL_SYNTAX)
        self.path.write_text(
            header
            + render.SQL_SYNTAX.orphan_banner
            + render.wrap_block("table:users", "body\n", render.SQL_SYNTAX),
            encoding="utf-8",
        )
        deleted = render.process_removed_object_file(self.path, header, render.SQL_SYNTAX)
        self.assertTrue(deleted)
        self.assertFalse(self.path.exists())

    def test_header_plus_banner_plus_manual_annotation_kept_sql(self):
        """孤立横幅之外若还有人工注记，MUST 保留文件且不重复横幅（DD-2 语义等价：
        剥离 header 与 banner 后仍有非空白行才算注记）。"""
        header = render.object_file_header("table", "auth", "users", render.SQL_SYNTAX)
        self.path.write_text(
            header
            + render.SQL_SYNTAX.orphan_banner
            + "-- 人工注记\n"
            + render.wrap_block("table:users", "body\n", render.SQL_SYNTAX),
            encoding="utf-8",
        )
        deleted = render.process_removed_object_file(self.path, header, render.SQL_SYNTAX)
        self.assertFalse(deleted)
        content = self.path.read_text(encoding="utf-8")
        self.assertEqual(content.count(render.SQL_SYNTAX.orphan_banner), 1)
        self.assertIn("-- 人工注记\n", content)


class ObjectFileHeaderSqlSyntaxTests(unittest.TestCase):
    """design.md DD-7: exact fixed-prefix text for the three SQL object kinds —
    also asserted as the `_has_annotation` exemption string via
    SqlProcessRemovedObjectFileTests.test_header_alone_does_not_trigger_orphan_banner_sql
    above, which proves a bare header (nothing else) never counts as annotation."""

    def test_table_header_matches_dd7_verbatim(self):
        header = render.object_file_header("table", "auth", "users", render.SQL_SYNTAX)
        self.assertEqual(
            header,
            "-- auth.users 表\n"
            "-- 本文件由 pg-dict skill 自动生成。托管块（-- pg-dict:<ident>:start/end）"
            "内容会在再生时整体重写；块外文本由人工维护，再生时逐字保留，人工注记 MUST "
            "写成 -- 注释行，否则本文件不可执行。\n"
            "-- 表 DDL 由 pg_catalog 拼装（不含 collation/所有者/权限），完整"
            "重建以 pg_dump 为准；触发器引用的函数在 ../functions/ 下，单文件不保证整库"
            "重放顺序。\n",
        )

    def test_view_header_matches_dd7_verbatim(self):
        header = render.object_file_header("view", "auth", "v_active_users", render.SQL_SYNTAX)
        self.assertEqual(
            header,
            "-- auth.v_active_users 视图\n"
            "-- 本文件由 pg-dict skill 自动生成。托管块（-- pg-dict:<ident>:start/end）"
            "内容会在再生时整体重写；块外文本由人工维护，再生时逐字保留，人工注记 MUST "
            "写成 -- 注释行，否则本文件不可执行。\n",
        )

    def test_function_header_starts_with_dd7_base_prefix_and_has_overload_note(self):
        header = render.object_file_header("fn", "auth", "fn_audit", render.SQL_SYNTAX)
        base = (
            "-- auth.fn_audit 函数\n"
            "-- 本文件由 pg-dict skill 自动生成。托管块（-- pg-dict:<ident>:start/end）"
            "内容会在再生时整体重写；块外文本由人工维护，再生时逐字保留，人工注记 MUST "
            "写成 -- 注释行，否则本文件不可执行。\n"
        )
        self.assertTrue(header.startswith(base))
        extra_lines = header[len(base):].splitlines(keepends=True)
        self.assertEqual(len(extra_lines), 1)
        self.assertTrue(extra_lines[0].startswith("-- "))
        self.assertTrue(extra_lines[0].endswith("\n"))

    def test_md_syntax_header_unchanged(self):
        # Sanity: passing MD_SYNTAX still returns the pre-existing HTML-comment
        # header (README/object-file contract untouched by this ticket).
        header = render.object_file_header("table", "auth", "users", render.MD_SYNTAX)
        self.assertTrue(header.startswith("# `auth.users` 表\n\n<!-- 本文件由 pg-dict skill"))


# ---------------------------------------------------------------------------
# Task 2 DD-4 (spec-review-amendment): functions[].definition fail-loud
# pre-flight check — MUST run before any .dbmeta/ write/delete, across ALL
# schemas, not inlined into a per-schema render path.
# ---------------------------------------------------------------------------


class FunctionDefinitionPreflightTests(TmpDirCase):
    def test_missing_definition_rejected_before_any_write_dbmeta_untouched(self):
        good_fn = make_function("fn_ok")
        self.run_render(v1_doc([schema_obj("auth", functions=[good_fn])]))
        before = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}

        bad_fn = make_function("fn_bad")
        del bad_fn["definition"]
        doc = v1_doc(
            [
                schema_obj("auth", functions=[good_fn]),
                schema_obj("broken", functions=[bad_fn]),
            ]
        )
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)

        exc = ctx.exception
        self.assertEqual(
            exc.problem,
            "函数 broken.fn_bad() 缺少 definition 键，无法生成可执行 DDL",
        )
        self.assertEqual(
            exc.cause,
            "collect JSON 来自旧版 shared/db-collect.sql（尚未采集 pg_get_functiondef）",
        )
        self.assertEqual(exc.fix, "重跑 shared/db-collect.sh 重新采集后再执行 /pg-dict")

        self.assertFalse((self.dbmeta_dir / "broken").exists())
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_empty_string_definition_also_rejected(self):
        bad_fn = make_function("fn_bad", definition="")
        doc = v1_doc([schema_obj("auth", functions=[bad_fn])])
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("fn_bad", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_present_definition_does_not_raise(self):
        result = self.run_render(v1_doc([schema_obj("auth", functions=[make_function("fn_ok")])]))
        self.assertIn("auth", result["schemas"])


# ---------------------------------------------------------------------------
# [impl-review-fix] V1 (code-review Important, outside-voice): a function's or
# view's `definition` source text that happens to contain a line shaped like an
# SQL_SYNTAX managed-block marker (`-- pg-dict:<ident>:start/end`) would corrupt
# SQL_BLOCK_RE's non-greedy parse on the NEXT re-generation — the embedded line
# gets mistaken for the block's own boundary. Rejected as a full pre-flight
# batch (design.md DD-4 discipline) before any filesystem write/delete.
# ---------------------------------------------------------------------------


class ManagedMarkerCollisionPreflightTests(TmpDirCase):
    def test_function_definition_with_marker_line_rejected_before_any_write(self):
        good_fn = make_function("fn_ok")
        self.run_render(v1_doc([schema_obj("auth", functions=[good_fn])]))
        before = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}

        bad_fn = make_function(
            "fn_bad",
            source="BEGIN\n-- pg-dict:fn:x:end\nEND;",
        )
        doc = v1_doc(
            [
                schema_obj("auth", functions=[good_fn]),
                schema_obj("broken", functions=[bad_fn]),
            ]
        )
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)

        exc = ctx.exception
        self.assertIn("broken.fn_bad", exc.problem)
        self.assertIn("-- pg-dict:fn:x:end", exc.problem)
        self.assertIn("托管块标记行", exc.problem)
        self.assertEqual(
            exc.cause,
            "对象源码中出现与 pg-dict 托管块同形的 -- pg-dict:<ident>:start/end 注释行",
        )
        self.assertEqual(exc.fix, "在数据库中修改该对象源码，去掉或改写该注释行后重新采集")

        self.assertFalse((self.dbmeta_dir / "broken").exists())
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_view_definition_with_marker_line_rejected(self):
        bad_view = make_view("v_bad", definition="SELECT 1\n-- pg-dict:view:v_bad:start\n")
        doc = v1_doc([schema_obj("auth", views=[bad_view])])
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("auth.v_bad", ctx.exception.problem)
        self.assertIn("-- pg-dict:view:v_bad:start", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_plain_comment_line_is_not_falsely_flagged(self):
        ok_fn = make_function("fn_ok", source="-- this is a normal comment\nBEGIN END;")
        result = self.run_render(v1_doc([schema_obj("auth", functions=[ok_fn])]))
        self.assertIn("auth", result["schemas"])


# ---------------------------------------------------------------------------
# [impl-review-fix] A1 (code-review Important, antagonist mirror A): a column
# dict missing a hard-subscripted key (`name`/`type`) reaches render_table_ddl
# unvalidated -> bare KeyError mid-run, after earlier schemas already wrote to
# disk (half-written state, non-three-part error). Now caught in
# `_validate_schemas` as a full pre-flight batch, same discipline as every
# other structural check there.
# ---------------------------------------------------------------------------


class ColumnFieldCompletenessPreflightTests(TmpDirCase):
    def test_column_missing_type_rejected_before_any_write(self):
        good_schema = schema_obj("auth", tables=[make_table("users")])
        self.run_render(v1_doc([good_schema]))
        before = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}

        bad_col = {"name": "id", "default": None, "nullable": False, "comment": None}
        bad_schema = schema_obj("broken", tables=[make_table("t1", columns=[bad_col])])
        doc = v1_doc([good_schema, bad_schema])

        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)

        exc = ctx.exception
        self.assertIn("broken.t1.id", exc.problem)
        self.assertIn("type", exc.problem)
        self.assertEqual(exc.cause, "collect JSON 结构不完整（旧版或手工编辑）")
        self.assertEqual(exc.fix, "重跑 shared/db-collect.sh 重新采集")

        self.assertFalse((self.dbmeta_dir / "broken").exists())
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_column_missing_name_rejected(self):
        bad_col = {"type": "bigint", "default": None, "nullable": False, "comment": None}
        doc = v1_doc([schema_obj("auth", tables=[make_table("t1", columns=[bad_col])])])
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(doc)
        self.assertIn("name", ctx.exception.problem)
        self.assertFalse(self.dbmeta_dir.exists())

    def test_normal_columns_do_not_raise(self):
        result = self.run_render(v1_doc([schema_obj("auth", tables=[make_table("users")])]))
        self.assertIn("auth", result["schemas"])

    # B5: the same pre-flight discipline covers every other hard-subscripted
    # `["name"]` — tables[].constraints/indexes/triggers and views[].columns.
    def _assert_rejected_before_any_write(self, bad_schema: dict, expect_in_problem: str):
        good_schema = schema_obj("auth", tables=[make_table("users")])
        self.run_render(v1_doc([good_schema]))
        before = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(v1_doc([good_schema, bad_schema]))
        self.assertIn(expect_in_problem, ctx.exception.problem)
        self.assertEqual(ctx.exception.fix, "重跑 shared/db-collect.sh 重新采集")
        self.assertFalse((self.dbmeta_dir / "broken").exists())
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_constraint_missing_name_rejected_before_any_write(self):
        t = make_table("t1")
        t["constraints"] = [{"type": "p", "definition": "PRIMARY KEY (id)"}]
        self._assert_rejected_before_any_write(
            schema_obj("broken", tables=[t]), "broken.t1 的 constraints[0] 缺少合法的 name"
        )

    def test_index_missing_name_rejected_before_any_write(self):
        t = make_table("t1")
        t["indexes"] = [{"definition": "CREATE INDEX i ON t1 (id)", "inherited_from": None}]
        self._assert_rejected_before_any_write(
            schema_obj("broken", tables=[t]), "broken.t1 的 indexes[0] 缺少合法的 name"
        )

    def test_trigger_missing_name_rejected_before_any_write(self):
        t = make_table("t1")
        t["triggers"] = [{"definition": "CREATE TRIGGER ...", "enabled": "O"}]
        self._assert_rejected_before_any_write(
            schema_obj("broken", tables=[t]), "broken.t1 的 triggers[0] 缺少合法的 name"
        )

    def test_view_column_missing_name_rejected_before_any_write(self):
        v = make_view("v1", columns=[{"position": 1, "type": "bigint", "comment": "c"}])
        self._assert_rejected_before_any_write(
            schema_obj("broken", views=[v]), "broken.v1 的 columns[0] 缺少合法的 name"
        )

    def test_view_options_not_a_list_rejected_before_any_write(self):
        v = make_view("v1")
        v["options"] = "not-a-list"
        self._assert_rejected_before_any_write(
            schema_obj("broken", views=[v]), "broken.v1 的 options 不是数组"
        )

    def test_view_options_element_not_a_string_rejected_before_any_write(self):
        v = make_view("v1")
        v["options"] = ["ok=1", 123]
        self._assert_rejected_before_any_write(
            schema_obj("broken", views=[v]), "broken.v1 的 options[1] 不是字符串"
        )

    # [T42] The name segment is spliced into `WITH (...)` unquoted, so B5 pins
    # its shape: an empty element would render `WITH ()` (invalid SQL, silently
    # written); a `)`/`;`-bearing key would splice extra statements in.
    def test_view_options_empty_element_rejected_before_any_write(self):
        v = make_view("v1")
        v["options"] = ["fillfactor=70", ""]
        self._assert_rejected_before_any_write(
            schema_obj("broken", views=[v]), "broken.v1 的 options[1] 不是合法的存储参数形态"
        )

    def test_view_options_key_with_sql_punctuation_rejected_before_any_write(self):
        v = make_view("v1")
        v["options"] = ["x); DROP TABLE users;--=1"]
        self._assert_rejected_before_any_write(
            schema_obj("broken", views=[v]), "broken.v1 的 options[0] 不是合法的存储参数形态"
        )

    def test_view_options_namespaced_and_bare_names_accepted(self):
        # Positive side of the shape check: namespace dot (`toast.*`) and a
        # bare no-`=` element are legitimate PG forms and MUST still pass B5.
        v = make_view("v1", options=["toast.autovacuum_enabled=true", "security_barrier"])
        self.run_render(v1_doc([schema_obj("ok", views=[v])]))
        ddl = (self.dbmeta_dir / "ok" / "views" / "v1.sql").read_text(encoding="utf-8")
        self.assertIn(
            "CREATE VIEW ok.v1 WITH (toast.autovacuum_enabled='true', security_barrier) AS", ddl
        )

    def test_constraints_not_a_list_rejected(self):
        t = make_table("t1")
        t["constraints"] = {"name": "pk"}
        self._assert_rejected_before_any_write(
            schema_obj("broken", tables=[t]), "broken.t1 的 constraints 不是数组"
        )

    def test_sub_objects_with_names_do_not_raise(self):
        t = make_table("t1")
        t["constraints"] = [{"name": "t1_pkey", "type": "p", "definition": "PRIMARY KEY (id)"}]
        t["indexes"] = [{"name": "t1_pkey", "definition": "CREATE UNIQUE INDEX t1_pkey ON t1 (id)"}]
        t["triggers"] = []
        result = self.run_render(v1_doc([schema_obj("auth", tables=[t])]))
        self.assertIn("auth", result["schemas"])


class RelationDdlEquivalenceB5Tests(TmpDirCase):
    """relation-ddl-equivalence T2.6: B5 shape checks for the seven new keys
    (tables[].options/tablespace, views[].populated/tablespace, tables[] and
    views[]' indexes[].options/tablespace/definition) — each rejected before
    any file is written; the合法 (legal) shapes at the bottom MUST pass."""

    def _assert_rejected(self, bad_schema: dict, expect_in_problem: str):
        good_schema = schema_obj("auth", tables=[make_table("users")])
        self.run_render(v1_doc([good_schema]))
        before = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(v1_doc([good_schema, bad_schema]))
        self.assertIn(expect_in_problem, ctx.exception.problem)
        self.assertFalse((self.dbmeta_dir / "broken").exists())
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_table_options_sql_punctuation_rejected(self):
        t = make_full_table("t1", options=["x); DROP TABLE t;--=1"])
        self._assert_rejected(
            schema_obj("broken", tables=[t]), "broken.t1 的 options[0] 不是合法的存储参数形态"
        )

    def test_table_options_not_a_list_rejected(self):
        t = make_full_table("t1")
        t["options"] = "not-a-list"
        self._assert_rejected(schema_obj("broken", tables=[t]), "broken.t1 的 options 不是数组")

    def test_table_options_non_string_element_rejected(self):
        t = make_full_table("t1")
        t["options"] = ["ok=1", 123]
        self._assert_rejected(schema_obj("broken", tables=[t]), "broken.t1 的 options[1] 不是字符串")

    def test_view_populated_wrong_type_rejected(self):
        v = make_view("v1")
        v["populated"] = "yes"
        self._assert_rejected(
            schema_obj("broken", views=[v]),
            "broken.v1 的 populated 形状不合法：应为布尔 / 实得 str",
        )

    def test_table_tablespace_wrong_type_rejected(self):
        t = make_full_table("t1", tablespace=None)
        t["tablespace"] = 1
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 tablespace 形状不合法：应为 null 或非空字符串 / 实得 int",
        )

    def test_table_tablespace_empty_string_rejected(self):
        t = make_full_table("t1", tablespace="")
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 tablespace 形状不合法：应为 null 或非空字符串 / 实得 ''",
        )

    def test_table_index_options_sql_punctuation_rejected(self):
        idx = [
            {
                "name": "i",
                "definition": "CREATE INDEX i ON broken.t1 (id)",
                "options": ["x); DROP INDEX i;--=1"],
            }
        ]
        t = make_full_table("t1", indexes=idx)
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1.i 的 options[0] 不是合法的存储参数形态",
        )

    def test_table_index_missing_definition_rejected(self):
        t = make_full_table("t1", indexes=[{"name": "i"}])
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 indexes[0] 缺少合法的 definition 字段",
        )

    def test_view_index_missing_definition_rejected(self):
        v = make_view("v1")
        v["indexes"] = [{"name": "i"}]
        self._assert_rejected(
            schema_obj("broken", views=[v]),
            "broken.v1 的 indexes[0] 缺少合法的 definition 字段",
        )

    # relation-ddl-equivalence T2.7/2.9: access_method / foreign / fdw_options
    # B5 shape checks (new keys added on top of the seven above).
    def test_table_access_method_empty_string_rejected(self):
        t = make_full_table("t1", access_method="")
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 access_method 形状不合法：应为 null 或非空字符串 / 实得 ''",
        )

    def test_table_access_method_wrong_type_rejected(self):
        t = make_full_table("t1", access_method=1)
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 access_method 形状不合法：应为 null 或非空字符串 / 实得 int",
        )

    def test_view_access_method_wrong_type_rejected(self):
        v = make_view("v1", access_method=1)
        self._assert_rejected(
            schema_obj("broken", views=[v]),
            "broken.v1 的 access_method 形状不合法：应为 null 或非空字符串 / 实得 int",
        )

    def test_table_foreign_not_a_dict_rejected(self):
        t = make_full_table("t1", foreign="srv")
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 foreign 形状不合法：应为 null 或对象 / 实得 str",
        )

    def test_table_foreign_empty_server_rejected(self):
        t = make_full_table("t1", foreign={"server": "", "options": []})
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 foreign.server 形状不合法：应为非空字符串 / 实得 ''",
        )

    def test_table_foreign_options_not_a_list_rejected(self):
        t = make_full_table("t1", foreign={"server": "srv", "options": "x"})
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 foreign.options 不是数组",
        )

    def test_foreign_table_kind_with_missing_foreign_rejected(self):
        t = make_full_table("t1", kind="foreign_table")
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 是外部表但缺少 foreign 字段",
        )

    def test_foreign_table_kind_with_foreign_key_absent_entirely_rejected(self):
        t = make_full_table("t1", kind="foreign_table")
        del t["foreign"]
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 是外部表但缺少 foreign 字段",
        )

    def test_column_fdw_options_not_a_list_rejected(self):
        col = {
            "name": "id", "type": "bigint", "default": None, "nullable": False,
            "comment": None, "fdw_options": "x",
        }
        t = make_full_table("t1", columns=[col])
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1.id 的 fdw_options 不是数组",
        )

    def test_column_fdw_options_non_string_element_rejected(self):
        col = {
            "name": "id", "type": "bigint", "default": None, "nullable": False,
            "comment": None, "fdw_options": [1],
        }
        t = make_full_table("t1", columns=[col])
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1.id 的 fdw_options[0] 不是字符串",
        )

    # [spec-review-amendment] every foreign.options[]/fdw_options[] element
    # MUST contain "=" with a non-empty name segment before it.
    def test_foreign_options_element_without_equals_rejected(self):
        t = make_full_table("t1", foreign={"server": "srv", "options": ["nokey"]})
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 foreign.options[0] 不是合法的 name=value 形态",
        )

    def test_foreign_options_element_with_empty_name_rejected(self):
        t = make_full_table("t1", foreign={"server": "srv", "options": ["=v"]})
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1 的 foreign.options[0] 不是合法的 name=value 形态",
        )

    def test_fdw_options_element_without_equals_rejected(self):
        col = {
            "name": "id", "type": "bigint", "default": None, "nullable": False,
            "comment": None, "fdw_options": ["nokey"],
        }
        t = make_full_table("t1", columns=[col])
        self._assert_rejected(
            schema_obj("broken", tables=[t]),
            "broken.t1.id 的 fdw_options[0] 不是合法的 name=value 形态",
        )

    def test_foreign_options_element_with_empty_value_accepted(self):
        # value MAY be empty — only the name segment before "=" is required.
        t = make_full_table("t1", foreign={"server": "srv", "options": ["k="]})
        result = self.run_render(v1_doc([schema_obj("ok", tables=[t])]))
        self.assertIn("ok", result["schemas"])

    def test_legal_new_field_shapes_pass(self):
        t = make_full_table("t1", access_method=None, foreign=None)
        t2 = make_full_table("t2", access_method="heap")
        ft = make_full_table(
            "ft", kind="foreign_table", foreign={"server": "srv", "options": []}
        )
        v = make_view("v1", access_method=None)
        result = self.run_render(v1_doc([schema_obj("ok", tables=[t, t2, ft], views=[v])]))
        self.assertIn("ok", result["schemas"])

    def test_legal_shapes_tablespace_none_indexes_empty_populated_true_pass(self):
        t = make_full_table("t1", options=[], tablespace=None, indexes=[])
        v = make_view("v1", options=[], tablespace=None, populated=True, indexes=[])
        result = self.run_render(v1_doc([schema_obj("ok", tables=[t], views=[v])]))
        self.assertIn("ok", result["schemas"])


class ManagedMarkerCollisionExpandedFieldsTests(TmpDirCase):
    """relation-ddl-equivalence T2.7 [spec-review-amendment Q2]: the marker-
    collision preflight extends beyond function/view `definition` to every
    other catalog string this change lets land verbatim inside a managed
    block or folded-child comment."""

    def _assert_rejected(self, bad_schema: dict):
        good_schema = schema_obj("auth", tables=[make_table("users")])
        self.run_render(v1_doc([good_schema]))
        before = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        with self.assertRaises(render.CollectFormatError) as ctx:
            self.run_render(v1_doc([good_schema, bad_schema]))
        self.assertIn("托管块标记行", ctx.exception.problem)
        self.assertFalse((self.dbmeta_dir / "broken").exists())
        after = {p: p.read_bytes() for p in self.dbmeta_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    _MARKER = "x\n-- pg-dict:table:t:end\nDROP TABLE victim; --"

    def test_table_comment_with_marker_line_rejected(self):
        t = make_full_table("t1", comment=self._MARKER)
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_column_comment_with_marker_line_rejected(self):
        col = {
            "name": "id", "type": "bigint", "default": None, "nullable": False,
            "comment": self._MARKER,
        }
        t = make_full_table("t1", columns=[col])
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_view_comment_with_marker_line_rejected(self):
        v = make_view("v1", comment=self._MARKER)
        self._assert_rejected(schema_obj("broken", views=[v]))

    def test_foreign_options_element_with_marker_line_rejected(self):
        t = make_full_table(
            "t1",
            kind="foreign_table",
            foreign={"server": "srv", "options": [f"filename=a\n-- pg-dict:table:ft:end"]},
        )
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_fdw_options_element_with_marker_line_rejected(self):
        col = {
            "name": "id", "type": "bigint", "default": None, "nullable": False,
            "comment": None, "fdw_options": ["filename=a\n-- pg-dict:table:ft:end"],
        }
        t = make_full_table(
            "t1", kind="foreign_table", columns=[col], foreign={"server": "srv", "options": []}
        )
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_foreign_server_with_marker_line_rejected(self):
        t = make_full_table(
            "t1",
            kind="foreign_table",
            foreign={"server": "x\n-- pg-dict:table:ft:end", "options": []},
        )
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_child_own_index_definition_with_marker_line_rejected(self):
        child = make_full_table(
            "audit_2026_01",
            partition_of="audit",
            indexes=[{"name": "i", "definition": self._MARKER}],
        )
        parent = make_table("audit")
        self._assert_rejected(schema_obj("broken", tables=[parent, child]))

    def test_table_access_method_with_marker_line_rejected(self):
        # code-review cross-model finding CV-1: access_method rides into the block
        # raw via quote_ident, so a newline-bearing amname must be marker-checked.
        t = make_full_table("t1", access_method=self._MARKER)
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_view_access_method_with_marker_line_rejected(self):
        v = make_view("v1", access_method=self._MARKER)
        self._assert_rejected(schema_obj("broken", views=[v]))

    def test_table_options_element_with_marker_line_rejected(self):
        t = make_full_table("t1", options=["fillfactor=70\n-- pg-dict:table:t:start"])
        self._assert_rejected(schema_obj("broken", tables=[t]))

    # --- B6 (issues cleanup r3): pre-existing quote_ident / verbatim fields ---

    def test_table_tablespace_with_marker_line_rejected(self):
        t = make_full_table("t1", tablespace=self._MARKER)
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_view_tablespace_with_marker_line_rejected(self):
        v = make_view("v1", tablespace=self._MARKER)
        self._assert_rejected(schema_obj("broken", views=[v]))

    def test_table_index_tablespace_with_marker_line_rejected(self):
        t = make_full_table(
            "t1",
            indexes=[{"name": "i", "definition": "CREATE INDEX i ON t1 (id)",
                      "tablespace": self._MARKER}],
        )
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_view_index_tablespace_with_marker_line_rejected(self):
        v = make_view(
            "v1",
            indexes=[{"name": "i", "definition": "CREATE INDEX i ON v1 (id)",
                      "tablespace": self._MARKER}],
        )
        self._assert_rejected(schema_obj("broken", views=[v]))

    def test_partition_key_with_marker_line_rejected(self):
        t = make_full_table("t1", kind="partitioned_table")
        t["partition_key"] = f"RANGE ({self._MARKER})"
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_column_type_with_marker_line_rejected(self):
        col = {
            "name": "id", "type": f'"{self._MARKER}"', "default": None,
            "nullable": False, "comment": None,
        }
        t = make_full_table("t1", columns=[col])
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_view_column_type_with_marker_line_rejected(self):
        col = {"name": "id", "type": f'"{self._MARKER}"', "nullable": True, "comment": None}
        v = make_view("v1", columns=[col])
        self._assert_rejected(schema_obj("broken", views=[v]))

    def test_column_default_with_marker_line_rejected(self):
        # pg_get_expr renders a string-literal default with its literal newline,
        # so `DEFAULT E'x\n-- pg-dict:table:t:end'` is enough to forge a marker.
        col = {
            "name": "id", "type": "text", "default": f"'{self._MARKER}'::text",
            "nullable": True, "comment": None,
        }
        t = make_full_table("t1", columns=[col])
        self._assert_rejected(schema_obj("broken", tables=[t]))

    def test_multiline_column_type_without_marker_regenerates_idempotently(self):
        # A newline-bearing quoted type name that does NOT forge a marker is
        # legal; the block must survive a second regen byte-for-byte.
        col = {
            "name": "id", "type": '"weird\ntype"', "default": None,
            "nullable": True, "comment": None,
        }
        t = make_full_table("t1", columns=[col])
        doc = v1_doc([schema_obj("ok", tables=[t])])
        self.run_render(doc)
        first = (self.dbmeta_dir / "ok" / "tables" / "t1.sql").read_bytes()
        self.run_render(doc)
        second = (self.dbmeta_dir / "ok" / "tables" / "t1.sql").read_bytes()
        self.assertEqual(first, second)
        self.assertIn(b'"weird\ntype"', first)

    def test_plain_comment_mentioning_db_dict_not_falsely_flagged(self):
        # A comment merely mentioning "-- pg-dict" in prose (not shaped exactly
        # like a full marker line) MUST NOT be rejected.
        t = make_full_table("t1", comment="见 -- pg-dict 说明")
        result = self.run_render(v1_doc([schema_obj("ok", tables=[t])]))
        self.assertIn("ok", result["schemas"])

    def test_block_body_line_with_marker_text_not_at_line_start_not_treated_as_end(self):
        # [spec-review-amendment Q2] SQL_BLOCK_RE's `^`/MULTILINE anchor: a
        # line that merely CONTAINS marker-shaped text but doesn't START with
        # it (e.g. produced by `_comment_lines` prefixing an embedded marker
        # line with "-- ", yielding "-- -- pg-dict:...") is not mistaken for
        # the block's real end marker on the next merge.
        existing = (
            "-- pg-dict:table:t:start\n"
            "x -- pg-dict:table:t:end\n"
            "real body\n"
            "-- pg-dict:table:t:end\n"
        )
        segments = render.parse_segments(existing, render.SQL_SYNTAX)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0][0], "block")
        self.assertIn("x -- pg-dict:table:t:end", segments[0][2])
        self.assertIn("real body", segments[0][2])


# ---------------------------------------------------------------------------
# Task 3 (design.md DD-5, Q1 拍板): quote_ident — PG quote_identifier() port.
#
# Independent source of truth: real PG 18.0 dev DB (2026-08-29), queried via
#   `SELECT word FROM pg_get_keywords() WHERE catcode <> 'U' ORDER BY word;`
# over read-only creds (llm_readonly role, PgBouncer :6432) — NOT recomputed from
# render.py's own PG_KEYWORDS_QUOTED set (that would be tautological). Result:
# 164 words total (R 78 + T 23 + C 63), matching design.md DD-5's own count.
# The individual word/quoting checks below cross a handful of those words
# against psql's own \d output behavior (design.md's "真库对照" examples).
# ---------------------------------------------------------------------------


class QuoteIdentTests(unittest.TestCase):
    def test_reserved_and_typefunc_and_colname_keywords_are_quoted(self):
        # order/user: reserved (R); check/interval: type-func-name (T);
        # int/between: col-name (C) — all four catcodes' quoting behavior is
        # exercised across these six real keywords (真库 pg_get_keywords()).
        for kw in ("order", "user", "check", "interval", "int", "between"):
            self.assertEqual(render.quote_ident(kw), f'"{kw}"')

    def test_non_keyword_lowercase_name_is_bare(self):
        self.assertEqual(render.quote_ident("name"), "name")

    def test_digit_leading_name_is_quoted(self):
        self.assertEqual(render.quote_ident("2abc"), '"2abc"')

    def test_dotted_name_is_quoted(self):
        # A single identifier component that literally contains a "." (e.g. the
        # golang-migrate table `example.schema_migrations`) fails the bare-ident
        # pattern (no "." in `^[a-z_][a-z0-9_]*$`) and must be quoted whole —
        # NOT split on the dot (that split is sequence_names_from_defaults's
        # job, a different call site).
        self.assertEqual(
            render.quote_ident("example.schema_migrations"), '"example.schema_migrations"'
        )

    def test_uppercase_name_is_quoted(self):
        self.assertEqual(render.quote_ident("Users"), '"Users"')

    def test_embedded_double_quote_is_doubled(self):
        self.assertEqual(render.quote_ident('a"b'), '"a""b"')

    def test_keyword_set_has_164_words_matching_real_db_query(self):
        # Anti-tautology: compares against the literal count from the real-DB
        # query (see class docstring), not against len() of the same constant.
        self.assertEqual(len(render.PG_KEYWORDS_QUOTED), 164)


class SqlLiteralTests(unittest.TestCase):
    def test_plain_string_wrapped_in_single_quotes(self):
        self.assertEqual(render.sql_literal("手机号（唯一）"), "'手机号（唯一）'")

    def test_embedded_single_quote_is_doubled(self):
        self.assertEqual(render.sql_literal("it's ok"), "'it''s ok'")

    # relation-ddl-equivalence T2.1 (decision-memo C5): a value containing a
    # backslash switches to an E-string with BOTH `\` and `'` doubled.
    def test_backslash_switches_to_e_string_with_both_escaped(self):
        self.assertEqual(
            render.sql_literal("C:\\data\\it's"), "E'C:\\\\data\\\\it''s'"
        )

    def test_no_backslash_stays_plain_quoted(self):
        self.assertEqual(render.sql_literal("无反斜杠 it's"), "'无反斜杠 it''s'")


class CommentLinesTests(unittest.TestCase):
    """relation-ddl-equivalence T2.1/T2.2: `_comment_lines` — the single choke
    point for catalog text (which may embed newlines) landing inside a `--`
    comment."""

    def test_single_line_text_unchanged_from_bare_dash_dash_splice(self):
        self.assertEqual(render._comment_lines("hello"), ["-- hello"])

    def test_empty_text_returns_one_bare_comment_line(self):
        self.assertEqual(render._comment_lines(""), ["-- "])

    def test_embedded_lf_splits_into_two_prefixed_lines(self):
        self.assertEqual(render._comment_lines("a\nb"), ["-- a", "-- b"])

    def test_embedded_crlf_and_cr_also_split(self):
        self.assertEqual(render._comment_lines("a\r\nb\rc"), ["-- a", "-- b", "-- c"])


# ---------------------------------------------------------------------------
# Task 3 (design.md DD-3 rule 1 / DD-5 [spec-review-amendment]):
# sequence_names_from_defaults — extracts + quote_ident-qualifies the sequence
# each column's `nextval(...)` default references.
#
# Independent source of truth: .dbmeta/auth/_collect.json (real db-collect
# output, checked into this repo) — `auth.users.id`'s actual default is
# `nextval('auth.users_id_seq'::regclass)`, i.e. ALREADY schema-qualified —
# confirming memo C4's "16 序列全部为 auth.x_id_seq 形态" claim independently of
# this ticket's own implementation.
# ---------------------------------------------------------------------------


class SequenceNamesFromDefaultsTests(unittest.TestCase):
    def test_schema_qualified_default_not_double_qualified(self):
        # Real shape from .dbmeta/auth/_collect.json auth.users.id.
        columns = [{"name": "id", "default": "nextval('auth.users_id_seq'::regclass)"}]
        self.assertEqual(
            render.sequence_names_from_defaults(columns, "auth"), ["auth.users_id_seq"]
        )

    def test_bare_default_gets_schema_prefix(self):
        columns = [{"name": "id", "default": "nextval('x_id_seq'::regclass)"}]
        self.assertEqual(
            render.sequence_names_from_defaults(columns, "auth"), ["auth.x_id_seq"]
        )

    def test_dedup_preserves_first_column_order(self):
        columns = [
            {"name": "a", "default": "nextval('auth.shared_seq'::regclass)"},
            {"name": "b", "default": "nextval('x_id_seq'::regclass)"},
            {"name": "c", "default": "nextval('auth.shared_seq'::regclass)"},
        ]
        self.assertEqual(
            render.sequence_names_from_defaults(columns, "auth"),
            ["auth.shared_seq", "auth.x_id_seq"],
        )

    def test_non_nextval_default_ignored(self):
        columns = [
            {"name": "id", "default": "now()"},
            {"name": "flag", "default": "1"},
            {"name": "no_default", "default": None},
        ]
        self.assertEqual(render.sequence_names_from_defaults(columns, "auth"), [])

    def test_keyword_schema_and_bare_seq_name_both_quoted(self):
        columns = [{"name": "id", "default": "nextval('order_id_seq'::regclass)"}]
        self.assertEqual(
            render.sequence_names_from_defaults(columns, "order"), ['"order".order_id_seq']
        )


# ---------------------------------------------------------------------------
# Task 3/4 (design.md DD-3 「触发器函数：<link>（function_file_link 扩展名改
# .sql）」): function_file_link / resolve_trigger_link's `ext` param, added in
# Task 3 so the SQL DDL renderer could request `.sql` links. Task 4 wired
# render_table_ddl's own call site to pass `ext=".sql"` explicitly (see
# TriggerFunctionLinkTests above). T30: the old `.md` default had no
# production caller left, so `ext` is now a required parameter.
# ---------------------------------------------------------------------------


class FunctionFileLinkExtParamTests(unittest.TestCase):
    def test_ext_is_required(self):
        with self.assertRaises(TypeError):
            render.function_file_link("auth", "auth", "fn_x")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            render.resolve_trigger_link("auth", "CREATE TRIGGER x", {})  # type: ignore[call-arg]

    def test_sql_ext_same_schema(self):
        self.assertEqual(
            render.function_file_link("auth", "auth", "fn_x", ext=".sql"),
            "../functions/fn_x.sql",
        )

    def test_sql_ext_cross_schema(self):
        self.assertEqual(
            render.function_file_link("auth", "logs", "fn_x", ext=".sql"),
            "../../logs/functions/fn_x.sql",
        )


# ---------------------------------------------------------------------------
# Task 3 (design.md DD-3): render_table_ddl — pure function, collect table dict
# -> executable DDL text. Each test below checks ONE statement/rule from the
# fixed six-part order (memo D8); independent source of truth = design.md's
# own literal wording + real shapes cross-checked against
# .dbmeta/auth/_collect.json (e.g. auth.users' users_pkey appearing in BOTH
# indexes[] and constraints[] — memo C3).
# ---------------------------------------------------------------------------


def ddl_table(
    name,
    columns=None,
    comment=None,
    kind=None,
    reltuples=None,
    indexes=None,
    constraints=None,
    triggers=None,
    partition_key=None,
    options=None,
    tablespace=None,
    access_method=None,
    foreign=None,
):
    t = make_full_table(
        name,
        columns=columns,
        comment=comment,
        kind=kind,
        reltuples=reltuples,
        indexes=indexes,
        constraints=constraints,
        triggers=triggers,
        options=options,
        tablespace=tablespace,
        access_method=access_method,
        foreign=foreign,
    )
    if partition_key is not None:
        t["partition_key"] = partition_key
    return t


class TableDdlSequenceAndCreateTableTests(unittest.TestCase):
    def test_sequence_statement_precedes_create_table_no_double_qualify(self):
        # Real shape: .dbmeta/auth/_collect.json auth.users.id.
        cols = [
            {
                "name": "id",
                "type": "bigint",
                "default": "nextval('auth.users_id_seq'::regclass)",
                "nullable": False,
                "comment": None,
            },
        ]
        table = ddl_table("users", columns=cols)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("CREATE SEQUENCE IF NOT EXISTS auth.users_id_seq;", ddl)
        self.assertNotIn("auth.auth.users_id_seq", ddl)
        seq_pos = ddl.index("CREATE SEQUENCE")
        table_pos = ddl.index("CREATE TABLE")
        self.assertLess(seq_pos, table_pos)

    def test_column_default_and_not_null_rendered(self):
        cols = [
            {"name": "id", "type": "bigint", "default": None, "nullable": False, "comment": None},
            {"name": "note", "type": "text", "default": "''::text", "nullable": True, "comment": None},
        ]
        table = ddl_table("t1", columns=cols)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("CREATE TABLE auth.t1 (", ddl)
        self.assertIn("id bigint NOT NULL", ddl)
        self.assertIn("note text DEFAULT ''::text", ddl)
        self.assertNotIn("note text DEFAULT ''::text NOT NULL", ddl)

    def test_partition_by_clause_appended(self):
        table = ddl_table("audit", kind="partitioned_table", partition_key="RANGE (created_at)")
        ddl = render.render_table_ddl("logs", table, [], None, {})
        self.assertIn(") PARTITION BY RANGE (created_at);", ddl)

    def test_no_partition_key_no_partition_by_clause(self):
        table = ddl_table("users")
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertNotIn("PARTITION BY", ddl)

    def test_foreign_table_kind_header_comment_no_longer_present(self):
        # [spec-review-amendment] relation-ddl-equivalence T2.5 replaces the old
        # "still assembled like a plain table" placeholder comment with a real
        # `CREATE FOREIGN TABLE ... SERVER ...` statement — this line MUST NOT
        # appear any more (reversed from the pre-T2 assertion; renamed to
        # reflect the new semantics).
        table = ddl_table("ext_t", kind="foreign_table", foreign={"server": "srv", "options": []})
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertNotIn("-- kind: foreign_table", ddl)
        self.assertNotIn("仍按普通表拼装", ddl)

    def test_keyword_schema_and_table_name_quoted(self):
        table = ddl_table("order")
        ddl = render.render_table_ddl("public", table, [], None, {})
        self.assertIn('CREATE TABLE public."order" (', ddl)

    def test_dotted_table_name_quoted_whole_not_split(self):
        table = ddl_table("example.schema_migrations")
        ddl = render.render_table_ddl("public", table, [], None, {})
        self.assertIn('CREATE TABLE public."example.schema_migrations" (', ddl)

    def test_all_six_sections_render_in_design_md_dd3_order(self):
        # [impl-review-fix] design.md DD-3 (memo D8) fixes the six-section
        # statement order as: 1 CREATE SEQUENCE -> 2 CREATE TABLE (+PARTITION
        # BY) -> 3 ALTER TABLE ADD CONSTRAINT (every contype, sorted by name)
        # -> 4 CREATE INDEX -> 5 triggers[].definition + enable-state ALTER
        # -> 6 COMMENT ON TABLE/COLUMN. The two pre-existing assertLess checks
        # only pin two *local* pairs (seq<CREATE TABLE; constraint a<z); this
        # test is the one composite fixture asserting all six macro sections'
        # *relative* order in a single chained assertion, independent of the
        # renderer's own section-numbering (truth source: design.md DD-3
        # text, not a recomputation of render_table_ddl's internals).
        cols = [
            {
                "name": "id",
                "type": "bigint",
                "default": "nextval('auth.t1_id_seq'::regclass)",
                "nullable": False,
                "comment": "主键",
            },
        ]
        cons = [{"name": "t1_pkey", "type": "p", "definition": "PRIMARY KEY (id)", "is_local": True}]
        idx = [
            {
                "name": "idx_t1_id",
                "definition": "CREATE INDEX idx_t1_id ON auth.t1 USING btree (id)",
                "inherited_from": None,
            }
        ]
        trg = [make_trigger("trg_t1", "fn_x", enabled="O")]
        table = ddl_table("t1", columns=cols, comment="表注释", indexes=idx, constraints=cons, triggers=trg)
        ddl = render.render_table_ddl("auth", table, [], None, {})

        seq_pos = ddl.index("CREATE SEQUENCE")
        table_pos = ddl.index("CREATE TABLE")
        constraint_pos = ddl.index("ALTER TABLE auth.t1 ADD CONSTRAINT")
        index_pos = ddl.index("CREATE INDEX idx_t1_id")
        trigger_pos = ddl.index(trg[0]["definition"])
        comment_table_pos = ddl.index("COMMENT ON TABLE")
        comment_column_pos = ddl.index("COMMENT ON COLUMN")

        self.assertLess(seq_pos, table_pos)
        self.assertLess(table_pos, constraint_pos)
        self.assertLess(constraint_pos, index_pos)
        self.assertLess(index_pos, trigger_pos)
        self.assertLess(trigger_pos, comment_table_pos)
        self.assertLess(comment_table_pos, comment_column_pos)


class ForeignTableDdlRenderTests(unittest.TestCase):
    """relation-ddl-equivalence T2.5④/2.8④: `kind == "foreign_table"` renders
    `CREATE FOREIGN TABLE ... SERVER ...[ OPTIONS (...)]` instead of a plain
    `CREATE TABLE`, with column-level `fdw_options` as an `OPTIONS (...)`
    clause between the column's type and DEFAULT/NOT NULL — no PARTITION BY/
    USING/WITH/TABLESPACE/CREATE INDEX; ALTER TABLE ADD CONSTRAINT/CREATE
    TRIGGER/COMMENT sections are unchanged (design.md「数据流图」)."""

    def test_full_foreign_table_shape(self):
        columns = [
            {
                "name": "id",
                "type": "integer",
                "default": None,
                "nullable": False,
                "comment": None,
                "fdw_options": ["column_name=ID"],
            },
            {
                "name": "note",
                "type": "text",
                "default": None,
                "nullable": True,
                "comment": None,
                "fdw_options": [],
            },
        ]
        constraints = [
            {"name": "note_check", "type": "c", "definition": "CHECK (note IS NOT NULL)", "is_local": True},
        ]
        table = ddl_table(
            "ft",
            columns=columns,
            kind="foreign_table",
            comment="外部表",
            constraints=constraints,
            foreign={"server": "srv", "options": ["schema_name=remote", "table_name=T"]},
        )
        ddl = render.render_table_ddl("s", table, [], None, {})

        self.assertIn("CREATE FOREIGN TABLE s.ft (", ddl)
        self.assertIn("  id integer OPTIONS (column_name 'ID') NOT NULL,", ddl)
        self.assertIn("  note text", ddl)
        self.assertIn(") SERVER srv OPTIONS (schema_name 'remote', table_name 'T');", ddl)
        self.assertIn("ALTER TABLE s.ft ADD CONSTRAINT note_check CHECK (note IS NOT NULL);", ddl)
        self.assertIn("COMMENT ON TABLE s.ft IS '外部表';", ddl)

        self.assertNotIn("CREATE TABLE", ddl)
        self.assertNotIn("WITH (", ddl)
        self.assertNotIn("TABLESPACE", ddl)
        self.assertNotIn("USING", ddl)
        self.assertNotIn("CREATE INDEX", ddl)
        self.assertNotIn("仍按普通表拼装", ddl)

    def test_no_foreign_options_ends_with_server_only(self):
        table = ddl_table("ft", kind="foreign_table", foreign={"server": "srv", "options": []})
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn(") SERVER srv;", ddl)
        self.assertNotIn("OPTIONS", ddl)

    def test_server_and_option_names_requiring_quotes_are_quoted(self):
        table = ddl_table(
            "ft",
            kind="foreign_table",
            foreign={"server": "My-Srv", "options": ["column-name=x"]},
        )
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn('SERVER "My-Srv" OPTIONS ("column-name" \'x\');', ddl)

    # relation-ddl-equivalence-r2 T2.8⑤: a column-level fdw_options value
    # containing a backslash switches its OPTIONS(...) item to E-string form
    # — same sql_literal choke point as table/column COMMENT, exercised here
    # through the foreign-table OPTIONS path specifically.
    def test_column_fdw_option_with_backslash_uses_e_string(self):
        columns = [
            {
                "name": "id",
                "type": "text",
                "default": None,
                "nullable": True,
                "comment": None,
                "fdw_options": ["filename=C:\\tmp\\a.csv"],
            },
        ]
        table = ddl_table(
            "ft",
            columns=columns,
            kind="foreign_table",
            foreign={"server": "srv", "options": []},
        )
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn("OPTIONS (filename E'C:\\\\tmp\\\\a.csv')", ddl)


class TableDdlOptionsAndTablespaceTests(unittest.TestCase):
    """relation-ddl-equivalence T2.5 ①②④⑨: `options`/`tablespace` round-trip
    into `CREATE TABLE ... [WITH (...)][ TABLESPACE ts];` — design.md「数据流图」
    render_table_ddl 一行。"""

    def test_options_and_tablespace_render_with_and_tablespace_clauses(self):
        table = ddl_table(
            "t1", options=["autovacuum_enabled=false", "fillfactor=70"], tablespace="fast"
        )
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn(
            ") WITH (autovacuum_enabled='false', fillfactor='70') TABLESPACE fast;", ddl
        )

    def test_partitioned_parent_tablespace_after_partition_by(self):
        table = ddl_table(
            "audit", kind="partitioned_table", partition_key="RANGE (id)", tablespace="fast"
        )
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn(") PARTITION BY RANGE (id) TABLESPACE fast;", ddl)

    def test_no_options_no_tablespace_byte_identical_to_baseline(self):
        with_defaults = render.render_table_ddl("auth", ddl_table("users"), [], None, {})
        without_new_keys = render.render_table_ddl("auth", make_table("users"), [], None, {})
        self.assertEqual(with_defaults, without_new_keys)
        self.assertNotIn("WITH (", with_defaults)
        self.assertNotIn("TABLESPACE", with_defaults)

    def test_tablespace_name_requiring_quotes_is_quoted(self):
        table = ddl_table("t1", tablespace="Fast-1")
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn('TABLESPACE "Fast-1";', ddl)

    # relation-ddl-equivalence T2.8①: toast.* storage params round-trip through
    # the SAME `options`/WITH path as any other reloption — no render change.
    def test_toast_option_renders_in_with_clause_alongside_plain_option(self):
        table = ddl_table("t1", options=["fillfactor=70", "toast.autovacuum_enabled=false"])
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn(") WITH (fillfactor='70', toast.autovacuum_enabled='false');", ddl)

    # relation-ddl-equivalence T2.8②: non-heap access_method -> ` USING <am>`,
    # positioned after PARTITION BY / before WITH (design.md 数据流图 order).
    def test_non_heap_access_method_renders_using_clause(self):
        table = ddl_table("t1", access_method="columnar", options=["fillfactor=70"])
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn(") USING columnar WITH (fillfactor='70');", ddl)

    def test_partitioned_parent_using_clause_after_partition_by(self):
        table = ddl_table(
            "audit", kind="partitioned_table", partition_key="RANGE (id)", access_method="columnar"
        )
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn(") PARTITION BY RANGE (id) USING columnar;", ddl)

    # relation-ddl-equivalence T2.8③: heap / None access_method -> byte-
    # identical output, no USING clause at all.
    def test_heap_access_method_byte_identical_no_using(self):
        heap_table = ddl_table("t1", access_method="heap")
        none_table = ddl_table("t1", access_method=None)
        baseline = ddl_table("t1")
        ddl_heap = render.render_table_ddl("s", heap_table, [], None, {})
        ddl_none = render.render_table_ddl("s", none_table, [], None, {})
        ddl_baseline = render.render_table_ddl("s", baseline, [], None, {})
        self.assertEqual(ddl_heap, ddl_baseline)
        self.assertEqual(ddl_none, ddl_baseline)
        self.assertNotIn("USING", ddl_heap)


class TableDdlConstraintIndexTests(unittest.TestCase):
    def test_add_constraint_rendered_for_every_contype_sorted_by_name(self):
        cons = [
            {"name": "z_check", "type": "c", "definition": "CHECK (id > 0)", "is_local": True},
            {"name": "a_pkey", "type": "p", "definition": "PRIMARY KEY (id)", "is_local": True},
        ]
        table = ddl_table("t1", constraints=cons)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        a_pos = ddl.index("ADD CONSTRAINT a_pkey")
        z_pos = ddl.index("ADD CONSTRAINT z_check")
        self.assertLess(a_pos, z_pos)
        self.assertIn("ALTER TABLE auth.t1 ADD CONSTRAINT a_pkey PRIMARY KEY (id);", ddl)
        self.assertIn("ALTER TABLE auth.t1 ADD CONSTRAINT z_check CHECK (id > 0);", ddl)

    def test_constraint_name_quoted_when_needed(self):
        cons = [{"name": "Order", "type": "c", "definition": "CHECK (true)", "is_local": True}]
        table = ddl_table("t1", constraints=cons)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn('ADD CONSTRAINT "Order" CHECK (true);', ddl)

    def test_pkey_backed_index_not_duplicated_as_create_index(self):
        # Real shape: .dbmeta/auth/_collect.json auth.users — users_pkey appears
        # in BOTH indexes[] and constraints[] (memo C3).
        idx = [{"name": "users_pkey", "definition": "CREATE UNIQUE INDEX users_pkey ON auth.users USING btree (id)", "inherited_from": None}]
        cons = [{"name": "users_pkey", "type": "p", "definition": "PRIMARY KEY (id)", "is_local": True}]
        table = ddl_table("users", indexes=idx, constraints=cons)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertEqual(ddl.count("CREATE UNIQUE INDEX users_pkey"), 0)
        self.assertIn("ADD CONSTRAINT users_pkey PRIMARY KEY (id);", ddl)

    def test_non_constraint_backed_index_rendered_verbatim(self):
        idx = [{"name": "idx_phone", "definition": "CREATE INDEX idx_phone ON auth.users USING btree (phone)", "inherited_from": None}]
        table = ddl_table("users", indexes=idx)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("CREATE INDEX idx_phone ON auth.users USING btree (phone);", ddl)

    def test_inherited_index_skipped(self):
        idx = [{"name": "child_pkey", "definition": "CREATE UNIQUE INDEX child_pkey ...", "inherited_from": "parent_pkey"}]
        table = ddl_table("child", indexes=idx)
        ddl = render.render_table_ddl("logs", table, [], None, {})
        self.assertNotIn("child_pkey", ddl)


class TableDdlIndexTablespaceAndOptionsAlterTests(unittest.TestCase):
    """relation-ddl-equivalence T2.5 ③⑩: non-constraint index `tablespace` ->
    `ALTER INDEX ... SET TABLESPACE ...;` right after its `CREATE INDEX ...;`
    line; p/u/x-constraint-backed index `tablespace`/`options` -> the same
    `ALTER INDEX` lines right after the `ADD CONSTRAINT` statement (design.md
    Decisions/Risks — `pg_get_constraintdef()` carries no `USING INDEX
    TABLESPACE`, so the constraint-backed index's own indexes[] entry is
    consulted by name instead)."""

    def test_index_tablespace_renders_alter_index_next_line(self):
        idx = [
            {"name": "idx_a", "definition": "CREATE INDEX idx_a ON s.t USING btree (a)", "tablespace": "fast"},
            {"name": "idx_b", "definition": "CREATE INDEX idx_b ON s.t USING btree (b)", "tablespace": None},
        ]
        table = ddl_table("t", indexes=idx)
        ddl = render.render_table_ddl("s", table, [], None, {})
        lines = ddl.splitlines()
        a_pos = lines.index("CREATE INDEX idx_a ON s.t USING btree (a);")
        self.assertEqual(lines[a_pos + 1], "ALTER INDEX s.idx_a SET TABLESPACE fast;")
        self.assertIn("CREATE INDEX idx_b ON s.t USING btree (b);", ddl)
        self.assertNotIn("idx_b SET TABLESPACE", ddl)

    def test_constraint_backed_index_tablespace_and_options_alter_after_add_constraint(self):
        cons = [
            {"name": "t_pkey", "type": "p", "definition": "PRIMARY KEY (id)", "is_local": True},
            {"name": "t_uniq", "type": "u", "definition": "UNIQUE (email)", "is_local": True},
        ]
        idx = [
            {
                "name": "t_pkey",
                "definition": "CREATE UNIQUE INDEX t_pkey ON s.t USING btree (id)",
                "tablespace": "fast",
                "options": ["fillfactor=90"],
            },
            {
                "name": "t_uniq",
                "definition": "CREATE UNIQUE INDEX t_uniq ON s.t USING btree (email)",
                "tablespace": None,
                "options": [],
            },
            {
                "name": "hits_idx",
                "definition": "CREATE INDEX hits_idx ON s.t USING btree (hits)",
                "tablespace": None,
                "options": ["fillfactor=80"],
            },
        ]
        table = ddl_table("t", indexes=idx, constraints=cons)
        ddl = render.render_table_ddl("s", table, [], None, {})
        lines = ddl.splitlines()

        pkey_pos = lines.index('ALTER TABLE s.t ADD CONSTRAINT t_pkey PRIMARY KEY (id);')
        self.assertEqual(lines[pkey_pos + 1], "ALTER INDEX s.t_pkey SET TABLESPACE fast;")
        self.assertEqual(lines[pkey_pos + 2], "ALTER INDEX s.t_pkey SET (fillfactor='90');")

        uniq_pos = lines.index("ALTER TABLE s.t ADD CONSTRAINT t_uniq UNIQUE (email);")
        self.assertNotEqual(lines[uniq_pos + 1], "ALTER INDEX s.t_uniq SET TABLESPACE fast;")
        self.assertNotIn("t_uniq SET", ddl)

        self.assertIn("CREATE INDEX hits_idx ON s.t USING btree (hits);", ddl)
        self.assertNotIn("hits_idx SET (", ddl)
        self.assertEqual(ddl.count("CREATE INDEX"), 1)  # constraint-backed indexes never get CREATE INDEX


class TableDdlTriggerTests(unittest.TestCase):
    def test_trigger_function_link_same_schema(self):
        trg = [make_trigger("trg_audit", "fn_audit", enabled="O")]
        table = ddl_table("users", triggers=trg)
        func_index = {"auth": {"fn_audit": [""]}}
        ddl = render.render_table_ddl("auth", table, [], None, func_index)
        self.assertIn("-- 触发器函数：../functions/fn_audit.sql", ddl)
        self.assertIn(trg[0]["definition"] + ";", ddl)

    def test_trigger_function_link_cross_schema(self):
        trg = [make_trigger("trg_audit", "logs.fn_write_audit", enabled="O")]
        table = ddl_table("users", triggers=trg)
        func_index = {"logs": {"fn_write_audit": [""]}}
        ddl = render.render_table_ddl("auth", table, [], None, func_index)
        self.assertIn("-- 触发器函数：../../logs/functions/fn_write_audit.sql", ddl)

    def test_trigger_function_not_in_collect_set_name_only(self):
        trg = [make_trigger("trg_ext", "some_extension_fn", enabled="O")]
        table = ddl_table("users", triggers=trg)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("-- 触发器函数：some_extension_fn", ddl)

    def test_disabled_trigger_gets_alter_disable_statement(self):
        trg = [make_trigger("trg_d", "fn_x", enabled="D")]
        table = ddl_table("users", triggers=trg)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("ALTER TABLE auth.users DISABLE TRIGGER trg_d;", ddl)

    def test_replica_trigger_gets_alter_enable_replica_statement(self):
        trg = [make_trigger("trg_r", "fn_x", enabled="R")]
        table = ddl_table("users", triggers=trg)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("ALTER TABLE auth.users ENABLE REPLICA TRIGGER trg_r;", ddl)

    def test_always_trigger_gets_alter_enable_always_statement(self):
        trg = [make_trigger("trg_a", "fn_x", enabled="A")]
        table = ddl_table("users", triggers=trg)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("ALTER TABLE auth.users ENABLE ALWAYS TRIGGER trg_a;", ddl)

    def test_enabled_trigger_gets_no_extra_alter_statement(self):
        trg = [make_trigger("trg_o", "fn_x", enabled="O")]
        table = ddl_table("users", triggers=trg)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertNotIn("ENABLE ALWAYS TRIGGER", ddl)
        self.assertNotIn("ENABLE REPLICA TRIGGER", ddl)
        self.assertNotIn("DISABLE TRIGGER", ddl)


class TableDdlCommentTests(unittest.TestCase):
    def test_comment_on_table_and_column_with_quote_escaping(self):
        cols = [
            {"name": "id", "type": "bigint", "default": None, "nullable": False, "comment": "it's the PK"},
        ]
        table = ddl_table("users", columns=cols, comment="用户's表")
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("COMMENT ON TABLE auth.users IS '用户''s表';", ddl)
        self.assertIn("COMMENT ON COLUMN auth.users.id IS 'it''s the PK';", ddl)

    def test_no_comment_no_comment_statement(self):
        table = ddl_table("users")
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertNotIn("COMMENT ON", ddl)

    # relation-ddl-equivalence T2.8⑤ (decision-memo C5): a comment value
    # containing a backslash switches its COMMENT ON literal to E-string form.
    def test_comment_with_backslash_uses_e_string(self):
        cols = [
            {
                "name": "id",
                "type": "bigint",
                "default": None,
                "nullable": False,
                "comment": None,
            },
        ]
        table = ddl_table("t", columns=cols, comment="路径 C:\\data\\it's")
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn("COMMENT ON TABLE s.t IS E'路径 C:\\\\data\\\\it''s';", ddl)

    def test_comment_without_backslash_unchanged_plain_quoted(self):
        table = ddl_table("t", comment="无反斜杠 it's")
        ddl = render.render_table_ddl("s", table, [], None, {})
        self.assertIn("COMMENT ON TABLE s.t IS '无反斜杠 it''s';", ddl)


class TableDdlPartitionAndOrphanTests(unittest.TestCase):
    def test_partition_children_listed_as_comment_no_partition_of(self):
        parent = ddl_table("audit")
        children = [make_full_table(f"audit_2026_{m:02d}", partition_of="audit") for m in range(1, 3)]
        ddl = render.render_table_ddl("logs", parent, children, None, {})
        self.assertIn("-- 分区子表（2）", ddl)
        self.assertIn("audit_2026_01", ddl)
        self.assertNotIn("PARTITION OF", ddl)

    def test_orphan_note_rendered_as_comment(self):
        table = ddl_table("audit_2026_01")
        ddl = render.render_table_ddl("logs", table, [], "分区父表 `audit` 不在本 schema / 为中间分区，按独立表渲染", {})
        self.assertIn("-- 分区父表 `audit` 不在本 schema / 为中间分区，按独立表渲染", ddl)

    def test_reltuples_note_present(self):
        table = ddl_table("users", reltuples=42)
        ddl = render.render_table_ddl("auth", table, [], None, {})
        self.assertIn("行数估计", ddl)
        self.assertIn("）：<100", ddl)

    def test_format_reltuples_buckets(self):
        # T32: one label per order of magnitude; boundaries are exclusive upper bounds.
        cases = [
            (None, "0"), ("x", "0"), (-5, "0"), (0, "0"),
            (1, "<100"), (99, "<100"),
            (100, "百级"), (999, "百级"),
            (1_000, "千级"), (9_999, "千级"),
            (10_000, "万级"), (99_999, "万级"),
            (100_000, "十万级"), (1_000_000, "百万级"), (10_000_000, "千万级"),
            (100_000_000, "亿级"), (999_999_999, "亿级"),
            (1_000_000_000, "十亿级+"), (10**12, "十亿级+"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(render.format_reltuples(value), expected)


# ---------------------------------------------------------------------------
# Task 3 (tasks.md 3.2 / design.md 「render_partition_children_lines 改 --
# 前缀」): every non-blank line render_partition_children_lines produces MUST
# now be its own valid `--`-comment line, since (post Task 4 wiring) it will
# sit inside an executable .sql file's DDL body rather than a markdown blob.
# ---------------------------------------------------------------------------


class PartitionChildrenLinesDashPrefixTests(unittest.TestCase):
    def test_every_non_blank_line_starts_with_dash_dash(self):
        children = [
            make_full_table("audit_2026_01", partition_of="audit"),
            make_full_table(
                "audit_2026_02",
                partition_of="audit",
                indexes=[{"name": "extra_idx", "definition": "CREATE INDEX extra_idx ON logs.audit_2026_02 (x)"}],
            ),
        ]
        lines = render.render_partition_children_lines(children)
        for line in lines:
            if line.strip():
                self.assertTrue(line.startswith("--"), f"not a comment line: {line!r}")
        joined = "\n".join(lines)
        self.assertIn("-- 分区子表（2）：audit_2026_01, audit_2026_02", joined)
        self.assertIn("extra_idx", joined)


class PartitionChildrenLinesNewFieldDetailTests(unittest.TestCase):
    """relation-ddl-equivalence T2.4/2.8⑦ [spec-review-amendment Q1]: the
    single-column gate widens to options/tablespace/access_method/foreign-
    table-kind, each emitting its own `--`-prefixed detail line after the
    existing index/constraint lines."""

    def test_options_and_tablespace_detail_lines_no_indexes_or_constraints(self):
        child = make_full_table(
            "audit_2026_01", partition_of="audit", options=["fillfactor=60"], tablespace="fast"
        )
        lines = render.render_partition_children_lines([child])
        joined = "\n".join(lines)
        self.assertIn("-- `audit_2026_01`", joined)
        self.assertIn("--   存储参数 `fillfactor=60`", joined)
        self.assertIn("--   表空间 `fast`", joined)

    def test_no_options_no_tablespace_no_detail_lines(self):
        child = make_full_table("audit_2026_01", partition_of="audit")
        lines = render.render_partition_children_lines([child])
        joined = "\n".join(lines)
        self.assertIn("-- 分区子表（1）：audit_2026_01", joined)
        self.assertNotIn("`audit_2026_01`", joined)

    def test_columnar_access_method_only_line(self):
        child = make_full_table("audit_2026_01", partition_of="audit", access_method="columnar")
        lines = render.render_partition_children_lines([child])
        joined = "\n".join(lines)
        self.assertIn("-- `audit_2026_01`", joined)
        self.assertIn("--   访问方法 `columnar`", joined)
        self.assertNotIn("存储参数", joined)
        self.assertNotIn("表空间", joined)

    def test_heap_or_none_access_method_no_detail_line(self):
        for am in ("heap", None):
            with self.subTest(access_method=am):
                child = make_full_table("audit_2026_01", partition_of="audit", access_method=am)
                lines = render.render_partition_children_lines([child])
                joined = "\n".join(lines)
                self.assertNotIn("`audit_2026_01`", joined)
                self.assertNotIn("访问方法", joined)

    def test_foreign_table_child_server_and_options_line(self):
        child = make_full_table(
            "audit_2026_01",
            partition_of="audit",
            kind="foreign_table",
            foreign={"server": "srv", "options": ["schema_name=remote"]},
        )
        lines = render.render_partition_children_lines([child])
        joined = "\n".join(lines)
        self.assertIn("--   外部表 SERVER `srv` OPTIONS (schema_name 'remote')", joined)

    def test_foreign_table_child_no_options_server_only_line(self):
        child = make_full_table(
            "audit_2026_01",
            partition_of="audit",
            kind="foreign_table",
            foreign={"server": "srv", "options": []},
        )
        lines = render.render_partition_children_lines([child])
        joined = "\n".join(lines)
        self.assertIn("--   外部表 SERVER `srv`", joined)
        self.assertNotIn("OPTIONS", joined)

    def test_thirteen_month_partitions_still_a_single_summary_line(self):
        # [spec-review-amendment] pre-existing "13 个月分区只有一行清单" invariant
        # unchanged by the widened single-column gate.
        children = [
            make_full_table(f"audit_2026_{m:02d}", partition_of="audit") for m in range(1, 14)
        ]
        lines = render.render_partition_children_lines(children)
        joined = "\n".join(lines)
        self.assertIn("-- 分区子表（13）：", joined)
        self.assertNotIn("`audit_2026_01`", joined)


# ---------------------------------------------------------------------------
# Task 3 (design.md DD-4): render_function_ddl / render_view_ddl — pure
# functions, collect function/view dict -> executable DDL text.
#
# Independent source of truth for the "no trailing ; added" vs "; appended"
# distinction: real PG 18.0 dev DB (2026-08-29, read-only creds), queried directly —
#   `pg_get_functiondef('auth.notify_user_perms_change()'::regprocedure)`
#     -> ends in "$function$\n" with NO trailing ";" (must be appended, DD-4).
#   `pg_get_viewdef('pg_catalog.pg_tables'::regclass, true)`
#     -> ends in ");" already (a trailing ";" is PART of pg_get_viewdef's own
#     output — DD-5 "透传文本 MUST NOT 再处理" means render must NOT append a
#     second one).
# ---------------------------------------------------------------------------


class FunctionDdlRenderTests(unittest.TestCase):
    def test_definition_verbatim_plus_semicolon(self):
        fn = make_function("fn_hello", identity_args="p_id bigint", result_type="text")
        ddl = render.render_function_ddl("auth", fn, [])
        self.assertIn(fn["definition"] + ";", ddl)

    def test_comment_on_function_with_identity_args_and_schema(self):
        fn = make_function("fn_hello", identity_args="p_id bigint", comment="返回问候语")
        ddl = render.render_function_ddl("auth", fn, [])
        self.assertIn(
            "COMMENT ON FUNCTION auth.fn_hello(p_id bigint) IS '返回问候语';", ddl
        )

    def test_no_comment_no_comment_on_function_statement(self):
        fn = make_function("fn_hello")
        ddl = render.render_function_ddl("auth", fn, [])
        self.assertNotIn("COMMENT ON FUNCTION", ddl)

    def test_keyword_schema_or_name_quoted_in_comment_on_function(self):
        fn = make_function("order", comment="x")
        ddl = render.render_function_ddl("order", fn, [])
        self.assertIn('COMMENT ON FUNCTION "order"."order"() IS ', ddl)

    def test_trigger_reverse_reference_rendered_as_comment(self):
        fn = make_function("fn_audit")
        ddl = render.render_function_ddl("auth", fn, ["`auth.users.trg_audit`"])
        self.assertIn("-- 被以下触发器引用：", ddl)
        self.assertIn("-- `auth.users.trg_audit`", ddl)

    def test_no_triggers_shows_none(self):
        fn = make_function("fn_lonely")
        ddl = render.render_function_ddl("auth", fn, [])
        self.assertIn("-- 被以下触发器引用：", ddl)
        self.assertIn("-- （无）", ddl)


class ViewDdlRenderTests(unittest.TestCase):
    def test_plain_view_create_statement(self):
        view = make_view("v_active_users", definition="SELECT id FROM auth.users;")
        ddl = render.render_view_ddl("auth", view)
        self.assertIn("CREATE VIEW auth.v_active_users AS", ddl)
        self.assertIn("SELECT id FROM auth.users;", ddl)
        self.assertNotIn("SELECT id FROM auth.users;;", ddl)  # no extra ";" appended

    def test_materialized_view_uses_materialized_keyword(self):
        view = make_view("mv_stats", definition="SELECT 1;")
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("auth", view)
        self.assertIn("CREATE MATERIALIZED VIEW auth.mv_stats AS", ddl)
        self.assertNotIn("CREATE VIEW", ddl)

    def test_comment_on_view_rendered(self):
        view = make_view("v_active_users", comment="活跃用户视图", definition="SELECT 1;")
        ddl = render.render_view_ddl("auth", view)
        self.assertIn("COMMENT ON VIEW auth.v_active_users IS '活跃用户视图';", ddl)

    def test_materialized_view_comment_uses_materialized_view_keyword(self):
        view = make_view("mv_stats", comment="统计物化视图", definition="SELECT 1;")
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("auth", view)
        self.assertIn("COMMENT ON MATERIALIZED VIEW auth.mv_stats IS '统计物化视图';", ddl)

    def test_column_comment_rendered(self):
        view = make_view(
            "v_active_users",
            definition="SELECT id, nickname FROM auth.users;",
            columns=[
                {"position": 1, "name": "id", "type": "bigint", "nullable": False, "comment": None},
                {"position": 2, "name": "nickname", "type": "text", "nullable": True, "comment": "昵称"},
            ],
        )
        ddl = render.render_view_ddl("auth", view)
        self.assertIn("COMMENT ON COLUMN auth.v_active_users.nickname IS '昵称';", ddl)

    def test_keyword_schema_or_name_quoted(self):
        view = make_view("order", definition="SELECT 1;")
        ddl = render.render_view_ddl("order", view)
        self.assertIn('CREATE VIEW "order"."order" AS', ddl)

    def test_view_with_options_renders_with_clause(self):
        view = make_view(
            "v",
            definition="SELECT 1;",
            options=["check_option=cascaded", "security_barrier=true"],
        )
        ddl = render.render_view_ddl("auth", view)
        first_line = ddl.splitlines()[0]
        self.assertEqual(
            first_line,
            "CREATE VIEW auth.v WITH (check_option='cascaded', security_barrier='true') AS",
        )

    def test_materialized_view_with_options_renders_with_clause(self):
        view = make_view("mv_stats", definition="SELECT 1;", options=["fillfactor=70"])
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("auth", view)
        first_line = ddl.splitlines()[0]
        self.assertIn("MATERIALIZED VIEW auth.mv_stats WITH (fillfactor='70') AS", first_line)

    # relation-ddl-equivalence T2.8①: toast.* option round-trips through the
    # matview's existing options -> WITH path unchanged.
    def test_materialized_view_toast_option_renders_in_with_clause(self):
        view = make_view("mv_stats", definition="SELECT 1;", options=["toast.autovacuum_enabled=false"])
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("auth", view)
        first_line = ddl.splitlines()[0]
        self.assertEqual(
            first_line, "CREATE MATERIALIZED VIEW auth.mv_stats WITH (toast.autovacuum_enabled='false') AS"
        )

    # relation-ddl-equivalence T2.8②: non-heap access_method on a materialized
    # view -> ` USING <am>` before ` AS` (and before WITH, per grammar order).
    def test_materialized_view_non_heap_access_method_renders_using_clause(self):
        view = make_view("mv_stats", definition="SELECT 1;", access_method="columnar")
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("auth", view)
        first_line = ddl.splitlines()[0]
        self.assertEqual(first_line, "CREATE MATERIALIZED VIEW auth.mv_stats USING columnar AS")

    # relation-ddl-equivalence T2.8③: a plain (non-materialized) view ignores
    # `access_method` entirely — never emits USING.
    def test_plain_view_ignores_access_method(self):
        view = make_view("v", definition="SELECT 1;", access_method="columnar")
        ddl = render.render_view_ddl("auth", view)
        self.assertNotIn("USING", ddl)
        self.assertEqual(ddl.splitlines()[0], "CREATE VIEW auth.v AS")

    # relation-ddl-equivalence T2.8⑧: explicit byte-identical comparison for a
    # plain view carrying none of the new fields vs. one explicitly set to the
    # "no-op" defaults.
    def test_plain_view_no_new_fields_byte_identical_to_defaults(self):
        baseline = make_view("v_active_users", definition="SELECT 1;")
        explicit_defaults = make_view(
            "v_active_users", definition="SELECT 1;", access_method=None, options=[], tablespace=None
        )
        self.assertEqual(
            render.render_view_ddl("auth", baseline),
            render.render_view_ddl("auth", explicit_defaults),
        )

    # relation-ddl-equivalence T2.8③: heap / None access_method -> byte-
    # identical output for both plain and materialized views.
    def test_matview_heap_access_method_byte_identical_no_using(self):
        baseline = make_view("mv_stats", definition="SELECT 1;")
        baseline["kind"] = "materialized_view"
        heap_view = make_view("mv_stats", definition="SELECT 1;", access_method="heap")
        heap_view["kind"] = "materialized_view"
        none_view = make_view("mv_stats", definition="SELECT 1;", access_method=None)
        none_view["kind"] = "materialized_view"
        ddl_baseline = render.render_view_ddl("auth", baseline)
        ddl_heap = render.render_view_ddl("auth", heap_view)
        ddl_none = render.render_view_ddl("auth", none_view)
        self.assertEqual(ddl_heap, ddl_baseline)
        self.assertEqual(ddl_none, ddl_baseline)
        self.assertNotIn("USING", ddl_heap)

    def test_matview_with_tablespace_renders_tablespace_clause(self):
        view = make_view("mv_stats", definition="SELECT 1;")
        view["kind"] = "materialized_view"
        view["tablespace"] = "fast"
        ddl = render.render_view_ddl("auth", view)
        self.assertEqual(
            "CREATE MATERIALIZED VIEW auth.mv_stats TABLESPACE fast AS", ddl.splitlines()[0]
        )

    def test_plain_view_with_tablespace_omits_tablespace_clause(self):
        # T46: a plain VIEW never carries storage. A stale/hand-edited
        # {kind:view, tablespace:X} MUST NOT render `CREATE VIEW ... TABLESPACE`
        # (illegal SQL); the is_matview gate keeps output unchanged for views
        # (design invariant「kind=view ⇒ 输出不变」).
        view = make_view("v_active", definition="SELECT 1;")
        view["tablespace"] = "fast"
        ddl = render.render_view_ddl("auth", view)
        self.assertNotIn("TABLESPACE", ddl)
        self.assertEqual("CREATE VIEW auth.v_active AS", ddl.splitlines()[0])

    def test_view_options_empty_and_default_byte_identical_no_with(self):
        view_default = make_view("v", definition="SELECT 1;")
        del view_default["options"]  # simulate a collect JSON without the key at all (D3)
        view_empty = make_view("v", definition="SELECT 1;", options=[])
        ddl_default = render.render_view_ddl("auth", view_default)
        ddl_empty = render.render_view_ddl("auth", view_empty)
        self.assertEqual(ddl_default, ddl_empty)
        self.assertNotIn("WITH", ddl_default)

    def test_view_options_value_with_single_quote_escaped(self):
        view = make_view("v", definition="SELECT 1;", options=["x=it's"])
        ddl = render.render_view_ddl("auth", view)
        first_line = ddl.splitlines()[0]
        self.assertEqual(first_line, "CREATE VIEW auth.v WITH (x='it''s') AS")

    def test_view_options_element_without_equals_renders_no_value(self):
        view = make_view("v", definition="SELECT 1;", options=["name_only"])
        ddl = render.render_view_ddl("auth", view)
        first_line = ddl.splitlines()[0]
        self.assertEqual(first_line, "CREATE VIEW auth.v WITH (name_only) AS")


class MaterializedViewTablespaceAndUnpopulatedTests(unittest.TestCase):
    """relation-ddl-equivalence T2.5 ⑤⑥⑦⑧⑪: materialized-view `tablespace` /
    `populated=False` (+ WITH NO DATA + its indexes[]) / combined-four-clause
    rendering; plain `view` kind ignores `populated`/`indexes` entirely."""

    def test_unpopulated_matview_strips_trailing_semicolon_then_with_no_data_then_index(self):
        idx = [{"name": "mv_id_key", "definition": "CREATE UNIQUE INDEX mv_id_key ON s.mv USING btree (id)", "tablespace": None, "options": []}]
        view = make_view(
            "mv", definition="SELECT id FROM s.t;", populated=False, indexes=idx
        )
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("s", view)
        self.assertNotIn(";;", ddl)
        lines = ddl.splitlines()
        def_pos = lines.index("SELECT id FROM s.t")
        self.assertEqual(lines[def_pos + 1], "WITH NO DATA;")
        self.assertEqual(lines[def_pos + 2], "CREATE UNIQUE INDEX mv_id_key ON s.mv USING btree (id);")
        # no comment in this fixture (comment is None) -> no COMMENT ON line
        self.assertNotIn("COMMENT ON", ddl)

    def test_populated_matview_no_indexes_no_tablespace_byte_identical_to_baseline(self):
        view_before = make_view("mv", definition="SELECT 1;")
        view_before["kind"] = "materialized_view"
        view_after = make_view("mv", definition="SELECT 1;", populated=True, indexes=[], tablespace=None)
        view_after["kind"] = "materialized_view"
        ddl_before = render.render_view_ddl("s", view_before)
        ddl_after = render.render_view_ddl("s", view_after)
        self.assertEqual(ddl_before, ddl_after)
        self.assertNotIn("WITH NO DATA", ddl_before)
        self.assertNotIn("TABLESPACE", ddl_before)

    def test_matview_tablespace_renders_first_line(self):
        view = make_view("mv", definition="SELECT 1;", tablespace="fast")
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("s", view)
        first_line = ddl.splitlines()[0]
        self.assertEqual(first_line, "CREATE MATERIALIZED VIEW s.mv TABLESPACE fast AS")

    def test_matview_options_and_tablespace_first_line(self):
        view = make_view("mv", definition="SELECT 1;", options=["fillfactor=70"], tablespace="fast")
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("s", view)
        first_line = ddl.splitlines()[0]
        self.assertEqual(first_line, "CREATE MATERIALIZED VIEW s.mv WITH (fillfactor='70') TABLESPACE fast AS")

    def test_plain_view_ignores_populated_and_indexes(self):
        idx = [{"name": "would_be_ignored", "definition": "CREATE INDEX would_be_ignored ON s.v (id)", "tablespace": None, "options": []}]
        view = make_view("v", definition="SELECT 1;", populated=False, indexes=idx)
        ddl = render.render_view_ddl("s", view)
        self.assertNotIn("WITH NO DATA", ddl)
        self.assertNotIn("would_be_ignored", ddl)
        self.assertIn("SELECT 1;", ddl)

    def test_matview_index_tablespace_alter_index_line(self):
        idx = [{"name": "mv_idx", "definition": "CREATE INDEX mv_idx ON s.mv USING btree (a)", "tablespace": "fast", "options": []}]
        view = make_view("mv", definition="SELECT a FROM s.t;", indexes=idx)
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("s", view)
        lines = ddl.splitlines()
        idx_pos = lines.index("CREATE INDEX mv_idx ON s.mv USING btree (a);")
        self.assertEqual(lines[idx_pos + 1], "ALTER INDEX s.mv_idx SET TABLESPACE fast;")

    def test_all_four_clauses_together(self):
        idx = [{"name": "mv_idx", "definition": "CREATE UNIQUE INDEX mv_idx ON s.mv USING btree (id)", "tablespace": "fast", "options": []}]
        view = make_view(
            "mv",
            definition="SELECT id FROM s.t;",
            options=["fillfactor=50"],
            tablespace="fast",
            populated=False,
            indexes=idx,
            comment="mv 注释",
        )
        view["kind"] = "materialized_view"
        ddl = render.render_view_ddl("s", view)
        lines = ddl.splitlines()
        self.assertEqual(
            lines[0], "CREATE MATERIALIZED VIEW s.mv WITH (fillfactor='50') TABLESPACE fast AS"
        )
        no_data_pos = lines.index("WITH NO DATA;")
        idx_pos = lines.index("CREATE UNIQUE INDEX mv_idx ON s.mv USING btree (id);")
        alter_pos = lines.index("ALTER INDEX s.mv_idx SET TABLESPACE fast;")
        comment_pos = lines.index("COMMENT ON MATERIALIZED VIEW s.mv IS 'mv 注释';")
        self.assertLess(no_data_pos, idx_pos)
        self.assertLess(idx_pos, alter_pos)
        self.assertLess(alter_pos, comment_pos)
        self.assertNotIn(";;", ddl)


if __name__ == "__main__":
    unittest.main()
