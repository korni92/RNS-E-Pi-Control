#!/usr/bin/env python3
#
# can_fis_writer.py
#
# This service periodically sends custom text to the instrument cluster
# display (FIS) based on settings in the central config.json file.
# It operates independently of other CAN-bus subscriber scripts.
#
# Designed to run as a robust, long-running systemd service.
#

import time
import subprocess
import logging
import signal
import sys
import json
import codecs
from unidecode import unidecode

# --- Global State ---
RUNNING = True
RELOAD_CONFIG = False
CONFIG = {}
FEATURES = {}


# --- Logging & Character Encoding Setup ---
def setup_logging():
    """Configures logging to a dedicated file and to standard output."""
    log_file = '/var/log/rnse_control/can_fis_writer.log'
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

def register_encoding_fallback():
    """Registers a fallback to handle special characters for the FIS display."""
    def unidecode_fallback(e):
        part = e.object[e.start:e.end]
        replacement = unidecode(part) or '?'
        return (replacement, e.start + len(part))
    codecs.register_error('unidecode_fallback', unidecode_fallback)

register_encoding_fallback()


# --- Configuration Handling ---
def load_and_initialize_config(config_path='/home/pi/config.json'):
    """Loads and processes the configuration needed for this script."""
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
        FEATURES.setdefault('fis_display', {'enabled': False})

        can_ids = cfg.setdefault('can_ids', {})
        CONFIG = {
            'can_interface': cfg['can_interface'],
            'fis_line1_id': int(can_ids.get('fis_line1', '0'), 16),
            'fis_line2_id': int(can_ids.get('fis_line2', '0'), 16),
            'fis_text_line1': FEATURES.get('fis_display', {}).get('line1', ''),
            'fis_text_line2': FEATURES.get('fis_display', {}).get('line2', ''),
        }
        logger.info("Configuration for FIS writer initialized.")
        return True
    except (KeyError, ValueError) as e:
        logger.critical(f"FATAL: Config is missing a key or has an invalid value: {e}", exc_info=True)
        return False


# --- Core Logic ---
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

def prepare_fis_text(text):
    """Encodes and formats a string for the 8-character Audi FIS display."""
    audi_ascii_map = {
        '61':'01', '62':'02', '63':'03', '64':'04', '65':'05', '66':'06', '67':'07',
        '68':'08', '69':'09', '6A':'0A', '6B':'0B', '6C':'0C', '6D':'0D', '6E':'0E',
        '6F':'0F', '70':'10', 'E4':'91', 'F6':'97', 'FC':'99', 'C4':'5F', 'D6':'60',
        'DC':'61', 'DF':'8D', 'B0':'BB', 'A7':'BF', 'A9':'A2', 'B1':'B4', 'B5':'B8',
        'B9':'B1', 'BA':'BB', '20':'20'
    }
    centered_text = text.center(8)
    hex_str = centered_text.encode('iso-8859-1', errors='unidecode_fallback').hex().upper()
    payload = "".join(audi_ascii_map.get(hex_str[i:i+2], '20') for i in range(0, len(hex_str), 2))
    # --- FIX: Use a single space ' ' for padding, not the string '20' ---
    return payload.ljust(16, ' ')

def send_fis_display_messages():
    """Sends the configured text lines to the instrument cluster."""
    line1_payload = prepare_fis_text(CONFIG['fis_text_line1'])
    send_can_message(CONFIG['fis_line1_id'], line1_payload)
    time.sleep(0.05)
    line2_payload = prepare_fis_text(CONFIG['fis_text_line2'])
    send_can_message(CONFIG['fis_line2_id'], line2_payload)


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
    if not load_and_initialize_config():
        sys.exit(1)

    setup_signal_handlers()
    logger.info("CAN FIS Writer service started successfully.")
    last_send_time = 0

    while RUNNING:
        try:
            if RELOAD_CONFIG:
                load_and_initialize_config()
                RELOAD_CONFIG = False
                logger.info("Configuration reloaded.")

            now = time.time()
            if FEATURES.get('fis_display', {}).get('enabled') and (now - last_send_time > 2.0):
                send_fis_display_messages()
                last_send_time = now

            time.sleep(0.1)

        except Exception:
            logger.critical("An unexpected critical error occurred in the main loop.", exc_info=True)
            break

    logger.info("CAN FIS Writer service has finished.")

if __name__ == '__main__':
    main()
