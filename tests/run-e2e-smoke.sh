#!/usr/bin/env bash
# tests/run-e2e-smoke.sh —— e2e 冒烟：完整只读凭据架构全链路 against 真 PG。
#
# ro-only-credential-architecture: the toolchain no longer holds any admin
# credential — and neither does this harness. 本仓 MUST NOT 执行 CREATE ROLE /
# GRANT 一类特权 SQL（见 CLAUDE.md「特权 SQL 边界」），所以步骤 ② 由**人**执行：
# 脚本跑到那里停下、打印确切命令、exit 2；人执行完重跑本脚本即从 ③ 继续。
# 这正是产品自己的 needs-human 范式（pg-readonly-setup 的 exit 2 → 人处置 → 重跑），
# 于是这条 e2e 连「停下等人」这一段也一并验到了。
#
# DBLLM_TEST_PG* 账号在此只用来只读地查 fixture schema 在不在，MUST NOT 持有
# CREATEROLE。只读凭据的唯一真相源是消费仓的 .dbllm.env——由 ① 的 ro-generate.sh
# 产出、人执行 setup.sql 时写进库里，其余每个工具调用（verify/pg-dict/pg-query-ro）
# 都只从那里取。此前本脚本把一对常驻只读凭据**直接写进** .dbllm.env，于是
# ro-generate.sh 这一整步——产品对外的第一道门——从未被 e2e 覆盖过（T71）。
#
# Onboarding order is reversed from the old architecture (SKILL.md
# "Onboarding order: run /pg-readonly-setup FIRST"): pg-dict.sh's own collect
# step now exclusively resolves RO credentials (shared/db-collect.sh REQ
# 「输出面与凭据来源」), so the read-only channel MUST be provisioned and
# verified before pg-dict can collect anything at all.
#
# 连接经 tests/.env.test 的隧道打到 **PgBouncer**（SSH_LOCAL_PORT → SSH_REMOTE_PORT=7432，session 池，见 .env.test.example 注释），
# 所以 ③ 的全链路探针（ro-session.sh 用新建只读角色跑 SELECT 1）是本仓唯一会真正穿过
# PgBouncer 认证面的断言——契约测试用的是早已在 userlist 里的 dbllm，验不到
# 新角色的认证。auth_file 模式下这要求人在步骤 ② 一并粘贴 userlist-fragment.txt。
#
# 把各单段测试（test_db_collect_contract.py / *_shell.py）分头验的东西串起来端到端跑：
#   generate（纯文本，零 DB）-> [人] psql -f setup.sql -> verify（六面审计 + 全链路探针）
#   -> pg-dict collect（RO 凭据）-> pg-query-ro（RO 凭据）
# 外加 render 的结构/幂等判据（见 devenv 测试策略 判据一/二）。
#
# executor: human —— 按需手动跑（发版前 / 改了只读凭据链路或 pg-dict.sh 编排后）。
# 前置：tests/.env.test 已配好（DB 连接 + SSH 隧道，见 tests/.env.test.example）。
#
# 用法：
#   bash tests/run-e2e-smoke.sh            # 首次：跑到 ② 停下，打印要人执行的 psql 命令
#   <人执行那条 psql 命令>                  # 一次性，之后不用再做
#   bash tests/run-e2e-smoke.sh            # 之后每次：①② 自动跳过，直跑 ③④⑤⑥+判据一/二
#
# 本脚本不接受任何参数。收工清理（三样东西，各有各的归属）：
#   本地产物        rm -rf build/e2e-consumer —— 含明文口令的 setup.sql 在这里面，
#                   收工最该删的就是它；纯本地文件，谁都能删。
#   fixture schema  重跑 tests/provision-test-db.sql 即重置（§③ 自己先 DROP 再建），
#                   或 `DROP SCHEMA dbllm_e2e, dbllm_e2e_ext CASCADE`——§③ 以
#                   `SET ROLE dbllm` 建，schema 归测试账号，它自己就删得掉，
#                   不需要超级用户（与契约测试的 dbllm_fixture 同一权限模型）。
#   只读角色        `DROP ROLE llm_readonly` —— 要 CREATEROLE，**归 DBA**。测试账号
#                   不持有（§① 的 ALTER USER ... NOCREATEROLE 主动收敛掉了）。
#
# 🔴 ①② 是**一次性供给**，不是每次回归都要跑的东西。角色建好后本脚本自动跳过它们，
#    回归真正在看的是 ③④⑤⑥ 与判据一/二——全程只读，与供给段解耦。
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

ENV_TEST="tests/.env.test"
SCHEMAS=(dbllm_e2e dbllm_e2e_ext)

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()   { echo "${GREEN}[OK]${NC} $1"; }
fail() { echo "${RED}[FAIL]${NC} $1" >&2; exit 1; }

# 本脚本无参数。此前 header 里写着一个 `--clean`，却从未有过任何参数解析——
# 照那份说明跑的人不会得到清理，而是静默跑完一整轮 e2e。宁可拒。
[[ $# -eq 0 ]] || fail "本脚本不接受参数（收到：$*）；清理见 header「收工清理」一节"

[[ -f "${ENV_TEST}" ]] || fail "找不到 ${ENV_TEST}（复制 tests/.env.test.example 填好再跑）"

set -a; # shellcheck disable=SC1090
source "${ENV_TEST}"; set +a

# fixture 灌注账号——只用来只读地查 fixture schema 在不在。它 MUST NOT 持有
# CREATEROLE（CLAUDE.md「特权 SQL 边界」），本脚本也从不用它建角色或授权。
H="${DBLLM_TEST_PGHOST:?}"; P="${DBLLM_TEST_PGPORT:?}"
DB="${DBLLM_TEST_PGDATABASE:?}"

# 只读角色名用产品缺省（同 shared/.dbllm.env.example）。这里刻意不再从
# tests/.env.test 取——只读凭据的唯一真相源是消费仓的 .dbllm.env，由
# ro-generate.sh 产出（T71）；再配一份 DBLLM_TEST_RO_* 就是第二个真相源，
# 且方向是反的（测试写进配置，而非从配置读）。
RO_ROLE="llm_readonly"

# 消费仓目录 MUST 跨两个阶段存活（阶段一产的 setup.sql 要留给阶段二用），
# 所以不能是 mktemp -d。落在 git-ignore 的 build/ 下。
CONSUMER="${REPO_ROOT}/build/e2e-consumer"
CONSUMER_ENV="${CONSUMER}/.dbmeta/.dbllm.env"
STARTED_TUNNEL=0

# fixture 存在性探测（owner 账号，只读 catalog 查询）
_ownerpsql() {
    PGPASSWORD="${DBLLM_TEST_PGPASSWORD:?}" psql -h "$H" -p "$P" \
        -U "${DBLLM_TEST_PGUSER:?}" -d "$DB" -v ON_ERROR_STOP=1 "$@"
}

# 只读通道探活——凭据只从消费仓 .dbllm.env 取，与产品路径同源。
_ropsql() {
    (
        # shellcheck disable=SC1091
        source "${REPO_ROOT}/shared/config.sh"
        db_llm_load_config "${CONSUMER_ENV}" >/dev/null 2>&1 || exit 1
        [[ "${DBS_DB_PASSWORD:-}" != "CHANGE_ME" ]] || exit 1
        PGPASSWORD="${DBS_DB_PASSWORD}" psql -h "${DBS_DB_HOST}" -p "${DBS_DB_PORT}" \
            -U "${DBS_DB_USER}" -d "${DBS_DB_NAME}" -v ON_ERROR_STOP=1 "$@"
    )
}

# 只关隧道。**不删角色、不删 fixture**：DROP ROLE 是特权语句（本仓不执行），
# 而 fixture schema 上挂着人执行 setup.sql 时授的 SELECT，删了阶段二就没得跑。
# 两者都是常驻的、由人一次性供给（tests/provision-test-db.sql 与 ① 产的
# setup.sql），清理是显式的收工动作、不该混进每次回归，故本脚本不提供入口
# ——具体三条命令见 header「收工清理」。
cleanup() {
    [[ "${STARTED_TUNNEL}" == "1" ]] && bash shared/ssh-tunnel.sh stop >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 端口监听探测（兼容 Linux ss / macOS+Linux lsof / Windows(GitBash) netstat，
# 与 shared/ssh-tunnel.sh 的 check_port 同一组判据）。
port_listening() {
    local p="$1"
    ss -tlnp "sport = :${p}" 2>/dev/null | grep -q LISTEN && return 0
    lsof -nP -iTCP:"${p}" -sTCP:LISTEN >/dev/null 2>&1 && return 0
    netstat -ano 2>/dev/null | grep -q ":${p}.*LISTENING" && return 0
    return 1
}

# ── 隧道：本地回环端口没监听才开（谁开的谁关）──────────────────
if [[ "$H" == "127.0.0.1" || "$H" == "localhost" ]]; then
    if ! port_listening "$P"; then
        echo "本地端口 $P 未监听，自动开 SSH 隧道..."
        bash shared/ssh-tunnel.sh start
        STARTED_TUNNEL=1
        for _ in 1 2 3 4 5; do port_listening "$P" && break; sleep 1; done
    fi
fi

# ── 前置核验：常驻 fixture 必须已由人灌 ────────────────────────
# 本脚本 MUST NOT 建角色、MUST NOT 授权、MUST NOT 灌 fixture——那些是 CREATE ROLE /
# GRANT / DDL，按「本仓只生成不执行特权 SQL」边界（CLAUDE.md）一律归人。
# 分工（2026-09-09 起）：fixture 由 tests/provision-test-db.sql 一次性灌；
# 只读角色与其授权由 ro-generate.sh 产脚本、人执行（下方 ①②）。
for s_ in "${SCHEMAS[@]}"; do
    _ownerpsql -Atc "SELECT 1 FROM pg_namespace WHERE nspname = '${s_}'" | grep -q 1 \
        || fail "常驻 fixture schema ${s_} 不存在——请先连到 ${DB} 执行 tests/provision-test-db.sql"
done
ok "前置就绪：常驻 fixture 齐（${SCHEMAS[*]}）"

# ── 消费项目布局 ───────────────────────────────────────────────
# 配置**只在首次创建**，密码留 CHANGE_ME 交给 ro-generate.sh 生成并写回。
# 重跑时整份保留：里面那个真密码正是人执行 setup.sql 时写进库里的那一个，
# 覆写它会让已建好的角色与配置对不上（ro-generate 的 REUSE 模式同理依赖它）。
mkdir -p "${CONSUMER}/.dbmeta"
[[ -d "${CONSUMER}/.git" ]] || (cd "${CONSUMER}" && git init -q)
printf '.dbmeta/.dbllm.env\n.dbmeta/db-readonly/\nbuild/\n' > "${CONSUMER}/.gitignore"
if [[ ! -f "${CONSUMER_ENV}" ]]; then
    (
        umask 077
        cat > "${CONSUMER_ENV}" <<EOF
SCHEMAS=dbllm_e2e,dbllm_e2e_ext
DB_HOST=$H
DB_PORT=$P
DB_NAME=$DB
DB_USER=${RO_ROLE}
DB_PASSWORD=CHANGE_ME
EOF
    )
    ok "消费仓配置已创建（DB_PASSWORD=CHANGE_ME，待 generate 写回）"
fi

# ── ① generate：产 setup.sql（纯文本，零 DB 连接）──────────────
# 这一步此前被整个跳过——旧脚本直接把常驻凭据写进 .dbllm.env，于是产品
# 对外的第一道门从未被 e2e 覆盖过（T71）。首次 CHANGE_ME 走 REPLACE 生成新
# 密码并写回；之后已是真密码，走 REUSE，产出同一份 setup.sql，重跑幂等。
CLAUDE_PROJECT_DIR="${CONSUMER}" bash shared/ro-generate.sh
[[ -f "${CONSUMER}/.dbmeta/db-readonly/setup.sql" ]] \
    || fail "ro-generate.sh 未产出 setup.sql"
ok "① generate：setup.sql 已生成（零 DB 连接）"

# ── ② 人执行 setup.sql：连得上就继续，连不上就停下等人 ──────────
# 这正是产品自己的 needs-human 范式，e2e 连「停下等人」这一段也一并验到。
if ! _ropsql -Atc "SELECT 1" >/dev/null 2>&1; then
    echo
    echo "${RED}[需要人]${NC} 只读角色 ${RO_ROLE} 尚不能连上 ${DB}@${H}:${P}。"
    echo "         ① 已产出供给脚本，② 需要由 DBA 亲手执行（本仓不执行特权 SQL）："
    echo
    echo "  psql -h ${H} -p ${P} -U <有 CREATEROLE 的账号> -d ${DB} \\"
    echo "       -f ${CONSUMER}/.dbmeta/db-readonly/setup.sql"
    echo
    echo "  若 PgBouncer 是 auth_file 模式，再把这份片段追加进 userlist.txt 并 RELOAD："
    echo "       ${CONSUMER}/.dbmeta/db-readonly/userlist-fragment.txt"
    echo
    echo "  执行完重跑本脚本即从 ③ 继续（①② 会自动跳过）。"
    echo
    exit 2
fi
ok "② 只读角色已可连（人已执行过 setup.sql）"

# ── ③ verify：六面审计 + 全链路探针，只用 .dbllm.env 凭据 ──────
CLAUDE_PROJECT_DIR="${CONSUMER}" bash shared/ro-verify.sh
ok "verify：六面审计通过，只读通道可用"

# ── ④ pg-dict.sh collect+render（RO 凭据，onboarding 顺序反转）──
CLAUDE_PROJECT_DIR="${CONSUMER}" bash pg-dict/scripts/pg-dict.sh >/dev/null
ok "pg-dict.sh 第一次运行成功（RO 凭据采集）"

M="${CONSUMER}/.dbmeta"
# ── 判据二：结构 ──────────────────────────────────────────────
for f in \
    "dbllm_e2e/tables/users.sql" \
    "dbllm_e2e/views/recent_orders.sql" \
    "dbllm_e2e/functions/fmt.sql" \
    "dbllm_e2e_ext/tables/widgets.sql"; do
    [[ -f "$M/$f" ]] || fail "缺文件：$f"
done
ok "对象→文件齐（含第二 schema widgets）"

[[ ! -f "$M/dbllm_e2e/tables/events_2026_08.sql" ]] \
    || fail "分区子表 events_2026_08 不应单独出文件（应折叠进 events）"
ok "分区子表已折叠"

grep -q 'pg-dict:.*:start' "$M/dbllm_e2e/tables/users.sql" || fail "users.sql 缺 managed-block 标记"
grep -q 'MATERIALIZED VIEW' "$M/dbllm_e2e/views/recent_orders.sql" || fail "matview 未渲染成 MATERIALIZED VIEW"
[[ "$(grep -c 'pg-dict:fn:fmt.*:start' "$M/dbllm_e2e/functions/fmt.sql")" == "2" ]] \
    || fail "fmt 重载应为一文件两 block"
ok "managed 标记 / matview / overload 多 block 结构正确"

# ── 判据一：幂等 ──────────────────────────────────────────────
if command -v md5sum >/dev/null 2>&1; then HASH_CMD=md5sum
elif command -v md5 >/dev/null 2>&1; then HASH_CMD=md5
else HASH_CMD=cksum; fi
before="$(cd "$M" && find . -type f -exec "$HASH_CMD" {} + | sort)"
CLAUDE_PROJECT_DIR="${CONSUMER}" bash pg-dict/scripts/pg-dict.sh >/dev/null
after="$(cd "$M" && find . -type f -exec "$HASH_CMD" {} + | sort)"
[[ "$before" == "$after" ]] || fail "二次运行非字节一致（不幂等）"
ok "幂等：二次运行字节一致"

# ── ⑤ pg-query-ro.sh --sql（RO 凭据，REQ-QR-3）────────────────
QR_OUT="$(CLAUDE_PROJECT_DIR="${CONSUMER}" bash pg-query-ro/scripts/pg-query-ro.sh \
    --sql "SELECT * FROM dbllm_e2e.users" --format csv)"
QR_PATH="$(printf '%s\n' "${QR_OUT}" | head -1)"
[[ -f "${QR_PATH}" ]] || fail "pg-query-ro.sh 未生成结果文件：${QR_PATH}"
[[ -f "${QR_PATH}.meta" ]] || fail "pg-query-ro.sh 未生成 .meta：${QR_PATH}.meta"
grep -q '^sql: ' "${QR_PATH}.meta" || fail ".meta 缺 sql: 字段"
ok "pg-query-ro.sh --sql 成功：结果文件 + .meta 齐全（${QR_PATH}）"

# ── ⑥ 编排入口本身也报「只读通道可用」（重跑应幂等地直达 ⑤）───
ORCH_OUT="$(CLAUDE_PROJECT_DIR="${CONSUMER}" bash pg-readonly-setup/scripts/readonly-setup.sh)"
[[ "${ORCH_OUT}" == *"只读通道可用"* ]] || fail "readonly-setup.sh 编排重跑未报「只读通道可用」：${ORCH_OUT}"
ok "readonly-setup.sh 编排入口重跑确认「只读通道可用」"

echo "${GREEN}[PASS]${NC} e2e 冒烟全通过"
