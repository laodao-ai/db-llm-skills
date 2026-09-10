#!/bin/bash
# Ad-hoc read-only query entry point (design.md DD-5, specs/query-ro/spec.md
# REQ-QR-1..QR-4). Wraps shared/ro-session.sh: checks .dbmeta/ exists (a
# proxy for "the caller had a data dictionary to read before writing SQL" —
# this script cannot prove the dictionary was actually READ, only that it
# exists; SKILL.md states that honesty boundary explicitly), writes the
# result to a file under build/pg-query-ro/ instead of dumping it to the
# terminal, and prints only the path plus a short preview.
#
# Usage:
#   pg-query-ro.sh --sql '<single statement>' [--limit N | --no-limit] [--format csv|text]
#
# Exit codes are passed straight through from shared/ro-session.sh:
#   0  success — result file + .meta written, path + preview printed
#   1  psql execution error (a 42501 permission-denied gets one extra fix
#      line appended to stderr)
#   2  fail-closed (needs-human.md) — same convention: stdout gets ONLY the
#      needs-human.md path
#   3  guard rejected the statement (e.g. multi-statement)
# .dbmeta/ missing is this script's own check, also exit 2, but with its own
# "先运行 /pg-dict" message rather than needs-human.md (it isn't a credentials
# problem).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")}}"
export ROOT_DIR

RO_SESSION_SH="${RO_SESSION_SH_OVERRIDE:-${SCRIPT_DIR}/../../shared/ro-session.sh}"
RO_GUARD="${SCRIPT_DIR}/../../shared/ro_guard.py"
CONFIG_LIB="${SCRIPT_DIR}/../../shared/config.sh"

RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
warn() { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }
fail3() { echo -e "${RED}[FAIL]${NC} problem: $1" >&2; echo -e "${RED}[FAIL]${NC} cause: $2" >&2; echo -e "${RED}[FAIL]${NC} fix: $3" >&2; }

usage() {
    cat <<'USAGE'
Usage: pg-query-ro.sh --sql '<single statement>' [--limit N | --no-limit] [--format csv|text]

  --sql STMT         Inline SQL statement (single SELECT/WITH/EXPLAIN/SHOW).
  --limit N          Row cap (default: RO_DEFAULT_LIMIT from .dbllm.env).
  --no-limit         Do not cap rows.
  --format csv|text  Output format (default: csv). explain/show are forced to text.
  -h, --help         Show this help and exit.
USAGE
}

# --- argument parsing --------------------------------------------------------
SQL_TEXT=""
SQL_GIVEN=0
LIMIT=""
NO_LIMIT=0
FORMAT="csv"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sql)
            [[ $# -ge 2 ]] || { fail3 "--sql 缺少参数" "—" "传入 --sql '<statement>'"; usage >&2; exit 1; }
            SQL_TEXT="$2"; SQL_GIVEN=1; shift 2 ;;
        --limit)
            [[ $# -ge 2 ]] || { fail3 "--limit 缺少参数" "—" "传入 --limit <正整数>"; usage >&2; exit 1; }
            LIMIT="$2"; shift 2 ;;
        --no-limit)
            NO_LIMIT=1; shift ;;
        --format)
            [[ $# -ge 2 ]] || { fail3 "--format 缺少参数" "—" "传入 --format csv|text"; usage >&2; exit 1; }
            FORMAT="$2"; shift 2 ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            fail3 "未知参数 $1" "—" "参见 --help"; usage >&2; exit 1 ;;
    esac
done

if [[ "${SQL_GIVEN}" -ne 1 ]]; then
    fail3 "缺少 --sql" "pg-query-ro 只接受单条即席语句" "传入 --sql '<statement>'"
    usage >&2
    exit 1
fi
if [[ -n "${LIMIT}" && "${NO_LIMIT}" == "1" ]]; then
    fail3 "--limit 与 --no-limit 同时给出" "两者互斥" "只传其中一个"
    exit 1
fi
if [[ "${FORMAT}" != "csv" && "${FORMAT}" != "text" ]]; then
    fail3 "--format 值非法（${FORMAT}）" "只支持 csv 或 text" "传入 --format csv 或 --format text"
    exit 1
fi

# --- ① .dbmeta/ existence gate (REQ-QR-1) — no connection made yet ----------
if [[ ! -d "${ROOT_DIR}/.dbmeta" ]]; then
    fail3 \
        "${ROOT_DIR}/.dbmeta/ 不存在" \
        "pg-query-ro 要求先有数据字典可读——拼 SQL 前必须先看过表/列的真实结构，本脚本只能验字典存在、不能证明已读" \
        "先运行 /pg-dict 生成数据字典，再重跑本查询"
    exit 2
fi

if [[ ! -f "${CONFIG_LIB}" ]]; then
    echo -e "${RED}[FAIL]${NC} problem: 找不到 shared/config.sh（${CONFIG_LIB}）" >&2
    echo -e "${RED}[FAIL]${NC} cause: 本地 db-llm 安装不完整" >&2
    echo -e "${RED}[FAIL]${NC} fix: 重新跑一次 db-llm 仓的 setup.sh，或核对该仓是否完整 clone" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "${CONFIG_LIB}"
db_llm_load_config "${ROOT_DIR}/.dbmeta/.dbllm.env" || exit 1

# --- ② build/ git-ignore three-state warn (same rule as pg-readonly-setup) --
BUILD_DIR="${ROOT_DIR}/build"
RESULT_DIR="${BUILD_DIR}/pg-query-ro"
mkdir -p "${RESULT_DIR}"

git_check_ignore_warn() {
    local path="$1" rc=0
    git -C "${ROOT_DIR}" check-ignore -q "${path}" 2>/dev/null || rc=$?
    if [[ ${rc} -eq 1 ]]; then
        warn "problem: ${path} 未被 git ignore"
        warn "cause: 查询结果文件可能含业务数据，若入库会随仓库分发"
        warn "fix: 把 ${path}（或其所在目录）加入 .gitignore"
    elif [[ ${rc} -ge 2 ]]; then
        warn "无法判定 ${path} 是否被 git ignore（非 git 目录，或 git 不可用）"
    fi
}
git_check_ignore_warn "${BUILD_DIR}"

command -v python3 >/dev/null 2>&1 || {
    fail3 "python3 未安装或不在 PATH 中" "ro_guard.py 是纯 python3 stdlib 脚本" "安装 python3 后重试"
    exit 1
}

# --- resolve the candidate output filename (kind + sha8) --------------------
# This is a SEPARATE ro_guard.py invocation from the one shared/ro-session.sh
# makes internally — ro-session.sh never surfaces sha8 externally (it computes
# it but only uses reason/kind/sql/wrapped), and this script needs it to name
# the result file (design.md DD-5: "sha8 取 ro_guard JSON"). It is not a second
# implementation of the guard's judgment: ro-session.sh remains the sole
# authority on accept/reject and on the accept/reject error message — this
# call is only ever used for filename derivation, never to short-circuit the
# actual accept/reject decision below.
GUARD_LIMIT_ARGS=()
if [[ "${NO_LIMIT}" == "1" ]]; then
    GUARD_LIMIT_ARGS=(--no-limit)
else
    EFFECTIVE_LIMIT="${LIMIT:-${DBS_RO_DEFAULT_LIMIT}}"
    GUARD_LIMIT_ARGS=(--limit "${EFFECTIVE_LIMIT}")
fi

GUARD_JSON="$(printf '%s' "${SQL_TEXT}" | python3 "${RO_GUARD}" guard "${GUARD_LIMIT_ARGS[@]}" 2>/dev/null || true)"
G_KIND=""; G_SHA8=""
if [[ -n "${GUARD_JSON}" ]]; then
    eval "$(printf '%s' "${GUARD_JSON}" | python3 -c '
import json, sys, shlex
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
for key in ("kind", "sha8"):
    v = d.get(key)
    v = "" if v is None else str(v)
    print("G_%s=%s" % (key.upper(), shlex.quote(v)))
' 2>/dev/null || true)"
fi

# EXPLAIN/SHOW cannot be wrapped in COPY(...) — shared/ro-session.sh downgrades
# csv->text for these kinds and warns; mirrored here (design.md DD-3) only to
# pick the right file extension, not to duplicate ro-session's enforcement.
ACTUAL_FORMAT="${FORMAT}"
if [[ "${G_KIND}" == "explain" || "${G_KIND}" == "show" ]]; then
    ACTUAL_FORMAT="text"
fi
EXT="csv"
[[ "${ACTUAL_FORMAT}" == "text" ]] && EXT="txt"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
SHA8="${G_SHA8:-unknown}"
BASE_NAME="${TS}-${SHA8}"

# Atomic "pick a name + claim it" to close a TOCTOU race: two parallel
# invocations (e.g. fan-out LLM sub-agents, a common pattern in this repo's
# own workflow) can compute the same UTC-second+sha8 name and both pass a
# plain `[[ -e ]]` existence check before either has written the file, then
# race each other on the write (byte-interleaved corruption or a silent
# overwrite, exit 0, no error). `set -C` (noclobber) makes `: > candidate`
# fail atomically (O_EXCL semantics) if the file already exists, so only one
# process can ever claim a given name — scoped to a subshell so it doesn't
# affect this script's own later `>` redirections (e.g. META_PATH) or
# ro-session.sh's `--out` write, which still truncates the now-claimed file
# as before. .meta is derived from FINAL_PATH afterward, so it inherits the
# same uniqueness for free.
FINAL_PATH=""
N=0
while [[ -z "${FINAL_PATH}" ]]; do
    if [[ ${N} -eq 0 ]]; then
        CANDIDATE="${RESULT_DIR}/${BASE_NAME}.${EXT}"
    else
        CANDIDATE="${RESULT_DIR}/${BASE_NAME}-$((N + 1)).${EXT}"
    fi
    if ( set -C; : > "${CANDIDATE}" ) 2>/dev/null; then
        FINAL_PATH="${CANDIDATE}"
    fi
    N=$((N + 1))
done

# --- ③ delegate to ro-session.sh (single source of truth for guard/exec) ----
if [[ ! -x "${RO_SESSION_SH}" ]]; then
    echo -e "${RED}[FAIL]${NC} problem: 找不到可执行的 ro-session.sh（${RO_SESSION_SH}）" >&2
    echo -e "${RED}[FAIL]${NC} cause: 本地 db-llm 安装不完整，或 RO_SESSION_SH_OVERRIDE 指向了错误路径" >&2
    echo -e "${RED}[FAIL]${NC} fix: 重新执行 db-llm 仓的 setup.sh 后重跑" >&2
    exit 1
fi

RS_LIMIT_ARGS=()
if [[ "${NO_LIMIT}" == "1" ]]; then
    RS_LIMIT_ARGS=(--no-limit)
elif [[ -n "${LIMIT}" ]]; then
    RS_LIMIT_ARGS=(--limit "${LIMIT}")
fi

RS_STDOUT="$(mktemp)"
cleanup() { rm -f "${RS_STDOUT}" "${RS_STDERR:-}"; }
trap cleanup EXIT
RS_STDERR="$(mktemp)"

START_NS="$(date +%s%N 2>/dev/null || echo 0)"
set +e
"${RO_SESSION_SH}" --sql "${SQL_TEXT}" "${RS_LIMIT_ARGS[@]+"${RS_LIMIT_ARGS[@]}"}" --format "${FORMAT}" --out "${FINAL_PATH}" \
    >"${RS_STDOUT}" 2>"${RS_STDERR}"
RS_RC=$?
set -e
END_NS="$(date +%s%N 2>/dev/null || echo 0)"
ELAPSED_MS=$(( (END_NS - START_NS) / 1000000 ))
[[ ${ELAPSED_MS} -lt 0 ]] && ELAPSED_MS=0

# Forward ro-session's stderr (already redacted) verbatim — includes any
# EXPLAIN/csv-downgrade warning and, on success, the RO_TRUNCATED=/RO_ROWS=
# trailer this script parses below.
if [[ -s "${RS_STDERR}" ]]; then
    cat "${RS_STDERR}" >&2
fi

if [[ ${RS_RC} -eq 2 ]]; then
    # ro-session.sh already wrote needs-human.md and printed ONLY that path.
    cat "${RS_STDOUT}"
    exit 2
fi
if [[ ${RS_RC} -eq 3 ]]; then
    exit 3
fi
if [[ ${RS_RC} -eq 1 ]]; then
    # psql's DEFAULT verbosity never prints the bare SQLSTATE code (confirmed
    # against a real PG 18.4: `ERROR:  permission denied for table ...`, no
    # "42501" substring) — shared/ro-session.sh doesn't set VERBOSITY=sqlstate
    # (out of this ticket's scope to change), so detection matches on the
    # English message text instead. This mirrors the same-shaped precedent
    # already in shared/ro-session.sh, which detects 53300/auth-failure the
    # same way (message substrings, not SQLSTATE codes).
    if grep -qi 'permission denied' "${RS_STDERR}" 2>/dev/null; then
        echo -e "${YELLOW}[WARN]${NC} fix: 重跑 /pg-readonly-setup 核对授权范围；若该表由非供给账号创建，还需 owner 登记默认权限（ALTER DEFAULT PRIVILEGES）" >&2
    fi
    exit 1
fi
if [[ ${RS_RC} -ne 0 ]]; then
    # Any exit code other than the three enumerated above (0 success handled
    # below, 1/2/3 already handled) MUST NOT fall through to the success
    # branch. This catches e.g. 130/143 (psql killed by a signal — the result
    # file may already contain a partial COPY write) and 126/127 (exec
    # failure) — none of those mean "success", and this script's own header
    # comment documents only 0/1/2/3 as valid exit codes, so an unenumerated
    # ro-session.sh code is reported as a generic failure rather than
    # propagated verbatim.
    fail3 \
        "ro-session.sh 返回未预期的退出码 ${RS_RC}" \
        "既非 0（成功）也非已知的 1/2/3（psql 执行错误 / fail-closed / guard 拒绝）——可能是进程被信号杀死或 shell 层异常退出，此时 ${FINAL_PATH} 可能已写入不完整数据" \
        "核对上方 stderr 输出；不要使用 ${FINAL_PATH}（若已生成）作为查询结果，重跑本次查询"
    exit 1
fi

# --- ④ success: write .meta, print path + preview (REQ-QR-3) ----------------
RO_ROWS="$(grep -o 'RO_ROWS=[0-9]*' "${RS_STDERR}" | tail -1 | cut -d= -f2)"
RO_TRUNCATED="$(grep -o 'RO_TRUNCATED=[a-z]*' "${RS_STDERR}" | tail -1 | cut -d= -f2)"
RO_ROWS="${RO_ROWS:-0}"
RO_TRUNCATED="${RO_TRUNCATED:-false}"
LIMIT_META="none"
[[ "${NO_LIMIT}" != "1" ]] && LIMIT_META="${LIMIT:-${DBS_RO_DEFAULT_LIMIT}}"

META_PATH="${FINAL_PATH}.meta"
# .meta is a fixed 6-line key:value format (SKILL.md contract). SQL_TEXT may
# contain embedded newlines (multi-line SELECT/CTE formatting passes
# shared/ro_guard.py's guard — it only rejects top-level semicolons and
# backslash meta-commands, not newlines), which would otherwise split the
# `sql:` value across lines and shift the 5 keys after it. Escape to a single
# line, reversibly: backslash MUST be escaped first (SQL string literals can
# contain literal backslashes, e.g. 'a\b') so the escaped backslash-n/
# backslash-r sequences introduced by the next two substitutions are never
# ambiguous with a backslash that was already in the SQL text.
SQL_META="${SQL_TEXT//\\/\\\\}"
SQL_META="${SQL_META//$'\n'/\\n}"
SQL_META="${SQL_META//$'\r'/\\r}"
{
    printf 'sql: %s\n' "${SQL_META}"
    printf 'rows: %s\n' "${RO_ROWS}"
    printf 'truncated: %s\n' "${RO_TRUNCATED}"
    printf 'limit: %s\n' "${LIMIT_META}"
    printf 'elapsed_ms: %s\n' "${ELAPSED_MS}"
    printf 'format: %s\n' "${ACTUAL_FORMAT}"
} > "${META_PATH}"

echo "${FINAL_PATH}"

# Preview: header + up to 19 data records, computed via the SAME ro_guard.py
# record-aware truncator used for the real result (not `head -n 20`, which
# would split a multi-line-quoted CSV field mid-record).
python3 "${RO_GUARD}" truncate --limit 19 --format "${ACTUAL_FORMAT}" < "${FINAL_PATH}" 2>/dev/null || true

if [[ "${RO_TRUNCATED}" == "true" ]]; then
    echo "… (truncated, see .meta)"
fi

exit 0
