#!/usr/bin/env python3
#
# can_base_function.py
#
# This service provides the base CAN bus functionality required for integrating
# a Raspberry Pi with a head unit like an RNS-E. Its primary purpose is to
# send a periodic "TV Tuner" simulation message when enabled in the config.
#
# It sends messages via ZeroMQ to the central can_handler.py service.
#
# Version: 1.1.0 (with feature flag support)
#

import zmq
import json
import time
import logging
import signal
import sys
from typing import Optional, Dict, Any

# --- Global State ---
RUNNING = True
RELOAD_CONFIG = False
CONFIG: Dict[str, Any] = {}
FEATURES: Dict[str, Any] = {} # MODIFIED: Added FEATURES global
ZMQ_CONTEXT: Optional[zmq.Context] = None
ZMQ_PUSH_SOCKET: Optional[zmq.Socket] = None

# --- Logging Setup ---
def setup_logging():
    """Configures logging to a dedicated file and to standard output."""
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

# --- Configuration Handling ---
def load_and_initialize_config(config_path='/home/pi/config.json') -> bool:
    """Loads the necessary configuration from the central JSON file."""
    global CONFIG, FEATURES
    logger.info(f"Loading configuration from {config_path}...")
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.critical(f"FATAL: Could not load or parse {config_path}: {e}")
        return False

    try:
        # MODIFIED: Load the features section to check if TV simulation is enabled
        FEATURES = cfg.setdefault('features', {})
        FEATURES.setdefault('tv_simulation', {'enabled': False})

        zmq_config = cfg.get('zmq', {})
        can_ids = cfg.get('can_ids', {})
        
        CONFIG = {
            'zmq_send_address': zmq_config.get('send_address'),
            'tv_presence_id': int(can_ids.get('tv_presence', '0x602'), 16),
        }
        
        if not CONFIG['zmq_send_address']:
            raise KeyError("'send_address' not found in 'zmq' section of config.json")
            
        logger.info("Configuration for base functions loaded successfully.")
        return True
    except (KeyError, ValueError) as e:
        logger.critical(f"FATAL: Config is missing a key or has an invalid value: {e}", exc_info=True)
        return False

# --- Core Logic ---
def initialize_zmq_sender() -> bool:
    """Initializes the ZeroMQ PUSH socket for sending CAN messages."""
    global ZMQ_CONTEXT, ZMQ_PUSH_SOCKET
    try:
        logger.info(f"Connecting ZeroMQ PUSH socket to {CONFIG['zmq_send_address']}...")
        ZMQ_CONTEXT = zmq.Context.instance()
        ZMQ_PUSH_SOCKET = ZMQ_CONTEXT.socket(zmq.PUSH)
        ZMQ_PUSH_SOCKET.connect(CONFIG['zmq_send_address'])
        return True
    except zmq.ZMQError as e:
        logger.error(f"Failed to connect ZMQ PUSH socket: {e}")
        return False

def send_can_message(can_id: int, payload_hex: str) -> bool:
    """Queues a CAN message to be sent by can_handler.py via ZeroMQ."""
    if not ZMQ_PUSH_SOCKET:
        return False
    try:
        ZMQ_PUSH_SOCKET.send_multipart([
            str(can_id).encode('utf-8'),
            payload_hex.encode('utf-8')
        ])
        logger.debug(f"Queued CAN message: ID={can_id:03X}, Payload={payload_hex}")
        return True
    except zmq.ZMQError as e:
        logger.error(f"Failed to queue CAN message via ZMQ: {e}")
        return False

# --- Signal Handling and Main Loop ---
def setup_signal_handlers():
    """Sets up handlers for graceful shutdown and config reload."""
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGHUP, reload_config_handler) # ADDED: For live config changes
    logger.info("Signal handlers for SIGINT, SIGTERM, and SIGHUP are set.")

def shutdown_handler(signum, frame):
    """Flags the application to exit the main loop."""
    global RUNNING
    if RUNNING:
        logger.info(f"Shutdown signal {signum} received. Exiting...")
        RUNNING = False

def reload_config_handler(signum, frame):
    """Flags the application to reload its configuration."""
    global RELOAD_CONFIG
    logger.info("SIGHUP signal received. Flagging for configuration reload.")
    RELOAD_CONFIG = True

def main():
    """The main application entry point and loop."""
    global RELOAD_CONFIG
    logger.info("Starting CAN Base Function service...")
    if not load_and_initialize_config():
        sys.exit(1)

    if not initialize_zmq_sender():
        sys.exit(1)

    setup_signal_handlers()
    logger.info("--- Service is running ---")
    
    last_send_time = 0
    tv_presence_payload = "0912300000000000"

    while RUNNING:
        try:
            # MODIFIED: Check for config reload requests
            if RELOAD_CONFIG:
                load_and_initialize_config()
                RELOAD_CONFIG = False
                logger.info("Configuration reloaded.")

            now = time.time()
            
            # MODIFIED: Check the feature flag before sending
            is_tv_sim_enabled = FEATURES.get('tv_simulation', {}).get('enabled', False)

            if is_tv_sim_enabled and (now - last_send_time > 0.5):
                send_can_message(CONFIG['tv_presence_id'], tv_presence_payload)
                last_send_time = now

            time.sleep(0.1)

        except Exception as e:
            logger.critical("An unexpected critical error in the main loop.", exc_info=True)
            break

    logger.info("Main loop terminated. Closing ZeroMQ resources.")
    if ZMQ_PUSH_SOCKET and not ZMQ_PUSH_SOCKET.closed:
        ZMQ_PUSH_SOCKET.close()
    if ZMQ_CONTEXT and not ZMQ_CONTEXT.closed:
        ZMQ_CONTEXT.term()
    logger.info("CAN Base Function service has finished.")

if __name__ == '__main__':
    main()
