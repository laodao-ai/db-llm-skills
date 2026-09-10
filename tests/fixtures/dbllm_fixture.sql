-- tests/fixtures/dbllm_fixture.sql
--
-- Version-controlled "pattern dictionary" DDL fixture for pg-dict's relation
-- inference engine (relation-inference-and-diagram change, Task 5 / tasks.md
-- Task 1.5). This fixture is the ONE source of truth for every column-name /
-- COMMENT shape the engine (`render.py`'s infer_column_name_candidates,
-- infer_comment_ref_candidates, collect_relations) must handle. When a
-- consuming project reports a new pattern the engine mishandles, add a table
-- here that reproduces it — don't special-case render.py for a pattern this
-- fixture can't exercise.
--
-- No INSERT statements: pg-dict reads pg_catalog structure (columns, types,
-- comments, partition membership), never row data.
--
-- Loaded by tests/test_db_collect_contract.py's setup_module via
-- `psql -f tests/fixtures/dbllm_fixture.sql` against a throwaway schema,
-- and dropped (`DROP SCHEMA dbllm_fixture CASCADE`) by teardown_module.

CREATE SCHEMA dbllm_fixture;

-- ---------------------------------------------------------------------------
-- users: base table + a sensitive column (REQ triggers SENSITIVE_NAME_RE via
-- "email", declared safe via "脱敏" per SENSITIVE_SAFE_RE).
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.users (
    id bigint PRIMARY KEY,
    email text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN dbllm_fixture.users.email IS '登录邮箱，已脱敏展示';

-- ---------------------------------------------------------------------------
-- categories: self-referential parent_id. DD-2's column-name stem rule only
-- does exact-match or "+s" pluralization ("user" -> "users") — it does NOT
-- handle English's irregular "-y -> -ies" plural, so a column literally named
-- "parent_id" can never stem-match back to a table named "categories" (stem
-- "parent" != "categorie"/"categories" under either rule). The self-reference
-- here is therefore correctly picked up via the COMMENT signal (DD-3 bare
-- table-name reference, "categories" is unique across all schemas) instead —
-- this is the realistic, common way such a column is annotated in practice.
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.categories (
    id bigint PRIMARY KEY,
    parent_id bigint REFERENCES dbllm_fixture.categories (id),
    name text NOT NULL
);
COMMENT ON COLUMN dbllm_fixture.categories.parent_id IS '父级分类，参考 categories 表';

-- ---------------------------------------------------------------------------
-- products: category_id exercises the same irregular-plural limitation as
-- above from the OTHER direction (products.category_id -> categories) — no
-- column-name candidate is expected here either (stem "category" matches
-- neither "category" nor "categorys"). Kept uncommented on purpose: this
-- table's role in the fixture is to document that limitation, not to be
-- asserted on by any Task 5 integration test.
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.products (
    id bigint PRIMARY KEY,
    category_id bigint NOT NULL REFERENCES dbllm_fixture.categories (id),
    name text NOT NULL
);

-- ---------------------------------------------------------------------------
-- orders: the primary column-name-inference target (user_id -> users.id,
-- regular "+s" pluralization), plus two boundary patterns that MUST NOT be
-- mistaken for FK-shaped columns:
--   - created_by_user_id: compound stem "created_by_user" (col_name[:-3]) has
--     no matching table (regardless of "s") -> no candidate.
--   - updated_by: does not end in "_id" at all -> never considered by the
--     column-name signal in the first place.
-- status carries an enum-COMMENT convention entry (unrelated to relation
-- inference, but part of the "pattern dictionary" this fixture documents).
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.orders (
    id bigint PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES dbllm_fixture.users (id),
    created_by_user_id bigint,
    updated_by bigint,
    status integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN dbllm_fixture.orders.status IS '订单状态：0=待处理 1=已完成 2=已取消';
COMMENT ON COLUMN dbllm_fixture.orders.created_by_user_id IS '创建人（复合 stem，无对应表，不应被误推导）';
COMMENT ON COLUMN dbllm_fixture.orders.updated_by IS '最后更新人（非 _id 结尾，不参与列名推导）';

-- ---------------------------------------------------------------------------
-- order_items: two regular column-name candidates in one table (order_id ->
-- orders.id, product_id -> products.id) — both use regular "+s" pluralization
-- and are expected to be inferred, though no Task 5 integration test asserts
-- on them individually (covered structurally as part of the "基础关系" set).
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.order_items (
    id bigint PRIMARY KEY,
    order_id bigint NOT NULL REFERENCES dbllm_fixture.orders (id),
    product_id bigint NOT NULL REFERENCES dbllm_fixture.products (id),
    quantity integer NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------------------
-- payments: order_id is ALREADY annotated via the `逻辑关联` COMMENT
-- convention -> MUST be excluded from candidates (REQ-RI-3), even though the
-- column-name signal alone would otherwise match it (order_id -> orders.id,
-- same regular pluralization as order_items.order_id above).
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.payments (
    id bigint PRIMARY KEY,
    order_id bigint NOT NULL REFERENCES dbllm_fixture.orders (id),
    amount numeric(12, 2) NOT NULL
);
COMMENT ON COLUMN dbllm_fixture.payments.order_id IS '支付关联订单，逻辑关联 dbllm_fixture.orders.id';

-- ---------------------------------------------------------------------------
-- audit_logs: ref_id is a generic/polymorphic reference column (stem "ref"
-- matches no table by column name), annotated with a fully-qualified
-- `<schema>.<table>` COMMENT reference (DD-3 rule 1) instead — no FK
-- constraint on purpose, since a real polymorphic audit-log column can't be
-- declared as a single-target foreign key.
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.audit_logs (
    id bigint PRIMARY KEY,
    ref_id bigint NOT NULL,
    action text NOT NULL
);
COMMENT ON COLUMN dbllm_fixture.audit_logs.ref_id IS '关联记录，参见 dbllm_fixture.orders 表';

-- ---------------------------------------------------------------------------
-- events / events_2026_08: partitioned table. group_children()/
-- _top_level_tables() must fold the partition child out of every inference
-- scan (it participates only via its parent) — exercised end-to-end by the
-- regen/idempotency integration test, not asserted on individually.
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.events (
    id bigint NOT NULL,
    occurred_at timestamptz NOT NULL,
    payload text,
    -- 表级 CHECK：分区子表会继承它，在子表上 conislocal=false ->
    -- is_local=(conislocal AND coninhcount=0)=false，验 db-collect.sql 的继承
    -- 约束排除分支（Dec-1）。父表上这条则是 is_local=true。
    CONSTRAINT events_id_positive CHECK (id > 0)
) PARTITION BY RANGE (occurred_at);

-- WITH (fillfactor = 60) (relation-ddl-equivalence-r2 T1) exercises a
-- partition child's OWN reloptions (partition parents can't carry storage
-- params at all — decision-memo C2 — children can) both in the raw collect
-- shape (tables[].options for events_2026_08) and, downstream (Task 2/3), the
-- folded "--   存储参数 `fillfactor=60`" line in the parent's rendered file.
CREATE TABLE dbllm_fixture.events_2026_08 PARTITION OF dbllm_fixture.events
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01') WITH (fillfactor = 60);

-- ---------------------------------------------------------------------------
-- metrics: 触发 db-collect.sql 里此前没被真 catalog 验证过的几条采集分支——
-- 二级/唯一索引 (pg_get_indexdef)、部分索引 (带 WHERE 谓词)、CHECK 约束
-- (contype='c')、行级触发器 (pg_get_triggerdef + tgisinternal 排除)。这些是真实
-- 消费库几乎必然出现、而基础 fixture 完全缺席的常见形态（冷审 HIGH）。
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.metrics (
    id bigint PRIMARY KEY,
    name text NOT NULL,
    value numeric NOT NULL CONSTRAINT metrics_value_nonneg CHECK (value >= 0)
);
CREATE UNIQUE INDEX metrics_name_key ON dbllm_fixture.metrics (name);
CREATE INDEX metrics_positive_value_idx ON dbllm_fixture.metrics (value) WHERE value > 0;

CREATE FUNCTION dbllm_fixture.noop_trigger() RETURNS trigger
    LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
CREATE TRIGGER metrics_before_update BEFORE UPDATE ON dbllm_fixture.metrics
    FOR EACH ROW EXECUTE FUNCTION dbllm_fixture.noop_trigger();

-- ---------------------------------------------------------------------------
-- A plain view and function -- preserves the pre-existing structural contract
-- coverage of collect_version=1's views[]/functions[] shape (the original
-- test/test_db_collect_contract.py fixture had one of each; Task 5 keeps
-- those structural assertions, per tasks.md Task 1.5: "保留现有结构断言").
-- Not part of the relation-inference "pattern dictionary" itself.
-- ---------------------------------------------------------------------------
CREATE VIEW dbllm_fixture.active_users AS
    SELECT id, email FROM dbllm_fixture.users;

CREATE FUNCTION dbllm_fixture.plain_fn(x integer) RETURNS integer
    LANGUAGE sql IMMUTABLE AS 'SELECT x + 1';

-- ---------------------------------------------------------------------------
-- fmt: two overloads of ONE function name -> exercises render.py's
-- one-file-per-name / one-block-PER-OVERLOAD path (the sole many-blocks-to-one-
-- file case). Real pg_catalog emits each overload as a separate pg_proc row;
-- this verifies the collect->render contract for overloaded functions, which
-- the single plain_fn above does not.
-- ---------------------------------------------------------------------------
CREATE FUNCTION dbllm_fixture.fmt(x integer) RETURNS text
    LANGUAGE sql IMMUTABLE AS 'SELECT x::text';

CREATE FUNCTION dbllm_fixture.fmt(x text) RETURNS text
    LANGUAGE sql IMMUTABLE AS 'SELECT x';

-- ---------------------------------------------------------------------------
-- recent_orders: a MATERIALIZED VIEW (relkind='m') -> collect emits it in
-- views[] with kind='materialized_view'; render_view_ddl emits MATERIALIZED
-- VIEW. Verifies the matview path on real catalog (active_users above only
-- covers the plain relkind='v' case). WITH (toast.autovacuum_enabled = false)
-- (relation-ddl-equivalence-r2 T1) exercises the toast-relation reloptions
-- merge into views[].options on the materialized-view path -- PG only
-- allocates a toast relation (reltoastrelid != 0) when at least one column is
-- variable-length (attlen = -1); the prior all-fixed-width projection
-- (id/user_id bigint, created_at timestamptz) got reltoastrelid=0 and the
-- toast.* option silently landed nowhere (verified against the real test DB:
-- reloptions stayed NULL on both the main and would-be toast relation), so
-- created_at is cast to text here purely to give this matview a real toast
-- relation for the option to attach to.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW dbllm_fixture.recent_orders
    WITH (toast.autovacuum_enabled = false) AS
    SELECT id, user_id, created_at::text AS created_at FROM dbllm_fixture.orders;

-- ---------------------------------------------------------------------------
-- guarded_users: a view WITH storage options set (view-reloptions-ddl T1) --
-- exercises views[].options in collect JSON (pg_class.reloptions, sorted by
-- element text). Write order (security_barrier, check_option) is deliberately
-- the REVERSE of the sorted-output order (check_option=cascaded before
-- security_barrier=true) to anchor "sorted output is independent of the
-- written WITH-clause order". MUST stay a single-table, no
-- JOIN/aggregate/DISTINCT/subquery auto-updatable view -- check_option is
-- only legal on an auto-updatable view; otherwise CREATE VIEW itself errors
-- and the whole fixture fails to load.
-- ---------------------------------------------------------------------------
CREATE VIEW dbllm_fixture.guarded_users
    WITH (security_barrier = true, check_option = cascaded) AS
    SELECT id, email FROM dbllm_fixture.users WHERE email <> '';

-- ---------------------------------------------------------------------------
-- tuned_counters: a table WITH storage parameters set (relation-ddl-equivalence
-- T1) -- exercises tables[].options in collect JSON (pg_class.reloptions,
-- sorted by element text, relation-ddl-equivalence T1), plus the same shape
-- on its PRIMARY KEY index (spec-review-amendment Q2: constraint-backed index
-- storage params) and on a plain non-unique index. Deliberately no foreign
-- key, no trigger, and referenced by no view -- keeps this fixture object
-- dependency-free per the privileged-SQL boundary. Write order (hits, then
-- id via the PK constraint) is deliberately the REVERSE of the
-- name-sorted-column-position order to anchor "options sort by element text,
-- independent of DDL write order" (same anchor style as guarded_users above).
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.tuned_counters (
    id integer,
    hits bigint NOT NULL DEFAULT 0,
    CONSTRAINT tuned_counters_pkey PRIMARY KEY (id) WITH (fillfactor = 90)
) WITH (fillfactor = 70, autovacuum_enabled = false);
CREATE INDEX tuned_counters_hits_idx ON dbllm_fixture.tuned_counters (hits);

-- ---------------------------------------------------------------------------
-- stale_orders: an UNPOPULATED materialized view (relation-ddl-equivalence
-- T1, WITH NO DATA) -- exercises views[].populated=false and views[].indexes[]
-- in collect JSON. A materialized view's own index requires the matview to
-- exist first, hence the separate CREATE (UNIQUE) INDEX statement below.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW dbllm_fixture.stale_orders AS
    SELECT id, user_id FROM dbllm_fixture.orders WITH NO DATA;
CREATE UNIQUE INDEX stale_orders_id_key ON dbllm_fixture.stale_orders (id);

-- ---------------------------------------------------------------------------
-- toasted_notes: a table WITH a toast relation (relation-ddl-equivalence-r2
-- T1) -- the `body text` column gives it a real reltoastrelid, so
-- WITH (toast.autovacuum_enabled = false) lands in the TOAST relation's own
-- reloptions (decision-memo C1 probe), not the main relation's -- exercises
-- tables[].options merging in the `toast.`-prefixed item from
-- pg_class.reltoastrelid's reloptions. Deliberately no foreign key, no
-- trigger, and referenced by no view -- keeps this fixture object
-- dependency-free per the privileged-SQL boundary (same rationale as
-- tuned_counters above).
-- ---------------------------------------------------------------------------
CREATE TABLE dbllm_fixture.toasted_notes (
    id integer PRIMARY KEY,
    body text
) WITH (toast.autovacuum_enabled = false);

-- ---------------------------------------------------------------------------
-- dbllm_fixture_ext: a SECOND schema, to exercise CROSS-SCHEMA relation
-- inference on real catalog. widgets.order_id carries a fully-qualified DD-3
-- COMMENT reference to dbllm_fixture.orders (a table in the OTHER schema) --
-- inferable only when the collect scope spans BOTH schemas (see
-- test_cross_schema_comment_ref_inference). The default single-schema collect
-- (dbllm_fixture only) never sees this table, so every existing single-schema
-- assertion above stays untouched.
-- ---------------------------------------------------------------------------
CREATE SCHEMA dbllm_fixture_ext;

CREATE TABLE dbllm_fixture_ext.widgets (
    id bigint PRIMARY KEY,
    order_id bigint NOT NULL
);
COMMENT ON COLUMN dbllm_fixture_ext.widgets.order_id IS '所属订单，参见 dbllm_fixture.orders 表';
