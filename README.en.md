# db-llm

A standalone, globally-installed skill repo for **letting a large language model access a
PostgreSQL database** — `pg-readonly-setup`, `pg-dict`, `pg-query-ro`, `pg-sql-check`: collect
`pg_catalog` metadata from any PostgreSQL database and render it into a human/LLM-readable data
dictionary (`.dbmeta/`), plus a provisioned read-only role, an ad-hoc read-only query channel, and
a zero-write `PREPARE`-based SQL validation channel.

> **The positioning is intentionally not nailed to "read-only"**: today's role provisioning
> implementation is read-only (`llm_readonly`), but the problem this repo solves is "how does an
> LLM access a database safely" — the permission tier is an implementation detail. See
> `openspec/issues/open/todo/T77.md` for the planned dev-writable / prod-read-only tier split.

The ops line — provisioning and operating the PostgreSQL service itself — lives in the sibling
repo `laodao-ai/pg-ops-skills`. The two repos are connected only by a **documentation-level
consuming relationship** (see ADR-0008) — no code dependency: this repo uses `pg-ops`'s
`/pg-dev-init` to provision its own dev/test database, and nothing else. See `README.md`
(Chinese, the primary/canonical doc) for the full skill table, directory layout, and per-key
configuration reference.

This repo is the **engine**: consuming projects wire it in through a single seam at their own
project root — `.dbmeta/.dbllm.env` (read-only-role credentials, readable by the model). It is
also **its own consuming project** ("testing is consumption"): it has its own dev/test database
(provisioned via `pg-ops`), its own `.pg-ops/` (pg-ops consuming credentials), and a `.dbmeta/` at
its root, and it uses its own skills to develop and test itself. That does not weaken the boundary
rule above — the rule constrains **what goes into a skill** (the engine must never name a specific
consuming project's binaries, schemas, or layout), not whether this repo happens to have a
database of its own.

## Security model

- **`.dbmeta/.dbllm.env`** (read-only-role credentials only). DBA/admin
  credentials never enter this toolchain — provisioning the read-only role is a separate
  generate/verify flow (see ADR-0006). This file is git-ignored, and is **parsed line-by-line
  as `KEY=VALUE`, never `source`d**, to avoid arbitrary code execution. Credentials reach `psql`
  only via the `PGPASSWORD` environment variable, never argv or string-interpolated SQL.
- **`.pg-ops/` (maintainer note, ADR-0002, not a runtime constraint of this repo)** — when this
  repo's own maintainers provision its dev/test database via `pg-ops`'s `/pg-dev-init`, the owner
  credentials land in this repo's own git-ignored `.pg-ops/` directory. The model MUST NOT read
  that directory's contents; retrieval goes through `pg-ops`'s helper scripts instead.

**Privileged-SQL boundary (hard rule, no exceptions):** this repo only ever *generates* privileged
SQL (`CREATE ROLE` / `GRANT` / `REVOKE` / `ALTER ROLE`) for a human DBA to execute by hand in the
consuming project's own database — no skill, script, test, or the model itself ever executes such
a statement here. See `pg-readonly-setup/SKILL.md`'s "安全边界" section and `shared/ro-generate.sh`'s
header comment for how the generate/verify split enforces it.

Install/provisioning scripts never touch data they don't own, and re-running is always a safe
no-op.

## Install

```bash
git clone https://github.com/laodao-ai/db-llm-skills.git ~/.skills/db-llm-skills   # a real clone, not a symlink into a dev checkout
bash ~/.skills/db-llm-skills/setup.sh                                             # idempotent; symlinks on Unix, copies on Windows
```

This installs all 5 skills into both `~/.claude/skills/` and `~/.codex/skills/`. Afterwards,
upgrade with `/db-llm-upgrade` (pull → setup → show version). Development happens in a separate
dev checkout; the run checkout is pull-only, never edited in place.
