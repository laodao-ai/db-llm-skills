-- tests/provision-test-db.sql —— 测试环境的一次性供给：测试专用特权角色 + 常驻 fixture。
--    运行时角色（库 owner）在本文件里只保留其身份，不做任何属性收敛（角色分离，见 §①）。
--    只读角色与其授权**不在本文件**，走 /pg-readonly-setup（见 §②）。
--
-- 谁来跑：有 CREATEROLE（或 superuser）的账号。**人手动执行**。
-- 在哪跑：**连到 dbllm 库**执行本文件（走 PgBouncer 或直连均可）。
--
-- ⚠️ 前置：数据库 `dbllm` 与同名 owner 角色需已存在——建库不在本文件范围内
--    （`CREATE DATABASE` 不能在事务块里跑、也过不了 PgBouncer，属环境搭建）。
--    用本仓自己的 skill 建：在仓根跑 `/pg-dev-init`（DB_NAME=dbllm），它会建库 +
--    同名 owner 角色 + scratch 库，口令由服务器生成并记进服务器上的交接文档。
--    ⚠️ 换名时注意：角色名 MUST NOT 以 `pg_` 开头——PG 保留该前缀，`CREATE USER pg_x` 会被拒。
--
-- 🔴 纯 SQL，零 psql 元命令（没有 \set / \gexec / \c）——Navicat / DBeaver / psql 都能直接跑。
--
-- 🔴 特权 SQL 的归属（见 CLAUDE.md「特权 SQL 边界」）：
--    本文件是**唯一**含 CREATE ROLE / GRANT 的可执行 SQL，且**只由人手动执行**。
--    本仓的任何 skill、脚本、测试，以及大模型，MUST NOT 执行特权语句——`ro-generate.sh`
--    只**生成** setup.sql，执行它的永远是消费仓的开发者。测试侧同理：只读角色由人建一次、
--    常驻，测试脚本全程只读地用它。
--
-- **本文件幂等**，整份重跑安全（角色先判存在再建、属性每次收敛、fixture 先 DROP 再建）。
-- 这是刻意的：上一版是「一次性、重跑报错」的脚本，于是它后来新增的属性要求从来没落到
-- 已经 provision 好的库上，也没有任何东西会发现这个差——直到某天真跑泳道才炸出来。
--
-- 密码明文写在下面——测试库是一次性用途，明文可接受（2026-08-30 拍板）。
-- 改密码请同步改 tests/.env.test 的对应键（运行时角色 dbllm / 测试专用特权角色
-- dbllm_test 各对应自己的一对键，见 tests/.env.test.example 的三凭据轴说明）。

-- ===========================================================================
-- ① 运行时角色（= `pg-dev-init` 建的库 owner，本节自 2026-09-10 阶段 0 角色分离起
--    只保留 owner/运行时身份，不再对它做任何属性收敛）
--    正常路径下这个角色**已经存在**（前置那步建的），下面的 DO 块不会触发，
--    `CREATE USER ... 'CHANGE_ME'` 只是没走前置时的兜底——真跑到它说明前置漏了。
--    权限说明：
--      · 它是 dbllm 库的 owner，天然能 CREATE SCHEMA；下面的 GRANT 是显式冗余，无害。
--      · **本文件 MUST NOT 再收敛它的 SUPERUSER/CREATEDB/CREATEROLE 等任何角色属性**
--        （`test-db-provisioning` R「运行时角色与测试特权角色分离」）——测试所需的额外
--        特权改由 §①b 的独立测试专用角色承载，见下。
--      · ⇒ 与 `pg-dev-init-createdb-switch` CDB-01「CREATEDB 只授不收」不再语义对撞：
--        `pg-dev-init` 授过的 `CREATEDB=1` 在这里不会被收回。
--    注意：账号已存在时**不改其密码**——避免冲掉 tests/.env.test 里已在用的真实值。
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dbllm') THEN
        CREATE USER dbllm PASSWORD 'CHANGE_ME';
    END IF;
END
$$;

GRANT CONNECT, CREATE ON DATABASE dbllm TO dbllm;

-- ===========================================================================
-- ①b 测试专用特权角色（`dbllm_test`）—— 2026-09-10 阶段 0 角色分离新增
--
--    承载运行时角色（①）不再持有的测试脚手架能力：建删自己的 fixture schema
--    （契约测试的 `dbllm_fixture` / e2e 的 `dbllm_e2e` `dbllm_e2e_ext`，
--    见 §③）。它是本仓测试**唯一**连接测试库时使用的账号——`tests/.env.test` 的
--    `DBLLM_TEST_PGUSER`/`PGPASSWORD` 指向它，不是运行时角色 ①（见该 env 模版
--    的注释区分三个凭据轴）。
--
--    权限说明（最小可写，非只读——契约测试与 e2e 都要建自己的 fixture schema）：
--      · 只 `GRANT CONNECT, CREATE ON DATABASE`——能建 schema，不能建库
--        （被测的 `db-collect.sql` 只读 `pg_catalog`，无需任何对象级 GRANT）。
--      · MUST NOT 持有 `SUPERUSER`/`CREATEROLE`/`CREATEDB`/`BYPASSRLS`/`REPLICATION`
--        中任何一项——常驻这些特权等于把刚从工具手里收走的能力又交给任何能跑
--        pytest 的东西（大模型在内）。下面的 ALTER 每次重跑都主动收敛，
--        历史环境手工授过的也会被收回。
--    注意：账号已存在时**不改其密码**，理由同 ①。
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dbllm_test') THEN
        CREATE USER dbllm_test PASSWORD 'CHANGE_ME';
    END IF;
END
$$;

-- 属性收敛（每次重跑都执行）：主动剥掉五项危险属性，含历史环境可能授过的任意一项。
ALTER USER dbllm_test NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;

GRANT CONNECT, CREATE ON DATABASE dbllm TO dbllm_test;

-- ===========================================================================
-- ② 只读角色 —— **本文件不建**（2026-09-09 拍板）
--
--    🔴 只读角色的常规供给路径唯一：`/pg-readonly-setup`
--       （ro-generate.sh 生成 setup.sql → 人执行 → ro-verify.sh 六面审计）。
--
--    历史上这里手工建过一个 `dbllm_e2e_ro`，理由是「验一个不同于库 owner 的角色能否
--    穿过 PgBouncer 认证面」——`/pg-readonly-setup` 建的 `llm_readonly` 本身就是这样一个
--    角色（非 owner、新建、要穿认证面），那条断言已被完全覆盖，故该例外取消、本节删空。
--
--    ⚠️ 顺序要求：本文件的 §③ fixture schema MUST 先于 setup.sql 执行——
--       `ALTER DEFAULT PRIVILEGES IN SCHEMA <s>` 要求 <s> 已存在。
--       正确顺序：/pg-dev-init → 本文件 → /pg-readonly-setup。
-- ===========================================================================

-- ===========================================================================
-- ③ e2e 的常驻 fixture（dbllm_e2e / dbllm_e2e_ext）
--
--    🔴 为什么不复用 tests/fixtures/dbllm_fixture.sql：
--    那份是关系推导引擎的「形态字典」、唯一真相源（见其文件头），由契约测试
--    tests/test_db_collect_contract.py 每次跑时**自建自删**（DROP SCHEMA ... CASCADE）。
--    e2e 若复用同名 schema，这里授给只读角色的 SELECT 会被那一 DROP 连表带授权冲掉。
--
--    🔴 为什么只造这么点形状：
--    形态字典归契约测试断言（14 个场景逐条有对应用例）；e2e 只验**接线**——凭据链路 →
--    采集 → render 落盘 → 查询，不重判单对象语义。所以这里只要够 e2e 那几条结构断言用的
--    最小形状集：普通表 / 物化视图 / 函数重载 / 分区父子 / 第二 schema。
-- ===========================================================================

-- DROP 在切换角色**之前**：整份 SQL 由特权账号执行，此时才删得掉上一轮遗留的
-- schema（无论它当初归谁）。切到 dbllm_test 之后就只删得动自己的了，幂等会破。
DROP SCHEMA IF EXISTS dbllm_e2e CASCADE;
DROP SCHEMA IF EXISTS dbllm_e2e_ext CASCADE;

-- 🔴 以 dbllm_test 身份建 §③ 的全部对象，让它们归 dbllm_test 而不是执行者。
--    §①/①b 含 CREATE ROLE，整份 SQL 只能用特权账号跑；不切角色的话 schema 与表
--    就都归那个账号（实测过一次：归了 postgres），于是重置 fixture 反而要动用
--    超级用户——与 ①b「爆炸半径锁死在这一个测试库内」的立意相反，也与契约测试
--    的 dbllm_fixture（dbllm_test 自建自删、全自动）两套权限模型一致。
--    SET ROLE 的前提：执行者是 superuser，或是 dbllm_test 的成员。用 postgres 跑
--    总是满足；换别的账号跑若报 "permission denied to set role"，先
--    `GRANT dbllm_test TO CURRENT_USER;` 再重跑。
SET ROLE dbllm_test;

CREATE SCHEMA dbllm_e2e;

-- 断言「对象→文件齐」+「managed-block 标记」
CREATE TABLE dbllm_e2e.users (
    id bigint PRIMARY KEY,
    email text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN dbllm_e2e.users.email IS '登录邮箱，已脱敏展示';

CREATE TABLE dbllm_e2e.orders (
    id bigint PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES dbllm_e2e.users (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 断言「matview 渲染成 MATERIALIZED VIEW」
CREATE MATERIALIZED VIEW dbllm_e2e.recent_orders AS
    SELECT id, user_id, created_at FROM dbllm_e2e.orders;

-- 断言「函数重载 = 一文件两 block」（本仓唯一的「多 block 一文件」情形）
CREATE FUNCTION dbllm_e2e.fmt(x integer) RETURNS text
    LANGUAGE sql IMMUTABLE AS 'SELECT x::text';
CREATE FUNCTION dbllm_e2e.fmt(x text) RETURNS text
    LANGUAGE sql IMMUTABLE AS 'SELECT x';

-- 断言「分区子表折叠进父表，不单独出文件」
CREATE TABLE dbllm_e2e.events (
    id bigint NOT NULL,
    occurred_at timestamptz NOT NULL,
    payload text
) PARTITION BY RANGE (occurred_at);
CREATE TABLE dbllm_e2e.events_2026_08 PARTITION OF dbllm_e2e.events
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');

-- 断言「跨 schema：第二 schema 的对象也落文件」
CREATE SCHEMA dbllm_e2e_ext;
CREATE TABLE dbllm_e2e_ext.widgets (
    id bigint PRIMARY KEY,
    order_id bigint NOT NULL
);
COMMENT ON COLUMN dbllm_e2e_ext.widgets.order_id IS '所属订单，参见 dbllm_e2e.orders 表';

-- §③ 到此为止，交回执行者身份。
RESET ROLE;

-- ===========================================================================
-- ④ 只读授权 —— **本文件不做**
--    `USAGE` / `SELECT` / `ALTER DEFAULT PRIVILEGES` 由 `/pg-readonly-setup` 生成的
--    setup.sql 按 `.dbmeta/.dbllm.env` 的 `SCHEMAS` 范围统一下发，避免两处各授一半、
--    漂移后 ro-verify.sh 的六面审计判 fail-closed。
--    ⇒ SCHEMAS 应含：dbllm_e2e,dbllm_e2e_ext
-- ===========================================================================
