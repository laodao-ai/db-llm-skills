#!/bin/bash
# Read-only session executor: guard -> ro credentials -> single psql connection
# (current_user check + READ ONLY transaction + statement) for one whitelisted
# SQL statement. See openspec/specs/ro-session/spec.md REQ-RS-2..RS-6.
#
# Usage:
#   shared/ro-session.sh --sql '<stmt>' [--limit N | --no-limit] [--out FILE] [--format csv|text]
#   shared/ro-session.sh --sql-file <path|-> [--limit N | --no-limit] [--out FILE] [--format csv|text]
#   shared/ro-session.sh --sql '<stmt>' --prepare [--out FILE]
#
# --prepare (ADR-0007, add-pg-sql-check REQ-RS-9): PREPARE-only validation
# path for pg-sql-check. The statement is only PREPARE'd, never EXECUTEd.
# Always equivalent to --no-limit (the caller need not also pass --no-limit);
# guard's keyword whitelist is not applied in this mode (guard mode=prepare).
#
# Exit codes:
#   0  success (result written to --out or stdout)
#   1  psql execution error (stderr already redacted), or a config error —
#      the underlying message is already problem/cause/fix
#   2  fail-closed: credentials not ready (CHANGE_ME placeholders or missing),
#      current_user != DB_USER, or the connection was refused — writes
#      .dbmeta/db-readonly/needs-human.md and prints ONLY that path to stdout
#   3  guard rejected the statement (multi-statement / meta-command /
#      not-whitelisted / bad-limit) — no credentials read, no connection made,
#      psql never invoked
#
# This script never establishes a database connection before the guard
# (shared/ro_guard.py) has judged the statement — REQ-RS-4 requires the guard
# to run before any connection. `.dbmeta/.dbllm.env` is loaded first only
# to resolve RO_DEFAULT_LIMIT for the guard's --limit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")}}"
RO_GUARD="${SCRIPT_DIR}/ro_guard.py"

RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
warn() { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }

usage() {
    cat <<'USAGE'
Usage: shared/ro-session.sh (--sql '<stmt>' | --sql-file <path|->)
                             [--limit N | --no-limit] [--out FILE] [--format csv|text]

  --sql STMT        Inline SQL statement (single statement; see ro_guard.py).
  --sql-file PATH    Read the statement from PATH, or from stdin if PATH is '-'.
  --limit N          Row cap for select/with (default: RO_DEFAULT_LIMIT from
                      .dbllm.env). Ignored for explain/show.
  --no-limit         Do not wrap select/with with a LIMIT clause.
  --out FILE         Write the result to FILE instead of stdout.
  --format csv|text  Output format (default: csv). explain/show are always
                      forced to text (a warning is printed if csv was requested).
  --prepare          PREPARE-only validation mode (never EXECUTEs the
                      statement); always equivalent to --no-limit.
  -h, --help         Show this help and exit.
USAGE
}

CONFIG_LIB="${SCRIPT_DIR}/config.sh"
if [[ ! -f "${CONFIG_LIB}" ]]; then
    echo -e "${RED}[FAIL]${NC} problem: 找不到 shared/config.sh（${CONFIG_LIB}）" >&2
    echo -e "${RED}[FAIL]${NC} cause: 本地 db-llm 安装不完整（config.sh 与 ro-session.sh 应同在 shared/ 下）" >&2
    echo -e "${RED}[FAIL]${NC} fix: 重新跑一次 db-llm 仓的 setup.sh，或核对该仓是否完整 clone" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "${CONFIG_LIB}"

# --- argument parsing ------------------------------------------------------
SQL_SRC=""       # "inline" | "file"
SQL_INLINE=""
SQL_FILE=""
LIMIT=""
NO_LIMIT=0
OUT_FILE=""
FORMAT="csv"
PREPARE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sql)
            [[ $# -ge 2 ]] || { db_llm_config_fail "--sql 缺少参数" "—" "传入 --sql '<statement>'" || true; usage >&2; exit 1; }
            SQL_INLINE="$2"; SQL_SRC="inline"; shift 2 ;;
        --sql-file)
            [[ $# -ge 2 ]] || { db_llm_config_fail "--sql-file 缺少参数" "—" "传入 --sql-file <path|->" || true; usage >&2; exit 1; }
            SQL_FILE="$2"; SQL_SRC="file"; shift 2 ;;
        --limit)
            [[ $# -ge 2 ]] || { db_llm_config_fail "--limit 缺少参数" "—" "传入 --limit <正整数>" || true; usage >&2; exit 1; }
            LIMIT="$2"; shift 2 ;;
        --no-limit)
            NO_LIMIT=1; shift ;;
        --prepare)
            PREPARE=1; shift ;;
        --out)
            [[ $# -ge 2 ]] || { db_llm_config_fail "--out 缺少参数" "—" "传入 --out <file>" || true; usage >&2; exit 1; }
            OUT_FILE="$2"; shift 2 ;;
        --format)
            [[ $# -ge 2 ]] || { db_llm_config_fail "--format 缺少参数" "—" "传入 --format csv|text" || true; usage >&2; exit 1; }
            FORMAT="$2"; shift 2 ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            db_llm_config_fail "未知参数 $1" "—" "参见 --help" || true
            usage >&2
            exit 1 ;;
    esac
done

if [[ -z "${SQL_SRC}" ]]; then
    db_llm_config_fail "缺少 --sql 或 --sql-file" "必须二选一指定要执行的语句" "传入 --sql '<statement>' 或 --sql-file <path|->" || true
    usage >&2
    exit 1
fi
if [[ "${PREPARE}" == "1" && -n "${LIMIT}" ]]; then
    db_llm_config_fail "--prepare 与 --limit 同时给出" "--prepare 恒等价于 --no-limit，二者语义冲突" "去掉 --limit（--prepare 无需搭配 --no-limit/--limit）" || true
    exit 1
fi
# --prepare is always equivalent to --no-limit (REQ-RS-9: the caller MUST NOT
# need to remember to also pass --no-limit). Forced here, before the mutex
# check below.
if [[ "${PREPARE}" == "1" ]]; then
    NO_LIMIT=1
fi
if [[ -n "${LIMIT}" && "${NO_LIMIT}" == "1" ]]; then
    db_llm_config_fail "--limit 与 --no-limit 同时给出" "两者互斥" "只传其中一个" || true
    exit 1
fi
if [[ "${FORMAT}" != "csv" && "${FORMAT}" != "text" ]]; then
    db_llm_config_fail "--format 值非法（${FORMAT}）" "只支持 csv 或 text" "传入 --format csv 或 --format text" || true
    exit 1
fi

if [[ "${SQL_SRC}" == "inline" ]]; then
    SQL_TEXT="${SQL_INLINE}"
elif [[ "${SQL_FILE}" == "-" ]]; then
    SQL_TEXT="$(cat)"
else
    if [[ ! -f "${SQL_FILE}" ]]; then
        db_llm_config_fail "找不到 --sql-file 指定的文件（${SQL_FILE}）" "路径不存在或不是常规文件" "核对 --sql-file 的路径" || true
        exit 1
    fi
    SQL_TEXT="$(cat "${SQL_FILE}")"
fi

# --- cleanup -----------------------------------------------------------
CLEANUP_FILES=()
cleanup() {
    if [[ ${#CLEANUP_FILES[@]} -gt 0 ]]; then
        rm -f "${CLEANUP_FILES[@]}"
    fi
}
trap cleanup EXIT

# --- needs-human helper --------------------------------------------------
# Writes .dbmeta/db-readonly/needs-human.md (fail-closed, REQ-RS-3), prints ONLY
# that path to stdout, and exits 2. MUST NOT be called with any content that
# still contains a password (callers only pass already-redacted stderr, or
# messages built from non-secret values like PGUSER/DBS_DB_USER).
write_needs_human_and_exit() {
    # Atomic replace via mktemp+mv [impl-review-fix] -- same shape
    # readonly-setup.sh already uses (REQ-RP-4). /pg-query-ro is a
    # high-frequency agent entry point, so concurrent invocations hitting the
    # same fail-closed branch would otherwise interleave their writes into one
    # truncate-in-place `>` target.
    local body="$1"
    local dir="${ROOT_DIR}/.dbmeta/db-readonly"
    mkdir -p "${dir}"
    local file="${dir}/needs-human.md"
    local tmp
    tmp="$(mktemp "${dir}/.needs-human.XXXXXX")"
    printf '%s\n' "${body}" > "${tmp}"
    mv "${tmp}" "${file}"
    printf '%s\n' "${file}"
    exit 2
}

# --- 1. guard: MUST run before any credential file is read or any DB connection
# db_llm_load_config (which also loads DB_PASSWORD) is deliberately NOT
# called here — REQ-RS-4 requires the structural judgment to finish before any
# credential is read. The only config value the guard itself needs is
# RO_DEFAULT_LIMIT (a non-credential key), so it is pulled directly via a raw
# line match on the config file instead of the full loader. The full
# credential load happens further below, only after the guard has accepted
# the statement.
command -v python3 >/dev/null 2>&1 || {
    db_llm_config_fail "python3 未安装或不在 PATH 中" "ro_guard.py 是纯 python3 stdlib 脚本" "安装 python3 后重试" || true
    exit 1
}

GUARD_LIMIT_ARGS=()
EFFECTIVE_LIMIT=""
if [[ "${NO_LIMIT}" == "1" ]]; then
    GUARD_LIMIT_ARGS=(--no-limit)
elif [[ -n "${LIMIT}" ]]; then
    EFFECTIVE_LIMIT="${LIMIT}"
    GUARD_LIMIT_ARGS=(--limit "${EFFECTIVE_LIMIT}")
else
    RO_DEFAULT_LIMIT_RAW=""
    RO_CONFIG_FILE="${ROOT_DIR}/.dbmeta/.dbllm.env"
    if [[ -f "${RO_CONFIG_FILE}" ]]; then
        RO_DEFAULT_LIMIT_RAW="$( { grep -E '^[[:space:]]*RO_DEFAULT_LIMIT[[:space:]]*=' "${RO_CONFIG_FILE}" || true; } \
            | tail -n1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    fi
    # Canonical default lives in shared/config.sh (DBS_RO_DEFAULT_LIMIT=200,
    # applied when .dbllm.env has no RO_DEFAULT_LIMIT line). Pulled here at
    # runtime instead of a second hardcoded "200" so the two can't drift; a
    # literal 200 fallback only guards against config.sh itself being
    # unreadable or its default line being reworded.
    CONFIG_SH_DEFAULT="$(grep -E '^\s*DBS_RO_DEFAULT_LIMIT=[0-9]+\s*$' "${SCRIPT_DIR}/config.sh" 2>/dev/null \
        | tail -n1 | grep -oE '[0-9]+' | tail -n1)"
    EFFECTIVE_LIMIT="${RO_DEFAULT_LIMIT_RAW:-${CONFIG_SH_DEFAULT:-200}}"
    GUARD_LIMIT_ARGS=(--limit "${EFFECTIVE_LIMIT}")
fi

# Built as two separate invocations (rather than an always-declared
# GUARD_MODE_ARGS=() array possibly expanded via "${GUARD_MODE_ARGS[@]}")
# because bash 3.2 (macOS's default /bin/bash) treats expanding an empty
# array under `set -u` as an unbound-variable error.
if [[ "${PREPARE}" == "1" ]]; then
    GUARD_JSON="$(printf '%s' "${SQL_TEXT}" | python3 "${RO_GUARD}" guard "${GUARD_LIMIT_ARGS[@]}" --prepare)" || {
        db_llm_config_fail "护栏子进程（ro_guard.py guard）执行失败" "参数或运行环境异常" "核对 shared/ro_guard.py 是否可执行、python3 版本" || true
        exit 1
    }
else
    GUARD_JSON="$(printf '%s' "${SQL_TEXT}" | python3 "${RO_GUARD}" guard "${GUARD_LIMIT_ARGS[@]}")" || {
        db_llm_config_fail "护栏子进程（ro_guard.py guard）执行失败" "参数或运行环境异常" "核对 shared/ro_guard.py 是否可执行、python3 版本" || true
        exit 1
    }
fi

# Extract the JSON fields into shell-safe variables (shlex.quote'd, so a
# multi-line/backslash-laden SQL statement in "sql"/"wrapped" round-trips
# intact through eval without being re-interpreted as shell syntax).
eval "$(printf '%s' "${GUARD_JSON}" | python3 -c '
import json, sys, shlex
d = json.load(sys.stdin)
for key in ("reason", "kind", "sql", "wrapped", "sha8"):
    v = d.get(key)
    v = "" if v is None else str(v)
    print("G_%s=%s" % (key.upper(), shlex.quote(v)))
print("G_OK=%s" % shlex.quote("1" if d.get("ok") else "0"))
')"

if [[ "${G_OK}" != "1" ]]; then
    case "${G_REASON}" in
        multi-statement)
            cause="输入含多条语句（顶层出现分号）"
            fixmsg="只发送单条 SELECT/WITH/EXPLAIN/SHOW 语句"
            ;;
        meta-command)
            cause="输入含 psql 元命令（如 \\! \\o \\i），语句经 psql -f 脚本执行时会被解释为元命令而非 SQL"
            fixmsg="去掉反斜杠开头的内容，只保留纯 SQL 文本"
            ;;
        bad-limit)
            cause="--limit 不是正整数"
            fixmsg="传入正整数 --limit，或改用 --no-limit"
            ;;
        not-whitelisted)
            cause="首关键字不在白名单 {SELECT, WITH, EXPLAIN, SHOW} 内，或语句中出现 set_config(...)"
            fixmsg="改写为 SELECT/WITH/EXPLAIN/SHOW 语句，且不要调用 set_config"
            ;;
        *)
            cause="reason=${G_REASON}"
            fixmsg="核对输入 SQL 是否为单条 SELECT/WITH/EXPLAIN/SHOW 语句"
            ;;
    esac
    db_llm_config_fail \
        "只读会话拒绝执行该语句（reason=${G_REASON}）" \
        "${cause}" \
        "${fixmsg}" || true
    exit 3
fi

KIND="${G_KIND}"
WRAPPED="${G_WRAPPED}"

# --- 2. credentials: only after the guard has accepted the statement -------
db_llm_load_config "${ROOT_DIR}/.dbmeta/.dbllm.env" || exit 1

PG_ENV_STDERR="$(mktemp)"
CLEANUP_FILES+=("${PG_ENV_STDERR}")
set +e
{ db_llm_export_pg_env; } 2>"${PG_ENV_STDERR}"
PG_ENV_RC=$?
set -e

if [[ ${PG_ENV_RC} -eq 2 ]]; then
    CAPTURED="$(db_llm_redact "$(cat "${PG_ENV_STDERR}" 2>/dev/null || true)")"
    BODY=$'# 只读会话不可用\n\n**原因**: .dbmeta/.dbllm.env 中的连接信息未填写或仍是 CHANGE_ME 占位符。\n\n## 需要人做什么\n\n1. 编辑 .dbmeta/.dbllm.env，填入 DB_HOST/DB_PORT/DB_NAME 的真实值。\n2. 运行 /pg-readonly-setup 完成只读角色供给（会自动生成 DB_PASSWORD）。\n3. 完成后重跑刚才的查询。\n\n<details><summary>诊断详情（已脱敏）</summary>\n\n'"${CAPTURED}"$'\n</details>'
    write_needs_human_and_exit "${BODY}"
fi
if [[ ${PG_ENV_RC} -ne 0 ]]; then
    cat "${PG_ENV_STDERR}" >&2
    exit 1
fi

# --- 3. EXPLAIN/SHOW cannot be wrapped in COPY(...) -------------------------
if [[ "${KIND}" == "explain" || "${KIND}" == "show" ]]; then
    if [[ "${FORMAT}" == "csv" ]]; then
        FORMAT="text"
        warn "EXPLAIN/SHOW 不支持 csv，已按 text 输出"
    fi
fi

# --- 4. generate the temp SQL script (REQ-RS-3, REQ-RS-5) ------------------
TMP_SQL="$(mktemp)"
CLEANUP_FILES+=("${TMP_SQL}")

# Shared security-critical session header (current_user check + READ ONLY
# transaction + statement_timeout) — defined once here and reused by both
# branches below, so a future change to these protections cannot be applied
# to one branch and forgotten in the other. [impl-review-fix]
COMMON_HEADER=(
    '\set ON_ERROR_STOP on'
    "SELECT current_user = :'ro_role' AS ro_ok \\gset"
    '\if :ro_ok'
    '\else'
    '  \echo RO_ROLE_MISMATCH'
    '  \quit 2'
    '\endif'
    'BEGIN;'
    'SET TRANSACTION READ ONLY;'
    "SET LOCAL statement_timeout = '30s';"
)

if [[ "${PREPARE}" == "1" ]]; then
    # ADR-0007 / REQ-RS-9: PREPARE-only validation path (pg-sql-check). WRAPPED
    # is the user statement's original text unmodified — guard's mode=prepare
    # + the forced no-limit above (step 1) mean it was never LIMIT-wrapped.
    # The prepared-statement name is randomized per invocation so concurrent
    # calls in the same session/connection never collide.
    PREPARE_NAME="_pgsc_$$_${RANDOM}${RANDOM}"
    SQL_SCRIPT_LINES=(
        "${COMMON_HEADER[0]}"
        '\set VERBOSITY verbose'
        "${COMMON_HEADER[@]:1}"
        "SET LOCAL lc_messages = 'C';"
        "PREPARE ${PREPARE_NAME} AS ${WRAPPED}"
        ';'
        "SELECT parameter_types, result_types FROM pg_prepared_statements WHERE name = '${PREPARE_NAME}';"
        "DEALLOCATE ${PREPARE_NAME};"
        'ROLLBACK;'
    )
else
    if [[ "${FORMAT}" == "csv" && ( "${KIND}" == "select" || "${KIND}" == "with" ) ]]; then
        BODY_STMT="COPY (${WRAPPED}) TO STDOUT CSV HEADER;"
    else
        BODY_STMT="${WRAPPED};"
    fi
    SQL_SCRIPT_LINES=(
        "${COMMON_HEADER[@]}"
        "${BODY_STMT}"
        'ROLLBACK;'
    )
fi
printf '%s\n' "${SQL_SCRIPT_LINES[@]}" > "${TMP_SQL}"

command -v psql >/dev/null 2>&1 || {
    db_llm_config_fail "psql 未安装或不在 PATH 中" "本机缺少 PostgreSQL 客户端" "安装 psql（如 brew install libpq）后重试" || true
    exit 1
}

# --- 5. one connection: current_user check + statement, same session -------
RAW_OUT="$(mktemp)"
CLEANUP_FILES+=("${RAW_OUT}")
RAW_ERR="$(mktemp)"
CLEANUP_FILES+=("${RAW_ERR}")

set +e
psql -X -q -A -t -v ON_ERROR_STOP=1 -v ro_role="${DBS_DB_USER}" -f "${TMP_SQL}" >"${RAW_OUT}" 2>"${RAW_ERR}"
PSQL_RC=$?
set -e

if [[ ${PSQL_RC} -eq 2 ]] && grep -q 'RO_ROLE_MISMATCH' "${RAW_OUT}" "${RAW_ERR}" 2>/dev/null; then
    BODY=$'# 只读会话不可用\n\n**原因**: 连接用户 `'"${PGUSER:-<unknown>}"$'` \xe2\x89\xa0 配置的 DB_USER `'"${DBS_DB_USER}"$'`。\n\n## 需要人做什么\n\n1. 确认 .dbmeta/.dbllm.env 的 DB_USER 与实际使用的角色名一致。\n2. 或运行 /pg-readonly-setup 重新供给只读角色。'
    write_needs_human_and_exit "${BODY}"
fi

if [[ ${PSQL_RC} -ne 0 ]]; then
    ERR_CONTENT="$(db_llm_redact "$(cat "${RAW_ERR}" 2>/dev/null || true)")"
    if printf '%s' "${ERR_CONTENT}" | grep -qiE '53300|too many connections'; then
        BODY=$'# 只读会话不可用\n\n**原因**: 连接被拒 —— 数据库连接数超限（53300）。\n\n## 需要人做什么\n\n1. 稍后重试，或检查是否有过多并发 ro-session 调用（角色 CONNECTION LIMIT 5）。\n\n<details><summary>诊断详情（已脱敏）</summary>\n\n'"${ERR_CONTENT}"$'\n</details>'
        write_needs_human_and_exit "${BODY}"
    elif printf '%s' "${ERR_CONTENT}" | grep -qiE "${DB_LLM_CONN_ERROR_RE}"; then
        BODY=$'# 只读会话不可用\n\n**原因**: 连接被拒 —— 认证失败或无法连接数据库。\n\n## 需要人做什么\n\n1. 核对 .dbmeta/.dbllm.env 中的 DB_HOST/DB_PORT/DB_NAME/DB_USER（不回显密码）。\n2. 确认数据库/PgBouncer 可达。\n\n<details><summary>诊断详情（已脱敏）</summary>\n\n'"${ERR_CONTENT}"$'\n</details>'
        write_needs_human_and_exit "${BODY}"
    else
        db_llm_config_fail "只读查询执行失败" "psql 返回非零退出码 ${PSQL_RC}（详见下方 stderr，已脱敏）" "核对 SQL 语句与 PG 报错详情；确认 .dbmeta/.dbllm.env 连接信息正确" || true
        echo "${ERR_CONTENT}" >&2
        exit 1
    fi
fi

if [[ -s "${RAW_ERR}" ]]; then
    db_llm_redact "$(cat "${RAW_ERR}")" >&2
fi

# --- 6. output (REQ-RS-6) ---------------------------------------------------
if [[ "${PREPARE}" == "1" ]]; then
    # Prepare mode's payload is a single parameter_types/result_types row
    # (or nothing, if PREPARE/read-back never ran) — never a result set to
    # truncate, so it is forwarded to --out/stdout as-is.
    if [[ -n "${OUT_FILE}" ]]; then
        mkdir -p "$(dirname "${OUT_FILE}")"
        cat "${RAW_OUT}" > "${OUT_FILE}"
    else
        cat "${RAW_OUT}"
    fi
else
    # per-kind truncation, forwarded to --out or stdout
    TRUNC_LIMIT="${EFFECTIVE_LIMIT:-2147483647}"
    if [[ -n "${OUT_FILE}" ]]; then
        mkdir -p "$(dirname "${OUT_FILE}")"
        python3 "${RO_GUARD}" truncate --limit "${TRUNC_LIMIT}" --format "${FORMAT}" < "${RAW_OUT}" > "${OUT_FILE}"
    else
        python3 "${RO_GUARD}" truncate --limit "${TRUNC_LIMIT}" --format "${FORMAT}" < "${RAW_OUT}"
    fi
fi

exit 0
