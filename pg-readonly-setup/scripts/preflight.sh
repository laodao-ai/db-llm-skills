#!/bin/bash
# Zero-DB, zero-network static preflight for the pg-readonly-setup onboarding
# wizard. Produces a single JSON object on stdout describing every judgeable
# dimension of "is this consuming project ready to onboard", a statically-
# derived resume `stage`, and a `blockers` list.
#
# Exit codes:
#   0  preflight itself completed (the verdict lives in the JSON)
#   1  preflight itself could not complete (unexpected internal error)
#
# JSON shape:
#   {"checks": {<check_id>: {"status": "ok|fail|skip", "detail": "..."}},
#    "stage": "<resume-stage>", "blockers": [<check_id>, ...]}
#
# JSON assembly MUST NOT depend on python3 — python3 is one of the things
# this script itself checks for.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")}}"
export ROOT_DIR

CONFIG_LIB="${SCRIPT_DIR}/../../shared/config.sh"
CONFIG_TEMPLATE="${DBLLM_TEMPLATE_OVERRIDE:-${SCRIPT_DIR}/../../shared/.dbllm.env.example}"
CONFIG_FILE="${ROOT_DIR}/.dbmeta/.dbllm.env"
EXAMPLE_FILE="${ROOT_DIR}/.dbmeta/.dbllm.env.example"

# --- ordered check accumulation ---------------------------------------------
CHECK_IDS=()
CHECK_STATUS=()
CHECK_DETAIL=()
BLOCKERS=()

add_check() {
    local id="$1" status="$2" detail="$3" blocking="${4:-no}"
    CHECK_IDS+=("${id}")
    CHECK_STATUS+=("${status}")
    CHECK_DETAIL+=("${detail}")
    if [[ "${status}" == "fail" && "${blocking}" == "yes" ]]; then
        BLOCKERS+=("${id}")
    fi
}

check_status() {
    local want="$1" i
    for i in "${!CHECK_IDS[@]}"; do
        if [[ "${CHECK_IDS[$i]}" == "${want}" ]]; then
            printf '%s' "${CHECK_STATUS[$i]}"
            return 0
        fi
    done
    printf '%s' ""
}

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\t'/\\t}"
    printf '%s' "${s}"
}

# field_state value placeholder
# Returns absent|placeholder|set.
field_state() {
    local val="$1" placeholder="$2"
    if [[ -z "${val}" ]]; then
        printf 'absent'
    elif [[ "${val}" == "${placeholder}" ]]; then
        printf 'placeholder'
    else
        printf 'set'
    fi
}

# =============================================================================
# 1. Dependency + engine completeness checks
# =============================================================================

if command -v psql >/dev/null 2>&1; then
    add_check "dep_psql" "ok" "psql 在 PATH 中"
else
    add_check "dep_psql" "fail" "psql 未安装或不在 PATH 中——按平台安装（如 brew install libpq）" "yes"
fi

if command -v python3 >/dev/null 2>&1; then
    add_check "dep_python3" "ok" "python3 在 PATH 中"
else
    add_check "dep_python3" "fail" "python3 未安装或不在 PATH 中" "yes"
fi

SHARED_SCRIPTS=(
    "${SCRIPT_DIR}/../../shared/config.sh"
    "${SCRIPT_DIR}/../../shared/db-collect.sh"
    "${SCRIPT_DIR}/../../shared/db-collect.sql"
    "${SCRIPT_DIR}/../../shared/ro-generate.sh"
    "${SCRIPT_DIR}/../../shared/ro-verify.sh"
    "${SCRIPT_DIR}/../../shared/ro-session.sh"
    "${SCRIPT_DIR}/../../shared/ro_guard.py"
)
_missing_shared=()
for _f in "${SHARED_SCRIPTS[@]}"; do
    [[ -f "${_f}" ]] || _missing_shared+=("$(basename "${_f}")")
done
if [[ ${#_missing_shared[@]} -eq 0 ]]; then
    add_check "engine_shared_scripts" "ok" "shared/ 引擎脚本齐全"
else
    add_check "engine_shared_scripts" "fail" "shared/ 缺失: $(IFS=,; echo "${_missing_shared[*]}")——本地 db-llm 安装不完整，重跑该仓 setup.sh" "yes"
fi

SKILL_ENTRIES=(
    "${SCRIPT_DIR}/../../pg-dict/scripts/pg-dict.sh"
    "${SCRIPT_DIR}/readonly-setup.sh"
    "${SCRIPT_DIR}/../../pg-query-ro/scripts/pg-query-ro.sh"
)
_missing_entries=()
for _f in "${SKILL_ENTRIES[@]}"; do
    [[ -f "${_f}" ]] || _missing_entries+=("$(basename "${_f}")")
done
if [[ ${#_missing_entries[@]} -eq 0 ]]; then
    add_check "engine_skill_entries" "ok" "pg-dict / readonly-setup / pg-query-ro 三个入口脚本齐全"
else
    add_check "engine_skill_entries" "fail" "缺失入口脚本: $(IFS=,; echo "${_missing_entries[*]}")——本地 db-llm 安装不完整" "yes"
fi

if [[ -f "${CONFIG_TEMPLATE}" ]]; then
    add_check "engine_template" "ok" "shared/.dbllm.env.example 存在"
else
    add_check "engine_template" "fail" "找不到 shared/.dbllm.env.example（${CONFIG_TEMPLATE}）——本地 db-llm 安装不完整" "yes"
fi

CONFIG_LIB_OK=0
if [[ -f "${CONFIG_LIB}" ]]; then
    # shellcheck source=/dev/null
    if source "${CONFIG_LIB}"; then
        CONFIG_LIB_OK=1
    fi
fi

# =============================================================================
# 2. git repo + .gitignore coverage
# =============================================================================

GIT_REPO_OK=0
if git -C "${ROOT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_REPO_OK=1
    add_check "git_repo" "ok" "${ROOT_DIR} 是 git 仓"
else
    add_check "git_repo" "fail" "${ROOT_DIR} 不是 git 仓——向导需要 git 仓做 gitignore 守卫；非 git 消费仓走 README 手工路径" "yes"
fi

is_git_ignored() {
    git -C "${ROOT_DIR}" check-ignore -q -- "$1" 2>/dev/null
}

if [[ ${GIT_REPO_OK} -eq 1 ]]; then
    if is_git_ignored ".dbmeta/db-readonly/setup.sql"; then
        add_check "gitignore_setup_sql" "ok" ".dbmeta/db-readonly/setup.sql 已被 .gitignore 覆盖"
    else
        add_check "gitignore_setup_sql" "fail" ".dbmeta/db-readonly/setup.sql 未被 .gitignore 覆盖——该文件含明文密码（在 .gitignore 加一行 .dbmeta/db-readonly/）" "yes"
    fi

    if is_git_ignored ".dbmeta/db-readonly/userlist-fragment.txt"; then
        add_check "gitignore_userlist_fragment" "ok" ".dbmeta/db-readonly/userlist-fragment.txt 已被 .gitignore 覆盖"
    else
        add_check "gitignore_userlist_fragment" "fail" ".dbmeta/db-readonly/userlist-fragment.txt 未被 .gitignore 覆盖——该文件含明文密码" "yes"
    fi

    # Config file contains credentials — MUST be git-ignored.
    if is_git_ignored ".dbmeta/.dbllm.env"; then
        add_check "gitignore_config" "ok" ".dbmeta/.dbllm.env 已被 .gitignore 覆盖"
    else
        add_check "gitignore_config" "fail" ".dbmeta/.dbllm.env 未被 .gitignore 覆盖——该文件含数据库凭据" "yes"
    fi
else
    add_check "gitignore_setup_sql" "skip" "非 git 仓，跳过 gitignore 判定"
    add_check "gitignore_userlist_fragment" "skip" "非 git 仓，跳过 gitignore 判定"
    add_check "gitignore_config" "skip" "非 git 仓，跳过 gitignore 判定"
fi

# =============================================================================
# 3. Config file + field states
# =============================================================================

CONFIG_LOAD_RC=1
_st_host="" _st_port="" _st_db="" _st_user="" _st_pw=""
_st_ssh_host="" _st_ssh_remote="" _st_ssh_auth=""

if [[ -f "${CONFIG_FILE}" ]]; then
    add_check "config_file" "ok" "${CONFIG_FILE} 已存在"

    if [[ ${CONFIG_LIB_OK} -eq 1 ]]; then
        _load_err_file="$(mktemp)"
        if db_llm_load_config "${CONFIG_FILE}" 2>"${_load_err_file}"; then
            CONFIG_LOAD_RC=0
        else
            CONFIG_LOAD_RC=$?
        fi
        rm -f "${_load_err_file}"

        if [[ ${CONFIG_LOAD_RC} -eq 0 ]]; then
            _st_host="$(field_state "${DBS_DB_HOST:-}" "CHANGE_ME")"
            _st_port="$(field_state "${DBS_DB_PORT:-}" "CHANGE_ME")"
            _st_db="$(field_state "${DBS_DB_NAME:-}" "CHANGE_ME")"
            _st_user="$(field_state "${DBS_DB_USER:-}" "CHANGE_ME")"
            _st_pw="$(field_state "${DBS_DB_PASSWORD:-}" "CHANGE_ME")"
            add_check "config_fields" "ok" "host=${_st_host},port=${_st_port},db=${_st_db},user=${_st_user},password=${_st_pw}"

            # -- SSH tunnel fields (REQ-IN-7): computed only after a successful
            # load_config, since they read the DBS_SSH_* globals it sets.
            _st_ssh_host="$(field_state "${DBS_SSH_HOST:-}" "CHANGE_ME")"
            _st_ssh_remote="$(field_state "${DBS_SSH_REMOTE_HOST:-}" "CHANGE_ME")"
            if [[ -n "${DBS_SSH_KEY_FILE:-}" ]]; then
                _st_ssh_auth="key"
            elif [[ -n "${DBS_SSH_PASSWORD:-}" ]]; then
                _st_ssh_auth="password"
            else
                _st_ssh_auth="absent"
            fi

            if [[ "${_st_ssh_host}" != "set" ]]; then
                add_check "ssh_fields" "skip" "SSH_HOST 未配置——隧道模式未启用"
                add_check "dep_sshpass" "skip" "隧道模式未启用，跳过 sshpass 判定"
            else
                # 这里曾判 host_port_consistent（DB_HOST 是回环且 DB_PORT ==
                # SSH_LOCAL_PORT），与 config.sh 的两条跨字段校验成对。T74 之后
                # 隧道模式不再读这两个字段（由隧道推导），预检若还拦就会出现
                # 「preflight 报 blocker、config.sh 却已放行」的两层不一致。
                _ssh_detail="ssh_remote=${_st_ssh_remote},ssh_auth=${_st_ssh_auth}"

                if [[ "${_st_ssh_auth}" == "absent" || "${_st_ssh_remote}" != "set" ]]; then
                    add_check "ssh_fields" "fail" "${_ssh_detail}" "yes"
                else
                    add_check "ssh_fields" "ok" "${_ssh_detail}"
                fi

                if [[ "${_st_ssh_auth}" == "password" ]]; then
                    if command -v sshpass >/dev/null 2>&1; then
                        add_check "dep_sshpass" "ok" "sshpass 在 PATH 中"
                    else
                        add_check "dep_sshpass" "fail" "SSH_PASSWORD 已设但 sshpass 未安装或不在 PATH 中——macOS: brew install sshpass；Debian/Ubuntu: sudo apt install sshpass；或改用 SSH_KEY_FILE" "yes"
                    fi
                else
                    add_check "dep_sshpass" "skip" "非密码认证模式，跳过 sshpass 判定"
                fi
            fi
        else
            add_check "config_fields" "fail" "配置文件解析失败"
            add_check "ssh_fields" "skip" "配置文件解析失败，跳过隧道字段判定"
            add_check "dep_sshpass" "skip" "配置文件解析失败，跳过 sshpass 判定"
        fi
    else
        add_check "config_fields" "skip" "shared/config.sh 不可用，跳过解析"
        add_check "ssh_fields" "skip" "shared/config.sh 不可用，跳过隧道字段判定"
        add_check "dep_sshpass" "skip" "shared/config.sh 不可用，跳过 sshpass 判定"
    fi
elif [[ -f "${EXAMPLE_FILE}" ]]; then
    add_check "config_file" "skip" "${CONFIG_FILE} 尚未创建（.example 模板已就位）"
    add_check "config_fields" "skip" "config 尚未创建"
    add_check "ssh_fields" "skip" "config 尚未创建"
    add_check "dep_sshpass" "skip" "config 尚未创建"
else
    add_check "config_file" "skip" "${CONFIG_FILE} 与 .example 均不存在，等待初始化"
    add_check "config_fields" "skip" "config 尚未创建"
    add_check "ssh_fields" "skip" "config 尚未创建"
    add_check "dep_sshpass" "skip" "config 尚未创建"
fi

# =============================================================================
# 4. .dbmeta/ dictionary tree + setup.sql in-flight
# =============================================================================

_dbmeta_dirs=0
if [[ -d "${ROOT_DIR}/.dbmeta" ]]; then
    _dbmeta_dirs="$(find "${ROOT_DIR}/.dbmeta" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
fi
if [[ "${_dbmeta_dirs}" -gt 0 ]]; then
    add_check "dbmeta_tree" "ok" ".dbmeta/ 下已有 ${_dbmeta_dirs} 个 schema 目录"
else
    add_check "dbmeta_tree" "skip" ".dbmeta/ 尚无字典树"
fi

if [[ -f "${ROOT_DIR}/.dbmeta/db-readonly/setup.sql" ]]; then
    if [[ -f "${CONFIG_FILE}" && "${ROOT_DIR}/.dbmeta/db-readonly/setup.sql" -ot "${CONFIG_FILE}" ]]; then
        # Advisory only — mtime is unreliable across git checkout/cp/container
        # mounts and MUST NOT participate in any stage judgment (status stays
        # "ok" either way; only the human-facing detail text changes).
        add_check "setup_sql_inflight" "ok" ".dbmeta/db-readonly/setup.sql 在途，但早于当前配置——重跑供给会重新生成"
    else
        add_check "setup_sql_inflight" "ok" ".dbmeta/db-readonly/setup.sql 在途——供给已生成，等待 DBA 执行"
    fi
else
    add_check "setup_sql_inflight" "skip" "setup.sql 不存在"
fi

# =============================================================================
# 5. Stage derivation
#    deps -> gitignore -> config -> provision -> collect -> smoke -> done-unknown
# =============================================================================

STAGE="done-unknown"

# DB_HOST/DB_PORT 只在直连模式下是必填项。隧道模式下它们由隧道推导（T74），
# 配置里不写正是目标态——若仍要求它们 set，这类配置会永远卡在 config 阶段。
_addr_fields_ok=1
if [[ "${_st_ssh_host:-}" != "set" ]]; then
    [[ "${_st_host:-}" == "set" && "${_st_port:-}" == "set" ]] || _addr_fields_ok=0
fi

if [[ "$(check_status dep_psql)" == "fail" || "$(check_status dep_python3)" == "fail" \
    || "$(check_status engine_shared_scripts)" == "fail" \
    || "$(check_status engine_skill_entries)" == "fail" \
    || "$(check_status engine_template)" == "fail" \
    || "$(check_status dep_sshpass)" == "fail" ]]; then
    STAGE="deps"
elif [[ "$(check_status git_repo)" == "fail" \
    || "$(check_status gitignore_setup_sql)" == "fail" \
    || "$(check_status gitignore_userlist_fragment)" == "fail" \
    || "$(check_status gitignore_config)" == "fail" ]]; then
    STAGE="gitignore"
elif [[ "$(check_status config_file)" != "ok" \
    || "$(check_status config_fields)" != "ok" \
    || ${_addr_fields_ok} -eq 0 \
    || "${_st_db:-}" != "set" \
    || "${_st_pw:-}" == "absent" \
    || "$(check_status ssh_fields)" == "fail" ]]; then
    STAGE="config"
elif [[ "$(check_status setup_sql_inflight)" == "ok" || "${_st_pw:-}" == "placeholder" ]]; then
    # password=placeholder means connection fields are fully filled in but the
    # channel itself hasn't been provisioned yet ⇒ provision, not config/collect.
    STAGE="provision"
elif [[ "$(check_status dbmeta_tree)" != "ok" ]]; then
    STAGE="collect"
else
    STAGE="smoke"
fi

# =============================================================================
# 6. JSON assembly (pure bash/printf, no python3)
# =============================================================================

checks_json="{"
for i in "${!CHECK_IDS[@]}"; do
    [[ ${i} -gt 0 ]] && checks_json+=","
    checks_json+="\"$(json_escape "${CHECK_IDS[$i]}")\":{\"status\":\"$(json_escape "${CHECK_STATUS[$i]}")\",\"detail\":\"$(json_escape "${CHECK_DETAIL[$i]}")\"}"
done
checks_json+="}"

blockers_json="["
for i in "${!BLOCKERS[@]}"; do
    [[ ${i} -gt 0 ]] && blockers_json+=","
    blockers_json+="\"$(json_escape "${BLOCKERS[$i]}")\""
done
blockers_json+="]"

printf '{"checks":%s,"stage":"%s","blockers":%s}\n' "${checks_json}" "$(json_escape "${STAGE}")" "${blockers_json}"
exit 0
