"""
Moonraker API Extension для ValgACE

This component extends the Moonraker API to access ACE status via REST API and WebSocket.

Installation:
1. Copy to ~/moonraker/moonraker/components/ace_status.py
2. Add to moonraker.conf:
   [ace_status]
"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Optional, Dict, Any
if TYPE_CHECKING:
    from confighelper import ConfigHelper
    from websockets import WebRequest
    from . import klippy_apis
    APIComp = klippy_apis.KlippyAPI


class AceStatus:
    def __init__(self, config: ConfigHelper):
        self.confighelper = config
        self.server = config.get_server()
        self.logger = logging.getLogger(__name__)
        
        # Get klippy_apis component
        self.klippy_apis: APIComp = self.server.lookup_component('klippy_apis')

        # Register API endpoints
        self.server.register_endpoint(
            "/server/ace/status", ["GET"], self.handle_status_request
        )
        self.server.register_endpoint(
            "/server/ace/slots", ["GET"], self.handle_slots_request
        )
        self.server.register_endpoint(
            "/server/ace/command", ["POST"], self.handle_command_request
        )
        
        # Subscribe to printer status updates
        self.server.register_event_handler(
            "server:status_update", self._handle_status_update
        )
        
        # Cache last known status
        self._last_status: Optional[Dict[str, Any]] = None
        
        self.logger.info("ACE Status API extension loaded")
    
    async def handle_status_request(self, webrequest: WebRequest) -> Dict[str, Any]:
        """Handle ACE status request."""
        try:
            # Obtain data directly from the ace module through query_objects
            # The ace module exports data through register_status_handler
            try:
                result = await self.klippy_apis.query_objects({'ace': None})
                ace_data = result.get('ace')
                
                if ace_data and isinstance(ace_data, dict):
                    self._last_status = ace_data
                    return ace_data
                else:
                    self.logger.debug("ACE data not found in query_objects response")
            
            except Exception as e:
                self.logger.debug(f"Could not get ACE data from query_objects: {e}")
            
            # Fallback: use cached status if available
            if self._last_status:
                self.logger.debug("Using cached ACE status")
                return self._last_status
            
            # If there is no data, return the default structure
            self.logger.warning("No ACE data available, returning default structure")
            return {
                "status": "unknown",
                "model": "Anycubic Color Engine Pro",
                "firmware": "Unknown",
                "dryer": {
                    "status": "stop",
                    "target_temp": 0,
                    "duration": 0,
                    "remain_time": 0
                },
                "temp": 0,
                "fan_speed": 0,
                "enable_rfid": 0,
                "slots": [
                    {
                        "index": i,
                        "status": "unknown",
                        "type": "",
                        "color": [0, 0, 0],
                        "sku": "",
                        "rfid": 0
                    }
                    for i in range(4)
                ]
            }
            
        except Exception as e:
            import traceback
            self.logger.error(f"Error getting ACE status: {e}")
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return {"error": str(e)}
    
    async def handle_slots_request(self, webrequest: WebRequest) -> Dict[str, Any]:
        """Handle slot info request."""
        try:
            status = await self.handle_status_request(webrequest)
            
            if "error" in status:
                return status
            
            slots = status.get("slots", [])
            return {
                "slots": slots
            }
        except Exception as e:
            self.logger.error(f"Error getting slots: {e}")
            return {"error": str(e)}
    
    async def handle_command_request(self, webrequest: WebRequest) -> Dict[str, Any]:
        """Handle ACE command execution."""
        try:
            # Command extraction
            command = webrequest.get_str("command", None)
            
            # If command is not in query parameters, try to get it from JSON body
            if not command:
                try:
                    json_body = await webrequest.get_json()
                    if isinstance(json_body, dict):
                        command = json_body.get("command")
                except Exception:
                    pass
            
            if not command:
                return {"error": "Command parameter is required"}
            
            # Obtain the command parameters
            params: Dict[str, Any] = {}

            # 1) Params from JSON body
            try:
                json_body = await webrequest.get_json()
                if isinstance(json_body, dict) and "params" in json_body:
                    jb_params = json_body["params"]
                    if isinstance(jb_params, dict):
                        params.update(jb_params)
            except Exception:
                pass

            # 2) Params from query string
            try:
                args = webrequest.get_args()
            except Exception:
                args = None

            if args:
                # If the query contains a key 'params', we attempt to parse it as JSON
                qp_params = args.get('params')
                if qp_params:
                    # Perhaps as a JSON string or a dict-like string
                    parsed = None
                    if isinstance(qp_params, str):
                        try:
                            import json as _json
                            parsed = _json.loads(qp_params)
                        except Exception:
                            # Attempt to parse the format of type "{'INDEX': 0}"
                            try:
                                parsed = eval(qp_params, {"__builtins__": {}})
                            except Exception:
                                parsed = None
                    elif isinstance(qp_params, dict):
                        parsed = qp_params
                    if isinstance(parsed, dict):
                        params.update(parsed)

                # We also support direct formats such as ?INDEX=0&SPEED=25, etc.
                for k, v in args.items():
                    if k in ("command", "params"):
                        continue
                    params[str(k)] = v
            
            # Forming a G-code command
            gcode_cmd = command
            if params:
                # Convert the values to a string without extra quotes
                def _fmt_val(val):
                    if isinstance(val, bool):
                        return '1' if val else '0'
                    return str(val)
                param_str = " ".join([f"{k}={_fmt_val(v)}" for k, v in params.items()])
                gcode_cmd = f"{command} {param_str}"
            
            # Executing the command through klippy_apis
            try:
                await self.klippy_apis.run_gcode(gcode_cmd)
                
                return {
                    "success": True,
                    "message": f"Command {command} executed successfully",
                    "command": gcode_cmd
                }
            except Exception as e:
                self.logger.error(f"Error executing ACE command {gcode_cmd}: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "command": gcode_cmd
                }
                
        except Exception as e:
            self.logger.error(f"Error handling ACE command request: {e}")
            return {"error": str(e)}
    
    async def _handle_status_update(self, status: Dict[str, Any]) -> None:
        """Handle printer status update"""
        try:
            # Extract ACE data from the printer status
            ace_data = status.get('ace')
            
            if ace_data:
                self._last_status = ace_data
                # Send the update through WebSocket
                self.server.send_event("ace:status_update", ace_data)
        except Exception as e:
            self.logger.debug(f"Error handling status update: {e}")


def load_component(config: ConfigHelper) -> AceStatus:
    return AceStatus(config)
