#!/usr/bin/env bash
# setup.sh — paper-database 环境部署脚本
#
# 功能:
#   1. 写入 PAPER_DATABASE_HOME 到 ~/.bashrc（幂等）
#   2. 自动发现 AI 工具 skill 目录
#   3. 创建 skills/ 到各工具 skill 目录的硬链接
#   4. 支持手动指定目标目录
#
# 用法:
#   ./setup.sh                        # 自动发现所有 AI 工具
#   ./setup.sh --dry-run              # 只打印操作，不执行
#   ./setup.sh ~/.claude/skills       # 手动指定目录
#   ./setup.sh --tools claude,hermes  # 只处理指定工具

set -euo pipefail

# ── 全局变量 ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"           # AI-config 根目录
PROJECT_DIR="$ROOT_DIR/paper-database"
SKILLS_SRC="$ROOT_DIR/skills"
BASHRC="$HOME/.bashrc"
DRY_RUN=false
SELECTED_TOOLS=""  # 逗号分隔的工具名列表

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
            echo "用法: ./setup.sh [OPTIONS] [DIRS...]"
            echo ""
            echo "Options:"
            echo "  --dry-run        只打印将要执行的操作，不实际执行"
            echo "  --tools NAME,...  只处理指定工具 (如: claude,hermes,codex)"
            echo "  --help           显示此帮助信息"
            echo ""
            echo "手动指定目录:"
            echo "  ./setup.sh ~/.claude/skills ~/.cursor/skills"
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

# ── 2. 自动发现 AI 工具 skill 目录 ──────────────────────────────

# 工具定义: "工具名:发现路径"
# 格式: name:path  —— path 中用 ~ 表示 $HOME
KNOWN_TOOLS=(
    "claude:~/.claude/skills"
    "codex:~/.codex/skills"
    "hermes:~/.hermes/skills"
    "hermes-agent:~/.hermes/hermes-agent/skills"
)

discover_skill_dirs() {
    local discovered=()

    if [[ -n "$SELECTED_TOOLS" ]]; then
        # 只搜索用户指定的工具
        IFS=',' read -ra TOOLS <<< "$SELECTED_TOOLS"
        for tool_name in "${TOOLS[@]}"; do
            tool_name=$(echo "$tool_name" | xargs)  # trim whitespace
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
        # 自动发现所有已知工具
        for entry in "${KNOWN_TOOLS[@]}"; do
            local name="${entry%%:*}"
            local path="${entry#*:}"
            path="${path/#\~/$HOME}"
            if [[ -d "$path" ]]; then
                discovered+=("$path ($name)")
            fi
        done
    fi

    # 添加用户手动指定的目录作为补充
    for d in "${USER_DIRS[@]}"; do
        local expanded="${d/#\~/$HOME}"
        discovered+=("$expanded (manual)")
    done

    # 去重（按路径）
    printf '%s\n' "${discovered[@]}" | sort -u
}

# ── 3. 硬链接创建 ──────────────────────────────────────────────

link_skill_file() {
    local src="$1"
    local dest="$2"

    if [[ ! -f "$src" ]]; then
        return
    fi

    if [[ -f "$dest" ]]; then
        # 检查是否已经是同一个 inode 的硬链接
        local src_inode dest_inode
        src_inode=$(stat -c '%i' "$src" 2>/dev/null || echo "")
        dest_inode=$(stat -c '%i' "$dest" 2>/dev/null || echo "")
        if [[ "$src_inode" == "$dest_inode" ]] && [[ -n "$src_inode" ]]; then
            return 0  # 已是硬链接，跳过
        fi
        # 不是硬链接，删除旧文件
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

    # 确保目标目录存在
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

    # 链接子目录（如果存在）
    for subdir in "$src_dir/"*/; do
        local sub_name
        sub_name=$(basename "$subdir")
        [[ "$sub_name" == "references" ]] && continue  # 已处理
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

    # 列出所有源 skill
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

    # 清理目标目录中已不在源目录的旧硬链接
    cleanup_stale_links "$target_dir"
}

cleanup_stale_links() {
    local target_dir="$1"

    for target_skill in "$target_dir"/*/; do
        [[ -d "$target_skill" ]] || continue
        local skill_name
        skill_name=$(basename "$target_skill")
        local src_skill="$SKILLS_SRC/$skill_name"

        if [[ ! -d "$src_skill" ]]; then
            # Skill not managed by us — skip silently
            continue
        fi
    done
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

    # 验证 skills 源目录存在
    if [[ ! -d "$SKILLS_SRC" ]]; then
        err "Skills 源目录不存在: $SKILLS_SRC"
        exit 1
    fi

    # Step 0: 安装 Python 包
    install_package

    # Step 1: 环境变量
    setup_env_var

    # Step 2: 发现目标目录
    step "发现 AI 工具 skill 目录"
    echo ""

    mapfile -t TARGET_DIRS < <(discover_skill_dirs)

    if [[ ${#TARGET_DIRS[@]} -eq 0 ]]; then
        warn "未发现任何 AI 工具 skill 目录"
        echo ""
        echo "手动指定示例:"
        echo "  ./setup.sh ~/.claude/skills"
        echo "  ./setup.sh ~/.hermes/skills"
        exit 0
    fi

    info "发现 ${#TARGET_DIRS[@]} 个目标目录:"
    for entry in "${TARGET_DIRS[@]}"; do
        echo "    - $entry"
    done
    echo ""

    # Step 3: 创建硬链接
    step "部署 Skills"
    echo ""

    for entry in "${TARGET_DIRS[@]}"; do
        local dir="${entry%% (*}"  # 提取路径（去掉 (tool_name) 后缀）
        echo "  → $entry"
        deploy_skills "$dir"
        echo ""
    done

    # ── 完成 ──────────────────────────────────────────────────────
    echo "=============================================="
    info "部署完成!"
    echo ""
    echo "  环境变量: \$PAPER_DATABASE_HOME = $PROJECT_DIR"
    echo "  Skills 源: $SKILLS_SRC"
    echo "=============================================="
    echo ""
}

main
