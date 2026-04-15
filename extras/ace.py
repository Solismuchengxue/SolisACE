# File: ace.py — ValgAce模块 for Klipper

import logging
import json
import struct
import queue
from typing import Optional, Dict, Any, Callable

# 检查所需库是否存在,如果不存在则抛出错误
try:
    import serial
    from serial import SerialException
except ImportError:
    serial = None
    SerialException = Exception
    raise ImportError("The 'pyserial' library is required for ValgAce module. Please install it using 'pip install pyserial'")


class ValgAce:
    """
    ValgAce模块 for Klipper
    提供自动换 filament 设备(ACE)的管理功能
    支持最多4个耗材料槽, 具备干燥、送料和回抽耗材的功能
    """
    def __init__(self, config):
        self.printer = config.get_printer()
        self.toolhead = None
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        
        # 首先初始化日志记录器
        self.logger = logging.getLogger('ace')
        self._name = 'ace'
        
        # 初始化耗材传感器
        self.filament_sensor_name = config.get('filament_sensor', None)
        self.filament_sensor = None
        if self.filament_sensor_name:
            try:
                self.filament_sensor = self.printer.lookup_object(f'filament_switch_sensor {self.filament_sensor_name}')
                self.logger.info(f"Filament sensor '{self.filament_sensor_name}' found and connected")
            except Exception as e:
                self.logger.warning(f"Filament sensor '{self.filament_sensor_name}' not found: {str(e)}")
                self.filament_sensor = None
        
        # 可选依赖: save_variables
        try:
            save_vars = self.printer.lookup_object('save_variables')
            self.variables = save_vars.allVariables
        except self.printer.config_error:
            # save_variables 未加载,创建后备字典
            self.variables = {}
            self.logger.warning("save_variables module not found, variables will not persist across restarts")
        self.read_buffer = bytearray()
        self.send_time = 0
        self._last_status_request = 0

        # 参数超时
        self._response_timeout = config.getfloat('response_timeout', 2.0)
        self._read_timeout = config.getfloat('read_timeout', 0.1)
        self._write_timeout = config.getfloat('write_timeout', 0.5)
        self._max_queue_size = config.getint('max_queue_size', 20)
        # 设备仅从配置中选择
        self.serial_name = config.get('serial', '/dev/ttyACM0')

        self.baud = config.getint('baud', 115200)

        # 配置参数
        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_mode = config.getint('retract_mode', 0)
        self.toolchange_retract_length = config.getint('toolchange_retract_length', 100)
        self.park_hit_count = config.getint('park_hit_count', 5)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.disable_assist_after_toolchange = config.getboolean('disable_assist_after_toolchange', True)
        self.infinity_spool_mode = config.getboolean('infinity_spool_mode', False)
        self.ins_spool_work = False  # ACE_INFINITY_SPOOL 操作执行标志
        
        # 新的激进停车参数
        self.aggressive_parking = config.getboolean('aggressive_parking', False)
        self.max_parking_distance = config.getint('max_parking_distance', 100)
        self.parking_speed = config.getint('parking_speed', 10)
        # 停车超时的额外时间(秒)
        self.extended_park_time = config.getint('extended_park_time', 10)
        # 停车的最大等待时间(秒)
        self.max_parking_timeout = config.getint('max_parking_timeout', 60)

        # 打印暂停宏(默认为 PAUSE)
        self.pause_macro_name = config.get('set_pause_macro_name', 'PAUSE')

        # 添加耗材传感器绑定支持

        # 设备状态
        self._info = self._get_default_info()
        self._callback_map = {}
        
        # 索引到料槽的映射(默认:0→0, 1→1, 2→2, 3→3)
        self.index_to_slot = [0, 1, 2, 3]
        self._request_id = 0
        self._connected = False
        self._manually_disconnected = False  # 通过用户指令跟踪是否已断开连接
        self._connection_attempts = 0
        self._max_connection_attempts = 5

        # 操作
        self._feed_assist_index = -1
        self._last_assist_count = 0
        self._assist_hit_count = 0
        self._park_in_progress = False
        self._park_error = False  # 用于跟踪停车错误的标记
        self._park_is_toolchange = False
        self._park_previous_tool = -1
        self._park_index = -1
        self._park_start_time = 0  # 初始化以防止出现AttributeError
        # 激进停车与传感器的标志
        self._sensor_parking_active = False  # True 当使用传感器停车时
        self._sensor_parking_completed = False  # True 当传感器成功触发时

        # 队列
        self._queue = queue.Queue(maxsize=self._max_queue_size)

        # 端口和反应器
        self._serial = None
        self._reader_timer = None
        self._writer_timer = None

        # 注册事件
        self._register_handlers()
        self._register_gcode_commands()

        # 启动时连接
        self.reactor.register_timer(self._connect_check, self.reactor.NOW)
        
        # 初始化标志以避免重复的 dwell 计时器
        self._dwell_scheduled = False
        # 初始化标志以跟踪停车计数器的增加
        self._park_count_increased = False
        # 防止递归调用 _ACE_POST_TOOLCHANGE 的标志
        self._post_toolchange_running = False
        # 活动计时器的引用以正确清理(防止泄漏)
        self._park_monitor_timer = None
        self._sensor_monitor_timer = None
        # 连接丢失的标志和计数器
        self._connection_lost = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10

        # 无限料盘 自动触发状态
        self.infsp_empty_detected = False        # empty 状态检测标志
        self.infsp_debounce_timer = None         # Reactor timer 用于防抖
        self.infsp_sensor_monitor_timer = None   # Reactor timer 用于传感器监控
        self.infsp_last_active_status = None     # 最后已知的活动料槽状态

        # 无限料盘 自动触发配置参数
        self.infinity_spool_debounce = config.getfloat('infinity_spool_debounce', 2.0)
        self.infinity_spool_pause_on_no_sensor = config.getboolean('infinity_spool_pause_on_no_sensor', True)

    def _get_default_info(self) -> Dict[str, Any]:
        return {
            'status': 'disconnected',
            'dryer': {
                'status': 'stop',
                'target_temp': 0,
                'duration': 0,
                'remain_time': 0
            },
            'temp': 0,
            'enable_rfid': 1,
            'fan_speed': 7000,
            'feed_assist_count': 0,
            'cont_assist_time': 0.0,
            'slots': [{
                'index': i,
                'status': 'empty',
                'sku': '',
                'type': '',
                'color': [0, 0, 0]
            } for i in range(4)]
        }

    def _init_slot_mapping(self):
        """
        从变量初始化索引到料槽的映射.
        如果变量不存在,则设置默认值(0→0, 1→1, 2→2, 3→3).
        """
        for i in range(4):
            var_name = f'ace_index{i}_to_slot'
            slot_value = self.variables.get(var_name, None)
            
            if slot_value is None:
                # 变量不存在,使用默认值创建
                self.index_to_slot[i] = i
                self._save_variable(var_name, i)
                self.logger.info(f"Slot mapping: initialized {var_name} = {i}")
            else:
                # 变量存在,验证并使用其值
                try:
                    slot_int = int(slot_value)
                    if 0 <= slot_int <= 3:
                        self.index_to_slot[i] = slot_int
                        self.logger.info(f"Slot mapping: loaded {var_name} = {slot_int}")
                    else:
                        # 值超出范围,重置为默认值
                        self.logger.warning(f"Slot mapping: {var_name} = {slot_value} out of range (0-3), resetting to {i}")
                        self.index_to_slot[i] = i
                        self._save_variable(var_name, i)
                except (ValueError, TypeError):
                    # 转换错误,重置为默认值
                    self.logger.warning(f"Slot mapping: {var_name} = {slot_value} invalid, resetting to {i}")
                    self.index_to_slot[i] = i
                    self._save_variable(var_name, i)
        
        self.logger.info(f"Slot mapping initialized: {self.index_to_slot}")

    def _get_real_slot(self, index: int) -> int:
        """
        将索引(来自Klipper)转换为设备的真实料槽.
        
        :param index: 来自Klipper的索引(0-3)
        :return: 设备的真实料槽(0-3)
        """
        if 0 <= index <= 3:
            return self.index_to_slot[index]
        return index

    def _set_slot_mapping(self, index: int, slot: int) -> bool:
        """
        设置索引到料槽的映射.
        
        :param index: 索引(0-3)
        :param slot: 料槽(0-3)
        :return: 成功返回True, 失败返回False
        """
        if not (0 <= index <= 3):
            return False
        if not (0 <= slot <= 3):
            return False
        
        self.index_to_slot[index] = slot
        var_name = f'ace_index{index}_to_slot'
        self._save_variable(var_name, slot)
        self.logger.info(f"Slot mapping updated: index {index} → slot {slot}")
        return True

    def _reset_slot_mapping(self):
        """
        将料槽映射重置为默认值(0→0, 1→1, 2→2, 3→3).
        """
        for i in range(4):
            self.index_to_slot[i] = i
            var_name = f'ace_index{i}_to_slot'
            self._save_variable(var_name, i)
        self.logger.info("Slot mapping reset to defaults: [0, 1, 2, 3]")

    def _validate_index(self, index: int) -> tuple:
        """
        验证INDEX并转换为真实料槽.
        
        :param index: 来自Klipper的索引(0-3)
        :return: 元组(real_slot, error_message)
                 - real_slot: 如果有效则为设备的真实料槽(0-3),否则为 -1
                 - error_message: 如果INDEX无效则为错误消息, 否则为None
        """
        # 检查INDEX范围
        if not isinstance(index, int):
            return -1, f"INDEX must be integer, got {type(index).__name__}"
        
        if index < 0 or index > 3:
            return -1, f"INDEX {index} out of range (must be 0-3)"
        
        # 通过映射转换
        real_slot = self.index_to_slot[index]
        
        self.logger.debug(f"INDEX validation: {index} → Slot {real_slot}")
        return real_slot, None

    def _validate_slot_status(self, real_slot: int, required_status: str = 'ready') -> tuple:
        """
        检查料槽状态.
        Check slot status.
        
        :param real_slot: 设备的真实料槽(0-3)
        :param required_status: 所需状态('ready', 'empty', etc.)
        :return: 元组(is_valid, error_message)
                 - is_valid: 如果料槽具有所需状态则为True
                 - error_message: 如果状态不匹配则为错误消息
        """
        # 检查连接状态
        if not self._connected:
            return False, "ACE device not connected"
        
        # 检查料槽范围
        if real_slot < 0 or real_slot > 3:
            return False, f"Invalid slot {real_slot} (must be 0-3)"
        
        # 获取当前料槽状态
        try:
            slots = self._info.get('slots', [])
            if real_slot >= len(slots):
                return False, f"Slot {real_slot} not found in device status"
            
            slot_info = slots[real_slot]
            current_status = slot_info.get('status', 'unknown')
            
            if current_status != required_status:
                return False, f"Slot {real_slot} status is '{current_status}', expected '{required_status}'"
            
            return True, None
            
        except Exception as e:
            self.logger.error(f"Error checking slot {real_slot} status: {str(e)}")
            return False, f"Error checking slot status: {str(e)}"

    def _validate_index_for_operation(self, index: int, operation_name: str = "operation") -> tuple:
        """
        对操作进行INDEX的综合验证(检查INDEX + 料槽状态).
        Comprehensive INDEX validation for operation(INDEX check + slot status check).
        
        :param index: 来自Klipper的索引(0-3)
        :param operation_name: 用于错误消息的操作名称
        :return: 元组(real_slot, error_message)
                 - real_slot: 如果有效则为设备的真实料槽, 否则为None
                 - error_message: 如果验证失败则为错误消息, 否则为None
        """
        # 验证INDEX
        real_slot, error = self._validate_index(index)
        if error:
            return None, error
        
        # 检查设备连接状态
        if not self._connected:
            return None, "ACE device not connected"
        
        return real_slot, None

    def _is_slot_ready(self, index: int) -> bool:
        """
        按索引检查料槽是否就绪.
        
        :param index: 料槽索引(0-3)
        :return: 如果料槽就绪则返回True, 否则返回False
        """
        try:
            slots = self._info.get('slots', [])
            if index < 0 or index >= len(slots):
                return False
            slot_info = slots[index]
            return slot_info.get('status', 'unknown') == 'ready'
        except Exception as e:
            self.logger.error(f"Error checking slot {index} readiness: {str(e)}")
            return False

    def _register_handlers(self):
        """
        注册打印机事件处理程序
        """
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)

    def _register_gcode_commands(self):
        commands = [
            ('ACE_DEBUG', self.cmd_ACE_DEBUG, "ACE调试连接"),
            ('ACE_STATUS', self.cmd_ACE_STATUS, "ACE设备状态"),
            ('ACE_START_DRYING', self.cmd_ACE_START_DRYING, "ACE开始干燥"),
            ('ACE_STOP_DRYING', self.cmd_ACE_STOP_DRYING, "ACE停止干燥"),
            ('ACE_ENABLE_FEED_ASSIST', self.cmd_ACE_ENABLE_FEED_ASSIST, "ACE启用送料辅助"),
            ('ACE_DISABLE_FEED_ASSIST', self.cmd_ACE_DISABLE_FEED_ASSIST, "ACE禁用送料辅助"),
            ('ACE_PARK_TO_TOOLHEAD', self.cmd_ACE_PARK_TO_TOOLHEAD, "ACE将耗材送至打印头"),
            ('ACE_FEED', self.cmd_ACE_FEED, "ACE送料"),
            ('ACE_UPDATE_FEEDING_SPEED', self.cmd_ACE_UPDATE_FEEDING_SPEED, "ACE更新送料速度"),
            ('ACE_STOP_FEED', self.cmd_ACE_STOP_FEED, "ACE停止送料"),
            ('ACE_RETRACT', self.cmd_ACE_RETRACT, "ACE回抽"),
            ('ACE_UPDATE_RETRACT_SPEED', self.cmd_ACE_UPDATE_RETRACT_SPEED, "ACE更新回抽速度"),
            ('ACE_STOP_RETRACT', self.cmd_ACE_STOP_RETRACT, "ACE停止回抽"),
            ('ACE_CHANGE_TOOL', self.cmd_ACE_CHANGE_TOOL, "ACE换色"),
            ('ACE_INFINITY_SPOOL', self.cmd_ACE_INFINITY_SPOOL, "ACE无限料盘模式"),
            ('ACE_SET_INFINITY_SPOOL_ORDER', self.cmd_ACE_SET_INFINITY_SPOOL_ORDER, "ACE设置无限料盘顺序"),
            ('ACE_FILAMENT_INFO', self.cmd_ACE_FILAMENT_INFO, "ACE显示耗材信息"),
            ('ACE_CHECK_FILAMENT_SENSOR', self.cmd_ACE_CHECK_FILAMENT_SENSOR, "ACE检查耗材传感器状态"),
            ('ACE_DISCONNECT', self.cmd_ACE_DISCONNECT, "ACE断开"),
            ('ACE_CONNECT', self.cmd_ACE_CONNECT, "ACE连接"),
            ('ACE_CONNECTION_STATUS', self.cmd_ACE_CONNECTION_STATUS, "ACE检查连接状态"),
            ('ACE_RECONNECT', self.cmd_ACE_RECONNECT, "ACE重连"),
            ('ACE_GET_HELP', self.cmd_ACE_GET_HELP, "ACE命令及帮助"),
            ('ACE_GET_SLOTMAPPING', self.cmd_ACE_GET_SLOTMAPPING, "ACE获取插槽映射"),
            ('ACE_SET_SLOTMAPPING', self.cmd_ACE_SET_SLOTMAPPING, "ACE设置插槽映射"),
            ('ACE_RESET_SLOTMAPPING', self.cmd_ACE_RESET_SLOTMAPPING, "ACE重置插槽映射"),
            ('ACE_GET_CURRENT_INDEX', self.cmd_ACE_GET_CURRENT_INDEX, "ACE获取当前索引"),
            ('ACE_SET_CURRENT_INDEX', self.cmd_ACE_SET_CURRENT_INDEX, "ACE设置工具索引(用于错误恢复)"),
        ]
        for name, func, desc in commands:
            self.gcode.register_command(name, func, desc=desc)

    def _connect_check(self, eventtime):
        # 仅当设备未连接且未被手动断开时自动连接
        if not self._connected and not self._manually_disconnected:
            # 尝试连接
            self._connect()
        return eventtime + 1.0

    def _connect(self) -> bool:
        if self._connected:
            return True
            
        # 确保所有现有连接均已正确关闭
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
            
        for attempt in range(self._max_connection_attempts):
            try:
                self.logger.info(f"Attempting to connect to ACE at {self.serial_name} (attempt {attempt + 1}/{self._max_connection_attempts})")
                
                self._serial = serial.Serial(
                    port=self.serial_name,
                    baudrate=self.baud,
                    timeout=0,
                    write_timeout=self._write_timeout
                )
                
                if self._serial.is_open:
                    self._connected = True
                    self._info['status'] = 'ready'
                    # 成功连接时重置尝试计数器
                    self._reconnect_attempts = 0
                    self._connection_lost = False
                    self.logger.info(f"Connected to ACE at {self.serial_name}")

                    def info_callback(response):
                        res = response['result']
                        self.logger.info(f"Device info: {res.get('model', 'Unknown')} {res.get('firmware', 'Unknown')}")
                        self.gcode.respond_info(f"Connected {res.get('model', 'Unknown')} {res.get('firmware', 'Unknown')}")

                    self.send_request({"method": "get_info"}, info_callback)

                    # 如果尚未注册,则注册计时器
                    if self._reader_timer is None:
                        self._reader_timer = self.reactor.register_timer(self._reader_loop, self.reactor.NOW)
                    if self._writer_timer is None:
                        self._writer_timer = self.reactor.register_timer(self._writer_loop, self.reactor.NOW)

                    self.logger.info("Connection established successfully")
                    return True
                else:
                    # 如果串口未正确打开,则关闭它
                    if self._serial:
                        self._serial.close()
                        self._serial = None
            except SerialException as e:
                self.logger.info(f"Connection attempt {attempt + 1} failed: {str(e)}")
                if self._serial:
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                    self._serial = None
                self.dwell(1.0, lambda: None)
            except Exception as e:
                self.logger.error(f"Unexpected error during connection: {str(e)}")
                if self._serial:
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                    self._serial = None
                self.dwell(1.0, lambda: None)
                
        self.logger.info("无法连接到ACE设备")
        return False

    def _disconnect(self):
        """优雅地断开与设备的连接,并停止所有计时器"""
        if not self._connected:
            return
            
        self.logger.info("正在断开与ACE设备的连接...")
        
        # 停止所有计时器
        if self._reader_timer:
            self.reactor.unregister_timer(self._reader_timer)
            self._reader_timer = None
        if self._writer_timer:
            self.reactor.unregister_timer(self._writer_timer)
            self._writer_timer = None
            
        # 关闭串行连接
        try:
            if self._serial and self._serial.is_open:
                self._serial.close()
        except Exception as e:
            self.logger.error(f"Error closing serial connection: {str(e)}")
        finally:
            self._serial = None
        
        # 更新连接状态
        self._connected = False
        self._info['status'] = 'disconnected'
        
        # 清除所有挂起的请求
        try:
            while not self._queue.empty():
                _, callback = self._queue.get_nowait()
                if callback:
                    try:
                        callback({'error': 'Device disconnected'})
                    except Exception as e:
                        self.logger.debug(f"Error in callback during disconnect: {str(e)}")
        except Exception as e:
            self.logger.debug(f"Error clearing request queue: {str(e)}")
        
        # Clear callback map
        self._callback_map.clear()
        
        self.logger.info("ACE device disconnected successfully")

    def _save_variable(self, name: str, value):
        """如果save_variables模块可用, 则安全地保存变量"""
        self.variables[name] = value
        try:
            self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE={name} VALUE={value}')
        except Exception as e:
            # save_variables 不可用或保存时出错
            self.logger.debug(f"Could not save variable {name}: {e}")

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        if self.toolhead is None:
            raise self.printer.config_error("Toolhead not found in ValgAce module")
        
        # 初始化料槽映射
        self._init_slot_mapping()

    def _handle_disconnect(self):
        # 当klipper断开连接时,重置手动断开连接标记,以便重启后自动重连功能能够正常工作
        self._manually_disconnected = False

        # 检查打印状态并在需要时调用暂停
        printer_state = self._get_printer_state()
        if printer_state == 'printing':
            self.logger.info(f"Klipper disconnect detected during printing, triggering {self.pause_macro_name}")
            try:
                self.gcode.run_script_from_command(self.pause_macro_name)
            except Exception as e:
                self.logger.error(f"Error triggering {self.pause_macro_name} during klipper disconnect: {str(e)}")

        self._disconnect()

    def get_status(self, eventtime):
        """通过 query_objects 返回 Moonraker API 的状态"""
        # Klipper 在通过 query_objects 请求时自动调用此方法
        # Moonraker 自动将结果包装在模块名称的键中('ace')
        
        # 获取烘干机数据
        dryer_data = self._info.get('dryer', {}) or self._info.get('dryer_status', {})
        
        # 标准化时间:如果需要,将秒转换为分钟
        if isinstance(dryer_data, dict):
            dryer_normalized = dryer_data.copy()
            
            # remain_time 始终以秒为单位传入 - 转换为分钟
            remain_time_raw = dryer_normalized.get('remain_time', 0)
            if remain_time_raw > 0:
                dryer_normalized['remain_time'] = remain_time_raw / 60  # 保留小数部分用于秒
            
            # duration 始终以分钟为单位传入 - 保持不变
            # (不做任何操作,已经是正确的格式)
        else:
            dryer_normalized = {}
        
        # 如果已配置,获取耗材传感器状态
        filament_sensor_status = None
        if self.filament_sensor:
            try:
                filament_sensor_status = self.filament_sensor.get_status(eventtime)
            except Exception as e:
                self.logger.warning(f"Error getting filament sensor status: {str(e)}")
                filament_sensor_status = {"filament_detected": False, "enabled": False}
        
        return {
            'status': self._info.get('status', 'unknown'),
            'connection_state': 'connected' if self._connected else 'disconnected',
            'current_index': self.variables.get('ace_current_index', -1),  # 添加这行
            'model': self._info.get('model', ''),
            'firmware': self._info.get('firmware', ''),
            'boot_firmware': self._info.get('boot_firmware', ''),
            'temp': self._info.get('temp', 0),
            'fan_speed': self._info.get('fan_speed', 0),
            'enable_rfid': self._info.get('enable_rfid', 0),
            'feed_assist_count': self._info.get('feed_assist_count', 0),
            'cont_assist_time': self._info.get('cont_assist_time', 0.0),
            'feed_assist_slot': self._feed_assist_index,  # 已激活送料辅助的料槽索引(-1 = 已禁用)
            'dryer': dryer_normalized,
            'dryer_status': dryer_normalized,
            'slots': self._info.get('slots', []),
            'filament_sensor': filament_sensor_status,
            'slot_mapping': self.index_to_slot.copy(),  # 索引到料槽的映射
            'usb_port': self.serial_name.split('/')[-1] if self.serial_name else '',
            'usb_path': self.serial_name or '',
        }

    def _calc_crc(self, buffer: bytes) -> int:
        """
        计算数据缓冲区的 CRC
        :param buffer: 用于计算 CRC 的字节缓冲区
        :return: CRC 值
        """
        crc = 0xffff
        for byte in buffer:
            data = byte ^ (crc & 0xff)
            data ^= (data & 0x0f) << 4
            crc = (((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)) & 0xffff
        return crc & 0xffff

    def send_request(self, request: Dict[str, Any], callback: Callable):
        if self._queue.qsize() >= self._max_queue_size:
            self.logger.info("Request queue overflow, clearing...")
            while not self._queue.empty():
                _, cb = self._queue.get_nowait()
                if cb:
                    try:
                        cb({'error': 'Queue overflow'})
                    except Exception:
                        pass
        request['id'] = self._get_next_request_id()
        self._queue.put((request, callback))

    def _get_next_request_id(self) -> int:
        self._request_id += 1
        if self._request_id >= 300000:
            self._request_id = 0
        return self._request_id

    def _send_request(self, request: Dict[str, Any]) -> bool:
        try:
            payload = json.dumps(request).encode('utf-8')
        except Exception as e:
            self.logger.info(f"JSON encoding error: {str(e)}")
            return False

        crc = self._calc_crc(payload)
        packet = (
            bytes([0xFF, 0xAA]) + 
            struct.pack('<H', len(payload)) + 
            payload + 
            struct.pack('<H', crc) + 
            bytes([0xFE])
        )

        try:
            if self._serial and self._serial.is_open:
                self._serial.write(packet)
                return True
            else:
                raise SerialException("Serial port closed")
        except SerialException as e:
            self.logger.info(f"Send error: {str(e)}")
            self._reconnect()
            return False

    def _reader_loop(self, eventtime):
        if not self._connected or not self._serial or not self._serial.is_open:
            return eventtime + 0.01
        try:
            raw_bytes = self._serial.read(16)
            if raw_bytes:
                self.read_buffer.extend(raw_bytes)
                self._process_messages()
        except SerialException as e:
            self.logger.info(f"Read error: {str(e)}")
            self._reconnect()
        return eventtime + 0.01

    def _process_messages(self):
        incomplete_message_count = 0
        max_incomplete_messages_before_reset = 10
        while self.read_buffer:
            end_idx = self.read_buffer.find(b'\xfe')
            if end_idx == -1:
                break
            msg = self.read_buffer[:end_idx + 1]
            self.read_buffer = self.read_buffer[end_idx + 1:]
            if len(msg) < 7 or msg[0:2] != bytes([0xFF, 0xAA]):
                continue
            payload_len = struct.unpack('<H', msg[2:4])[0]
            expected_length = 4 + payload_len + 3
            if len(msg) < expected_length:
                self.logger.info(f"Incomplete message received (expected {expected_length}, got {len(msg)})")
                incomplete_message_count += 1
                if incomplete_message_count > max_incomplete_messages_before_reset:
                    self.logger.info("Too many incomplete messages, resetting connection")
                    self._reset_connection()
                    incomplete_message_count = 0
                continue
            incomplete_message_count = 0
            payload = msg[4:4 + payload_len]
            crc = struct.unpack('<H', msg[4 + payload_len:4 + payload_len + 2])[0]
            if crc != self._calc_crc(payload):
                return
            try:
                response = json.loads(payload.decode('utf-8'))
                self._handle_response(response)
            except json.JSONDecodeError as je:
                self.logger.info(f"JSON decode error: {str(je)} Data: {msg}")
            except Exception as e:
                self.logger.info(f"Message processing error: {str(e)} Data: {msg}")

    def _writer_loop(self, eventtime):
        if not self._connected:
            return eventtime + 0.05
        now = eventtime
        if now - self._last_status_request > (0.2 if self._park_in_progress else 1.0):
            self._request_status()
            self._last_status_request = now
        if not self._queue.empty():
            task = self._queue.get_nowait()
            if task:
                request, callback = task
                self._callback_map[request['id']] = callback
                if not self._send_request(request):
                    self.logger.info("Failed to send request, requeuing...")
                    self._queue.put(task)
        return eventtime + 0.05

    def _request_status(self):
        def status_callback(response):
            if 'result' in response:
                self._info.update(response['result'])
        if self.reactor.monotonic() - self._last_status_request > (0.2 if self._park_in_progress else 1.0):
            try:
                self.send_request({
                    "id": self._get_next_request_id(),
                    "method": "get_status"
                }, status_callback)
                self._last_status_request = self.reactor.monotonic()
            except Exception as e:
                self.logger.info(f"Status request error: {str(e)}")

    def _handle_response(self, response: dict):
        if 'id' in response:
            callback = self._callback_map.pop(response['id'], None)
            if callback:
                try:
                    callback(response)
                except Exception as e:
                    self.logger.info(f"Callback error: {str(e)}")
        if 'result' in response and isinstance(response['result'], dict):
            result = response['result']
            
            # 调试:为所有带有状态的响应输出原始 JSON
            # 检查烘干机数据的存在作为 get_status 响应的标志
            if 'dryer' in result or 'dryer_status' in result or 'slots' in result:
                self.logger.info(f"RAW JSON response from device (get_status): {json.dumps(response, indent=2)}")
                if 'dryer' in result or 'dryer_status' in result:
                    dryer_data = result.get('dryer') or result.get('dryer_status', {})
                    self.logger.info(f"RAW dryer data: {json.dumps(dryer_data, indent=2)}")
            
            # 标准化烘干机数据:如果传入 dryer_status,则也保存为 dryer
            if 'dryer_status' in result and isinstance(result['dryer_status'], dict):
                result['dryer'] = result['dryer_status']
            self._info.update(result)
            
            # Infinity Spool Auto-trigger: 在打印时检查 empty 状态
            # 重要:如果已经在进行料槽切换(ins_spool_work=True),则不要启动监控
            if self.infinity_spool_mode and self._is_printer_printing() and not self.ins_spool_work:
                if self._check_slot_empty_status():
                    self.logger.info(f"_handle_response: Starting empty slot monitoring, ins_spool_work={self.ins_spool_work}")
                    self._start_empty_slot_monitoring()
            
            if self._park_in_progress:
                current_status = result.get('status', 'unknown')
                current_assist_count = result.get('feed_assist_count', 0)
                elapsed_time = self.reactor.monotonic() - self._park_start_time

                # 确定停车模式
                parking_mode = "sensor" if self._sensor_parking_active else ("traditional" if self._sensor_parking_completed else "normal")
                self.logger.debug(f"Parking check ({parking_mode}): slot {self._park_index}, count={current_assist_count}, \
                                  last={self._last_assist_count}, hits={self._assist_hit_count}, elapsed={elapsed_time:.1f}s")
                            
                # 在基于传感器的停车期间跳过计数监控 - 它有自己的计时器
                # 基于传感器的停车由 _monitor_filament_sensor_for_parking() 管理
                if self._sensor_parking_active:
                    self.logger.debug(f"Skipping count check during sensor-based parking for slot {self._park_index}")
                    return
                
                if current_status == 'ready':
                    if current_assist_count != self._last_assist_count:
                        self._last_assist_count = current_assist_count
                        self._assist_hit_count = 0
                        # 标记计数至少增加了一次
                        if current_assist_count > 0:
                            self._park_count_increased = True
                            self.logger.info(f"Feed assist working for slot {self._park_index}, count: {current_assist_count}")
                    else:
                        self._assist_hit_count += 1

                        # 检查送料辅助是否确实工作
                        # 但是:在基于传感器的停车期间跳过此检查 - 计数不会改变直到传感器触发
                        if not self._sensor_parking_active and elapsed_time > 3.0 and not self._park_count_increased:
                            # 3秒过去了但计数从未增加 - feed assist 不工作
                            self.logger.error(f"Feed assist for slot {self._park_index} not working - count stayed at {current_assist_count}")
                            self._park_error = True  # 在重置标志之前标记为错误
                            self._park_in_progress = False
                            self._park_index = -1
                            # 重置传感器停车标志
                            self._sensor_parking_active = False
                            self._sensor_parking_completed = False
                            return
                        
                        if self._assist_hit_count >= self.park_hit_count:
                            # 只有在计数确实增加时才完成
                            if self._park_count_increased:
                                self._complete_parking()
                            else:
                                self.logger.warning(f"Parking check completed but count never increased (stayed at {current_assist_count})")
                                # 标记为错误并中止
                                self._park_error = True
                                self._park_in_progress = False
                                # 重置传感器停车标志
                                self._sensor_parking_active = False
                                self._sensor_parking_completed = False
                            return
                        # 检查是否不会无限创建计时器
                        # 如果 self.dwell 已经计划,不再次调用它
                        if not self._dwell_scheduled:
                            self._dwell_scheduled = True
                            self.dwell(0.7, lambda: setattr(self, '_dwell_scheduled', False))

    def _complete_parking(self):
        if not self._park_in_progress:
            return
        self.logger.info(f"Parking completed for slot {self._park_index}")
        
        # 停止指定料槽的送料辅助
        def stop_feed_assist_callback(response):
            if response.get('code', 0) != 0:
                self.logger.warning(f"Warning stopping feed assist after parking: {response.get('msg', 'Unknown error')}")
            else:
                self.logger.info(f"Feed assist stopped successfully after parking for slot {self._park_index}")
        
        self.send_request({
            "method": "stop_feed_assist",
            "params": {"index": self._park_index}
        }, stop_feed_assist_callback)
        
        # 如果这是工具切换,则执行后处理宏
        if self._park_is_toolchange:
            self.logger.info(f"Executing post-toolchange macro: FROM={self._park_previous_tool} TO={self._park_index}")
            # 根据模式调用相应的 POST 宏
            if self.ins_spool_work:
                self.gcode.run_script_from_command(
                    f'_ACE_POST_INFINITYSPOOL FROM={self._park_previous_tool} TO={self._park_index}'
                )
            else:
                self.gcode.run_script_from_command(
                    f'_ACE_POST_TOOLCHANGE FROM={self._park_previous_tool} TO={self._park_index}'
                )
        
        self._park_in_progress = False
        self._park_error = False  # 重置错误标志
        self._park_is_toolchange = False
        self._park_previous_tool = -1
        self._park_index = -1
        # 重置传感器停车标志
        self._sensor_parking_active = False
        self._sensor_parking_completed = False
        # 清理计时器引用以防止泄漏
        self._park_monitor_timer = None
        self._sensor_monitor_timer = None
        if self.disable_assist_after_toolchange:
            self._feed_assist_index = -1

    def dwell(self, delay: float = 1.0, callback: Optional[Callable] = None):
        """暂停执行,使用反应器"""
        if delay <= 0:
            if callback:
                try:
                    callback()
                except Exception as e:
                    self.logger.error(f"Error in dwell callback: {e}")
            return
        
        def timer_handler(event_time):
            if callback:
                try:
                    callback()
                except Exception as e:
                    self.logger.error(f"Error in dwell callback: {e}")
            return self.reactor.NEVER
        
        self.reactor.register_timer(timer_handler, self.reactor.monotonic() + delay)

    def _get_printer_state(self):
        """获取当前打印状态"""
        eventtime = self.reactor.monotonic()
        try:
            print_stats = self.printer.lookup_object('print_stats')
            ps_status = print_stats.get_status(eventtime)
            return ps_status.get('state', 'unknown')
        except Exception:
            return 'unknown'
        
    def _pause_print_if_needed(self):
        """如果打印机正在打印则调用暂停"""
        printer_state = self._get_printer_state()
        if printer_state == 'printing':
            self.logger.info(f"Print in progress, triggering {self.pause_macro_name}")
            try:
                self.gcode.run_script_from_command(self.pause_macro_name)
            except Exception as e:
                self.logger.error(f"Error triggering {self.pause_macro_name}: {str(e)}")

    def _notify_connection_lost(self):
        """通知用户连接丢失并在打印时调用暂停"""
        self.gcode.respond_raw("ACE: CRITICAL - Connection lost after maximum attempts")
        self._pause_print_if_needed()

    def _reconnect(self):
        """尝试重新连接到设备"""
        if self._connection_lost:
            return  # 已超过限制,尝试连接
            
        self._reconnect_attempts += 1
        if self._reconnect_attempts > self._max_reconnect_attempts:
            # 尝试次数超出限制
            self._connection_lost = True
            self._notify_connection_lost()
            return
        
        # 通知重新连接尝试
        self.logger.info(f"Attempting to reconnect to ACE (attempt {self._reconnect_attempts}/{self._max_reconnect_attempts})")
        
        # 在自动重连期间,重置手动断开连接标志
        self._manually_disconnected = False
        self._disconnect()
        self.dwell(1.0, lambda: None)
        self._connect()

    def _reset_connection(self):
        # 在断开连接时也检查尝试次数限制
        if self._connection_lost:
            return  # 已超过限额
        
        self._reconnect_attempts += 1
        
        if self._reconnect_attempts > self._max_reconnect_attempts:
            # 尝试次数超出限制
            self._connection_lost = True
            self._notify_connection_lost()
            return
        
        self.logger.info(f"Resetting ACE connection (attempt {self._reconnect_attempts}/{self._max_reconnect_attempts})")
        
        # 在连接重置过程中,重置手动断开连接标志
        self._manually_disconnected = False
        self._disconnect()
        self.dwell(1.0, lambda: None)
        self._connect()

    def cmd_ACE_STATUS(self, gcmd):
        try:
            # 输出前请求最新状态
            def status_callback(response):
                # 调试:输出原始JSON响应
                self.logger.info(f"RAW JSON response in ACE_STATUS callback: {json.dumps(response, indent=2)}")
                
                if 'result' in response:
                    result = response['result']
                    # 调试:输出数据关于烘干机
                    if 'dryer' in result or 'dryer_status' in result:
                        dryer_data = result.get('dryer') or result.get('dryer_status', {})
                        self.logger.info(f"RAW dryer data in callback: {json.dumps(dryer_data, indent=2)}")
                    
                    # 标准化烘干机数据
                    if 'dryer_status' in result and isinstance(result['dryer_status'], dict):
                        result['dryer'] = result['dryer_status']
                    self._info.update(result)
                    # 输出状态 after 更新
                    self._output_status(gcmd)
            
            # 发送状态请求
            self.send_request({"method": "get_status"}, status_callback)
            
        except Exception as e:
            self.logger.info(f"Status command error: {str(e)}")
            gcmd.respond_raw(f"Error retrieving status: {str(e)}")
    
    def _output_status(self, gcmd):
        """输出ACE状态(在获取数据后调用)"""
        try:
            info = self._info
            output = []
            
            # 设备信息
            output.append("=== ACE 设备状态 ===")
            output.append(f"设备状态: {info.get('status', 'unknown')}")
            
            # 设备信息
            if 'model' in info:
                output.append(f"设备类型: {info.get('model', 'Unknown')}")
            if 'firmware' in info:
                output.append(f"固件版本: {info.get('firmware', 'Unknown')}")
            if 'boot_firmware' in info:
                output.append(f"Boot版本: {info.get('boot_firmware', 'Unknown')}")
            
            output.append("")
            
            # 烘干机状态
            output.append("=== 烘干机 ===")
            # 检查两个密钥的兼容性
            dryer = info.get('dryer', {})
            if not dryer and 'dryer_status' in info:
                dryer = info.get('dryer_status', {})
            
            dryer_status = dryer.get('status', 'unknown') if isinstance(dryer, dict) else 'unknown'
            output.append(f"状态: {dryer_status}")
            if dryer_status == 'drying':
                output.append(f"目标温度: {dryer.get('target_temp', 0)}°C")
                output.append(f"当前温度: {info.get('temp', 0)}°C")
                # duration 总是以分钟为单位
                duration = dryer.get('duration', 0)
                output.append(f"持续时间: {duration} 分钟")
                
                # remain_time 总是以秒为单位 - 转换为分钟
                remain_time_raw = dryer.get('remain_time', 0)
                # 将秒转换为分钟(保留秒的小数部分)
                remain_time = remain_time_raw / 60 if remain_time_raw > 0 else 0
                
                if remain_time > 0:
                    # 格式化为“119分54秒”
                    total_minutes = int(remain_time)
                    fractional_part = remain_time - total_minutes
                    seconds = int(round(fractional_part * 60))
                    if seconds >= 60:
                        total_minutes += 1
                        seconds = 0
                    if total_minutes > 0:
                        if seconds > 0:
                            output.append(f"剩余时间: {total_minutes}m {seconds}s")
                        else:
                            output.append(f"剩余时间: {total_minutes}m")
                    else:
                        output.append(f"剩余时间: {seconds}s")
            else:
                output.append(f"温度: {info.get('temp', 0)}°C")
            
            output.append("")
            
            # 设备参数
            output.append("=== 设备参数 ===")
            output.append(f"风扇速度: {info.get('fan_speed', 0)} RPM")
            output.append(f"启用RFID: {'Yes' if info.get('enable_rfid', 0) else 'No'}")
            output.append(f"送料辅助计数: {info.get('feed_assist_count', 0)}")
            cont_assist = info.get('cont_assist_time', 0.0)
            if cont_assist > 0:
                output.append(f"持续辅助时间: {cont_assist:.1f} ms")
            
            output.append("")
            
            # 料槽信息
            output.append("=== 耗材料槽 ===")
            slots = info.get('slots', [])
            for slot in slots:
                index = slot.get('index', -1)
                status = slot.get('status', 'unknown')
                slot_type = slot.get('type', '')
                color = slot.get('color', [0, 0, 0])
                sku = slot.get('sku', '')
                rfid_status = slot.get('rfid', 0)
                
                output.append(f"料槽 {index}:")
                output.append(f"  状态: {status}")
                if slot_type:
                    output.append(f"  类型: {slot_type}")
                if sku:
                    output.append(f"  SKU: {sku}")
                if color and isinstance(color, list) and len(color) >= 3:
                    output.append(f"  颜色: RGB({color[0]}, {color[1]}, {color[2]})")
                rfid_text = {0: "未找到", 1: "失败", 2: "已识别", 3: "正在识别"}.get(rfid_status, "未知")
                output.append(f"  RFID: {rfid_text}")
                output.append("")
            
            # 耗材传感器状态
            if self.filament_sensor:
                try:
                    eventtime = self.reactor.monotonic()
                    sensor_status = self.filament_sensor.get_status(eventtime)
                    
                    filament_detected = sensor_status.get('filament_detected', False)
                    sensor_enabled = sensor_status.get('enabled', False)
                    
                    output.append("=== 耗材传感器 ===")
                    if filament_detected:
                        output.append("状态: 已检测到耗材")
                    else:
                        output.append("状态: 未检测到耗材")
                    output.append(f"启用: {'Yes' if sensor_enabled else 'No'}")
                    output.append("")
                except Exception as e:
                    output.append("=== 耗材传感器 ===")
                    output.append(f"读取传感器错误: {str(e)}")
                    output.append("")
            
            gcmd.respond_info("\n".join(output))
        except Exception as e:
            self.logger.info(f"Status output error: {str(e)}")
            gcmd.respond_raw(f"Error outputting status: {str(e)}")

    def cmd_ACE_DEBUG(self, gcmd):
        method = gcmd.get('METHOD')
        params = gcmd.get('PARAMS', '{}')
        try:
            request = {"method": method}
            if params.strip():
                request["params"] = json.loads(params)
            
            def callback(response):
                # 为get_status方法进行特殊处理
                if method == 'get_status' and 'result' in response:
                    # 将耗材传感器的信息添加到结果中
                    eventtime = self.reactor.monotonic()
                    response_with_filament = response.copy()
                    response_with_filament['result'] = response['result'].copy()
                    
                    # 添加关于耗材传感器的信息
                    filament_sensor_status = None
                    if self.filament_sensor:
                        try:
                            filament_sensor_status = self.filament_sensor.get_status(eventtime)
                        except Exception as e:
                            self.logger.warning(f"Error getting filament sensor status: {str(e)}")
                            filament_sensor_status = {"filament_detected": False, "enabled": False}
                    
                    response_with_filament['result']['filament_sensor'] = filament_sensor_status
                    
                    # 显示增强结果
                    gcmd.respond_info(json.dumps(response_with_filament, indent=2))
                else:
                    # 对于其他方法,返回常规响应
                    gcmd.respond_info(json.dumps(response, indent=2))
            
            self.send_request(request, callback)
        except Exception as e:
            self.logger.info(f"Debug command error: {str(e)}")
            gcmd.respond_raw(f"Error: {str(e)}")
            return

    def cmd_ACE_FILAMENT_INFO(self, gcmd):
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        
        # 验证INDEX并转换真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_FILAMENT_INFO")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        try:
            def callback(response):
                if 'result' in response:
                    slot_info = response['result']
                    self.gcode.respond_info(str(slot_info))
                else:
                    self.gcode.respond_info('Error: No result in response')
            self.send_request({"method": "get_filament_info", "params": {"index": real_slot}}, callback)
        except Exception as e:
            self.logger.info(f"Filament info error: {str(e)}")
            self.gcode.respond_info('Error: ' + str(e))
 
    def cmd_ACE_CHECK_FILAMENT_SENSOR(self, gcmd):
        """检查耗材传感器状态的指令"""
        if self.filament_sensor:
            try:
                eventtime = self.reactor.monotonic()
                sensor_status = self.filament_sensor.get_status(eventtime)
                
                filament_detected = sensor_status.get('filament_detected', False)
                sensor_enabled = sensor_status.get('enabled', False)
                
                if filament_detected:
                    gcmd.respond_info("Filament sensor: filament detected")
                else:
                    gcmd.respond_info("Filament sensor: filament not detected")
                    
                gcmd.respond_info(f"Filament sensor: {'enabled' if sensor_enabled else 'disabled'}")
            except Exception as e:
                gcmd.respond_info(f"Error checking filament sensor: {str(e)}")
        else:
            gcmd.respond_info("No filament sensor configured")
 
    def cmd_ACE_START_DRYING(self, gcmd):
        temperature = gcmd.get_int('TEMP', minval=20, maxval=self.max_dryer_temperature)
        duration = gcmd.get_int('DURATION', 240, minval=1)

        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info(f"Drying started at {temperature}°C for {duration} minutes")
        self.send_request({
            "method": "drying",
            "params": {
                "temp": temperature,
                "fan_speed": 7000,
                "duration": duration
            }
        }, callback)
 
    def cmd_ACE_STOP_DRYING(self, gcmd):
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info("Drying stopped")
        self.send_request({"method": "drying_stop"}, callback)
 
    def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        
        # 验证 INDEX 并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_ENABLE_FEED_ASSIST")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                self._feed_assist_index = index
                gcmd.respond_info(f"Feed assist enabled for index {index} (slot {real_slot})")
                self.dwell(0.3, lambda: None)
        self.send_request({"method": "start_feed_assist", "params": {"index": real_slot}}, callback)
 
    def cmd_ACE_DISABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX', self._feed_assist_index, minval=0, maxval=3)
        
        # 验证 INDEX 并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_DISABLE_FEED_ASSIST")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                self._feed_assist_index = -1
                gcmd.respond_info(f"Feed assist disabled for index {index} (slot {real_slot})")
                self.dwell(0.3, lambda: None)
        self.send_request({"method": "stop_feed_assist", "params": {"index": real_slot}}, callback)
 
    def cmd_ACE_PARK_TO_TOOLHEAD(self, gcmd):
        if self._park_in_progress:
            gcmd.respond_raw("Already parking to toolhead")
            return
        
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        
        # 验证 INDEX 并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_PARK_TO_TOOLHEAD")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        # 检查料槽状态(应为'ready')
        is_valid, error = self._validate_slot_status(real_slot, 'ready')
        if not is_valid:
            self.gcode.run_script_from_command(f"_ACE_ON_EMPTY_ERROR INDEX={index}")
            return
        
        self._park_to_toolhead(real_slot)
 
    def cmd_ACE_FEED(self, gcmd):
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        length = gcmd.get_int('LENGTH', minval=1)
        speed = gcmd.get_int('SPEED', self.feed_speed, minval=1)
        
        # 验证INDEX并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_FEED")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        self.send_request({
            "method": "feed_filament",
            "params": {"index": real_slot, "length": length, "speed": speed}
        }, callback)
        self.dwell((length / speed) + 0.1, lambda: None)
 
    def cmd_ACE_UPDATE_FEEDING_SPEED(self, gcmd):
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        speed = gcmd.get_int('SPEED', self.feed_speed, minval=1)
        
        # 验证 INDEX 并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_UPDATE_FEEDING_SPEED")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        self.send_request({
            "method": "update_feeding_speed",
            "params": {"index": real_slot, "speed": speed}
        }, callback)
        self.dwell(0.5, lambda: None)
 
    def cmd_ACE_STOP_FEED(self, gcmd):
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        
        # 验证INDEX并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_STOP_FEED")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info("Feed stopped")
        self.send_request({"method": "stop_feed_filament", "params": {"index": real_slot}}, callback)
        self.dwell(0.5, lambda: None)
 
    def cmd_ACE_RETRACT(self, gcmd):
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        length = gcmd.get_int('LENGTH', minval=1)
        speed = gcmd.get_int('SPEED', self.retract_speed, minval=1)
        mode = gcmd.get_int('MODE', self.retract_mode, minval=0, maxval=1)
        
        # 验证INDEX并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_RETRACT")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        self.send_request({
            "method": "unwind_filament",
            "params": {"index": real_slot, "length": length, "speed": speed, "mode": mode}
        }, callback)
        # 使用异步等待而不是阻塞式等待
        self.dwell((length / speed) + 0.1, lambda: None)
 
    def cmd_ACE_UPDATE_RETRACT_SPEED(self, gcmd):
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        speed = gcmd.get_int('SPEED', self.retract_speed, minval=1)
        
        # 验证INDEX并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_UPDATE_RETRACT_SPEED")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        self.send_request({
            "method": "update_unwinding_speed",
            "params": {"index": real_slot, "speed": speed}
        }, callback)
        self.dwell(0.5, lambda: None)
 
    def cmd_ACE_STOP_RETRACT(self, gcmd):
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        
        # 验证INDEX并转换为真实料槽
        real_slot, error = self._validate_index_for_operation(index, "ACE_STOP_RETRACT")
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info("Retract stopped")
        self.send_request({"method": "stop_unwind_filament", "params": {"index": real_slot}}, callback)
        self.dwell(0.5, lambda: None)
 
    def _distance_based_parking(self, index: int):
        """
        当未配置耗材传感器时,使用基于距离的停车算法.

        算法:
        1. 送料(最大停车距离 - 20) 毫米
        2. 等待(最大停车距离 / 停车速度) 秒
        3. 轮询料槽状态,直至其变为“就绪”状态
        4. 启动传统停车(送料辅助)
        """
        self.logger.info(f"Starting distance-based parking for slot {index}")

        # 设置停车标志
        self._park_in_progress = True
        self._park_error = False
        self._park_index = index
        self._park_start_time = self.reactor.monotonic()
        # 设置传感器停车标志(使用相同的标志以保持兼容性)
        self._sensor_parking_active = True
        self._sensor_parking_completed = False

        # 计算进给距离:最大停车距离 - 20毫米
        feed_distance = max(self.max_parking_distance - 20, 10)  # 最小10毫米
        # 计算等待时间:最大停车距离 / 停车速度(秒)
        wait_time = self.max_parking_distance / self.parking_speed
        
        self.logger.info(f"Distance-based parking: feeding {feed_distance}mm, wait time {wait_time:.1f}s")

        # 开始送料
        def start_feed_callback(response):
            if response.get('code', 0) != 0:
                self.logger.error(f"Error starting feed for distance-based parking: {response.get('msg', 'Unknown error')}")
                self._park_in_progress = False
                self._park_error = True
                self._sensor_parking_active = False
                return

            self.logger.info(f"Started feeding filament for slot {index}: {feed_distance}mm at speed {self.parking_speed}")
            
            # 安排等待和状态检查
            self.dwell(wait_time, lambda: self._check_slot_status_for_parking(index))

        # 发送进给命令
        self.send_request({
            "method": "feed_filament",
            "params": {"index": index, "length": feed_distance, "speed": self.parking_speed}
        }, start_feed_callback)
        
        return True

    def _check_slot_status_for_parking(self, index: int):
        """
        在基于距离的送料后检查料槽状态,准备就绪后开始传统停车.
        """
        if not self._park_in_progress:
            self.logger.info(f"Parking already cancelled for slot {index}")
            return

        # 检查料槽状态
        slots = self._info.get('slots', [])
        slot_status = 'unknown'
        if index >= 0 and index < len(slots):
            slot_status = slots[index].get('status', 'unknown')
            
            if slot_status == 'ready':
                self.logger.info(f"Slot {index} is ready, switching to traditional parking")
                self._sensor_parking_active = False
                self._sensor_parking_completed = True
                self._switch_to_traditional_parking(index)
                return
        
        # 料槽尚未就绪,请稍后再次检查
        elapsed = self.reactor.monotonic() - self._park_start_time
        max_wait_time = self.max_parking_timeout
        
        if elapsed > max_wait_time:
            self.logger.error(f"Distance-based parking timeout for slot {index} after {elapsed:.1f}s")
            self._park_in_progress = False
            self._park_error = True
            self._sensor_parking_active = False
            self._sensor_parking_completed = False
            self._pause_print_if_needed()
            return
        
        # 继续轮询
        self.logger.debug(f"Slot {index} not ready yet (status: {slot_status}), waiting...")
        self.dwell(0.5, lambda: self._check_slot_status_for_parking(index))

    def _sensor_based_parking(self, index: int):
        """
        使用耗材传感器检测的替代停车算法.
        开始给耗材供料并监控传感器.当传感器触发时,
        停止送料,并切换至传统停车算法.
        """
        if not self.filament_sensor:
            self.logger.error("Filament sensor not configured for sensor-based parking")
            return False

        self.logger.info(f"Starting sensor-based parking for slot {index}")

        # 设置停车标志
        self._park_in_progress = True
        self._park_error = False
        self._park_index = index
        self._park_start_time = self.reactor.monotonic()
        # 设置传感器停车标志
        self._sensor_parking_active = True
        self._sensor_parking_completed = False

        # 计算超时时间:(最大停车距离 / 停车速度) + 延长停车时间秒
        timeout_duration = (self.max_parking_distance / self.parking_speed) + self.extended_park_time
        self.logger.info(f"Sensor-based parking timeout: {timeout_duration:.1f}s")
        
        # 以停车速度开始送料
        def start_feed_callback(response):
            if response.get('code', 0) != 0:
                self.logger.error(f"Error starting feed for sensor-based parking: {response.get('msg', 'Unknown error')}")
                self._park_in_progress = False
                self._park_error = True
                # 清理计时器引用
                self._park_monitor_timer = None
                self._sensor_monitor_timer = None
                return

            self.logger.info(f"Started feeding filament for slot {index} at speed {self.parking_speed}")

            # 开始监测耗材传感器
            self._monitor_filament_sensor_for_parking(index, timeout_duration)
        
        # 发送送料命令
        self.send_request({
            "method": "feed_filament",
            "params": {"index": index, "length": self.max_parking_distance, "speed": self.parking_speed}
        }, start_feed_callback)
        
        return True

    def _monitor_filament_sensor_for_parking(self, index: int, timeout_duration: float):
        """
        在停车过程中监测耗材传感器,并在其被触发时切换至传统算法.
        """
        start_time = self.reactor.monotonic()

        def cleanup_sensor_timer():
            """清理传感器计时器引用"""
            self._sensor_monitor_timer = None

        def check_sensor(eventtime):
            if not self._park_in_progress:
                # 停车取消或已在其他地方完成
                cleanup_sensor_timer()
                return self.reactor.NEVER
            
            # 检查是否已达到超时时间
            elapsed = eventtime - start_time
            if elapsed > timeout_duration:
                self.logger.error(f"Sensor-based parking timeout for slot {index} after {elapsed:.1f}s")
                # 停止送料
                self.send_request({
                    "method": "stop_feed_filament",
                    "params": {"index": index}
                }, lambda r: None)
                
                # 同时停止进给辅助以防止冲突
                self.send_request({
                    "method": "stop_feed_assist",
                    "params": {"index": index}
                }, lambda r: None)
                
                self._park_in_progress = False
                self._park_error = True
                # 重置传感器停车标志
                self._sensor_parking_active = False
                self._sensor_parking_completed = False
                # 检查打印状态并在需要时调用暂停
                self._pause_print_if_needed()
                cleanup_sensor_timer()
                return self.reactor.NEVER
            
            # 检查耗材传感器状态
            try:
                sensor_status = self.filament_sensor.get_status(eventtime)
                filament_detected = sensor_status.get('filament_detected', False)
                
                if filament_detected:
                    self.logger.info(f"Filament detected by sensor for slot {index}, switching to traditional parking")
                    # 停止供给耗材,并可能停止任何主动的送料辅助
                    self.send_request({
                        "method": "stop_feed_filament",
                        "params": {"index": index}
                    }, lambda r: None)
                    
                    # 切换标志:传感器停车完成,启动传统停车
                    self._sensor_parking_active = False
                    self._sensor_parking_completed = True
                    
                    # 在切换到传统停车前等待设备状态(ready) 的循环
                    # 轮询间隔:0.2秒,超时:5秒
                    status_wait_start = self.reactor.monotonic()
                    status_wait_timeout = 5.0
                    status_poll_interval = 0.2
                    
                    def wait_for_device_ready(eventtime):
                        elapsed = eventtime - status_wait_start
                        
                        # 检查超时
                        if elapsed > status_wait_timeout:
                            self.logger.warning(f"Timeout waiting for device ready status after stop_feed_filament for slot {index}, continuing anyway")
                            self.gcode.respond_info("ACE: Timeout waiting for device ready, continuing with traditional parking")
                            self._switch_to_traditional_parking(index)
                            cleanup_sensor_timer()
                            return self.reactor.NEVER
                        
                        # 检查设备状态
                        current_status = self._info.get('status', 'unknown')
                        if current_status == 'ready':
                            self.logger.info(f"Device ready after {elapsed:.1f}s, switching to traditional parking for slot {index}")
                            self._switch_to_traditional_parking(index)
                            cleanup_sensor_timer()
                            return self.reactor.NEVER
                        
                        # 继续轮询
                        return eventtime + status_poll_interval
                    
                    # 启动状态轮询计时器
                    self.reactor.register_timer(wait_for_device_ready, self.reactor.NOW)
                    return self.reactor.NEVER
                else:
                    # 继续监测
                    return eventtime + 0.1  # 每100毫秒检查一次
            except Exception as e:
                self.logger.error(f"Error checking filament sensor during parking: {str(e)}")
                # Stop feeding filament
                self.send_request({
                    "method": "stop_feed_filament",
                    "params": {"index": index}
                }, lambda r: None)
                
                # 同时停止送料辅助,以防止冲突
                self.send_request({
                    "method": "stop_feed_assist",
                    "params": {"index": index}
                }, lambda r: None)
                
                self._park_in_progress = False
                self._park_error = True
                # 重置传感器停车标志
                self._sensor_parking_active = False
                self._sensor_parking_completed = False
                # 检查打印状态并在需要时调用暂停
                self._pause_print_if_needed()
                cleanup_sensor_timer()
                return self.reactor.NEVER

        # 注册定时器以监控传感器并保存参考数据
        self._sensor_monitor_timer = self.reactor.register_timer(check_sensor, self.reactor.NOW)

    def _switch_to_traditional_parking(self, index: int):
        """
        从基于传感器的停车切换到传统停车算法.
        传感器检测到耗材后,我们开始送料辅助并监控停车完成情况
        使用传统算法(feed_assist_count跟踪)
        """
        self.logger.info(f"Switching to traditional parking for slot {index} after sensor detection")

        # 关键:为新的停车阶段重置计时器和计数器
        # 这是必要的,因为elapsed_time和hit_count是累积计算的
        # 在基于传感器的停车阶段,这会引起错误报警
        self._park_start_time = self.reactor.monotonic()
        self._assist_hit_count = 0
        self._park_count_increased = False
        self._last_assist_count = 0
        self.logger.info("Reset parking timers for traditional phase: start_time reset, hit_count=0")

        # 首先,在开始传统停车之前,确保送料辅助已停止
        # 这可以防止两个停车阶段之间发生冲突
        def ensure_feed_assist_stopped(response):
            if response.get('code', 0) != 0:
                self.logger.warning(f"Warning stopping feed assist before traditional parking: {response.get('msg', 'Unknown error')}")
            else:
                self.logger.info(f"Feed assist stopped successfully before traditional parking for slot {index}")
            
            # 现在开始为传统停车提供送料辅助功能
            def start_feed_callback(response):
                if response.get('code', 0) != 0:
                    self.logger.error(f"Error starting feed assist for traditional parking: {response.get('msg', 'Unknown error')}")
                    self._park_error = True
                    self._park_in_progress = False
                    return

                # 获取初始的 feed_assist_count 计数器
                self._last_assist_count = response.get('result', {}).get('feed_assist_count', 0)
                self.logger.info(f"Traditional parking started for slot {index}, initial count: {self._last_assist_count}")
                # 后续监控将在 _reader_loop 中通过 _handle_response 进行

            # 为该料槽激活送料辅助功能
            self.send_request({
                "method": "start_feed_assist",
                "params": {"index": index}
            }, start_feed_callback)

        # 在切换到传统驻车模式之前,停止任何正在进行的辅助停车
        # 这确保了停车阶段之间的平稳过渡
        self.send_request({
            "method": "stop_feed_assist",
            "params": {"index": index}
        }, ensure_feed_assist_stopped)

    def _park_to_toolhead(self, index: int):
        # 在调用任何方法之前设置停车标志,以防止数据竞争
        self._park_in_progress = True
        self._park_error = False
        self._park_index = index
        self._assist_hit_count = 0
        self._park_start_time = self.reactor.monotonic()
        self._park_count_increased = False

        # 检查是否应使用激进停车策略
        if self.aggressive_parking:
            # 检查耗材传感器是否已配置且可用
            if self.filament_sensor:
                self.logger.info(f"Using sensor-based aggressive parking for slot {index}")
                self._sensor_based_parking(index)
            else:
                self.logger.info(f"Using distance-based aggressive parking for slot {index} (no filament sensor)")
                self._distance_based_parking(index)
        else:
            self.logger.info(f"Starting traditional parking for slot {index}")

            def callback(response):
                if response.get('code', 0) != 0:
                    if 'result' in response and 'msg' in response['result']:
                        self.logger.error(f"ACE Error starting feed assist: {response['result']['msg']}")
                    else:
                        self.logger.error(f"ACE Error starting feed assist: {response.get('msg', 'Unknown error')}")
                    # 由于设备无法开始送料,出错时重置停车标志
                    self._park_in_progress = False
                    self._park_monitor_timer = None
                    self._sensor_monitor_timer = None
                    self.logger.error(f"Parking aborted for slot {index} due to start_feed_assist error")
                else:
                    self._last_assist_count = response.get('result', {}).get('feed_assist_count', 0)
                    self.logger.info(f"Feed assist started for slot {index}, count: {self._last_assist_count}")
                self.dwell(0.3, lambda: None)
            self.send_request({"method": "start_feed_assist", "params": {"index": index}}, callback)

    def cmd_ACE_CHANGE_TOOL(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=-1, maxval=3)
        was = self.variables.get('ace_current_index', -1)

        if was == tool:
            gcmd.respond_info(f"Tool already set to {tool}")
            return

        # 将 Klipper 索引转换为设备的真实料槽
        real_tool = self._get_real_slot(tool) if tool != -1 else -1
        real_was = self._get_real_slot(was) if was != -1 else -1

        if tool != -1 and self._info['slots'][real_tool]['status'] != 'ready':
            self.gcode.run_script_from_command(f"_ACE_ON_EMPTY_ERROR INDEX={tool}")
            return

        # 根据模式调用相应的PRE宏
        if self.ins_spool_work:
            self.gcode.run_script_from_command(f"_ACE_PRE_INFINITYSPOOL FROM={was} TO={tool}")
        else:
            self.gcode.run_script_from_command(f"_ACE_PRE_TOOLCHANGE FROM={was} TO={tool}")
        self._park_is_toolchange = True
        self._park_previous_tool = was
        if self.toolhead:
            self.toolhead.wait_moves()
        self.variables['ace_current_index'] = tool
        self._save_variable('ace_current_index', tool)

        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")

        if was != -1:
            # 当 infinity spool 工作时,不执行回退 - 耗材已经用完
            if not self.ins_spool_work:
                # 首先回退当前工具(使用真实料槽)
                self.logger.info(f"Retracting from real slot {real_was} (Klipper index {was})")
                self.send_request({
                    "method": "unwind_filament",
                    "params": {
                        "index": real_was,
                        "length": self.toolchange_retract_length,
                        "speed": self.retract_speed
                    }
                }, callback)
                
                # 等待回抽动作确实完成
                retract_time = (self.toolchange_retract_length / self.retract_speed) + 1.0
                self.logger.info(f"Waiting {retract_time:.1f}s for retract to complete")
                if self.toolhead:
                    self.toolhead.dwell(retract_time)
                
                # 等待料槽准备就绪(回抽后状态变为“就绪”)
                self.logger.info(f"Waiting for real slot {real_was} to be ready")
                timeout = self.reactor.monotonic() + 10.0  # 10 second timeout
                while self._info['slots'][real_was]['status'] != 'ready':
                    if self.reactor.monotonic() > timeout:
                        gcmd.respond_raw(f"ACE Error: Timeout waiting for slot {real_was} to be ready")
                        return
                    if self.toolhead:
                        self.toolhead.dwell(1.0)
                
                self.logger.info(f"Slot {real_was} is ready, parking new tool {tool} (real slot {real_tool})")
            else:
                self.logger.info(f"Skipping retract for infinity spool - slot {real_was} is empty, parking new tool {tool} (real slot {real_tool})")
            
            if tool != -1:
                # 将新工具停靠到喷嘴(使用真实料槽)
                self._park_to_toolhead(real_tool)

                # 等待停车完成(检查self._park_in_progress)
                self.logger.info(f"Waiting for parking to complete (real slot {real_tool})")
                timeout = self.reactor.monotonic() + self.max_parking_timeout  # max_parking_timeout 停车超时秒数
                while self._park_in_progress:
                    if self._connection_lost:
                        gcmd.respond_raw(f"ACE Error: Connection lost during parking for slot {real_tool}")
                        self._pause_print_if_needed()
                        return
                    if self._park_error:
                        gcmd.respond_raw(f"ACE Error: Parking failed for slot {real_tool}")
                        return
                    if self.reactor.monotonic() > timeout:
                        gcmd.respond_raw(f"ACE Error: Timeout waiting for parking to complete ({self.max_parking_timeout}s)")
                        self._pause_print_if_needed()
                        return
                    if self.toolhead:
                        self.toolhead.dwell(1.0)

                self.logger.info("Parking completed, executing post-toolchange")
                if self.toolhead:
                    self.toolhead.wait_moves()

                # 执行换刀后宏程序
                if self.ins_spool_work:
                    self.gcode.run_script_from_command(f'_ACE_POST_INFINITYSPOOL FROM={was} TO={tool}')
                else:
                    self.gcode.run_script_from_command(f'_ACE_POST_TOOLCHANGE FROM={was} TO={tool}')
                if self.toolhead:
                    self.toolhead.wait_moves()
                gcmd.respond_info(f"Tool changed from {was} to {tool} (real slot {real_tool})")
            else:
                # 仅卸载,不添加新工具
                if self.ins_spool_work:
                    self.gcode.run_script_from_command(f'_ACE_POST_INFINITYSPOOL FROM={was} TO={tool}')
                else:
                    self.gcode.run_script_from_command(f'_ACE_POST_TOOLCHANGE FROM={was} TO={tool}')
                if self.toolhead:
                    self.toolhead.wait_moves()
                gcmd.respond_info(f"Tool changed from {was} to {tool}")
        else:
            # 没有前一个工具,直接停靠新工具(使用真实料槽)
            self.logger.info(f"Starting parking for real slot {real_tool} (Klipper index {tool}, no previous tool)")
            self._park_to_toolhead(real_tool)

            # 等待停车完成(检查self._park_in_progress)
            self.logger.info(f"Waiting for parking to complete (real slot {real_tool})")
            timeout = self.reactor.monotonic() + self.max_parking_timeout  # max_parking_timeout seconds timeout for parking
            while self._park_in_progress:
                if self._connection_lost:
                    gcmd.respond_raw(f"ACE Error: Connection lost during parking for slot {real_tool}")
                    self._pause_print_if_needed()
                    return
                if self._park_error:
                    gcmd.respond_raw(f"ACE Error: Parking failed for slot {real_tool}")
                    return
                if self.reactor.monotonic() > timeout:
                    gcmd.respond_raw(f"ACE Error: Timeout waiting for parking to complete ({self.max_parking_timeout}s)")
                    self._pause_print_if_needed()
                    return
                if self.toolhead:
                    self.toolhead.dwell(1.0)
            
            self.logger.info("停车完成,正在执行换色后程序")
            if self.toolhead:
                self.toolhead.wait_moves()

            # 执行换色后宏程序
            if self.ins_spool_work:
                self.gcode.run_script_from_command(f'_ACE_POST_INFINITYSPOOL FROM={was} TO={tool}')
            else:
                self.gcode.run_script_from_command(f'_ACE_POST_TOOLCHANGE FROM={was} TO={tool}')
            if self.toolhead:
                self.toolhead.wait_moves()
            gcmd.respond_info(f"Tool changed from {was} to {tool} (real slot {real_tool})")
     
    def cmd_ACE_DISCONNECT(self, gcmd):
        """强制断开与设备连接的G代码指令"""
        try:
            if self._connected:
                self._manually_disconnected = True  # 标记为手动断开
                self._disconnect()
                gcmd.respond_info("ACE device disconnected successfully")
                self.logger.info("Device manually disconnected via ACE_DISCONNECT command")
            else:
                gcmd.respond_info("ACE device is already disconnected")
        except Exception as e:
            self.logger.error(f"Error during forced disconnect: {str(e)}")
            gcmd.respond_raw(f"Error disconnecting: {str(e)}")

    def cmd_ACE_CONNECT(self, gcmd):
        """用于连接设备的G代码指令"""
        try:
            if self._connected:
                gcmd.respond_info("ACE device is already connected")
            else:
                self._manually_disconnected = False  # 重置手动断开标志
                
                # 尝试立即连接
                success = self._connect()
                
                if success:
                    gcmd.respond_info("ACE device connected successfully")
                    self.logger.info("Device manually connected via ACE_CONNECT command")
                else:
                    gcmd.respond_raw("Failed to connect to ACE device")
                    self.logger.error("Manual connection attempt failed")
        except Exception as e:
            self.logger.error(f"Error during manual connect: {str(e)}")
            gcmd.respond_raw(f"Error connecting: {str(e)}")

    def cmd_ACE_CONNECTION_STATUS(self, gcmd):
        """用于检查连接状态的G代码指令"""
        try:
            status = "connected" if self._connected else "disconnected"
            gcmd.respond_info(f"ACE Connection Status: {status}")

            if self._connected:
                # 提供额外的连接详情
                try:
                    model = self._info.get('model', 'Unknown')
                    firmware = self._info.get('firmware', 'Unknown')
                    gcmd.respond_info(f"Device: {model}, Firmware: {firmware}")
                except Exception:
                    gcmd.respond_info("Device: connected (details unavailable)")
            else:
                gcmd.respond_info(f"Serial Port: {self.serial_name}")
                gcmd.respond_info(f"Baud Rate: {self.baud}")
            
            # 额外的连接丢失状态信息
            if self._connection_lost:
                gcmd.respond_raw(f"ACE: Connection lost flag is set (attempts: {self._reconnect_attempts}/{self._max_reconnect_attempts})")
                gcmd.respond_info("Try ACE_RECONNECT to reset the connection")
        except Exception as e:
            self.logger.error(f"Error checking connection status: {str(e)}")
            gcmd.respond_raw(f"Error checking status: {str(e)}")

    def cmd_ACE_RECONNECT(self, gcmd):
        """用于手动重置连接并清除错误标志的G代码指令"""
        try:
            self.logger.info("Manual reconnection requested via ACE_RECONNECT")
            # 重置错误标志
            self._connection_lost = False
            self._reconnect_attempts = 0
            
            # 尝试连接
            self._manually_disconnected = False
            self._disconnect()
            self.dwell(1.0, lambda: None)
            
            success = self._connect()
            if success:
                gcmd.respond_info("ACE: Reconnection successful")
            else:
                gcmd.respond_raw("ACE: Reconnection failed, will retry automatically")
        except Exception as e:
            self.logger.error(f"Error during manual reconnect: {str(e)}")
            gcmd.respond_raw(f"Error reconnecting: {str(e)}")

    def cmd_ACE_SET_INFINITY_SPOOL_ORDER(self, gcmd):
        """设置无限料盘模式的料槽顺序"""
        order_str = gcmd.get('ORDER', '')
        
        if not order_str:
            gcmd.respond_raw("Error: ORDER parameter is required")
            gcmd.respond_info("Usage: ACE_SET_INFINITY_SPOOL_ORDER ORDER=\"0,1,2,3\"")
            gcmd.respond_info("Use 'none' for empty slots, e.g.: ORDER=\"0,1,none,3\"")
            return
        
        # 解析顺序字符串
        try:
            order_list = [item.strip().lower() for item in order_str.split(',')]
            
            # 验证顺序
            if len(order_list) != 4:
                gcmd.respond_raw(f"Error: Order must contain exactly 4 items, got {len(order_list)}")
                return
            
            # 验证每一项
            valid_slots = []
            for i, item in enumerate(order_list):
                if item == 'none':
                    valid_slots.append('none')
                else:
                    try:
                        slot_num = int(item)
                        if slot_num < 0 or slot_num > 3:
                            gcmd.respond_raw(f"Error: Slot number {slot_num} at position {i + 1} is out of range (0-3)")
                            return
                        valid_slots.append(slot_num)
                    except ValueError:
                        gcmd.respond_raw(f"Error: Invalid value '{item}' at position {i + 1}. Use slot number (0-3) or 'none'")
                        return
            
            # 将顺序保存为逗号分隔的字符串
            order_str_saved = ','.join(str(s) if s != 'none' else 'none' for s in valid_slots)
            self._save_variable('ace_infsp_order', order_str_saved)
            self._save_variable('ace_infsp_position', 0)  # 复位至起始位置
            
            gcmd.respond_info(f"Infinity spool order set: {order_str_saved}")
            gcmd.respond_info(f"Order: {valid_slots}")
            
        except Exception as e:
            self.logger.error(f"Error setting infinity spool order: {str(e)}")
            gcmd.respond_raw(f"Error: {str(e)}")
 
    def cmd_ACE_INFINITY_SPOOL(self, gcmd):
        """
        当 filament 结束时自动切换料槽.
        调用 ACE_CHANGE_TOOL 并设置 ins_spool_work 标志,
        该标志决定将调用哪些宏(PRE/POST_INFINITYSPOOL 而不是 PRE/POST_TOOLCHANGE).
        """
        # 1. 检查操作是否已在进行中
        if self.ins_spool_work:
            gcmd.respond_info("ACE_INFINITY_SPOOL: Operation already in progress")
            self.logger.info("ACE_INFINITY_SPOOL: BLOCKED - ins_spool_work is already True")
            return
        
        # 2. 在开始切换前取消所有活动的监控计时器
        if self.infsp_debounce_timer is not None:
            self.logger.info("ACE_INFINITY_SPOOL: Cancelling debounce timer")
            try:
                self.reactor.unregister_timer(self.infsp_debounce_timer)
            except Exception:
                pass
            self.infsp_debounce_timer = None
        
        if self.infsp_sensor_monitor_timer is not None:
            self.logger.info("ACE_INFINITY_SPOOL: Cancelling sensor monitor timer")
            try:
                self.reactor.unregister_timer(self.infsp_sensor_monitor_timer)
            except Exception:
                pass
            self.infsp_sensor_monitor_timer = None
        
        # 3. 重置 empty_detected 标志
        self.infsp_empty_detected = False
        
        # 4. 设置工作标志
        self.ins_spool_work = True
        self.logger.info("ACE_INFINITY_SPOOL: STARTED - ins_spool_work set to True")
        
        try:
            # 3. 检查 infinity_spool_mode
            if not self.infinity_spool_mode:
                gcmd.respond_info("ACE_INFINITY_SPOOL: Mode is disabled")
                return
            
            # 4. 获取当前索引
            current_index = self.variables.get('ace_current_index', -1)
            
            if current_index == -1:
                gcmd.respond_info("ACE_INFINITY_SPOOL: Tool is not set")
                return
            
            # 5. 获取料槽顺序
            order_str = self.variables.get('ace_infsp_order', '')
            
            # 6. 选择下一个料槽
            next_slot = None
            new_position = None
            
            if order_str:
                # 解析顺序(格式 "0,2,1,3" 或类似)
                # 检查 order_str 的类型 - 可能是字符串或元组
                self.logger.debug(f"ace_infsp_order type: {type(order_str).__name__}, value: {order_str}")
                try:
                    order_list = []
                    # 如果 order_str 是元组或列表,直接转换为列表
                    if isinstance(order_str, (tuple, list)):
                        self.logger.info(f"ace_infsp_order is {type(order_str).__name__}, converting to list")
                        for item in order_str:
                            item_str = str(item).strip().lower()
                            if item_str == 'none':
                                order_list.append('none')
                            else:
                                order_list.append(int(item_str))
                    else:
                        # 字符串格式 - 通过 split 解析
                        for item in str(order_str).split(','):
                            item = item.strip().lower()
                            if item == 'none':
                                order_list.append('none')
                            else:
                                order_list.append(int(item))
                    
                    # 获取顺序中的当前位置
                    current_pos = self.variables.get('ace_infsp_position', -1)
                    
                    # 如果位置未保存,则在顺序中查找当前料槽
                    if current_pos < 0 or current_pos >= len(order_list):
                        for i, slot in enumerate(order_list):
                            if slot != 'none' and slot == current_index:
                                current_pos = i
                                break
                    
                    # 查找顺序中的下一个
                    for i in range(len(order_list)):
                        idx = (current_pos + 1 + i) % len(order_list)
                        slot = order_list[idx]
                        if slot != 'none' and self._is_slot_ready(slot):
                            next_slot = slot
                            new_position = idx
                            break
                            
                except Exception as e:
                    self.logger.error(f"Error parsing infinity spool order: {str(e)}")
            else:
                # 按顺序 0,1,2,3 查找第一个可用的
                for idx in range(4):
                    if self._is_slot_ready(idx):
                        next_slot = idx
                        new_position = idx
                        break

            if next_slot is None:
                gcmd.respond_info("ACE_INFINITY_SPOOL: No ready slot found")
                return

            # 7.更新位置
            if new_position is not None:
                self._save_variable('ace_infsp_position', new_position)
            
            self.logger.info(f"ACE_INFINITY_SPOOL: changing from {current_index} to {next_slot}")
            
            # 8.执行工具切换
            self.gcode.run_script_from_command(f"ACE_CHANGE_TOOL TOOL={next_slot}")
            
        finally:
            # 9. 在完成前重置标志和状态
            self.logger.info(f"ACE_INFINITY_SPOOL: FINALLY - resetting ins_spool_work from {self.ins_spool_work} to False")
            self.ins_spool_work = False
            # 重置最后已知状态,以避免在下次调用 _check_slot_empty_status 时重复触发
            self.infsp_last_active_status = None

    def cmd_ACE_GET_HELP(self, gcmd):
        """显示所有可用的ACE命令及其说明"""
        help_text = """
====== ValgACE 命令&帮助 ======

信息指令:
  ACE_STATUS                - 获取完整的ACE设备状态
  ACE_FILAMENT_INFO         - 从料槽获取耗材信息(需使用RFID)
  ACE_CHECK_FILAMENT_SENSOR - 检查外部耗材传感器状态

工具管理:
  ACE_CHANGE_TOOL           - 更换工具(自动加载/卸载耗材)
  ACE_PARK_TO_TOOLHEAD      - 将耗材停放在工具头喷嘴处

耗材控制:
  ACE_FEED                  - 从指定料槽送入耗材
  ACE_RETRACT               - 将耗材回收到料槽中
  ACE_STOP_FEED             - 停止退料
  ACE_STOP_RETRACT          - 停止回抽
  ACE_UPDATE_FEEDING_SPEED  - 实时更新送料速度
  ACE_UPDATE_RETRACT_SPEED  - 实时更新回抽速度

送料辅助:
  ACE_ENABLE_FEED_ASSIST    - 为料槽启用进料辅助
  ACE_DISABLE_FEED_ASSIST   - 禁用料槽的进料辅助

干燥控制:
  ACE_START_DRYING          - 开始耗材干燥过程
  ACE_STOP_DRYING           - 停止耗材干燥过程

连接:
  ACE_DISCONNECT            - 强制断开与ACE设备的连接
  ACE_CONNECT               - 连接到ACE设备
  ACE_CONNECTION_STATUS     - 检查连接状态
  ACE_RECONNECT             - 重置连接并清除错误标志

无限耗材模式:
  ACE_SET_INFINITY_SPOOL_ORDER - 设置无限料盘的料槽切换顺序
  ACE_INFINITY_SPOOL        - 耗材用尽时自动更换料盘

料槽映射:
  ACE_GET_SLOTMAPPING       - 获取当前料槽映射(索引到料槽)
  ACE_SET_SLOTMAPPING       - 设置料槽映射(INDEX=0-3,SLOT=0-3)
  ACE_RESET_SLOTMAPPING     - 将料槽映射重置为默认值(0→0,1→1,2→2,3→3)

索引管理:
  ACE_GET_CURRENT_INDEX     - 获取当前工具索引值
  ACE_SET_CURRENT_INDEX     - 设置当前工具索引值(用于错误恢复)

调试:
  ACE_DEBUG                 - 用于直接设备交互的调试命令
===================================

"""
        gcmd.respond_info(help_text)

    def cmd_ACE_GET_SLOTMAPPING(self, gcmd):
        """
        获取当前的索引到料槽映射.
        
        输出格式
        Slot Mapping:
          Index 0 → Slot X
          Index 1 → Slot X
          Index 2 → Slot X
          Index 3 → Slot X
        """
        output = ["=== Slot Mapping ==="]
        for i in range(4):
            output.append(f"  Index {i} → Slot {self.index_to_slot[i]}")
        output.append("")
        output.append(f"Current mapping: {self.index_to_slot}")
        gcmd.respond_info("\n".join(output))

    def cmd_ACE_SET_SLOTMAPPING(self, gcmd):
        """
        设置索引到料槽的映射.
        
        参数 / Parameters:
          INDEX=0-3  - 来自Klipper的索引(T0-T3) / Index from Klipper(T0-T3)
          SLOT=0-3   - 设备的真实料槽 / Real device slot
        """
        index = gcmd.get_int('INDEX', minval=0, maxval=3)
        slot = gcmd.get_int('SLOT', minval=0, maxval=3)
        
        # 验证INDEX
        real_index, error = self._validate_index(index)
        if error:
            gcmd.respond_raw(f"ACE Error: {error}")
            return
        
        # 验证SLOT
        if slot < 0 or slot > 3:
            gcmd.respond_raw(f"ACE Error: SLOT {slot} out of range(must be 0-3)")
            return
        
        old_slot = self.index_to_slot[index]
        
        if self._set_slot_mapping(index, slot):
            gcmd.respond_info(f"Slot mapping updated: Index {index} → Slot {slot} (was Slot {old_slot})")
            gcmd.respond_info(f"Current mapping: {self.index_to_slot}")
        else:
            gcmd.respond_raw(f"Error: Failed to set slot mapping for index {index}")

    def cmd_ACE_RESET_SLOTMAPPING(self, gcmd):
        """
        将料槽映射重置为默认值.
        将料槽映射重置为默认值(0→0,1→1,2→2,3→3).
        """
        old_mapping = self.index_to_slot.copy()
        self._reset_slot_mapping()
        gcmd.respond_info("Slot mapping reset to defaults")
        gcmd.respond_info(f"  Old mapping: {old_mapping}")
        gcmd.respond_info(f"  New mapping: {self.index_to_slot}")

    def cmd_ACE_GET_CURRENT_INDEX(self, gcmd):
        """
        获取当前工具索引值.
        此命令输出ace_current_index变量的当前值.
        """
        current_index = self.variables.get('ace_current_index', -1)
        gcmd.respond_info(f"Current tool index: {current_index}")
        
    def cmd_ACE_SET_CURRENT_INDEX(self, gcmd):
        """
        设置当前工具索引值.
        此命令允许用户在 -1 到 3 的范围内设置任意索引.
        当打印机遇到错误且在耗材更换过程中未记录正确索引时非常有用.
        
        Parameters:
          INDEX: The index to set(-1 to 3)
        """
        new_index = gcmd.get_int('INDEX', minval=-1, maxval=3)
        
        old_index = self.variables.get('ace_current_index', -1)
        
        # 更新变量
        self.variables['ace_current_index'] = new_index
        self._save_variable('ace_current_index', new_index)
        
        gcmd.respond_info(f"Tool index changed from {old_index} to {new_index}")

    # ============================================================
    # 无限料盘自动触发方法
    # ============================================================

    def _is_printer_printing(self):
        """检查打印机是否处于打印状态."""
        try:
            idle_timeout = self.printer.lookup_object('idle_timeout')
            state = idle_timeout.get_status(eventtime=self.reactor.monotonic()).get('state', 'idle')
            return state == 'Printing'
        except Exception:
            return False

    def _get_active_slot_index(self):
        """返回当前活动料槽的索引或 -1."""
        return self.variables.get('ace_current_index', -1)

    def _get_active_slot_status(self):
        """返回当前活动料槽的状态或 None."""
        idx = self._get_active_slot_index()
        if idx is None or idx < 0:
            return None
        # 通过映射获取真实料槽
        real_slot = self._get_real_slot(idx)
        slots = self._info.get('slots', [])
        if real_slot < 0 or real_slot >= len(slots):
            return None
        return slots[real_slot].get('status', None)

    def _check_slot_empty_status(self):
        """检查活动料槽状态是否已更改为 'empty'."""
        if not self.infinity_spool_mode:
            return False
        
        # 重要:如果已经在进行料槽切换,则不要启动监控
        if self.ins_spool_work:
            self.logger.debug("_check_slot_empty_status: 跳过 - ins_spool_work 为 True")
            return False

        current_status = self._get_active_slot_status()
        self.logger.debug(f"_check_slot_empty_status: current_status={current_status}, last_status={self.infsp_last_active_status}, ins_spool_work={self.ins_spool_work}")

        # 检测到切换到 empty
        if current_status == 'empty' and self.infsp_last_active_status != 'empty':
            self.infsp_last_active_status = current_status
            self.logger.info(f"_check_slot_empty_status: EMPTY detected! ins_spool_work={self.ins_spool_work}")
            return True

        self.infsp_last_active_status = current_status
        return False

    def _start_empty_slot_monitoring(self):
        """在检测到 empty 状态时启动 debounce 监控."""
        self.logger.info(f"_start_empty_slot_monitoring: CALLED, ins_spool_work={self.ins_spool_work}, debounce_timer={self.infsp_debounce_timer is not None}")
        
        # 重要:如果已经在进行料槽切换,则不要启动监控
        if self.ins_spool_work:
            self.logger.info("_start_empty_slot_monitoring: SKIP - ins_spool_work is True")
            return
        
        if self.infsp_debounce_timer is not None:
            self.logger.info("_start_empty_slot_monitoring: Cancelling existing debounce timer")
            try:
                self.reactor.unregister_timer(self.infsp_debounce_timer)
            except Exception:
                pass
            self.infsp_debounce_timer = None

        self.infsp_empty_detected = True
        self.infsp_debounce_timer = self.reactor.register_timer(
            self._monitor_empty_slot_debounce,
            self.reactor.monotonic() + self.infinity_spool_debounce
        )

    def _monitor_empty_slot_debounce(self, eventtime):
        """在 debounce 周期后确认 empty 状态."""
        self.infsp_debounce_timer = None

        # 重要:如果已经在进行料槽切换,则不要继续
        if self.ins_spool_work:
            self.logger.info("_monitor_empty_slot_debounce: SKIP - ins_spool_work is True")
            self.infsp_empty_detected = False
            return self.reactor.NEVER

        # 检查条件
        if not self._is_printer_printing():
            self.infsp_empty_detected = False
            return self.reactor.NEVER

        if self._get_active_slot_status() != 'empty':
            self.infsp_empty_detected = False
            return self.reactor.NEVER

        # Empty 状态已确认 — 进入处理流程
        self._handle_infinity_spool_scenario()
        return self.reactor.NEVER

    def _handle_infinity_spool_scenario(self):
        """处理 empty 料槽场景:有传感器或无传感器."""
        # 重要:如果已经在进行料槽切换,则不要继续
        if self.ins_spool_work:
            self.logger.info("_handle_infinity_spool_scenario: SKIP - ins_spool_work is True")
            self.infsp_empty_detected = False
            return
        
        if not self._is_printer_printing():
            self.infsp_empty_detected = False
            return

        # 如果有耗材传感器 — 等待其触发
        if self.filament_sensor:
            self._monitor_filament_sensor_for_empty()
        else:
            # 无传感器 — 暂停或立即切换
            if self.infinity_spool_pause_on_no_sensor:
                self._trigger_pause_macro()
            else:
                self._trigger_infinity_spool_auto()

    def _monitor_filament_sensor_for_empty(self):
        """监控耗材传感器,无超时限制."""
        if self.infsp_sensor_monitor_timer is not None:
            self.infsp_sensor_monitor_timer.cancel()

        self.infsp_sensor_monitor_timer = self.reactor.register_timer(
            self._check_filament_sensor_trigger,
            self.reactor.monotonic() + 1.0  # 每秒检查一次
        )

    def _check_filament_sensor_trigger(self, eventtime):
        """定期检查耗材传感器,无超时限制."""
        # 重要:如果已经在进行料槽切换,则不要继续
        if self.ins_spool_work:
            self.logger.info("_check_filament_sensor_trigger: SKIP - ins_spool_work is True")
            self.infsp_sensor_monitor_timer = None
            self.infsp_empty_detected = False
            return self.reactor.NEVER
        
        # 检查传感器
        try:
            fs = self.printer.lookup_object(f'filament_switch_sensor {self.filament_sensor_name}')
            sensor_active = fs.get_status(eventtime).get('filament_detected', True)

            if not sensor_active:  # 未检测到耗材
                self.infsp_sensor_monitor_timer = None
                self._trigger_infinity_spool_auto()
                return self.reactor.NEVER
        except Exception as e:
            self.logger.warning(f"Error checking filament sensor: {str(e)}")
            pass

        return eventtime + 1.0  # 下一秒再次检查

    def _trigger_infinity_spool_auto(self):
        """以编程方式调用 ACE_INFINITY_SPOOL."""
        self.logger.info(f"_trigger_infinity_spool_auto: 被调用, ins_spool_work={self.ins_spool_work}")
        
        # 重要:如果已经在进行料槽切换,则不要启动
        if self.ins_spool_work:
            self.logger.info("_trigger_infinity_spool_auto: 跳过 - ins_spool_work 为 True")
            self.infsp_empty_detected = False
            return
        
        self.infsp_empty_detected = False

        # 创建虚拟 GCode 命令
        gcode = self.printer.lookup_object('gcode')
        gcode.run_script('ACE_INFINITY_SPOOL')

    def _trigger_pause_macro(self):
        """调用打印暂停宏."""
        self.infsp_empty_detected = False
        gcode = self.printer.lookup_object('gcode')
        gcode.run_script('PAUSE')


def load_config(config):
    return ValgAce(config)
