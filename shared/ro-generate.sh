#!/bin/bash
# Pure-text generator for the read-only role's provisioning script.
#
# Input = config values (DB_USER/SCHEMAS/password from .dbllm.env).
# Output = SQL text (.dbmeta/db-readonly/setup.sql) + a credentials-bearing
# PgBouncer userlist fragment + password written back to .dbllm.env
# (replacing CHANGE_ME placeholder). Zero database connections, zero side
# effects beyond writing these files.
#
# The generated setup.sql is handed to a human DBA to execute by hand — this
# script never runs CREATE ROLE/GRANT/REVOKE itself, and never holds or parses
# admin credentials (ADR-0006).
#
# Usage:
#   shared/ro-generate.sh [-h|--help] [--allow-unignored]
#
# Exit codes:
#   0  success -- setup.sql / userlist-fragment.txt written, password resolved
#   1  fail-loud: bad config, illegal identifier, unsafe password, files not
#      git-ignored (without --allow-unignored) -- .dbmeta/db-readonly/ gets NO
#      new files in any of these cases
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
Usage: shared/ro-generate.sh [-h|--help] [--allow-unignored]

Zero-DB-connection text generator: produces .dbmeta/db-readonly/setup.sql (0600,
a self-contained `DO $$` block a DBA runs by hand), .dbmeta/db-readonly/
userlist-fragment.txt (0600), and writes the generated password back into
.dbmeta/.dbllm.env (replacing CHANGE_ME). Makes no database connections.

Options:
  --allow-unignored
      Skip the git-ignore guard for files that carry the plaintext password
      (for consuming projects that are not a git repo).
USAGE
}

ALLOW_UNIGNORED=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --allow-unignored) ALLOW_UNIGNORED=1; shift ;;
        *) fail "problem: 未知参数 $1"; usage >&2; exit 1 ;;
    esac
done

CONFIG_LIB="${SCRIPT_DIR}/config.sh"
if [[ ! -f "${CONFIG_LIB}" ]]; then
    fail "problem: 找不到 shared/config.sh（${CONFIG_LIB}）"
    fail "cause: 本地 db-llm 安装不完整（config.sh 与 ro-generate.sh 应同在 shared/ 下）"
    fail "fix: 重新跑一次 db-llm 仓的 setup.sh，或核对该仓是否完整 clone"
    exit 1
fi
# shellcheck source=/dev/null
source "${CONFIG_LIB}"

CONFIG_FILE="${ROOT_DIR}/.dbmeta/.dbllm.env"
db_llm_load_config "${CONFIG_FILE}" || exit 1

RO_ROLE="${DBS_DB_USER}"
OUT_DIR="${ROOT_DIR}/.dbmeta/db-readonly"
SETUP_SQL_FILE="${OUT_DIR}/setup.sql"
FRAGMENT_FILE="${OUT_DIR}/userlist-fragment.txt"

# --- Identifier validation (fail-loud, MUST NOT produce a script) -----------
IDENT_RE='^[A-Za-z0-9_]+$'

validate_identifier() {
    local value="$1" label="$2"
    if [[ ! "${value}" =~ ${IDENT_RE} ]]; then
        db_llm_config_fail \
            "${label}=${value} 不是合法标识符" \
            "生成器要求 DB_USER/SCHEMAS 每一项匹配 ${IDENT_RE}，避免非法标识符产出含注入风险的 DDL" \
            "把 ${label} 改成只含字母、数字、下划线的值后重跑"
        return 1
    fi
    return 0
}

validate_identifier "${RO_ROLE}" "DB_USER" || exit 1

SCHEMA_ARR=()
if [[ -n "${DBS_SCHEMAS}" ]]; then
    IFS=',' read -ra SCHEMA_ARR <<< "${DBS_SCHEMAS}"
    for _s in "${SCHEMA_ARR[@]}"; do
        validate_identifier "${_s}" "SCHEMAS 项 '${_s}'" || exit 1
    done
fi

# --- SQL literal helpers ----------------------------------------------------
sql_literal_escape() {
    printf '%s' "${1//\'/\'\'}"
}

# --- git-ignore guard -------------------------------------------------------
is_git_ignored() {
    command -v git >/dev/null 2>&1 || return 1
    git -C "${ROOT_DIR}" check-ignore -q -- "$1" 2>/dev/null
}

CONFIG_RELPATH="${CONFIG_FILE#"${ROOT_DIR}"/}"

# Config file carries credentials — must be git-ignored.
if [[ "${ALLOW_UNIGNORED}" -ne 1 ]] && ! is_git_ignored "${CONFIG_RELPATH}"; then
    db_llm_config_fail \
        "配置文件（${CONFIG_FILE}）未被 git-ignore" \
        "该文件含数据库凭据（DB_PASSWORD），未被 .gitignore 排除时有密码入库风险" \
        "先在 .gitignore 里加入 ${CONFIG_RELPATH} 后重跑；非 git 消费仓可传 --allow-unignored 豁免"
    exit 1
fi

# setup.sql and userlist-fragment.txt also embed the password.
for _relpath in "${SETUP_SQL_FILE#"${ROOT_DIR}"/}" "${FRAGMENT_FILE#"${ROOT_DIR}"/}"; do
    if [[ "${ALLOW_UNIGNORED}" -ne 1 ]] && ! is_git_ignored "${_relpath}"; then
        db_llm_config_fail \
            "即将生成的 ${_relpath} 未被 git-ignore" \
            "该文件含明文密码，未被 .gitignore 排除时有密码入库风险" \
            "先在 .gitignore 里加入能匹配 ${_relpath} 的规则（通常一行 .dbmeta/db-readonly/ 即可）后重跑；非 git 消费仓可传 --allow-unignored 豁免"
        exit 1
    fi
done

# --- Password source-of-truth resolution -----------------------------------
generate_password() {
    local pw
    set +o pipefail
    pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)"
    set -o pipefail
    printf '%s' "${pw}"
}

password_is_safe() {
    local pw="$1"
    case "${pw}" in
        *"'"*) return 1 ;;
        *'"'*) return 1 ;;
        *'\'*) return 1 ;;
        *'$'*) return 1 ;;
    esac
    [[ "${pw}" == *$'\n'* ]] && return 1
    return 0
}

PASSWORD_MODE=""   # REPLACE | REUSE
FINAL_PASSWORD=""

if [[ -z "${DBS_DB_PASSWORD:-}" ]]; then
    db_llm_config_fail \
        ".dbllm.env 的 DB_PASSWORD 字段为空" \
        "密码字段既不是占位符 CHANGE_ME 也没有可复用的值" \
        "把 DB_PASSWORD 设为 CHANGE_ME（让生成步生成新密码）或填入真实密码后重跑"
    exit 1
elif [[ "${DBS_DB_PASSWORD}" == "CHANGE_ME" ]]; then
    PASSWORD_MODE="REPLACE"
    FINAL_PASSWORD="$(generate_password)"
else
    PASSWORD_MODE="REUSE"
    FINAL_PASSWORD="${DBS_DB_PASSWORD}"
    if ! password_is_safe "${FINAL_PASSWORD}"; then
        db_llm_config_fail \
            ".dbllm.env 中既存密码含无法在 SQL/shell/PgBouncer 三种载体中安全表示的字符（引号/反斜杠/\$/换行）" \
            "静默把这类字符嵌入生成的 setup.sql 会产出语法错误或被错误解析的文件" \
            "把 DB_PASSWORD 换成只含普通字符的密码（或设为 CHANGE_ME 让生成步随机生成一个安全密码）后重跑；若该角色被其它消费仓共享，轮换须与之协调（各仓 .dbllm.env 同步更新）"
        exit 1
    fi
fi

# --- Write password back to config file (REPLACE only) ---------------------
if [[ "${PASSWORD_MODE}" == "REPLACE" ]]; then
    _tmp_config="$(mktemp)"
    if ! DBS_RO_GENERATE_NEWVAL="${FINAL_PASSWORD}" awk -v key="DB_PASSWORD" '
        BEGIN { done = 0; prefix = key "="; newval = ENVIRON["DBS_RO_GENERATE_NEWVAL"] }
        {
            if (!done && index($0, prefix) == 1) {
                print key "=" newval
                done = 1
            } else {
                print $0
            }
        }
        END { exit !done }
    ' "${CONFIG_FILE}" > "${_tmp_config}"; then
        rm -f "${_tmp_config}"
        db_llm_config_fail \
            ".dbllm.env 里找不到 DB_PASSWORD= 开头的行" \
            "已判定密码字段为占位符 CHANGE_ME，但按行定位替换时没能命中同一行——文件可能在两次读取之间被改动" \
            "核对 .dbmeta/.dbllm.env 是否含 DB_PASSWORD=CHANGE_ME 一行且未被其它进程并发改写后重跑"
        exit 1
    fi
    mv "${_tmp_config}" "${CONFIG_FILE}"
    chmod 600 "${CONFIG_FILE}"
    info "已替换 .dbllm.env 的密码字段"
else
    info ".dbllm.env 已含可用密码，复用不轮换"
fi

# --- Build setup.sql --------------------------------------------------------
RO_ROLE_LITERAL="'$(sql_literal_escape "${RO_ROLE}")'"
PASSWORD_LITERAL="'$(sql_literal_escape "${FINAL_PASSWORD}")'"

if [[ ${#SCHEMA_ARR[@]} -gt 0 ]]; then
    EXPLICIT_SCOPE_LITERAL="true"
    _parts=()
    for _s in "${SCHEMA_ARR[@]}"; do
        _parts+=("'$(sql_literal_escape "${_s}")'")
    done
    EXPLICIT_SCHEMAS_LITERAL="ARRAY[$(IFS=,; echo "${_parts[*]}")]::name[]"
else
    EXPLICIT_SCOPE_LITERAL="false"
    EXPLICIT_SCHEMAS_LITERAL="ARRAY[]::name[]"
fi

SQL_TEMPLATE="$(cat <<'SQL'
\set ON_ERROR_STOP on
-- Generated by shared/ro-generate.sh -- a pure-text generator, zero DB
-- connection at generation time. DO NOT hand-edit: rerun /pg-readonly-setup
-- to regenerate. Execute with an account that holds CREATEROLE (superuser is
-- NOT required). Delete this file after the DBA has run it -- it embeds a
-- plaintext password.
DO $do$
DECLARE
    ro_role           CONSTANT name    := __RO_ROLE_LITERAL__;
    ro_password       CONSTANT text    := __PASSWORD_LITERAL__;
    explicit_scope    CONSTANT boolean := __EXPLICIT_SCOPE_LITERAL__;
    explicit_schemas  CONSTANT name[]  := __EXPLICIT_SCHEMAS_LITERAL__;
    scope_schemas     name[];
    rec               record;
    findings          text[] := ARRAY[]::text[];
BEGIN
    -- ro-generate: resolve in-scope schemas AT EXECUTION TIME --
    IF explicit_scope THEN
        scope_schemas := explicit_schemas;
    ELSE
        SELECT COALESCE(array_agg(n.nspname ORDER BY n.nspname), ARRAY[]::name[])
        INTO scope_schemas
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
          );
    END IF;

    -- ro-generate: pre-execution dangerous-role audit (zero-change abort) --
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ro_role) THEN
        IF EXISTS (
            SELECT 1 FROM pg_roles
            WHERE rolname = ro_role
              AND (rolsuper OR rolcreatedb OR rolbypassrls OR rolreplication)
        ) THEN
            findings := findings || (ro_role || ' 已持有 SUPERUSER/CREATEDB/BYPASSRLS/REPLICATION 之一');
        END IF;

        IF EXISTS (
            SELECT 1 FROM pg_auth_members m
            JOIN pg_roles g ON g.oid = m.member
            WHERE g.rolname = ro_role
        ) THEN
            findings := findings || (ro_role || ' 已是其它角色的成员');
        END IF;

        IF EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg\_%'
              AND has_table_privilege(ro_role, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        ) THEN
            findings := findings || (ro_role || ' 对某张表持有非 SELECT 权限');
        END IF;

        IF EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg\_%'
              AND has_table_privilege('public', c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        ) THEN
            findings := findings || ('PUBLIC 对某张表持有非 SELECT 权限');
        END IF;

        IF array_length(findings, 1) IS NOT NULL THEN
            RAISE EXCEPTION '危险角色审计命中，零变更中止: %', array_to_string(findings, '; ');
        END IF;
    END IF;

    -- ro-generate: CREATE ROLE idempotent (existing role keeps its password) --
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ro_role) THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', ro_role, ro_password);
    ELSE
        RAISE NOTICE '角色 % 已存在，密码未改动。若本仓要复用它，请把已有密码填进 .dbmeta/.dbllm.env 的 DB_PASSWORD；确认本仓独占后，可执行本脚本末尾注释态的 ALTER ROLE 同步密码。', ro_role;
    END IF;

    -- ro-generate: attribute convergence (every run) --
    EXECUTE format('ALTER ROLE %I LOGIN NOCREATEROLE NOINHERIT CONNECTION LIMIT 5', ro_role);
    EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', ro_role, '15s');
    EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', ro_role, '30s');

    -- ro-generate: grant SET on lc_messages (PG 15+; non-privilege-escalation —
    -- only lets the role SET this one GUC, needed by ro-session.sh --prepare
    -- mode which runs `SET LOCAL lc_messages='C'` to get locale-stable
    -- SQLSTATE-parseable error text; lc_messages has GUC context=superuser,
    -- so without this grant --prepare fails with 42501 for any non-superuser
    -- read-only role) --
    EXECUTE format('GRANT SET ON PARAMETER lc_messages TO %I', ro_role);

    -- ro-generate: grant in-scope schemas --
    -- NOTE: ALTER DEFAULT PRIVILEGES FOR ROLE current_user only covers tables
    -- that *the account running this script* creates later. Tables created by
    -- another owner in these schemas stay unreadable for the read-only role
    -- until that owner (or a DBA, FOR ROLE <owner>) runs the same statement.
    FOR rec IN SELECT unnest(scope_schemas) AS nspname LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', rec.nspname, ro_role);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', rec.nspname, ro_role);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE current_user IN SCHEMA %I GRANT SELECT ON TABLES TO %I', rec.nspname, ro_role);
    END LOOP;

    -- ro-generate: revoke out-of-scope schemas (scope-narrowing convergence) --
    FOR rec IN
        SELECT n.nspname
        FROM pg_namespace n
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_%'
          AND has_schema_privilege(ro_role, n.oid, 'USAGE')
          AND NOT (n.nspname = ANY (scope_schemas))
    LOOP
        RAISE NOTICE '回收范围外 schema 的权限: %', rec.nspname;
        EXECUTE format('REVOKE SELECT ON ALL TABLES IN SCHEMA %I FROM %I', rec.nspname, ro_role);
        EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM %I', rec.nspname, ro_role);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE current_user IN SCHEMA %I REVOKE SELECT ON TABLES FROM %I', rec.nspname, ro_role);
    END LOOP;
END
$do$;
-- ro-generate: 仅当上方 NOTICE 提示「角色已存在」且确认该角色为本仓独占时，取消下一行注释后执行（会改掉其它消费方的密码）：
-- ALTER ROLE __RO_ROLE_IDENT__ PASSWORD __PASSWORD_LITERAL__;
SQL
)"

SQL_TEMPLATE="${SQL_TEMPLATE//__RO_ROLE_LITERAL__/${RO_ROLE_LITERAL}}"
SQL_TEMPLATE="${SQL_TEMPLATE//__RO_ROLE_IDENT__/\"${RO_ROLE}\"}"
SQL_TEMPLATE="${SQL_TEMPLATE//__PASSWORD_LITERAL__/${PASSWORD_LITERAL}}"
SQL_TEMPLATE="${SQL_TEMPLATE//__EXPLICIT_SCOPE_LITERAL__/${EXPLICIT_SCOPE_LITERAL}}"
SQL_TEMPLATE="${SQL_TEMPLATE//__EXPLICIT_SCHEMAS_LITERAL__/${EXPLICIT_SCHEMAS_LITERAL}}"

mkdir -p "${OUT_DIR}"

_tmp_setup="$(mktemp "${OUT_DIR}/.setup.sql.XXXXXX")"
printf '%s\n' "${SQL_TEMPLATE}" > "${_tmp_setup}"
chmod 600 "${_tmp_setup}"
mv "${_tmp_setup}" "${SETUP_SQL_FILE}"

# --- Write userlist-fragment.txt (PgBouncer userlist.txt one line) ---------
_tmp_fragment="$(mktemp "${OUT_DIR}/.userlist-fragment.XXXXXX")"
{
    printf '; db-llm: 仅 PgBouncer auth_file 模式需要本文件（pgbouncer.ini 含 auth_file= 且无 auth_query=）；auth_query 模式直接忽略/删除。\n'
    printf '"%s" "%s"\n' "${RO_ROLE}" "${FINAL_PASSWORD}"
} > "${_tmp_fragment}"
chmod 600 "${_tmp_fragment}"
mv "${_tmp_fragment}" "${FRAGMENT_FILE}"

# --- Cross-file consistency re-read ----------------------------------------
# Re-read the password from the config file after writing all artifacts. If it
# differs from the password used to generate them, two generate runs raced.
_verify_rc=0
# Re-parse the config to get the current DB_PASSWORD value.
_password_now=""
while IFS= read -r _line || [[ -n "${_line}" ]]; do
    _line="${_line#"${_line%%[![:space:]]*}"}"
    [[ -z "${_line}" ]] && continue
    [[ "${_line}" == '#'* ]] && continue
    [[ "${_line}" != *"="* ]] && continue
    _k="${_line%%=*}"
    _k="${_k%"${_k##*[![:space:]]}"}"
    if [[ "${_k}" == "DB_PASSWORD" ]]; then
        _password_now="${_line#*=}"
        _password_now="${_password_now#"${_password_now%%[![:space:]]*}"}"
        _password_now="${_password_now%"${_password_now##*[![:space:]]}"}"
    fi
done < "${CONFIG_FILE}"

if [[ "${_password_now}" != "${FINAL_PASSWORD}" ]]; then
    db_llm_config_fail \
        ".dbllm.env 的密码字段与本次刚生成的产物不一致" \
        "本次运行读到/写下的密码与文件当前内容不同——几乎总是因为另一个 ro-generate 进程并发跑了同一个消费仓" \
        "确认没有其它 /pg-readonly-setup 或 ro-generate 在同时运行，然后重跑一次"
    exit 1
fi

pass "已生成 ${SETUP_SQL_FILE}（0600）"
pass "已生成 ${FRAGMENT_FILE}（0600）"
info "下一步：请 DBA 以持 CREATEROLE 的账号执行 psql -f ${SETUP_SQL_FILE}，完成后删除该文件"
exit 0
