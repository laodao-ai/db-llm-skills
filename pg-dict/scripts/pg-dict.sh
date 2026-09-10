#!/bin/bash
# Generate/refresh .dbmeta/<schema>/... data dictionary files from the dev database's
# live pg_catalog metadata (tables/columns/types/defaults/nullability/indexes/COMMENT
# ON comments/reltuples row estimate/分区子表折叠), plus the whole-repo knowledge base
# entry points documented by dbmeta's spec (root/schema README, _relations.md,
# _gaps.md, per-schema _collect.json). One PG schema per subdirectory; every
# non-system schema owning at least one supported object is auto-discovered — no
# per-schema registration needed.
#
# Managed-block merge: each non-partition table's block (<!-- pg-dict:<table>:start/
# end -->) is fully rewritten; text outside blocks is hand-maintained and preserved
# verbatim, in place. A dropped table's block is removed and any directly-adjacent
# hand-written note is flagged as orphaned rather than silently dropped (the file's
# own leading header is never flagged). See render.py (same dir).
#
# This script no longer talks to pg_catalog itself — it delegates all metadata
# collection to shared/db-collect.sh (db-collect-foundation), the sole pg_catalog read
# point shared with the contract guard and any future db-lint tooling. collect and
# render stay two separate steps (rather than one pipeline) so a failure can be
# pinned to a specific layer instead of a generic "generation failed".
#
# Usage:
#   ./pg-dict.sh                       # regenerate .dbmeta/ (run via the /pg-dict skill; from the skill scripts/ dir)
#
# This is a development-time tool (invoked by the /pg-dict skill), not part of the
# server's boot path — no long-running/always-on validation is introduced.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ROOT_DIR_SOURCE records which fallback branch actually
# fired, surfaced in the closing summary — CLAUDE_PROJECT_DIR/git-toplevel/$PWD
# resolve to the same directory in the common case, but silently diverge when a
# tool sets CLAUDE_PROJECT_DIR to something stale or a wrapper runs this outside
# a git worktree.
if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
    ROOT_DIR="${CLAUDE_PROJECT_DIR}"
    ROOT_DIR_SOURCE="CLAUDE_PROJECT_DIR"
elif ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    ROOT_DIR_SOURCE="git toplevel"
else
    ROOT_DIR="${PWD}"
    ROOT_DIR_SOURCE='$PWD'
fi
export ROOT_DIR
DB_COLLECT_SH="${SCRIPT_DIR}/../../shared/db-collect.sh"
CONFIG_LIB="${SCRIPT_DIR}/../../shared/config.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }
step() { echo -e "${CYAN}[STEP]${NC} $1"; }
warn3() {
    echo -e "${YELLOW}[WARN]${NC} problem: $1"
    echo -e "${YELLOW}[WARN]${NC} cause: $2"
    echo -e "${YELLOW}[WARN]${NC} fix: $3"
}

info "=== 数据字典生成 ==="
step "项目根: ${ROOT_DIR}（来源: ${ROOT_DIR_SOURCE}）"

if [[ ! -f "${CONFIG_LIB}" ]]; then
    fail "problem: 找不到 shared/config.sh（${CONFIG_LIB}）"
    fail "cause: 该脚本随 db-llm 仓一起安装，路径不对说明本地安装不完整"
    fail "fix: 确认 db-llm 仓的 shared/config.sh 存在，或核对 CLAUDE_PROJECT_DIR/git toplevel 是否指向正确仓库"
    exit 1
fi
# shellcheck source=/dev/null
source "${CONFIG_LIB}"

# 输出目录 + 配置文件位置固定为 "${ROOT_DIR}/.dbmeta"（relation-inference-and-diagram
# D1）——不再可配置。凭据（DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD）与配置
# （SCHEMAS）统一在 .dbmeta/.dbllm.env 里，pg-dict.sh 自己只读 SCHEMAS，凭据
# 由 shared/db-collect.sh 在采集时解析。先跑 /pg-readonly-setup 供给只读角色，
# 通道可用后再跑本 skill。
DBMETA_DIR="${ROOT_DIR}/.dbmeta"
NEW_CONFIG="${DBMETA_DIR}/.dbllm.env"
CONFIG_TEMPLATE="${DBLLM_TEMPLATE_OVERRIDE:-${SCRIPT_DIR}/../../shared/.dbllm.env.example}"

# 自动初始化判定表，MUST 在 db_llm_load_config 之前跑：
#   .dbllm.env 存在      -> 正常流程
#   不存在 + 模版存在        -> 自动建 .dbmeta/ + 复制模版，提示填写后重跑
#   不存在 + 模版也不存在    -> fail-loud（安装不完整）
# 所有"缺失"分支均 exit 1，不继续执行采集/渲染。
if [[ ! -f "${NEW_CONFIG}" ]]; then
    if [[ -e "${NEW_CONFIG}" ]]; then
        fail "problem: ${NEW_CONFIG} 存在但不是普通文件（可能是目录）"
        fail "cause: 该路径被占用，pg-dict.sh 无法在此创建/读取配置文件"
        fail "fix: 删除或移走 ${NEW_CONFIG}（该路径本应是配置文件，不应是目录或其它类型）后重跑"
        exit 1
    fi
    if [[ -f "${CONFIG_TEMPLATE}" ]]; then
        if ! mkdir -p "${DBMETA_DIR}"; then
            fail "problem: 自动初始化失败——无法创建输出目录 ${DBMETA_DIR}"
            fail "cause: 目录创建失败（权限不足或磁盘已满）"
            fail "fix: 检查 ${ROOT_DIR} 的写权限与磁盘剩余空间后重跑；或手动执行 mkdir -p ${DBMETA_DIR}"
            exit 1
        fi
        if ! cp "${CONFIG_TEMPLATE}" "${NEW_CONFIG}"; then
            fail "problem: 自动初始化失败——无法复制模版到 ${NEW_CONFIG}"
            fail "cause: 文件复制失败（权限不足或磁盘已满）"
            fail "fix: 检查 ${DBMETA_DIR} 的写权限与磁盘剩余空间后重跑；或手动执行 cp ${CONFIG_TEMPLATE} ${NEW_CONFIG}"
            exit 1
        fi
        fail "problem: 未找到配置文件 ${NEW_CONFIG}，已自动从模版创建一份"
        fail "cause: 首次在本项目运行 pg-dict，需要只读角色凭据才能连接开发库"
        fail "fix: 编辑 ${NEW_CONFIG} 填入连接信息（DB_HOST/DB_PORT/DB_NAME），然后跑 /pg-readonly-setup 供给只读角色，通道报「只读通道可用」后再重跑本 skill"
        exit 1
    fi
    fail "problem: 未找到配置文件 ${NEW_CONFIG}，也未找到安装模版 ${CONFIG_TEMPLATE}"
    fail "cause: 本地 db-llm 安装不完整（随 skill 安装的 .dbllm.env.example 缺失）"
    fail "fix: 重新执行 db-llm 仓的 setup.sh 完整安装后重跑"
    exit 1
fi

db_llm_load_config "${NEW_CONFIG}" || exit 1
step "输出目录: ${DBMETA_DIR}"

# pg-dict.sh 本身从不给 shared/db-collect.sh 传 --schema
# （范围三级取值 CLI>SCHEMAS>全量中的 CLI 档只服务手工/契约测试等直接调用
# db-collect.sh 的场景）——因此这里的覆盖范围来源只会落在 SCHEMAS 配置或全量二选一。
if [[ -n "${DBS_SCHEMAS}" ]]; then
    COVERAGE_SOURCE="SCHEMAS 配置（.dbllm.env: ${DBS_SCHEMAS}）"
else
    COVERAGE_SOURCE="全量（.dbllm.env 未声明 SCHEMAS）"
fi
step "覆盖范围来源: ${COVERAGE_SOURCE}"

if [[ ! -x "${DB_COLLECT_SH}" ]]; then
    fail "problem: 找不到可执行的 shared/db-collect.sh（${DB_COLLECT_SH}）"
    fail "cause: 该脚本随 db-llm 仓一起安装，路径不对或权限缺失"
    fail "fix: 确认 db-llm 仓的 shared/db-collect.sh 存在且可执行（chmod +x），或核对 CLAUDE_PROJECT_DIR/git toplevel 是否指向正确仓库"
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    fail "problem: python3 未安装或不在 PATH 中"
    fail "cause: render.py 需要 python3 才能运行"
    fail "fix: 安装 python3 后重试"
    exit 1
fi

# collect（shared/db-collect.sh 采集）与 render（python 渲染）分两步跑：合并成一根管道时
# || 捕获不到具体哪层失败，报错只能笼统说「生成失败」，排查得靠猜（T9）。采集层失败时
# MUST NOT 写任何 .dbmeta/ 文件（下面这个分支在 render.py 跑之前就 exit）。
COLLECTED="$("${DB_COLLECT_SH}")" || {
    fail "problem: 元数据采集失败（collect 层）"
    fail "cause: shared/db-collect.sh 非零退出（详见上方 stderr）"
    fail "fix: 单独重跑 shared/db-collect.sh 复现；不写任何 .dbmeta/ 文件"
    exit 1
}
RESULT="$(printf '%s' "${COLLECTED}" | python3 "${SCRIPT_DIR}/render.py" \
    --dbmeta-dir "${DBMETA_DIR}")" || {
    fail "problem: 字典渲染失败（render 层）"
    fail "cause: render.py 处理元数据报错（详见上方 stderr 的 problem/cause/fix）"
    fail "fix: 按 render.py 给出的 fix 排查；必要时把 shared/db-collect.sh 的输出存文件后手动喂给 render.py 复现"
    exit 1
}

pass "字典生成完成"
echo "${RESULT}" | python3 -c '
import json, sys
r = json.load(sys.stdin)
schemas = ", ".join(r["schemas"]) or "(无)"
print(f"       覆盖 schema: {schemas}")
if r["written"]:
    print("       已更新:")
    for p in r["written"]:
        print(f"         - {p}")
if r["deleted"]:
    print("       已删除:")
    for p in r["deleted"]:
        print(f"         - {p}")
unchanged = r["unchanged"]
if unchanged:
    print(f"       无变化: {len(unchanged)} 个文件")

# 范围外未更新的 schema（磁盘有、但不在 requested_schemas
# 内——render 本次运行未删未改）与「范围里声明了但采集结果为空」的 schema（拼写错误
# 或零对象，属于 requested_schemas 但不在 schemas[] 内——将在下次运行被收敛删除）
# 分开报告，任一非空都 MUST NOT 静默略过。
sep = ","
sep += " "
requested = r.get("requested_schemas")
out_of_scope = r.get("out_of_scope_schemas") or []
if out_of_scope:
    print(f"       范围外未更新 schema: {sep.join(out_of_scope)}")
if requested is not None:
    declared_empty = sorted(set(requested) - set(r["schemas"]))
    if declared_empty:
        print(
            f"       [WARN] 范围内已声明但采集结果为空的 schema: {sep.join(declared_empty)}"
            "（拼写错误或零对象；若磁盘上存在同名目录，将在下次运行被收敛删除）"
        )
g = r.get("gaps", {})
print(
    "       缺注释：表 {tables} / 列 {columns} / 函数 {functions} / 敏感 warning {sensitive}".format(
        tables=g.get("tables", 0),
        columns=g.get("columns", 0),
        functions=g.get("functions", 0),
        sensitive=g.get("sensitive", 0),
    )
)
'
