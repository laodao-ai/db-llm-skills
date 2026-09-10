#!/bin/bash
# Onboarding wrapper for the read-only channel: turns the "config file missing /
# credentials not filled / role not yet provisioned / audit findings" states
# into a five-step, three-exit-state flow a human or agent can drive to
# "只读通道可用" by rerunning this script.
#
# The toolchain no longer holds or uses any admin/DBA credential (ADR-0006):
# this script only orchestrates a pure-text generator (shared/ro-generate.sh,
# zero DB connections) and a read-only-credential auditor+prober
# (shared/ro-verify.sh) — every database connection THIS script's own process
# tree makes uses ONLY the .dbllm.env read-only credentials. The privileged
# SQL (.dbmeta/db-readonly/setup.sql) crosses the trust boundary as a text
# artifact a human DBA executes by hand.
#
# Steps (stop at the first unmet one):
#   ① .dbmeta/.dbllm.env missing -> auto-scaffold from the template,
#      exit 1; config file present but parse fails -> exit 1/2
#   ② shared/ro-generate.sh (pure text, zero DB connections) -> setup.sql +
#      userlist-fragment.txt + password written back to .dbllm.env; its own
#      fail-loud (exit 1) propagates as-is
#   ③ .dbllm.env's DB_HOST/DB_PORT/DB_NAME still literal CHANGE_ME
#      placeholders -> needs-human.md, exit 2; else probe role readiness and
#      distinguish (a) role not yet created from (b) authentication rejected
#      (four-way fix) -> needs-human.md, exit 2
#   ④ shared/ro-verify.sh: six-face audit + full-link probe — propagated as-is
#   ⑤ exit 0, "只读通道可用"
#
# Every exit-2 branch below writes .dbmeta/db-readonly/needs-human.md via
# mktemp+mv (atomic replace, REQ-RP-4 [spec-review-amendment]) and prints
# ONLY that file path to stdout — same fail-closed convention as
# shared/ro-session.sh and shared/ro-verify.sh.
#
# Testability seams (same pattern as pg-dict/scripts/pg-dict.sh's
# DBLLM_TEMPLATE_OVERRIDE): RO_GENERATE_SH_OVERRIDE / RO_VERIFY_SH_OVERRIDE
# let tests substitute stub scripts for steps ②/④; RO_PROBE_PSQL_OVERRIDE lets
# tests substitute a mock psql binary for step ③'s direct role-readiness probe
# without needing a real database.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")}}"
export ROOT_DIR

RO_GENERATE_SH="${RO_GENERATE_SH_OVERRIDE:-${SCRIPT_DIR}/../../shared/ro-generate.sh}"
RO_VERIFY_SH="${RO_VERIFY_SH_OVERRIDE:-${SCRIPT_DIR}/../../shared/ro-verify.sh}"
CONFIG_TEMPLATE="${DBLLM_TEMPLATE_OVERRIDE:-${SCRIPT_DIR}/../../shared/.dbllm.env.example}"
PROBE_PSQL="${RO_PROBE_PSQL_OVERRIDE:-psql}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $1" >&2; }
info() { echo -e "${YELLOW}[INFO]${NC} $1" >&2; }
fail() { echo -e "${RED}[FAIL]${NC} $1" >&2; }

CONFIG_LIB="${SCRIPT_DIR}/../../shared/config.sh"
if [[ ! -f "${CONFIG_LIB}" ]]; then
    fail "problem: 找不到 shared/config.sh（${CONFIG_LIB}）"
    fail "cause: 本地 db-llm 安装不完整"
    fail "fix: 重新跑一次 db-llm 仓的 setup.sh，或核对该仓是否完整 clone"
    exit 1
fi
# shellcheck source=/dev/null
source "${CONFIG_LIB}"

NEEDS_HUMAN_FILE="${ROOT_DIR}/.dbmeta/db-readonly/needs-human.md"

# write_needs_human_and_exit2 body
# mkdir -p's the target dir, atomically writes `body` via mktemp+mv (REQ-RP-4
# [spec-review-amendment]), prints ONLY the file path to stdout, exits 2.
# `body` MUST already be fully redacted (only non-secret values — key names,
# paths, role names — ever go into it below).
write_needs_human_and_exit2() {
    local body="$1"
    local dir="${ROOT_DIR}/.dbmeta/db-readonly"
    mkdir -p "${dir}"
    local tmp
    tmp="$(mktemp "${dir}/.needs-human.XXXXXX")"
    printf '%s\n' "${body}" > "${tmp}"
    mv "${tmp}" "${NEEDS_HUMAN_FILE}"
    printf '%s\n' "${NEEDS_HUMAN_FILE}"
    exit 2
}

strip_ansi() {
    printf '%s' "$1" | sed -E $'s/\x1b\\[[0-9;]*m//g'
}

# =============================================================================
# ① config: file present? parseable?
# =============================================================================
CONFIG_FILE="${ROOT_DIR}/.dbmeta/.dbllm.env"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    if [[ -e "${CONFIG_FILE}" ]]; then
        fail "problem: ${CONFIG_FILE} 存在但不是普通文件（可能是目录）"
        fail "cause: 该路径被占用，readonly-setup.sh 无法在此创建/读取配置文件"
        fail "fix: 删除或移走 ${CONFIG_FILE} 后重跑"
        exit 1
    fi
    if [[ ! -f "${CONFIG_TEMPLATE}" ]]; then
        fail "problem: 未找到配置文件 ${CONFIG_FILE}，也未找到安装模版 ${CONFIG_TEMPLATE}"
        fail "cause: 本地 db-llm 安装不完整"
        fail "fix: 重新跑一次 db-llm 仓的 setup.sh，或核对该仓是否完整 clone"
        exit 1
    fi
    if ! mkdir -p "$(dirname "${CONFIG_FILE}")"; then
        fail "problem: 自动初始化失败——无法创建目录 $(dirname "${CONFIG_FILE}")"
        fail "cause: 权限不足或磁盘已满"
        fail "fix: 检查 ${ROOT_DIR} 的写权限与磁盘剩余空间后重跑"
        exit 1
    fi
    if ! cp "${CONFIG_TEMPLATE}" "${CONFIG_FILE}"; then
        fail "problem: 自动初始化失败——无法复制模版到 ${CONFIG_FILE}"
        fail "cause: 文件复制失败（权限不足或磁盘已满）"
        fail "fix: 检查目录写权限与磁盘剩余空间后重跑；或手动执行 cp ${CONFIG_TEMPLATE} ${CONFIG_FILE}"
        exit 1
    fi
    fail "problem: 未找到配置文件 ${CONFIG_FILE}，已自动从模版创建一份"
    fail "cause: 首次在本项目运行 /pg-readonly-setup，需要先声明只读凭据文件路径与授权范围"
    fail "fix: 编辑 ${CONFIG_FILE}（填入 DB_HOST/DB_PORT/DB_NAME，按需调整 SCHEMAS/DB_USER），然后重跑本 skill"
    exit 1
fi

LOAD_STDERR_FILE="$(mktemp)"
set +e
db_llm_load_config "${CONFIG_FILE}" 2>"${LOAD_STDERR_FILE}"
LOAD_RC=$?
set -e
LOAD_MSG="$(cat "${LOAD_STDERR_FILE}" 2>/dev/null || true)"
rm -f "${LOAD_STDERR_FILE}"

if [[ ${LOAD_RC} -ne 0 ]]; then
    # Config parse failure — propagate as exit 1 with the problem/cause/fix
    # message config.sh already printed.
    printf '%s\n' "${LOAD_MSG}" >&2
    exit 1
fi

# =============================================================================
# ② generate (pure text, zero DB connections) — propagate its own exit code
# =============================================================================
if [[ ! -x "${RO_GENERATE_SH}" ]]; then
    fail "problem: 找不到可执行的 ro-generate.sh（${RO_GENERATE_SH}）"
    fail "cause: 本地 db-llm 安装不完整，或 RO_GENERATE_SH_OVERRIDE 指向了错误路径"
    fail "fix: 重新执行 db-llm 仓的 setup.sh 后重跑"
    exit 1
fi

set +e
"${RO_GENERATE_SH}"
GENERATE_RC=$?
set -e
if [[ ${GENERATE_RC} -ne 0 ]]; then
    exit "${GENERATE_RC}"
fi

# =============================================================================
# ③ role readiness probe — distinguish (a) role not yet created from
# (b) authentication rejected (four-way fix); .dbllm.env's
# DB_HOST/DB_PORT/DB_NAME may still be CHANGE_ME placeholders, which is
# neither (a) nor (b) — checked first.
# =============================================================================
db_llm_load_config "${CONFIG_FILE}" || exit 1

EXPORT_STDERR_FILE="$(mktemp)"
set +e
{ db_llm_export_pg_env; } 2>"${EXPORT_STDERR_FILE}"
EXPORT_RC=$?
set -e
if [[ ${EXPORT_RC} -ne 0 ]]; then
    if [[ ${EXPORT_RC} -eq 2 ]]; then
        BODY="# 只读通道尚未配置

## problem

.dbllm.env 的连接信息（DB_HOST/DB_PORT/DB_NAME）仍是占位值或未填写

## cause

密码已由 generate 步生成，但连接目标信息需要人手填。

## fix

编辑 ${CONFIG_FILE}，把 DB_HOST/DB_PORT/DB_NAME 换成真实连接信息后重跑 /pg-readonly-setup。"
        write_needs_human_and_exit2 "${BODY}"
    fi
    cat "${EXPORT_STDERR_FILE}" >&2
    rm -f "${EXPORT_STDERR_FILE}"
    exit 1
fi
rm -f "${EXPORT_STDERR_FILE}"

RO_ROLE="${DBS_DB_USER}"

if ! command -v "${PROBE_PSQL}" >/dev/null 2>&1; then
    fail "problem: psql 未安装或不在 PATH 中"
    fail "cause: 本机缺少 PostgreSQL 客户端"
    fail "fix: 安装 psql（如 brew install libpq）后重试"
    exit 1
fi

PROBE_STDERR_FILE="$(mktemp)"
set +e
"${PROBE_PSQL}" -X -q -At -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>"${PROBE_STDERR_FILE}"
PROBE_RC=$?
set -e

if [[ ${PROBE_RC} -ne 0 ]]; then
    PROBE_RAW="$(cat "${PROBE_STDERR_FILE}" 2>/dev/null || true)"
    rm -f "${PROBE_STDERR_FILE}"
    PROBE_REDACTED="$(db_llm_redact "${PROBE_RAW}")"

    if printf '%s' "${PROBE_RAW}" | grep -qiE 'role .* does not exist|no such user'; then
        BODY="# 只读通道尚未配置

## problem

只读角色 ${RO_ROLE} 尚未在数据库中创建

## cause

generate 只产出了 .dbmeta/db-readonly/setup.sql（纯文本，零 DB 连接）——角色需要由 DBA 亲手执行该脚本才会真正创建。

## fix

请以 DBA 身份执行 \`psql -f .dbmeta/db-readonly/setup.sql\`（账号只需 CREATEROLE，不需要 superuser），完成后重跑 /pg-readonly-setup 并删除 setup.sql。

<details><summary>诊断详情（已脱敏）</summary>

$(strip_ansi "${PROBE_REDACTED}")
</details>"
        write_needs_human_and_exit2 "${BODY}"
    elif printf '%s' "${PROBE_RAW}" | grep -qiE 'password authentication failed|SASL authentication failed|no password supplied'; then
        BODY="# 只读通道尚未配置

## problem

以 ${RO_ROLE} 的凭据认证被拒（经 PgBouncer 时无法区分角色不存在 / 密码不同 / userlist 缺条目）

## cause

经 PgBouncer 转发时，客户端只会收到塌缩后的通用认证失败字符串（如 SASL authentication failed），无法像直连 PG 那样细分「角色不存在」与「密码不同」；生成脚本对既存角色 MUST NOT 重设密码（幂等约束），所以单纯重跑 setup.sql 不会收敛这类不一致。

## fix

请按顺序排查：

1. 用任意账号执行以下 SQL 核验角色是否存在：\`SELECT rolname, rolcanlogin, rolconnlimit FROM pg_roles WHERE rolname = '${RO_ROLE}';\`
2. 无此行 → 请 DBA 执行 \`.dbmeta/db-readonly/setup.sql\`（账号只需 CREATEROLE，不需要 superuser）。
3. 有此行，且该角色是由其它项目经本套件建的 → 从先接入项目的 .dbmeta/.dbllm.env 取 DB_PASSWORD，填入本仓 ${CONFIG_FILE} 的同名字段（前提：不同库，或同库且 SCHEMAS 一致）。
4. 密码拿不到，或同库但 SCHEMAS 不同 → 由开发者在编辑器中把 ${CONFIG_FILE} 的 DB_USER 改为带项目名的值（如 ${RO_ROLE}_<project>）后重跑（本脚本与 agent 不代改）。
5. 确认本仓独占该角色 → 请 DBA 执行 \`ALTER ROLE ${RO_ROLE} PASSWORD '<取自 .dbllm.env 的 DB_PASSWORD，或 setup.sql 末尾注释态语句>';\`
   ⚠ 会改掉其它消费方的密码，确认独占再执行。
6. 仅当 PgBouncer 使用 auth_file 模式 → 把 .dbmeta/db-readonly/userlist-fragment.txt 追加进 userlist.txt 并 RELOAD。

<details><summary>诊断详情（已脱敏）</summary>

$(strip_ansi "${PROBE_REDACTED}")
</details>"
        write_needs_human_and_exit2 "${BODY}"
    else
        BODY="# 只读通道尚未配置

## problem

以只读角色 ${RO_ROLE} 的凭据连接数据库失败（非「角色不存在」或「认证被拒」）

## cause

可能是网络不可达、数据库/PgBouncer 未起，或 .dbllm.env 的 DB_HOST/DB_PORT/DB_NAME 填写有误。

## fix

核对 .dbllm.env 的连接信息与数据库/PgBouncer 是否可达后重跑 /pg-readonly-setup。

<details><summary>诊断详情（已脱敏）</summary>

$(strip_ansi "${PROBE_REDACTED}")
</details>"
        write_needs_human_and_exit2 "${BODY}"
    fi
fi
rm -f "${PROBE_STDERR_FILE}"

# =============================================================================
# ④ verify: six-face audit + full-link probe — propagate its own exit code
# (ro-verify.sh already writes its own needs-human.md / points at
# userlist-fragment.txt as appropriate)
# =============================================================================
if [[ ! -x "${RO_VERIFY_SH}" ]]; then
    fail "problem: 找不到可执行的 ro-verify.sh（${RO_VERIFY_SH}）"
    fail "cause: 本地 db-llm 安装不完整，或 RO_VERIFY_SH_OVERRIDE 指向了错误路径"
    fail "fix: 重新执行 db-llm 仓的 setup.sh 后重跑"
    exit 1
fi

set +e
"${RO_VERIFY_SH}"
VERIFY_RC=$?
set -e
if [[ ${VERIFY_RC} -ne 0 ]]; then
    exit "${VERIFY_RC}"
fi

# =============================================================================
# ⑤ done
# =============================================================================
pass "只读通道可用"
echo "只读通道可用"
exit 0
