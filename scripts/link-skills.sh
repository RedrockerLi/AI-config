#!/usr/bin/env bash
# link-skills.sh — 将 skills/ 目录硬链接到各 AI 工具的 skill 目录
#
# 功能:
#   1. 自动发现 AI 工具 skill 目录 (claude, codex, hermes 等)
#   2. 将 skills/* 硬链接到每个工具的 skill 目录
#   3. 新增 skill 后直接运行即可同步
#
# 用法:
#   ./scripts/link-skills.sh                        # 自动发现所有 AI 工具
#   ./scripts/link-skills.sh --dry-run              # 只打印操作，不执行
#   ./scripts/link-skills.sh ~/.claude/skills       # 手动指定目录
#   ./scripts/link-skills.sh --tools claude,hermes  # 只处理指定工具
#   ./scripts/link-skills.sh --help                 # 显示帮助

set -euo pipefail

# ── 全局变量 ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SKILLS_SRC="$ROOT_DIR/skills"
DRY_RUN=false
SELECTED_TOOLS=""  # 逗号分隔的工具名列表

# ── 颜色输出 ────────────────────────────────────────────────────

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[info]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[err]${NC}   $*"; }
step()  { echo -e "${CYAN}==>${NC} $*"; }

# ── 参数解析 ────────────────────────────────────────────────────

USER_DIRS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --tools)
            SELECTED_TOOLS="$2"
            shift 2
            ;;
        --help|-h)
            echo "用法: ./scripts/link-skills.sh [OPTIONS] [DIRS...]"
            echo ""
            echo "将 skills/ 下的所有 skill 硬链接到 AI 工具的 skill 目录。"
            echo "新增 skill 后运行此脚本即可同步到所有工具。"
            echo ""
            echo "Options:"
            echo "  --dry-run        只打印将要执行的操作，不实际执行"
            echo "  --tools NAME,...  只处理指定工具 (如: claude,hermes,codex)"
            echo "  --help           显示此帮助信息"
            echo ""
            echo "支持的自动发现工具:"
            for entry in "${KNOWN_TOOLS[@]}"; do
                echo "  ${entry%%:*}"
            done
            echo ""
            echo "手动指定目录:"
            echo "  ./scripts/link-skills.sh ~/.claude/skills ~/.cursor/skills"
            exit 0
            ;;
        -*)
            err "未知选项: $1"
            exit 1
            ;;
        *)
            USER_DIRS+=("$1")
            shift
            ;;
    esac
done

# ── 工具定义 ────────────────────────────────────────────────────

# 格式: "工具名:发现路径"  —— path 中用 ~ 表示 $HOME
KNOWN_TOOLS=(
    "claude:~/.claude/skills"
    "codex:~/.codex/skills"
    "hermes:~/.hermes/skills"
    "hermes-agent:~/.hermes/hermes-agent/skills"
)

# ── 自动发现目标目录 ────────────────────────────────────────────

discover_skill_dirs() {
    local discovered=()

    if [[ -n "$SELECTED_TOOLS" ]]; then
        IFS=',' read -ra TOOLS <<< "$SELECTED_TOOLS"
        for tool_name in "${TOOLS[@]}"; do
            tool_name=$(echo "$tool_name" | xargs)  # trim
            local found=false
            for entry in "${KNOWN_TOOLS[@]}"; do
                local name="${entry%%:*}"
                local path="${entry#*:}"
                if [[ "$name" == "$tool_name" ]]; then
                    path="${path/#\~/$HOME}"
                    if [[ -d "$path" ]]; then
                        discovered+=("$path ($name)")
                    else
                        warn "未找到 $name 的 skill 目录: $path"
                    fi
                    found=true
                    break
                fi
            done
            if ! $found; then
                warn "未知工具: $tool_name"
            fi
        done
    else
        for entry in "${KNOWN_TOOLS[@]}"; do
            local name="${entry%%:*}"
            local path="${entry#*:}"
            path="${path/#\~/$HOME}"
            if [[ -d "$path" ]]; then
                discovered+=("$path ($name)")
            fi
        done
    fi

    for d in "${USER_DIRS[@]}"; do
        local expanded="${d/#\~/$HOME}"
        discovered+=("$expanded (manual)")
    done

    printf '%s\n' "${discovered[@]}" | sort -u
}

# ── 硬链接核心逻辑 ──────────────────────────────────────────────

link_skill_file() {
    local src="$1"
    local dest="$2"

    if [[ ! -f "$src" ]]; then
        return
    fi

    if [[ -f "$dest" ]]; then
        local src_inode dest_inode
        src_inode=$(stat -c '%i' "$src" 2>/dev/null || echo "")
        dest_inode=$(stat -c '%i' "$dest" 2>/dev/null || echo "")
        if [[ "$src_inode" == "$dest_inode" ]] && [[ -n "$src_inode" ]]; then
            return 0  # 已是硬链接，跳过
        fi
        warn "  目标文件已存在但不是硬链接，将覆盖: $dest"
        if ! $DRY_RUN; then
            rm -f "$dest"
        fi
    fi

    if $DRY_RUN; then
        info "  [dry-run] ln $src → $dest"
    else
        mkdir -p "$(dirname "$dest")"
        ln "$src" "$dest"
    fi
}

link_skill() {
    local skill_name="$1"
    local target_skills_dir="$2"

    local src_dir="$SKILLS_SRC/$skill_name"
    local dest_dir="$target_skills_dir/$skill_name"

    if [[ ! -d "$src_dir" ]]; then
        warn "Skill 源目录不存在: $src_dir"
        return
    fi

    mkdir -p "$dest_dir"

    # 链接 SKILL.md
    if [[ -f "$src_dir/SKILL.md" ]]; then
        link_skill_file "$src_dir/SKILL.md" "$dest_dir/SKILL.md"
    fi

    # 链接 references/ 目录下的文件
    if [[ -d "$src_dir/references" ]]; then
        mkdir -p "$dest_dir/references"
        for ref_file in "$src_dir/references/"*; do
            if [[ -f "$ref_file" ]]; then
                link_skill_file "$ref_file" "$dest_dir/references/$(basename "$ref_file")"
            fi
        done
    fi

    # 链接其他子目录（排除 references）
    for subdir in "$src_dir/"*/; do
        local sub_name
        sub_name=$(basename "$subdir")
        [[ "$sub_name" == "references" ]] && continue
        if [[ -d "$subdir" ]]; then
            mkdir -p "$dest_dir/$sub_name"
            for sub_file in "$subdir/"*; do
                if [[ -f "$sub_file" ]]; then
                    link_skill_file "$sub_file" "$dest_dir/$sub_name/$(basename "$sub_file")"
                fi
            done
        fi
    done
}

deploy_skills() {
    local target_dir="$1"

    if [[ ! -d "$target_dir" ]]; then
        info "创建目录: $target_dir"
        if ! $DRY_RUN; then
            mkdir -p "$target_dir"
        fi
    fi

    local skill_count=0
    for skill_subdir in "$SKILLS_SRC"/*/; do
        [[ -d "$skill_subdir" ]] || continue
        local skill_name
        skill_name=$(basename "$skill_subdir")
        link_skill "$skill_name" "$target_dir"
        skill_count=$((skill_count + 1))
    done

    if [[ $skill_count -eq 0 ]]; then
        warn "skills/ 目录中未找到任何 Skill"
    fi
}

# ── 主流程 ──────────────────────────────────────────────────────

main() {
    echo ""
    echo "=============================================="
    echo "  Link Skills — 硬链接 skills/ 到 AI 工具"
    echo "=============================================="
    echo ""

    if $DRY_RUN; then
        warn "DRY-RUN 模式 — 只打印操作，不实际执行"
        echo ""
    fi

    if [[ ! -d "$SKILLS_SRC" ]]; then
        err "Skills 源目录不存在: $SKILLS_SRC"
        exit 1
    fi

    # Step 1: 发现目标目录
    step "发现 AI 工具 skill 目录"
    echo ""

    mapfile -t TARGET_DIRS < <(discover_skill_dirs)

    if [[ ${#TARGET_DIRS[@]} -eq 0 ]]; then
        warn "未发现任何 AI 工具 skill 目录"
        echo ""
        echo "手动指定示例:"
        echo "  ./scripts/link-skills.sh ~/.claude/skills"
        echo "  ./scripts/link-skills.sh ~/.hermes/skills"
        exit 0
    fi

    info "发现 ${#TARGET_DIRS[@]} 个目标目录:"
    for entry in "${TARGET_DIRS[@]}"; do
        echo "    - $entry"
    done
    echo ""

    # Step 2: 创建硬链接
    step "部署 Skills"
    echo ""

    for entry in "${TARGET_DIRS[@]}"; do
        local dir="${entry%% (*}"
        echo "  → $entry"
        deploy_skills "$dir"
        echo ""
    done

    echo "=============================================="
    info "链接完成!"
    echo "  Skills 源: $SKILLS_SRC"
    echo "=============================================="
    echo ""
}

main
