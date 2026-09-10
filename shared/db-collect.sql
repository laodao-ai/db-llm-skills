-- Collect pg_catalog metadata for every non-system PG schema and emit a single-row,
-- single-column JSON text value (collect_version=1). Run via hack/db-collect.sh
-- (psql -At -X -v ON_ERROR_STOP=1 -v schemas_csv='...' -f hack/db-collect.sql).
--
-- Contract (openspec/changes/db-collect-foundation/design.md Dec-1/Dec-2;
-- requested_schemas):
--   {
--     "collect_version": 1, "collected_at": "...", "database": "...",
--     "requested_schemas": ["a","b"] | null,  -- null = full scan (no --schema/SCHEMAS)
--     "schemas": [ { "schema", "comment", "tables": [...], "views": [...], "functions": [...] } ]
--   }
--
-- Scope per schema:
--   tables   — relkind IN ('r','p','f') (ordinary/partitioned/foreign), each with
--              columns[]/indexes[]/constraints[]/triggers[] and partition_of (parent
--              table name, or null when not a partition). partition_key (DD-8,
--              dbmeta-ddl-files) is pg_get_partkeydef(oid) for partitioned parent
--              tables (relkind='p'), null otherwise (including partition children).
--              options[] (relation-ddl-equivalence T1) is pg_class.reloptions
--              verbatim, same shape as views[].options below; [] for partition
--              parent / foreign tables (PG allows no storage params on them).
--              [relation-ddl-equivalence-r2 T1] options[] additionally merges in
--              this relation's own TOAST relation's reloptions (reltoastrelid),
--              each element prefixed 'toast.', folded into the same sorted set
--              (toast.* storage params live on the toast relation, not the main
--              one, and never carry the prefix there). tablespace
--              (relation-ddl-equivalence T1) is pg_class.reltablespace:
--              null when 0 (database default), else pg_tablespace.spcname.
--              access_method (relation-ddl-equivalence-r2 T1) is pg_am.amname
--              (relam=0 -> null; partition parent tables never carry an explicit
--              AM). foreign (relation-ddl-equivalence-r2 T1) is
--              {server, options[]} for foreign tables (relkind='f') — server is
--              pg_foreign_server.srvname, options[] is pg_foreign_table.ftoptions
--              kept in catalog array order (not sorted, unlike options[] above);
--              null for every non-foreign table. columns[] additionally carry
--              fdw_options (relation-ddl-equivalence-r2 T1) — pg_attribute.
--              attfdwoptions kept in catalog array order; [] for non-FDW columns.
--              indexes[] entries additionally carry tablespace (same expression,
--              applied to the index relation) and options[] (index reloptions,
--              same shape, no toast merge — indexes have no toast relation).
--   views    — relkind IN ('v','m') (view / materialized view), each with
--              columns[] (name/type/nullable/comment/position — no 'default',
--              unlike tables[].columns; dbmeta-knowledge-base D-N). options[]
--              (view-reloptions-ddl T1) is pg_class.reloptions verbatim, one
--              'name=value' string per element, sorted by element text; [] when
--              no options are set (never null, never a missing key).
--              [relation-ddl-equivalence-r2 T1] options[] additionally merges in
--              the view's own TOAST relation's reloptions with the same
--              'toast.'-prefix rule as tables[].options above (materialized
--              views only — ordinary views never have a toast relation).
--              tablespace (relation-ddl-equivalence T1) — same expression as
--              tables[].tablespace. access_method (relation-ddl-equivalence-r2
--              T1) — same expression as tables[].access_method (ordinary views
--              never have an explicit AM -> always null). populated
--              (relation-ddl-equivalence T1) is relispopulated verbatim
--              (always true for ordinary views). indexes[] (relation-ddl-equivalence
--              T1) is the same shape as tables[].indexes minus inherited_from
--              (materialized views can't be partitions); always [] for ordinary
--              views.
--   functions— pg_proc prokind='f' (ordinary functions; no procedures/aggregates in
--              this codebase, see design.md Risks). definition (DD-8,
--              dbmeta-ddl-files) is pg_get_functiondef(oid), the full executable
--              CREATE OR REPLACE FUNCTION shell; source (prosrc, body only) is kept
--              for back-compat/_gaps consumers.
--
-- Schema inclusion rule (Dec-1): a schema is included iff it owns at least one
-- supported, non-extension-owned object among {table/partitioned table/foreign
-- table, view/materialized view, function}. This replaces the old collect.sql
-- rule (relkind IN ('r','p') only), which would miss a schema holding only views
-- or functions.
--
-- Extension exclusion (Dec-2): pg_depend rows with deptype='e' mark objects owned
-- by an extension (classid identifies the catalog: pg_class for relations,
-- pg_proc for functions); relations/views/functions/schema-membership all
-- NOT EXISTS against this set.
--
-- Index inherited_from (Dec-1): pg_inherits carries BOTH table-inheritance rows
-- (table oids on both sides) and partition-index-attach rows (index oids on both
-- sides) — MUST filter pg_class.relkind IN ('i','I') on both joined sides, never a
-- bare `inhrelid = <index oid>` join.
--
-- Constraint is_local / contype='n' exclusion (Dec-1): is_local = conislocal AND
-- coninhcount = 0 (a constraint can be locally defined AND inherited at once, per
-- PG docs — conislocal alone is not "not inherited"). PG18 emits contype='n' rows
-- for column NOT NULL — excluded (columns[].nullable already carries this; audit
-- fields are 9x NOT NULL per table and would otherwise flood constraints[]).
--
-- Trigger inherited_from (Dec-1): pg_trigger.tgparentid (nonzero => name of the
-- parent trigger this row was cloned from onto a partition), tgisinternal excluded.
--
-- --schema multi-value filter: hack/db-collect.sh passes a comma-joined list via
-- `-v schemas_csv=...`; empty string means "all schemas" (no filter applied).

WITH ext_relations AS (
    -- (classid, objid) pairs for objects owned by an extension (Dec-2).
    SELECT d.classid, d.objid
    FROM pg_depend d
    JOIN pg_extension e ON d.refobjid = e.oid AND d.deptype = 'e'
),
target_schemas AS (
    SELECT n.oid, n.nspname
    FROM pg_namespace n
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_%'
      AND (
          :'schemas_csv' = ''
          OR n.nspname = ANY (string_to_array(:'schemas_csv', ','))
      )
      -- Schema inclusion rule (Dec-1): at least one supported, non-extension object.
      AND EXISTS (
          SELECT 1 FROM pg_class c
          WHERE c.relnamespace = n.oid
            AND c.relkind IN ('r', 'p', 'f', 'v', 'm')
            AND NOT EXISTS (
                SELECT 1 FROM ext_relations er
                WHERE er.classid = 'pg_class'::regclass AND er.objid = c.oid
            )
          UNION ALL
          SELECT 1 FROM pg_proc p
          WHERE p.pronamespace = n.oid
            AND p.prokind = 'f'
            AND NOT EXISTS (
                SELECT 1 FROM ext_relations er
                WHERE er.classid = 'pg_proc'::regclass AND er.objid = p.oid
            )
      )
)
SELECT jsonb_build_object(
    'collect_version', 1,
    'collected_at', now(),
    'database', current_database(),
    'requested_schemas',
        CASE WHEN :'schemas_csv' = '' THEN NULL
             ELSE to_jsonb(string_to_array(:'schemas_csv', ',')) END,
    'schemas', COALESCE(
        (SELECT jsonb_agg(schema_obj ORDER BY schema_obj->>'schema') FROM (
            SELECT jsonb_build_object(
                'schema', ts.nspname,
                'comment', obj_description(ts.oid, 'pg_namespace'),
                'tables', (
                    SELECT COALESCE(jsonb_agg(t ORDER BY t->>'name'), '[]'::jsonb)
                    FROM (
                        SELECT jsonb_build_object(
                            'name', c.relname,
                            'kind', CASE c.relkind
                                WHEN 'p' THEN 'partitioned_table'
                                WHEN 'f' THEN 'foreign_table'
                                ELSE 'table'
                            END,
                            'comment', obj_description(c.oid, 'pg_class'),
                            'reltuples', GREATEST(c.reltuples, 0)::bigint,
                            -- [relation-ddl-equivalence T1] pg_class.reloptions verbatim,
                            -- same shape as views[].options (view-reloptions-ddl T1):
                            -- one 'name=value' string per element, sorted by element
                            -- text; [] when no options are set (never null, never a
                            -- missing key). Partition parent / foreign tables have no
                            -- storage parameters, so this is naturally [] for them.
                            -- [relation-ddl-equivalence-r2 T1] options also merges in
                            -- this relation's own TOAST relation's reloptions (each
                            -- element prefixed 'toast.'), since toast.* storage params
                            -- (e.g. toast.autovacuum_enabled) live on the toast
                            -- relation (reltoastrelid), not the main relation, and
                            -- never carry the 'toast.' prefix there (decision-memo C1).
                            -- reltoastrelid=0 (no toast relation) or a toast relation
                            -- with no reloptions both leave this union's second leg
                            -- empty, so the result is unchanged for such rows.
                            'options', (
                                SELECT COALESCE(jsonb_agg(o ORDER BY o), '[]'::jsonb)
                                FROM (
                                    SELECT o FROM unnest(c.reloptions) AS o
                                    UNION ALL
                                    SELECT 'toast.' || o
                                    FROM pg_class tc
                                    CROSS JOIN unnest(tc.reloptions) AS o
                                    WHERE tc.oid = c.reltoastrelid
                                ) AS u
                            ),
                            -- [relation-ddl-equivalence-r2 T1] pg_class.relam ->
                            -- pg_am.amname; relam=0 (partition parent tables, which
                            -- never carry an explicit AM) -> null.
                            'access_method', (SELECT amname FROM pg_am WHERE oid = NULLIF(c.relam, 0)),
                            -- [relation-ddl-equivalence-r2 T1] foreign tables only:
                            -- server name + table-level FDW options (ftoptions),
                            -- kept in catalog array order (pg_dump/user-written order,
                            -- not sorted like reloptions); null for every non-foreign
                            -- table (relkind != 'f').
                            'foreign', (
                                CASE WHEN c.relkind = 'f' THEN (
                                    SELECT jsonb_build_object(
                                        'server', s.srvname,
                                        'options', COALESCE(
                                            (SELECT jsonb_agg(o ORDER BY ord)
                                             FROM unnest(ft.ftoptions) WITH ORDINALITY AS t(o, ord)),
                                            '[]'::jsonb
                                        )
                                    )
                                    FROM pg_foreign_table ft
                                    JOIN pg_foreign_server s ON s.oid = ft.ftserver
                                    WHERE ft.ftrelid = c.oid
                                ) ELSE NULL END
                            ),
                            -- [relation-ddl-equivalence T1] pg_class.reltablespace: 0 means
                            -- the relation uses the database default tablespace, rendered
                            -- as null (no TABLESPACE clause on render, matching pg_dump).
                            'tablespace', (SELECT spcname FROM pg_tablespace WHERE oid = NULLIF(c.reltablespace, 0)),
                            -- DD-8: partition key definition text for partitioned
                            -- parent tables (relkind='p'); null for everything else,
                            -- including partition children themselves.
                            'partition_key', CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END,
                            -- [impl-review-fix] F1: pg_inherits also carries plain table
                            -- INHERITS rows (not just partition attach rows), and a
                            -- multi-parent INHERITS table would make this scalar subquery
                            -- return >1 row and error the whole collection. Restrict to
                            -- true partitions (c.relispartition) so INHERITS tables get
                            -- partition_of=null instead.
                            'partition_of', (
                                CASE WHEN c.relispartition THEN (
                                    SELECT p.relname
                                    FROM pg_inherits i
                                    JOIN pg_class p
                                        ON p.oid = i.inhparent
                                       AND p.relkind IN ('r', 'p', 'f')
                                    WHERE i.inhrelid = c.oid
                                ) ELSE NULL END
                            ),
                            'columns', (
                                SELECT COALESCE(jsonb_agg(col ORDER BY (col->>'position')::int), '[]'::jsonb)
                                FROM (
                                    SELECT jsonb_build_object(
                                        'position', a.attnum,
                                        'name', a.attname,
                                        'type', format_type(a.atttypid, a.atttypmod),
                                        'default', pg_get_expr(ad.adbin, ad.adrelid),
                                        'nullable', NOT a.attnotnull,
                                        'comment', col_description(c.oid, a.attnum),
                                        -- [relation-ddl-equivalence-r2 T1] pg_attribute.
                                        -- attfdwoptions verbatim, catalog array order
                                        -- (not sorted); [] for every non-FDW column
                                        -- (attfdwoptions is NULL there).
                                        'fdw_options', COALESCE(
                                            (SELECT jsonb_agg(o ORDER BY ord)
                                             FROM unnest(a.attfdwoptions) WITH ORDINALITY AS t(o, ord)),
                                            '[]'::jsonb
                                        )
                                    ) AS col
                                    FROM pg_attribute a
                                    LEFT JOIN pg_attrdef ad
                                        ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                                    WHERE a.attrelid = c.oid
                                      AND a.attnum > 0
                                      AND NOT a.attisdropped
                                ) cols
                            ),
                            'indexes', (
                                SELECT COALESCE(jsonb_agg(idx ORDER BY idx->>'name'), '[]'::jsonb)
                                FROM (
                                    SELECT jsonb_build_object(
                                        'name', i.relname,
                                        'definition', pg_get_indexdef(i.oid),
                                        'inherited_from', (
                                            SELECT pi.relname
                                            FROM pg_inherits inh
                                            JOIN pg_class pi
                                                ON pi.oid = inh.inhparent
                                               AND pi.relkind IN ('i', 'I')
                                            WHERE inh.inhrelid = i.oid
                                        ),
                                        -- [relation-ddl-equivalence T1] index reltablespace,
                                        -- same expression as tables[].tablespace below,
                                        -- applied to the index relation.
                                        'tablespace', (SELECT spcname FROM pg_tablespace WHERE oid = NULLIF(i.reltablespace, 0)),
                                        -- [relation-ddl-equivalence T1] index reloptions,
                                        -- same shape as tables[].options / views[].options.
                                        'options', (
                                            SELECT COALESCE(jsonb_agg(o ORDER BY o), '[]'::jsonb)
                                            FROM unnest(i.reloptions) AS o
                                        )
                                    ) AS idx
                                    FROM pg_index ix
                                    JOIN pg_class i
                                        ON i.oid = ix.indexrelid
                                       AND i.relkind IN ('i', 'I')
                                    WHERE ix.indrelid = c.oid
                                      -- [T45⑤] skip invalid indexes (failed CREATE INDEX
                                      -- CONCURRENTLY / REINDEX CONCURRENTLY leftovers):
                                      -- they are not usable and must not be replayed as
                                      -- if they were real DDL.
                                      AND ix.indisvalid
                                ) idxs
                            ),
                            'constraints', (
                                SELECT COALESCE(jsonb_agg(con ORDER BY con->>'name'), '[]'::jsonb)
                                FROM (
                                    SELECT jsonb_build_object(
                                        'name', k.conname,
                                        'type', k.contype,
                                        'definition', pg_get_constraintdef(k.oid),
                                        'is_local', (k.conislocal AND k.coninhcount = 0)
                                    ) AS con
                                    FROM pg_constraint k
                                    WHERE k.conrelid = c.oid
                                      AND k.contype != 'n'
                                ) cons
                            ),
                            'triggers', (
                                SELECT COALESCE(jsonb_agg(trg ORDER BY trg->>'name'), '[]'::jsonb)
                                FROM (
                                    SELECT jsonb_build_object(
                                        'name', tg.tgname,
                                        'definition', pg_get_triggerdef(tg.oid),
                                        'enabled', tg.tgenabled,
                                        'inherited_from', (
                                            SELECT ptg.tgname
                                            FROM pg_trigger ptg
                                            WHERE ptg.oid = tg.tgparentid
                                        )
                                    ) AS trg
                                    FROM pg_trigger tg
                                    WHERE tg.tgrelid = c.oid
                                      AND NOT tg.tgisinternal
                                ) trgs
                            )
                        ) AS t
                        FROM pg_class c
                        WHERE c.relnamespace = ts.oid
                          AND c.relkind IN ('r', 'p', 'f')
                          AND NOT EXISTS (
                              SELECT 1 FROM ext_relations er
                              WHERE er.classid = 'pg_class'::regclass AND er.objid = c.oid
                          )
                    ) tables
                ),
                'views', (
                    SELECT COALESCE(jsonb_agg(v ORDER BY v->>'name'), '[]'::jsonb)
                    FROM (
                        SELECT jsonb_build_object(
                            'name', c.relname,
                            'kind', CASE c.relkind WHEN 'm' THEN 'materialized_view' ELSE 'view' END,
                            'comment', obj_description(c.oid, 'pg_class'),
                            'definition', pg_get_viewdef(c.oid, true),
                            -- [spec-review-amendment] D-N (Q1 拍板 A): view columns, same shape
                            -- as tables[].columns minus 'default' (views don't carry
                            -- pg_attrdef-level column defaults the way base tables do).
                            'columns', (
                                SELECT COALESCE(jsonb_agg(col ORDER BY (col->>'position')::int), '[]'::jsonb)
                                FROM (
                                    SELECT jsonb_build_object(
                                        'position', a.attnum,
                                        'name', a.attname,
                                        'type', format_type(a.atttypid, a.atttypmod),
                                        'nullable', NOT a.attnotnull,
                                        'comment', col_description(c.oid, a.attnum)
                                    ) AS col
                                    FROM pg_attribute a
                                    WHERE a.attrelid = c.oid
                                      AND a.attnum > 0
                                      AND NOT a.attisdropped
                                ) cols
                            ),
                            -- [view-reloptions-ddl T1] pg_class.reloptions verbatim,
                            -- one string per element ('name=value'), sorted by element
                            -- text; zero options -> jsonb_agg NULL -> COALESCE '[]'.
                            -- [relation-ddl-equivalence-r2 T1] same toast-merge as
                            -- tables[].options above -- a materialized view's own
                            -- toast relation's reloptions, 'toast.'-prefixed, folded
                            -- into the same sorted set (decision-memo C1: ordinary
                            -- views never have a toast relation, so this is a no-op
                            -- for kind='view').
                            'options', (
                                SELECT COALESCE(jsonb_agg(o ORDER BY o), '[]'::jsonb)
                                FROM (
                                    SELECT o FROM unnest(c.reloptions) AS o
                                    UNION ALL
                                    SELECT 'toast.' || o
                                    FROM pg_class tc
                                    CROSS JOIN unnest(tc.reloptions) AS o
                                    WHERE tc.oid = c.reltoastrelid
                                ) AS u
                            ),
                            -- [relation-ddl-equivalence-r2 T1] same expression as
                            -- tables[].access_method above; relam=0 (ordinary views
                            -- never have an explicit AM) -> null.
                            'access_method', (SELECT amname FROM pg_am WHERE oid = NULLIF(c.relam, 0)),
                            -- [relation-ddl-equivalence T1] relispopulated verbatim; always
                            -- true for ordinary views (kind='view'), meaningful only for
                            -- materialized views.
                            'populated', c.relispopulated,
                            -- [relation-ddl-equivalence T1] same expression as
                            -- tables[].tablespace above, applied to the view relation.
                            'tablespace', (SELECT spcname FROM pg_tablespace WHERE oid = NULLIF(c.reltablespace, 0)),
                            -- [relation-ddl-equivalence T1] same shape as tables[].indexes,
                            -- minus 'inherited_from' (materialized views can't be
                            -- partitions); ordinary views always []. Materialized views
                            -- can't hold constraints, so no p/u/x skip logic is needed.
                            'indexes', (
                                SELECT COALESCE(jsonb_agg(idx ORDER BY idx->>'name'), '[]'::jsonb)
                                FROM (
                                    SELECT jsonb_build_object(
                                        'name', i.relname,
                                        'definition', pg_get_indexdef(i.oid),
                                        'options', (
                                            SELECT COALESCE(jsonb_agg(o ORDER BY o), '[]'::jsonb)
                                            FROM unnest(i.reloptions) AS o
                                        ),
                                        'tablespace', (SELECT spcname FROM pg_tablespace WHERE oid = NULLIF(i.reltablespace, 0))
                                    ) AS idx
                                    FROM pg_index ix
                                    JOIN pg_class i
                                        ON i.oid = ix.indexrelid
                                       AND i.relkind IN ('i', 'I')
                                    WHERE ix.indrelid = c.oid
                                      -- [T45⑤] same invalid-index filter as tables[].indexes.
                                      AND ix.indisvalid
                                ) idxs
                            )
                        ) AS v
                        FROM pg_class c
                        WHERE c.relnamespace = ts.oid
                          AND c.relkind IN ('v', 'm')
                          AND NOT EXISTS (
                              SELECT 1 FROM ext_relations er
                              WHERE er.classid = 'pg_class'::regclass AND er.objid = c.oid
                          )
                    ) views
                ),
                'functions', (
                    SELECT COALESCE(jsonb_agg(f ORDER BY f->>'name'), '[]'::jsonb)
                    FROM (
                        SELECT jsonb_build_object(
                            'name', p.proname,
                            'identity_args', pg_get_function_identity_arguments(p.oid),
                            'arg_names', COALESCE(to_jsonb(p.proargnames), '[]'::jsonb),
                            'result_type', pg_get_function_result(p.oid),
                            'language', l.lanname,
                            'comment', obj_description(p.oid, 'pg_proc'),
                            'source', p.prosrc,
                            -- DD-8: full executable function shell (CREATE OR REPLACE
                            -- FUNCTION ... $function$...$function$), source kept above
                            -- for _gaps/back-compat consumers that only want the body.
                            'definition', pg_get_functiondef(p.oid)
                        ) AS f
                        FROM pg_proc p
                        JOIN pg_language l ON l.oid = p.prolang
                        WHERE p.pronamespace = ts.oid
                          AND p.prokind = 'f'
                          AND NOT EXISTS (
                              SELECT 1 FROM ext_relations er
                              WHERE er.classid = 'pg_proc'::regclass AND er.objid = p.oid
                          )
                    ) funcs
                )
            ) AS schema_obj
            FROM target_schemas ts
        ) schemas_wrap),
        '[]'::jsonb
    )
)::text;
