#!/bin/bash
# Read-only-credential effective-access audit + full-link probe
# (ro-only-credential-architecture, design.md 「六面审计」/「安全与数据保护」,
# specs/ro-provision/spec.md REQ-RP-5, REQ-RP-3).
#
# Every SQL statement this script runs uses ONLY the .dbllm.env credentials
# (db_llm_export_pg_env) — no admin/superuser connection is ever opened.
# That is possible because every check below is expressed via
# the has_*_privilege() family (or reads pg_roles/pg_auth_members/pg_database,
# all world-readable) — PG's documentation (§9.27.2, "Access Privilege
# Inquiry Functions") states these are safe to call by any role about any
# other role, regardless of the caller's own privileges.
#
# Six-face audit (design.md 「六面审计」 table):
#   ① role flags (SUPERUSER/CREATEDB/BYPASSRLS/REPLICATION/CREATEROLE) + role
#      membership                                                        fail-closed
#   ② schema CREATE / database CREATE-TEMP                               fail-closed
#   ③ any table's non-SELECT privilege, any sequence's USAGE/UPDATE       fail-closed
#      (unscoped — checked across ALL schemas, not just the SCHEMAS scope)
#   ④ out-of-scope schema USAGE / table SELECT residual                  fail-closed
#   ⑤ object ownership, dblink*/postgres_fdw escape surface              fail-closed
#   ⑥ PUBLIC pseudo-role double-check (folded into every face's query via
#      an explicit has_*_privilege('public', ...) OR clause) + out-of-scope
#      SECURITY DEFINER (in-scope SECURITY DEFINER is report-only)
# Additional checks (design gate approved, REQ-RP-5 「附加检查项」):
#   (a) pg_default_acl drift favoring RO_ROLE/PUBLIC — out-of-scope fail-closed,
#       in-scope report-only
#   (b) cluster CONNECT on a non-target database — always report-only
#   (c) in-scope table owner without a matching default-ACL registrant —
#       always report-only
#
# Any fail-closed finding -> .dbmeta/db-readonly/needs-human.md (REVOKE/ALTER
# ROLE SQL a DBA can run as-is) + exit 2. Report-only findings are printed to
# stderr as [WARN] and never affect the exit code.
#
# If the audit is clean, this script delegates a full-link probe to
# shared/ro-session.sh (--sql 'SELECT 1') — the SAME path pg-query-ro.sh uses
# in production, not a raw psql connectivity check. Probe failure -> exit 1,
# fixed and mechanically distinct from the audit's exit 2 (REQ-RP-3); the
# orchestrator (readonly-setup.sh) relies on that distinction to decide
# which needs-human guidance to show. This script does not print anything
# ro-session.sh itself produced (it already redacts, but the safest guarantee
# of "terminal 零密码/hash" is to not relay subprocess output at all) — it
# only points at .dbmeta/db-readonly/userlist-fragment.txt, which is produced by
# shared/ro-generate.sh, not by this script.
#
# Non-Goal: this script does not touch ro_guard.py or ro-session.sh's query
# guardrails/session semantics — the probe step is a pure delegation.
#
# Usage:
#   shared/ro-verify.sh [-h|--help]
#
# Exit codes:
#   0  six-face audit clean AND the full-link probe succeeded — "只读通道可用"
#   1  generic fail-loud (bad config, RO connection itself failed, psql
#      missing, ...) OR the full-link probe failed (REQ-RP-3) — these two
#      sub-cases share exit code 1 by design (both mean "not a security
#      finding, try again after fixing connectivity/role provisioning"); they
#      are only distinguished by message text, never by exit code
#   2  fail-closed: a six-face (or additional-check) audit finding — writes
#      .dbmeta/db-readonly/needs-human.md with ready-to-run remediation SQL
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")}}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $1" >&2; }
info() { echo -e "${YELLOW}[INFO]${NC} $1" >&2; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }
fail() { echo -e "${RED}[FAIL]${NC} $1" >&2; }

usage() {
    cat <<'USAGE'
Usage: shared/ro-verify.sh [-h|--help]

Runs the six-face read-only-role effective-access audit (all queries execute
under the .dbllm.env credentials only) and, if clean, a full-link probe
delegated to shared/ro-session.sh. See the file header for the exit-code
contract.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        *) fail "problem: 未知参数 $1"; usage >&2; exit 1 ;;
    esac
done

CONFIG_LIB="${SCRIPT_DIR}/config.sh"
if [[ ! -f "${CONFIG_LIB}" ]]; then
    fail "problem: 找不到 shared/config.sh（${CONFIG_LIB}）"
    fail "cause: 本地 db-llm 安装不完整（config.sh 与 ro-verify.sh 应同在 shared/ 下）"
    fail "fix: 重新跑一次 db-llm 仓的 setup.sh，或核对该仓是否完整 clone"
    exit 1
fi
# shellcheck source=/dev/null
source "${CONFIG_LIB}"

db_llm_load_config "${ROOT_DIR}/.dbmeta/.dbllm.env" || exit 1

if ! command -v psql >/dev/null 2>&1; then
    fail "problem: psql 未安装或不在 PATH 中"
    fail "cause: 本机缺少 PostgreSQL 客户端"
    fail "fix: 安装 psql（如 brew install libpq）后重试"
    exit 1
fi

strip_ansi() {
    printf '%s' "$1" | sed -E $'s/\x1b\\[[0-9;]*m//g'
}

write_needs_human_and_exit2() {
    # Atomic replace via mktemp+mv [impl-review-fix] -- same shape
    # readonly-setup.sh already uses (REQ-RP-4). A truncate-in-place `>`
    # lets two concurrent runs interleave their writes into a file whose whole
    # point is that a DBA copy-pastes the SQL out of it.
    local body="$1"
    local dir="${ROOT_DIR}/.dbmeta/db-readonly"
    mkdir -p "${dir}"
    local tmp
    tmp="$(mktemp "${dir}/.needs-human.XXXXXX")"
    printf '%s\n' "${body}" > "${tmp}"
    mv "${tmp}" "${dir}/needs-human.md"
    printf '%s\n' "${dir}/needs-human.md"
    exit 2
}

# Signal-interrupt cleanup: track all mktemp files and remove on EXIT (T19).
_RV_TMPFILES=()
_rv_cleanup() { rm -f "${_RV_TMPFILES[@]}"; }
trap _rv_cleanup EXIT

# --- RO-only connection (single-mode credential resolution) -----------------
# rc 2 from the resolver means "credential file missing / not a regular file /
# escapes the repo root" -- this script exit-code contract reserves 2 for
# "fail-closed, and .dbmeta/db-readonly/needs-human.md explains it", so the bare
# `exit $?` that used to sit here broke its own contract by exiting 2 with no
# such file on disk [impl-review-fix]. Mirrors ro-session.sh:224 handling of
# the exact same failure source.
set +e
PG_ENV_STDERR="$(mktemp)"; _RV_TMPFILES+=("${PG_ENV_STDERR}")
{ db_llm_export_pg_env; } 2>"${PG_ENV_STDERR}"
PG_ENV_RC=$?
set -e
PG_ENV_MSG="$(db_llm_redact "$(cat "${PG_ENV_STDERR}" 2>/dev/null || true)")"
rm -f "${PG_ENV_STDERR}"
if [[ ${PG_ENV_RC} -eq 2 ]]; then
    write_needs_human_and_exit2 "# 只读凭据不可解析

## problem

无法从 .dbllm.env 解析出只读连接凭据，审计无法开始。

## cause

$(strip_ansi "${PG_ENV_MSG}")

## fix

编辑 .dbmeta/.dbllm.env，补全 DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD 五项连接信息（DB_PASSWORD 由 /pg-readonly-setup 自动生成，其余手动填写），然后重跑 /pg-readonly-setup。"
elif [[ ${PG_ENV_RC} -ne 0 ]]; then
    printf '%s\n' "${PG_ENV_MSG}" >&2
    exit "${PG_ENV_RC}"
fi

RO_ROLE="${DBS_DB_USER}"
info "=== ro-verify ==="
info "数据库: ${PGDATABASE}@${PGHOST}:${PGPORT}"
info "审计角色: ${RO_ROLE}"

sql_ident() {
    # Double-quote a Postgres identifier, doubling any embedded `"`.
    printf '"%s"' "${1//\"/\"\"}"
}

# ro_audit_sql [extra psql flags...] < SQL-on-stdin
# Opens ONE connection (the RO credentials already exported above) per call,
# always via `-f -`, always prefixed with an explicit statement_timeout
# override — the RO_ROLE's own role-level default is 15s (set by
# ro-generate.sh's DO block); several of the faces below scan every
# non-system table/sequence/schema in the database and can legitimately take
# longer than that on a large schema, so a false 57014 timeout MUST NOT be
# misread as an audit hit (design.md Risks/Trade-offs, "低频不等于允许假失败").
# 60s mirrors db-collect.sh's existing override precedent.
ro_audit_sql() {
    local _stderr_file _rc=0
    _stderr_file="$(mktemp)"; _RV_TMPFILES+=("${_stderr_file}")
    { printf '%s\n' "SET statement_timeout = '60s';"; cat; } \
        | psql -X -q -v ON_ERROR_STOP=1 "$@" -f - 2>"${_stderr_file}" || _rc=$?
    if [[ ${_rc} -ne 0 ]]; then
        local _redacted
        _redacted="$(db_llm_redact "$(cat "${_stderr_file}" 2>/dev/null || true)")"
        rm -f "${_stderr_file}"
        fail "problem: 只读审计连接执行 SQL 失败（退出码 ${_rc}）"
        fail "cause: 角色可能尚未由 DBA 供给（setup.sql 未执行），或凭据/网络异常；详见下方 stderr（已脱敏）"
        echo "${_redacted}" >&2
        # Generic fail-loud, same bucket as the probe-failure exit code below
        # (REQ-RP-3) — the audit's own connection never having succeeded is
        # "not ready yet", not a security finding, so it MUST NOT reuse exit 2.
        exit 1
    fi
    rm -f "${_stderr_file}"
}

# --- Authorization scope (same inclusion rule as db-collect.sql /
# shared/ro-generate.sh's DO block — duplicated here, not \i-included, same
# rationale as the equivalent duplication this replaced in ro-provision.sh:
# keep the WHERE/EXISTS clauses in sync if the rule ever changes) ------------
SCOPE_SCHEMAS=()
if [[ -n "${DBS_SCHEMAS}" ]]; then
    IFS=',' read -ra SCOPE_SCHEMAS <<< "${DBS_SCHEMAS}"
else
    INCLUSION_SQL="
SELECT n.nspname
FROM pg_namespace n
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_%'
  AND EXISTS (
      SELECT 1 FROM pg_class c
      WHERE c.relnamespace = n.oid
        AND c.relkind IN ('r', 'p', 'f', 'v', 'm')
        AND NOT EXISTS (
            SELECT 1 FROM pg_depend d JOIN pg_extension e
              ON d.refobjid = e.oid AND d.deptype = 'e'
            WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
        )
      UNION ALL
      SELECT 1 FROM pg_proc p
      WHERE p.pronamespace = n.oid AND p.prokind = 'f'
        AND NOT EXISTS (
            SELECT 1 FROM pg_depend d JOIN pg_extension e
              ON d.refobjid = e.oid AND d.deptype = 'e'
            WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
        )
  )
ORDER BY n.nspname;
"
    _inclusion_output="$(printf '%s' "${INCLUSION_SQL}" | ro_audit_sql -At)"
    while IFS= read -r _schema; do
        [[ -n "${_schema}" ]] && SCOPE_SCHEMAS+=("${_schema}")
    done <<< "${_inclusion_output}"
fi
SCOPE_CSV="$(IFS=,; echo "${SCOPE_SCHEMAS[*]:-}")"
info "授权范围: ${SCOPE_CSV:-<empty>}"

in_scope() {
    local _ns="$1" _s
    for _s in "${SCOPE_SCHEMAS[@]+"${SCOPE_SCHEMAS[@]}"}"; do
        [[ "${_s}" == "${_ns}" ]] && return 0
    done
    return 1
}

# --- Finding accumulators ----------------------------------------------------
_BLOCKING_FINDINGS=""
_BLOCKING_FIXES=""
add_blocking() {
    local _desc="$1" _fix="$2"
    _BLOCKING_FINDINGS+="- ${_desc}"$'\n'
    [[ -n "${_fix}" ]] && _BLOCKING_FIXES+="${_fix}"$'\n'
}
add_report() {
    warn "$1"
}

# =============================================================================
# Face 0: the audited identity MUST be the connected identity [impl-review-fix]
# =============================================================================
# Every face below asks `has_*_privilege(RO_ROLE, ...)`, where RO_ROLE comes
# from .dbllm.env -- a config string, not from this connection. If it drifts
# away from the user this connection actually authenticated as (DB_USER in
# .dbllm.env), all six faces would audit some OTHER role and print
# "六面审计通过" about an identity nobody is using. ro-generate.sh does check
# this consistency, but only for the already-exists branch and only when the
# five-step orchestration runs it -- and this script's own failure messages
# tell people to "重跑本脚本" directly. ro-session.sh:264 already enforces the
# same invariant per session (`SELECT current_user = :'ro_role'`); this makes
# ro-verify.sh self-sufficient rather than relying on the probe (which runs
# only AFTER the audit has already reported).
_CURRENT_USER="$(printf '%s' "SELECT current_user" | ro_audit_sql -At)"
if [[ "${_CURRENT_USER}" != "${RO_ROLE}" ]]; then
    write_needs_human_and_exit2 "# 审计身份与连接身份不一致

## problem

.dbllm.env 的 DB_USER 是 \`${RO_ROLE}\`，但本次连接实际认证的角色是 \`${_CURRENT_USER}\`。审计已中止（若继续，六面审计会去审一个并非本工具链在用的角色）。

## cause

.dbllm.env 的 DB_USER 与实际连接的用户名不一致——通常是配置从别的项目拷来后忘了改 DB_USER。

## fix

编辑 .dbmeta/.dbllm.env，确保 DB_USER 与实际要审计的只读角色名一致，然后重跑 /pg-readonly-setup。"
fi

# =============================================================================
# Face 1: role flags + role membership
# =============================================================================
_Q_ROLE="$(sql_ident "${RO_ROLE}")"

_flags="$(printf '%s' \
    "SELECT 'SUPERUSER', rolsuper FROM pg_roles WHERE rolname = :'ro_role'
     UNION ALL SELECT 'CREATEDB', rolcreatedb FROM pg_roles WHERE rolname = :'ro_role'
     UNION ALL SELECT 'BYPASSRLS', rolbypassrls FROM pg_roles WHERE rolname = :'ro_role'
     UNION ALL SELECT 'REPLICATION', rolreplication FROM pg_roles WHERE rolname = :'ro_role'
     UNION ALL SELECT 'CREATEROLE', rolcreaterole FROM pg_roles WHERE rolname = :'ro_role'" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}")"
while IFS='|' read -r _flag _val; do
    [[ -z "${_flag}" ]] && continue
    if [[ "${_val}" == "t" ]]; then
        add_blocking "角色持有 ${_flag}" "ALTER ROLE ${_Q_ROLE} NO${_flag};"
    fi
done <<< "${_flags}"

_members="$(printf '%s' \
    "SELECT r.rolname FROM pg_auth_members m
     JOIN pg_roles r ON r.oid = m.roleid
     JOIN pg_roles g ON g.oid = m.member
     WHERE g.rolname = :'ro_role'" \
    | ro_audit_sql -At -v ro_role="${RO_ROLE}")"
while IFS= read -r _parent; do
    [[ -z "${_parent}" ]] && continue
    add_blocking "角色是 ${_parent} 的成员" "REVOKE $(sql_ident "${_parent}") FROM ${_Q_ROLE};"
done <<< "${_members}"

# =============================================================================
# Face 2: schema CREATE / database CREATE-TEMP (RO_ROLE and PUBLIC, folded)
# =============================================================================
_schema_create="$(printf '%s' \
    "SELECT n.nspname,
            has_schema_privilege(:'ro_role', n.oid, 'CREATE'),
            has_schema_privilege('public', n.oid, 'CREATE')
     FROM pg_namespace n
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
       AND (has_schema_privilege(:'ro_role', n.oid, 'CREATE') OR has_schema_privilege('public', n.oid, 'CREATE'))" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}")"
while IFS='|' read -r _ns _ro_hit _pub_hit; do
    [[ -z "${_ns}" ]] && continue
    _q_ns="$(sql_ident "${_ns}")"
    [[ "${_ro_hit}" == "t" ]] && add_blocking "schema ${_ns} 上角色持有 CREATE" "REVOKE CREATE ON SCHEMA ${_q_ns} FROM ${_Q_ROLE};"
    [[ "${_pub_hit}" == "t" ]] && add_blocking "schema ${_ns} 上 PUBLIC 持有 CREATE" "REVOKE CREATE ON SCHEMA ${_q_ns} FROM PUBLIC;"
done <<< "${_schema_create}"

_db_create="$(printf '%s' \
    "SELECT 'CREATE', has_database_privilege(:'ro_role', current_database(), 'CREATE'), has_database_privilege('public', current_database(), 'CREATE')
     UNION ALL
     SELECT 'TEMP', has_database_privilege(:'ro_role', current_database(), 'TEMP'), has_database_privilege('public', current_database(), 'TEMP')" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}")"
while IFS='|' read -r _kind _ro_hit _pub_hit; do
    [[ -z "${_kind}" ]] && continue
    _priv="CREATE"; [[ "${_kind}" == "TEMP" ]] && _priv="TEMPORARY"
    _q_db="$(sql_ident "${PGDATABASE}")"
    if [[ "${_kind}" == "TEMP" ]]; then
        # TEMP 是 report-only（不阻断）：PG 建库时缺省就把 TEMPORARY 授给 PUBLIC
        # （`datacl` 里的 `=Tc/owner`），fail-closed 会在**任何默认配置的集群**上假阳；
        # 而唯一的整改手段 `REVOKE TEMPORARY … FROM PUBLIC` 动的是全库公共缺省、影响该库
        # 所有用户——PG 没有针对单个角色的反向撤销，权限是累加的。风险面本身也小：临时表
        # 落在会话私有的 pg_temp_N、断连即消，最坏是占磁盘，且工具链每次会话都
        # `SET TRANSACTION READ ONLY`（ro-session.sh），走工具链根本建不出来。
        # 与附加检查项 (b) 的 cluster CONNECT 同档同因。
        [[ "${_ro_hit}" == "t" || "${_pub_hit}" == "t" ]] \
            && add_report "当前库 ${PGDATABASE} 上可建临时表（PG 缺省把 TEMPORARY 授予 PUBLIC；仅报告，不阻断。如需收紧: REVOKE ${_priv} ON DATABASE ${_q_db} FROM PUBLIC; —— 注意这会影响该库所有用户）"
        continue
    fi
    [[ "${_ro_hit}" == "t" ]] && add_blocking "当前库 ${PGDATABASE} 上角色持有 ${_kind}" "REVOKE ${_priv} ON DATABASE ${_q_db} FROM ${_Q_ROLE};"
    [[ "${_pub_hit}" == "t" ]] && add_blocking "当前库 ${PGDATABASE} 上 PUBLIC 持有 ${_kind}" "REVOKE ${_priv} ON DATABASE ${_q_db} FROM PUBLIC;"
done <<< "${_db_create}"

# =============================================================================
# Face 3: any table's non-SELECT privilege (unscoped), any sequence's
# USAGE/UPDATE (unscoped) — RO_ROLE and PUBLIC, folded
# =============================================================================
_table_priv_list="INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER"
_table_hits="$(printf '%s' \
    "SELECT c.oid::regclass::text,
            has_table_privilege(:'ro_role', c.oid, 'INSERT'), has_table_privilege(:'ro_role', c.oid, 'UPDATE'),
            has_table_privilege(:'ro_role', c.oid, 'DELETE'), has_table_privilege(:'ro_role', c.oid, 'TRUNCATE'),
            has_table_privilege(:'ro_role', c.oid, 'REFERENCES'), has_table_privilege(:'ro_role', c.oid, 'TRIGGER'),
            has_table_privilege('public', c.oid, 'INSERT'), has_table_privilege('public', c.oid, 'UPDATE'),
            has_table_privilege('public', c.oid, 'DELETE'), has_table_privilege('public', c.oid, 'TRUNCATE'),
            has_table_privilege('public', c.oid, 'REFERENCES'), has_table_privilege('public', c.oid, 'TRIGGER')
     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm')
       AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
       AND (has_table_privilege(:'ro_role', c.oid, '${_table_priv_list}')
            OR has_table_privilege('public', c.oid, '${_table_priv_list}'))" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}")"
while IFS='|' read -r _tbl _ri _ru _rd _rt _rr _rg _pi _pu _pd _pt _pr _pg; do
    [[ -z "${_tbl}" ]] && continue
    [[ "${_ri}" == "t" ]] && add_blocking "表 ${_tbl} 上角色持有 INSERT" "REVOKE INSERT ON ${_tbl} FROM ${_Q_ROLE};"
    [[ "${_ru}" == "t" ]] && add_blocking "表 ${_tbl} 上角色持有 UPDATE" "REVOKE UPDATE ON ${_tbl} FROM ${_Q_ROLE};"
    [[ "${_rd}" == "t" ]] && add_blocking "表 ${_tbl} 上角色持有 DELETE" "REVOKE DELETE ON ${_tbl} FROM ${_Q_ROLE};"
    [[ "${_rt}" == "t" ]] && add_blocking "表 ${_tbl} 上角色持有 TRUNCATE" "REVOKE TRUNCATE ON ${_tbl} FROM ${_Q_ROLE};"
    [[ "${_rr}" == "t" ]] && add_blocking "表 ${_tbl} 上角色持有 REFERENCES" "REVOKE REFERENCES ON ${_tbl} FROM ${_Q_ROLE};"
    [[ "${_rg}" == "t" ]] && add_blocking "表 ${_tbl} 上角色持有 TRIGGER" "REVOKE TRIGGER ON ${_tbl} FROM ${_Q_ROLE};"
    [[ "${_pi}" == "t" ]] && add_blocking "表 ${_tbl} 上 PUBLIC 持有 INSERT" "REVOKE INSERT ON ${_tbl} FROM PUBLIC;"
    [[ "${_pu}" == "t" ]] && add_blocking "表 ${_tbl} 上 PUBLIC 持有 UPDATE" "REVOKE UPDATE ON ${_tbl} FROM PUBLIC;"
    [[ "${_pd}" == "t" ]] && add_blocking "表 ${_tbl} 上 PUBLIC 持有 DELETE" "REVOKE DELETE ON ${_tbl} FROM PUBLIC;"
    [[ "${_pt}" == "t" ]] && add_blocking "表 ${_tbl} 上 PUBLIC 持有 TRUNCATE" "REVOKE TRUNCATE ON ${_tbl} FROM PUBLIC;"
    [[ "${_pr}" == "t" ]] && add_blocking "表 ${_tbl} 上 PUBLIC 持有 REFERENCES" "REVOKE REFERENCES ON ${_tbl} FROM PUBLIC;"
    [[ "${_pg}" == "t" ]] && add_blocking "表 ${_tbl} 上 PUBLIC 持有 TRIGGER" "REVOKE TRIGGER ON ${_tbl} FROM PUBLIC;"
done <<< "${_table_hits}"

_seq_hits="$(printf '%s' \
    "SELECT c.oid::regclass::text,
            has_sequence_privilege(:'ro_role', c.oid, 'USAGE'), has_sequence_privilege(:'ro_role', c.oid, 'UPDATE'),
            has_sequence_privilege('public', c.oid, 'USAGE'), has_sequence_privilege('public', c.oid, 'UPDATE')
     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind = 'S'
       AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
       AND (has_sequence_privilege(:'ro_role', c.oid, 'USAGE,UPDATE') OR has_sequence_privilege('public', c.oid, 'USAGE,UPDATE'))" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}")"
while IFS='|' read -r _seq _ru _rup _pu _pup; do
    [[ -z "${_seq}" ]] && continue
    [[ "${_ru}" == "t" ]] && add_blocking "sequence ${_seq} 上角色持有 USAGE" "REVOKE USAGE ON SEQUENCE ${_seq} FROM ${_Q_ROLE};"
    [[ "${_rup}" == "t" ]] && add_blocking "sequence ${_seq} 上角色持有 UPDATE" "REVOKE UPDATE ON SEQUENCE ${_seq} FROM ${_Q_ROLE};"
    [[ "${_pu}" == "t" ]] && add_blocking "sequence ${_seq} 上 PUBLIC 持有 USAGE" "REVOKE USAGE ON SEQUENCE ${_seq} FROM PUBLIC;"
    [[ "${_pup}" == "t" ]] && add_blocking "sequence ${_seq} 上 PUBLIC 持有 UPDATE" "REVOKE UPDATE ON SEQUENCE ${_seq} FROM PUBLIC;"
done <<< "${_seq_hits}"

# =============================================================================
# Face 4: out-of-scope schema USAGE / table SELECT residual
# =============================================================================
_out_usage="$(printf '%s' \
    "SELECT n.nspname,
            has_schema_privilege(:'ro_role', n.oid, 'USAGE'), has_schema_privilege('public', n.oid, 'USAGE')
     FROM pg_namespace n
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
       AND n.nspname <> ALL (string_to_array(:'scope_csv', ','))
       AND (has_schema_privilege(:'ro_role', n.oid, 'USAGE') OR has_schema_privilege('public', n.oid, 'USAGE'))" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}" -v scope_csv="${SCOPE_CSV}")"
while IFS='|' read -r _ns _ro_hit _pub_hit; do
    [[ -z "${_ns}" ]] && continue
    _q_ns="$(sql_ident "${_ns}")"
    if [[ "${_ns}" == "public" ]]; then
        # 同 TEMP：PG 缺省把 public 的 USAGE 授给 PUBLIC（`nspacl` 里的 `=U/owner`），
        # fail-closed 会在任何默认集群上假阳，整改又只能 FROM PUBLIC、影响全库用户。
        # 单有 USAGE 读不到任何数据——真正的暴露面是表上的 SELECT，那条（_out_select）
        # 仍然 fail-closed，public 里若真有业务表照样会被拦。
        [[ "${_ro_hit}" == "t" || "${_pub_hit}" == "t" ]] \
            && add_report "范围外 schema public 上有 USAGE（PG 缺省授予 PUBLIC；仅报告，不阻断——单有 USAGE 读不到数据，public 内表的 SELECT 仍 fail-closed）"
        continue
    fi
    [[ "${_ro_hit}" == "t" ]] && add_blocking "范围外 schema ${_ns} 上角色仍持有 USAGE" "REVOKE USAGE ON SCHEMA ${_q_ns} FROM ${_Q_ROLE};"
    [[ "${_pub_hit}" == "t" ]] && add_blocking "范围外 schema ${_ns} 上 PUBLIC 持有 USAGE" "REVOKE USAGE ON SCHEMA ${_q_ns} FROM PUBLIC;"
done <<< "${_out_usage}"

_out_select="$(printf '%s' \
    "SELECT c.oid::regclass::text,
            has_table_privilege(:'ro_role', c.oid, 'SELECT'), has_table_privilege('public', c.oid, 'SELECT')
     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm')
       AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
       AND n.nspname <> ALL (string_to_array(:'scope_csv', ','))
       AND (has_table_privilege(:'ro_role', c.oid, 'SELECT') OR has_table_privilege('public', c.oid, 'SELECT'))" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}" -v scope_csv="${SCOPE_CSV}")"
while IFS='|' read -r _tbl _ro_hit _pub_hit; do
    [[ -z "${_tbl}" ]] && continue
    [[ "${_ro_hit}" == "t" ]] && add_blocking "范围外表 ${_tbl} 上角色仍持有 SELECT" "REVOKE SELECT ON ${_tbl} FROM ${_Q_ROLE};"
    [[ "${_pub_hit}" == "t" ]] && add_blocking "范围外表 ${_tbl} 上 PUBLIC 持有 SELECT" "REVOKE SELECT ON ${_tbl} FROM PUBLIC;"
done <<< "${_out_select}"

# =============================================================================
# Face 5: object ownership, dblink*/postgres_fdw escape surface
# =============================================================================
_owned="$(printf '%s' \
    "SELECT c.oid::regclass::text FROM pg_class c WHERE pg_get_userbyid(c.relowner) = :'ro_role'
     UNION ALL
     SELECT p.oid::regprocedure::text FROM pg_proc p WHERE pg_get_userbyid(p.proowner) = :'ro_role'" \
    | ro_audit_sql -At -v ro_role="${RO_ROLE}")"
while IFS= read -r _obj; do
    [[ -z "${_obj}" ]] && continue
    add_blocking "角色是对象 ${_obj} 的 owner" "-- 请人工核定新 owner 后执行：ALTER TABLE/SEQUENCE/VIEW/FUNCTION ${_obj} OWNER TO <new_owner>;"
done <<< "${_owned}"

# `p.oid::regprocedure::text` renders the signature with PostgreSQL's own
# identifier quoting [impl-review-fix]. The previous `nspname || '.' || proname
# || '(' || ... || ')'` concatenation emitted RAW object names straight into
# the REVOKE statements this script writes into needs-human.md for a DBA to
# copy-paste -- a second-order injection surface, and already wrong for any
# name needing quotes (mixed case, spaces, a literal parenthesis, which also
# broke the `%%(` / `#*(` split below). Face 5's `_owned` query above already
# used the `::regclass`/`::regprocedure` form; this brings the escape and
# SECURITY DEFINER faces in line with it.
_escape_sql="
SELECT 'function:' || p.oid::regprocedure::text
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proname LIKE 'dblink%'
  AND has_function_privilege(:'ro_role', p.oid, 'EXECUTE')
UNION ALL
SELECT 'fdw_server:' || srvname
FROM pg_foreign_server
WHERE has_server_privilege(:'ro_role', oid, 'USAGE');
"
_escape_hits="$(printf '%s' "${_escape_sql}" | ro_audit_sql -At -v ro_role="${RO_ROLE}")"
while IFS= read -r _hit; do
    [[ -z "${_hit}" ]] && continue
    case "${_hit}" in
        function:*)
            _sig="${_hit#function:}"
            # Revoke from BOTH grantees [impl-review-fix]: the predicate above
            # is has_function_privilege(RO_ROLE, ...), which is satisfied by a
            # grant made directly TO the role as well as by one to PUBLIC, so
            # a "FROM PUBLIC"-only fix leaves the direct grant in place and
            # verify never converges. Revoking a grant that was never made is
            # a no-op, so covering both is safe.
            add_blocking "可执行逃逸函数 ${_sig}" "REVOKE EXECUTE ON FUNCTION ${_sig} FROM PUBLIC, ${_Q_ROLE};"
            ;;
        fdw_server:*)
            _srv="${_hit#fdw_server:}"
            add_blocking "可用 fdw server ${_srv}" "REVOKE USAGE ON FOREIGN SERVER $(sql_ident "${_srv}") FROM PUBLIC, ${_Q_ROLE};"
            ;;
    esac
done <<< "${_escape_hits}"

# =============================================================================
# Face 6: out-of-scope SECURITY DEFINER (fail-closed) / in-scope (report-only)
# =============================================================================
_secdef="$(printf '%s' \
    "SELECT p.oid::regprocedure::text, n.nspname
     FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE p.prosecdef
       AND (has_function_privilege('public', p.oid, 'EXECUTE') OR has_function_privilege(:'ro_role', p.oid, 'EXECUTE'))" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}")"
while IFS='|' read -r _fn _ns; do
    [[ -z "${_fn}" ]] && continue
    if in_scope "${_ns}"; then
        add_report "范围内 SECURITY DEFINER 函数对 PUBLIC/角色开放 EXECUTE（仅报告，不阻断）：${_fn}"
    else
        add_blocking "范围外 SECURITY DEFINER 函数 ${_fn} 对 PUBLIC/角色开放 EXECUTE" "REVOKE EXECUTE ON FUNCTION ${_fn} FROM PUBLIC, ${_Q_ROLE};"
    fi
done <<< "${_secdef}"

# =============================================================================
# Additional (a): pg_default_acl drift favoring RO_ROLE/PUBLIC
# out-of-scope (incl. database-level, defaclnamespace=0) -> fail-closed
# in-scope -> report-only
# =============================================================================
_defacl="$(printf '%s' \
    "SELECT COALESCE(n.nspname, '<database-level>'), pg_get_userbyid(a.defaclrole), a.defaclobjtype
     FROM pg_default_acl a
     LEFT JOIN pg_namespace n ON n.oid = a.defaclnamespace
     WHERE EXISTS (
         SELECT 1 FROM aclexplode(a.defaclacl) x
         WHERE x.grantee = 0
            OR x.grantee = (SELECT oid FROM pg_roles WHERE rolname = :'ro_role')
     )" \
    | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}")"
while IFS='|' read -r _ns _owner _objtype; do
    [[ -z "${_ns}" ]] && continue
    _desc="owner ${_owner} 在 ${_ns}（objtype=${_objtype}）为 RO_ROLE/PUBLIC 登记了默认权限"
    if [[ "${_ns}" == "<database-level>" ]]; then
        add_blocking "${_desc}（数据库级，超出任何 schema 范围）" "-- 请人工核对并执行：ALTER DEFAULT PRIVILEGES FOR ROLE $(sql_ident "${_owner}") REVOKE ALL ON <objtype> FROM ${_Q_ROLE}, PUBLIC;"
    elif in_scope "${_ns}"; then
        add_report "范围内${_desc}（仅报告，不阻断）"
    else
        add_blocking "${_desc}" "ALTER DEFAULT PRIVILEGES FOR ROLE $(sql_ident "${_owner}") IN SCHEMA $(sql_ident "${_ns}") REVOKE ALL ON TABLES FROM ${_Q_ROLE}, PUBLIC;"
    fi
done <<< "${_defacl}"

# =============================================================================
# Additional (b): cluster CONNECT on a non-target database — always report-only
# (PG's own default grants PUBLIC CONNECT on every database, so fail-closed
# here would be a near-universal false positive — design.md Risks/附加检查项)
# =============================================================================
_cluster_connect="$(printf '%s' \
    "SELECT datname FROM pg_database
     WHERE datname <> current_database()
       AND has_database_privilege(:'ro_role', datname, 'CONNECT')" \
    | ro_audit_sql -At -v ro_role="${RO_ROLE}")"
while IFS= read -r _db; do
    [[ -z "${_db}" ]] && continue
    add_report "角色对非目标库 ${_db} 仍可 CONNECT（仅报告；如需收紧: REVOKE CONNECT ON DATABASE $(sql_ident "${_db}") FROM ${_Q_ROLE};）"
done <<< "${_cluster_connect}"

# =============================================================================
# Additional (c): in-scope table owner without a matching default-ACL
# registrant for RO_ROLE — always report-only (future tables from that owner
# will not automatically be readable without a rerun)
# =============================================================================
if [[ ${#SCOPE_SCHEMAS[@]} -gt 0 ]]; then
    _owner_gap="$(printf '%s' \
        "WITH registrants AS (
             SELECT DISTINCT a.defaclnamespace AS nsp, a.defaclrole AS role
             FROM pg_default_acl a
             WHERE a.defaclobjtype = 'r'
               AND EXISTS (SELECT 1 FROM aclexplode(a.defaclacl) x WHERE x.grantee = (SELECT oid FROM pg_roles WHERE rolname = :'ro_role'))
         )
         SELECT DISTINCT n.nspname, pg_get_userbyid(c.relowner)
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = ANY (string_to_array(:'scope_csv', ','))
           AND c.relkind IN ('r', 'p', 'f', 'v', 'm')
           AND NOT EXISTS (SELECT 1 FROM registrants r WHERE r.nsp = c.relnamespace AND r.role = c.relowner)" \
        | ro_audit_sql -At -F'|' -v ro_role="${RO_ROLE}" -v scope_csv="${SCOPE_CSV}")"
    while IFS='|' read -r _ns _owner; do
        [[ -z "${_ns}" ]] && continue
        # "或重跑供给" was wrong [impl-review-fix]: the generated setup.sql only
        # ever issues `ALTER DEFAULT PRIVILEGES FOR ROLE current_user`, i.e. it
        # registers the executing DBA's own future objects. Re-running it can
        # never close a gap belonging to a DIFFERENT owner -- that owner (or a
        # role that is a member of it) has to run the statement itself.
        add_report "范围内 schema ${_ns} 存在 owner ${_owner} 的表且无匹配的默认权限登记——其后新建的表不会自动可读（仅报告；重跑供给脚本无法修复，须由该 owner 本人或其成员执行: ALTER DEFAULT PRIVILEGES FOR ROLE $(sql_ident "${_owner}") IN SCHEMA $(sql_ident "${_ns}") GRANT SELECT ON TABLES TO ${_Q_ROLE};）"
    done <<< "${_owner_gap}"
fi

# =============================================================================
# Verdict
# =============================================================================
NEEDS_HUMAN_FILE="${ROOT_DIR}/.dbmeta/db-readonly/needs-human.md"

if [[ -n "${_BLOCKING_FINDINGS}" ]]; then
    mkdir -p "${ROOT_DIR}/.dbmeta/db-readonly"
    # mktemp+mv, not `> file` [impl-review-fix] -- see write_needs_human_and_exit2.
    _NH_TMP="$(mktemp "${ROOT_DIR}/.dbmeta/db-readonly/.needs-human.XXXXXX")"
    {
        printf '# 只读凭据审计：命中 fail-closed 项\n\n'
        printf '## problem\n\n只读角色 %s 的有效访问审计命中以下项，通道 MUST NOT 视为可用：\n\n%s\n' "${RO_ROLE}" "${_BLOCKING_FINDINGS}"
        printf '## cause\n\n六面有效访问审计（has_*_privilege 族 + pg_roles/pg_auth_members/pg_default_acl）发现该角色当前持有超出只读预期的访问面。\n\n'
        printf '## fix\n\n以有权限账号（DBA）执行以下 SQL，然后重跑 verify：\n\n```sql\n%s```\n' "${_BLOCKING_FIXES}"
    } > "${_NH_TMP}"
    mv "${_NH_TMP}" "${NEEDS_HUMAN_FILE}"
    fail "problem: 只读角色 ${RO_ROLE} 的有效访问审计命中 fail-closed 项"
    fail "cause: 详见 ${NEEDS_HUMAN_FILE}"
    fail "fix: 由 DBA 执行 ${NEEDS_HUMAN_FILE} 中的 SQL 后重跑本脚本"
    printf '%s\n' "${NEEDS_HUMAN_FILE}"
    exit 2
fi

pass "六面审计通过（含附加检查项）"

# --- Full-link probe, delegated to ro-session.sh (REQ-RP-3) ----------------
# RO_SESSION_SH_OVERRIDE is the same testability seam readonly-setup.sh
# already establishes for this exact delegation — lets a test substitute a
# stub without needing a full multi-query mock psql for the probe path.
RO_SESSION_SH="${RO_SESSION_SH_OVERRIDE:-${SCRIPT_DIR}/ro-session.sh}"
set +e
"${RO_SESSION_SH}" --sql 'SELECT 1' >/dev/null 2>&1
PROBE_RC=$?
set -e

if [[ ${PROBE_RC} -ne 0 ]]; then
    FRAGMENT_FILE="${ROOT_DIR}/.dbmeta/db-readonly/userlist-fragment.txt"
    fail "problem: 只读会话全链路探针（经 ro-session.sh 的 SELECT 1）失败"
    fail "cause: 角色可能已建但当前连接方式尚未认可该账号（PgBouncer 认证模式相关），或凭据/网络异常"
    fail "fix: ① 先在 PgBouncer 主机执行: grep -E '^(auth_type|auth_file|auth_query)' <pgbouncer.ini>"
    if [[ -f "${FRAGMENT_FILE}" ]]; then
        fail "fix: ② 仅含 auth_query=（不含 auth_file=）⇒ userlist-fragment.txt 不适用，按 .dbmeta/db-readonly/needs-human.md（若存在）或重跑 /pg-readonly-setup 的「认证被拒」指引排查"
        fail "fix: ③ 仅 auth_file= ⇒ 把 ${FRAGMENT_FILE}（0600，首行为 ; 注释、第二行为 userlist.txt 一行）追加进 userlist.txt 并 RELOAD，粘贴后删除该文件，随后重跑本脚本"
    else
        fail "fix: ② 仅含 auth_query=（不含 auth_file=）⇒ 按认证被拒指引排查"
        fail "fix: ③ 仅 auth_file= ⇒ 先运行 shared/ro-generate.sh 产出 ${FRAGMENT_FILE} 再回到本步"
    fi
    fail "fix: ④ 两者都命中（混合配置：auth_file 对在册用户优先，角色若在 userlist.txt 有旧条目仍需更新 fragment 并 RELOAD）或都不命中（auth_type=trust/hba、键在 include 文件里、或未经 PgBouncer 直连）⇒ 先向 DBA 确认生效的认证后端（该角色走静态文件还是动态查询）再选路径，MUST NOT 仅凭 auth_query= 在场就判 fragment 不适用"
    printf '%s\n' "${FRAGMENT_FILE}"
    exit 1
fi

pass "只读通道可用"
exit 0
