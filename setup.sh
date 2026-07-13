#!/usr/bin/env bash
# setup.sh — AI-config 总部署脚本
#
# 用法:
#   ./setup.sh              # 部署全部
#   ./setup.sh --dry-run    # 预览操作

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 子脚本清单（按需添加）────────────────────────────────────────

SETUP_SCRIPTS=(
    "$SCRIPT_DIR/scripts/setup-paper-database.sh"
)

# ── 执行 ─────────────────────────────────────────────────────────

main() {
    echo "=============================================="
    echo "  AI-config — 一键部署全部工具"
    echo "=============================================="
    echo ""

    for script in "${SETUP_SCRIPTS[@]}"; do
        if [[ -x "$script" ]]; then
            bash "$script" "$@"
            echo ""
        else
            echo "[skip] 脚本不存在或不可执行: $script"
        fi
    done

    echo "=============================================="
    echo "  全部部署完成"
    echo "  source ~/.bashrc 使环境变量生效"
    echo "=============================================="
}

main "$@"
