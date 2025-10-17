#!/usr/bin/env python3
#
# can_base_function.py
#
# This service provides base CAN bus functionality, including TV Tuner simulation,
# Time Synchronization, and Auto-Shutdown, using a robust asyncio architecture.
#
# Version: 1.3.6 (Fixed global RUNNING declaration in shutdown_monitor_task)
#

import zmq
import json
import time
import logging
import signal
import sys
from datetime import datetime
import pytz
from typing import Optional, List, Dict, Any
import asyncio
import aiozmq
import subprocess

# --- Global State ---
RUNNING = True
RELOAD_CONFIG = False
CONFIG: Dict[str, Any] = {}
FEATURES: Dict[str, Any] = {}
ZMQ_CONTEXT: Optional[zmq.Context] = None
ZMQ_PUSH_SOCKET: Optional[zmq.Socket] = None

# --- Logging Setup ---
def setup_logging():
    log_file = '/var/log/rnse_control/can_base_function.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# --- Helper function for BCD conversion ---
def hex_to_bcd(hex_str: str) -> int:
    if not (isinstance(hex_str, str) and len(hex_str) == 2 and hex_str.isalnum()):
        raise ValueError(f"Input must be a 2-char hex string, got '{hex_str}'")
    return int(hex_str[0]) * 10 + int(hex_str[1])

# --- State Management Class ---
class AppState:
    def __init__(self):
        self.last_time_sync_attempt_time: float = 0.0
        # Auto-shutdown state
        self.last_kl15_status: int = 1  # Ignition status (1=ON, 0=OFF)
        self.last_kls_status: int = 1   # Key in lock sensor status (1=IN, 0=PULLED)
        self.shutdown_trigger_timestamp: Optional[float] = None
        self.shutdown_pending: bool = False

    def check_shutdown_condition(self) -> bool:
        """Check if shutdown delay has been reached and execute shutdown."""
        if self.shutdown_pending and self.shutdown_trigger_timestamp:
            if time.time() - self.shutdown_trigger_timestamp >= CONFIG.get('shutdown_delay', 300):
                logger.info("Shutdown delay reached. Shutting down system NOW.")
                shutdown_command = ["sudo", "shutdown", "-h", "now"]
                if execute_system_command(shutdown_command):
                    logger.info("Shutdown command executed successfully.")
                    return True  # Signal to stop the service
                else:
                    logger.error("Shutdown command failed! Resetting shutdown state.")
                    self.shutdown_pending = False
                    self.shutdown_trigger_timestamp = None
        return False

# --- Configuration Handling ---
def load_and_initialize_config(config_path='/home/pi/config.json') -> bool:
    global CONFIG, FEATURES
    logger.info(f"Loading configuration from {config_path}...")
    try:
        with open(config_path, 'r') as f: cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.critical(f"FATAL: Could not load or parse {config_path}: {e}"); return False

    try:
        FEATURES = cfg.setdefault('features', {})
        FEATURES.setdefault('tv_simulation', {'enabled': False})
        FEATURES.setdefault('time_sync', {'enabled': False, 'data_format': 'new_logic'})
        FEATURES.setdefault('auto_shutdown', {'enabled': False, 'trigger': 'ignition_off'})

        zmq_config = cfg.get('zmq', {})
        can_ids = cfg.get('can_ids', {})
        thresholds = cfg.get('thresholds', {})
        
        CONFIG = {
            'zmq_publish_address': zmq_config.get('publish_address'),
            'zmq_send_address': zmq_config.get('send_address'),
            'can_ids': {
                'tv_presence': int(can_ids.get('tv_presence', '0x602'), 16),
                'time_data': int(can_ids.get('time_data', '0x623'), 16),
                'ignition_status': int(can_ids.get('ignition_status', '0x2C3'), 16),
            },
            'time_data_format': FEATURES['time_sync']['data_format'],
            'car_time_zone': FEATURES.get('car_time_zone', 'UTC'),
            'time_sync_threshold_seconds': thresholds.get('time_sync_threshold_minutes', 1.0) * 60,
            'shutdown_delay': thresholds.get('shutdown_delay_ignition_off_seconds', 300),
        }
        
        if not CONFIG['zmq_send_address'] or not CONFIG['zmq_publish_address']:
            raise KeyError("'send_address' or 'publish_address' not found in 'zmq' section")

        log_level = logging.DEBUG if FEATURES.get('debug_mode', False) else logging.INFO
        logger.setLevel(log_level)
            
        logger.info("Configuration for base functions loaded successfully.")
        return True
    except (KeyError, ValueError) as e:
        logger.critical(f"FATAL: Config is missing a key or has an invalid value: {e}", exc_info=True); return False

# --- Core Logic ---
def initialize_zmq_sender() -> bool:
    global ZMQ_CONTEXT, ZMQ_PUSH_SOCKET
    try:
        logger.info(f"Connecting ZeroMQ PUSH socket to {CONFIG['zmq_send_address']}...")
        ZMQ_CONTEXT = zmq.Context.instance()
        ZMQ_PUSH_SOCKET = ZMQ_CONTEXT.socket(zmq.PUSH)
        ZMQ_PUSH_SOCKET.connect(CONFIG['zmq_send_address'])
        return True
    except zmq.ZMQError as e:
        logger.error(f"Failed to connect ZMQ PUSH socket: {e}"); return False

def send_can_message(can_id: int, payload_hex: str) -> bool:
    if not ZMQ_PUSH_SOCKET: return False
    try:
        ZMQ_PUSH_SOCKET.send_multipart([str(can_id).encode('utf-8'), payload_hex.encode('utf-8')])
        return True
    except zmq.ZMQError as e:
        logger.error(f"Failed to queue CAN message via ZMQ: {e}"); return False

def execute_system_command(command_list: List[str]) -> bool:
    if not command_list: return False
    cmd_str = ' '.join(command_list)
    try:
        logger.info(f"Executing system command: {cmd_str}")
        subprocess.run(command_list, check=True, capture_output=True, text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Failed to execute command '{cmd_str}': {e}"); return False

# --- Message Handling ---
def handle_time_data_message(msg: Dict[str, Any], state: AppState):
    if not FEATURES.get('time_sync', {}).get('enabled', False) or msg.get('dlc', 0) < 8: return
    
    data_hex = msg['data_hex']
    time_format = CONFIG['time_data_format']
    
    try:
        if time_format == 'old_logic':
            second, minute, hour = hex_to_bcd(data_hex[6:8]), hex_to_bcd(data_hex[4:6]), hex_to_bcd(data_hex[2:4])
            day, month, year = hex_to_bcd(data_hex[8:10]), hex_to_bcd(data_hex[10:12]), int(data_hex[12:14] + data_hex[14:16])
        else:
            second, minute, hour = int(data_hex[6:8], 16), int(data_hex[4:6], 16), int(data_hex[2:4], 16)
            day, month, year = int(data_hex[8:10], 16), int(data_hex[10:12], 16), (int(data_hex[12:14], 16) * 100) + int(data_hex[14:16], 16)
        
        state.last_time_sync_attempt_time = time.time()
        car_dt = datetime(year=year, month=month, day=day, hour=hour, minute=minute, second=second)
        pi_utc_dt = datetime.now(pytz.utc)
        car_utc_dt = pytz.timezone(CONFIG['car_time_zone']).localize(car_dt).astimezone(pytz.utc)
        time_diff_seconds = abs((car_utc_dt - pi_utc_dt).total_seconds())

        if time_diff_seconds > CONFIG['time_sync_threshold_seconds']:
            date_str = car_utc_dt.strftime('%m%d%H%M%Y.%S')
            logger.info(f"Car time differs by {time_diff_seconds:.1f}s. Syncing system time.")
            execute_system_command(["sudo", "date", "-u", date_str])
        else:
            logger.debug(f"Time sync check: difference {time_diff_seconds:.1f}s (threshold: {CONFIG['time_sync_threshold_seconds']}s)")
            
    except Exception as e:
        logger.warning(f"Could not parse time message (data_hex: {data_hex}): {e}")

def handle_power_status_message(msg: Dict[str, Any], state: AppState):
    """Handle ignition/key status messages for auto-shutdown."""
    if not FEATURES.get('auto_shutdown', {}).get('enabled', False):
        return
        
    if msg.get('dlc', 0) < 1:
        logger.debug(f"Power status message too short (DLC: {msg.get('dlc', 'N/A')}). Skipping.")
        return
        
    try:
        data_hex = msg['data_hex']
        data_byte0 = int(data_hex[:2], 16)
        kls_status = data_byte0 & 0x01       # Bit 0: Key in Lock Sensor (1=IN, 0=PULLED)
        kl15_status = (data_byte0 >> 1) & 0x01 # Bit 1: Ignition KL15 (1=ON, 0=OFF)

        kls_changed = kls_status != state.last_kls_status
        kl15_changed = kl15_status != state.last_kl15_status
        state.last_kls_status = kls_status
        state.last_kl15_status = kl15_status

        trigger_config = FEATURES['auto_shutdown'].get('trigger', 'ignition_off')
        trigger_event = False
        
        # Check for trigger conditions
        if trigger_config == 'ignition_off' and kl15_changed and kl15_status == 0:
            trigger_event = True
            logger.info("Ignition OFF detected. Starting shutdown timer.")
        elif trigger_config == 'key_pulled' and kls_changed and kls_status == 0:
            trigger_event = True
            logger.info("Key PULLED detected. Starting shutdown timer.")

        if trigger_event and not state.shutdown_pending:
            logger.info(f"Starting {CONFIG['shutdown_delay']}s shutdown timer due to '{trigger_config}' trigger.")
            state.shutdown_pending = True
            state.shutdown_trigger_timestamp = time.time()
        # Cancel shutdown if ignition/key comes back ON/IN
        elif state.shutdown_pending:
            if (trigger_config == 'ignition_off' and kl15_changed and kl15_status == 1) or \
               (trigger_config == 'key_pulled' and kls_changed and kls_status == 1):
                logger.info("Ignition ON or Key INSERTED detected. Cancelling pending shutdown.")
                state.shutdown_pending = False
                state.shutdown_trigger_timestamp = None
                
    except (IndexError, ValueError) as e:
        logger.warning(f"Could not parse power status message (data_hex: {msg.get('data_hex', 'N/A')}): {e}")

# --- Async Tasks ---
async def send_periodic_messages_task():
    logger.info("Periodic sender task started.")
    while RUNNING:
        try:
            if FEATURES.get('tv_simulation', {}).get('enabled'):
                send_can_message(CONFIG['can_ids']['tv_presence'], "0912300000000000")
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic sender task: {e}", exc_info=True)
            await asyncio.sleep(5)
    logger.info("Periodic sender task finished.")

async def listen_for_can_messages_task(state: AppState):
    logger.info("ZMQ listener task started.")
    sub_stream = None
    try:
        sub_stream = await aiozmq.create_zmq_stream(
            zmq.SUB,
            connect=CONFIG['zmq_publish_address']
        )
        
        # Subscribe to relevant topics
        time_topic = f"CAN_{CONFIG['can_ids']['time_data']:03X}"
        power_topic = f"CAN_{CONFIG['can_ids']['ignition_status']:03X}"
        
        if FEATURES.get('time_sync', {}).get('enabled', False):
            sub_stream.transport.subscribe(time_topic.encode('utf-8'))
            logger.info(f"Subscribing to time sync topic: {time_topic}")
            
        if FEATURES.get('auto_shutdown', {}).get('enabled', False):
            sub_stream.transport.subscribe(power_topic.encode('utf-8'))
            logger.info(f"Subscribing to power status topic: {power_topic}")

        while RUNNING:
            msg = await sub_stream.read()
            if len(msg) < 2:
                logger.warning(f"Received incomplete message: {msg}")
                continue
            _, msg_bytes = msg
            try:
                msg_dict = json.loads(msg_bytes.decode('utf-8'))
                can_id = msg_dict.get('arbitration_id', 0)
                
                logger.debug(f"Received CAN message ID={can_id:03X}: {msg_dict}")
                
                # Dispatch to handlers
                if can_id == CONFIG['can_ids']['time_data']:
                    handle_time_data_message(msg_dict, state)
                elif can_id == CONFIG['can_ids']['ignition_status']:
                    handle_power_status_message(msg_dict, state)
                    
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to decode JSON from message: {msg_bytes[:100]}... ({e})")
            
    except asyncio.CancelledError:
        logger.info("ZMQ listener task was cancelled.")
    except Exception as e:
        logger.error(f"Critical error in ZMQ listener task setup or loop: {e}", exc_info=True)
    finally:
        if sub_stream:
            sub_stream.close()
        logger.info("ZMQ listener task finished.")

async def shutdown_monitor_task(state: AppState):
    """Monitor shutdown conditions periodically."""
    global RUNNING  # FIXED: Declare global at function start, before use
    while RUNNING:
        try:
            if state.check_shutdown_condition():
                # Shutdown initiated, stop the service
                RUNNING = False
                break
            await asyncio.sleep(1.0)  # Check every second
        except Exception as e:
            logger.error(f"Error in shutdown monitor task: {e}")
            await asyncio.sleep(5)

# --- Signal Handling and Main Loop ---
def setup_signal_handlers(loop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: shutdown_handler(s))
    loop.add_signal_handler(signal.SIGHUP, lambda s=signal.SIGHUP: reload_config_handler(s))

def shutdown_handler(sig):
    global RUNNING
    if RUNNING: logger.info(f"Shutdown signal {sig.name} received. Stopping..."); RUNNING = False

def reload_config_handler(sig):
    global RELOAD_CONFIG
    logger.info("SIGHUP signal received. Flagging for configuration reload."); RELOAD_CONFIG = True

async def main_async():
    global RELOAD_CONFIG
    state = AppState()
    
    tasks = [
        asyncio.create_task(listen_for_can_messages_task(state)),
        asyncio.create_task(send_periodic_messages_task()),
        asyncio.create_task(shutdown_monitor_task(state))
    ]
    
    while RUNNING:
        if RELOAD_CONFIG:
            logger.info("Reloading configuration...")
            if load_and_initialize_config(): 
                RELOAD_CONFIG = False
                logger.warning("Listener subscriptions will not change until the service is fully restarted.")
            else:
                logger.error("Config reload failed!")
        
        await asyncio.sleep(1)

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

def main():
    logger.info("Starting CAN Base Function service...")
    if not load_and_initialize_config(): sys.exit(1)
    if not initialize_zmq_sender(): sys.exit(1)

    loop = asyncio.get_event_loop()
    setup_signal_handlers(loop)
    
    try:
        logger.info("--- Service is running ---")
        loop.run_until_complete(main_async())
    finally:
        logger.info("Main loop terminated. Closing ZeroMQ resources.")
        if ZMQ_PUSH_SOCKET and not ZMQ_PUSH_SOCKET.closed: ZMQ_PUSH_SOCKET.close()
        if ZMQ_CONTEXT and not ZMQ_CONTEXT.closed: ZMQ_CONTEXT.term()
        loop.close()
        logger.info("CAN Base Function service has finished.")

if __name__ == '__main__':
    main()
