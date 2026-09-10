---
name: pg-dict
description: 从开发库 pg_catalog 生成/再生数据字典（`.dbmeta/<schema>/...`），并承接"孤立注记怎么处置"的人工判断。当用户说"生成数据字典"、"更新 pg-dict"、"再生字典"、"$pg-dict (Codex) or /pg-dict (other agents)"时触发。
license: MIT
metadata:
  author: db-llm
  version: "2.0"
---

把开发库的真实结构元数据（表/列/类型/默认值/可空性/索引/约束/触发器/函数/视图/`COMMENT ON` 注释/
`pg_class.reltuples` 行数估计——按数量级分档）渲染成仓库根 `.dbmeta/` 下、按 PG schema 分目录、按对象分文件的
知识库，供人与 LLM 双读。对象文件（`tables/`、`views/`、`functions/`）落盘为可执行 `.sql`
（DDL 而非人类叙述文字）；README/`_relations.md`/`_gaps.md`/`rules.md` 等索引与派生文档仍是
markdown。含用户对象的非系统 schema 自动全部纳入，不需要逐个登记。生成内容一律来自库内真实元数据，
不由模型推测填充。此外，render 会双路推导表间逻辑关联候选（列名模式 + COMMENT 文本）供人 review，
人工确认后 render 据此生成可选的 DB COMMENT 回写 SQL 片段（见下方「关系推导与确认流程」节）。

## 何时跑

- 消费项目的迁移脚本在 schema 结构发生变化后，若打印了字典再生建议提示——**不自动执行**，因为迁移
  在部署环境也会跑，字典生成只该发生在开发场景的显式触发下。看到这类提示，或人工确认 schema 确有
  变化时执行本 skill。
- 首次在新环境铺 `.dbmeta/` 时。

## Onboarding 顺序：先 `/pg-readonly-setup`，后本 skill

本 skill 采集元数据用的是只读角色凭据（`.dbmeta/.dbllm.env` 中的 DB_USER/DB_PASSWORD，
工具链唯一的数据库凭据出口——见 ADR-0006），不是管理员凭据。全新消费仓第一次接入时，先跑
`/pg-readonly-setup` 把只读角色从零供给到「探针可用」（它会顺带在 `.dbllm.env` 里生成密码），
通道报「只读通道可用」后再跑本 skill——本 skill 自己不做只读角色的供给/校验，直接跑只会在
collect 层因凭据未就绪而报错退出。

## SSH 隧道（远程库经跳板时自动处理）

若 `.dbmeta/.dbllm.env` 配置了 `SSH_HOST` 等隧道字段（见 `shared/.dbllm.env.example`
SSH 段），本 skill 底层的连接层（`db_llm_export_pg_env`）会在每次连接前自动确保隧道就绪
——已监听则复用，未监听则自动启动并等待端口可用，不需要人先手动跑
`shared/ssh-tunnel.sh start`。隧道启动失败会在 collect 层直接 fail-loud（三要素 + 排障命令），
同样不写不删任何 `.dbmeta/` 文件。

## 执行

```bash
<skill-dir>/scripts/pg-dict.sh
```

脚本会：
1. 调用同仓 `shared/db-collect.sh`——pg-dict 自身不再持有任何 pg_catalog 采集 SQL，元数据统一经
   这一个采集入口获取（它自己从消费项目的凭据文件读取 DB 连接参数）。若该脚本不存在/不可执行、
   连接失败，或输出不是 `collect_version==1` 的 JSON，pg-dict.sh 会在 collect 层直接报错
   （problem/cause/fix）并退出非 0，**不写不删任何 `.dbmeta/` 文件**。
2. `python3 <skill-dir>/scripts/render.py` 把采集到的 JSON 渲染为 `.dbmeta/` 全树：每个非系统 schema
   一个目录，表/视图/函数各一个 `.sql` 文件，与磁盘既有内容按托管块合并后落盘；同时（重）写
   `.dbmeta/_relations.md`（派生语义，含逻辑关联 Mermaid erDiagram）、`.dbmeta/_gaps.md`
   （缺口报告）、`.dbmeta/_relations.candidates.yaml`（关系推导候选）、
   `.dbmeta/_relations.pending.sql`（待执行的 COMMENT 回写 SQL）与两级 `README.md`
   （根 + 每个 schema）。render 若检测到对象名/schema 名/托管块名不满足标识符契约会 fail-loud，
   同样不写不删。

跑完检查终端的三组清单（已更新/已删除/无变化）与「缺注释：表 n / 列 n / 函数 n / 敏感 warning n」
汇总行；若某对象目录下还残留旧格式 `*.md`（`.sql` 迁移前的存量文件），终端会额外打印一行提示
（`[pg-dict] 发现旧格式对象文件 N 个（已改为 .sql，旧 .md 未读未删）：<path1>, <path2>, …；请人工
把 .md 里的注记搬到同名 .sql 后 git rm 这些 .md`）——render **不读不删**这些 `.md`，需人工把注记
（若有）搬到同名 `.sql` 后 `git rm`。`git diff .dbmeta/` 确认改动符合预期后再提交。

## `.dbmeta/` 目录契约

```
.dbmeta/
├── .dbllm.env                     # 消费项目配置+凭据（git 忽略，含密码）
├── .dbllm.env.example             # 模板（git 追踪，供团队参考）
├── README.md                          # 总入口：schema 清单 + 对象计数 + 缺注释计数
├── rules.md                           # 唯一手写真相件，render 永不创建/修改/删除它（见下）
├── _relations.md                       # 派生语义：逻辑关联（含 Mermaid erDiagram）/ 枚举取值 / 敏感列
├── _gaps.md                            # 缺注释缺口报告（沿用既有语义，见「缺注释缺口报告」节）
├── _relations.candidates.yaml          # 关系推导候选（生成件，每次 regen 全量重写，供人 review）
├── _relations.confirmed.yaml           # 人工确认的关系（人工维护件，render 只读不写）
├── _relations.pending.sql              # 待执行的 COMMENT 回写 SQL（生成件，每次 regen 全量重写）
└── <schema>/
    ├── README.md         # 该 schema 索引：表清单(名/COMMENT/行数估计/分区子表数)、视图、函数签名
    ├── _collect.json      # 该 schema 的采集切片（见「机器事实切片」节）
    ├── tables/<table>.sql
    ├── views/<view>.sql
    └── functions/<fn_name>.sql
```

消费项目的 `.gitignore` **不得**整体忽略 `.dbmeta/`——其中 `.dbllm.env.example`（模板）与
`_relations.confirmed.yaml`（人工确认的关系）需要 git 追踪，否则会随 clone 丢失。
`.dbllm.env` 本身含凭据，MUST 被 `.gitignore` 覆盖。

`.dbmeta/` 路径固定为 `${ROOT_DIR}/.dbmeta/`，不可通过配置更改（无 `DBMETA_DIR` 键）；不再有
`backfill/` 占位目录（存量 COMMENT 补全职责归后续独立的 `db-comment` skill，产物落点由该 skill
自定）——遗留的 `.dbmeta/backfill/` 与任何未知目录同等处理：只清理 render 认识的托管文件，手写文件
原样保留，空目录随收敛清理。

阅读顺序（消费者约定，见 `.dbmeta/README.md` 固定文案）：根 `README.md` → 对应
`<schema>/README.md` → 具体对象文件；表/列 join 依据看 `_relations.md`；查询侧通用过滤规则看
`rules.md`。`/dynapi-gen`、`/new-listpage` 写 SQL / 判定字段前都按这个顺序读。

## 关系推导与确认流程

render 每次运行都会双路推导表间逻辑关联候选，供人工 review 后手动确认——**pg-dict 不自动写 DB**，
写 `COMMENT` 落库永远是人的动作：

1. **推导**（每次 regen 全量重写 `_relations.candidates.yaml`）：
   - **列名模式**：扫描全部顶级表（分区子表除外）里 `_id` 结尾的列，按 stem 在全库顶级表中找
     `<stem>` 或 `<stem>s` 表，目标表有 `id` 列取 `id`、否则取同名列，否则不产出候选；自引用
     （source 表列 == target 表列）被过滤。
   - **COMMENT 文本**：扫描顶级表列 COMMENT 中形如 `<schema>.<table>` 的全限定引用（排除已经是
     `逻辑关联 ...` 约定格式的片段），裸表名仅在全库唯一时命中，目标列固定 `id`。
   - 两路候选按 `(source, target)` 去重（列名模式优先），并排除已在 `_relations.confirmed.yaml`
     中出现（含 `ignore: true` 的显式忽略条目）或已用 `逻辑关联` 约定格式标注过的对。
2. **人工确认**：打开 `_relations.candidates.yaml`，把认可的候选原样搬进
   `_relations.confirmed.yaml`（render 对该文件只读不写，格式不合规的条目会被跳过并告警、不阻断
   regen）；不想要的候选可以什么都不做（下次 regen 若信号仍在会再次出现），或显式写一条
   `ignore: true` 的条目使其永久不再出现。
3. **回写**（每次 regen 全量重写 `_relations.pending.sql`）：依据 `_relations.confirmed.yaml`（排除
   `ignore: true`）生成 `COMMENT ON COLUMN ... IS '... 逻辑关联 ...'` 语句，只追加该列现有 COMMENT
   （从采集结果读取）里尚未包含的 target；全部已包含则跳过该 source。这些语句**不会**被自动执行，
   人工 review 后自行用 `psql` 或迁移工具落库；落库后 COMMENT 里的 `逻辑关联 ...` 约定格式会在下次
   regen 被识别进 `_relations.md`，候选与 `_relations.confirmed.yaml` 中的对应条目也会因此不再重复
   出现。
4. **展示**：`_relations.md` 的逻辑关联节数据源 = 已用 `逻辑关联` 约定格式标注的存量关系 ∪
   `_relations.confirmed.yaml`（排除 `ignore: true`）的合集，并在文字列表上方生成一份 Mermaid
   `erDiagram` 全貌图（`<target> ||--o{ <source> : "<列名>"`，`.` 换 `_` 后的 `schema_table`
   命名）。

文件名与托管块名 MUST 与 PG 对象名逐字相同（含点号，如 golang-migrate 建的
`example.schema_migrations` 原样落盘为 `example.schema_migrations.sql`）。分区子表 MUST NOT 拥有
独立文件——折叠进父表文件的托管块内，渲染为「分区子表（N）」清单行；只有子表自身持有非父表下推的
索引/约束时才单列；父表不在本 schema 时（跨 schema 分区、或多级分区中间层）子表回退为独立文件，
按普通表同等计入缺注释统计，不被静默丢弃。

## 一文件一块：合并、孤立与删除语义（人工判断指引）

每个对象文件恰一个托管块（函数文件是唯一例外——每个重载各占一块，块集合 = 该名下全部现存重载）。
对象文件（`.sql`）用 `--` 注释形式的托管块标记：表 `-- pg-dict:table:<name>:start` …
`-- pg-dict:table:<name>:end`，视图 `-- pg-dict:view:<name>:start/end`，函数
`-- pg-dict:fn:<name>(<identity_args>):start/end`；索引文件（根/schema `README.md`，仍是
markdown）沿用 HTML 注释形式 `<!-- pg-dict:index:start/end -->`。

- **块内**：每次再生整体重写，不要手动编辑——下次再生会覆盖。
- **块外**：人工写的注记（如"此列已废弃，勿在新查询引用"、业务背景说明）逐字保留、不会被再生冲掉，
  可放在任意块之间或文件末尾；每个文件固定的文件头（对象文件是 `--` 注释行，README 是标题 + 紧随
  的说明性 HTML 注释）不算"块外人工文本"，不会触发孤立标注。**对象文件（`.sql`）里人工注记 MUST
  写成 `--` 注释行**——块外若混入非注释的裸文本，该文件即不再是合法 SQL，`psql -f` 执行会报语法
  错误（DD-7；render 不校验这一点，见下方 Risks 引用）。
- **对象从库中消失**：若其文件在托管块外无非空、非注释文本 ⇒ 再生 **删除该文件**（随后清理空
  目录）；否则删除托管块、保留文件，并在文件顶部插入一行孤立标注（幂等，不重复插入）；对象文件
  （`.sql`）用 `--` 注释：
  ```
  -- pg-dict: 孤立注记（原挂靠对象已从数据库删除，人工确认是否仍需保留）
  ```
  （README 索引文件仍用 `<!-- pg-dict: 孤立注记（原挂靠对象已从数据库删除，人工确认是否仍需保留） -->`。）
  **看到这个标注时人工判断**：注记已无意义 → 连同标注行一起删；仍有存档价值（迁移历史/废弃原因）
  → 保留，标注行本身可手动删掉（不会被重复添加，不影响后续幂等）；不确定 → 保留现状，留给下一个
  看到这份字典的人判断。
- **整个 schema 消失**（本次采集结果不再包含该 schema，含"连错库导致覆盖 schema 集合缩小"这一种
  形态——见下方「异常情况」）：render 起手会比较磁盘上 `.dbmeta/*/` 目录集合与本次采集的 schema
  名集合，差集中每个目录按同一规则逐对象处理——`_collect.json`/`README.md` 是纯生成件直接删除，
  `tables/views/functions/` 下逐个对象文件走上面「消失」规则，最终清理空目录。这是设计既定行为，
  被删的都是生成物，`git checkout`/`git revert` 可恢复。

## 约束/触发器/函数/视图渲染

表文件渲染 `CREATE TABLE`/`ALTER TABLE ... ADD CONSTRAINT`（主键/唯一/检查/排他，NOT NULL 除外）。
外部表（`foreign_table`）改为 `CREATE FOREIGN TABLE … SERVER …[ OPTIONS (…)]`，列级 `OPTIONS` 随
列定义；非 heap 访问方法出 `USING <am>`，toast.* 存储参数与其它项同列于 `WITH (…)`。
与触发器 `CREATE TRIGGER`；触发器所调函数若在本次采集集内，以 `--` 注释形式的相对链接指向其函数
文件（`../functions/<fn>.sql`，跨 schema 时 `../../<schema>/functions/<fn>.sql`），扩展/系统函数
（未采集到）只写名不链接。函数文件渲染每个重载的 `CREATE OR REPLACE FUNCTION` 完整外壳（签名、
返回类型、语言、`AS $function$...$function$`、`COMMENT ON FUNCTION ...`）与「被以下触发器引用」
反向清单（`--` 注释）。视图文件渲染 `CREATE OR REPLACE VIEW ... AS <definition>` 与
`COMMENT ON VIEW`。

### 对象文件头（DD-7）

每个对象文件固定的文件头是 `--` 注释行，逐字一致，不随对象内容变化：

```
-- <schema>.<name> 表|视图|函数
-- 本文件由 pg-dict skill 自动生成。托管块（-- pg-dict:<ident>:start/end）内容会在再生时整体重写；
-- 块外文本由人工维护，再生时逐字保留，人工注记 MUST 写成 -- 注释行，否则本文件不可执行。
```

表文件在此之后多一行「表 DDL 由 pg_catalog 拼装（不含 collation/所有者/权限），完整重建
以 `pg_dump` 为准；触发器引用的函数在 `../functions/` 下，单文件不保证整库重放顺序」；函数文件多
一行说明"每重载一块"。文件头本身不算"块外人工文本"，不会触发孤立标注；它是 `_has_annotation` 的
豁免串，与 `object_file_header()` 逐字一致（改文件头文案需同步改 `render.py` 与本节）。

## 机器事实切片（`_collect.json`）

每个 schema 目录含 `_collect.json`：`{"collect_version": 1, "schema": {...}}`，`schema` 为
`shared/db-collect.sh` v1 输出中该 schema 对象原样，但**不含** `collected_at`/`database`（运行期
字段）与 `reltuples`（随 PG autovacuum ANALYZE 抖动的统计字段；行数估计只在表对象文件（`.sql`）
与 schema README 以数量级分档呈现，跨数量级才产生 diff）；序列化确定性（缩进 2、保持采集输出键序、非 ASCII 不转义），schema 无变化时逐字节
幂等。

## 派生语义（`_relations.md`）

只由 COMMENT 中符合约定格式的片段机械派生，不由模型推测：逻辑关联取
`逻辑关联 <schema>.<table>.<column>`（三段全限定，`openspec/rules/database.md` §3.1 COMMENT 机读
约定）；枚举取 COMMENT 内 `<整数>=<文本>` 连续序列（≥1 项即收）；敏感列沿用 `_gaps.md` 同款关键词
判据，但覆盖全部命中者（含已声明脱敏的，标注"已声明脱敏"）。不符合格式的写法不产生条目，也不计入
`_gaps.md` 缺口——三节按目标表/表分组。

## `rules.md`（唯一手写件）

`.dbmeta/rules.md` 收查询侧写 SQL 时必须遵守的规则（软删除过滤/审计字段/禁 ORM/禁 BOOLEAN/禁
VARCHAR/禁数组/无外键/`internal/utils/mdb` 参数语法/PgBouncer 限制等），render **永远不会**
创建、修改或删除它——不存在时也不会自动补一份。改它直接编辑该文件，不经 pg-dict 再生。

## 缺注释缺口报告（`.dbmeta/_gaps.md`）

无开关、每次运行整文件重写（没有人工维护成分），列出：缺表注释的表、缺列注释的列、缺函数注释的
函数（以名称+参数签名联合定位，避免同名重载互相遮蔽），以及敏感列名 warning（列名命中密码/令牌/
密钥/手机号/邮箱/证件号类关键词，且该列 `COMMENT` 未提及脱敏/密文/哈希/加密者）。分区子表（父表在
本 schema、被正常折叠者）不计入任何一节；缺逻辑关联/缺枚举约定同样不计入。这是一份**待补清单**，
被列入敏感 warning 不代表该字段实际无保护，只代表数据库注释里没写明——补上说明后下次再生自动消失。
报告不含时间戳等随运行变化的字段，schema 无变化时内容逐字节幂等。

## 输出摘要与幂等性

schema 无变化时连续跑两次，`.dbmeta/` 下全部文件（含 `_collect.json`）逐字节一致（脚本输出对应
文件计入"无变化"而非"已更新"，`written`/`deleted` 均为空数组）。若发现同一份文件反复被判定为
"已更新"但看不出实质差异，那是 bug，去看 `render.py` 的合并逻辑（`render_test.py` 有 fixture
单测）。

## 异常情况

- **`shared/db-collect.sh` 不可用/输出非法**：pg-dict.sh 会在调用前先检查它存在且可执行；缺失/不可
  执行/连接失败/非 `collect_version==1` JSON 都在 collect 层直接报错退出，**不会**写入或覆盖任何
  `.dbmeta/` 文件——不存在"半份字典"的风险。想单独排查采集层，直接运行
  `shared/db-collect.sh --out /tmp/c.json`（或不带 `--out` 看 stdout）复现，不需要经过本 skill。
- **render 中途异常退出**：不做整树原子提交（临时目录 + rename 成本高、发生概率低），异常退出可能
  留下半棵树。render 本身幂等——**直接重跑即收敛**，不需要先手动清理。
- **连错库（如误连测试库/其他项目库）导致本次覆盖的 schema 集合比磁盘上 `.dbmeta/*/` 小**：会触发
  上面「整个 schema 消失」路径，把磁盘上多出来的 schema 目录当作已删除处理——这是设计既定行为，
  不是 bug；被删的是生成物，`git checkout`/`git revert` 可恢复。连错库时先核对消费项目的凭据配置，
  恢复后重跑一次收敛回正确状态。
- **`.dbmeta/.dbllm.env` 不存在**：pg-dict.sh 在读取任何数据库凭据前会先做自动初始化判定
  （不写任何字典文件，只处理配置本身）：自动
  `mkdir -p .dbmeta/`、从 `shared/.dbllm.env.example` 复制一份到 `.dbmeta/.dbllm.env`，
  提示先填入连接信息、跑 `/pg-readonly-setup` 供给只读角色、通道可用后再重跑本 skill，`exit 1`；
  模版本身缺失（安装不完整）同样 fail-loud、`exit 1`。所有分支均在采集/渲染动作之前退出，**不写
  任何 `.dbmeta/` 字典文件**——未配置的仓库不会被误操作。
