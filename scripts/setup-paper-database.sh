#!/usr/bin/env bash
# setup.sh — paper-database 环境部署脚本
#
# 功能:
#   1. 写入 PAPER_DATABASE_HOME 到 ~/.bashrc（幂等）
#   2. 安装 paper-database Python 包 (pip install -e .)
#
# 技能硬链接由 scripts/link-skills.sh 独立处理，
# 由 setup.sh 统一调度。
#
# 用法:
#   ./setup.sh              # 安装包 + 设置环境变量
#   ./setup.sh --dry-run    # 只打印操作，不执行

set -euo pipefail

# ── 全局变量 ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"           # AI-config 根目录
PROJECT_DIR="$ROOT_DIR/paper-database"
BASHRC="$HOME/.bashrc"
DRY_RUN=false

# ── 颜色输出 ────────────────────────────────────────────────────

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[info]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[err]${NC}   $*"; }
step()  { echo -e "${CYAN}==>${NC} $*"; }

# ── 参数解析 ────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "用法: ./setup.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --dry-run  只打印将要执行的操作，不实际执行"
            echo "  --help     显示此帮助信息"
            exit 0
            ;;
        *)
            # 忽略透传的参数（如 --tools USER_DIRS），
            # 这些参数由 setup.sh 传递给 link-skills.sh 使用
            shift
            ;;
    esac
done

# ── 0. 安装 Python 包 ──────────────────────────────────────────

install_package() {
    step "安装 paper-database Python 包"

    if ! command -v pip &>/dev/null; then
        err "pip 未找到，请先安装 Python 和 pip"
        exit 1
    fi

    if $DRY_RUN; then
        info "[dry-run] cd $PROJECT_DIR && pip install -e ."
    else
        cd "$PROJECT_DIR"
        if pip install -e . &>/dev/null; then
            info "pip install -e . 完成"
        else
            err "pip install 失败，请检查 Python 环境和依赖"
            exit 1
        fi
    fi

    echo ""
}

# ── 1. 环境变量 ────────────────────────────────────────────────

setup_env_var() {
    step "设置环境变量 PAPER_DATABASE_HOME"

    local var_line="export PAPER_DATABASE_HOME=\"$PROJECT_DIR\""

    if grep -q "^export PAPER_DATABASE_HOME=" "$BASHRC" 2>/dev/null; then
        local current_value
        current_value=$(grep "^export PAPER_DATABASE_HOME=" "$BASHRC" | head -1)
        if [[ "$current_value" == "$var_line" ]]; then
            info "PAPER_DATABASE_HOME 已正确设置: $PROJECT_DIR"
        else
            warn "PAPER_DATABASE_HOME 值需要更新:"
            warn "  当前: $current_value"
            warn "  新的: $var_line"
            if $DRY_RUN; then
                info "[dry-run] 将更新 ~/.bashrc"
            else
                sed -i "s|^export PAPER_DATABASE_HOME=.*|$var_line|" "$BASHRC"
                info "已更新 ~/.bashrc"
            fi
        fi
    else
        if $DRY_RUN; then
            info "[dry-run] 将写入: $var_line"
        else
            echo "" >> "$BASHRC"
            echo "# Paper Database 环境变量" >> "$BASHRC"
            echo "$var_line" >> "$BASHRC"
            info "已写入 ~/.bashrc: $var_line"
        fi
    fi

    # 导出到当前 shell
    if ! $DRY_RUN; then
        export PAPER_DATABASE_HOME="$PROJECT_DIR"
    fi

    echo ""
}


# ── 4. 主流程 ────────────────────────────────────────────────────

main() {
    echo ""
    echo "=============================================="
    echo "  Paper Database — 环境部署"
    echo "=============================================="
    echo ""

    if $DRY_RUN; then
        warn "DRY-RUN 模式 — 只打印操作，不实际执行"
        echo ""
    fi

    # 验证项目目录存在
    if [[ ! -d "$PROJECT_DIR" ]]; then
        err "项目目录不存在: $PROJECT_DIR"
        err "请确保 setup.sh 与 paper-database/ 在同一父目录下"
        exit 1
    fi

    # Step 0: 安装 Python 包
    install_package

    # Step 1: 环境变量
    setup_env_var

    # ── 完成 ──────────────────────────────────────────────────────
    echo "=============================================="
    info "部署完成!"
    echo ""
    echo "  环境变量: \$PAPER_DATABASE_HOME = $PROJECT_DIR"
    echo "=============================================="
    echo ""
}

main
