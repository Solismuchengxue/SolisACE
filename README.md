# ValgACE - Anycubic 彩色引擎专业版驱动程序

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**ValgACE** - Klipper 模块，提供 Anycubic Color Engine Pro (ACE Pro) 自动换料装置的完整管理功能。

[English](./README_EN.md) | **简体中文** | [Русский](./README_RU.md) | [日本語](./README_JA.md)

**主要仓库迁移到 [GitVerse](https://gitverse.ru/Agrloki/ValgAce)**

**ace-solo** [ace-solo](https://github.com/agrloki/ace-solo) 独立 Python 应用程序，用于无需 Klipper 管理 Anycubic ACE Pro。

**acepro-mmu-dashboard** [acepro-mmu-dashboard](https://github.com/ducati1198/acepro-mmu-dashboard) @ducati1198 提供的替代网页界面

## 📋 目录

- [简介](#简介)
- [功能](#功能)
- [系统要求](#系统要求)
- [快速开始](#快速开始)
- [设备连接](#设备连接)
- [文档](#文档)
- [支持](#支持)
- [致谢](#致谢)

## 简介

ValgACE 是一个功能完整的驱动程序，用于通过 Klipper 管理 Anycubic Color Engine Pro 设备。该驱动程序提供 4 个槽位之间的自动换料、干燥管理、进料和回退功能，以及 RFID 标签支持。

### 项目状态

**状态：** 开发版本

**验证平台：** Voron Trident 3D打印机

**基于：** [DuckACE](https://github.com/utkabobr/DuckACE)

**未来计划：**
- 暂无计划 😊 所有需求都已实现。

## 功能

✅ **取料管理**
- 自动换色（4 个槽位）
- 可调速度的进料和回退
- 自动装载到喷嘴
- 无限线轴模式（infinity spool），可自定义槽位顺序
- **无限线轴自动触发** - 自动监控并在耗材用尽时更换槽位

✅ **干燥管理**
- 可编程耗材干燥
- 温度和时间控制
- 自动风扇管理

✅ **信息功能**
- 设备状态监控
- 耗材信息（RFID）
- 调试命令

✅ **Klipper 集成**
- 完整的 G-code 宏支持
- 异步命令处理

✅ **连接管理**
- 连接管理命令（ACE_CONNECT、ACE_DISCONNECT、ACE_CONNECTION_STATUS）
- 外部耗材传感器支持
- 耗材传感器状态检查命令（ACE_CHECK_FILAMENT_SENSOR）
- 错误恢复重新连接（ACE_RECONNECT）
- 可自定义的暂停宏

✅ **槽位映射**
- 将 Klipper 索引（T0-T3）重新分配到设备物理槽位
- 获取、设置和重置映射的命令
- 批量配置槽位的宏

✅ **强制停泊**
- 使用耗材传感器的替代停泊算法
- 可自定义参数：最大距离、速度、超时
- 适合长进料轨道的打印机

- 与现有配置的兼容性

✅ **通过 Moonraker 的 REST API**
- 通过 HTTP API 获取 ACE 状态
- 通过 REST 端点执行命令
- WebSocket 订阅状态更新

## 系统要求

- **Klipper** - 新安装（推荐）
- **Python 3** - 用于模块运行
- **pyserial** - 用于串口通信的库
- **USB 连接** - 连接到 ACE Pro

### 支持的打印机

- ✅ Creality K1 / K1 Max
- ⚠️ 其他 Klipper 打印机（需要测试）

## 快速开始

### 1. 安装

```bash
# 克隆仓库
git clone https://github.com/agrloki/ValgACE.git
cd ValgACE

# 运行安装脚本
./install.sh
```

### 2. 配置

在 `printer.cfg` 中添加：

```ini
[include ace.cfg]
```

### 3. 检查连接

```gcode
ACE_STATUS
ACE_DEBUG METHOD=get_info
```

## 设备连接

### 连接器针脚图

ACE Pro 设备通过MX3.0 6P端子连接到标准 USB：

![Molex](/.github/img/molex.png)

**连接器针脚分布：**

- **1** - 无（VCC，工作不需要，ACE 提供自己的电源）
- **2** - 接地（地线）
- **3** - D- （USB 数据-）
- **4** - D+（USB 数据+）

**连接方式：** 将 Molex 连接器连接到普通 USB 电缆 - 无需任何额外操作。

详见 [安装指南](docs/INSTALLATION.md#设备连接)。

## 文档

完整文档可在 `docs/` 文件夹中获得：

**英文文档：**
- **[安装](docs/INSTALLATION.md)**            - 详细的安装指南
- **[用户指南](docs/USER_GUIDE.md)**          - 如何使用 ValgACE
- **[命令参考](docs/COMMANDS.md)**            - 所有可用的 G-code 命令
- **[配置](docs/CONFIGURATION.md)**           - 参数配置
- **[故障排除](docs/TROUBLESHOOTING.md)**     - 常见问题和解决方案
- **[协议](docs/PROTOCOL.md)**                - 技术协议文档
- **[Moonraker API](docs/MOONRAKER_API.md)**  - Moonraker API 集成和 REST 端点


## 主要命令

```gcode
# 获取设备状态
ACE_STATUS

# 换工具
ACE_CHANGE_TOOL TOOL=0    # 加载槽位 0
ACE_CHANGE_TOOL TOOL=-1   # 卸载线轴

# 停泊线轴
ACE_PARK_TO_TOOLHEAD INDEX=0

# 进给管理
ACE_FEED INDEX=0 LENGTH=50 SPEED=25
ACE_RETRACT INDEX=0 LENGTH=50 SPEED=25

# 线轴干燥
ACE_START_DRYING TEMP=50 DURATION=120
ACE_STOP_DRYING

# 无限线轴模式
ACE_SET_INFINITY_SPOOL_ORDER ORDER="0,1,2,3"  # 设置槽位顺序
ACE_INFINITY_SPOOL  # 线轴耗尽时自动换槽位

# 槽位映射
ACE_GET_SLOTMAPPING                 # 获取当前映射
ACE_SET_SLOTMAPPING INDEX=0 SLOT=1  # 将 T0 分配到槽位 1
ACE_RESET_SLOTMAPPING               # 重置为默认值
SET_ALL_SLOTMAPPING S0=0 S1=1 S2=2 S3=3  # 批量配置

# 连接管理
ACE_RECONNECT                       # 错误时重新连接

# 帮助
ACE_GET_HELP                        # 显示所有命令列表
```

完整命令列表见 [命令参考](docs/COMMANDS.md)。

## REST API

安装后，可通过 Moonraker 使用 REST API 端点：

```bash
# 获取 ACE 状态
curl http://localhost:7125/server/ace/status

# 获取槽位信息
curl http://localhost:7125/server/ace/slots

# 执行 ACE 命令
curl -X POST http://localhost:7125/server/ace/command \
  -H "Content-Type: application/json" \
  -d '{"command":"ACE_PARK_TO_TOOLHEAD","params":{"INDEX":0}}'
```

详细的 REST API 文档：[Moonraker API](docs/MOONRAKER_API.md)

## 网页界面
![Web](/.github/img/valgace-web.png)

在 `web-interface/` 中提供了用于管理 ACE 的现成网页界面：

- **[ValgACE 仪表板](web-interface/README.md)** - 使用 Vue.js 的功能完整的网页界面
- 实时设备状态显示
- 线轴管理（加载、停泊、进给助手、进料、回退）
- 干燥管理
- WebSocket 连接用于实时更新

### 快速安装仪表板

```bash
# 复制整个 web-interface 目录
mkdir -p ~/ace-dashboard
cp -r ~/ValgACE/web-interface/* ~/ace-dashboard/

# 运行 HTTP 服务器
cd ~/ace-dashboard
python3 -m http.server 8080
```

在浏览器中打开：`http://<打印机IP>:8080/index.html`

**对于持久使用，建议通过 nginx 安装** 

参见 [安装说明](docs/INSTALLATION.md#2-安装-valgace-仪表板) 和 [nginx 配置示例](web-interface/server/ace_dashboard.nginx.conf)

主要文件：
- `index.html` - 主页面入口
- `js/ace-dashboard.js` - 主应用逻辑
- `css/ace-dashboard.css` - 样式表
- `config/ace-dashboard-config.js` - API 配置
- `assets/favicon.svg` - 网站图标
- `ace-dashboard-config.js` - Moonraker 地址配置

## 支持

### 讨论

- **主要讨论：** [Telegram - perdoling3d](https://t.me/perdoling3d/45834)
- **一般讨论：** [Telegram - ERCFcrealityACEpro](https://t.me/ERCFcrealityACEpro/21334)

### 视频

- [工作演示](https://youtu.be/hozubbjeEw8)
### GitHub

- **仓库：** https://github.com/agrloki/ValgACE
- **问题：** 使用 GitHub Issues 报告错误

## 致谢

项目基于：
- [DuckACE](https://github.com/utkabobr/DuckACE) by utkabobr
- [BunnyACE](https://github.com/BlackFrogKok/BunnyACE) by BlackFrogKok
- [ValgACE](https://github.com/agrloki/ValgACE) by agrloki

## 许可证

项目采用 [GNU GPL v3](LICENSE.md) 许可证发布。

