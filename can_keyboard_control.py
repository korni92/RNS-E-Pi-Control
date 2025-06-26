#!/usr/bin/env python3
#
# can_keyboard_control.py
#
# Listens for specific CAN bus messages published by can_handler.py via ZeroMQ
# to translate car controls (MMI, MFSW) into keyboard presses and system commands.
#
# This script is designed to run as a systemd service and uses python-uinput.
# It uses a message counter logic and a global cooldown for robust press detection.
# Version: Final/Robust with Precise Source Toggle logic
#

import zmq
import json
import time
import subprocess
import logging
import signal
import sys
import uinput
import os

# --- Global State ---
RUNNING = True
ZMQ_CONTEXT = None
ZMQ_SUB_SOCKET = None
UINPUT_DEVICE = None
FEATURES = {}
CONFIG = {}

# --- Logging Setup ---
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# --- State Management Class ---
class ControlState:
    def __init__(self):
        self.mmi_press_counters = {}
        self.mmi_long_action_fired = {}
        self.mmi_extended_action_fired = {}
        self.last_mmi_action_info = {'command': None, 'time': 0}
        self.mfsw_mode_press_count = 0
        self.mfsw_mode_long_action_fired = False
        self.is_pi_source_active = None
        self.last_status_log_time = time.time()

    def reset_mmi_state(self, mmi_command):
        self.mmi_press_counters.pop(mmi_command, None)
        self.mmi_long_action_fired.pop(mmi_command, None)
        self.mmi_extended_action_fired.pop(mmi_command, None)

    def log_periodic_status(self):
        active_source = 'Unknown'
        if self.is_pi_source_active is True: active_source = 'Active (Pi)'
        elif self.is_pi_source_active is False: active_source = 'Inactive (Other)'
        logger.info(f"Status | Active Source: {active_source}")
        self.last_status_log_time = time.time()

# --- Configuration Handling ---
def parse_key(key_string):
    if not key_string: return None
    key = getattr(uinput, key_string, None)
    if not key: logger.warning(f"Invalid uinput key name '{key_string}' in config. Ignored.")
    return key

def load_and_initialize_config(config_path='/home/pi/config.json'):
    global CONFIG, FEATURES
    try:
        with open(config_path, 'r') as f: cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.critical(f"FATAL: Could not load or parse {config_path}: {e}")
        return False

    try:
        FEATURES = cfg['features']
        key_maps = cfg['key_mappings']
        thresholds = cfg['thresholds']
        source_data = cfg['source_data']
        
        CONFIG = {
            'zmq_address': cfg['zmq']['publish_address'],
            'can_ids': {k: int(v, 16) for k, v in cfg['can_ids'].items()},
            'mmi_scroll_cmds': {tuple(map(int, k.split(','))) for k in cfg['mmi_scroll_commands']},
            'mmi_short_map': {tuple(map(int, k.split(','))): parse_key(v) for k, v in key_maps['mmi_short'].items()},
            'mmi_long_map': {tuple(map(int, k.split(','))): parse_key(v) for k, v in key_maps['mmi_long'].items()},
            'mmi_extended_map': {tuple(map(int, k.split(','))): v for k, v in key_maps['mmi_extended'].items()},
            'mfsw_cmds': {k: int(v, 16) for k, v in key_maps['mfsw_commands'].items() if isinstance(v, str)},
            'mfsw_release_cmds': [int(v, 16) for v in key_maps['mfsw_commands']['release']],
            'mfsw_map': {k: parse_key(v) for k, v in key_maps['mfsw'].items()},
            'tv_mode_id': int(source_data['tv_mode_identifier'], 16),
            'play_key': parse_key(source_data['play_key']),
            'pause_key': parse_key(source_data['pause_key']),
            'cooldown': thresholds['cooldown_period'],
            'long_press_count': thresholds['long_press_message_count'],
            'extended_press_count': thresholds.get('extended_long_press_message_count', 30),
        }
        logger.info("Configuration loaded and processed successfully.")
        return True
    except (KeyError, ValueError) as e:
        logger.critical(f"FATAL: Configuration is missing a key or has an invalid value: {e}", exc_info=True)
        return False

# --- Core Logic Functions ---
def initialize_zmq_subscriber():
    global ZMQ_CONTEXT, ZMQ_SUB_SOCKET
    try:
        logger.info(f"Connecting ZeroMQ subscriber to {CONFIG['zmq_address']}...")
        ZMQ_CONTEXT = zmq.Context.instance()
        ZMQ_SUB_SOCKET = ZMQ_CONTEXT.socket(zmq.SUB)
        ZMQ_SUB_SOCKET.set(zmq.RCVTIMEO, 1000)
        ZMQ_SUB_SOCKET.connect(CONFIG['zmq_address'])
        
        feature_map = {
            'mmi': 'mmi_controls',
            'mfsw': 'mfsw_controls',
            'source': 'source_controls'
        }

        for key, can_id in CONFIG['can_ids'].items():
            feature_name = feature_map.get(key)
            if feature_name and FEATURES.get(feature_name, False):
                topic = f"CAN_{can_id:03X}"
                logger.info(f"Subscribing to topic: {topic} (feature: {feature_name})")
                ZMQ_SUB_SOCKET.setsockopt_string(zmq.SUBSCRIBE, topic)
        return True
    except zmq.ZMQError as e:
        logger.error(f"Failed to initialize ZeroMQ subscriber: {e}")
        return False

def get_all_possible_keys():
    keys = set()
    for key_map in [CONFIG['mmi_short_map'], CONFIG['mmi_long_map'], CONFIG['mfsw_map']]:
        for key in key_map.values():
            if key: keys.add(key)
    if CONFIG.get('play_key'): keys.add(CONFIG['play_key'])
    if CONFIG.get('pause_key'): keys.add(CONFIG['pause_key'])
    logger.info(f"Found {len(keys)} unique keys to register for the virtual device.")
    return list(keys)

def initialize_uinput_device():
    uinput_path = "/dev/uinput"
    for _ in range(10): 
        if os.path.exists(uinput_path):
            logger.info(f"'{uinput_path}' found.")
            break
        logger.warning(f"Waiting for '{uinput_path}' to become available...")
        time.sleep(1)
    else:
        logger.critical(f"FATAL: Device '{uinput_path}' not found after waiting.")
        return None

    try:
        events = get_all_possible_keys()
        if not events:
            logger.warning("No keys mapped. Keyboard device not created.")
            return None
        
        logger.info("Creating virtual keyboard device...")
        device = uinput.Device(events, name="can-virtual-keyboard")
        logger.info("Virtual keyboard device created successfully.")
        return device
    except Exception as e:
        logger.critical(f"FATAL: Could not initialize uinput keyboard device: {e}", exc_info=True)
        return None

def press_key(key):
    if not key or not UINPUT_DEVICE: return
    try:
        logger.info(f"Simulating key press: {key}")
        UINPUT_DEVICE.emit_click(key)
    except Exception as e:
        logger.error(f"Failed to simulate key '{key}': {e}")

def run_command(command_str):
    if not command_str: return
    try:
        logger.info(f"Executing system command: {command_str}")
        subprocess.run(command_str, shell=True, check=False)
    except Exception as e:
        logger.error(f"Failed to execute command '{command_str}': {e}")

# --- Message Handlers ---
def handle_mmi_message(msg, state):
    if msg['dlc'] < 5: return
    data = bytes.fromhex(msg['data_hex'])
    status, cmd = data[2], (data[3], data[4])
    now = time.time()

    if status == 0x01: # Press Event
        if cmd not in state.mmi_press_counters: 
            state.reset_mmi_state(cmd)
            if now - state.last_mmi_action_info.get('time', 0) < CONFIG['cooldown']:
                return 
        
        current_count = state.mmi_press_counters.get(cmd, 0) + 1
        state.mmi_press_counters[cmd] = current_count

        if cmd in CONFIG['mmi_scroll_cmds']:
            press_key(CONFIG['mmi_short_map'].get(cmd))
            state.mmi_press_counters[cmd] = 0
            return

        if FEATURES.get('system_actions') and not state.mmi_extended_action_fired.get(cmd) and current_count >= CONFIG['extended_press_count']:
            action = CONFIG['mmi_extended_map'].get(cmd)
            logger.info(f"MMI Extended Press: {cmd}")
            run_command(action)
            state.mmi_extended_action_fired[cmd] = True
            state.mmi_long_action_fired[cmd] = True
            state.last_mmi_action_info = {'command': cmd, 'time': now}
        
        elif not state.mmi_long_action_fired.get(cmd) and current_count >= CONFIG['long_press_count']:
            key = CONFIG['mmi_long_map'].get(cmd)
            logger.info(f"MMI Long Press: {cmd}")
            press_key(key)
            state.mmi_long_action_fired[cmd] = True
            state.last_mmi_action_info = {'command': cmd, 'time': now}

    elif status == 0x04: # Release Event
        if cmd in state.mmi_press_counters and not state.mmi_long_action_fired.get(cmd):
            if cmd not in CONFIG['mmi_scroll_cmds']:
                key = CONFIG['mmi_short_map'].get(cmd)
                logger.info(f"MMI Short Press: {cmd}")
                press_key(key)
                state.last_mmi_action_info = {'command': cmd, 'time': now}
        
        state.mmi_press_counters.pop(cmd, None)

def handle_mfsw_message(msg, state):
    if msg['dlc'] < 2: return
    cmd_byte = int(msg['data_hex'][2:4], 16)
    if cmd_byte == CONFIG['mfsw_cmds']['scroll_up']: press_key(CONFIG['mfsw_map'].get('scroll_up'))
    elif cmd_byte == CONFIG['mfsw_cmds']['scroll_down']: press_key(CONFIG['mfsw_map'].get('scroll_down'))
    elif cmd_byte == CONFIG['mfsw_cmds']['mode_press']:
        state.mfsw_mode_press_count += 1
        if not state.mfsw_mode_long_action_fired and state.mfsw_mode_press_count >= CONFIG['long_press_count']:
            logger.info("MFSW Mode Long Press")
            press_key(CONFIG['mfsw_map'].get('mode_long'))
            state.mfsw_mode_long_action_fired = True
    elif cmd_byte in CONFIG['mfsw_release_cmds']:
        if not state.mfsw_mode_long_action_fired and state.mfsw_mode_press_count > 0:
            logger.info("MFSW Mode Short Press")
            press_key(CONFIG['mfsw_map'].get('mode_short'))
        state.mfsw_mode_press_count = 0
        state.mfsw_mode_long_action_fired = False

def handle_source_message(msg, state):
    """Processes RNS-E source messages to auto-play/pause media."""
    # We need at least 4 bytes to check the 4th one (index 3)
    if msg['dlc'] < 4: return
    data = bytes.fromhex(msg['data_hex'])
    
    # Precise check: Examine the 4th byte (index 3) for the TV mode identifier.
    # This corresponds to RA4_Radio_Para2 in the DBC and is more robust.
    current_mode_byte = data[3]
    is_pi_active = (current_mode_byte == CONFIG.get('tv_mode_id'))

    if is_pi_active != state.is_pi_source_active:
        state.is_pi_source_active = is_pi_active
        key_to_press = CONFIG['play_key'] if is_pi_active else CONFIG['pause_key']
        action = "PLAY" if is_pi_active else "PAUSE"
        logger.info(f"Source switched. Simulating {action}.")
        press_key(key_to_press)

# --- Signal Handling and Main Loop ---
def setup_signal_handlers():
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

def shutdown_handler(signum, frame):
    global RUNNING
    if RUNNING:
        logger.info(f"Shutdown signal {signum} received. Cleaning up...")
        RUNNING = False

def main():
    global UINPUT_DEVICE, RUNNING

    logger.info("Starting can_keyboard_control.py service...")
    if not load_and_initialize_config(): sys.exit(1)
    
    UINPUT_DEVICE = initialize_uinput_device()
    if not UINPUT_DEVICE:
        logger.warning("Continuing without virtual keyboard. Only logging will occur.")
    
    setup_signal_handlers()
    state = ControlState()
    if not initialize_zmq_subscriber():
        if UINPUT_DEVICE: UINPUT_DEVICE.destroy()
        sys.exit(1)
        
    logger.info("--- Service is running ---")
    while RUNNING:
        try:
            if ZMQ_SUB_SOCKET.poll(timeout=1000):
                _, msg_bytes = ZMQ_SUB_SOCKET.recv_multipart()
                msg_dict = json.loads(msg_bytes.decode('utf-8'))
                can_id = msg_dict.get('arbitration_id')
                
                # Check for feature flags before calling handlers
                if can_id == CONFIG['can_ids'].get('mmi') and FEATURES.get('mmi_controls', False):
                    handle_mmi_message(msg_dict, state)
                elif can_id == CONFIG['can_ids'].get('mfsw') and FEATURES.get('mfsw_controls', False):
                    handle_mfsw_message(msg_dict, state)
                elif can_id == CONFIG['can_ids'].get('source') and FEATURES.get('source_controls', False):
                    handle_source_message(msg_dict, state)
            
            if time.time() - state.last_status_log_time > 60:
                state.log_periodic_status()

        except (zmq.ZMQError, json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"A recoverable error occurred: {e}. Reconnecting...")
            if ZMQ_SUB_SOCKET and not ZMQ_SUB_SOCKET.closed: ZMQ_SUB_SOCKET.close()
            initialize_zmq_subscriber()
            time.sleep(5)
        except Exception:
            logger.critical("An unexpected critical error in main loop.", exc_info=True)
            RUNNING = False

    logger.info("Main loop terminated. Closing resources.")
    if UINPUT_DEVICE: UINPUT_DEVICE.destroy()
    if ZMQ_SUB_SOCKET and not ZMQ_SUB_SOCKET.closed: ZMQ_SUB_SOCKET.close()
    if ZMQ_CONTEXT and not ZMQ_CONTEXT.closed: ZMQ_CONTEXT.term()
    logger.info("can_keyboard_control.py has finished.")

if __name__ == '__main__':
    main()
