#!/bin/bash

# =============================================================================
# SolisACE 交互式安装脚本
# 功能：
#   1. 安装 Klipper extras (ace.py, temperature_ace.py)
#   2. 安装配置文件 (ace.cfg) 并引用至 printer.cfg
#   3. 安装 Moonraker 组件 (ace_status.py)
#   4. 配置 Moonraker 扩展及更新管理器
#   5. 安装 Web 仪表板至 Mainsail / Fluidd 或跳过
#   6. 配置 API 端点（仅当安装 Web 界面时）
#   7. 设置文件权限（仅当安装 Web 界面时）
#   8. 重启服务
# 使用 -u 参数卸载所有安装项
# =============================================================================

set -e

# ----------------------------- 全局变量 ----------------------------------
SCRIPT_VERSION="1.2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_USER="${SUDO_USER:-$(id -un)}"
INSTALL_HOME="$(getent passwd "$INSTALL_USER" 2>/dev/null | cut -d: -f6 || echo "$HOME")"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 默认路径（可交互修改）
KLIPPER_HOME="${INSTALL_HOME}/klipper"
KLIPPER_CONFIG_HOME="${INSTALL_HOME}/printer_data/config"
MOONRAKER_HOME="${INSTALL_HOME}/moonraker"
MOONRAKER_CONFIG="${KLIPPER_CONFIG_HOME}/moonraker.conf"
PRINTER_CFG="${KLIPPER_CONFIG_HOME}/printer.cfg"

# 源文件位置
SRC_EXTRAS="${SCRIPT_DIR}/extras"
SRC_MOONRAKER="${SCRIPT_DIR}/moonraker"
SRC_WEB="${SCRIPT_DIR}/web-interface"
SRC_ACE_CFG="${SCRIPT_DIR}/ace.cfg"
SRC_REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"

# 服务名称
KLIPPER_SERVICE="klipper"
MOONRAKER_SERVICE="moonraker"

# Web 安装标志
INSTALL_WEB=0

# ----------------------------- 辅助函数 ----------------------------------
print_header() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

print_info()   { echo -e "${BLUE}ℹ ${1}${NC}"; }
print_success(){ echo -e "${GREEN}✓ ${1}${NC}"; }
print_warning(){ echo -e "${YELLOW}⚠ ${1}${NC}"; }
print_error()  { echo -e "${RED}✗ ${1}${NC}"; }

prompt_yes_no() {
    local prompt="$1"
    local response
    while true; do
        read -p "$(echo -e ${BLUE}${prompt}${NC} [y/N]: )" response
        case "$response" in
            [yY][eE][sS]|[yY]) return 0 ;;
            [nN][oO]|[nN]|"")   return 1 ;;
            *) echo "请回答 y 或 n" ;;
        esac
    done
}

prompt_input() {
    local prompt="$1"
    local default="$2"
    local response
    read -p "$(echo -e ${BLUE}${prompt}${NC} [${default}]: )" response
    echo "${response:-$default}"
}

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

create_symlink() {
    local src="$1"
    local dest="$2"
    local desc="$3"
    
    if [ ! -e "$src" ]; then
        print_error "源文件不存在: $src"
        return 1
    fi
    
    mkdir -p "$(dirname "$dest")"
    if [ -L "$dest" ] || [ -e "$dest" ]; then
        if [ -L "$dest" ]; then
            print_warning "符号链接已存在: $dest → $(readlink "$dest")"
        else
            print_warning "文件/目录已存在: $dest"
        fi
        if prompt_yes_no "是否替换？"; then
            rm -f "$dest"
        else
            print_info "跳过 ${desc}"
            return 1
        fi
    fi
    ln -sf "$src" "$dest"
    print_success "${desc} 符号链接已创建: $dest → $src"
    return 0
}

add_line_to_file_if_missing() {
    local file="$1"
    local line="$2"
    if ! grep -qF "$line" "$file" 2>/dev/null; then
        echo "$line" >> "$file"
        print_success "已添加行至 $file"
    else
        print_info "行已存在于 $file"
    fi
}

ensure_printer_cfg_include() {
    local include_line="[include ace.cfg]"
    if ! grep -qF "$include_line" "$PRINTER_CFG" 2>/dev/null; then
        print_info "正在将 '[include ace.cfg]' 插入 printer.cfg 顶部..."
        backup_file "$PRINTER_CFG"
        sed -i "1i $include_line" "$PRINTER_CFG"
        print_success "已添加引用至 printer.cfg"
    else
        print_success "printer.cfg 已包含 ace.cfg 引用"
    fi
}

# ----------------------------- 安装步骤 ----------------------------------
install_requirements() {
    print_header "0. 安装 Python 依赖"
    if [ ! -f "$SRC_REQUIREMENTS" ]; then
        print_warning "未找到 requirements.txt，跳过依赖安装"
        return
    fi

    local pip_cmd="pip3"
    if [ -d "${INSTALL_HOME}/klippy-env" ]; then
        pip_cmd="${INSTALL_HOME}/klippy-env/bin/pip3"
    fi

    print_info "使用 $pip_cmd 安装依赖..."
    if $pip_cmd install -r "$SRC_REQUIREMENTS" --quiet; then
        print_success "依赖安装完成"
    else
        print_error "依赖安装失败，请检查网络或手动安装 pyserial"
        exit 1
    fi
}

install_klipper_extras() {
    print_header "1. 安装 Klipper 扩展"
    create_symlink "$SRC_EXTRAS/ace.py" "$KLIPPER_HOME/klippy/extras/ace.py" "ace.py"
    create_symlink "$SRC_EXTRAS/temperature_ace.py" "$KLIPPER_HOME/klippy/extras/temperature_ace.py" "temperature_ace.py"
}

install_config() {
    print_header "2. 安装配置文件"
    if [ ! -f "$SRC_ACE_CFG" ]; then
        print_error "未找到 ace.cfg: $SRC_ACE_CFG"
        return 1
    fi
    if [ -f "$KLIPPER_CONFIG_HOME/ace.cfg" ]; then
        print_warning "ace.cfg 已存在，将备份后覆盖"
        backup_file "$KLIPPER_CONFIG_HOME/ace.cfg"
    fi
    cp "$SRC_ACE_CFG" "$KLIPPER_CONFIG_HOME/"
    print_success "ace.cfg 已复制到 $KLIPPER_CONFIG_HOME"
    ensure_printer_cfg_include
}

install_moonraker_component() {
    print_header "3. 安装 Moonraker 组件"
    create_symlink "$SRC_MOONRAKER/ace_status.py" "$MOONRAKER_HOME/moonraker/components/ace_status.py" "ace_status.py"
    
    print_info "检查 moonraker.conf 中的 [ace_status]..."
    if ! grep -qi '^[[:space:]]*\[ace_status\]' "$MOONRAKER_CONFIG" 2>/dev/null; then
        backup_file "$MOONRAKER_CONFIG"
        echo -e "\n[ace_status]" >> "$MOONRAKER_CONFIG"
        print_success "已添加 [ace_status] 到 moonraker.conf"
    else
        print_success "[ace_status] 已存在于 moonraker.conf"
    fi
}

add_update_manager() {
    print_header "4. 添加更新管理器"
    local updater_section="[update_manager SolisACE]
type: git_repo
path: ${SCRIPT_DIR}
primary_branch: main
origin: https://github.com/Solismuchengxue/SolisACE.git
managed_services: klipper"
    
    if grep -qF "[update_manager SolisACE]" "$MOONRAKER_CONFIG" 2>/dev/null; then
        print_success "更新管理器已存在"
    else
        backup_file "$MOONRAKER_CONFIG"
        echo -e "\n$updater_section" >> "$MOONRAKER_CONFIG"
        print_success "已添加更新管理器配置"
    fi
}

install_web_dashboard() {
    print_header "5. 安装 Web 仪表板"
    
    if [ ! -d "$SRC_WEB" ]; then
        print_error "Web 源目录不存在: $SRC_WEB"
        return 1
    fi

    echo "请选择要安装 Web 仪表板的目标界面："
    echo "  1) Mainsail"
    echo "  2) Fluidd"
    echo "  3) 都不安装"
    local choice
    while true; do
        read -p "$(echo -e ${BLUE}请输入选择 [1-3]${NC}: )" choice
        case "$choice" in
            1) TARGET="mainsail"; break;;
            2) TARGET="fluidd"; break;;
            3) TARGET="none"; break;;
            *) echo "无效输入，请重试";;
        esac
    done

    if [ "$TARGET" = "none" ]; then
        print_info "跳过 Web 仪表板安装。"
        INSTALL_WEB=0
        return 0
    fi

    INSTALL_WEB=1
    local target_dir=""
    if [ "$TARGET" = "mainsail" ]; then
        target_dir=$(prompt_input "Mainsail 安装目录" "${INSTALL_HOME}/mainsail")
    else
        target_dir=$(prompt_input "Fluidd 安装目录" "${INSTALL_HOME}/fluidd")
    fi

    if [ ! -d "$target_dir" ]; then
        print_warning "目录 $target_dir 不存在，将尝试创建"
        mkdir -p "$target_dir"
    fi

    local web_files=("ace.html" "ace-dashboard.js" "ace-dashboard.css" "ace-dashboard-config.js" "favicon.svg")
    for file in "${web_files[@]}"; do
        create_symlink "$SRC_WEB/$file" "$target_dir/$file" "${TARGET^} $file"
    done
    print_success "Web 文件已链接到 $target_dir"
}

configure_api_endpoint() {
    print_header "6. 配置 API 端点"
    local config_file="$SRC_WEB/ace-dashboard-config.js"
    if [ ! -f "$config_file" ]; then
        print_error "配置文件不存在: $config_file"
        return 1
    fi
    
    print_info "当前 API 端点配置:"
    grep "apiBase:" "$config_file" | head -1
    
    if prompt_yes_no "是否修改 API 端点？"; then
        local new_api=$(prompt_input "请输入 Moonraker API 地址 (例如 http://192.168.1.100:7125)" "http://localhost:7125")
        backup_file "$config_file"
        sed -i "s|apiBase:.*|apiBase: '${new_api}',|" "$config_file"
        print_success "API 端点已更新为 $new_api"
    fi
}

set_permissions() {
    print_header "7. 设置文件权限"
    print_info "设置 Web 源文件权限为 644..."
    chmod 644 "$SRC_WEB"/* 2>/dev/null || true
    print_success "权限设置完成"
}

restart_services() {
    print_header "8. 重启服务"
    if prompt_yes_no "是否立即重启 Klipper 和 Moonraker 服务？"; then
        sudo systemctl restart $KLIPPER_SERVICE && print_success "Klipper 已重启"
        sudo systemctl restart $MOONRAKER_SERVICE && print_success "Moonraker 已重启"
    else
        print_warning "请稍后手动重启服务:"
        echo "  sudo systemctl restart klipper moonraker"
    fi
}

# ----------------------------- 卸载流程 ----------------------------------
uninstall_all() {
    print_header "卸载 SolisACE"
    
    print_info "正在移除 Klipper 扩展符号链接..."
    rm -f "$KLIPPER_HOME/klippy/extras/ace.py" 2>/dev/null && print_success "已移除 ace.py"
    rm -f "$KLIPPER_HOME/klippy/extras/temperature_ace.py" 2>/dev/null && print_success "已移除 temperature_ace.py"
    
    print_info "正在移除 Moonraker 组件符号链接..."
    rm -f "$MOONRAKER_HOME/moonraker/components/ace_status.py" 2>/dev/null && print_success "已移除 ace_status.py"
    
    print_info "正在移除 Web 仪表板符号链接（常见位置）..."
    local web_files=("ace.html" "ace-dashboard.js" "ace-dashboard.css" "ace-dashboard-config.js" "favicon.svg")
    for dir in "${INSTALL_HOME}/mainsail" "${INSTALL_HOME}/fluidd"; do
        if [ -d "$dir" ]; then
            for file in "${web_files[@]}"; do
                rm -f "$dir/$file" 2>/dev/null
            done
            print_info "已清理 $dir 中的 Web 文件"
        fi
    done
    
    print_info "注意：配置文件及 printer.cfg/moonraker.conf 中的引用需要手动移除："
    echo "  - $KLIPPER_CONFIG_HOME/ace.cfg"
    echo "  - printer.cfg 中的 '[include ace.cfg]'"
    echo "  - moonraker.conf 中的 '[ace_status]' 及 '[update_manager SolisACE]' 段落"
    
    if prompt_yes_no "是否立即重启服务？"; then
        sudo systemctl restart $KLIPPER_SERVICE $MOONRAKER_SERVICE
        print_success "服务已重启"
    fi
}

# ----------------------------- 主流程 ----------------------------------
show_help() {
    cat << EOF
用法: $0 [选项]

选项:
  -u          卸载 SolisACE
  -h          显示此帮助信息
  -v          显示版本信息

不带选项运行时将启动交互式安装向导。
EOF
}

show_version() {
    echo "SolisACE 安装脚本 v${SCRIPT_VERSION}"
}

# 解析命令行参数
UNINSTALL=0
while getopts "uhv" opt; do
    case $opt in
        u) UNINSTALL=1 ;;
        h) show_help; exit 0 ;;
        v) show_version; exit 0 ;;
        *) show_help; exit 1 ;;
    esac
done

# 检查是否以 root 运行
if [ "$EUID" -eq 0 ] && [ "$(uname -m)" != "mips" ]; then
    print_error "请勿以 root 用户运行此脚本。"
    exit 1
fi

# 执行相应操作
if [ "$UNINSTALL" -eq 1 ]; then
    uninstall_all
    exit 0
fi

# 交互式安装
print_header "SolisACE 交互式安装向导 v${SCRIPT_VERSION}"

# 确认或修改默认路径
print_info "检测到以下默认路径，如有不符请修改："
KLIPPER_HOME=$(prompt_input "Klipper 安装目录" "$KLIPPER_HOME")
KLIPPER_CONFIG_HOME=$(prompt_input "Klipper 配置目录" "$KLIPPER_CONFIG_HOME")
MOONRAKER_HOME=$(prompt_input "Moonraker 安装目录" "$MOONRAKER_HOME")
MOONRAKER_CONFIG="${KLIPPER_CONFIG_HOME}/moonraker.conf"
PRINTER_CFG="${KLIPPER_CONFIG_HOME}/printer.cfg"

# 验证关键路径
if [ ! -d "$KLIPPER_HOME/klippy/extras" ]; then
    print_error "Klipper extras 目录不存在: $KLIPPER_HOME/klippy/extras"
    exit 1
fi
if [ ! -d "$MOONRAKER_HOME/moonraker/components" ]; then
    print_error "Moonraker components 目录不存在: $MOONRAKER_HOME/moonraker/components"
    exit 1
fi

# 执行安装步骤
install_requirements
install_klipper_extras
install_config
install_moonraker_component
add_update_manager
install_web_dashboard

if [ $INSTALL_WEB -eq 1 ]; then
    configure_api_endpoint
    set_permissions
else
    print_info "已跳过 API 端点配置和权限设置（未安装 Web 界面）"
fi

restart_services

print_header "安装成功完成！"
cat << EOF

SolisACE 已成功安装。

- Klipper 扩展:     $KLIPPER_HOME/klippy/extras/ace.py
                    $KLIPPER_HOME/klippy/extras/temperature_ace.py
- Moonraker 扩展:   $MOONRAKER_HOME/moonraker/components/ace_status.py
- Web 仪表板:       $([ $INSTALL_WEB -eq 1 ] && echo "已安装" || echo "未安装")
                    $([ $INSTALL_WEB -eq 1 ] && echo "文件位于: ${INSTALL_HOME}/mainsail 或 ${INSTALL_HOME}/fluidd (根据选择)")
- 配置文件:         $KLIPPER_CONFIG_HOME/ace.cfg
                    $KLIPPER_CONFIG_HOME/printer.cfg (包含 [include ace.cfg])
                    $KLIPPER_CONFIG_HOME/moonraker.conf (包含 [ace_status] 及 [update_manager SolisACE])

如需卸载，请运行: $0 -u

EOF