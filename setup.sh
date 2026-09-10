#!/usr/bin/env bash
# db-llm setup — install skills into BOTH global hosts:
#   - Claude  ~/.claude/skills/
#   - Codex   ~/.codex/skills/
#
# Idempotent. Unix: symlink (git pull auto-reflects). Windows(Git Bash): copy +
# marker — MSYS `ln -s` silently degrades to a copy that `git pull` won't
# refresh, so on Windows we copy explicitly and stamp a `.db-llm` marker
# (holding the installed HEAD sha) for ownership + versioning; update = re-run.
#
# Conflict policy:
#   - target already a symlink pointing at the same path we'd install -> success,
#     no-op (idempotent re-run).
#   - target exists as a real directory, or a symlink pointing elsewhere, or a
#     regular file -> fail loud with a migration command. MUST NOT overwrite or
#     recursively delete anything we don't own.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIRS=("$HOME/.claude/skills" "$HOME/.codex/skills")

# 平台探测：Git Bash / MSYS / Cygwin 一律走 Windows 拷贝分支。
IS_WINDOWS=0
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*|Windows_NT) IS_WINDOWS=1 ;;
esac
MARKER=".db-llm"                             # Windows 拷贝的所有权标记，内含安装版本 sha
is_our_copy() {                              # 一份拷贝是不是本脚本装的
    [[ -f "$1/${MARKER}" ]]
}

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1" >&2; }

install_skill() {
    local skill_name="$1" dest_dir="$2"
    local src="${REPO_DIR}/${skill_name}"
    local target="${dest_dir}/${skill_name}"

    mkdir -p "${dest_dir}"

    if [[ "${IS_WINDOWS}" -eq 1 ]]; then
        # Windows(Git Bash): 拷贝 + marker。只覆盖本脚本自己装的拷贝，绝不动别人的目录。
        # 例外：shared/ 是两仓各自 owns 互斥文件名的共用目录（db-llm 8 件、pg-ops 4 件），
        # 后装的一方 MUST NOT rm -rf 先装一方的文件——合并拷贝，逐文件覆盖同名（design F9）。
        if [[ "${skill_name}" == "shared" ]]; then
            if [[ -e "${target}" ]] && ! is_our_copy "${target}"; then
                fail "problem: ${target} 已存在（非本脚本管理的目录/普通文件）"
                fail "cause: 该路径被非本脚本安装的内容占用，拒绝覆盖"
                fail "fix: 确认内容后手动迁移，例如 mv '${target}' '${target}.bak'，再重跑本脚本"
                exit 1
            fi
            mkdir -p "${target}"
            cp -r "${src}/." "${target}/"          # 合并拷贝：逐文件覆盖同名，不清空目标目录
            git -C "${REPO_DIR}" rev-parse HEAD > "${target}/${MARKER}" 2>/dev/null \
                || echo unknown > "${target}/${MARKER}"
            pass "${skill_name} @ ${dest_dir} — 已合并拷贝（Windows），更新请重跑 setup.sh"
            return 0
        fi
        if [[ -e "${target}" ]] && ! is_our_copy "${target}"; then
            fail "problem: ${target} 已存在（非本脚本管理的目录/普通文件）"
            fail "cause: 该路径被非本脚本安装的内容占用，拒绝覆盖"
            fail "fix: 确认内容后手动迁移，例如 mv '${target}' '${target}.bak'，再重跑本脚本"
            exit 1
        fi
        rm -rf "${target}"                    # 自属旧拷贝（或不存在）→ 清掉重拷，保幂等更新；旧标记随目录一起被换掉
        cp -r "${src}" "${target}"
        git -C "${REPO_DIR}" rev-parse HEAD > "${target}/${MARKER}" 2>/dev/null \
            || echo unknown > "${target}/${MARKER}"
        pass "${skill_name} @ ${dest_dir} — 已安装拷贝（Windows），更新请重跑 setup.sh"
        return 0
    fi

    if [[ -L "${target}" ]]; then
        local existing_link
        existing_link="$(readlink "${target}")"
        if [[ "${existing_link}" == "${src}" ]]; then
            pass "${skill_name} @ ${dest_dir} — 已是正确软链，跳过"
            return 0
        fi
        fail "problem: ${target} 是软链，但指向别处（${existing_link}）"
        fail "cause: 目标已被另一份安装占用（另一个仓 / 另一次手工链接）"
        fail "fix: 确认无误后手动执行 rm '${target}' && ln -s '${src}' '${target}'，或重新运行本脚本前先清理"
        exit 1
    fi

    if [[ -e "${target}" ]]; then
        fail "problem: ${target} 已存在（目录或普通文件，非软链）"
        fail "cause: 该路径被非本脚本管理的内容占用，拒绝覆盖"
        fail "fix: 确认内容后手动迁移，例如 mv '${target}' '${target}.bak' && ln -s '${src}' '${target}'"
        exit 1
    fi

    ln -s "${src}" "${target}"
    pass "${skill_name} @ ${dest_dir} — 已安装软链 → ${src}"
}

info "=== db-llm setup ==="
for dest in "${TARGET_DIRS[@]}"; do
    install_skill "pg-readonly-setup" "${dest}"
    install_skill "pg-dict" "${dest}"
    install_skill "pg-query-ro" "${dest}"
    install_skill "pg-sql-check" "${dest}"
    install_skill "db-llm-upgrade" "${dest}"
    if [[ "${IS_WINDOWS}" -eq 1 ]]; then
        # Windows 拷贝模式下 pg-dict/scripts/../../shared 落在 <dest>/shared，不像 Unix
        # 软链那样自然可达（pwd -P 解回真源），需把 shared/ 也拷一份到各宿主目录
        install_skill "shared" "${dest}"
    fi
done
if [[ "${IS_WINDOWS}" -eq 1 ]]; then
    info "mode: copy (Windows) —— 更新请重跑本脚本（git pull 不会自动反映）"
else
    info "mode: symlink (Unix) —— git pull 自动反映最新"
fi
pass "全部完成"
