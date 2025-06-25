#!/usr/bin/env python3
#
# can_keyboard_control.py
#
# Listens for specific CAN bus messages published by can_handler.py via ZeroMQ
# to translate car controls (MMI, MFSW) into keyboard presses and system commands.
# It also handles media state changes (e.g., play/pause on source switch).
#
# This script is designed to run as a systemd service and uses python-uinput.
# It uses a message counter logic for robust short/long press detection.
#

import zmq
import json
import time
import subprocess
import logging
import signal
import sys
import uinput

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

# --- REFACTORED: State Management Class with Counter Logic ---
class ControlState:
    """Holds the runtime state for MMI, MFSW, and media controls using message counters."""
    def __init__(self):
        # MMI state using counters
        self.mmi_press_counters = {}         # { (byte3, byte4): count }
        self.mmi_long_action_fired = {}      # { (byte3, byte4): bool }
        self.mmi_extended_action_fired = {}  # { (byte3, byte4): bool }
        self.last_mmi_scroll_time = 0

        # MFSW state for mode button using counters
        self.mfsw_mode_press_count = 0
        self.mfsw_mode_long_action_fired = False

        # Media source state
        self.is_pi_source_active = None
        self.last_status_log_time = time.time()

    def reset_mmi_state(self, mmi_command):
        """Resets all states for a specific MMI command tuple."""
        self.mmi_press_counters.pop(mmi_command, None)
        self.mmi_long_action_fired.pop(mmi_command, None)
        self.mmi_extended_action_fired.pop(mmi_command, None)

    def log_periodic_status(self):
        """Logs the current operational status."""
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
            'tv_mode_bytes': [bytes.fromhex(s.replace("0x", "")) for s in cfg['source_data']['tv_mode']],
            'play_key': parse_key(cfg['source_data']['play_key']),
            'pause_key': parse_key(cfg['source_data']['pause_key']),
            'cooldown': thresholds['cooldown_period'],
            'long_press_count': thresholds['long_press_message_count'],
            'extended_press_count': thresholds.get('extended_long_press_message_count', 30), # Default to 30 if not in config
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
        for key, can_id in CONFIG['can_ids'].items():
            if FEATURES.get(f'{key}_controls', True):
                 topic = f"CAN_{can_id:03X}"
                 logger.info(f"Subscribing to topic: {topic}")
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
    try:
        events = get_all_possible_keys()
        if not events:
            logger.warning("No keys mapped. Keyboard device not created.")
            return None
        return uinput.Device(events, name="can-virtual-keyboard")
    except Exception as e:
        logger.critical(f"FATAL: Could not initialize uinput keyboard device: {e}", exc_info=True)
        logger.critical("This may be due to missing permissions for /dev/uinput.")
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

# --- REFACTORED: CAN Message Handlers with Counter Logic ---

def handle_mmi_message(msg, state):
    """Handles MMI inputs using a message counter for short/long/extended presses."""
    if msg['dlc'] < 5: return
    data = bytes.fromhex(msg['data_hex'])
    status, cmd = data[2], (data[3], data[4])
    now = time.time()

    # --- Key Press / Hold Event ---
    if status == 0x01:
        # Increment press counter
        current_count = state.mmi_press_counters.get(cmd, 0) + 1
        state.mmi_press_counters[cmd] = current_count

        # Handle scroll wheels immediately but with a cooldown
        if cmd in CONFIG['mmi_scroll_cmds']:
            if now - state.last_mmi_scroll_time > CONFIG['cooldown']:
                press_key(CONFIG['mmi_short_map'].get(cmd))
                state.last_mmi_scroll_time = now
            return # Don't process scroll commands any further

        # Check for extended press (system actions)
        if FEATURES.get('system_actions') and not state.mmi_extended_action_fired.get(cmd):
            if current_count >= CONFIG['extended_press_count']:
                action = CONFIG['mmi_extended_map'].get(cmd)
                logger.info(f"MMI Extended Press detected for {cmd}. Action: {action}")
                run_command(action)
                state.mmi_extended_action_fired[cmd] = True
                state.mmi_long_action_fired[cmd] = True # Prevent normal long press
        
        # Check for normal long press
        if not state.mmi_long_action_fired.get(cmd):
            if current_count >= CONFIG['long_press_count']:
                key = CONFIG['mmi_long_map'].get(cmd)
                logger.info(f"MMI Long Press detected for {cmd}.")
                press_key(key)
                state.mmi_long_action_fired[cmd] = True

    # --- Key Release Event ---
    elif status == 0x04:
        # Check for short press on release if no long/extended action was fired
        if not state.mmi_long_action_fired.get(cmd) and cmd not in CONFIG['mmi_scroll_cmds']:
            key = CONFIG['mmi_short_map'].get(cmd)
            logger.info(f"MMI Short Press detected for {cmd}.")
            press_key(key)
        
        # Reset all states for this command
        state.reset_mmi_state(cmd)

def handle_mfsw_message(msg, state):
    """Handles MFSW inputs using message counters."""
    if msg['dlc'] < 2: return
    cmd_byte = int(msg['data_hex'][2:4], 16)

    # Handle scroll buttons immediately
    if cmd_byte == CONFIG['mfsw_cmds']['scroll_up']:
        press_key(CONFIG['mfsw_map']['scroll_up'])
    elif cmd_byte == CONFIG['mfsw_cmds']['scroll_down']:
        press_key(CONFIG['mfsw_map']['scroll_down'])

    # Handle press/hold/release for the 'mode' button
    elif cmd_byte == CONFIG['mfsw_cmds']['mode_press']:
        state.mfsw_mode_press_count += 1
        if not state.mfsw_mode_long_action_fired and state.mfsw_mode_press_count >= CONFIG['long_press_count']:
            logger.info("MFSW mode long press detected.")
            press_key(CONFIG['mfsw_map']['mode_long'])
            state.mfsw_mode_long_action_fired = True

    elif cmd_byte in CONFIG['mfsw_release_cmds']:
        if not state.mfsw_mode_long_action_fired and state.mfsw_mode_press_count > 0:
            logger.info("MFSW mode short press detected.")
            press_key(CONFIG['mfsw_map']['mode_short'])
        
        # Reset state on release
        state.mfsw_mode_press_count = 0
        state.mfsw_mode_long_action_fired = False

def handle_source_message(msg, state):
    """Handles media source changes to auto-play/pause."""
    if msg['dlc'] < 8: return
    data = bytes.fromhex(msg['data_hex'])
    is_pi_active = any(data.startswith(sig) for sig in CONFIG['tv_mode_bytes'])
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
    global UINPUT_DEVICE
    logger.info("Starting can_keyboard_control.py service...")

    if not load_and_initialize_config(): sys.exit(1)

    with initialize_uinput_device() as uinput_dev:
        if not uinput_dev: sys.exit(1)
        
        UINPUT_DEVICE = uinput_dev
        setup_signal_handlers()
        state = ControlState()

        if not initialize_zmq_subscriber(): sys.exit(1)
        
        logger.info("--- Service is running ---")
        while RUNNING:
            try:
                topic_bytes, msg_bytes = ZMQ_SUB_SOCKET.recv_multipart()
                msg_dict = json.loads(msg_bytes.decode('utf-8'))
                can_id = msg_dict.get('arbitration_id')

                if FEATURES.get('mmi_controls') and can_id == CONFIG['can_ids']['mmi']: handle_mmi_message(msg_dict, state)
                elif FEATURES.get('mfsw_controls') and can_id == CONFIG['can_ids']['mfsw']: handle_mfsw_message(msg_dict, state)
                elif FEATURES.get('media_control') and can_id == CONFIG['can_ids']['source']: handle_source_message(msg_dict, state)

            except zmq.error.Again:
                if time.time() - state.last_status_log_time > 60: state.log_periodic_status()
            except (zmq.ZMQError, json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(f"A recoverable error occurred: {e}. Reconnecting...")
                if ZMQ_SUB_SOCKET and not ZMQ_SUB_SOCKET.closed: ZMQ_SUB_SOCKET.close()
                initialize_zmq_subscriber()
                time.sleep(5)
            except Exception:
                logger.critical("An unexpected critical error occurred in main loop.", exc_info=True)
                break

    logger.info("Main loop terminated. Closing resources.")
    if ZMQ_SUB_SOCKET and not ZMQ_SUB_SOCKET.closed: ZMQ_SUB_SOCKET.close()
    if ZMQ_CONTEXT and not ZMQ_CONTEXT.closed: ZMQ_CONTEXT.term()
    logger.info("can_keyboard_control.py has finished.")

if __name__ == '__main__':
    main()
