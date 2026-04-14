#!/bin/bash

# =============================================================================
# ACE 仪表板 - 交互式卸载程序
# =============================================================================
# 此脚本从 Mainsail/Fluidd 中移除 ACE 仪表板的符号链接，
# 移除 Moonraker 组件，并清理相关配置。
#
# 使用方法: ./uninstall.sh
# =============================================================================

set -u

# 输出颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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

# 如果存在符号链接则移除（仅在它是符号链接时）
remove_symlink() {
    local file="$1"
    local description="$2"
    if [ -L "$file" ]; then
        local target=$(readlink "$file")
        rm -f "$file"
        print_success "已移除符号链接: $file (指向 $target)"
        return 0
    elif [ -e "$file" ]; then
        print_warning "$file 存在但不是符号链接。已跳过。"
        return 1
    else
        print_info "$file 未找到（已移除）。"
        return 0
    fi
}

# 从 moonraker.conf 中移除 [ace_status] 部分（先创建备份）
remove_moonraker_section() {
    local conf="$1"
    if [ ! -f "$conf" ]; then
        print_warning "$conf 不存在，无需操作。"
        return 0
    fi

    # 检查该节是否存在
    if ! grep -qi '^[[:space:]]*\[ace_status\]' "$conf"; then
        print_info "在 $conf 中未找到 [ace_status] 部分"
        return 0
    fi

    # 创建备份
    local timestamp=$(date +"%Y%m%d_%H%M%S")
    local backup="${conf}.backup_uninstall_${timestamp}"
    cp "$conf" "$backup"
    print_success "已备份 $conf → $backup"

    # 使用 awk 删除该节
    # 这会删除从第一个匹配 [ace_status] 的行（可能包含空格）
    # 直到下一行以 '[' 开头（下一节）或文件结束。
    awk '
        BEGIN { in_section = 0; output = 1; }
        /^[[:space:]]*\[ace_status\]/ { in_section = 1; output = 0; next; }
        /^[[:space:]]*\[/ && in_section { in_section = 0; output = 1; }
        output { print }
    ' "$conf" > "${conf}.tmp" && mv "${conf}.tmp" "$conf"

    print_success "已从 $conf 中移除 [ace_status] 部分"
}

# ============================================================================
# 主卸载流程
# ============================================================================

main() {
    print_header "ACE 仪表板 - 交互式卸载程序"

    # ========================================================================
    # 收集目标位置的用户输入
    # ========================================================================

    # 1. Mainsail
    if prompt_yes_no "\n从 Mainsail 中移除仪表板文件?"; then
        DEFAULT_MAINSAIL_DIR="$INSTALL_HOME/mainsail"
        MAINSAIL_DIR=$(prompt_input "Mainsail 安装目录" "$DEFAULT_MAINSAIL_DIR")
        if [ ! -d "$MAINSAIL_DIR" ]; then
            print_warning "目录 $MAINSAIL_DIR 不存在。"
            if ! prompt_yes_no "仍然继续?"; then
                MAINSAIL_DIR=""
            fi
        fi
    else
        MAINSAIL_DIR=""
    fi

    # 2. Fluidd
    if prompt_yes_no "\n从 Fluidd 中移除仪表板文件?"; then
        DEFAULT_FLUIDD_DIR="$INSTALL_HOME/fluidd"
        FLUIDD_DIR=$(prompt_input "Fluidd 安装目录" "$DEFAULT_FLUIDD_DIR")
        if [ ! -d "$FLUIDD_DIR" ]; then
            print_warning "目录 $FLUIDD_DIR 不存在。"
            if ! prompt_yes_no "仍然继续?"; then
                FLUIDD_DIR=""
            fi
        fi
    else
        FLUIDD_DIR=""
    fi

    # 3. Moonraker 组件
    if prompt_yes_no "\n移除 Moonraker ACE 状态组件?"; then
        DEFAULT_MOONRAKER_DIR="$INSTALL_HOME/moonraker"
        MOONRAKER_DIR=$(prompt_input "Moonraker 安装目录" "$DEFAULT_MOONRAKER_DIR")
        if [ ! -d "$MOONRAKER_DIR" ]; then
            print_error "未找到 Moonraker 目录: $MOONRAKER_DIR"
            print_info "跳过 Moonraker 组件移除。"
            MOONRAKER_DIR=""
        else
            DEFAULT_MOONRAKER_CONF="$INSTALL_HOME/printer_data/config/moonraker.conf"
            MOONRAKER_CONF=$(prompt_input "moonraker.conf 路径" "$DEFAULT_MOONRAKER_CONF")
        fi
    else
        MOONRAKER_DIR=""
    fi

    # ========================================================================
    # 摘要
    # ========================================================================

    echo ""
    print_header "卸载摘要"
    if [ -n "$MAINSAIL_DIR" ]; then
        echo "Mainsail:  $MAINSAIL_DIR"
    else
        echo "Mainsail:  未选择"
    fi
    if [ -n "$FLUIDD_DIR" ]; then
        echo "Fluidd:    $FLUIDD_DIR"
    else
        echo "Fluidd:    未选择"
    fi
    if [ -n "$MOONRAKER_DIR" ]; then
        echo "Moonraker 组件: $MOONRAKER_DIR/moonraker/components/ace_status.py"
        echo "Moonraker 配置:    $MOONRAKER_CONF"
    else
        echo "Moonraker 组件: 未选择"
    fi

    if ! prompt_yes_no "\n继续卸载?"; then
        print_info "已取消卸载。"
        exit 0
    fi

    # ========================================================================
    # 从 Mainsail/Fluidd 中移除符号链接
    # ========================================================================

    if [ -n "$MAINSAIL_DIR" ]; then
        print_header "从 Mainsail 中移除仪表板文件"
        for file in ace.html ace-dashboard.js ace-dashboard.css ace-dashboard-config.js favicon.svg; do
            remove_symlink "$MAINSAIL_DIR/$file" "Mainsail $file"
        done
    fi

    if [ -n "$FLUIDD_DIR" ]; then
        print_header "从 Fluidd 中移除仪表板文件"
        for file in ace.html ace-dashboard.js ace-dashboard.css ace-dashboard-config.js favicon.svg; do
            remove_symlink "$FLUIDD_DIR/$file" "Fluidd $file"
        done
    fi

    # ========================================================================
    # Remove Moonraker component
    # ========================================================================

    moonraker_changed=0

    if [ -n "$MOONRAKER_DIR" ]; then
        print_header "移除 Moonraker ACE 状态组件"
        COMPONENT_FILE="$MOONRAKER_DIR/moonraker/components/ace_status.py"
        remove_symlink "$COMPONENT_FILE" "Moonraker ace_status 组件"
        if [ $? -eq 0 ] && [ -L "$COMPONENT_FILE" ]; then
            moonraker_changed=1
        fi

        if [ -n "$MOONRAKER_CONF" ]; then
            if prompt_yes_no "\nRemove [ace_status] section from $MOONRAKER_CONF?"; then
                remove_moonraker_section "$MOONRAKER_CONF"
                moonraker_changed=1
            else
                print_info "已跳过修改 moonraker.conf。"
            fi
        fi
    fi

    # ========================================================================
    # 可选：删除生成的配置文件
    # ========================================================================

    if prompt_yes_no "\n从打印机配置目录中删除生成的配置文件 (ace_dashboard_settings.json、ace_orca_presets.json)?"; then
        PRINTER_CONFIG_DIR="$INSTALL_HOME/printer_data/config"
        if [ -d "$PRINTER_CONFIG_DIR" ]; then
            rm -f "$PRINTER_CONFIG_DIR/ace_dashboard_settings.json"
            rm -f "$PRINTER_CONFIG_DIR/ace_orca_presets.json"
            print_success "已从 $PRINTER_CONFIG_DIR 删除配置文件"
        else
            print_warning "未找到打印机配置目录: $PRINTER_CONFIG_DIR"
        fi
    else
        print_info "已跳过删除配置文件。"
    fi

    # ========================================================================
    # 如需重启 Moonraker
    # ========================================================================

    if [ $moonraker_changed -eq 1 ]; then
        print_header "重启 Moonraker"
        echo "Moonraker 组件和/或配置已被移除。"
        if prompt_yes_no "现在重启 Moonraker 服务?"; then
            print_info "正在重启 Moonraker..."
            sudo systemctl restart moonraker
            if [ $? -eq 0 ]; then
                print_success "Moonraker 已重启"
            else
                print_error "重启 Moonraker 失败"
            fi
        else
            print_warning "Moonraker 尚未重启。您可以手动重启："
            echo "  sudo systemctl restart moonraker"
        fi
    fi

    # ========================================================================
    # 卸载完成
    # ========================================================================

    print_header "卸载完成！"
    echo "ACE 仪表板已从您的系统中移除。"
    echo ""
    echo "如果您还想删除原始源码仓库，可以手动执行："
    echo "  rm -rf $(dirname "$(readlink -f "$0")")"
    echo ""
    echo "感谢您使用 ACE 仪表板！"
}

# ============================================================================
# 入口点
# ============================================================================

if [ "${BASH_SOURCE[0]}" == "${0}" ]; then
    main "$@"
fi