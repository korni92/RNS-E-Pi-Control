#!/usr/bin/env python3
#
# crankshaft_can_features.py
#
# This service acts as an advanced bridge between the car's CAN bus and the
# Raspberry Pi running Crankshaft. It both listens for specific CAN messages
# to trigger system actions and periodically sends messages to simulate devices.
#
# Designed to run as a robust, long-running systemd service.
#
# Features:
# - Day/Night Mode synchronization based on car's light status.
# - Time synchronization from car's CAN bus to Raspberry Pi's system clock.
# - Auto-shutdown based on ignition or key status.
# - TV tuner simulation for RNS-E.
# - Extensible message handling.

import zmq
import json
import time
import subprocess
import logging
import signal
import sys
from datetime import datetime
import pytz
from typing import Optional, List, Tuple, Dict, Any 

# --- Version ---
VERSION = "1.0.0" # Current version of the script

# --- Global State ---
RUNNING = True
RELOAD_CONFIG = False
CONFIG: Dict[str, Any] = {} # Use Any as type is complex
FEATURES: Dict[str, Any] = {} # Use Any as type is complex
ZMQ_CONTEXT: Optional[zmq.Context] = None
ZMQ_SUB_SOCKET: Optional[zmq.Socket] = None


# --- Logging Setup ---
def setup_logging():
    """Configures logging to a dedicated file and to standard output."""
    log_file = '/var/log/rnse_control/crankshaft_can_features.log'
    # Ensure log directory exists
    subprocess.run(['sudo', 'mkdir', '-p', '/var/log/rnse_control'], check=False)

    logging.basicConfig(
        level=logging.INFO, # Default to INFO for production, use logging.DEBUG for troubleshooting
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()
logger.info(f"Starting Crankshaft CAN Features service v{VERSION}...")


# --- Helper function for BCD conversion ---
def hex_to_bcd(hex_str: str) -> int:
    """
    Converts a 2-character hexadecimal string (e.g., '13', '40')
    representing a Binary-Coded Decimal (BCD) value to its decimal integer.
    Example: '13' (hex BCD) -> 13 (decimal), '40' (hex BCD) -> 40 (decimal).
    """
    if not (isinstance(hex_str, str) and len(hex_str) == 2 and hex_str.isalnum()):
        raise ValueError(f"Input must be a 2-char hex string, got '{hex_str}'")
    
    # This assumes BCD where each hex digit directly maps to a decimal digit.
    # E.g., '40' hex is 40 decimal, not 64.
    return int(hex_str[0]) * 10 + int(hex_str[1])


# --- State Management Class ---
class CrankshaftState:
    """A simple class to hold the runtime state of all features."""
    def __init__(self):
        # Receiver states
        self.light_status: int = 0 # 0 for day, 1 for night
        self.last_daynight_mode: Optional[str] = None # 'day' or 'night'
        self.last_mode_change_time: float = 0.0 # Unix timestamp of last day/night mode change
        self.last_time_sync_attempt_time: float = 0.0 # Unix timestamp of last time a CAN time message was processed
        self.last_kl15_status: int = 1 # Ignition status (1=ON, 0=OFF)
        self.last_kls_status: int = 1 # Key in lock sensor status (1=IN, 0=PULLED)
        self.shutdown_trigger_timestamp: Optional[float] = None # Timestamp when shutdown sequence began
        self.shutdown_pending: bool = False # True if shutdown process is initiated and waiting for delay
        # Sender states (for timing)
        self.last_tv_send_time: float = 0.0 # Unix timestamp of last TV presence message sent
        # General state
        self.last_status_log_time: float = time.time() # Unix timestamp of last periodic status log

    def log_periodic_status(self):
        """Logs the current state of all features to the logger."""
        auto_shutdown_config = FEATURES.get('auto_shutdown', {})
        if not auto_shutdown_config.get('enabled', False):
            shutdown_status = "Disabled"
        elif self.shutdown_pending and self.shutdown_trigger_timestamp is not None:
            delay = CONFIG.get('shutdown_delay', 300) 
            remaining = delay - (time.time() - self.shutdown_trigger_timestamp)
            trigger = auto_shutdown_config.get('trigger', 'N/A')
            shutdown_status = f"Pending ({remaining:.0f}s left, Trigger: {trigger})"
        else:
            trigger = auto_shutdown_config.get('trigger', 'N/A')
            shutdown_status = f"Armed (Trigger: {trigger})"

        # Determine time sync status based on if time messages are being processed recently
        time_sync_status = "Pending"
        # If a time message has been processed within the last 5 minutes, consider active.
        # This doesn't guarantee actual time sync, but indicates the feature is alive.
        if time.time() - self.last_time_sync_attempt_time < 300: 
            time_sync_status = "OK (Active)" 
            
        logger.info(
            f"Status | "
            f"Light: {'ON' if self.light_status else 'OFF'} | "
            f"Time Sync: {time_sync_status} | "
            f"Ignition: {'ON' if self.last_kl15_status else 'OFF'} | "
            f"Key: {'IN' if self.last_kls_status else 'PULLED'} | "
            f"Shutdown: {shutdown_status}"
        )
        self.last_status_log_time = time.time()


# --- Configuration Handling ---
def load_and_initialize_config(config_path: str = '/home/pi/config.json') -> bool:
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
        # Defaulting features and ensuring required sub-keys exist
        FEATURES = cfg.setdefault('features', {})
        FEATURES.setdefault('day_night_mode', False)
        # time_sync is now an object in config.json
        FEATURES.setdefault('time_sync', {'enabled': False, 'data_format': 'new_logic'})
        FEATURES.setdefault('auto_shutdown', {'enabled': False, 'trigger': 'ignition_off'})
        # Ensure trigger is valid, default if not
        if FEATURES['auto_shutdown'].get('trigger') not in ['ignition_off', 'key_pulled']:
            FEATURES['auto_shutdown']['trigger'] = 'ignition_off'
        FEATURES.setdefault('tv_simulation', {'enabled': False})
        FEATURES.setdefault('light_sensor_installed', False)
        FEATURES.setdefault('car_time_zone', 'UTC')
        FEATURES.setdefault('debug_mode', False)

        # Defaulting thresholds
        thresholds = cfg.setdefault('thresholds', {})
        thresholds.setdefault('cooldown_period', 0.2)
        thresholds.setdefault('long_press_message_count', 5)
        thresholds.setdefault('extended_long_press_message_count', 30)
        thresholds.setdefault('shutdown_delay_ignition_off_seconds', 300)
        thresholds.setdefault('time_sync_threshold_minutes', 1.0)
        thresholds.setdefault('daynight_cooldown_seconds', 10)

        # Defaulting CAN IDs
        can_ids = cfg.setdefault('can_ids', {})
        can_ids.setdefault('light_status', '0x635')
        can_ids.setdefault('time_data', '0x623')
        can_ids.setdefault('ignition_status', '0x2C3')
        can_ids.setdefault('tv_presence', '0x602')

        # Populate global CONFIG dictionary
        CONFIG = {
            'can_interface': cfg.get('can_interface', 'can0'),
            'zmq_address': cfg.get('zmq', {}).get('publish_address', 'ipc:///run/rnse_control/can_stream.ipc'),
            'can_ids': {
                'light': int(can_ids['light_status'], 16),
                'time': int(can_ids['time_data'], 16),
                'power': int(can_ids['ignition_status'], 16),
                'tv_presence': int(can_ids['tv_presence'], 16),
            },
            # time_data_format is now read from FEATURES.time_sync
            'time_data_format': FEATURES['time_sync']['data_format'], 
            'daynight_script_path': cfg.get('paths', {}).get('crankshaft_daynight_script', '/opt/crankshaft/service_daynight.sh'),
            'shutdown_delay': thresholds['shutdown_delay_ignition_off_seconds'],
            'daynight_cooldown_seconds': thresholds['daynight_cooldown_seconds'],
            'car_time_zone': FEATURES['car_time_zone'],
            'time_sync_threshold_seconds': thresholds['time_sync_threshold_minutes'] * 60 # Convert to seconds
        }
        logger.info("Configuration loaded successfully.")
        # Optionally set logging level from config.debug_mode
        if FEATURES.get('debug_mode', False):
            logger.setLevel(logging.DEBUG)
            logger.debug("Debug mode enabled from config.")
        else:
            logger.setLevel(logging.INFO)
            
        return True
    except (KeyError, ValueError) as e:
        logger.critical(f"FATAL: Config is missing a key or has an invalid value: {e}", exc_info=True)
        return False


# --- Core Logic Functions ---
def send_can_message(can_id: int, payload_hex: str) -> bool:
    """Sends a CAN frame using the cansend command-line tool."""
    try:
        interface = CONFIG['can_interface']
        can_id_hex = f"{can_id:03X}" # Format CAN ID to 3-digit hex (e.g., 623)
        command = ['cansend', interface, f'{can_id_hex}#{payload_hex}']
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        logger.debug(f"CAN Send OK: {' '.join(command)}")
        if result.stdout:
            logger.debug(f"cansend stdout: {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to send CAN message via cansend (Exit code: {e.returncode}): {e.stderr.strip()}")
        return False
    except FileNotFoundError:
        logger.error(f"cansend command not found. Is can-utils installed and in PATH? Tried: {' '.join(command)}")
        return False

def execute_system_command(command_list: List[str]) -> bool: # type hint list of strings
    """Executes a generic system command safely."""
    if not command_list:
        logger.warning("Attempted to execute an empty command list.")
        return False
    cmd_str = ' '.join(command_list) # For logging
    try:
        logger.info(f"Executing system command: {cmd_str}")
        result = subprocess.run(command_list, check=True, capture_output=True, text=True)
        if result.stdout:
            logger.debug(f"Command stdout: {result.stdout.strip()}")
        if result.stderr:
            logger.debug(f"Command stderr: {result.stderr.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to execute command '{cmd_str}' (Exit code: {e.returncode}): {e.stderr.strip()}")
        return False
    except FileNotFoundError:
        logger.error(f"Command '{command_list[0]}' not found. Is it installed and in PATH? Tried: {cmd_str}")
        return False


def initialize_zmq_subscriber() -> bool:
    """Initializes and connects the ZeroMQ subscriber socket."""
    global ZMQ_CONTEXT, ZMQ_SUB_SOCKET
    try:
        logger.info(f"Connecting ZeroMQ subscriber to {CONFIG['zmq_address']}...")
        ZMQ_CONTEXT = zmq.Context.instance()
        ZMQ_SUB_SOCKET = ZMQ_CONTEXT.socket(zmq.SUB)
        ZMQ_SUB_SOCKET.set(zmq.RCVTIMEO, 1000) # Set a timeout for recv_multipart
        ZMQ_SUB_SOCKET.connect(CONFIG['zmq_address'])

        subscriptions: List[str] = [] # Type hint list of strings
        if FEATURES.get('day_night_mode'): subscriptions.append(f"CAN_{CONFIG['can_ids']['light']:03X}")
        # Check FEATURES.time_sync.enabled for subscription
        if FEATURES.get('time_sync', {}).get('enabled', False): subscriptions.append(f"CAN_{CONFIG['can_ids']['time']:03X}")
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
    Sends the CAN message to simulate a TV tuner for the RNS-E.
    This message typically needs to be sent periodically (e.g., every 0.5s)
    to maintain the TV tuner's presence in the RNS-E menu.
    """
    payload = "0912300000000000" # Common payload for TV tuner presence
    send_can_message(CONFIG['can_ids']['tv_presence'], payload)


# --- Message Receiving Handlers ---
def handle_light_status_message(msg: Dict[str, Any], state: CrankshaftState):
    """
    Processes light status messages (CAN ID: light_status) to toggle day/night mode
    for the Crankshaft application, with a configurable cooldown period.
    """
    if not FEATURES.get('day_night_mode', False):
        return # Feature disabled

    try:
        # Assuming byte at index 1 indicates light status (0=OFF/Day, >0=ON/Night)
        # Adjust index if your car's message differs.
        new_status = 1 if bytes.fromhex(msg['data_hex'])[1] > 0 else 0
        
        if new_status != state.light_status:
            logger.debug(f"Light status changed from {state.light_status} to {new_status}. Data: {msg['data_hex']}")
            state.light_status = new_status
            mode = "night" if new_status == 1 else "day"
            
            cooldown = CONFIG.get('daynight_cooldown_seconds', 10)
            # Prevent rapid toggling if mode already matches or within cooldown
            if mode != state.last_daynight_mode and (time.time() - state.last_mode_change_time > cooldown):
                logger.info(f"Light status changed. Setting mode to '{mode}'. Starting {cooldown}s cooldown.")
                script_path = CONFIG.get('daynight_script_path')
                if script_path and execute_system_command([script_path, "app", mode]):
                    state.last_daynight_mode = mode
                    state.last_mode_change_time = time.time()
                else:
                    logger.warning(f"Day/night script not configured or failed to execute for mode '{mode}'. Path: {script_path}")
            else:
                logger.debug(f"Light status changed to '{mode}', but change is suppressed by cooldown ({time.time() - state.last_mode_change_time:.1f}s left) or no-op (mode already {state.last_daynight_mode}).")
                
    except (IndexError, ValueError) as e:
        logger.warning(f"Could not parse light status message (data_hex: {msg.get('data_hex', 'N/A')}): {e}")

def handle_time_data_message(msg: Dict[str, Any], state: CrankshaftState):
    """
    Processes time data messages (CAN ID: time_data) to synchronize the
    Raspberry Pi's system clock with the car's clock.
    Supports 'old_logic' (BCD) and 'new_logic' (standard hex) interpretations.
    """
    # Check if time sync feature is enabled from its new location
    if not FEATURES.get('time_sync', {}).get('enabled', False):
        logger.debug("Time sync feature is disabled in configuration.")
        return
    
    # Ensure message has enough data bytes (8 bytes expected for time data)
    if msg.get('dlc', 0) < 8:
        logger.debug(f"Time data message too short (DLC: {msg.get('dlc', 'N/A')}). Skipping sync. Data: {msg.get('data_hex', 'N/A')}")
        return
    
    time_format = CONFIG['time_data_format'] # Now directly from CONFIG (which gets it from FEATURES.time_sync)
    data_hex = msg['data_hex'] # Full hex string of the data payload

    try:
        # The 'valid bit' check was removed as it caused issues and is likely not
        # universally applicable across car models. The logic now assumes data is valid.
        # if not (int(data_hex[0:2], 16) >> 4) & 0x01:
        #     logger.debug("Time data message received, but 'valid' bit not set. Skipping sync.")
        #     return

        year, month, day, hour, minute, second = 0, 0, 0, 0, 0, 0 # Initialize variables

        if time_format == 'old_logic':
            # This logic is based on: 0x623 00 11 22 33 04 05 20 26 for 11:22:33 AM on 04. May 2026
            # It uses BCD (Binary Coded Decimal) for time/date fields and string concatenation for year.
            
            second = hex_to_bcd(data_hex[6:8])
            minute = hex_to_bcd(data_hex[4:6])
            hour = hex_to_bcd(data_hex[2:4])
            day = hex_to_bcd(data_hex[8:10])
            month = hex_to_bcd(data_hex[10:12])
            
            year = int(data_hex[12:14] + data_hex[14:16])

        elif time_format == 'new_logic':
            # This logic is based on: 0x623 00 13 21 36 10 12 20 34 for 13:21:36 on 10. Dec 2034
            # It uses standard hexadecimal to decimal conversion for all fields.
            
            second = int(data_hex[6:8], 16)
            minute = int(data_hex[4:6], 16)
            hour = int(data_hex[2:4], 16)
            day = int(data_hex[8:10], 16)
            month = int(data_hex[10:12], 16)
            year = (int(data_hex[12:14], 16) * 100) + int(data_hex[14:16], 16) 

        else:
            logger.warning(f"Unknown time_data_format: '{time_format}'. Skipping time sync.")
            return

        # Update last_time_sync_attempt_time as soon as data is successfully parsed
        state.last_time_sync_attempt_time = time.time() 

        car_dt = datetime(year=year, month=month, day=day, hour=hour, minute=minute, second=second)
        logger.debug(f"Parsed car time ({time_format} format): {car_dt.isoformat()}")

        pi_utc_dt = datetime.now(pytz.utc)
        car_utc_dt = pytz.timezone(CONFIG['car_time_zone']).localize(car_dt).astimezone(pytz.utc)

        logger.debug(f"Car UTC time: {car_utc_dt.isoformat()}, Pi UTC time: {pi_utc_dt.isoformat()}")

        time_diff_seconds = abs((car_utc_dt - pi_utc_dt).total_seconds())
        sync_threshold = CONFIG.get('time_sync_threshold_seconds', 60.0)

        # Only synchronize if the time difference exceeds the configured threshold
        if time_diff_seconds > sync_threshold:
            date_str = car_utc_dt.strftime('%m%d%H%M%Y.%S')
            logger.info(f"Car time differs by {time_diff_seconds:.1f}s (>{sync_threshold}s threshold). Syncing system time to: {car_utc_dt.isoformat()}")
            
            command = ["sudo", "date", "-u", date_str]
            logger.info(f"Executing system command: {' '.join(command)}")

            if execute_system_command(command):
                logger.info("System time synced successfully.")
            else:
                logger.error("Failed to execute time sync command 'sudo date -u'. Check permissions or command.")
        else:
            logger.debug(f"Car time is within sync threshold ({time_diff_seconds:.1f}s <= {sync_threshold}s). No time sync needed.")
            
    except (IndexError, ValueError) as e:
        logger.warning(f"Could not parse time message (data_hex: {msg.get('data_hex', 'N/A')}, format: {time_format}): {e}", exc_info=True)
    except pytz.exceptions.UnknownTimeZoneError:
        logger.error(f"Unknown time zone configured: {CONFIG['car_time_zone']}. Please check your config.json.", exc_info=True)
    except Exception as e:
        logger.critical(f"An unexpected error occurred in handle_time_data_message: {e}", exc_info=True)


def handle_power_status_message(msg: Dict[str, Any], state: CrankshaftState):
    """
    Processes ignition/key status messages (CAN ID: ignition_status) to manage
    the auto-shutdown feature of the Raspberry Pi.
    """
    if msg.get('dlc', 0) < 1: # Ensure at least one byte for relevant status bits
        logger.debug(f"Power status message too short (DLC: {msg.get('dlc', 'N/A')}). Skipping.")
        return
    try:
        data_byte0 = int(msg['data_hex'][:2], 16)
        kls_status = data_byte0 & 0x01       # Bit 0 for KLS (Key in Lock Sensor) - 1=IN, 0=PULLED
        kl15_status = (data_byte0 >> 1) & 0x01 # Bit 1 for KL15 (Ignition ON/OFF) - 1=ON, 0=OFF

        kls_changed = kls_status != state.last_kls_status
        kl15_changed = kl15_status != state.last_kl15_status
        state.last_kls_status = kls_status
        state.last_kl15_status = kl15_status

        auto_shutdown_enabled = FEATURES.get('auto_shutdown', {}).get('enabled', False)
        if not auto_shutdown_enabled:
            logger.debug("Auto-shutdown feature is disabled.")
            return

        trigger_config = FEATURES.get('auto_shutdown', {}).get('trigger')
        trigger_event = False
        
        # Check for ignition off event (KL15 goes from 1 to 0)
        if trigger_config == 'ignition_off' and kl15_changed and kl15_status == 0:
            trigger_event = True
            logger.info("Ignition OFF detected. Starting shutdown timer.")
        # Check for key pulled event (KLS goes from 1 to 0)
        elif trigger_config == 'key_pulled' and kls_changed and kls_status == 0:
            trigger_event = True
            logger.info("Key PULLED detected. Starting shutdown timer.")

        if trigger_event and not state.shutdown_pending:
            logger.info(f"Starting {CONFIG['shutdown_delay']}s shutdown timer due to '{trigger_config}' trigger.")
            state.shutdown_pending = True
            state.shutdown_trigger_timestamp = time.time()
        # If ignition or key comes back ON/IN while shutdown is pending, cancel it
        elif state.shutdown_pending:
            if (trigger_config == 'ignition_off' and kl15_changed and kl15_status == 1) or \
               (trigger_config == 'key_pulled' and kls_changed and kls_status == 1):
                logger.info("Ignition ON or Key INSERTED detected. Cancelling pending shutdown.")
                state.shutdown_pending = False
                state.shutdown_trigger_timestamp = None

    except (IndexError, ValueError) as e:
        logger.warning(f"Could not parse power status message (data_hex: {msg.get('data_hex', 'N/A')}): {e}")


# --- Signal Handling and Main Loop ---
def setup_signal_handlers():
    """Sets up handlers for graceful shutdown and config reload."""
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGHUP, reload_config_handler) # For config reload

def shutdown_handler(signum: int, frame):
    """Flags the application to exit the main loop upon receiving a shutdown signal."""
    global RUNNING
    if RUNNING: # Prevent multiple log entries if multiple signals are received rapidly
        logger.info(f"Shutdown signal {signum} received. Initiating graceful shutdown...")
        RUNNING = False

def reload_config_handler(signum: int, frame):
    """Flags the application to reload its configuration upon SIGHUP signal."""
    global RELOAD_CONFIG
    logger.info("SIGHUP signal received. Flagging for configuration reload.")
    RELOAD_CONFIG = True

def main():
    """The main application entry point and loop."""
    global RUNNING, RELOAD_CONFIG # Declare global variables used in this scope

    if not load_and_initialize_config():
        sys.exit(1) # Exit if initial configuration fails

    setup_signal_handlers()
    state = CrankshaftState()

    # Initial setup for day/night mode to ensure a consistent starting state
    logger.info("Ensuring system starts in day mode by removing potential stale night mode flag.")
    try:
        # This helps ensure the system GUI/themes start in a known state (day mode)
        # by removing a file that some day/night scripts might use as a flag.
        subprocess.run(['sudo', 'rm', '-f', '/tmp/night_mode_enabled'], check=True, capture_output=True)
        state.last_daynight_mode = 'day' # Initialize state to 'day'
        state.light_status = 0 # Initialize light status to OFF (day)
    except Exception as e:
        logger.error(f"Could not remove /tmp/night_mode_enabled on startup, continuing anyway: {e}")

    if not initialize_zmq_subscriber():
        sys.exit(1) # Exit if ZeroMQ subscriber cannot be initialized

    logger.info("Crankshaft CAN features service started successfully. Entering main loop.")

    while RUNNING: # Main application loop
        try:
            if RELOAD_CONFIG:
                logger.info("Reloading configuration and re-initializing ZeroMQ subscriber...")
                if ZMQ_SUB_SOCKET and not ZMQ_SUB_SOCKET.closed:
                    ZMQ_SUB_SOCKET.close() # Close old socket before re-initialization
                if load_and_initialize_config(): # Reload config
                    if not initialize_zmq_subscriber(): # Re-initialize ZMQ with new config
                        logger.error("Failed to re-initialize ZMQ subscriber after config reload. Exiting.")
                        RUNNING = False # Stop if ZMQ cannot be re-established
                RELOAD_CONFIG = False # Reset flag
                logger.info("Configuration reload complete.")

            now = time.time()
            # Handle TV simulation (sending periodic messages)
            if FEATURES.get('tv_simulation', {}).get('enabled') and (now - state.last_tv_send_time > 0.5):
                send_tv_presence_message()
                state.last_tv_send_time = now

            # Process incoming ZeroMQ messages (non-blocking)
            if ZMQ_SUB_SOCKET:
                try:
                    topic_bytes, msg_bytes = ZMQ_SUB_SOCKET.recv_multipart(flags=zmq.NOBLOCK)
                    msg_dict = json.loads(msg_bytes.decode('utf-8'))
                    can_id = msg_dict.get('arbitration_id')

                    # Dispatch received CAN messages to appropriate handlers
                    if can_id == CONFIG['can_ids'].get('light') and FEATURES.get('day_night_mode', False): # Added feature check
                        handle_light_status_message(msg_dict, state)
                    # Check FEATURES.time_sync.enabled for handling
                    elif can_id == CONFIG['can_ids'].get('time') and FEATURES.get('time_sync', {}).get('enabled', False):
                        handle_time_data_message(msg_dict, state)
                    elif can_id == CONFIG['can_ids'].get('power') and FEATURES.get('auto_shutdown', {}).get('enabled', False): # Added feature check
                        handle_power_status_message(msg_dict, state)
                    else:
                        logger.debug(f"Received unhandled CAN message: ID={can_id:03X}. Data: {msg_dict.get('data_hex', 'N/A')}")
            
                except zmq.Again:
                    # No message received within the RCVTIMEO timeout, continue loop
                    pass
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode ZMQ message as JSON: {e}. Message bytes: {msg_bytes}")
                except Exception as e:
                    logger.error(f"Error processing ZMQ message: {e}", exc_info=True)


            # Check for auto-shutdown trigger and execute if delay reached
            if state.shutdown_pending and (time.time() - state.shutdown_trigger_timestamp >= CONFIG['shutdown_delay']):
                logger.info("Shutdown delay reached. Shutting down system NOW.")
                shutdown_command = CONFIG.get('shutdown_command', ["sudo", "shutdown", "-h", "now"])
                if execute_system_command(shutdown_command):
                    break # Exit main loop after initiating shutdown
                else:
                    logger.error("Shutdown command failed! Aborting shutdown process and continuing service.")
                    state.shutdown_pending = False # Reset pending status if command fails
            
            # Periodically log current service status
            if now - state.last_status_log_time > 60: # Log every 60 seconds
                state.log_periodic_status()

            time.sleep(0.1) # Small delay to prevent busy-waiting and reduce CPU usage

        except Exception as e:
            logger.critical("An unexpected critical error occurred in main loop. Exiting.", exc_info=True)
            RUNNING = False # Terminate the loop and allow cleanup

    # Cleanup resources upon loop termination
    logger.info("Main loop terminated. Closing ZeroMQ resources.")
    if ZMQ_SUB_SOCKET and not ZMQ_SUB_SOCKET.closed: ZMQ_SUB_SOCKET.close() # Close ZMQ socket
    if ZMQ_CONTEXT and not ZMQ_CONTEXT.closed: ZMQ_CONTEXT.term() # Terminate ZMQ context
    logger.info("Crankshaft CAN features service has finished.")


if __name__ == '__main__':
    main()
