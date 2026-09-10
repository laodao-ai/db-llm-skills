# db-llm

独立、全局安装的 **大模型访问 PostgreSQL 数据库 skill 仓**：从任意 PG 库的 `pg_catalog` 采集元数据，
渲染成消费仓可读的数据字典（`.dbmeta/`），并提供只读角色开通、只读即席查询与单条 SQL 预校验通道。
从一个消费项目里抽出，成为可服务任意 PostgreSQL 项目的独立引擎（见
ADR-0003）。

> **定位不焊死「只读」**：当前实现的角色供给是只读（`llm_readonly`），但本仓要解决的问题是
> 「大模型怎么安全地访问数据库」——权限档位是实现细节，见
> `openspec/issues/open/todo/T77.md`（dev 库允许写、生产库
> 仍只读的档位规划）。

> **姊妹仓**：运维线（把 PostgreSQL 服务本身立起来并运维）在
> [`laodao-ai/pg-ops-skills`](https://github.com/laodao-ai/pg-ops-skills)。两仓经**文档级消费关系**
> 相连（ADR-0008），无代码依赖：本仓
> 用 pg-ops 的 `/pg-dev-init` 建自己的开发/测试库，除此之外互不引用。

`setup.sh` 把各 skill symlink 到 `~/.claude/skills/` 与 `~/.codex/skills/`，任何 PG 项目全局可用。

**边界判据（唯一一条）**：不知道消费项目存在的东西才在这里。凡是引用某个项目的二进制、
schema 名、CLI 子命令、制品布局的，留在那个项目的 `hack/`，通过薄包装调用本仓。这条判据同样把
「把 PG 服务本身立起来并运维」的职能挡在外面——那是 `pg-ops` 的边界，不是本仓的。

本仓是**引擎**：消费项目经 `.dbmeta/.dbllm.env`（只读口令，模型可读、按行解析不 `source`，
ADR-0006）接入。同时它也是**自己的消费仓**（「测试也是消费」）——有自己的开发/测试库、自己的
`.pg-ops/`（pg-ops 消费凭据）与仓根 `.dbmeta/`，用自己的 skill 做自己的开发与测试。这与上面那条
边界判据不矛盾：判据约束的是 **skill 的内容**（引擎一行都不能提某个消费项目的名字与布局），不是
仓里有没有库和 `.dbmeta/`——引擎自己吃狗粮是对的。

## Skills

| skill | 做什么 |
|---|---|
| `pg-readonly-setup` | 消费仓只读通道的单一、可重入入口：环境预检 → `.dbmeta/.dbllm.env` 自动创建 → 只读角色（`DB_USER`，缺省 `llm_readonly`）生成/verify 供给 → 首次 `/pg-dict` 采集 → `/pg-query-ro` 冒烟，由预检定位从哪一阶段接话 |
| `pg-dict` | 从开发库 `pg_catalog` 生成/再生数据字典到仓根 `.dbmeta/`（按 schema / 按对象分文件，人与 LLM 双读），并处理孤立注记的人工判断 |
| `pg-query-ro` | 面向 agent 的即席只读查询唯一入口：以只读角色跑单条 SELECT/WITH/EXPLAIN/SHOW，结果落 `build/pg-query-ro/`，不进终端刷屏 |
| `pg-sql-check` | 对单条即席 SQL 语句（含写语句）用只读角色跑 `PREPARE` 核验，验列名 / 类型 / 参数占位是否对得上真实 schema——只 `PREPARE`，不 `EXPLAIN`、不执行、零写入 |
| `db-llm-upgrade` | 升级本机运行 checkout `~/.skills/db-llm-skills`：pull → setup → 显示版本 |

规划中的 skill、实施顺序、卡点与规格来源，见 **[`docs/skills-roadmap.md`](docs/skills-roadmap.md)**——那是 skill 规划的唯一真相源，新增 / 改名 / 去重先在那里落位。

## 安装

```bash
git clone https://github.com/laodao-ai/db-llm-skills.git ~/.skills/db-llm-skills   # 运行 checkout（真 clone，不是软链）
bash ~/.skills/db-llm-skills/setup.sh            # 幂等；Unix symlink，Windows 拷贝
```

之后升级用 `/db-llm-upgrade`（pull → setup → 显示版本）。开发改动在另一份开发 checkout 里做、push 后
在运行机跑升级；运行 checkout 只读，勿在其中改代码。

## 使用

五个 skill 的总览（我该用哪个 · 按什么顺序 · 卡住了在哪一格，含流程图）见
**[`docs/skills-guide.md`](docs/skills-guide.md)**。

接入向导见上表 `pg-readonly-setup`——在消费仓根说「接入向导」/「配置只读通道」即可触发，
或直接 `/pg-readonly-setup`。

## Credential architecture（凭据架构）

全部配置与凭据集中在消费仓一个 git-ignored 文件里：`.dbmeta/.dbllm.env`。仓内提供 git-tracked 的
`.dbmeta/.dbllm.env.example` 模板供团队参考。DBA / 管理员凭据永不进入本工具链——见
ADR-0006。

**推荐入口**：跑 `/pg-readonly-setup`——只读通道唯一、可重入的入口。从零项目会走完整 onboarding
路径（建 `.dbmeta/`、拷贝配置模板，接着串联供给 → 首次 `/pg-dict` 采集 → `/pg-query-ro` 冒烟）；已接入
项目（如 `SCHEMAS` 改了，或密码漂移了）则直接从供给阶段重入。大模型不经手凭据——由用户自己在编辑器里
填写 `.dbllm.env`。

只读角色的供给本身拆成两步：**generate**（纯文本生成器产出幂等的 `setup.sql`，由人类 DBA 手动执行）与
**verify**（工具链用只读凭据连接，跑一次六面有效权限审计——角色标志、schema/temp CREATE、非 SELECT 的
表与序列权限、越权残留、ownership/dblink 逃逸面、PUBLIC + **SECURITY DEFINER 函数 EXECUTE** 暴露面
——并探测完整会话路径）。消费项目应依赖此审计而非自建同类检查脚本；完整审计面清单见
`pg-readonly-setup/SKILL.md` 第④步与 `shared/ro-verify.sh` 头注释。

**同一物理数据库被多个消费仓共用时，每个仓 MUST 用不同的 `DB_USER`**——`/pg-readonly-setup` 的作用域
收敛会 `REVOKE` 掉不再属于本仓 `SCHEMAS` 的任何 schema，两个仓共用一个角色会互相清掉对方的权限。

**开发库怎么来**：本仓自己的开发/测试库由姊妹仓 `pg-ops` 的 `/pg-dev-init` 建立——那是开发库 owner
凭据的供给方，本仓从不建库、不建 owner，产物写进仓根 `.pg-ops/`（模型 MUST NOT 读其内容——这是给
维护者的约束，规则原文在 pg-ops 仓侧）。

## `.dbmeta/` 与 Configuration

每个消费项目需要一个 git-ignored 的 `.dbmeta/.dbllm.env`——唯一同时装配置与数据库凭据的文件。输出
目录与配置文件被收拢进同一个固定的 `.dbmeta/` 目录（仓根，不可配置）。`pg-dict`/`db-collect` 拒绝在没有
它的情况下运行，也不会回退猜测路径。按行解析为 `KEY=VALUE`（从不 `source`——原因见 `shared/config.sh`
头注释）。未知 key 会被忽略。

首次运行（`.dbmeta/.dbllm.env` 缺失）时，`pg-dict.sh` 会自动建 `.dbmeta/` 并拷贝
`shared/.dbllm.env.example` 到那里，然后退出请你填连接信息并跑 `/pg-readonly-setup`。

消费项目的 `.gitignore` MUST NOT 整份忽略 `.dbmeta/`——`.dbmeta/.dbllm.env.example` 与
`.dbmeta/_relations.confirmed.yaml` 需要 git 跟踪。MUST 忽略 `.dbmeta/.dbllm.env`（凭据）与
`.dbmeta/db-readonly/setup.sql` / `.dbmeta/db-readonly/userlist-fragment.txt`（一行 `.dbmeta/db-readonly/` 覆盖两者）。
`shared/ro-generate.sh` 在这些文件被 git-ignore 前拒绝写含口令的文件。

```
SCHEMAS=
DB_HOST=CHANGE_ME
DB_PORT=5432
DB_NAME=CHANGE_ME
DB_USER=llm_readonly
DB_PASSWORD=CHANGE_ME
RO_DEFAULT_LIMIT=200
```

| Key | 必填 | 含义 |
|---|---|---|
| `SCHEMAS` | 否 | 逗号分隔的 schema 名，采集/渲染范围，也是 `/pg-readonly-setup` 授权只读角色的同一范围。空/缺省 = 全库。可被 CLI `--schema` 覆盖（仅 db-collect）。 |
| `DB_HOST` | **是** | PostgreSQL host。 |
| `DB_PORT` | **是** | PostgreSQL port，模板默认 `5432`。 |
| `DB_NAME` | **是** | 数据库名。 |
| `DB_USER` | 否 | 待供给/使用的只读角色名，默认 `llm_readonly`。同库多消费仓场景每仓 MUST 用不同值——见上「Credential architecture」。同时也是 `CREATE ROLE` 生成时的角色名。 |
| `DB_PASSWORD` | **是** | 只读角色密码。首次配置留 `CHANGE_ME`——`/pg-readonly-setup` 会生成随机 `[A-Za-z0-9]` 密码并写回。 |
| `RO_DEFAULT_LIMIT` | 否 | `pg-query-ro` 在查询未带 `--limit`/`--no-limit` 时的默认行数上限，默认 200。 |
| `SSH_HOST` | 否 | 跳板机地址。非空且 `!= CHANGE_ME` 时启用 SSH 隧道模式——连接层会在每次连接前自动确保隧道（未监听则起、已监听则复用）。启用后 `DB_HOST`/`DB_PORT` 不必填——两个值由隧道确定（引擎取 `localhost` 与 `SSH_LOCAL_PORT`），配置里留着旧值也只是被忽略。完整 `SSH_*` 字段集（端口/用户/本地端口/远端 host/远端端口/密钥文件/密码）见 `shared/.dbllm.env.example`。 |

凭据只经 `PGPASSWORD` 环境变量传给 `psql`/libpq（从不进 argv、从不字符串拼进 SQL）。连接受
`PGCONNECT_TIMEOUT=10` 限制；查询受连接后立即 `SET statement_timeout` / `SET lock_timeout` 限制。

### 非 git 消费项目（手动路径，`--allow-unignored`）

当项目不是 git 仓库时 `/pg-readonly-setup` 会停下：生成器拒绝写它无法证明已被 git-ignore 的含口令文件，
编排器也不转发 bypass 参数。只有 generate 这一步被拦；collect、verify 与只读会话不需要任何 git 前提。
手动路径：

1. 手工建配置并填 `DB_HOST`/`DB_PORT`/`DB_NAME`（想收窄范围可加 `SCHEMAS`），`DB_PASSWORD` 留 `CHANGE_ME`。

   ```bash
   mkdir -p .dbmeta
   cp <db-llm checkout>/shared/.dbllm.env.example .dbmeta/.dbllm.env
   chmod 600 .dbmeta/.dbllm.env
   ```

2. 从项目根直接跑生成器并带 bypass 参数。它会把生成的密码写回 `.dbmeta/.dbllm.env`，产出
   `.dbmeta/db-readonly/setup.sql` + `userlist-fragment.txt`（权限 0600）。

   ```bash
   bash <db-llm checkout>/shared/ro-generate.sh --allow-unignored
   ```

3. 让 DBA 执行 `.dbmeta/db-readonly/setup.sql`，然后删掉它。

4. 校验只读通道（六面审计 + 全链路探测；退出 0 表示「只读通道可用」）：

   ```bash
   bash <db-llm checkout>/shared/ro-verify.sh
   ```

5. 之后 `/pg-dict` 与 `/pg-query-ro` 照常可用。

带上这个参数，你就自己接管了原本由 guard 强制的凭据卫生：`.dbmeta/.dbllm.env` 与 `.dbmeta/db-readonly/` 要自己确保
不进任何 VCS / 同步 / 备份覆盖的范围。`--allow-unignored` 只为这种情形存在——git 仓库内应直接修
`.gitignore`。

## 目录

```
setup.sh              全局安装（symlink 到两宿主）
pg-readonly-setup/     只读通道从零 onboarding + 供给编排入口（SKILL.md + scripts/preflight.sh 等）
pg-dict/               数据字典引擎（SKILL.md + scripts/pg-dict.sh 编排 + scripts/render.py 渲染 + render_test.py）
pg-query-ro/           只读即席查询通道（SKILL.md + scripts/）
pg-sql-check/          单条 SQL 语句 PREPARE 校验（SKILL.md + scripts/pg-sql-check.sh + scripts/candidate_match.py 候选名补全）
db-llm-upgrade/        运行 checkout 升级三连（pull → setup → 显示版本）
shared/                跨 skill 共用的 shell / Python 库：
                        config.sh / db-collect.sh / db-collect.sql / ro-generate.sh / ro-session.sh / ro-verify.sh / ro_guard.py / ssh-tunnel.sh
tests/                 pytest：render 合并/幂等 + shell 层集成 + pg_catalog 契约测试（唯一入口 tests/run-contract-test.sh）
docs/                  人读文档
  skills-roadmap.md     规划唯一真相源：目标态 / 规划 skill / 顺序 / 规格来源
  skills-guide.md       五个 skill 总览：执行流程 / 安全边界（md + 同内容 html）
LICENSE                Apache-2.0
```

## 消费方接缝（以一个 Go 服务为例）

- 只读线经 `.dbmeta/.dbllm.env` 声明式接入，见上「Credential architecture」「Configuration」两节。
- 开发库/运维职能经姊妹仓 `pg-ops` 的 `hack/` 薄包装接入，与本仓无关（ADR-0008）。

## 许可

Apache-2.0，见 [LICENSE](./LICENSE)。
