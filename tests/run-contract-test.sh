#!/usr/bin/env bash
# tests/run-contract-test.sh —— 跑契约测试的启动脚本。
#
# 契约测试（tests/test_db_collect_contract.py）从环境变量读 5 个 DBLLM_TEST_PG*，
# 本脚本把「source tests/.env.test 并 export」这一步内置，省得每次手敲。
#
# 这是真 PG 泳道的**唯一入口**——裸跑 `pytest` 不会 source tests/.env.test、也不会开隧道，
# 那五个变量不在环境里，契约测试就按仓内 fail-loud（fail-not-skip）约定整批报错。那种报错
# 是「入口用错了」，MUST NOT 读作「本机没有测试库」。
#
# 🔴 本泳道只跑**零特权 SQL** 的契约测试。本仓 MUST NOT 执行 CREATE ROLE / CREATE USER /
# GRANT / REVOKE（见 CLAUDE.md「特权 SQL 边界」）——`ro-generate.sh` 只**生成** setup.sql，
# 执行它的永远是消费仓的开发者，不是本仓的任何脚本、测试或大模型。因此这里不存在、也
# MUST NOT 新增「执行 setup.sql 再断言」一类的契约测试。
#
# 用法（先建好 tests/.env.test，见 tests/.env.test.example；库和账号用
# tests/provision-test-db.sql 一次性 provision）：
#   bash tests/run-contract-test.sh            # 跑契约测试
#   bash tests/run-contract-test.sh -v         # 附加参数原样透传给 pytest
set -euo pipefail

# 无论从哪个目录调用，都锚到仓库根，让相对路径稳定。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

ENV_TEST="tests/.env.test"
if [[ ! -f "${ENV_TEST}" ]]; then
    echo "problem: 找不到 ${ENV_TEST}" >&2
    echo "cause: 契约测试的连接凭据文件还没建（它是 git-ignored 的，不随仓库分发）" >&2
    echo "fix: 复制 tests/.env.test.example 为 ${ENV_TEST} 并填入真实值，再重跑本脚本" >&2
    exit 1
fi

# set -a：source 期间赋的变量自动 export 到子进程（pytest）环境。
set -a
# shellcheck disable=SC1090
source "${ENV_TEST}"
set +a

# 隧道生命周期：本脚本「谁开的谁关」——
#   · 端口已在监听（你手动开过隧道、或别的测试还开着）→ 直接复用，不碰它；
#   · 端口没监听 → 本脚本自己开，记下「是我开的」，测试收尾（trap EXIT）时自动关闭。
# 于是：想反复快跑就先手动 `bash shared/ssh-tunnel.sh start`（脚本复用、不关）；只想一次性
# 冷跑就直接跑本脚本，跑完不留悬空的隧道 / root-SSH 会话。隧道参数已随 .env.test 一起
# source 进来，ssh-tunnel.sh 从环境变量读。
STARTED_TUNNEL=0
cleanup() {
    if [[ "${STARTED_TUNNEL}" == "1" ]]; then
        echo "本脚本开的隧道，测试收尾自动关闭..." >&2
        bash shared/ssh-tunnel.sh stop || true
    fi
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

HOST="${DBLLM_TEST_PGHOST:-127.0.0.1}"
PORT="${DBLLM_TEST_PGPORT:-6432}"
if [[ "${HOST}" == "127.0.0.1" || "${HOST}" == "localhost" ]]; then
    if ! port_listening "${PORT}"; then
        echo "本地端口 ${PORT} 未监听，自动开 SSH 隧道..."
        bash shared/ssh-tunnel.sh start
        STARTED_TUNNEL=1
        # 等隧道本地端就绪（最多 ~5s），避免 pytest 抢在端口转发建立前连接。
        for _ in 1 2 3 4 5; do
            port_listening "${PORT}" && break
            sleep 1
        done
    fi
fi

# 真 PG 泳道覆盖的契约测试。新增时 MUST 加进这个数组（漏加不会报错，只会让那些真库断言
# 静默不跑），且新增项 MUST 零特权 SQL——见文件头「特权 SQL 边界」。
CONTRACT_TESTS=(
    tests/test_db_collect_contract.py     # db-collect 的 pg_catalog JSON 结构契约（只建删自有 fixture schema）
    tests/test_sql_check_contract.py      # pg-sql-check 的 PREPARE 校验契约（复用常驻 dbllm_e2e fixture + 只读角色，零特权 SQL）
)

# 不用 exec：exec 会替换掉本进程，trap EXIT 就收不到、cleanup 跑不了。让 pytest 正常返回，
# set -e 下它失败会触发 EXIT trap，退出码仍是 pytest 的。
pytest "${CONTRACT_TESTS[@]}" "$@"
