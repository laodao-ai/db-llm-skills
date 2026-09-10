#!/bin/bash
# Collect pg_catalog metadata (schemas/tables/columns/indexes/constraints/triggers/
# views/functions) from the dev database into a single collect_version=1 JSON
# document (see shared/db-collect.sql for the full contract). This is the sole
# pg_catalog read point consumed by the /pg-dict skill, the db_collect_contract
# guard, and any future db-lint tooling — collect once, render/lint separately.
#
# Usage:
#   shared/db-collect.sh                          # write JSON to stdout
#   shared/db-collect.sh --out /tmp/collect.json   # write JSON to a file (atomic)
#   shared/db-collect.sh --schema auth --schema logs   # limit to given schemas
#   shared/db-collect.sh -h | --help
#
# Credentials are declarative, not hardcoded: the consuming project's
# .dbmeta/.dbllm.env declares DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD
# directly — see shared/config.sh. This script has zero project knowledge
# beyond that declaration.
#
# This is a development-time, read-only tool (psql -X, pure SELECT SQL) — it is
# not invoked from any server startup/deploy path and never writes to the DB.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")}}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $1" >&2; }
info() { echo -e "${YELLOW}[INFO]${NC} $1" >&2; }
fail() { echo -e "${RED}[FAIL]${NC} $1" >&2; }

usage() {
    cat <<'USAGE'
Usage: shared/db-collect.sh [--out FILE] [--schema NAME]... [-h|--help]

  --out FILE       Write the collected JSON to FILE (atomic write). Default: stdout.
  --schema NAME     Limit collection to this schema; repeat for multiple. Overrides
                     the .dbllm.env SCHEMAS declaration when given. Default (no
                     --schema and no SCHEMAS declared): all non-system schemas that
                     own at least one supported object.
  -h, --help        Show this help and exit.
USAGE
}

OUT_FILE=""
CLI_SCHEMAS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)
            [[ $# -ge 2 ]] || { fail "problem: --out 缺少参数"; usage >&2; exit 1; }
            OUT_FILE="$2"
            shift 2
            ;;
        --schema)
            [[ $# -ge 2 ]] || { fail "problem: --schema 缺少参数"; usage >&2; exit 1; }
            CLI_SCHEMAS+=("$2")
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "problem: 未知参数 $1"
            usage >&2
            exit 1
            ;;
    esac
done

CONFIG_LIB="${SCRIPT_DIR}/config.sh"
if [[ ! -f "${CONFIG_LIB}" ]]; then
    fail "problem: 找不到 shared/config.sh（${CONFIG_LIB}）"
    fail "cause: 本地 db-llm 安装不完整（config.sh 与 db-collect.sh 应同在 shared/ 下）"
    fail "fix: 重新跑一次 db-llm 仓的 setup.sh，或核对该仓是否完整 clone"
    exit 1
fi
# shellcheck source=/dev/null
source "${CONFIG_LIB}"

db_llm_load_config "${ROOT_DIR}/.dbmeta/.dbllm.env" || exit 1

# Priority: CLI --schema > .dbllm.env SCHEMAS > full DB (empty CSV).
# Comma-join for psql -v schemas_csv=... (SQL side does string_to_array + ANY;
# MUST NOT paste values into SQL text here — see shared/db-collect.sql header and
# hack/llm-readonly.sh's ":'var' only, never string-build SQL" precedent).
SCHEMAS_CSV=""
if [[ ${#CLI_SCHEMAS[@]} -gt 0 ]]; then
    SCHEMAS_CSV="$(IFS=,; echo "${CLI_SCHEMAS[*]}")"
elif [[ -n "${DBS_SCHEMAS}" ]]; then
    SCHEMAS_CSV="${DBS_SCHEMAS}"
fi

# Resolve credentials and export PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD +
# PGCONNECT_TIMEOUT=10 (unsetting DATABASE_URL) — reads DBS_DB_* globals set
# by db_llm_load_config above.
db_llm_export_pg_env || exit $?

# psql has no default connect/statement timeout — a stuck TCP handshake or a query
# fighting a lock would otherwise hang db-collect.sh indefinitely (PGCONNECT_TIMEOUT
# is exported by db_llm_export_pg_env above).
# statement_timeout/lock_timeout are set via SQL (SET ..., issued as -c args right
# before -f below), NOT via PGOPTIONS='-c statement_timeout=...'. Deliberate: a
# PGOPTIONS `options` startup parameter goes through PgBouncer's fixed allowlist of
# forwarded GUCs (client_encoding/datestyle/timezone/application_name/search_path —
# confirmed by reading the installed pgbouncer 1.25.2 binary's known-parameter table),
# which does NOT include statement_timeout/lock_timeout — PgBouncer rejects the whole
# connection with "unsupported startup parameter in options: statement_timeout"
# (verified against this project's actual PgBouncer, on the 6432 instance). The
# rejection happens at PgBouncer's startup-parameter allowlist, i.e. before and
# independent of pool_mode, so it applies to both instances alike. A plain `SET`
# issued after connect is a normal query, not a startup parameter, so it always
# works — through PgBouncer (this deployment defaults to the session-mode
# instance, 7432) and against a bare postgres alike.
#
# `--single-transaction` is what makes those SETs actually reach the payload's
# backend. Without it each -c and the -f are separate implicit transactions, and
# PgBouncer's *transaction* pooling assigns a server connection per transaction —
# the two SETs can land on backend A while the collect payload runs on backend B,
# which then has no timeout at all, with no error and no warning to say so (B9).
# One explicit transaction pins all three to the same backend under either pool
# mode, so the guard no longer depends on DB_PORT pointing at a session-mode
# instance. Same shape as shared/ro-session.sh, which wraps SET LOCAL + payload
# in a single BEGIN.

if ! command -v psql >/dev/null 2>&1; then
    fail "problem: psql 未安装或不在 PATH 中"
    fail "cause: 本机缺少 PostgreSQL 客户端"
    fail "fix: 安装 psql（如 brew install libpq）后重试"
    exit 1
fi

# psql stderr is untreated text from the server/driver and may echo back connection
# parameters verbatim (e.g. a "password authentication failed for user X" style
# message, or a broken pipe error including PGPASSWORD via some driver versions) —
# treat it as untrusted and redact known-secret values before it ever hits the
# terminal, via db_llm_redact/db_llm_redact_one (shared/config.sh).

info "=== db-collect ==="
info "数据库: ${PGDATABASE}@${PGHOST}:${PGPORT}"
[[ -n "${SCHEMAS_CSV}" ]] && info "限定 schema: ${SCHEMAS_CSV}"

STDERR_FILE="$(mktemp)"
TMP_OUT=""
# 两个临时文件共用一个 EXIT trap：mv 之前任何异常退出都不残留 ${OUT_FILE}.XXXXXX（T28）
trap 'rm -f "${STDERR_FILE}" "${TMP_OUT}"' EXIT

RESULT="$(psql -At -X -q -v ON_ERROR_STOP=1 -v schemas_csv="${SCHEMAS_CSV}" \
    --single-transaction \
    -c "SET statement_timeout = '60s'" -c "SET lock_timeout = '10s'" \
    -f "${SCRIPT_DIR}/db-collect.sql" 2>"${STDERR_FILE}")" || {
    STATUS=$?
    STDERR_CONTENT="$(cat "${STDERR_FILE}" 2>/dev/null || true)"
    STDERR_CONTENT="$(db_llm_redact "${STDERR_CONTENT}")"
    if printf '%s' "${STDERR_CONTENT}" | grep -qiE "${DB_LLM_CONN_ERROR_RE}"; then
        fail "problem: 无法连接开发库（${PGHOST}:${PGPORT}/${PGDATABASE}）"
        fail "cause: psql 连接被拒或凭据缺失（.dbmeta/.dbllm.env 未正确配置或 PgBouncer 未起）"
        fail "fix: 核对 .dbmeta/.dbllm.env 的连接信息（不回显密码）"
    else
        fail "problem: 元数据采集失败（collect 层）"
        fail "cause: psql 执行 db-collect.sql 报错（详见下方 stderr，已脱敏）"
        echo "${STDERR_CONTENT}" >&2
        fail "fix: 单独重跑 shared/db-collect.sh 复现"
    fi
    exit "${STATUS}"
}

if [[ -n "${OUT_FILE}" ]]; then
    mkdir -p "$(dirname "${OUT_FILE}")"
    TMP_OUT="$(mktemp "${OUT_FILE}.XXXXXX")"
    printf '%s\n' "${RESULT}" > "${TMP_OUT}"
    mv "${TMP_OUT}" "${OUT_FILE}"
    TMP_OUT=""
    pass "已写入 ${OUT_FILE}"
else
    printf '%s\n' "${RESULT}"
fi
