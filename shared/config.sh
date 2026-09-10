#!/bin/bash
# Shared `.dbllm.env` config loader, sourced by pg-dict/scripts/pg-dict.sh,
# shared/db-collect.sh, shared/ro-session.sh, shared/ro-verify.sh, and
# shared/ro-generate.sh. Not meant to be executed directly.
#
# `.dbllm.env` lives at `.dbmeta/.dbllm.env` in the consuming project and
# is git-IGNORED (it contains database credentials). A git-tracked
# `.dbmeta/.dbllm.env.example` template ships alongside it for team reference.
#
# The file is parsed line-by-line as KEY=VALUE, never `source`d — a config file
# that gets `source`d is an arbitrary code execution surface.
#
# Recognized keys (unknown keys are ignored, not an error):
#   SCHEMAS            optional  — comma-separated schema names; empty/absent = full DB
#   DB_HOST            required for connection — PostgreSQL host
#   DB_PORT            required for connection — PostgreSQL port
#   DB_NAME            required for connection — database name
#   DB_USER            required for connection — read-only role name (also used as
#                      the role name for CREATE ROLE generation)
#   DB_PASSWORD        required for connection — read-only role password
#   RO_DEFAULT_LIMIT   optional  — default row-limit for read-only queries; default
#                                  `200`; must be a positive integer if given
#   SSH_HOST           optional  — jump-host address; non-empty and != CHANGE_ME
#                                  enables SSH tunnel mode (all other SSH_* keys
#                                  are ignored while this is absent/CHANGE_ME)
#   SSH_PORT           optional  — jump-host SSH port; default `22`
#   SSH_USER           optional  — jump-host login user; default `root`
#   SSH_LOCAL_PORT     optional  — local forwarded port; default `15432`
#   SSH_REMOTE_HOST    required in tunnel mode — DB host behind the jump host
#   SSH_REMOTE_PORT    optional  — DB port behind the jump host; default `5432`
#   SSH_KEY_FILE       optional  — private key path; `~/` prefix is replaced
#                                  with `$HOME` (literal prefix substitution,
#                                  no other shell expansion); cert auth wins
#                                  over password auth when both are set
#   SSH_PASSWORD       optional  — jump-host login password (needs `sshpass`)
#
# On success, db_llm_load_config sets these globals:
#   DBS_SCHEMAS  DBS_DB_HOST  DBS_DB_PORT  DBS_DB_NAME
#   DBS_DB_USER  DBS_DB_PASSWORD  DBS_RO_DEFAULT_LIMIT
#   DBS_SSH_HOST  DBS_SSH_PORT  DBS_SSH_USER  DBS_SSH_LOCAL_PORT
#   DBS_SSH_REMOTE_HOST  DBS_SSH_REMOTE_PORT  DBS_SSH_KEY_FILE  DBS_SSH_PASSWORD
#
# db_llm_load_config does not validate the SSH_* fields (that would break
# preflight's field-state reporting, which relies on this function to just
# parse); use db_llm_ensure_tunnel for structural/environment validation
# and to bring the tunnel up.

# Absolute directory this file lives in, resolved at source time so
# db_llm_ensure_tunnel can invoke the shared/ssh-tunnel.sh engine by an
# absolute path regardless of the caller's CWD (callers run with CWD = the
# consuming project root, not shared/).
DBS_SHARED_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# db_llm_config_fail problem cause fix
db_llm_config_fail() {
    echo -e "\033[0;31m[FAIL]\033[0m problem: $1" >&2
    echo -e "\033[0;31m[FAIL]\033[0m cause: $2" >&2
    echo -e "\033[0;31m[FAIL]\033[0m fix: $3" >&2
    return 1
}

# db_llm_load_config config_file
# Parses config_file line-by-line. Blank lines and lines whose first
# non-whitespace char is `#` are skipped. Any other line MUST be KEY=VALUE
# (leading/trailing whitespace around KEY and VALUE is trimmed) or parsing
# fails loud.
db_llm_load_config() {
    local config_file="$1"
    unset DBS_SCHEMAS DBS_DB_HOST DBS_DB_PORT DBS_DB_NAME \
        DBS_DB_USER DBS_DB_PASSWORD DBS_RO_DEFAULT_LIMIT \
        DBS_SSH_HOST DBS_SSH_PORT DBS_SSH_USER DBS_SSH_LOCAL_PORT \
        DBS_SSH_REMOTE_HOST DBS_SSH_REMOTE_PORT DBS_SSH_KEY_FILE DBS_SSH_PASSWORD
    local _schemas_raw=""

    if [[ ! -f "${config_file}" ]]; then
        db_llm_config_fail \
            "找不到配置文件 ${config_file}" \
            "db-llm 需要 .dbmeta/.dbllm.env 声明连接信息" \
            "运行 /pg-readonly-setup 初始化，或从 .dbllm.env.example 复制一份到 ${config_file}"
        return 1
    fi

    local line key value
    while IFS= read -r line || [[ -n "${line}" ]]; do
        [[ "${line}" =~ ^[[:space:]]*$ ]] && continue
        [[ "${line}" =~ ^[[:space:]]*# ]] && continue

        if [[ "${line}" != *"="* ]]; then
            db_llm_config_fail \
                "${config_file} 中有一行不是 KEY=VALUE 形式: ${line}" \
                "配置解析是逐行 KEY=VALUE，不接受任何其它语法（不 source，避免任意代码执行）" \
                "把该行改成 KEY=VALUE 形式，或删除"
            return 1
        fi

        key="${line%%=*}"
        value="${line#*=}"
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"

        case "${key}" in
            SCHEMAS) _schemas_raw="${value}" ;;
            DB_HOST) DBS_DB_HOST="${value}" ;;
            DB_PORT) DBS_DB_PORT="${value}" ;;
            DB_NAME) DBS_DB_NAME="${value}" ;;
            DB_USER) DBS_DB_USER="${value}" ;;
            DB_PASSWORD) DBS_DB_PASSWORD="${value}" ;;
            RO_DEFAULT_LIMIT) DBS_RO_DEFAULT_LIMIT="${value}" ;;
            SSH_HOST) DBS_SSH_HOST="${value}" ;;
            SSH_PORT) DBS_SSH_PORT="${value}" ;;
            SSH_USER) DBS_SSH_USER="${value}" ;;
            SSH_LOCAL_PORT) DBS_SSH_LOCAL_PORT="${value}" ;;
            SSH_REMOTE_HOST) DBS_SSH_REMOTE_HOST="${value}" ;;
            SSH_REMOTE_PORT) DBS_SSH_REMOTE_PORT="${value}" ;;
            SSH_KEY_FILE)
                # Literal `~/` prefix substitution only — no other expansion
                # (the parser does not `source`, so this is not a shell glob
                # or `$HOME`-style shell expansion; it is a plain string edit).
                if [[ "${value}" == "~/"* ]]; then
                    DBS_SSH_KEY_FILE="${HOME}/${value#\~/}"
                else
                    DBS_SSH_KEY_FILE="${value}"
                fi
                ;;
            SSH_PASSWORD) DBS_SSH_PASSWORD="${value}" ;;
            *) : ;; # unknown key — ignored, not an error (forward-compat)
        esac
    done < "${config_file}"

    # SCHEMAS normalization: comma-split -> trim each -> drop empty -> comma-rejoin.
    DBS_SCHEMAS=""
    if [[ -n "${_schemas_raw}" ]]; then
        local -a _parts
        IFS=',' read -ra _parts <<< "${_schemas_raw}"
        local -a _clean=()
        local _p
        for _p in "${_parts[@]}"; do
            _p="${_p#"${_p%%[![:space:]]*}"}"
            _p="${_p%"${_p##*[![:space:]]}"}"
            [[ -n "${_p}" ]] && _clean+=("${_p}")
        done
        if [[ ${#_clean[@]} -gt 0 ]]; then
            DBS_SCHEMAS="$(IFS=,; echo "${_clean[*]}")"
        fi
    fi

    # DB_USER defaults to llm_readonly if absent.
    DBS_DB_USER="${DBS_DB_USER:-llm_readonly}"

    # RO_DEFAULT_LIMIT defaults and validation.
    if [[ -z "${DBS_RO_DEFAULT_LIMIT:-}" ]]; then
        DBS_RO_DEFAULT_LIMIT=200
    elif ! [[ "${DBS_RO_DEFAULT_LIMIT}" =~ ^[0-9]+$ ]] || [[ "${DBS_RO_DEFAULT_LIMIT}" -le 0 ]]; then
        db_llm_config_fail \
            "${config_file} 的 RO_DEFAULT_LIMIT=${DBS_RO_DEFAULT_LIMIT} 不是正整数" \
            "RO_DEFAULT_LIMIT 控制只读查询未指定 --limit 时的默认截断行数，必须是正整数" \
            "把 RO_DEFAULT_LIMIT 改成正整数（如 200），或删掉该行使用缺省值 200"
        return 1
    fi

    # SSH tunnel field defaults (design.md REQ-ST-1). DBS_SSH_HOST,
    # DBS_SSH_REMOTE_HOST, DBS_SSH_KEY_FILE, DBS_SSH_PASSWORD have no default
    # — empty means "not configured"; db_llm_ensure_tunnel treats an empty/
    # CHANGE_ME DBS_SSH_HOST as "tunnel mode disabled".
    DBS_SSH_HOST="${DBS_SSH_HOST:-}"
    DBS_SSH_PORT="${DBS_SSH_PORT:-22}"
    DBS_SSH_USER="${DBS_SSH_USER:-root}"
    DBS_SSH_LOCAL_PORT="${DBS_SSH_LOCAL_PORT:-15432}"
    DBS_SSH_REMOTE_HOST="${DBS_SSH_REMOTE_HOST:-}"
    DBS_SSH_REMOTE_PORT="${DBS_SSH_REMOTE_PORT:-5432}"
    DBS_SSH_KEY_FILE="${DBS_SSH_KEY_FILE:-}"
    DBS_SSH_PASSWORD="${DBS_SSH_PASSWORD:-}"

    return 0
}

# db_llm_redact_one text pat
# Literal (non-glob) substring replace of `pat` with [REDACTED] in `text`.
# `pat` is passed to awk via ENVIRON rather than `-v` — `-v` interprets
# backslash escapes (`\n`, `\t`, ...) in the assigned value, so a pattern
# containing a literal backslash (e.g. a password) would silently fail to
# match; ENVIRON's value is the raw string, untouched.
db_llm_redact_one() {
    local text="$1" pat="$2"
    [[ -z "${pat}" ]] && { printf '%s' "${text}"; return 0; }
    DBS_REDACT_PAT="${pat}" awk '
        BEGIN { pat = ENVIRON["DBS_REDACT_PAT"]; rep = "[REDACTED]" }
        {
            s = $0; out = ""; plen = length(pat)
            while (plen > 0) {
                i = index(s, pat)
                if (i == 0) break
                out = out substr(s, 1, i - 1) rep
                s = substr(s, i + plen)
            }
            print out s
        }' <<< "${text}"
}

# db_llm_redact text
# Redacts PGPASSWORD/PGHOST/PGPORT/PGDATABASE/PGUSER/DBS_SSH_PASSWORD/
# DBS_SSH_HOST out of `text`.
db_llm_redact() {
    local text="$1" value
    for value in "${PGPASSWORD:-}" "${PGHOST:-}" "${PGPORT:-}" "${PGDATABASE:-}" "${PGUSER:-}" \
        "${DBS_SSH_PASSWORD:-}" "${DBS_SSH_HOST:-}"; do
        [[ -n "${value}" ]] && text="$(db_llm_redact_one "${text}" "${value}")"
    done
    printf '%s' "${text}"
}

# db_llm_ensure_tunnel
# Validates SSH tunnel configuration and (if enabled) calls the
# shared/ssh-tunnel.sh engine's `ensure` subcommand so the local forwarded
# port is ready before any psql connection. Requires db_llm_load_config to
# have been called first (reads the DBS_SSH_*/DBS_DB_HOST/DBS_DB_PORT
# globals it sets).
#
# Enablement: tunnel mode is opt-in — this is a no-op (return 0) unless
# DBS_SSH_HOST is non-empty and not the CHANGE_ME placeholder.
#
# Validation order (cheapest-first): structural (pure config, no environment
# dependency) before environment (filesystem / PATH) — design.md 「校验顺序」.
#
# Return codes:
#   0  tunnel disabled, or enabled and ready
#   1  config validation failed (db_llm_config_fail printed its own
#      three-part message), or the engine failed to bring the tunnel up
#      (the engine's redacted combined output was forwarded to stderr)
# db_llm_tunnel_enabled
# 隧道模式的唯一判据：SSH_HOST 填了真值。两种模式下必填字段不同、PGHOST/PGPORT
# 的来源也不同，所以这个判断有两个调用方，抽出来避免两处各写一份。
db_llm_tunnel_enabled() {
    [[ -n "${DBS_SSH_HOST:-}" && "${DBS_SSH_HOST}" != "CHANGE_ME" ]]
}

db_llm_ensure_tunnel() {
    if ! db_llm_tunnel_enabled; then
        return 0
    fi

    # -- structural validation ------------------------------------------------
    if [[ -z "${DBS_SSH_REMOTE_HOST:-}" ]]; then
        db_llm_config_fail \
            "SSH_HOST 已设但 SSH_REMOTE_HOST 为空" \
            "隧道需要知道跳板后面的数据库地址" \
            "在 .dbllm.env 填 SSH_REMOTE_HOST=<内网 PG 地址>"
        return 1
    fi

    # 这里曾有两条跨字段校验：DB_HOST 必须是回环、DB_PORT 必须等于 SSH_LOCAL_PORT。
    # 两个值在隧道模式下已被完全确定（psql 只可能连本机转发端口），却仍逼人填、
    # 填错了再拿校验把人拦下来——信息量为零，出错面为二。现在两个字段在隧道模式
    # 下不再被读取，由 db_llm_export_pg_env 直接推导，校验也就没有对象了（T74）。
    local _port_spec _port_label _port_val
    for _port_spec in "SSH_PORT:${DBS_SSH_PORT}" "SSH_LOCAL_PORT:${DBS_SSH_LOCAL_PORT}" "SSH_REMOTE_PORT:${DBS_SSH_REMOTE_PORT}"; do
        _port_label="${_port_spec%%:*}"
        _port_val="${_port_spec#*:}"
        if ! [[ "${_port_val}" =~ ^[0-9]+$ ]] || [[ "${_port_val}" -lt 1 ]] || [[ "${_port_val}" -gt 65535 ]]; then
            db_llm_config_fail \
                "${_port_label}=${_port_val} 不是 1..65535 的整数" \
                "该值会直接交给 ssh -p / -L" \
                "改为合法端口号"
            return 1
        fi
    done

    if [[ -z "${DBS_SSH_KEY_FILE:-}" && -z "${DBS_SSH_PASSWORD:-}" ]]; then
        db_llm_config_fail \
            "SSH_HOST 已设但未配置认证方式" \
            "证书与密码至少填一项（两者都填时证书优先）" \
            "证书：SSH_KEY_FILE=~/.ssh/<key>；密码：SSH_PASSWORD=<pw>（需 sshpass）"
        return 1
    fi

    # -- environment validation (cert wins over password when both are set) --
    if [[ -n "${DBS_SSH_KEY_FILE:-}" ]]; then
        # An unexpanded `$` is its own diagnosis: .dbllm.env is parsed, never
        # sourced (ADR-0006), so `$HOME/...` stays literal and the file genuinely
        # is not there. Reporting that as "file not found" sends the reader off to
        # `ls` a path that does exist — the wrong cause for the right symptom.
        if [[ "${DBS_SSH_KEY_FILE}" == *'$'* ]]; then
            db_llm_config_fail \
                "SSH_KEY_FILE 含未展开的变量：${DBS_SSH_KEY_FILE}" \
                ".dbllm.env 是被逐行解析的、从不 source（ADR-0006），\$VAR 不会展开成路径" \
                "改用 ~/ 前缀：SSH_KEY_FILE=~/.ssh/<私钥文件名>"
            return 1
        fi
        if [[ ! -f "${DBS_SSH_KEY_FILE}" ]]; then
            db_llm_config_fail \
                "证书文件不存在：${DBS_SSH_KEY_FILE}" \
                "SSH_KEY_FILE 指向的文件不存在或不可读（~/ 已按字面替换为 \$HOME）" \
                "核对路径或把私钥放到该位置"
            return 1
        fi
    elif [[ -n "${DBS_SSH_PASSWORD:-}" ]]; then
        if ! command -v sshpass >/dev/null 2>&1; then
            db_llm_config_fail \
                "密码模式需要 sshpass，PATH 中未找到" \
                "SSH_PASSWORD 已设、SSH_KEY_FILE 为空，引擎以 sshpass -e 喂密码" \
                "macOS: brew install sshpass；Debian/Ubuntu: sudo apt install sshpass；或改用 SSH_KEY_FILE"
            return 1
        fi
    fi

    # -- call the engine -------------------------------------------------------
    # 🔴 这段看起来是恒等映射（SSH_X="${DBS_SSH_X}"），**不是冗余，MUST NOT 删**。
    # 2026-09-09 键名统一为 SSH_* 之前，它兼做名字翻译（JUMP_HOST/LOCAL_PORT/...），
    # 统一后翻译这一层消失了，只剩下面两件与命名无关、但都必需的事：
    #
    #   ① 进程作用域：命令前缀形式让这些变量**只对下面那一个 spawned 进程可见**，
    #      从不 `export` 进 config.sh 自己的进程 —— 所以 SSH_PASSWORD 不会漏进
    #      调用方后续的 psql 子进程。删掉这段就得 `export SSH_*`，正好破掉这条。
    #   ② 命名空间：config.sh 是被 db-collect.sh 等 **source 进调用方 shell** 的，
    #      它设的变量活在调用方的 shell 里。配置值一律存 DBS_* 前缀（.dbllm.env
    #      是被解析、不被 source 的，ADR-0006），裸 SSH_* 只在这一行的作用域里出现，
    #      两个命名空间不互相污染。
    #
    # MUST NOT go through `sh -c`/`eval`, which would stringify the values into argv.
    local _tmp_out _rc _forwarded
    _tmp_out="$(mktemp)"
    _rc=0
    SSH_HOST="${DBS_SSH_HOST}" \
    SSH_PORT="${DBS_SSH_PORT}" \
    SSH_USER="${DBS_SSH_USER}" \
    SSH_LOCAL_PORT="${DBS_SSH_LOCAL_PORT}" \
    SSH_REMOTE_HOST="${DBS_SSH_REMOTE_HOST}" \
    SSH_REMOTE_PORT="${DBS_SSH_REMOTE_PORT}" \
    SSH_KEY_FILE="${DBS_SSH_KEY_FILE}" \
    SSH_PASSWORD="${DBS_SSH_PASSWORD}" \
    bash "${DBS_SHARED_DIR}/ssh-tunnel.sh" ensure >"${_tmp_out}" 2>&1 || _rc=$?

    _forwarded="$(db_llm_redact "$(cat "${_tmp_out}")")"
    rm -f "${_tmp_out}"
    [[ -n "${_forwarded}" ]] && printf '%s\n' "${_forwarded}" >&2

    [[ ${_rc} -ne 0 ]] && return 1
    return 0
}

# db_llm_export_pg_env
# Exports PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD from the loaded DBS_DB_*
# globals. Requires db_llm_load_config to have been called first. Once
# credentials are confirmed present (not CHANGE_ME, not missing), calls
# db_llm_ensure_tunnel so the local forwarded port is ready before any
# caller opens a psql connection — this is the single choke point all four
# connection entry scripts (db-collect.sh / ro-session.sh / ro-verify.sh /
# readonly-setup.sh) already pass through (decision-memo D6).
#
# Return codes:
#   0  all five values present and exported, and the tunnel (if enabled) is
#      ready
#   1  config error (db_llm_config_fail printed its own message), or
#      db_llm_ensure_tunnel failed (its own problem/cause/fix message, or
#      the engine's redacted output, already went to stderr)
#   2  credentials not ready (CHANGE_ME placeholders or missing values) —
#      callers use this to decide whether to write needs-human.md
db_llm_export_pg_env() {
    local _key _val _missing=0
    # 必填清单按模式分支：隧道模式下 DB_HOST/DB_PORT 由隧道确定（见下方推导），
    # 不问人、也不读——既有消费仓的配置里多填的那两行因此只是被忽略，不报错，
    # 升级不会炸。SSH_* 各字段的完整性与合法性归 db_llm_ensure_tunnel 管，
    # 这里不重复检查。
    local -a _required=(DB_NAME DB_USER DB_PASSWORD)
    db_llm_tunnel_enabled || _required=(DB_HOST DB_PORT "${_required[@]}")
    for _key in "${_required[@]}"; do
        local _dbs_var="DBS_${_key}"
        _val="${!_dbs_var:-}"
        if [[ -z "${_val}" ]]; then
            _missing=1
            break
        fi
        if [[ "${_val}" == "CHANGE_ME" ]]; then
            db_llm_config_fail \
                ".dbllm.env 的 ${_key} 仍是占位符 CHANGE_ME" \
                "配置文件中的连接信息未填写" \
                "编辑 .dbmeta/.dbllm.env，把 ${_key} 改成真实值（DB_PASSWORD 由 /pg-readonly-setup 自动生成，其余手动填写）"
            return 2
        fi
    done

    if [[ ${_missing} -eq 1 ]]; then
        db_llm_config_fail \
            ".dbllm.env 缺少必需的连接字段" \
            "本模式下 ${_required[*]} 必须全部填写才能连接数据库" \
            "编辑 .dbmeta/.dbllm.env 补全连接信息，或运行 /pg-readonly-setup 重新初始化"
        return 2
    fi

    db_llm_ensure_tunnel || return 1

    if db_llm_tunnel_enabled; then
        # 隧道把本机回环的 SSH_LOCAL_PORT 转发到目标机，psql 只可能连这一个地址。
        # 用 localhost 而非 127.0.0.1：ssh-tunnel.sh 的 -L 不指定 bind 地址，ssh
        # 因此绑整个 loopback（IPv4 与 IPv6 都绑），而 libpq 连 localhost 会把解析
        # 出的地址逐个试过去——写死某一个字面地址反而在纯 IPv6 环境下更脆。
        export PGHOST="localhost"
        export PGPORT="${DBS_SSH_LOCAL_PORT}"
    else
        export PGHOST="${DBS_DB_HOST}"
        export PGPORT="${DBS_DB_PORT}"
    fi
    export PGDATABASE="${DBS_DB_NAME}"
    export PGUSER="${DBS_DB_USER}"
    export PGPASSWORD="${DBS_DB_PASSWORD}"
    unset DATABASE_URL
    export PGCONNECT_TIMEOUT=10

    return 0
}

# Shared connection-error classification pattern (T11: single source of truth).
DB_LLM_CONN_ERROR_RE='could not connect|connection refused|no password supplied|password authentication failed|SASL authentication failed|timeout expired|server closed the connection'
