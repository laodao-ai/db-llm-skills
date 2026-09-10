---
name: db-llm-upgrade
description: 升级 db-llm 套件运行 checkout（~/.skills/db-llm-skills）：git pull → bash setup.sh → 显示版本与最新变更。当用户说"升级 db-llm"、"更新 db-llm skills"、"db-llm upgrade"、"db-llm 有新版本吗"、"刷新 db-llm"、"升级数据字典"，或使用 /db-llm-upgrade 时触发。
---

# db-llm-upgrade — 运行 checkout 一键升级

对**运行 checkout** `~/.skills/db-llm-skills/` 执行升级三连。pull 与 setup 必须连跑——skill 软链即时生效，
只 pull 不 setup 不会刷新新增的 skill 目录。

## 步骤

```bash
<skill-dir>/scripts/upgrade.sh
```

脚本封装了以下升级三连，退出码 0 成功 / 1 pull 层失败（含 checkout 不存在、remote 不匹配、
pull 失败、detached HEAD）/ 2 setup.sh 失败，每个失败分支输出 problem/cause/fix 三件套：

1. **校验**：确认 `~/.skills/db-llm-skills/` 存在，且 remote 指向 `laodao-ai/db-llm`。
2. **pull**：`git -C ~/.skills/db-llm-skills pull --ff-only`
   - 非 ff（本地被改过）→ 停下报告，不强推；提示"运行 checkout 只读，改动应发生在开发 checkout"。
   - detached HEAD → 提示先 `git -C ~/.skills/db-llm-skills checkout main`。
3. **setup**：`bash ~/.skills/db-llm-skills/setup.sh`
   - 刷 skills 软链到 `~/.claude/skills/` 和 `~/.codex/skills/`。
4. **展示**：`git -C ~/.skills/db-llm-skills describe --tags --always --dirty` + `git -C ~/.skills/db-llm-skills log --oneline -5`，向用户汇报版本与最新变更。
5. **提示**：Unix 软链模式下 git pull 自动反映最新；Windows 拷贝模式需重跑 setup.sh。

## 回滚

推坏一版时：`git -C ~/.skills/db-llm-skills checkout <上一已知良好 commit> && bash ~/.skills/db-llm-skills/setup.sh`。

## 注意

- 只动运行 checkout；开发 checkout（编辑代码的 clone）不归本 skill 管。
- 运行 checkout remote 必须是 `laodao-ai/db-llm-skills.git`。
- 姊妹仓 `pg-ops` 的运维线三 skill 由 `/pg-ops-upgrade` 单独升级，两者互不越界
  （各自只动自己的 checkout）。
