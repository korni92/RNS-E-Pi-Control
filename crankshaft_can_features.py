#!/usr/bin/env python3
#
# crankshaft_can_features.py
#
# This service acts as an advanced bridge between the car's CAN bus and the
# Raspberry Pi running Crankshaft. It both listens for specific CAN messages
# to trigger system actions and periodically sends messages to simulate devices.
#
# Receiving Features:
#  - Automatic Day/Night Mode.
#  - System time synchronization.
#  - Automatic shutdown based on ignition/key status.
#
# Sending Features:
#  - TV-Tuner presence simulation to enable the video input on RNS-E head units.
#
# Designed to run as a robust, long-running systemd service.
#

import zmq
import json
import time
import subprocess
import logging
import signal
import sys
from datetime import datetime
import pytz

# --- Global State ---
RUNNING = True
RELOAD_CONFIG = False
CONFIG = {}
FEATURES = {}
ZMQ_CONTEXT = None
ZMQ_SUB_SOCKET = None


# --- Logging Setup ---
def setup_logging():
    """Configures logging to a dedicated file and to standard output."""
    log_file = '/var/log/rnse_control/crankshaft_can_features.log'
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


# --- State Management Class ---
class CrankshaftState:
    """A simple class to hold the runtime state of all features."""
    def __init__(self):
        # Receiver states
        self.light_status = 0
        self.last_daynight_mode = None
        self.time_synced_this_session = False
        self.last_kl15_status = 1
        self.last_kls_status = 1
        self.shutdown_trigger_timestamp = None
        self.shutdown_pending = False
        # Sender states (for timing)
        self.last_tv_send_time = 0
        # General state
        self.last_status_log_time = time.time()

    def log_periodic_status(self):
        """Logs the current state of all features to the logger."""
        auto_shutdown_config = FEATURES.get('auto_shutdown', {})
        if not auto_shutdown_config.get('enabled', False):
            shutdown_status = "Disabled"
        elif self.shutdown_pending and self.shutdown_trigger_timestamp:
            delay = CONFIG.get('shutdown_delay', 300)
            remaining = delay - (time.time() - self.shutdown_trigger_timestamp)
            trigger = auto_shutdown_config.get('trigger', 'N/A')
            shutdown_status = f"Pending ({remaining:.0f}s left, Trigger: {trigger})"
        else:
            shutdown_status = f"Armed (Trigger: {auto_shutdown_config.get('trigger', 'N/A')})"

        logger.info(
            f"Status | "
            f"Light: {'ON' if self.light_status else 'OFF'} | "
            f"Time Sync: {'OK' if self.time_synced_this_session else 'Pending'} | "
            f"Ignition: {'ON' if self.last_kl15_status else 'OFF'} | "
            f"Key: {'IN' if self.last_kls_status else 'PULLED'} | "
            f"Shutdown: {shutdown_status}"
        )
        self.last_status_log_time = time.time()


# --- Configuration Handling ---
def load_and_initialize_config(config_path='/home/pi/config.json'):
    """Loads, validates, and processes the entire JSON configuration."""
    global CONFIG, FEATURES
    logger.info(f"Loading configuration from {config_path}...")
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.critical(f"FATAL: Could not load or parse {config_path}: {e}")
        return False

    try:
        FEATURES = cfg.setdefault('features', {})
        FEATURES.setdefault('auto_shutdown', {'enabled': False})
        if FEATURES['auto_shutdown'].get('trigger') not in ['ignition_off', 'key_pulled']:
            FEATURES['auto_shutdown']['trigger'] = 'ignition_off'
        FEATURES.setdefault('tv_simulation', {'enabled': False})

        thresholds = cfg.setdefault('thresholds', {})
        can_ids = cfg.setdefault('can_ids', {})
        CONFIG = {
            'can_interface': cfg['can_interface'],
            'zmq_address': cfg['zmq']['publish_address'],
            'can_ids': {
                'light': int(can_ids.get('light_status', '0'), 16),
                'time': int(can_ids.get('time_data', '0'), 16),
                'power': int(can_ids.get('ignition_status', '0'), 16),
                'tv_presence': int(can_ids.get('tv_presence', '0'), 16),
            },
            'daynight_script_path': cfg.get('paths', {}).get('crankshaft_daynight_script'),
            'shutdown_delay': thresholds.get('shutdown_delay_ignition_off_seconds', 300),
            'car_time_zone': FEATURES.get('car_time_zone', 'UTC'),
        }
        logger.info("Configuration loaded successfully.")
        return True
    except (KeyError, ValueError) as e:
        logger.critical(f"FATAL: Config is missing a key or has an invalid value: {e}", exc_info=True)
        return False


# --- Core Logic Functions ---
def send_can_message(can_id, payload_hex):
    """Sends a CAN frame using the cansend command-line tool."""
    try:
        interface = CONFIG['can_interface']
        can_id_hex = f"{can_id:03X}"
        command = ['cansend', interface, f'{can_id_hex}#{payload_hex}']
        subprocess.run(command, check=True, capture_output=True)
        logger.debug(f"CAN Send OK: {' '.join(command)}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Failed to send CAN message via cansend: {e}")
        return False

def execute_system_command(command_list):
    """Executes a generic system command safely."""
    if not command_list: return False
    try:
        cmd_str = ' '.join(command_list)
        logger.info(f"Executing system command: {cmd_str}")
        subprocess.run(command_list, check=True, capture_output=True, text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Failed to execute command '{cmd_str}': {e}")
        return False

def initialize_zmq_subscriber():
    """Initializes and connects the ZeroMQ subscriber socket."""
    global ZMQ_CONTEXT, ZMQ_SUB_SOCKET
    try:
        logger.info(f"Connecting ZeroMQ subscriber to {CONFIG['zmq_address']}...")
        ZMQ_CONTEXT = zmq.Context.instance()
        ZMQ_SUB_SOCKET = ZMQ_CONTEXT.socket(zmq.SUB)
        ZMQ_SUB_SOCKET.set(zmq.RCVTIMEO, 1000)
        ZMQ_SUB_SOCKET.connect(CONFIG['zmq_address'])

        subscriptions = []
        if FEATURES.get('day_night_mode'): subscriptions.append(f"CAN_{CONFIG['can_ids']['light']:03X}")
        if FEATURES.get('time_sync'): subscriptions.append(f"CAN_{CONFIG['can_ids']['time']:03X}")
        if FEATURES.get('auto_shutdown', {}).get('enabled'): subscriptions.append(f"CAN_{CONFIG['can_ids']['power']:03X}")

        if not subscriptions:
            logger.warning("No receiving features enabled. Subscriber will not listen for any messages.")
        else:
            for topic in subscriptions:
                logger.info(f"Subscribing to topic: {topic}")
                ZMQ_SUB_SOCKET.setsockopt_string(zmq.SUBSCRIBE, topic)
        return True
    except zmq.ZMQError as e:
        logger.error(f"Failed to initialize ZeroMQ subscriber: {e}")
        return False


# --- Message Sending Logic ---
def send_tv_presence_message():
    """
    Sends the CAN message 0x602 to simulate a TV tuner for the RNS-E.
    Payload '0912300000000000' means: Tuner ON, 50Hz (PAL), Mode 18 (PAL B/G).
    """
    payload = "0912300000000000"
    send_can_message(CONFIG['can_ids']['tv_presence'], payload)


# --- Message Receiving Handlers ---
def handle_light_status_message(msg, state):
    """Processes light status messages to toggle day/night mode."""
    try:
        new_status = 1 if bytes.fromhex(msg['data_hex'])[1] > 0 else 0
        if new_status != state.light_status:
            state.light_status = new_status
            mode = "night" if new_status == 1 else "day"
            logger.info(f"Light status changed. Setting mode to '{mode}'.")
            if mode != state.last_daynight_mode:
                if execute_system_command([CONFIG['daynight_script_path'], "app", mode]):
                    state.last_daynight_mode = mode
    except (IndexError, ValueError) as e:
        logger.warning(f"Could not parse light status message: {e}")

def handle_time_data_message(msg, state):
    """Processes time data messages to sync system time once per session."""
    if state.time_synced_this_session or msg['dlc'] < 8: return
    try:
        data_hex = msg['data_hex']
        if not (int(data_hex[0:2], 16) >> 4) & 0x01: return # Skip if quality bit is not set

        car_dt = datetime(
            year=(int(data_hex[12:14], 16) * 100) + int(data_hex[14:16], 16),
            month=int(data_hex[10:12], 16),
            day=int(data_hex[8:10], 16),
            hour=int(data_hex[2:4], 16),
            minute=int(data_hex[4:6], 16),
            second=int(data_hex[6:8], 16)
        )
        pi_utc_dt = datetime.now(pytz.utc)
        car_utc_dt = pytz.timezone(CONFIG['car_time_zone']).localize(car_dt).astimezone(pytz.utc)

        if abs((car_utc_dt - pi_utc_dt).total_seconds()) > 60:
            date_str = car_utc_dt.strftime('%m%d%H%M%y.%S')
            logger.info(f"Car time differs. Syncing system time to: {car_utc_dt.isoformat()}")
            if execute_system_command(["sudo", "date", "-u", date_str]):
                state.time_synced_this_session = True
        else:
            state.time_synced_this_session = True
    except (ValueError, pytz.exceptions.UnknownTimeZoneError) as e:
        logger.error(f"Could not process time message: {e}")

def handle_power_status_message(msg, state):
    """Processes ignition/key status to manage auto-shutdown."""
    if msg['dlc'] < 1: return
    try:
        data_byte0 = int(msg['data_hex'][:2], 16)
        kls_status = data_byte0 & 0x01
        kl15_status = (data_byte0 >> 1) & 0x01

        kls_changed = kls_status != state.last_kls_status
        kl15_changed = kl15_status != state.last_kl15_status
        state.last_kls_status = kls_status
        state.last_kl15_status = kl15_status

        trigger_config = FEATURES.get('auto_shutdown', {}).get('trigger')
        trigger_event = False
        if trigger_config == 'ignition_off' and kl15_changed and kl15_status == 0:
            trigger_event = True
            logger.info("Ignition OFF detected.")
        elif trigger_config == 'key_pulled' and kls_changed and kls_status == 0:
            trigger_event = True
            logger.info("Key PULLED detected.")

        if trigger_event and not state.shutdown_pending:
            logger.info(f"Starting {CONFIG['shutdown_delay']}s shutdown timer.")
            state.shutdown_pending = True
            state.shutdown_trigger_timestamp = time.time()

        if kl15_changed and kl15_status == 1 and state.shutdown_pending:
            logger.info("Ignition ON detected. Cancelling shutdown.")
            state.shutdown_pending = False
            state.shutdown_trigger_timestamp = None
    except (IndexError, ValueError) as e:
        logger.warning(f"Could not parse power status message: {e}")


# --- Signal Handling and Main Loop ---
def setup_signal_handlers():
    """Sets up handlers for graceful shutdown and config reload."""
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGHUP, reload_config_handler)
    logger.info("Signal handlers for SIGINT, SIGTERM, and SIGHUP are set.")

def shutdown_handler(signum, frame):
    """Flags the application to exit the main loop."""
    global RUNNING
    if RUNNING:
        logger.info(f"Shutdown signal {signum} received. Cleaning up...")
        RUNNING = False

def reload_config_handler(signum, frame):
    """Flags the application to reload its configuration."""
    global RELOAD_CONFIG
    logger.info("SIGHUP signal received. Flagging for configuration reload.")
    RELOAD_CONFIG = True

def main():
    """The main application entry point and loop."""
    global RELOAD_CONFIG
    if not load_and_initialize_config():
        sys.exit(1)

    setup_signal_handlers()
    state = CrankshaftState()

    if not initialize_zmq_subscriber():
        sys.exit(1)

    logger.info("Crankshaft CAN features service started successfully.")

    while RUNNING:
        try:
            if RELOAD_CONFIG:
                logger.info("Reloading configuration and re-initializing...")
                if ZMQ_SUB_SOCKET and not ZMQ_SUB_SOCKET.closed: ZMQ_SUB_SOCKET.close()
                if load_and_initialize_config(): initialize_zmq_subscriber()
                RELOAD_CONFIG = False
                logger.info("Configuration reload complete.")

            now = time.time()
            if FEATURES.get('tv_simulation', {}).get('enabled') and (now - state.last_tv_send_time > 0.5):
                send_tv_presence_message()
                state.last_tv_send_time = now

            if ZMQ_SUB_SOCKET:
                try:
                    topic_bytes, msg_bytes = ZMQ_SUB_SOCKET.recv_multipart(flags=zmq.NOBLOCK)
                    msg_dict = json.loads(msg_bytes.decode('utf-8'))
                    can_id = msg_dict.get('arbitration_id')

                    if can_id == CONFIG['can_ids']['light']: handle_light_status_message(msg_dict, state)
                    elif can_id == CONFIG['can_ids']['time']: handle_time_data_message(msg_dict, state)
                    elif can_id == CONFIG['can_ids']['power']: handle_power_status_message(msg_dict, state)
                except zmq.Again:
                    pass

            if state.shutdown_pending and (time.time() - state.shutdown_trigger_timestamp >= CONFIG['shutdown_delay']):
                logger.info("Shutdown delay reached. Shutting down system NOW.")
                shutdown_command = CONFIG.get('shutdown_command', ["sudo", "shutdown", "-h", "now"])
                if execute_system_command(shutdown_command):
                    break
                else:
                    logger.error("Shutdown command failed! Aborting.")
                    state.shutdown_pending = False
            
            if now - state.last_status_log_time > 60:
                state.log_periodic_status()

            time.sleep(0.1)

        except Exception as e:
            logger.critical("An unexpected critical error occurred in the main loop.", exc_info=True)
            break

    logger.info("Main loop terminated. Closing resources.")
    if ZMQ_SUB_SOCKET and not ZMQ_SUB_SOCKET.closed: ZMQ_SUB_SOCKET.close()
    if ZMQ_CONTEXT and not ZMQ_CONTEXT.closed: ZMQ_CONTEXT.term()
    logger.info("crankshaft_can_features.py has finished.")


if __name__ == '__main__':
    main()
