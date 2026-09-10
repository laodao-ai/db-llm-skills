#!/usr/bin/env bash
# pg-sql-check: validate a single SQL statement against the real schema
# without executing it, via shared/ro-session.sh's PREPARE-only mode
# (ADR-0007, design.md "数据流图" / "退出码契约", tasks.md group 3-4,
# specs/sql-check/spec.md REQ-SC-1..SC-4).
#
# Task 3 (S3) wired the tracer-bullet path "one bad-column SQL -> exit 4 +
# SQLSTATE" end to end. Task 4 (S4, this revision) adds: the stdout
# human-readable diagnostic summary (verdict/SQLSTATE/message/HINT/position/
# candidates placeholder), position translation from the injected
# "PREPARE <name> AS <sql>" coordinates back to the user's original text,
# the machine-readable JSON artifact under build/pg-sql-check/, and the
# contract snapshot's null-vs-[]-vs-missing distinction for result_types.
# It does NOT implement candidate-name fuzzy matching (S5, design.md's
# slicing table) — the "candidates" field is always null in this revision,
# a reserved interface for Task 5 to fill in.
#
# Usage:
#   pg-sql-check.sh --sql '<single statement>'
#
# Exit codes (design.md "退出码契约"):
#   0  validation passed — prints the parameter_types/result_types contract
#      snapshot
#   1  hard error: bad arguments, missing .dbmeta/shared/ro-session.sh
#      installation, PG < 16, transaction-pool topology, SQLSTATE could not
#      be extracted, 42P18 (indeterminate placeholder type), or any other
#      non-class-42 psql failure
#   2  fail-closed, needs human: ro-session.sh's own needs-human.md path
#      (credentials / current_user mismatch / connection refused), .dbmeta/
#      missing (fix points to /pg-dict), or SQLSTATE 42501 (insufficient
#      privilege) — further split into "schema is in .dbmeta/ scope" (fix:
#      re-run /pg-readonly-setup) vs "schema not in scope" (fix: add it to
#      SCHEMAS first)
#   3  shared/ro_guard.py rejected the statement (multi-statement /
#      meta-command / not-whitelisted) — passed straight through
#   4  validation FAILED — the SQL does not match the real schema (SQLSTATE
#      class 42, excluding 42501 and 42P18). This is pg-sql-check's core,
#      expected output: "SQL 有问题" is normal operation, not an exception.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")}}"
export ROOT_DIR

RO_SESSION_SH="${RO_SESSION_SH_OVERRIDE:-${SCRIPT_DIR}/../../shared/ro-session.sh}"
# Used ONLY to derive a filename sha8 for the JSON artifact (write_diag_json
# below) — the exact same "separate guard call, never the accept/reject
# authority" pattern pg-query-ro.sh already uses and documents at its own
# call site. shared/ro-session.sh remains the sole judge of accept/reject.
RO_GUARD="${RO_GUARD_OVERRIDE:-${SCRIPT_DIR}/../../shared/ro_guard.py}"
CANDIDATE_MATCH_PY="${CANDIDATE_MATCH_PY_OVERRIDE:-${SCRIPT_DIR}/candidate_match.py}"

RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
warn() { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }
fail3() { echo -e "${RED}[FAIL]${NC} problem: $1" >&2; echo -e "${RED}[FAIL]${NC} cause: $2" >&2; echo -e "${RED}[FAIL]${NC} fix: $3" >&2; }

# --- diagnostic double-output helpers (Task 4, REQ-SC-4/SC-5) --------------
# DIAG_* globals are set by callers right before invoking write_diag_json /
# print_diag_stdout, so both functions read the SAME judgment — the dual
# output's consistency is structural (one source, two renderers), not
# something asserted after the fact.

# Extracts a PostgreSQL HINT (if any) from the (already redacted by
# shared/ro-session.sh) stderr capture. Sets DIAG_HINT (empty if absent).
extract_pg_hint() {
    local hint_line=""
    hint_line="$(grep -m1 -E '^HINT:[[:space:]]*' "${RS_STDERR}" 2>/dev/null || true)"
    if [[ -n "${hint_line}" ]]; then
        DIAG_HINT="$(printf '%s' "${hint_line}" | sed -E 's/^HINT:[[:space:]]*//')"
    else
        DIAG_HINT=""
    fi
}

# Extracts PostgreSQL's LINE n + caret (if any) and translates it back to
# the user's original SQL coordinates (design.md 可观测性 / specs/sql-check
# REQ-SC-4). PostgreSQL echoes the exact statement text it received, so the
# "PREPARE <random-name> AS " prefix length is recovered from that echoed
# text via regex — this script never learns ro-session.sh's randomized
# prepared-statement name any other way.
#
# The printed "LINE n: <stmt-line>" text's OWN length (prefix included) is
# what the caret is aligned under (confirmed against a real PG 18.6
# VERBOSITY=verbose sample, see impl-report) — not just <stmt-line> alone.
# Only PostgreSQL's line 1 carries the injected "PREPARE ... AS " text
# (ro-session.sh's SQL_SCRIPT_LINES puts it and the start of the user's SQL
# on one line); every subsequent line is the user's text verbatim, so a
# line-1 hit needs the extra prefix subtracted and line 1 IS the user's
# line 1, while a line n>1 hit is already the user's line n unmodified.
#
# Sets DIAG_POS_MODE to one of:
#   none      - PostgreSQL gave no LINE/caret at all (many SQLSTATEs, e.g.
#               42501, never carry a position)
#   raw       - LINE/caret present but the offset could not be converted
#               back to the user's coordinates (regex mismatch, or the
#               computed column would be non-positive) — DIAG_POS_LINE/COL
#               hold the UNCONVERTED (injected-script-relative) coordinates
#   converted - DIAG_POS_LINE/COL hold the user's original coordinates
extract_pg_position() {
    DIAG_POS_MODE="none"
    DIAG_POS_LINE=""
    DIAG_POS_COL=""

    local block line_text caret_text pg_line stmt_text prefix prefix_len
    block="$(grep -A1 -m1 -E '^LINE [0-9]+:' "${RS_STDERR}" 2>/dev/null || true)"
    [[ -z "${block}" ]] && return 0

    line_text="$(printf '%s\n' "${block}" | sed -n '1p')"
    caret_text="$(printf '%s\n' "${block}" | sed -n '2p')"

    [[ "${line_text}" =~ ^LINE\ ([0-9]+):\ (.*)$ ]] || return 0
    pg_line="${BASH_REMATCH[1]}"
    stmt_text="${BASH_REMATCH[2]}"
    prefix="LINE ${pg_line}: "
    prefix_len=${#prefix}

    [[ "${caret_text}" == *"^"* ]] || return 0
    local caret_col_printed stmt_col
    caret_col_printed="$(awk -v s="${caret_text}" 'BEGIN{print index(s, "^")}')"
    stmt_col=$(( caret_col_printed - prefix_len ))

    if [[ "${pg_line}" == "1" ]]; then
        if [[ "${stmt_text}" =~ ^PREPARE[[:space:]]+[^[:space:]]+[[:space:]]+AS[[:space:]] ]]; then
            local prep_prefix pp_len user_col
            prep_prefix="${BASH_REMATCH[0]}"
            pp_len=${#prep_prefix}
            user_col=$(( stmt_col - pp_len ))
            if [[ ${user_col} -ge 1 ]]; then
                DIAG_POS_MODE="converted"; DIAG_POS_LINE=1; DIAG_POS_COL=${user_col}
                return 0
            fi
        fi
        DIAG_POS_MODE="raw"; DIAG_POS_LINE="${pg_line}"; DIAG_POS_COL="${stmt_col}"
        return 0
    fi

    if [[ ${stmt_col} -ge 1 ]]; then
        DIAG_POS_MODE="converted"; DIAG_POS_LINE="${pg_line}"; DIAG_POS_COL="${stmt_col}"
    else
        DIAG_POS_MODE="raw"; DIAG_POS_LINE="${pg_line}"; DIAG_POS_COL="${stmt_col}"
    fi
}

# Writes build/pg-sql-check/<UTC-ts>-<sha8>.json from the DIAG_* globals the
# caller has already set (DIAG_SQLSTATE/DIAG_MESSAGE/DIAG_HINT/DIAG_POS_MODE/
# DIAG_POS_LINE/DIAG_POS_COL/DIAG_PARAM_RAW/DIAG_RESULT_RAW — all may be
# empty). Sets DIAG_JSON_PATH to the claimed path. Concurrent same-second,
# same-SQL calls are raced via `set -C` (noclobber) exactly like
# pg-query-ro.sh's FINAL_PATH claim loop — first writer of a given
# ts-sha8[-N] name wins, a collision retries with a numeric suffix.
write_diag_json() {
    local guard_json sha8 ts base candidate final n
    guard_json="$(printf '%s' "${SQL_TEXT}" | python3 "${RO_GUARD}" guard --prepare --no-limit 2>/dev/null || true)"
    sha8="unknown"
    if [[ -n "${guard_json}" ]]; then
        sha8="$(printf '%s' "${guard_json}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get("sha8") or "unknown")
' 2>/dev/null || echo unknown)"
    fi

    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    base="${ts}-${sha8}"
    final=""
    n=0
    while [[ -z "${final}" ]]; do
        if [[ ${n} -eq 0 ]]; then
            candidate="${RESULT_DIR}/${base}.json"
        else
            candidate="${RESULT_DIR}/${base}-$((n + 1)).json"
        fi
        if ( set -C; : > "${candidate}" ) 2>/dev/null; then
            final="${candidate}"
        fi
        n=$((n + 1))
    done
    DIAG_JSON_PATH="${final}"

    DIAG_SQLSTATE="${DIAG_SQLSTATE:-}" \
    DIAG_MESSAGE="${DIAG_MESSAGE:-}" \
    DIAG_HINT="${DIAG_HINT:-}" \
    DIAG_POS_MODE="${DIAG_POS_MODE:-none}" \
    DIAG_POS_LINE="${DIAG_POS_LINE:-}" \
    DIAG_POS_COL="${DIAG_POS_COL:-}" \
    DIAG_PARAM_RAW="${DIAG_PARAM_RAW:-}" \
    DIAG_RESULT_RAW="${DIAG_RESULT_RAW:-}" \
    DIAG_CANDIDATES_JSON="${DIAG_CANDIDATES_JSON:-}" \
    python3 - "${final}" <<'PYEOF'
import json
import os
import sys

path = sys.argv[1]


def env(name):
    return os.environ.get(name, "")


def parse_pg_array(raw):
    # PG's external array text form, e.g. "{integer,text}" / "{}". Type
    # names never contain the array delimiter in practice (no built-in or
    # commonly-used type name has an embedded comma), so a quote-aware
    # linear scan (handling PG's own "..."-quoting/backslash-escaping of
    # array elements, used for e.g. empty-string or NULL-literal elements)
    # is sufficient without a full grammar.
    if raw == "":
        return None
    s = raw.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    inner = s[1:-1]
    if inner == "":
        return []
    items = []
    cur = ""
    in_quotes = False
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if c == "\\" and i + 1 < len(inner):
            cur += inner[i + 1]
            i += 2
            continue
        if c == "," and not in_quotes:
            items.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    items.append(cur)
    return items


sqlstate = env("DIAG_SQLSTATE") or None
message = env("DIAG_MESSAGE") or None
hint = env("DIAG_HINT") or None

pos_mode = env("DIAG_POS_MODE")
position = None
if pos_mode == "converted":
    position = {"line": int(env("DIAG_POS_LINE")), "column": int(env("DIAG_POS_COL"))}
elif pos_mode == "raw":
    position = {
        "raw_line": int(env("DIAG_POS_LINE")),
        "raw_column": int(env("DIAG_POS_COL")),
        "note": "位置相对注入脚本，无法换算回用户原文",
    }

param_raw = env("DIAG_PARAM_RAW")
result_raw = env("DIAG_RESULT_RAW")
parameter_types = parse_pg_array(param_raw) if param_raw != "" else None
result_types = parse_pg_array(result_raw) if result_raw != "" else None

candidates_json = env("DIAG_CANDIDATES_JSON")
candidates = None
if candidates_json:
    try:
        cj = json.loads(candidates_json)
        candidates = cj.get("candidates") if isinstance(cj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass

doc = {
    "sqlstate": sqlstate,
    "message": message,
    "hint": hint,
    "position": position,
    "parameter_types": parameter_types,
    "result_types": result_types,
    "candidates": candidates,
}

with open(path, "w") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
    f.write("\n")
PYEOF
}

# Prints the stdout human-readable summary (design.md 可观测性 / REQ-SC-4).
# Reads the same DIAG_* globals write_diag_json just consumed, plus
# DIAG_JSON_PATH it set — so stdout and the JSON artifact are two renderings
# of one judgment, never two independently-computed ones.
print_diag_stdout() {
    local verdict="$1" fix="${2:-}"
    echo "判定: ${verdict}"
    echo "SQLSTATE: ${DIAG_SQLSTATE:-（无）}"
    echo "消息: ${DIAG_MESSAGE:-（无）}"
    if [[ -n "${DIAG_HINT:-}" ]]; then
        echo "HINT: ${DIAG_HINT}"
    fi
    case "${DIAG_POS_MODE:-none}" in
        converted)
            echo "位置: 第 ${DIAG_POS_LINE} 行第 ${DIAG_POS_COL} 列（已换算回用户原文坐标）"
            ;;
        raw)
            echo "位置: 第 ${DIAG_POS_LINE} 行第 ${DIAG_POS_COL} 列（位置相对注入脚本，无法换算回用户原文）"
            ;;
        *)
            echo "位置: （PostgreSQL 本次未提供位置信息）"
            ;;
    esac
    echo "候选名: （本版本尚未启用候选名匹配，见 docs/skills-roadmap.md，Task 5 实现）"
    if [[ -n "${fix}" ]]; then
        echo "建议: ${fix}"
    fi
    if [[ -n "${DIAG_JSON_PATH:-}" ]]; then
        echo "诊断详情: ${DIAG_JSON_PATH}"
    fi
}

usage() {
    cat <<'USAGE'
Usage: pg-sql-check.sh --sql '<single statement>'

  --sql STMT   Inline SQL statement to validate (never executed — only
               PREPARE'd against the real schema, then rolled back).
  -h, --help   Show this help and exit.
USAGE
}

# --- argument parsing --------------------------------------------------------
SQL_TEXT=""
SQL_GIVEN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sql)
            [[ $# -ge 2 ]] || { fail3 "--sql 缺少参数" "—" "传入 --sql '<statement>'"; usage >&2; exit 1; }
            SQL_TEXT="$2"; SQL_GIVEN=1; shift 2 ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            fail3 "未知参数 $1" "—" "参见 --help"; usage >&2; exit 1 ;;
    esac
done

if [[ "${SQL_GIVEN}" -ne 1 ]]; then
    fail3 "缺少 --sql" "pg-sql-check 只接受单条即席语句" "传入 --sql '<statement>'"
    usage >&2
    exit 1
fi

# --- ① .dbmeta/ existence gate (REQ-SC-1) — no connection made yet ---------
if [[ ! -d "${ROOT_DIR}/.dbmeta" ]]; then
    fail3 \
        "${ROOT_DIR}/.dbmeta/ 不存在" \
        "pg-sql-check 要靠数据字典判定候选名/权限范围——拼 SQL 前必须先有字典" \
        "先运行 /pg-dict 生成数据字典，再重跑本次校验"
    exit 2
fi

if [[ ! -x "${RO_SESSION_SH}" ]]; then
    fail3 "找不到可执行的 ro-session.sh（${RO_SESSION_SH}）" \
          "本地 db-llm 安装不完整，或 RO_SESSION_SH_OVERRIDE 指向了错误路径" \
          "重新执行 db-llm 仓的 setup.sh 后重跑"
    exit 1
fi

# --- ② build/ git-ignore three-state warn (same rule as pg-query-ro) -------
BUILD_DIR="${ROOT_DIR}/build"
RESULT_DIR="${BUILD_DIR}/pg-sql-check"
mkdir -p "${RESULT_DIR}"

git_check_ignore_warn() {
    local path="$1" rc=0
    git -C "${ROOT_DIR}" check-ignore -q "${path}" 2>/dev/null || rc=$?
    if [[ ${rc} -eq 1 ]]; then
        warn "problem: ${path} 未被 git ignore"
        warn "cause: 校验产物可能含业务字面量或 schema 结构细节，若入库会随仓库分发"
        warn "fix: 把 ${path}（或其所在目录）加入 .gitignore"
    elif [[ ${rc} -ge 2 ]]; then
        warn "无法判定 ${path} 是否被 git ignore（非 git 目录，或 git 不可用）"
    fi
}
git_check_ignore_warn "${BUILD_DIR}"

# --- cleanup -----------------------------------------------------------
CLEANUP_FILES=()
cleanup() {
    if [[ ${#CLEANUP_FILES[@]} -gt 0 ]]; then
        rm -f "${CLEANUP_FILES[@]}"
    fi
}
trap cleanup EXIT

# --- ③ PG version gate (REQ-SC-2): SHOW server_version_num over the SAME ---
# executor, through its ordinary whitelisted SELECT/SHOW path (SHOW is in
# ro_guard.py's WHITELIST_KEYWORDS) — not a second, independent psql
# connection this script would have to manage credentials/tunnel for itself.
# ro-session.sh remains the sole connection authority (design.md 组件清单).
VER_STDOUT="$(mktemp)"; CLEANUP_FILES+=("${VER_STDOUT}")
VER_STDERR="$(mktemp)"; CLEANUP_FILES+=("${VER_STDERR}")

set +e
"${RO_SESSION_SH}" --sql "SHOW server_version_num" --no-limit >"${VER_STDOUT}" 2>"${VER_STDERR}"
VER_RC=$?
set -e

if [[ ${VER_RC} -eq 2 ]]; then
    # ro-session.sh already wrote needs-human.md and printed ONLY that path.
    cat "${VER_STDOUT}"
    exit 2
fi
if [[ ${VER_RC} -ne 0 ]]; then
    # 0 handled below; 2 handled above. 3 (guard rejected a fixed literal
    # SHOW statement) should never happen — treated as a hard error rather
    # than silently guessing, same as any other unenumerated code.
    cat "${VER_STDERR}" >&2
    fail3 "无法探测目标库版本" "ro-session.sh 返回退出码 ${VER_RC}（预期 0 或 2）" "核对上方 stderr 输出"
    exit 1
fi

VERSION_NUM="$(tr -d '[:space:]' < "${VER_STDOUT}")"
if ! [[ "${VERSION_NUM}" =~ ^[0-9]+$ ]]; then
    fail3 "无法解析目标库版本号" "SHOW server_version_num 的输出（${VERSION_NUM}）不是纯数字" "核对上方连接是否正常"
    exit 1
fi
PG_MAJOR=$(( VERSION_NUM / 10000 ))
if [[ ${PG_MAJOR} -lt 16 ]]; then
    fail3 "目标库 PostgreSQL 主版本号为 ${PG_MAJOR}，pg-sql-check 要求 >= 16" \
          "契约快照读取 pg_prepared_statements 的 result_types 列，该列 PG16 起才存在（PG15 及更早无此列）" \
          "升级目标库至 PG16 及以上后重试"
    exit 1
fi

# --- ④ delegate to ro-session.sh --prepare (REQ-SC-1 / REQ-SC-2) -----------
# --no-limit is passed alongside --prepare even though --prepare already
# forces it internally (ro-session.sh:117-ish) — this call site does not
# rely on that internal default so the intent reads standalone here too.
RS_STDOUT="$(mktemp)"; CLEANUP_FILES+=("${RS_STDOUT}")
RS_STDERR="$(mktemp)"; CLEANUP_FILES+=("${RS_STDERR}")

set +e
"${RO_SESSION_SH}" --sql "${SQL_TEXT}" --prepare --no-limit >"${RS_STDOUT}" 2>"${RS_STDERR}"
RS_RC=$?
set -e

if [[ ${RS_RC} -eq 2 ]]; then
    cat "${RS_STDOUT}"
    exit 2
fi
if [[ ${RS_RC} -eq 3 ]]; then
    cat "${RS_STDERR}" >&2
    exit 3
fi

if [[ ${RS_RC} -eq 0 ]]; then
    # --- ⑤ connection-topology gate (REQ-SC-2): a PgBouncer *transaction*
    # pool can let PREPARE + ROLLBACK succeed (rc=0) while routing the
    # read-back SELECT to a different backend than the one that ran
    # PREPARE — PgBouncer's own feature matrix marks SQL-level PREPARE/
    # DEALLOCATE as "Never" supported under transaction pooling. The
    # observable symptom is exactly this: rc=0 but the snapshot query
    # returned zero rows. MUST NOT be reported as "passed with an empty
    # snapshot" (design.md 失败模式表) — that would be silently wrong.
    if [[ ! -s "${RS_STDOUT}" ]]; then
        fail3 "校验返回了空快照" \
              "很可能经由 PgBouncer transaction 池连接——SQL 级 PREPARE/DEALLOCATE 在该池模式下不受支持（PgBouncer 官方特性矩阵标为 Never），预备语句与读回查询被路由到了不同后端连接" \
              "改用 PgBouncer session 池（本仓默认）或直连数据库后重试"
        exit 1
    fi
    # --- contract snapshot + diagnostic JSON (REQ-SC-5) --- psql -A -t
    # (unaligned, tuples-only) prints the two selected columns separated by
    # the default '|' field separator on one line. A SQL NULL renders as an
    # empty string in this mode — result_types genuinely IS NULL for
    # statements with no result set (design.md 契约快照 / REQ-SC-5: PG's own
    # docs say so for DML), so an empty second field here reliably means
    # JSON null, never [] and never a missing key.
    SNAPSHOT_LINE="$(head -n1 "${RS_STDOUT}")"
    if [[ "${SNAPSHOT_LINE}" == *"|"* ]]; then
        DIAG_PARAM_RAW="${SNAPSHOT_LINE%%|*}"
        DIAG_RESULT_RAW="${SNAPSHOT_LINE#*|}"
    else
        DIAG_PARAM_RAW=""
        DIAG_RESULT_RAW=""
    fi
    DIAG_SQLSTATE=""
    DIAG_MESSAGE=""
    DIAG_HINT=""
    DIAG_POS_MODE="none"
    DIAG_POS_LINE=""
    DIAG_POS_COL=""
    write_diag_json

    echo "校验通过 —— 契约快照 (parameter_types|result_types):"
    cat "${RS_STDOUT}"
    echo "诊断详情: ${DIAG_JSON_PATH}"
    exit 0
fi

if [[ ${RS_RC} -ne 1 ]]; then
    fail3 "ro-session.sh 返回未预期的退出码 ${RS_RC}" \
          "既非 0/1/2/3 中已知的成功或失败形态" \
          "核对上方 stderr 输出，不要信任本次结果"
    cat "${RS_STDERR}" >&2
    exit 1
fi

# --- ⑥ RS_RC == 1: extract SQLSTATE from the first error frame -------------
# ON_ERROR_STOP halts psql at the first failing statement, so the injected
# script's tool statements (current_user check / BEGIN / SET / DEALLOCATE /
# ROLLBACK) and the user's PREPARE can never both surface an error in the
# same run — there is exactly one frame to read. `\set VERBOSITY verbose`
# (set unconditionally in --prepare mode by ro-session.sh) makes psql print
# "ERROR:  <SQLSTATE>: <message>" instead of the default "ERROR:  <message>"
# with no code at all — this MUST be relied on and asserted, not assumed
# from "there's only one ERROR line" (design.md 可观测性).
FULL_ERR_LINE="$(grep -m1 -E 'ERROR:[[:space:]]+[0-9A-Z]{5}:' "${RS_STDERR}" 2>/dev/null || true)"
# Captured via the group in the same anchored pattern (not a bare
# `grep -oE '[0-9A-Z]{5}'` over the whole line) — the literal word "ERROR"
# is itself five uppercase letters and would otherwise match first.
SQLSTATE="$(printf '%s' "${FULL_ERR_LINE}" | sed -E 's/^.*ERROR:[[:space:]]+([0-9A-Z]{5}):.*/\1/')"
[[ "${SQLSTATE}" == "${FULL_ERR_LINE}" ]] && SQLSTATE=""

if ! [[ "${SQLSTATE}" =~ ^[0-9A-Z]{5}$ ]]; then
    fail3 "无法从 psql 输出中提取 SQLSTATE" \
          "首帧错误未匹配到 'ERROR:  <5 位 SQLSTATE>:' 形态，格式不符合预期或存在多帧歧义" \
          "核对下方 stderr 原文；MUST NOT 猜测退出码"
    cat "${RS_STDERR}" >&2
    exit 1
fi

PG_MESSAGE="$(printf '%s' "${FULL_ERR_LINE}" | sed -E 's/^.*ERROR:[[:space:]]+[0-9A-Z]{5}:[[:space:]]*//')"
SQLSTATE_CLASS="${SQLSTATE:0:2}"

if [[ "${SQLSTATE_CLASS}" != "42" ]]; then
    fail3 "校验时发生硬错误（SQLSTATE ${SQLSTATE}）" \
          "${PG_MESSAGE}" \
          "核对下方 stderr 原文"
    cat "${RS_STDERR}" >&2
    exit 1
fi

# --- diagnostic double-output setup (REQ-SC-4): one judgment, two renders --
DIAG_SQLSTATE="${SQLSTATE}"
DIAG_MESSAGE="${PG_MESSAGE}"
DIAG_PARAM_RAW=""
DIAG_RESULT_RAW=""
extract_pg_hint
extract_pg_position

# --- candidate-name completion (REQ-SC-6) ------------------------------------
render_candidates() {
    local mode="$1" identifier="$2"
    DIAG_CANDIDATES_JSON=""
    if [[ ! -f "${CANDIDATE_MATCH_PY}" ]]; then
        printf '（候选名补全不可用：找不到 %s）\n' "${CANDIDATE_MATCH_PY}"
        return
    fi
    # JSON output for write_diag_json (dual-output consistency, REQ-SC-4)
    local json_out=""
    json_out="$(python3 "${CANDIDATE_MATCH_PY}" \
        --dbmeta-root "${ROOT_DIR}/.dbmeta" \
        --mode "${mode}" \
        --identifier "${identifier}" \
        --format json 2>/dev/null)" || json_out=""
    DIAG_CANDIDATES_JSON="${json_out}"
    # Text output for stdout summary (guarded against errexit, same as above)
    local out=""
    out="$(python3 "${CANDIDATE_MATCH_PY}" \
        --dbmeta-root "${ROOT_DIR}/.dbmeta" \
        --mode "${mode}" \
        --identifier "${identifier}" \
        --format text 2>/dev/null)" || out=""
    printf '%s' "${out}"
}

# --- ⑦ class-42 exit-code fan-out (REQ-SC-3) --------------------------------
case "${SQLSTATE}" in
    42703)
        # REQ-SC-6: PG gave HINT → present as-is, don't replace with candidates
        if [[ -n "${DIAG_HINT}" ]]; then
            fail3 "SQL 引用了不存在的列（SQLSTATE 42703）" \
                  "${PG_MESSAGE}" \
                  "PostgreSQL 提示：${DIAG_HINT}"
            write_diag_json
            print_diag_stdout "SQL 引用了不存在的列" "PostgreSQL 提示：${DIAG_HINT}"
            exit 4
        fi
        COL_IDENT=""
        if [[ "${PG_MESSAGE}" =~ column\ \"([^\"]+)\" ]]; then
            COL_IDENT="${BASH_REMATCH[1]}"
        fi
        if [[ -z "${COL_IDENT}" ]]; then
            fail3 "SQL 引用了不存在的列（SQLSTATE 42703）" \
                  "${PG_MESSAGE}" \
                  "无法从错误消息中提取列名（非预期格式，可能是非英文 locale），不提供候选名；核对 SQL 中引用的列是否与 .dbmeta/ 记录的真实结构一致"
            write_diag_json
            print_diag_stdout "SQL 引用了不存在的列（列名提取失败，不提供候选名）" \
                "核对 SQL 中引用的列是否与 .dbmeta/ 记录的真实结构一致"
            exit 4
        fi
        DIAG_CANDIDATES="$(render_candidates column "${COL_IDENT}")"
        fail3 "SQL 引用了不存在的列（SQLSTATE 42703）" \
              "${PG_MESSAGE}" \
              "候选列名（取自 .dbmeta/，可能滞后于活库，必要时重跑 /pg-dict）：
${DIAG_CANDIDATES}"
        write_diag_json
        print_diag_stdout "SQL 引用了不存在的列" \
            "候选列名（取自 .dbmeta/）：${DIAG_CANDIDATES}"
        exit 4
        ;;
    42P01)
        TBL_IDENT=""
        if [[ "${PG_MESSAGE}" =~ relation\ \"([^\"]+)\" ]]; then
            TBL_IDENT="${BASH_REMATCH[1]}"
        fi
        if [[ -z "${TBL_IDENT}" ]]; then
            fail3 "SQL 引用了不存在的表/schema（SQLSTATE 42P01）" \
                  "${PG_MESSAGE}" \
                  "无法从错误消息中提取标识符（非预期格式，可能是非英文 locale），不提供候选名；核对 SQL 中引用的表/schema 是否与 .dbmeta/ 记录的真实结构一致"
            write_diag_json
            print_diag_stdout "SQL 引用了不存在的表/schema（标识符提取失败，不提供候选名）" \
                "核对 SQL 中引用的表/schema 是否与 .dbmeta/ 记录的真实结构一致"
            exit 4
        fi
        DIAG_CANDIDATES="$(render_candidates table "${TBL_IDENT}")"
        fail3 "SQL 引用了不存在的表/schema（SQLSTATE 42P01）" \
              "${PG_MESSAGE}" \
              "候选表/schema 名（取自 .dbmeta/，可能滞后于活库，必要时重跑 /pg-dict）：
${DIAG_CANDIDATES}"
        write_diag_json
        print_diag_stdout "SQL 引用了不存在的表/schema" \
            "候选表/schema 名（取自 .dbmeta/）：${DIAG_CANDIDATES}"
        exit 4
        ;;
    42501)
        # Off-ticket finding (see impl-report): shared/ro-session.sh's
        # --prepare injection script runs `SET LOCAL lc_messages='C'`
        # unconditionally, but lc_messages' GUC context is `superuser` — a
        # genuinely non-superuser read-only role (the only kind ADR-0006
        # allows this toolchain to hold) gets 42501 on THAT statement, not
        # on the user's SQL. Detected first and reported distinctly so it
        # is never conflated with a real schema/table permission gap.
        if [[ "${PG_MESSAGE}" == *'permission denied to set parameter "lc_messages"'* ]]; then
            fail3 "只读角色缺少设置 lc_messages 的权限（SQLSTATE 42501）" \
                  "pg-sql-check 依赖会话内 SET LOCAL lc_messages='C' 让错误消息保持英文可解析，但该角色未被授予此权限（lc_messages 的 GUC context 为 superuser）" \
                  "以 DBA 身份对只读角色执行: GRANT SET ON PARAMETER lc_messages TO <只读角色名>;（PG15+ 特性，不提升角色权限，仅授予设置该参数的能力）"
            write_diag_json
            print_diag_stdout "权限不足（lc_messages 参数设置权限缺失）" \
                "以 DBA 身份对只读角色执行: GRANT SET ON PARAMETER lc_messages TO <只读角色名>;（PG15+ 特性，不提升角色权限，仅授予设置该参数的能力）"
            exit 2
        fi
        # Table-level denial messages ("permission denied for table X") do
        # not include the schema name (design.md C10) — only schema-level
        # denial ("permission denied for schema X") does. When extractable,
        # cross-check against .dbmeta/<schema>/ to disambiguate "supply
        # problem" (schema is in scope, /pg-readonly-setup under-granted)
        # from "not onboarded" (schema was never added to SCHEMAS).
        SCHEMA_NAME=""
        if [[ "${PG_MESSAGE}" =~ permission\ denied\ for\ schema\ \"?([A-Za-z_][A-Za-z0-9_]*)\"? ]]; then
            SCHEMA_NAME="${BASH_REMATCH[1]}"
        fi
        if [[ -n "${SCHEMA_NAME}" ]]; then
            if [[ -d "${ROOT_DIR}/.dbmeta/${SCHEMA_NAME}" ]]; then
                fail3 "只读角色对 schema \"${SCHEMA_NAME}\" 权限不足（SQLSTATE 42501）" \
                      "${PG_MESSAGE}" \
                      "重跑 /pg-readonly-setup 核对该 schema 的授权范围"
                write_diag_json
                print_diag_stdout "只读角色对 schema \"${SCHEMA_NAME}\" 权限不足" \
                    "重跑 /pg-readonly-setup 核对该 schema 的授权范围"
            else
                fail3 "schema \"${SCHEMA_NAME}\" 未纳入只读通道范围（SQLSTATE 42501）" \
                      "${PG_MESSAGE}" \
                      "把 ${SCHEMA_NAME} 加进 .dbmeta/.dbllm.env 的 SCHEMAS 后重跑 /pg-readonly-setup"
                write_diag_json
                print_diag_stdout "schema \"${SCHEMA_NAME}\" 未纳入只读通道范围" \
                    "把 ${SCHEMA_NAME} 加进 .dbmeta/.dbllm.env 的 SCHEMAS 后重跑 /pg-readonly-setup"
            fi
            exit 2
        fi
        fail3 "权限不足，且无法判定涉及的 schema（SQLSTATE 42501）" \
              "${PG_MESSAGE}" \
              "核对上方 PG 原文消息；表级拒绝消息不含 schema 名，必要时联系 DBA 核实授权范围"
        write_diag_json
        print_diag_stdout "权限不足，且无法判定涉及的 schema" \
            "核对上方 PG 原文消息；表级拒绝消息不含 schema 名，必要时联系 DBA 核实授权范围"
        exit 1
        ;;
    42P18)
        # Not classified as 4: an indeterminate placeholder type does not
        # mean the SQL is wrong against the schema — a caller binding the
        # parameter's type at the protocol level could execute the same
        # statement successfully (design.md 失败模式表).
        fail3 "占位符类型无法推断（SQLSTATE 42P18）" \
              "${PG_MESSAGE}" \
              "为占位符加显式类型转换（如 \$1::int）后重试"
        write_diag_json
        print_diag_stdout "占位符类型无法推断" "为占位符加显式类型转换（如 \$1::int）后重试"
        exit 1
        ;;
    42601)
        # Unified message (tasks.md 3.4 / ADR-0007): a hand-slip syntax
        # typo and a DDL statement (PREPARE's grammar rejects DDL outright)
        # both surface as 42601 — distinguishing them would require
        # building a DDL-keyword list, which ADR-0007 explicitly rules out.
        # MUST NOT name a specific skill (pg-migrate-verify is unimplemented).
        fail3 "SQL 存在语法错误（SQLSTATE 42601）" \
              "${PG_MESSAGE} —— 手滑语法错误与 DDL 语句（PREPARE 不接受 DDL 语法）在此层同为 42601，不做区分；如需校验 DDL/迁移语句，见 docs/skills-roadmap.md §3.2 的规划" \
              "核对 SQL 拼写；确系表结构变更（DDL）则不适用本工具"
        write_diag_json
        print_diag_stdout "SQL 存在语法错误（含 DDL 不受支持的情形，二者不区分）" \
            "核对 SQL 拼写；确系表结构变更（DDL）则不适用本工具，见 docs/skills-roadmap.md §3.2 的规划"
        exit 4
        ;;
    *)
        fail3 "SQL 校验不通过（SQLSTATE ${SQLSTATE}）" \
              "${PG_MESSAGE}" \
              "核对 SQL 中引用的 schema / 表 / 列 / 运算符是否与 .dbmeta/ 记录的真实结构一致"
        write_diag_json
        print_diag_stdout "SQL 校验不通过" \
            "核对 SQL 中引用的 schema / 表 / 列 / 运算符是否与 .dbmeta/ 记录的真实结构一致"
        exit 4
        ;;
esac
