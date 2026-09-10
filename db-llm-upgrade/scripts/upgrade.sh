#!/bin/bash
# Upgrade the db-llm run checkout (~/.skills/db-llm-skills): git pull --ff-only,
# then bash setup.sh to refresh the skill symlinks, then show the resulting
# version and recent changelog. Pull and setup MUST run back-to-back — pulling
# without re-running setup.sh would leave newly-added skill directories
# unlinked (see db-llm-upgrade/SKILL.md).
#
# This script only touches the RUN checkout. It never edits code and never
# touches the dev checkout that /db-llm-upgrade is not responsible for.
#
# Exit codes:
#   0  success
#   1  run checkout missing / remote mismatch / pull failed (non-ff, network,
#      detached HEAD)
#   2  setup.sh failed
set -euo pipefail

CHECKOUT_DIR="${HOME}/.skills/db-llm-skills"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${YELLOW}[INFO]${NC} $1" >&2; }
pass() { echo -e "${GREEN}[PASS]${NC} $1" >&2; }
fail() { echo -e "${RED}[FAIL]${NC} $1" >&2; }

fail3() {
    # fail3 <problem> <cause> <fix>
    fail "problem: $1"
    fail "cause: $2"
    fail "fix: $3"
}

# [1] CHECKOUT_DIR must exist.
if [[ ! -d "$CHECKOUT_DIR" ]]; then
    fail3 \
        "运行 checkout 不存在" \
        "${CHECKOUT_DIR}/ 目录缺失" \
        "重新 clone: git clone <repo> ${CHECKOUT_DIR} && cd ${CHECKOUT_DIR} && bash setup.sh"
    exit 1
fi

# [2] remote must point at the official repo. Explicitly guard the command
# itself failing (non-git directory / no origin remote) rather than letting
# set -e handle it implicitly.
REMOTE=$(git -C "$CHECKOUT_DIR" remote get-url origin 2>/dev/null) || REMOTE=""
# The public repo is the only valid target. This generic check decides; the
# branch inside it only picks a more actionable message.
#
# Note the two repo names are prefixes of each other:
#   dev    laodao-ai/db-llm          (PRIVATE)
#   public laodao-ai/db-llm-skills   (PUBLIC)
# So the dev-repo call-out MUST anchor at end-of-string (optional .git / slash),
# never a bare substring match -- `*laodao-ai/db-llm*` matches the public URL too.
if [[ -z "$REMOTE" || "$REMOTE" != *"laodao-ai/db-llm-skills"* ]]; then
    if [[ "$REMOTE" =~ laodao-ai/db-llm(\.git)?/?$ ]]; then
        # Running setup.sh from a dev checkout repoints the host symlinks at it,
        # which is exactly what the run/dev split exists to prevent -- say so.
        fail3 \
            "当前 checkout 指向开发仓，不是公开仓" \
            "remote 是 laodao-ai/db-llm（开发仓，PRIVATE）；升级只能对公开仓 laodao-ai/db-llm-skills 的 checkout 做" \
            "删掉 ${CHECKOUT_DIR} 后重新 clone: git clone https://github.com/laodao-ai/db-llm-skills.git ${CHECKOUT_DIR}"
    else
        fail3 \
            "当前 checkout 不是官方仓库" \
            "remote URL 不含 laodao-ai/db-llm-skills（当前值: ${REMOTE:-<空，get-url 失败或非 git 仓库>}）" \
            "检查 git -C ${CHECKOUT_DIR} remote -v，或删除后重新 clone"
    fi
    exit 1
fi

# [3] git pull --ff-only. Distinguish detached HEAD from other pull failures
# (network, non-fast-forward) since the fix differs.
info "拉取最新版本..."
PULL_OUTPUT=""
PULL_STATUS=0
PULL_OUTPUT=$(git -C "$CHECKOUT_DIR" pull --ff-only 2>&1) || PULL_STATUS=$?
if [[ $PULL_STATUS -ne 0 ]]; then
    echo "$PULL_OUTPUT" >&2
    if [[ "$PULL_OUTPUT" == *"not currently on a branch"* ]]; then
        fail3 \
            "升级拉取失败" \
            "运行 checkout 处于 detached HEAD 状态" \
            "先 git -C ${CHECKOUT_DIR} checkout main，再重跑本脚本"
    else
        fail3 \
            "升级拉取失败" \
            "网络错误，或本地被改过导致非 fast-forward（运行 checkout 只读，改动应发生在开发 checkout）" \
            "检查网络后重试；若本地有改动，先在开发 checkout 中处理，勿在运行 checkout 内修改"
    fi
    exit 1
fi
echo "$PULL_OUTPUT" >&2
pass "拉取完成"

# [4] bash setup.sh — refresh skill symlinks.
info "刷新 skill 软链..."
if ! bash "${CHECKOUT_DIR}/setup.sh"; then
    fail3 \
        "安装脚本执行失败" \
        "setup.sh 非零退出" \
        "查看上方错误输出，手动排查后重跑 bash ${CHECKOUT_DIR}/setup.sh"
    exit 2
fi
pass "setup.sh 完成"

# [5] Show version and recent changelog.
echo "" >&2
info "当前版本:"
git -C "$CHECKOUT_DIR" describe --tags --always --dirty
info "最近变更:"
git -C "$CHECKOUT_DIR" log --oneline -5

exit 0
