#!/bin/bash

# =============================================================================
# ACE 仪表板 - 交互式安装程序
# =============================================================================
# 此脚本将 ACE 仪表板 Web 文件安装到 Mainsail/Fluidd 中
# 并安装 Moonraker 组件以获取 ACE 状态。
#
# 使用方法: ./install.sh
# =============================================================================

set -u  # 遇到未定义变量时退出

# 输出颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 脚本目录（此脚本所在位置）
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# 解析安装用户/主目录以设置默认值
INSTALL_USER="${SUDO_USER:-$(id -un)}"
INSTALL_HOME="$(getent passwd "$INSTALL_USER" 2>/dev/null | cut -d: -f6 || true)"
if [ -z "$INSTALL_HOME" ]; then
    INSTALL_HOME="$HOME"
fi

# ============================================================================
# 辅助函数
# ============================================================================

print_header() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

print_info() {
    echo -e "${BLUE}ℹ ${1}${NC}"
}

print_success() {
    echo -e "${GREEN}✓ ${1}${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ ${1}${NC}"
}

print_error() {
    echo -e "${RED}✗ ${1}${NC}"
}

# 是/否提示
prompt_yes_no() {
    local prompt="$1"
    local response
    while true; do
        read -p "$(echo -e ${BLUE}${prompt}${NC} [y/N]: )" response
        case "$response" in
            [yY][eE][sS]|[yY]) return 0 ;;
            [nN][oO]|[nN]|"") return 1 ;;
            *) echo "请回答 y 或 n" ;;
        esac
    done
}

# 使用默认值提示输入
prompt_input() {
    local prompt="$1"
    local default="$2"
    local response
    read -p "$(echo -e ${BLUE}${prompt}${NC} [${default}]: )" response
    echo "${response:-$default}"
}

# 创建带时间戳的备份
backup_file() {
    local file="$1"
    if [ -f "$file" ]; then
        local timestamp=$(date +"%Y%m%d_%H%M%S")
        local backup="${file}.backup_${timestamp}"
        cp "$file" "$backup"
        print_success "已备份: $file → $backup"
        return 0
    fi
    return 1
}

# 如果存在符号链接则移除，并显示其指向的目标
remove_symlink_if_exists() {
    local path="$1"
    if is_symlink "$path"; then
        local target=$(readlink "$path")
        rm -f "$path"
        print_info "已移除符号链接: $path (原来指向: $target)"
        return 0
    fi
    return 1
}

# 检查路径是否为符号链接
is_symlink() {
    [ -L "$1" ]
}

# 创建或替换符号链接
create_or_replace_symlink() {
    local source="$1"
    local target="$2"
    local description="$3"
    
    if [ ! -e "$source" ]; then
        print_error "$source 不存在，跳过符号链接"
        return 1
    fi
    
    if [ -e "$target" ] || is_symlink "$target" ]; then
        if is_symlink "$target"; then
            print_warning "符号链接已存在: $target"
            local current_target=$(readlink "$target")
            print_info "  → 当前指向: $current_target"
        else
            print_warning "文件/目录已存在: $target"
        fi
        
        if prompt_yes_no "替换它?"; then
            rm -f "$target"
            ln -sf "$source" "$target"
            print_success "符号链接已创建: $target → $source"
            return 0
        else
            print_info "跳过 $description 的符号链接"
            return 1
        fi
    else
        # 目标不存在，创建符号链接
        mkdir -p "$(dirname "$target")"
        ln -sf "$source" "$target"
        print_success "符号链接已创建: $target → $source"
        return 0
    fi
}

# 确保 moonraker.conf 中存在 [ace_status] 部分（如果缺失则创建文件）
ensure_moonraker_ace_status() {
    local conf="$1"

    if [ -f "$conf" ] && grep -qi '^[[:space:]]*\[ace_status\]' "$conf"; then
        print_success "moonraker.conf: [ace_status] 已存在"
        return 0
    fi

    mkdir -p "$(dirname "$conf")"
    if [ ! -f "$conf" ]; then
        printf '# Moonraker 配置\n\n' > "$conf"
        print_warning "在 $conf 创建了新的 moonraker.conf"
    fi

    printf '\n# ACE 状态扩展\n[ace_status]\n' >> "$conf"
    print_success "已将 [ace_status] 添加到 $conf"
}

# ============================================================================
# 主安装
# ============================================================================

main() {
    print_header "ACE 仪表板 - 交互式安装程序"
    
    local moonraker_changed=0
    local restart_moonraker=0

    # ========================================================================
    # 步骤 1: 确定源目录
    # ========================================================================
    
    print_info "定位 ACE 仪表板源文件..."
    
    # 默认: 假设脚本位于仓库根目录，源文件在 acepro_dashboard/
    DEFAULT_SOURCE_DIR="$SCRIPT_DIR/acepro_dashboard"
    
    # 检查默认是否存在；如果不存在，尝试 SCRIPT_DIR 本身
    if [ -d "$DEFAULT_SOURCE_DIR/web" ] && [ -d "$DEFAULT_SOURCE_DIR/moonraker" ]; then
        SOURCE_DIR="$DEFAULT_SOURCE_DIR"
        print_success "在 $SOURCE_DIR 中找到源文件"
    elif [ -d "$SCRIPT_DIR/web" ] && [ -d "$SCRIPT_DIR/moonraker" ]; then
        SOURCE_DIR="$SCRIPT_DIR"
        print_success "在 $SOURCE_DIR 中找到源文件"
    else
        print_warning "无法自动定位 'web' 和 'moonraker' 文件夹。"
        SOURCE_DIR=$(prompt_input "请输入包含 'web' 和 'moonraker' 子文件夹的目录的完整路径" "$DEFAULT_SOURCE_DIR")
    fi

    # 验证 SOURCE_DIR 下是否存在 web/ 和 moonraker/
    if [ ! -d "$SOURCE_DIR/web" ]; then
        print_error "在 $SOURCE_DIR/web 未找到 web 目录"
        exit 1
    fi
    if [ ! -d "$SOURCE_DIR/moonraker" ]; then
        print_error "在 $SOURCE_DIR/moonraker 未找到 moonraker 目录"
        exit 1
    fi

    # ========================================================================
    # 步骤 2: 收集用户输入的目标位置
    # ========================================================================
    
    print_info "安装源已确认: $SOURCE_DIR"
    print_info "源 web 文件: $SOURCE_DIR/web/"
    print_info "源 moonraker 组件: $SOURCE_DIR/moonraker/ace_status.py"

    # 2.1 询问 Mainsail
    if prompt_yes_no "\n将仪表板文件安装到 Mainsail 中?"; then
        DEFAULT_MAINSAIL_DIR="$INSTALL_HOME/mainsail"
        MAINSAIL_DIR=$(prompt_input "Mainsail 安装目录" "$DEFAULT_MAINSAIL_DIR")
        if [ ! -d "$MAINSAIL_DIR" ]; then
            print_warning "未找到 Mainsail 目录: $MAINSAIL_DIR"
            if ! prompt_yes_no "目录不存在。仍然创建符号链接?"; then
                MAINSAIL_DIR=""
            fi
        fi
    else
        MAINSAIL_DIR=""
    fi

    # 2.2 询问 Fluidd
    if prompt_yes_no "\n将仪表板文件安装到 Fluidd 中?"; then
        DEFAULT_FLUIDD_DIR="$INSTALL_HOME/fluidd"
        FLUIDD_DIR=$(prompt_input "Fluidd 安装目录" "$DEFAULT_FLUIDD_DIR")
        if [ ! -d "$FLUIDD_DIR" ]; then
            print_warning "未找到 Fluidd 目录: $FLUIDD_DIR"
            if ! prompt_yes_no "目录不存在。仍然创建符号链接?"; then
                FLUIDD_DIR=""
            fi
        fi
    else
        FLUIDD_DIR=""
    fi

    # 2.3 询问 Moonraker 组件
    if prompt_yes_no "\n安装 Moonraker ACE 状态组件?"; then
        DEFAULT_MOONRAKER_DIR="$INSTALL_HOME/moonraker"
        MOONRAKER_DIR=$(prompt_input "Moonraker 安装目录" "$DEFAULT_MOONRAKER_DIR")
        if [ ! -d "$MOONRAKER_DIR" ]; then
            print_error "未找到 Moonraker 目录: $MOONRAKER_DIR"
            print_info "跳过 Moonraker 组件安装。"
            MOONRAKER_DIR=""
        else
            DEFAULT_MOONRAKER_CONF="$INSTALL_HOME/printer_data/config/moonraker.conf"
            MOONRAKER_CONF=$(prompt_input "moonraker.conf 路径" "$DEFAULT_MOONRAKER_CONF")
        fi
    else
        MOONRAKER_DIR=""
    fi

    # ========================================================================
    # 步骤 3: 显示摘要并询问确认
    # ========================================================================
    
    echo ""
    print_header "安装摘要"
    
    if [ -n "$MAINSAIL_DIR" ]; then
        echo "Mainsail: $MAINSAIL_DIR"
    else
        echo "Mainsail: 未选择"
    fi
    if [ -n "$FLUIDD_DIR" ]; then
        echo "Fluidd:   $FLUIDD_DIR"
    else
        echo "Fluidd:   未选择"
    fi
    if [ -n "$MOONRAKER_DIR" ]; then
        echo "Moonraker 组件: $MOONRAKER_DIR/moonraker/components/"
        echo "Moonraker 配置:    $MOONRAKER_CONF"
    else
        echo "Moonraker 组件: 未选择"
    fi
    
    if ! prompt_yes_no "\n继续安装?"; then
        print_info "安装已取消"
        exit 0
    fi
    
    # ========================================================================
    # 步骤 4: 将 Web 文件链接到 Mainsail/Fluidd
    # ========================================================================
    
    if [ -n "$MAINSAIL_DIR" ]; then
        print_header "将仪表板文件链接到 Mainsail"
        for file in ace.html ace-dashboard.js ace-dashboard.css ace-dashboard-config.js favicon.svg; do
            create_or_replace_symlink "$SOURCE_DIR/web/$file" "$MAINSAIL_DIR/$file" "Mainsail $file"
        done
    fi
    
    if [ -n "$FLUIDD_DIR" ]; then
        print_header "将仪表板文件链接到 Fluidd"
        for file in ace.html ace-dashboard.js ace-dashboard.css ace-dashboard-config.js favicon.svg; do
            create_or_replace_symlink "$SOURCE_DIR/web/$file" "$FLUIDD_DIR/$file" "Fluidd $file"
        done
    fi
    
    # ========================================================================
    # 步骤 5: 安装 Moonraker 组件并更新配置
    # ========================================================================
    
    if [ -n "$MOONRAKER_DIR" ]; then
        print_header "安装 Moonraker ACE 状态组件"
        ACE_STATUS_SOURCE="$SOURCE_DIR/moonraker/ace_status.py"
        ACE_STATUS_TARGET="$MOONRAKER_DIR/moonraker/components/ace_status.py"
        if [ -f "$ACE_STATUS_SOURCE" ]; then
            create_or_replace_symlink "$ACE_STATUS_SOURCE" "$ACE_STATUS_TARGET" "Moonraker ace_status 组件"
            if [ $? -eq 0 ]; then
                moonraker_changed=1
            fi
        else
            print_error "在 $ACE_STATUS_SOURCE 未找到 ace_status.py"
        fi

        # 确保 moonraker.conf 包含 [ace_status]
        if [ -n "$MOONRAKER_CONF" ]; then
            ensure_moonraker_ace_status "$MOONRAKER_CONF"
            moonraker_changed=1
        fi
    fi
    
    # ========================================================================
    # 步骤 6: 设置源 Web 文件权限（确保可读）
    # ========================================================================
    
    print_header "设置文件权限"
    print_info "使源 web 文件对所有人可读 (644)..."
    if chmod 644 "$SOURCE_DIR"/web/* 2>/dev/null; then
        print_success "Web 文件权限已设置"
    else
        print_warning "无法设置权限（文件可能已经正确？）"
    fi
    
    # ========================================================================
    # 步骤 7: 如需要重启 Moonraker
    # ========================================================================
    
    if [ $moonraker_changed -eq 1 ]; then
        print_header "Moonraker 重启"
        echo "Moonraker 组件和/或配置已更新。"
        if prompt_yes_no "现在重启 Moonraker 服务?"; then
            print_info "正在重启 Moonraker..."
            sudo systemctl restart moonraker
            if [ $? -eq 0 ]; then
                print_success "Moonraker 已重启"
            else
                print_error "重启 Moonraker 失败"
            fi
        else
            print_warning "Moonraker 未重启。您可以手动重启:"
            echo "  sudo systemctl restart moonraker"
        fi
    fi
    
    # ========================================================================
    # 步骤 8: 安装完成
    # ========================================================================
    
    print_header "安装完成！"
    
    cat << EOF
ACE 仪表板已安装。

- Web 文件链接到: ${MAINSAIL_DIR:-无} ${FLUIDD_DIR:-无}
- Moonraker 组件: ${MOONRAKER_DIR:-未安装}
- Moonraker 配置已更新: ${MOONRAKER_CONF:-无更改}

后续步骤:
  1. 在以下位置打开您的仪表板:
     http://<your-printer-ip>/ace.html

  2. 如需要，调整 ace-dashboard-config.js 中的 API 主机
     （编辑源位置的文件: $SOURCE_DIR/web/ace-dashboard-config.js）

  3. （可选）查看提供的 nginx 配置片段:
     $SOURCE_DIR/web/ace_dashboard.nginx.conf
     如果您需要在同一主机上代理 Moonraker，可以使用此配置。

  4. 享受从浏览器控制您的 ACE 设备！
EOF
}

# ============================================================================
# 入口点
# ============================================================================

if [ "${BASH_SOURCE[0]}" == "${0}" ]; then
    main "$@"
fi
