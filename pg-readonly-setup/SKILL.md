---
name: pg-readonly-setup
description: 消费仓只读通道的单一入口——首次接入与重跑同一个入口：环境预检、配置文件自动创建、只读角色（`DB_USER`，缺省 `llm_readonly`）供给/收敛、首次字典采集、只读查询冒烟，由预检定位从哪一阶段接话。当用户说"接入向导"、"新项目接入 db-llm"、"onboarding 这个仓"、"从零配置只读通道"、"配置只读角色"、"初始化只读通道"、"llm_readonly 角色"、"SCHEMAS 改了重跑供给"、"密码漂移了"、"/pg-readonly-setup"时触发。不做查询——查询走 /pg-query-ro。开发库与其 owner 角色由 pg-ops 的 `/pg-dev-init` 建立（本仓是它的消费仓）；本 skill 只管只读通道 / 字典 / 查询这条线，不建库、不建 owner。
license: MIT
metadata:
  author: db-llm
  version: "3.0"
---

## 定位

消费仓只读通道的**单一入口**。把「环境预检 → 配置文件自动创建 → 用户填写连接信息 →
只读角色供给编排 → 首次字典采集 → 只读查询冒烟」串成一条可重入的流程，首次接入与重跑走
同一入口，由 preflight 定位从哪一阶段接话。快乐路径下人只需要在两个坐席里出现：一次填写
`.dbllm.env` 配置文件，一次（异步）由 DBA 执行生成好的 `setup.sql`。

本 skill 自己**不**发明任何新的判定或写库逻辑——预检是新脚本，其余全部编排既有的
`pg-dict` / `pg-query-ro` 两个 skill 入口与自身的供给编排脚本，原样尊重它们的退出码契约。

**敏感信息不进对话**：数据库连接信息（host/port/db/password）不通过访谈收集——agent 自动
创建配置文件模版，用户在编辑器中自行填写。

## 执行

```bash
<skill-dir>/scripts/preflight.sh
```

零 DB、零网络，stdout 输出单个 JSON 对象：

```json
{"checks": {"<check_id>": {"status": "ok|fail|skip", "detail": "..."}},
 "stage": "<resume-stage>",
 "blockers": ["<check_id>", ...]}
```

退出码语义：`0` = 预检本身完成（结论都在 JSON 里）；`1` = 预检自身跑崩了（内部错误），
把 stderr 贴给人，不往下走。

`stage` 值域固定：`deps → gitignore → config → provision → collect → smoke →
done-unknown`。这是**起点建议**，不是权威判定——`provision` 及之后的真实进度预检静态不可知，
权威判定来自实际跑一次底层脚本的退出码。

密码字段在 JSON 里只呈现 `absent|placeholder|set` 三态，**永远不含值**。

## stage 处置

拿到 JSON 后先看 `blockers` 是不是空的——这是唯一决定「能不能往下走」的字段。再看 `stage`
决定从哪一步接话：

- `deps` → 缺 psql/python3，或本地 db-llm 安装不完整。按 `checks` 里对应项的 `detail`
  给出该平台的安装指令（macOS: `brew install libpq`，Linux: `apt`/`yum`）；引擎安装不完整
  则指向 `./setup.sh` 重装。**不要自动执行任何安装命令**——只给指令，等人装完重跑预检。

- `gitignore` → `.gitignore` 未覆盖 `.dbmeta/.dbllm.env` 或 `.dbmeta/db-readonly/` 路径。
  **自动写入** `.gitignore` 追加行（不需要人确认），写入后**重跑预检**确认 `blockers` 清空。
  若 `.dbmeta/.dbllm.env.example` 被误整目录忽略（应该是 tracked），加排除规则
  `!.dbmeta/.dbllm.env.example`。

- `config` → 配置文件缺失或字段仍为占位符。执行配置文件创建规则（见下方），然后告诉用户
  编辑 `.dbmeta/.dbllm.env` 填入连接信息，等用户确认后重跑预检验证。

- `provision` → 只读通道未就绪（密码仍为占位符，或 `setup.sql` 在途）。跳过配置，直接跑
  `<skill-dir>/scripts/readonly-setup.sh`，由它判定是首次生成还是等 DBA。

- `collect` → 只读通道已就绪但 `.dbmeta/` 还没有字典树。直接调用 `pg-dict.sh`。

- `smoke` → 字典树已存在。跑一次冒烟查询确认全链路。

- `done-unknown` → 静态信息已经看不出更多，把 `checks` 全量转述给人自己判断。

## 配置文件创建规则

当 `stage` 为 `config` 时，按以下顺序执行：

1. **创建 `.dbmeta/` 目录**（如不存在）：`mkdir -p .dbmeta`
2. **复制模版到消费仓**：把 `shared/.dbllm.env.example` 复制到 `.dbmeta/.dbllm.env.example`
   （这份 example 是 git-tracked 的，供团队参考格式）
3. **创建配置文件**（如 `.dbmeta/.dbllm.env` 不存在）：从 example 复制一份
4. **告知用户需要填写的字段**：
   - `DB_HOST` — PostgreSQL 主机地址（必填）
   - `DB_PORT` — 端口（已有默认值 5432，通常不需要改）
   - `DB_NAME` — 数据库名（必填）
   - `DB_USER` — 只读角色名（默认 `llm_readonly`）；若集群上该名可能已被其它项目或旧脚本占用，
     改成带项目名的值如 `llm_readonly_<project>`
   - `DB_PASSWORD` — 三选一：新建角色 → 留 `CHANGE_ME` 不动，后续自动生成；复用同集群其它项目
     经本套件建的角色 → 从该项目 `.dbmeta/.dbllm.env` 拷 `DB_PASSWORD`（前提：不同库，
     或同库且 `SCHEMAS` 一致）；密码拿不到 → 把 `DB_USER` 改为专用名并留 `CHANGE_ME`
   - `SCHEMAS` — 采集范围（可选，留空 = 全库）
   - `SSH_HOST` 等 SSH 段（可选，仅数据库在跳板机之后时需要）——填好即可，连接层会在采集/查询前
     自动确保隧道就绪，不需要额外命令；字段说明见 `shared/.dbllm.env.example` SSH 段注释

   以上选择由用户在编辑器里完成，agent 不替用户选、不改文件。
5. **等用户确认**填写完成后，重跑预检验证 DB_HOST/DB_PORT/DB_NAME 已从 `placeholder` 变为
   `set`；此时 `stage` 应为 `provision`

**MUST NOT 在对话中收集 host/port/db 等连接信息**——这些信息由用户在编辑器中直接写入
配置文件，不经过大模型。

## 供给编排（REQ-IN-3）

把开发库的只读角色（缺省 `llm_readonly`）从零供给到「探针可用」收拢成一个可重复运行、直到成功为止的
入口。跑之前不需要知道 SQL——本 skill 不执行任何用户查询，那是 `/pg-query-ro` 的职责。

**工具链自身不再持有或使用任何管理员/DBA 凭据**（ADR-0006）：本 skill 只生成一份人可读的 SQL 脚本，
特权语句由 DBA 亲手执行；本 skill 进程发起的每一次数据库连接都只用 `.dbllm.env` 里的只读凭据。

### 五步编排

```bash
<skill-dir>/scripts/readonly-setup.sh
```

原样调用，**不重实现它的判定，不绕过任何一步**。脚本按序执行五步，在**首个未满足处停止**：

| 步骤 | 检查 | 未满足时 |
|---|---|---|
| ① 配置 | `.dbmeta/.dbllm.env` 是否存在、DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD 是否填写 | 配置文件本身缺失 → 与 `pg-dict` 对称的自举：从模版复制一份提示填写，退出码 1；文件在但必需字段缺失 → 退出码 2，写 `.dbmeta/db-readonly/needs-human.md` |
| ② 生成 | 调用 `shared/ro-generate.sh`（纯文本，零 DB 连接） | 产出 `.dbmeta/db-readonly/setup.sql`（0600）与 `userlist-fragment.txt`（0600）；`.dbllm.env` 的 DB_PASSWORD 仍是占位值 `CHANGE_ME` → 生成随机密码并写回 `.dbllm.env`（0600）与 setup.sql；已有可用密码则复用、不轮换。**本步成功不停下**，直接进 ③；生成器自身的 fail-loud（退出码 1，配置/标识符/git-ignore 守卫错误）原样传播 |
| ③ 连接就绪 | 先看 `.dbllm.env` 的 DB_HOST/DB_PORT/DB_NAME 是否仍是占位值，再以其凭据尝试连接 | 退出码 2，区分三种根因：(a) DB_HOST/DB_PORT/DB_NAME 仍是 `CHANGE_ME`（密码能生成，连接目标猜不出来）→「编辑 `.dbllm.env` 填入真实连接信息后重跑」；(b) 角色不存在（或 PgBouncer `auth_query` 模式下的 `no such user`）→「请以 DBA 身份执行 setup.sql，完成后重跑本 skill 并删除脚本」；(c) 认证被拒（经 PgBouncer 时无法细分「角色不存在」与「密码不同」）→ `needs-human.md` 按固定顺序给六项整改：核验 `pg_roles` 是否有该角色 → 无此行转 DBA 执行 `setup.sql` → 有此行且属其它项目共享角色则从其 `.dbllm.env` 拷密码复用（前提：不同库，或同库且 `SCHEMAS` 一致）→ 密码拿不到或 `SCHEMAS` 不同则手动改 `DB_USER` 为专用名（本脚本不代改）→ 确认本仓独占该角色才请 DBA 执行 `ALTER ROLE ... PASSWORD ...`（附「会改掉其它消费方密码」警告）→ 仅 PgBouncer `auth_file` 模式才贴 `userlist-fragment.txt` 并 RELOAD |
| ④ 校验 | 调用 `shared/ro-verify.sh`：六面有效访问审计 + 只读会话探针。六面为：① 角色标志（SUPERUSER/CREATEDB/BYPASSRLS/REPLICATION/CREATEROLE）与角色成员关系；② schema CREATE / 库级 CREATE-TEMP；③ 任何表的非 SELECT 权限、任何序列的 USAGE/UPDATE（跨全部 schema，不限 `SCHEMAS`）；④ 范围外 schema USAGE / 表 SELECT 残留；⑤ 对象所有权、dblink*/postgres_fdw 逃逸面；⑥ PUBLIC 伪角色复核 + **SECURITY DEFINER 函数对 PUBLIC 或只读角色开放 EXECUTE**（范围外 fail-closed，范围内仅报告）。另有三项附加检查（default ACL 漂移、非目标库 CONNECT、范围内表 owner 无 default-ACL 登记）。消费仓 MUST NOT 为这些面另写自己的守卫脚本 | 审计命中 → 退出码 2，`needs-human.md` 附现成整改 SQL（`REVOKE`/`ALTER`，交 DBA）；探针失败（角色对 PG 已就绪但 PgBouncer 未认可等）→ 退出码 1，见下方「PgBouncer 两种认证模式」子节 |
| ⑤ 完成 | 全部通过 | 退出码 0，打印「只读通道可用」 |

**从零到可用需要运行两到三次**：`.dbllm.env` 已有真实 DB_HOST/DB_PORT/DB_NAME、只差角色时是**两次**——第一次
在 ③ 停下提示交 DBA 执行 `setup.sql`，DBA 执行完成后第二次运行走完 ③④，打印「只读通道可用」。
全新项目是**三次**：第一次 ② 生成 `setup.sql` 并写入密码、③ 因 DB_HOST/DB_PORT/DB_NAME 仍是 `CHANGE_ME`
停下（分支 a）；人填完连接信息后第二次停在「角色不存在」（分支 b）；DBA 执行完 `setup.sql` 后第三次
才走完 ③④。

退出码翻译：

- **exit 0**「只读通道可用」→ 直接进入下一节「采集与冒烟」。
- **exit 2** 需要人类介入，stdout 只有一行 `.dbmeta/db-readonly/needs-human.md` 路径。
  agent 读这份文件的原文，翻译成「谁做什么、做完后怎么续跑」。三种子情形只转述文件里实际
  写的整改内容，不要脑补文件之外的额外步骤：
  - **角色不存在**（坐席 1 快乐路径终点）：呈现 `setup.sql` 路径与
    `needs-human.md` 路径，明确告诉人 DBA 只需要 `CREATEROLE`（**不需要 superuser**）就能
    执行这份脚本，然后说「DBA 执行完后重跑 `/pg-readonly-setup` 续接」，本坐席到此结束。
  - **认证被拒**：按 `needs-human.md` 内的顺序完整转述六项，不挑选、不改序（核验角色是否
    存在 → 无此行转 DBA 执行 `setup.sql` → 有此行且属其它项目共享角色则拷密码复用 → 密码
    拿不到或 SCHEMAS 不同则改 `DB_USER` 专用名，仅指路、不代改 → 确认独占后才请 DBA
    `ALTER ROLE` 同步密码，`ALTER ROLE` 的共享警告必须带上 → 仅 PgBouncer `auth_file` 模式
    才贴 `userlist-fragment.txt`），落在通用指引（既非「角色不存在」也非「认证被拒」的连接
    失败）时 agent MUST 读 `<details>` 脱敏详情：若详情显示连接已建立（认证阶段错误串）
    则向人说明 needs-human.md 的 cause（网络/PgBouncer 未起/连接信息有误）不准，并指向认证
    类整改，不复述「核对 DB_HOST」。
  - **审计命中**：转述 `needs-human.md` 里的 `REVOKE`/`ALTER` 整改 SQL，只交给 DBA。
- **exit 1** 硬错误：按 stderr 的 problem/cause/fix 转述。若指向探针失败（④ 校验阶段，PgBouncer
  认证模式相关）——转述判别命令（`grep -E '^(auth_type|auth_file|auth_query)' <pgbouncer.ini>`）
  与分支，见下方「PgBouncer 两种认证模式」子节。

### generate 生成什么

`ro-generate.sh` 是纯文本函数——给定 `DB_USER`/`SCHEMAS`/当前密码状态，产出确定性的 `setup.sql`：
一个幂等的 `DO $$` 块，内含执行前危险角色审计（既存角色若已带 `SUPERUSER`/`CREATEDB`/`BYPASSRLS`/
`REPLICATION`/成员关系/任一表的非 SELECT 权限，直接 `RAISE EXCEPTION` 中止、零变更）、
`CREATE ROLE`（幂等，已存在不重设密码）、属性收敛（`CONNECTION LIMIT 5`、角色级超时、
`NOCREATEROLE NOINHERIT`）、以及**在执行时刻**按 `SCHEMAS` 展开的 `GRANT`/`REVOKE`/
`ALTER DEFAULT PRIVILEGES`（scope 判定发生在 DBA 执行脚本那一刻，不是生成那一刻，脚本改动后库结构
变化也能收敛）。生成阶段本身不连接数据库，脚本本身在真正被 DBA 执行前不改变任何东西。

### PgBouncer 两种认证模式

如果开发库经 PgBouncer，④ 的探针（经 `shared/ro-session.sh` 走完整只读会话链路）失败一次（角色已在
PG 建好，但 PgBouncer 尚未认可）时，**先判别** PgBouncer 用的是哪种认证模式：

```bash
grep -E '^(auth_type|auth_file|auth_query)' <pgbouncer.ini>
```

- **仅命中 `auth_query=`（不含 `auth_file=`）**（纯 `auth_query` 模式）：`userlist-fragment.txt`
  不适用——按上方「五步编排」③ (c)「认证被拒」的六项整改顺序排查（角色可能确实没建、或密码/权限没同步到
  `auth_query` 查询的角色表）。
- **仅命中 `auth_file=`**（`auth_file` 模式）：终端会指向 ② 已生成的
  `.dbmeta/db-readonly/userlist-fragment.txt`（0600，一行 `"角色名" "密码"`），提示粘贴进
  PgBouncer 的 `userlist.txt` 并 `reload`。粘贴完成、reload 后**删除该片段文件**，再重跑本
  skill 完成 ④ 的探针。

两者都命中（**混合配置**：`auth_file` 对已列入 `userlist.txt` 的用户优先、`auth_query` 只兜底未列入者）
⇒ 角色若在 `userlist.txt` 有旧条目，仍需更新 fragment 并 `reload`；先向 DBA 确认该角色实际走静态
文件还是动态查询，**MUST NOT 仅凭 `auth_query=` 在场就判 fragment 不适用**。
两者都判不出（`auth_type=trust/hba`、键在 include 文件里、或未经 PgBouncer 直连）⇒ 先向 DBA
确认生效的认证后端，不默认贴 fragment。

### 既存角色与共享前提

`DB_USER`（缺省 `llm_readonly`）可以被同一集群的多个消费仓共享，但共享有前提：

- **跨库共享合法**：不同数据库各自独立的权限空间，同名角色在不同库里各自 `GRANT`/`REVOKE`，互不干扰。
- **同库共享须 `SCHEMAS` 一致**：同一个库被多个消费仓共用同一角色时，各仓的 `SCHEMAS` 声明必须
  一致——否则后跑的仓会把先跑的仓的授权范围之外的 schema 权限当作「越界」而 `REVOKE` 掉，
  并让 `ro-verify.sh` 的六面审计④误报。
- **同库共享还须 owner 一致**：`setup.sql` 里的 `ALTER DEFAULT PRIVILEGES FOR ROLE current_user`
  只覆盖「执行 setup.sql 的那个账号」日后新建的表。若同一个库里还有别的 owner 会在目标 schema
  建表，初始审计能过（`GRANT SELECT ON ALL TABLES` 覆盖存量表），但那个 owner 之后新建的表对只读
  角色仍会拒绝访问。共享前提因此要加一条：**所有会在目标 schema 建对象的 owner 都已配置对应默认
  权限**——做不到就请 DBA 以每个 owner 的身份各跑一次
  `ALTER DEFAULT PRIVILEGES [FOR ROLE <owner>] IN SCHEMA <s> GRANT SELECT ON TABLES TO <DB_USER>`。
  本 skill 不检测 owner 分布（与 `SCHEMAS` 冲突同为文档前提，Non-Goal）。
- **密码真相源是先接入项目**：库内取不回已设置的密码（PostgreSQL 不存明文），复用密码只能从
  先接入该角色的项目的 `.dbmeta/.dbllm.env` 里拷贝，本项目内部没有别的取值渠道。
- **`ALTER ROLE` 只在独占时执行**：确认本仓独占该角色（没有其它消费仓在用）才能请 DBA 执行
  `ALTER ROLE ... PASSWORD ...`；否则会让所有共享该角色的项目一起密码漂移。
- **改名由人手动完成**：本 skill 与 agent 都不会自动把 `DB_USER` 改成专用名——密码拿不到或
  `SCHEMAS` 冲突时，只指路让开发者在编辑器里手动改名后重跑。
- **限额由共享方分摊**：`CONNECTION LIMIT 5` 与角色级 `statement_timeout`/
  `idle_in_transaction_session_timeout` 是角色级设置，多个消费仓共享同一角色时，这些限额被
  所有使用方共同分摊，不是每仓各自 5 个连接。

### DBA 只需 CREATEROLE，不需要 superuser

`setup.sql` 只对目标只读角色执行 `NOCREATEROLE NOINHERIT CONNECTION LIMIT 5` 与角色级超时设置、
`CREATE ROLE`/`GRANT`/`ALTER DEFAULT PRIVILEGES`——这些都是持 `CREATEROLE`（非 superuser）的账号可以
完成的操作。若目标角色在库内已经带有 `SUPERUSER`/`CREATEDB`/`BYPASSRLS`/`REPLICATION`/`CREATEROLE`
中的任何一个（多半是被人手工建过），脚本内嵌的执行前审计会在任何库变更前中止（`RAISE EXCEPTION`），
交给持有相应权限的账号先手动 `ALTER ROLE ... NO...` 清掉这些危险 flag 后再重新执行。

### 失败排查

任一步失败都会给 problem/cause/fix；退出码 2 的情形（① 缺键、②/③ 需要 DBA 介入、④ 审计命中）
统一去读 `.dbmeta/db-readonly/needs-human.md`（该路径也是当次运行 stdout 的唯一内容）。退出码 1 是
④ 探针失败（如 PgBouncer 未同步）或配置/连接层面的硬错误（`psql` 未安装等），直接看 stderr/stdout
提示。

## 采集与冒烟（REQ-IN-4）

供给报告 exit 0 之后，依次跑：

```bash
<pg-dict skill 入口>/scripts/pg-dict.sh
```

失败 → 原样转述 problem/cause/fix，**不进入冒烟**。

成功 → 跑一条冒烟查询：

```bash
<pg-query-ro skill 入口>/scripts/pg-query-ro.sh --sql 'SELECT 1'
```

- exit 0 → 进入终态总结。
- exit 1/2/3 → 按退出码语义转述。

## 终态总结（三要素）

1. **覆盖 schema 清单**
2. **`.dbmeta/` 产物根路径**
3. **三个后续入口指引**：`/pg-dict` 重采、`/pg-query-ro` 查询、配置改动 / `SCHEMAS` 改动后
   重跑本 skill

## 重入规则

任意时刻都可以重新触发本 skill——第一步永远是重跑 `preflight.sh`。

- 从 `stage` 给出的起点开始接话，但如果实际调用底层脚本得到的结果和 `stage` 的静态推导不
  一致，**以实际运行结果为准**。
- 重跑 **MUST NOT** 触发密码轮换——`ro-generate.sh` 本身的幂等语义已经保证「已有可用密码
  则复用，不重新生成」。

`preflight.sh` 的静态推导看不到 `SCHEMAS` 与密码值的变化——只读通道已就绪且字典树已存在时
静态起点恒为冒烟阶段。因此向导 SHALL 额外遵守以下三条规则：

- ① 用户明示 `SCHEMAS` 改动、密码漂移或要求重跑供给时，向导 MUST 无视静态起点、从供给阶段
  起跑供给编排，且 `SCHEMAS` 改动时无论供给编排退出码（既有健康角色下探针与审计会通过、退出
  0）向导 MUST 停在「交 DBA 执行新 `setup.sql`」、MUST NOT 进入采集，DBA 执行后再次触发才继续。
- ② 冒烟或采集以连接或认证类错误失败时（`needs-human.md`「连接被拒」或采集「无法连接开发库」，
  不要求区分认证被拒与不可达），向导 MUST 回到供给阶段而非只转述错误。
- ③ 供给编排报「只读通道可用」之后（① 的 `SCHEMAS` 待 DBA 情形除外），向导 MUST 接着执行
  字典采集（即使字典树已存在）再冒烟——`SCHEMAS` 同时决定授权范围与采集范围，重跑供给 SHALL
  顺带重采字典，MUST NOT 让二者脱节。

`SCHEMAS` 改动后必须重跑本 skill——供给编排会重新生成一份 `setup.sql`（内嵌新范围），但
**只有 DBA 再次执行这份新脚本，收窄/新增的范围才会真正在库内生效**：生成本身零连接，不会替
你把旧范围的权限 `REVOKE` 掉。既有健康角色下脚本会退出 0，由向导（①）停在交 DBA。

## 安全边界

- **敏感信息不进对话**：数据库连接信息由用户在编辑器中直接写入 `.dbllm.env`，不通过
  访谈收集。agent 可以看到 preflight 报告的字段三态（absent/placeholder/set），但**永远
  看不到也不需要看到**密码的实际值（host/port/dbname 会出现在采集与诊断日志中，属非密钥配置信息）。
- **密码永远不进对话**：密码只出现在 0600 权限的 `.dbllm.env`/`setup.sql`/
  `userlist-fragment.txt` 三个文件里，且只由受控脚本经 `PGPASSWORD` 环境变量使用。
- **generate 之后 agent MUST NOT 读三个明文文件**：`.dbmeta/.dbllm.env`、
  `.dbmeta/db-readonly/setup.sql`、`.dbmeta/db-readonly/userlist-fragment.txt`——不管是直接
  `Read` 工具还是 `cat` 等手段，一律不做，三者都内嵌明文密码。预检的三态报告已经替代了
  "读内容确认状态"这件事；需要了解 `setup.sql` 逻辑时读本仓 `shared/ro-generate.sh` 模板，
  不读产物。
- **agent MUST NOT 以任何手段（`Edit`/`Write`/`sed`/`echo` 重定向等）改写 `.dbmeta/.dbllm.env`
  的任何字段（含 `DB_USER`）**——复用密码、改角色名都只能指路让开发者在编辑器里改；该文件
  的唯一机器写入面是 `ro-generate.sh` 对密码字段的就地替换。
- **特权语句零执行**：本向导全链路**不执行**任何 `CREATE ROLE`/`GRANT`/`REVOKE`/`ALTER ROLE`
  ——执行永远是 DBA 拿着 `setup.sql` 手动做的事。
- **诚实边界**：「2 坐席」是快乐路径的估算描述，不是数值承诺。
