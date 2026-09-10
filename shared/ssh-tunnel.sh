#!/usr/bin/env bash
# SSH 本地端口转发隧道
#
# 所有参数均通过环境变量配置，支持同时运行多个实例：
#
#   SSH_HOST      跳板机地址         （启动时必填）
#   SSH_PORT      跳板机 SSH 端口    （默认 22）
#   SSH_USER       跳板机登录用户     （默认 root）
#   SSH_LOCAL_PORT     本地监听端口       （默认 6432）
#   SSH_REMOTE_HOST    目标内网地址       （启动时必填）
#   SSH_REMOTE_PORT    目标内网端口       （默认同 SSH_LOCAL_PORT）
#
# 认证模式（二选一，证书优先，启动时必填）：
#   SSH_KEY_FILE   私钥路径 → 证书模式
#   SSH_PASSWORD   登录密码 → 密码模式（需安装 sshpass）
#
# Usage:
#   ./hack/ssh-tunnel.sh start        # 启动隧道（后台运行）
#   ./hack/ssh-tunnel.sh stop         # 停止隧道（仅需 SSH_LOCAL_PORT）
#   ./hack/ssh-tunnel.sh status       # 查看隧道状态（仅需 SSH_LOCAL_PORT）

set -euo pipefail

# ── 旧键名迁移守卫（2026-09-09 统一为 SSH_* 前缀）────────────────
# 旧名：JUMP_HOST / JUMP_PORT / LOCAL_PORT / REMOTE_HOST / REMOTE_PORT
# 不加这道守卫的话，还用旧名的调用方会**静默**落到下面的默认值上
# （SSH_LOCAL_PORT 回落 6432 = 连错池，SSH_PORT 回落 22），比报错难查得多。
for _old in JUMP_HOST JUMP_PORT LOCAL_PORT REMOTE_HOST REMOTE_PORT; do
    if [[ -n "${!_old:-}" ]]; then
        echo "problem: 检测到已废弃的环境变量 ${_old}" >&2
        echo "cause:   隧道键名已统一为 SSH_* 前缀，与 .dbmeta/.dbllm.env 一致；旧名不再被读取" >&2
        echo "fix:     改名 JUMP_HOST->SSH_HOST  JUMP_PORT->SSH_PORT  LOCAL_PORT->SSH_LOCAL_PORT" >&2
        echo "         REMOTE_HOST->SSH_REMOTE_HOST  REMOTE_PORT->SSH_REMOTE_PORT" >&2
        exit 1
    fi
done
unset _old

SSH_LOCAL_PORT="${SSH_LOCAL_PORT:-6432}"       # 本地监听端口（stop/status 只需要这个）
PID_FILE="/tmp/ssh-tunnel-${SSH_LOCAL_PORT}.pid"

# ── 停止后台隧道（不需要认证参数）────────────────────────────
stop_tunnel() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "停止隧道 (PID: $pid, 本地端口: $SSH_LOCAL_PORT)..."
            kill "$pid"
        else
            echo "隧道进程不存在 (PID: $pid 已退出)"
        fi
        rm -f "$PID_FILE"
    else
        echo "未找到运行中的隧道 (${PID_FILE} 不存在)"
    fi
}

# ── 查看隧道状态（不需要认证参数）────────────────────────────
status_tunnel() {
    local jump_host="${SSH_HOST:-?}"
    local jump_port="${SSH_PORT:-22}"
    local jump_user="${SSH_USER:-root}"
    local remote_host="${SSH_REMOTE_HOST:-?}"
    local remote_port="${SSH_REMOTE_PORT:-${SSH_LOCAL_PORT}}"

    echo "── SSH 隧道状态 ──────────────────────────────"
    echo "  路由：localhost:${SSH_LOCAL_PORT} → ${jump_user}@${jump_host}:${jump_port} → ${remote_host}:${remote_port}"

    # 1. 检查 PID 文件
    echo ""
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "  进程：运行中 (PID: $pid)"
        else
            echo "  进程：PID 文件存在但进程已退出 (PID: $pid，可能已崩溃)"
        fi
    else
        echo "  进程：未通过本脚本启动（无 PID 文件）"
    fi

    # 2. 检查本地端口是否在监听（兼容 Linux ss/lsof 和 Windows netstat）
    echo ""
    if ss -tlnp "sport = :${SSH_LOCAL_PORT}" 2>/dev/null | grep -q LISTEN; then
        echo "  端口：localhost:${SSH_LOCAL_PORT} 正在监听 ✓"
    elif lsof -i ":${SSH_LOCAL_PORT}" 2>/dev/null | grep -q LISTEN; then
        echo "  端口：localhost:${SSH_LOCAL_PORT} 正在监听 ✓"
    elif netstat -ano 2>/dev/null | grep -q ":${SSH_LOCAL_PORT}.*LISTENING"; then
        echo "  端口：localhost:${SSH_LOCAL_PORT} 正在监听 ✓"
    else
        echo "  端口：localhost:${SSH_LOCAL_PORT} 未监听 ✗"
    fi

    # 3. 查找 SSH 进程（兼容 Linux pgrep 和 Windows tasklist）
    echo ""
    local ssh_procs=""
    local ssh_list=""
    local pgrep_status=1
    if command -v pgrep &>/dev/null; then
        if ssh_list=$(pgrep -a ssh 2>/dev/null); then
            pgrep_status=0
            ssh_procs=$(grep -F "${remote_host}:${remote_port}" <<<"$ssh_list" || true)
        else
            pgrep_status=2
        fi
    fi
    if [[ -n "$ssh_procs" ]]; then
        echo "  SSH 进程："
        echo "$ssh_procs" | while read -r line; do echo "    $line"; done
    elif [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            # Windows 下用 tasklist 显示进程详情
            local proc_info
            proc_info=$(tasklist /FI "PID eq $pid" /FO LIST 2>/dev/null | grep -E "^(映像名称|Image Name|PID)" || true)
            echo "  SSH 进程：PID $pid 运行中"
            [[ -n "$proc_info" ]] && echo "$proc_info" | while read -r line; do echo "    $line"; done
        fi
    else
        if [[ "$pgrep_status" -eq 0 ]]; then
            echo "  SSH 进程：未发现匹配进程"
        elif [[ "$pgrep_status" -eq 2 ]]; then
            echo "  SSH 进程：无法查询（pgrep 查询失败）"
        else
            echo "  SSH 进程：无法查询（pgrep 不可用）"
        fi
    fi
    echo "──────────────────────────────────────────────"
}

# ── 判断本地端口是否正在监听（0=监听，非0=未监听）──────────────
port_listening() {
    ss -tlnp "sport = :${SSH_LOCAL_PORT}" 2>/dev/null | grep -q LISTEN || \
    lsof -i ":${SSH_LOCAL_PORT}" 2>/dev/null | grep -q LISTEN || \
    netstat -ano 2>/dev/null | grep -q ":${SSH_LOCAL_PORT}.*LISTENING"
}

# ── 检查端口是否已被占用 ──────────────────────────────────────
check_port() {
    if port_listening; then
        echo "警告：本地端口 ${SSH_LOCAL_PORT} 已被占用，可能隧道已在运行"
        echo "  使用 '$0 stop' 先停止旧隧道，或 '$0 status' 查看详情"
        exit 1
    fi
}

# ── 初始化连接参数（仅启动时调用）────────────────────────────
init_connect_params() {
    SSH_HOST="${SSH_HOST:-}"
    SSH_PORT="${SSH_PORT:-22}"
    JUMP_USER="${SSH_USER:-root}"
    SSH_REMOTE_HOST="${SSH_REMOTE_HOST:-}"
    SSH_REMOTE_PORT="${SSH_REMOTE_PORT:-${SSH_LOCAL_PORT}}"

    if [[ -z "$SSH_HOST" ]]; then
        echo "错误：必须设置 SSH_HOST（跳板机地址）"
        exit 1
    fi
    if [[ -z "$SSH_REMOTE_HOST" ]]; then
        echo "错误：必须设置 SSH_REMOTE_HOST（目标内网地址）"
        exit 1
    fi

    # 认证模式：证书优先，其次密码，否则报错
    AUTH_LABEL=""
    SSH_COMMAND=(ssh)
    SSH_AUTH_OPTS=()

    if [[ -n "${SSH_KEY_FILE:-}" ]]; then
        if [[ ! -f "${SSH_KEY_FILE}" ]]; then
            echo "错误：证书文件不存在：${SSH_KEY_FILE}"
            exit 1
        fi
        AUTH_LABEL="证书：${SSH_KEY_FILE}"
        SSH_AUTH_OPTS=(
            -i "${SSH_KEY_FILE}"
            -o PreferredAuthentications=publickey
            -o PasswordAuthentication=no
        )
    elif [[ -n "${SSH_PASSWORD:-}" ]]; then
        if ! command -v sshpass &>/dev/null; then
            echo "错误：密码模式需要安装 sshpass"
            echo "  Ubuntu/Debian: sudo apt install sshpass"
            echo "  macOS:         brew install sshpass"
            exit 1
        fi
        AUTH_LABEL="密码模式（sshpass）"
        export SSHPASS="${SSH_PASSWORD}"
        SSH_COMMAND=(sshpass -e ssh)
        SSH_AUTH_OPTS=(
            -o PreferredAuthentications=password
            -o PubkeyAuthentication=no
        )
    else
        echo "错误：未配置认证方式，请设置以下任一环境变量："
        echo "  证书模式：export SSH_KEY_FILE=~/.ssh/your_private_key"
        echo "  密码模式：export SSH_PASSWORD=your_password"
        exit 1
    fi

    SSH_OPTS=(
        "${SSH_AUTH_OPTS[@]}"
        -p "${SSH_PORT}"
        -N
        -L "${SSH_LOCAL_PORT}:${SSH_REMOTE_HOST}:${SSH_REMOTE_PORT}"
        -o StrictHostKeyChecking=accept-new
        -o ServerAliveInterval=30
        -o ServerAliveCountMax=3
        -o ExitOnForwardFailure=yes
        "${JUMP_USER}@${SSH_HOST}"
    )
}

# ── 入口逻辑 ──────────────────────────────────────────────────
case "${1:-}" in
    status)
        status_tunnel
        ;;
    stop)
        stop_tunnel
        ;;
    start)
        init_connect_params
        check_port
        echo "启动 SSH 隧道..."
        echo "  ${JUMP_USER}@${SSH_HOST}:${SSH_PORT}  →  localhost:${SSH_LOCAL_PORT} ⇒ ${SSH_REMOTE_HOST}:${SSH_REMOTE_PORT}"
        echo "  认证：${AUTH_LABEL}"
        "${SSH_COMMAND[@]}" "${SSH_OPTS[@]}" &
        echo $! > "$PID_FILE"
        sleep 1
        if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "隧道已启动 (PID: $(cat "$PID_FILE"))"
            echo "  查看状态：${TUNNEL_SCRIPT:-$0} status"
            echo "  停止命令：${TUNNEL_SCRIPT:-$0} stop"
        else
            echo "隧道启动失败，请检查 SSH 连接或端口占用"
            rm -f "$PID_FILE"
            exit 1
        fi
        ;;
    ensure)
        if port_listening; then
            echo "已就绪（复用）"
            exit 0
        fi
        init_connect_params
        "${SSH_COMMAND[@]}" "${SSH_OPTS[@]}" &
        ensure_pid=$!
        echo "$ensure_pid" > "$PID_FILE"

        ensure_timeout="${SSH_TUNNEL_ENSURE_TIMEOUT:-10}"
        ensure_ready=0
        ensure_elapsed=0
        while (( ensure_elapsed < ensure_timeout )); do
            if port_listening; then
                ensure_ready=1
                break
            fi
            if ! kill -0 "$ensure_pid" 2>/dev/null; then
                break
            fi
            sleep 1
            ensure_elapsed=$(( ensure_elapsed + 1 ))
        done

        if [[ "$ensure_ready" -eq 1 ]]; then
            echo "已启动 (PID ${ensure_pid})"
            exit 0
        fi

        # 失败清理：只信本次 spawn 的 pid，不重读 PID 文件；kill 前先判活。
        if kill -0 "$ensure_pid" 2>/dev/null; then
            kill "$ensure_pid" 2>/dev/null || true
        fi
        if [[ -f "$PID_FILE" ]] && [[ "$(cat "$PID_FILE" 2>/dev/null || true)" == "$ensure_pid" ]]; then
            rm -f "$PID_FILE"
        fi

        ensure_script_abs="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
        echo -e "\033[0;31m[FAIL]\033[0m problem: SSH 隧道 ${ensure_timeout}s 内未就绪（localhost:${SSH_LOCAL_PORT}）" >&2
        echo -e "\033[0;31m[FAIL]\033[0m cause: ssh 进程退出或握手挂起（跳板不可达 / 认证失败 / 转发被拒）" >&2
        echo -e "\033[0;31m[FAIL]\033[0m fix: bash ${ensure_script_abs} status 查看详情；已清理本次启动的 ssh 进程" >&2
        exit 1
        ;;
    *)
        echo "用法: $0 {start|stop|status|ensure}"
        exit 1
        ;;
esac
