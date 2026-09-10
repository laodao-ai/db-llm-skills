---
name: pg-sql-check
description: 对单条即席 SQL 语句（含写语句）以只读角色跑 `PREPARE` 校验，验列名/类型/参数占位是否对得上真实 schema——不 `EXPLAIN`、不执行、零写入；当用户/agent 说"校验这条 SQL"、"这条语句列名对不对"、"这条 SQL 能跑吗"时触发。前置：`.dbmeta/` 已生成（否则先 /pg-dict）与只读通道已供给（否则先 /pg-readonly-setup）。
license: MIT
metadata:
  author: db-llm
  version: "1.0"
---

面向 agent 的单条 SQL 校验**唯一入口**——写完一条 SQL、不确定列名/类型/参数占位对不对时，
先用本 skill 校验一遍，再决定要不要真的拿它去查询/建迁移。不要直接对开发库 `psql` 手跑
`EXPLAIN` 判断，也不要绕开本 skill 自己拼 `ro-session.sh --prepare` 调用。

## 唯一机制：`PREPARE`，不 `EXPLAIN`，不执行

校验只做一件事——把语句 `PREPARE` 到目标库（PG ≥ 16），读回
`pg_prepared_statements.parameter_types`/`result_types`，再 `ROLLBACK`。**从不 `EXPLAIN`**（那
会真的规划甚至触发执行侧效应），**从不 `EXECUTE`**（那会真的写库）。

这正是本 skill 敢校验 `INSERT`/`UPDATE`/`DELETE` 一类写语句、且只读角色就够用的原因：
PostgreSQL 的执行期权限检查（`ExecCheckRTPerms`）发生在 `EXECUTE` 阶段，`PREPARE` 只做语法
分析与目录查找（列名/表名/类型是否存在），从不检查"这个角色能不能真的写"。于是只读角色下
校验一条写语句，得到的是"列名对不对"这个真问题的答案，而不是一句无意义的权限拒绝。

## 唯一安全边界

与 `pg-query-ro` 同一条边界：只读角色的 PostgreSQL ACL 才是**唯一**安全边界。本 skill 全程
不产生任何持久变更——不执行用户语句，不创建/修改/删除任何对象、角色或权限；会话在只读事务中
进行且以 `ROLLBACK` 收尾。本 skill 全链路 MUST NOT 生成也 MUST NOT 执行
`CREATE ROLE`/`CREATE USER`/`GRANT`/`REVOKE`/`ALTER ROLE`。

## 执行

```bash
<skill-dir>/scripts/pg-sql-check.sh --sql '<单条语句>'
```

- `--sql`：必填，单条语句（`SELECT`/`INSERT`/`UPDATE`/`DELETE`/`MERGE`/`VALUES`/`WITH` 等均可，
  读写语句获得同等校验能力）。多语句、psql 元命令、`set_config(...)` 一律被拒，退出码 3。

前置条件（按序检查）：

1. `${ROOT_DIR}/.dbmeta/` 必须存在，否则退出码 2，提示先跑 `/pg-dict`——本 skill 靠数据字典
   判定候选名，拼 SQL 前必须先有字典。
2. 只读通道必须已供给（同 `pg-query-ro` 的前置），否则透传 `shared/ro-session.sh` 的
   fail-closed 退出码 2。
3. 目标库 PostgreSQL 版本 MUST ≥ 16（契约快照读 `pg_prepared_statements.result_types`，该列
   PG16 起才存在）；低于此版本退出码 1。
4. 目标库连接 MUST 为直连或经 PgBouncer **session 池**——**transaction 池**下 SQL 级
   `PREPARE`/`DEALLOCATE` 不受支持（PgBouncer 官方特性矩阵标为 Never），本 skill 检测到该拓扑
   会 fail-loud（退出码 1），而不是静默返回一份空契约快照。

## 诊断双出：stdout 摘要 + JSON 产物

每次调用同时产出两份诊断，来自同一次判定：

- **stdout**：判定结论、SQLSTATE、PostgreSQL 原文消息、HINT（若有）、出错位置（已换算回你
  原文的坐标，换算不出会显式标注"位置相对注入脚本"）、候选名（若适用）。
- **JSON**：落 `build/pg-sql-check/<UTC 时间戳>-<语句内容 sha256 前 8 位>.json`，同一份判定的
  机器可读形态，字段含 `sqlstate`/`message`/`hint`/`position`/`parameter_types`/`result_types`/
  `candidates`。并发调用（含同一秒同一条 SQL）不互相覆盖（`set -C` 占名 + 序号后缀重试，与
  `pg-query-ro` 同一解法）。

`build/` 未被 git 忽略时会告警——诊断可能含业务字面量或 schema 结构细节。

## 契约快照（校验通过时）

校验通过时输出该语句的参数类型序列与结果列类型序列，反映 PostgreSQL 的实际类型推断：

- **写语句无结果列**：`result_types` 在 JSON 里是 **`null`**（PostgreSQL 官方语义：DML 无结果集
  时该字段为 NULL），**不是** `[]`、**也不会缺失该字段**——三者对下游消费方语义不同。
- 本 skill MUST NOT 把快照存为基线、MUST NOT 做跨次比对——每次调用都是一次独立判定。

## 候选名补全

PostgreSQL 自身已对多数错拼/截断/大小写给出 HINT，本 skill 只补它**不给** HINT 的两类：
① 未定义列且无 HINT；② 未定义表或 schema。候选名取自 `.dbmeta/`（编辑距离归一化 ≥ 0.6，最多
5 个，按相似度降序），标注来源与"可能滞后于活库，必要时重跑 `/pg-dict`"；无候选达阈值时显式
呈现"无候选"，不退化为罗列全库标识符。PostgreSQL 已给 HINT 时原样呈现该 HINT，不用候选名
取代它。

## 退出码

| 码 | 含义 | 典型场景 / 处置 |
|---|---|---|
| `0` | 校验通过 | 打印契约快照，随附 JSON 产物路径 |
| `1` | 硬错误 | 参数错误、PG 版本 < 16、PgBouncer transaction 池拓扑、SQLSTATE 提取不出、`42P18`（占位符类型推断不出，加显式 cast 如 `$1::int` 后重试）、非 class-42 硬错误 |
| `2` | fail-closed，需人介入 | `.dbmeta/` 缺失（先 `/pg-dict`）、只读通道未就绪（先 `/pg-readonly-setup`）、`42501` 权限不足——进一步分流：该 schema **在** `.dbmeta/` 范围内 ⇒ 供给未做对，重跑 `/pg-readonly-setup`；**不在**范围内 ⇒ 先把它加进 `SCHEMAS` 再重跑供给 |
| `3` | 护栏拒绝 | 多语句 / psql 元命令 / `set_config` |
| `4` | **校验不通过**——SQL 对不上真实 schema | 这是本能力的**预期主输出**，不是异常；诊断给出 SQLSTATE + 候选名（若适用） |

## 边界

- 只校验**单条**即席语句，不校验文件、不校验一批语句、不校验 DDL（`PREPARE` 语法本身不接受
  DDL；手滑语法错与 DDL 语句在本 skill 眼里同为 `42601`，统一诊断，不做区分——需要校验迁移/DDL
  文件另见 `docs/skills-roadmap.md` §3.2 的规划，本 skill 不做，也不指向任何具体 skill 名）。
- 不做基线比对、不做回归判定——每次调用只回答"这一次、对着这份 schema，这条语句校验通过吗"。
- 用户 SQL 的字面值不进入任何常驻日志；诊断 JSON 是 `build/` 下的一次性产物，不是日志。
