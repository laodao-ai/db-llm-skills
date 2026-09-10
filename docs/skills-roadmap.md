# db-llm 规划：目标态与实施路线

> **本文是 db-llm 全部 skill 规划的唯一真相源。** 只覆盖 L0–L4（采集 / 认知 / 校验 / 变更 /
> 数据）四层；L5 运维层属姊妹仓 `pg-ops`（见该仓自己的 `docs/skills-roadmap.md`）。
> 新增 / 改名 / 去重任何 skill，先改本文的 §2 落位表。

---

## 0. 怎么读这份文档

### 0.1 分区

| 节 | 装什么 | 谁看 |
|---|---|---|
| §1 | **目标态**：为什么要这一套、按什么轴分层 | 第一次接触本仓的人 |
| §2 | **落位表**：本仓 skill 一张表，已实现 + 规划中 | 想知道「有没有这个 skill」 |
| §3 | **规划中的逐个**：做什么 / 依据 / 前置 / 成本 | 准备开下一个 change |
| §4 | **实施顺序与依赖** | 决定先做哪个 |
| §5 | **规格来源**：未来 skill 的输入规格（规则引擎格式、验证库方案） | 真正动手写那个 skill 时 |
| §6 | **待拍板 · 待核** | 卡住时看这里 |
| §7 | 落位约定 | 新增 skill 时 |

### 0.2 与 pg-ops 侧同名文档的关系

`pg-ops` 仓也有一份 `docs/skills-roadmap.md`，管它自己的 L5 运维层六个 skill（`pg-sizing` /
`pg-prod-server` / `pg-backup` / `pg-roles` / `pg-monitor` / `pg-tune`）。两份文档各管各线，互不
引用彼此的落位表。

`openspec/architecture/sad.md`（SAD）**不退役**：它管**空间轴**（本仓子系统边界与 contract 定义），
本文管**时间轴**（做什么、什么顺序、卡在哪）。分层依据引自 SAD / ADR-0005；skill 清单与状态以本文为准。
两者冲突时，先改本文，再回头同步 SAD 的成熟度标记。

---

## 1. 目标态

### 1.1 不是一个数据字典引擎，是一套「大模型访问数据库」工具面

目标态是**覆盖 AI 辅助开发全生命周期里「碰数据库」这半的工具面**：从采集底座到读 / 校 / 改 / 数据
各层的一组 skill，服务任意 PostgreSQL 项目，本仓零项目知识。

AI 参与开发后，数据库痛点的性质变了——人的痛点是「麻烦」，AI 的痛点是**「自信地错」+「一发不可收」**：

| 维度 | 人 | AI |
|---|---|---|
| 记忆 | 脑子里有 schema | 每次 fresh context，靠猜列名 |
| 错误形态 | 犹豫、去查 | 编一个像样的列名，SQL 语法正确、语义错 |
| 破坏半径 | 手抖一次 | 一个循环里跑 50 次，`DROP` 也只是一次工具调用 |
| 知识沉淀 | 口口相传 | 只有脚本 / 文件里的东西才「存在」 |

### 1.2 三条设计原则（所有 skill MUST 遵守）

1. **真相机器可读**：schema 知识来自 `pg_catalog` 再生的文件（`.dbmeta/`），不是 wiki，不是记忆。
2. **写权限在 PG 角色层 enforced**：只读靠角色 ACL，不靠 prompt 里说「别删」。
3. **每个操作是可复跑脚本 + 明确出口**：成功 / 失败 / 需要人做什么，三态清楚；agent 看不见终端，
   所以「需要人做什么」MUST 以**产物文件**形式交付（stdout 只给路径）。

**安全边界（高于任何单个 skill）**：MUST NOT 有任何 skill 持有生产库的写凭据。写类 skill 的执行动作
要么在 dev 库自动跑，要么产出文件交人在受控通道执行。

这三条已经在已实现的 4 个 skill 里兑现（`.dbmeta/` 文件化真相、`llm_readonly` 角色 ACL、
`needs-human.md` 产物式出口、特权 SQL 只生成不执行），规划中的继承同一套。

### 1.3 生命周期 × 该考虑的问题（本仓覆盖的部分）

```
+----------+------------+------------+-----------+
| 建模     | 结构变更   | 查询/API   | 数据      |
+----------+------------+------------+-----------+
|规范怎么定|迁移怎么写  |列名对不对  |初始数据   |
|关系怎么表|谁审/谁拦   |计划好不好  |测试数据   |
|注释纪律  |up/down对称 |N+1/全表扫  |脱敏回灌   |
|敏感列标记|prod 预演   |分页/排序   |一次性修复 |
+----------+------------+------------+-----------+
  AI 视角:   猜列名 <----------- 这三格是 AI 最高频出错区 ----------->
             破坏性 <-- 这几格是 AI 最不该直接碰、要「AI 准备、人执行」的区 -->
```

优先级不按阶段顺序，按 **出错频率 × 破坏半径**：

- **高频低害**（查询列名错、迁移违反规范）→ 要**快反馈的校验器**，agent 循环内自动跑。
- **低频高害**（数据修复）→ 要**产物式接口**，AI 只准备，人执行。

（「测试 / 发布 / 运维」三格及其对应的 L5 运维层属姊妹仓 `pg-ops`。）

### 1.4 四子系统分层（依据 ADR-0005，本仓部分）

轴是**破坏半径 / 写模型 / 触发频率**——读 → 校 → 改 → 数据，越往下越收紧。

```
 L0 采集底座 shared/     凭据缝 · 采集引擎 · 只读角色供给 · 连接/超时上界     [已实现大半]
        |
        +-- L1 认知（读）   零副作用，随便跑                              [已实现]
        |     pg-dict · 关系推导/ER 图 · pg-query-ro
        |
        +-- L2 校验（校）   agent 循环内自动跑，零写入                     [部分已实现]
        |     pg-sql-check（已实现）· pg-migrate-lint · pg-explain
        |
        +-- L3 变更（改）   只写临时验证库或只产文件                       [规划 3]
        |     pg-migrate-gen · pg-migrate-verify · pg-comment
        |
        +-- L4 数据         干跑 -> 影响面 -> 人确认 -> 落库 + 留痕        [规划 3]
              pg-seed · pg-anonymize · pg-datafix
```

**跨子系统契约**（SAD §5 定义，本文只列名字）：`collect_version=N` JSON · `.dbllm.env` 配置缝 ·
`.dbmeta/` 真相面 · 只读角色 · `candidate→confirmed→pending.sql` 机制 · `pg-rules.toml` ·
`tmpdb-replay-verify` 临时验证库。

---

## 2. 落位表

**状态**：✅ 已实现（`setup.sh` 已登记）· ⬜ 规划（`setup.sh` MUST NOT 登记）

| skill | 层 | 状态 | 一句话 | 依据 |
|---|---|---|---|---|
| `pg-readonly-setup` | L0/L1 | ✅ | 只读通道单一可重入入口：预检 → 供给 → 采集 → 冒烟 | 只读线 P1 |
| `pg-dict` | L1 认知 | ✅ | `pg_catalog` → `.dbmeta/` 字典 + 关系推导 + ER 图 | 只读线 P1 |
| `pg-query-ro` | L1 认知 | ✅ | 即席只读查询唯一入口，结果落文件 | 只读线 P1 |
| `db-llm-upgrade` | 工具 | ✅ | 运行 checkout 升级三连 | — |
| `pg-sql-check` | L2 校验 | ✅ | 对单条 SQL 语句跑 `PREPARE` 核验（列名 / 类型 / 参数占位） | 只读线 P2 |
| `pg-migrate-lint` | L2 校验 | ⬜ | 迁移文件规范守卫，squawk + 项目规范四 kind | 只读线 P5 |
| `pg-explain` | L2 校验 | ⬜ | 执行计划解读 + 索引建议（晚做） | 只读线 P8+ |
| `pg-migrate-gen` | L3 变更 | ⬜ | 从意图生成迁移对，只产文件不执行 | 只读线 P7 |
| `pg-migrate-verify` | L3 变更 | ⬜ | 空库从零重放 + baseline 比对 | 只读线 P6 |
| `pg-comment` | L3 变更 | ⬜ | 从 `_gaps.md` 缺口起草 COMMENT → 人确认 → pending.sql | 只读线 P4 |
| `pg-seed` | L4 数据 | ⬜ | 幂等测试 / 初始数据 | 只读线 P8+ |
| `pg-anonymize` | L4 数据 | ⬜ | 生产快照脱敏回灌开发库 | 只读线 P8+ |
| `pg-datafix` | L4 数据 | ⬜ | 一次性业务数据修复通道（晚做，需求未真实出现） | 只读线 P8+ |

（姊妹仓 `pg-ops` 另有 6 个 L5 运维层 skill：`pg-sizing` / `pg-prod-server` / `pg-backup` /
`pg-roles` / `pg-monitor` / `pg-tune`，见该仓自己的 `docs/skills-roadmap.md`。）

---

## 3. 规划中的 6 个

### 3.1 L2 校验层（2 个规划中）

零写入，agent 循环内自动跑。这一层的价值密度最高——它直接打 §1.1 表里「AI 自信地错」那一格。

`pg-sql-check` 已实现，见 README「Skills」表。

#### `pg-migrate-lint`

- **做什么**：迁移文件规范守卫——禁 FK / VARCHAR / BOOLEAN / 数组、审计字段齐全、每表每列有 COMMENT、
  up/down 成对。安全规则交 `squawk` 依赖，自有引擎只做项目规范四 kind。
- **规则来源**：见 §5。skill 内置缺省 + 项目 `.dbmeta/pg-rules.toml` 整份覆盖（存在只读它，不合并）。
- **前置**：无技术前置；与 `pg-migrate-gen` 规则同源，roadmap 建议捆一个 change。
- **成本**：中。规则引擎的 `kind` 集合就是它的能力面，新增 rule 不改代码。

#### `pg-explain`（晚做）

- **做什么**：执行计划解读 + 索引建议，输出人读得懂的报告。
- **为什么晚**：模板期用不上，项目上量后才需要。

### 3.2 L3 变更层（3 个）

只写临时验证库或只产文件。这一层是「AI 准备、人执行」的分水岭。

#### `pg-comment` — 语义补全

- **做什么**：从 `.dbmeta/_gaps.md` 的缺口 + DDL / 迁移文件线索，起草每个表 / 列的 COMMENT 草案 →
  人在 YAML 逐条确认 / 修改 → 产 `pending.sql` → 经迁移通道落库 → 再生字典。
- **为什么要有**：COMMENT 是 `.dbmeta/` 语义的唯一来源，**光靠人写不会发生**。
- **机制复用**：复用 `pg-dict` 已实现的「候选 → 确认 → pending.sql」机制，但**文件独立**——
  `_comment.{candidates,confirmed}.yaml` + `_comment.pending.sql` 三个文件由本 skill 单写，
  `pg-dict` 再生 MUST NOT 触碰。理由：`_relations.pending.sql` 每次 regen 由 confirmed 全量重写，
  共用文件会被擦掉。
- **边界**：**只看 DDL + 迁移文件，不读应用代码**（2026-08-30 拍板），保持 skill 与项目语言 / 布局无关；
  线索不足就产「空草案 + 待人填」，不猜。
- **前置**：无。`pg-dict` 的候选机制已实现，可直接复用。

#### `pg-migrate-verify`

- **做什么**：**空库从零重放** + 与 `.dbmeta/` 比对；有 down 则追加 down→up 往返。
  超阈值时用 `pg_dump` baseline 加速（记有序迁移清单 + 逐文件哈希 + PG 版本，身份不匹配则拒用并全量重放）。
- **验证库方案的演进**（重要，别退回旧方案）：原方案是 `CREATE DATABASE tmp_<run> TEMPLATE <dev>` 克隆，
  **2026-08-30 已否决**——模板库不能有活跃连接。改为 `template0` 空库从零重放，直连 5432。
- **前置**：目标库需 `CREATEDB`（pg-ops 的 `pg-dev-init` 的 `CREATEDB=1` 开关已实现，正是为此）。

#### `pg-migrate-gen`

- **做什么**：从「意图」生成迁移对（up/down 对称、编号、COMMENT 齐、过 lint）；**只产文件，不执行**。
  走 roll-forward，down 可选，hazards 需人 approve。
- **前置**：`pg-migrate-lint`（规则同源）+ `pg-migrate-verify`（生成后要能验）。是这一层的最后一个。

### 3.3 L4 数据层（3 个）

干跑 → 报影响面 → 人确认 → 落库 + 留痕。**按拉动做，不预支。**

| skill | 做什么 | 何时才做 |
|---|---|---|
| `pg-seed` | 幂等测试 / 初始数据：清理标记、可重放、按环境 | 项目需要可重放 fixture 时 |
| `pg-anonymize` | 生产快照脱敏回灌开发库，敏感列来自 `.dbmeta/` 的敏感标记 | 与 pg-ops 的 `pg-sync` 配套（跨仓协作，落地时再定接口） |
| `pg-datafix` | 一次性业务数据修复通道：干跑（ROLLBACK）→ 报影响行数 → 人确认 → 落库 + 留痕 | **需求尚未真实出现，不预支** |

---

## 4. 实施顺序与依赖（本仓部分）

```
 梯队   skill                          为什么排这里
 ----   ----------------------------   -----------------------------------------------
  一     1. pg-sql-check（已实现）      无前置 · 有雏形 · agent 循环内每天受益
         2. pg-comment                 无前置 · 复用已实现的候选机制 · 补 .dbmeta 语义
 ----   ----------------------------   -----------------------------------------------
  二     3. pg-migrate-verify          独立 · 不碰规则引擎 · CREATEDB 已实现
         4. pg-migrate-lint    ]       规则同源，捆一个 change
         5. pg-migrate-gen     ]
 ----   ----------------------------   -----------------------------------------------
  三   6+. pg-anonymize · pg-seed ·    按拉动，不预支（§3.3）
             pg-explain · pg-datafix
```

- **梯队一内部无依赖**，谁先取决于消费仓当下在干什么：高频写查询 / 改 API ⇒ `pg-sql-check`（已实现）；
  做 schema 评审 / 新人接手 / 给 AI 补语义 ⇒ `pg-comment` 先。
- 依赖图：`pg-migrate-lint` + `pg-migrate-verify` → `pg-migrate-gen`（两条硬依赖，`pg-migrate-gen`
  需要规则引擎与验证器都先落地才能校验自己生成的迁移）。其余全是建议顺序，不是技术依赖，允许按
  拉动重排。

（`pg-ops` 侧的 L5 运维层实施顺序见该仓自己的 `docs/skills-roadmap.md` §4。）

---

## 5. 规格来源

> 本节是未来 skill 的**输入规格**，不是路线图。只在真正实施某个 skill 时读对应小节。

### `pg-rules.toml`：规范来源 = 整份复制到项目

- skill 内置有观点的缺省（禁 FK / VARCHAR / BOOLEAN / 数组、审计字段、COMMENT 必填……）。
- 项目首次使用时**整份复制**为 `.dbmeta/pg-rules.toml`，之后在项目内直接改：可禁用缺省 rule、改参数、
  **新增自定义 rule**。
- 解析规则：`.dbmeta/pg-rules.toml` 存在 ⇒ **只读它**；不存在 ⇒ 读 skill 缺省。**不做合并**——
  一份文件即全部规则，所见即所得。
- **代价（已知、接受）**：skill 升级新增的缺省 rule 不会自动进项目。配套一个 `pg-rules diff` 子命令
  列出「缺省有、项目没有」的 rule id，由人决定是否手工引入。
- **rule 格式**（草案，落地时在 `pg-migrate-lint` 的 change 里定稿）：每条 rule 自描述，
  lint 引擎不硬编码任何具体规则，只实现有限几种 `kind`：

```toml
[[rule]]
id       = "no-varchar"          # 稳定 id，diff / 禁用 / 报告都靠它
enabled  = true
severity = "error"               # error | warn
kind     = "regex"               # regex | column-type | table-columns | comment-required | ...
pattern  = '(?i)\bVARCHAR\s*\('
message  = "禁止 VARCHAR，统一用 TEXT（长度校验在应用层）"

[[rule]]
id       = "audit-columns"
enabled  = true
severity = "error"
kind     = "table-columns"       # 每个 CREATE TABLE 必须含这些列
columns  = ["enabled", "created_at", "updated_at", "is_deleted"]
message  = "缺统一审计字段"
```

`kind` 的集合就是引擎的能力面：`regex` 覆盖大多数「禁止 X」；结构类（审计字段、COMMENT 必填、
up/down 成对）各一种 kind。**新增 kind 才需要改引擎代码，新增 rule 不需要。**

### 验证库：空库从零重放（TEMPLATE 克隆方案已否决）

需要写的 skill（`pg-migrate-verify` / `pg-seed` / `pg-comment` 落库 / `pg-datafix` 干跑）共同特点是
「跑完就该消失」，常驻库会积累脏状态。

| 方案 | 隔离 | 成本 | 结论 |
|---|---|---|---|
| A 常驻共享 test 库 | 弱：多次 verify 互相残留 | 0 | 适合 e2e，不适合往返验证 |
| B 本地 docker 一次性 PG+PgBouncer | 强 | 中：每机装 docker | 与远程 PG 版本/扩展漂移；作无远程 PG 项目的备选 |
| ~~C 同服务器 TEMPLATE 克隆~~ | 强 | 低 | **已否决**：模板库不能有活跃连接 |
| **D 空库从零重放（已定）** | 强 | 低 | `CREATE DATABASE tmp_<run>`（template0，直连 5432）→ [载入 baseline] → 重放迁移 → collect → 比对；trap 保证 `DROP`（含失败路径） |

baseline 记「有序迁移清单 + 逐文件哈希 + PG 版本」，**前缀身份不匹配则拒用并全量重放再生**。

---

## 6. 待拍板 · 待核

| # | 事项 | 阻塞什么 | 现状与倾向 |
|---|---|---|---|
| 1 | `pg-anonymize` 独立成 skill 还是做成 pg-ops `pg-sync` 的一个阶段 | L4 数据层的切法（跨仓协作面） | pg-ops 的 `pg-sync` 已有 `DATA_RULES` 按表选数据的机制，脱敏是天然的下一道；具体接口留待实施时与 pg-ops 侧协商 |

---

## 7. 落位约定

1. **新增 / 改名 / 去重任何 skill，先改本文 §2 落位表**，再动代码。
2. `setup.sh` 的 `install_skill` 列表**只登记已实现的 skill**；规划中的 MUST NOT 登记。
3. skill 实现完成后：§2 状态改 ✅ → `setup.sh` 登记 → `README.md` 的 Skills 表加一行 →
   `docs/skills-guide.md` / `.html` 加使用说明。
4. 与 SAD（`openspec/architecture/sad.md`）的分工：SAD 管子系统边界与 contract（空间），本文管
   清单、顺序、卡点（时间）。本文改了之后，回头同步 SAD 里对应的成熟度标记。
