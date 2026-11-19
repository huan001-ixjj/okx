#!/usr/bin/env bash
#
# 把项目发布到可直连 OKX 的远程服务器，并可选地跑测试。
#
# 凭据只从本地 .env 读取（DEPLOY_SSH_HOST / DEPLOY_SSH_PORT / DEPLOY_SSH_PASSWORD），
# 通过 SSH_ASKPASS + 环境变量交给 ssh，密码不会出现在命令行参数与输出里，
# 也不会被上传到远程（打包时排除 .env）。
#
# 用法：
#   bash scripts/deploy_remote.sh                  # 上传 + 建环境 + 装依赖
#   bash scripts/deploy_remote.sh --only-tests     # 只跑远程 pytest 与 ruff
#   bash scripts/deploy_remote.sh --skip-install   # 上传但不重装依赖
#   bash scripts/deploy_remote.sh --clean          # 删除远程 .venv 与运行数据
#
set -euo pipefail

REMOTE_DIR="${REMOTE_DIR:-okxquant}"
SKIP_INSTALL=0
ONLY_TESTS=0
DO_CLEAN=0

usage() {
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
}

for arg in "$@"; do
    case "$arg" in
        --skip-install) SKIP_INSTALL=1 ;;
        --only-tests) ONLY_TESTS=1; SKIP_INSTALL=1 ;;
        --clean) DO_CLEAN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $arg（用 --help 查看用法）" >&2; exit 2 ;;
    esac
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m    %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m[失败] %s\033[0m\n  %s\n' "$1" "${2:-}"; exit 1; }

# ---------------------------------------------------------------------------
# 读取部署凭据
# ---------------------------------------------------------------------------
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

if [[ -z "${DEPLOY_SSH_HOST:-}" ]]; then
    fail "缺少 DEPLOY_SSH_HOST" "在 .env 中设置 DEPLOY_SSH_HOST=user@host"
fi
DEPLOY_SSH_PORT="${DEPLOY_SSH_PORT:-22}"

if [[ -z "${DEPLOY_SSH_PASSWORD:-}" ]]; then
    fail "缺少 DEPLOY_SSH_PASSWORD" "在 .env 中设置部署密码（脚本不会打印它）"
fi

ASKPASS="$(mktemp)"
chmod 700 "$ASKPASS"
printf '#!/bin/sh\nprintf "%%s\\n" "$DEPLOY_SSH_PASSWORD"\n' > "$ASKPASS"
cleanup() { rm -f "$ASKPASS"; }
trap cleanup EXIT

KNOWN_HOSTS="${HOME}/.ssh/known_hosts_deploy"
mkdir -p "${HOME}/.ssh"
touch "$KNOWN_HOSTS"

SSH_OPTS=(
    -p "$DEPLOY_SSH_PORT"
    -o StrictHostKeyChecking=accept-new
    -o UserKnownHostsFile="$KNOWN_HOSTS"
    -o ConnectTimeout=20
    -o PreferredAuthentications=password
    -o PubkeyAuthentication=no
    -o LogLevel=ERROR
)

run_ssh() {
    SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force \
        setsid -w ssh "${SSH_OPTS[@]}" "$DEPLOY_SSH_HOST" "$@"
}

# ---------------------------------------------------------------------------
# 连通性
# ---------------------------------------------------------------------------
step "检查远程连接与 Python 3.12"
run_ssh "python3.12 --version || /usr/bin/python3.12 --version" \
    || fail "无法登录或缺少 Python 3.12" \
            "确认 DEPLOY_SSH_HOST / DEPLOY_SSH_PASSWORD 正确，且服务器有 /usr/bin/python3.12"
ok "连接正常"

if [[ "$DO_CLEAN" -eq 1 ]]; then
    step "清理远程运行产物"
    run_ssh "cd ~/${REMOTE_DIR} 2>/dev/null && rm -rf .venv data logs .pytest_cache .ruff_cache || true"
    ok "已删除 .venv / data / logs / 缓存（源码保留）"
    exit 0
fi

# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------
if [[ "$ONLY_TESTS" -eq 0 ]]; then
    step "打包并上传到 ~/${REMOTE_DIR}"
    [[ -f pyproject.toml ]] || fail "找不到 pyproject.toml" "确认在项目根目录执行"
    [[ -f src/okxquant/cli.py ]] || fail "找不到 src/okxquant/cli.py" "确认工作区完整"

    tar czf - \
        --exclude='./.venv' \
        --exclude='./.git' \
        --exclude='./.env' \
        --exclude='./data' \
        --exclude='./logs' \
        --exclude='./.run' \
        --exclude='./.codebuddy' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='.ruff_cache' \
        --exclude='*.pyc' \
        --exclude='*.db' \
        --exclude='*.db-wal' \
        --exclude='*.db-shm' \
        . | run_ssh "mkdir -p ~/${REMOTE_DIR} && tar xzf - -C ~/${REMOTE_DIR}" \
        || fail "上传失败" "检查网络与远程磁盘空间"

    run_ssh "test -f ~/${REMOTE_DIR}/pyproject.toml" \
        || fail "上传后缺少 pyproject.toml" "检查 tar 排除规则是否误伤"

    # 只在缺失时创建：已有 .env 可能含用户填好的密钥，重复部署不能覆盖
    run_ssh "cd ~/${REMOTE_DIR} && if [ ! -f .env ]; then printf 'OKX_FLAG=1\n' > .env && chmod 600 .env; fi"
    ok "上传完成（远程 .env 已存在时保留原内容）"
fi

# ---------------------------------------------------------------------------
# 安装依赖
# ---------------------------------------------------------------------------
if [[ "$SKIP_INSTALL" -eq 0 ]]; then
    step "创建远程虚拟环境并安装依赖"
    run_ssh "cd ~/${REMOTE_DIR} && \
        /usr/bin/python3.12 -m venv .venv && \
        .venv/bin/pip install -q -U pip && \
        .venv/bin/pip install -q -e . && \
        .venv/bin/pip install -q pytest ruff" \
        || fail "远程安装依赖失败" \
                "查看上面的 pip 输出；若 PyPI 不可达，需检查服务器出网策略"
    ok "依赖安装完成"
fi

# ---------------------------------------------------------------------------
# 回归测试
# ---------------------------------------------------------------------------
step "远程运行 ruff 与 pytest"
run_ssh "cd ~/${REMOTE_DIR} && .venv/bin/ruff check . && .venv/bin/pytest -q" \
    || fail "远程测试未通过" "查看上面失败用例；本地可先用 uv run pytest 复现"

step "远程环境信息"
run_ssh "cd ~/${REMOTE_DIR} && .venv/bin/okxquant info 2>&1 | head -20"

printf '\n\033[32m全部完成。\033[0m 远程目录：~/%s\n' "$REMOTE_DIR"
printf '下一步可运行：\n'
printf '  bash scripts/deploy_remote.sh --only-tests\n'
printf '  或直接登录服务器执行 okxquant download --days 1\n'
