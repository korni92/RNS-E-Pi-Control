#!/usr/bin/env python3
#
# can_handler.py (Refactored)
#
# This service acts as the central CAN bus gateway.
# - Reads all messages from the CAN interface and broadcasts them via a ZMQ PUB socket.
# - Listens on a ZMQ PULL socket for outgoing messages and sends them to the CAN bus.
# This centralizes all hardware interaction for efficiency and robustness.
#

import can
import zmq
import time
import logging
import signal
import sys
import json

# --- Global State ---
RUNNING = True
RELOAD_CONFIG = False
CONFIG = {}
CAN_BUS = None
ZMQ_CONTEXT = None
ZMQ_PUB_SOCKET = None
ZMQ_PULL_SOCKET = None # NEW: Socket to receive messages to be sent

# --- Logging Setup ---
def setup_logging():
    """Configures logging to file and stdout for systemd compatibility."""
    log_file = '/var/log/rnse_control/can_handler.log'
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
def load_and_initialize_config(config_path='/home/pi/config.json'):
    """
    Loads the JSON configuration and populates the global CONFIG dictionary.
    Returns True on success, False on failure.
    """
    global CONFIG
    logger.info(f"Attempting to load configuration from {config_path}...")
    try:
        with open(config_path, 'r') as f:
            config_data = json.load(f)

        # MODIFIED: Added ZMQ send address to config
        CONFIG = {
            'can_interface': config_data['can_interface'],
            'zmq_publish_address': config_data['zmq']['publish_address'],
            'zmq_send_address': config_data['zmq']['send_address']
        }
        logger.info("Configuration loaded successfully.")
        return True
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.critical(f"FATAL: Could not load or parse config.json: {e}")
        return False


# --- Initialization and Teardown ---
def initialize_can_bus(retries=5, delay=5):
    """Initializes the connection to the CAN bus with retries."""
    global CAN_BUS
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Attempting to connect to CAN bus '{CONFIG['can_interface']}'...")
            CAN_BUS = can.interface.Bus(
                channel=CONFIG['can_interface'],
                bustype='socketcan',
                receive_own_messages=False
            )
            logger.info("CAN bus connection successful.")
            return True
        except can.CanError as e:
            logger.error(f"Attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(delay)
    logger.critical("Could not initialize CAN bus after multiple retries.")
    return False

# MODIFIED: Unified ZMQ socket initialization
def initialize_zmq_sockets():
    """Initializes all required ZeroMQ sockets."""
    global ZMQ_CONTEXT, ZMQ_PUB_SOCKET, ZMQ_PULL_SOCKET
    try:
        ZMQ_CONTEXT = zmq.Context()
        
        # Publisher for broadcasting received CAN messages
        logger.info(f"Binding ZeroMQ publisher to {CONFIG['zmq_publish_address']}...")
        ZMQ_PUB_SOCKET = ZMQ_CONTEXT.socket(zmq.PUB)
        ZMQ_PUB_SOCKET.set_hwm(1000)
        ZMQ_PUB_SOCKET.bind(CONFIG['zmq_publish_address'])
        logger.info("ZeroMQ publisher bound successfully.")
        
        # PULL socket to receive outgoing CAN messages from other services
        logger.info(f"Binding ZeroMQ PULL socket to {CONFIG['zmq_send_address']}...")
        ZMQ_PULL_SOCKET = ZMQ_CONTEXT.socket(zmq.PULL)
        ZMQ_PULL_SOCKET.bind(CONFIG['zmq_send_address'])
        logger.info("ZeroMQ PULL socket bound successfully.")
        
        return True
    except zmq.ZMQError as e:
        logger.critical(f"Failed to initialize ZeroMQ sockets: {e}")
        return False

# MODIFIED: Teardown also cleans up the new socket
def teardown_resources():
    """Gracefully closes all active resources."""
    global CAN_BUS, ZMQ_PUB_SOCKET, ZMQ_PULL_SOCKET, ZMQ_CONTEXT
    logger.info("Tearing down resources...")
    if ZMQ_PUB_SOCKET and not ZMQ_PUB_SOCKET.closed:
        ZMQ_PUB_SOCKET.close()
        logger.info("ZMQ publisher socket closed.")
    if ZMQ_PULL_SOCKET and not ZMQ_PULL_SOCKET.closed:
        ZMQ_PULL_SOCKET.close()
        logger.info("ZMQ PULL socket closed.")
    if ZMQ_CONTEXT and not ZMQ_CONTEXT.closed:
        ZMQ_CONTEXT.term()
        logger.info("ZMQ context terminated.")
    if CAN_BUS:
        CAN_BUS.shutdown()
        logger.info("CAN bus shut down.")
        CAN_BUS = None


# --- Signal Handling ---
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
        logger.info(f"Shutdown signal {signum} received. Exiting...")
        RUNNING = False

def reload_config_handler(signum, frame):
    """Flags the application to reload its configuration."""
    global RELOAD_CONFIG
    logger.info("SIGHUP signal received. Flagging for configuration reload.")
    RELOAD_CONFIG = True


# --- Main Application ---
def main():
    """The main application entry point and loop."""
    global RELOAD_CONFIG, CAN_BUS
    if not load_and_initialize_config():
        sys.exit(1)

    setup_signal_handlers()

    if not initialize_zmq_sockets() or not initialize_can_bus():
        teardown_resources()
        sys.exit(1)

    logger.info("CAN handler service started successfully.")
    message_count = 0
    last_log_time = time.time()

    while RUNNING:
        try:
            if RELOAD_CONFIG:
                logger.info("Reloading configuration...")
                teardown_resources()
                load_and_initialize_config()
                initialize_zmq_sockets()
                initialize_can_bus()
                RELOAD_CONFIG = False
                logger.info("Configuration reload complete.")

            if CAN_BUS is None:
                logger.warning("CAN bus is not available. Attempting to reconnect...")
                if not initialize_can_bus():
                    time.sleep(10) 
                    continue
            
            # --- MODIFIED: Handle incoming and outgoing messages ---

            # 1. Receive from CAN and publish to ZMQ
            message = CAN_BUS.recv(timeout=0.01) # Use a short timeout
            if message:
                msg_dict = {
                    "timestamp": message.timestamp,
                    "arbitration_id": message.arbitration_id,
                    "dlc": message.dlc,
                    "data_hex": message.data.hex()
                }
                topic = f"CAN_{message.arbitration_id:03X}"
                ZMQ_PUB_SOCKET.send_multipart([
                    topic.encode('utf-8'),
                    json.dumps(msg_dict).encode('utf-8')
                ])
                message_count += 1
            
            # 2. Receive from ZMQ and send to CAN (non-blocking)
            try:
                parts = ZMQ_PULL_SOCKET.recv_multipart(flags=zmq.NOBLOCK)
                if len(parts) == 2:
                    can_id = int(parts[0].decode())
                    data_hex = parts[1].decode()
                    msg_to_send = can.Message(
                        arbitration_id=can_id,
                        data=bytes.fromhex(data_hex),
                        is_extended_id=False
                    )
                    CAN_BUS.send(msg_to_send)
                    logger.debug(f"Sent CAN message from ZMQ: ID={can_id:03X}, Data={data_hex}")
            except zmq.Again:
                pass # No outgoing messages waiting

            if time.time() - last_log_time > 60:
                logger.info(f"Published {message_count} messages in the last minute.")
                message_count = 0
                last_log_time = time.time()

        except can.CanError as e:
            logger.error(f"CAN bus error occurred: {e}. Attempting to recover.")
            if CAN_BUS:
                CAN_BUS.shutdown()
            CAN_BUS = None 
            time.sleep(5)
        except Exception as e:
            logger.critical(f"An unexpected critical error occurred: {e}", exc_info=True)
            break 

    teardown_resources()
    logger.info("can_handler.py has finished.")


if __name__ == '__main__':
    main()
