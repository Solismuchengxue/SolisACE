#!/bin/sh

# =============================================================================
# ACE 仪表板 - 交互式安装程序
# =============================================================================
# 1. 安装 ValgACE 组件到 Klipper
# 2. 创建 ACE 配置文件
# 3. 创建 Moonraker 组件
# 4. 创建 Klipper 配置文件
# 5. 创建 Moonraker 配置文件
# 6. 将 ACE 仪表板 Web 文件安装到 Mainsail/Fluidd 中
# 7. 重启 Klipper 和 Moonraker 服务
# 并安装 Moonraker 组件以获取 ACE 状态。
#
# 使用方法: ./install.sh
# =============================================================================

# ============================================================================
# 全局参数
# ============================================================================
VERSION="1.2"                                               # 脚本版本
IS_MIPS=$(uname -m | grep -q "mips" && echo 1 || echo 0)    # 是否MIPS架构

# 配置路径
KLIPPER_HOME="${HOME}/klipper"
KLIPPER_CONFIG_HOME="${HOME}/printer_data/config"
MOONRAKER_CONFIG_DIR="${HOME}/printer_data/config"
MOONRAKER_HOME="${HOME}/moonraker"
SRCDIR="$PWD"
KLIPPER_ENV="${HOME}/klippy-env/bin"

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

# 服务名称
KLIPPER_SERVICE="klipper"
MOONRAKER_SERVICE="moonraker"

usage() { 
    echo "用法: $0 [-u] [-h] [-v]" 1>&2
    echo "选项:" 1>&2
    echo "  -u    卸载 ValgACE" 1>&2
    echo "  -h    显示此帮助" 1>&2
    echo "  -v    显示版本" 1>&2
    exit 1
}

show_version() {
    echo "ValgACE 安装程序 v${VERSION}"
    exit 0
}

# 解析参数
UNINSTALL=0
while getopts "uhv" arg; do
   case $arg in
       u) UNINSTALL=1;;
       h) usage;;
       v) show_version;;
       *) usage;;
   esac
done

verify_ready() {
  if [ "$IS_MIPS" -ne 1 ]; then
    if [ "$EUID" -eq 0 ]; then
        echo "[错误] 此脚本不能以 root 用户运行。退出。"
        exit 1
    fi
  else
    echo "[警告] 在 MIPS 系统上运行 - 期望 root 权限"
  fi
}

check_service() {
    local service=$1
    if ! sudo systemctl is-enabled --quiet "$service" 2>/dev/null; then
        echo "[错误] 服务 $service 未找到或未启用"
        return 1
    fi
    return 0
}

check_folders() {
    local missing=0
    
    if [ ! -d "$KLIPPER_HOME/klippy/extras/" ]; then
        echo "[错误] 在 $KLIPPER_HOME 中未找到 Klipper 安装"
        missing=1
    fi

    if [ ! -d "${KLIPPER_CONFIG_HOME}/" ]; then
        echo "[错误] 配置目录未找到: $KLIPPER_CONFIG_HOME"
        missing=1
    fi

    if [ ! -f "${MOONRAKER_CONFIG_DIR}/moonraker.conf" ]; then
        echo "[错误] 在 $MOONRAKER_CONFIG_DIR 中未找到 moonraker.conf"
        missing=1
    fi

    if [ ! -d "${KLIPPER_ENV}" ]; then
        echo "[错误] Klipper 环境目录未找到: $KLIPPER_ENV"
        missing=1
    fi

    # Moonraker 主目录（用于链接组件）
    if [ ! -d "${MOONRAKER_HOME}" ]; then
        echo "[错误] Moonraker 主目录未找到: ${MOONRAKER_HOME}"
        missing=1
    fi

    if [ $missing -ne 0 ]; then
        exit 1
    fi

    echo "[确定] 找到所有必需的目录和文件"
}

link_extension() {
    if [ ! -f "${SRCDIR}/extras/ace.py" ]; then
        echo "[错误] 源文件 ${SRCDIR}/extras/ace.py 未找到"
        exit 1
    fi

    echo -n "将扩展链接到 Klipper... "
    if ln -sf "${SRCDIR}/extras/ace.py" "${KLIPPER_HOME}/klippy/extras/ace.py"; then
        echo "[确定]"
    else
        echo "[失败]"
        exit 1
    fi
}

link_temperature_sensor() {
    if [ ! -f "${SRCDIR}/extras/temperature_ace.py" ]; then
        echo "[错误] 源文件 ${SRCDIR}/extras/temperature_ace.py 未找到"
        exit 1
    fi

    echo -n "将温度传感器链接到 Klipper... "
    if ln -sf "${SRCDIR}/extras/temperature_ace.py" "${KLIPPER_HOME}/klippy/extras/temperature_ace.py"; then
        echo "[确定]"
    else
        echo "[失败]"
        exit 1
    fi
}

link_moonraker_component() {
    if [ ! -f "${SRCDIR}/moonraker/ace_status.py" ]; then
        echo "[错误] 源文件 ${SRCDIR}/moonraker/ace_status.py 未找到"
        exit 1
    fi

    # 确保目标目录存在
    DEST_DIR="${MOONRAKER_HOME}/moonraker/components"
    mkdir -p "${DEST_DIR}"

    echo -n "链接 Moonraker 组件... "
    if ln -sf "${SRCDIR}/moonraker/ace_status.py" "${DEST_DIR}/ace_status.py"; then
        echo "[确定]"
    else
        echo "[失败]"
        exit 1
    fi

    # 确保moonraker.conf配置文件中存在相应的配置节
    if ! grep -q "^\[ace_status\]" "${MOONRAKER_CONFIG_DIR}/moonraker.conf"; then
        echo -n "将 [ace_status] 添加到 moonraker.conf... "
        printf "\n[ace_status]\n" >> "${MOONRAKER_CONFIG_DIR}/moonraker.conf" && echo "[确定]" || echo "[失败]"
    else
        echo "[跳过] ([ace_status] 已存在于 moonraker.conf 中)"
    fi
}

copy_config() {
    echo -n "复制配置文件... "
    if [ ! -f "${KLIPPER_CONFIG_HOME}/ace.cfg" ]; then
        if cp "${SRCDIR}/ace.cfg" "${KLIPPER_CONFIG_HOME}/"; then
            echo "[确定]"
        else
            echo "[失败]"
            exit 1
        fi
    else
        echo "[跳过] (已存在)"
    fi
}

install_requirements() {
    echo -n "安装依赖... "
    if [ ! -f "${SRCDIR}/requirements.txt" ]; then
        echo "[跳过] (未找到 requirements.txt)"
        return
    fi

    if "${KLIPPER_ENV}/pip3" install -r "${SRCDIR}/requirements.txt"; then
        echo "[确定]"
    else
        echo "[失败]"
        exit 1
    fi
}

uninstall() {
    echo -n "卸载 ValgACE... "
    local removed=0
    
    if [ -f "${KLIPPER_HOME}/klippy/extras/ace.py" ]; then
        if rm -f "${KLIPPER_HOME}/klippy/extras/ace.py"; then
            echo "[确定] ace.py 已移除"
            removed=1
        else
            echo "[失败]"
            exit 1
        fi
    fi
    
    if [ -f "${KLIPPER_HOME}/klippy/extras/temperature_ace.py" ]; then
        if rm -f "${KLIPPER_HOME}/klippy/extras/temperature_ace.py"; then
            echo "[确定] temperature_ace.py 已移除"
            removed=1
        else
            echo "[失败]"
            exit 1
        fi
    fi
    
    if [ $removed -eq 0 ]; then
        echo "[跳过] (未找到 ValgACE 文件)"
    else
        echo "注意: 您需要手动移除:"
        echo "1. moonraker.conf 中的 [update_manager ValgACE] 部分"
        echo "2. printer.cfg 中的所有 ValgACE 相关配置"
    fi
}

restart_service() {
    local service=$1
    echo -n "重启 $service... "
    if sudo systemctl restart "$service"; then
        echo "[确定]"
    else
        echo "[失败]"
        exit 1
    fi
}

stop_service() {
    local service=$1
    echo -n "停止 $service... "
    if sudo systemctl stop "$service"; then
        echo "[确定]"
    else
        echo "[失败]"
        exit 1
    fi
}

start_service() {
    local service=$1
    echo -n "启动 $service... "
    if sudo systemctl start "$service"; then
        echo "[确定]"
    else
        echo "[失败]"
        exit 1
    fi
}

add_updater() {
    echo -n "将更新管理器添加到 moonraker.conf... "
    if grep -q "\[update_manager ValgACE\]" "${MOONRAKER_CONFIG_DIR}/moonraker.conf"; then
        echo "[跳过] (已存在)"
        return
    fi

    cat << EOF >> "${MOONRAKER_CONFIG_DIR}/moonraker.conf"

[update_manager ValgACE]
type: git_repo
path: ${SRCDIR}
primary_branch: main
origin: https://github.com/agrloki/ValgACE.git
managed_services: klipper
EOF

    echo "[确定]"
}

# 主要流程
verify_ready
check_folders
check_service "$KLIPPER_SERVICE" || exit 1
check_service "$MOONRAKER_SERVICE" || exit 1

stop_service "$KLIPPER_SERVICE"

if [ "$UNINSTALL" -eq 1 ]; then
    uninstall
else
    install_requirements
    link_extension
    link_temperature_sensor
    link_moonraker_component
    copy_config
    add_updater
    restart_service "$MOONRAKER_SERVICE"
fi

start_service "$KLIPPER_SERVICE"

echo "操作成功完成"
exit 0

# ============================================================================
# 主安装
# ============================================================================


# ============================================================================
# 入口点
# ============================================================================

if [ "${BASH_SOURCE[0]}" == "${0}" ]; then
    main "$@"
fi