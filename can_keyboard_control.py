#!/usr/bin/env python3
# Import necessary libraries
import can
import time # Needed for cooldown logic and sleep
from pynput.keyboard import Key, Controller # Use pynput for key simulation

# --- Configuration ---
CAN_INTERFACE = 'can0' # CAN interface name
# Target CAN ID for Audi RNS-E MMI controls (TV Mode)
TARGET_CAN_ID = 0x461
# Cooldown period in seconds to prevent rapid key repeats (for non-scroll actions)
COOLDOWN_PERIOD = 0.3 # 300 ms
# Threshold for long press detection (adjust based on testing)
# How many 'press' messages count as a long press?
LONG_PRESS_THRESHOLD = 5

# Commands exempt from cooldown AND executed immediately on press (e.g., scrolling)
# Add command tuples (Byte 3, Byte 4) for knob left/right here
SCROLL_COMMANDS = {
    (0, 32), # MMI Knob left (Hex 00 20 -> mapped to '2')
    (0, 64)  # MMI Knob right (Hex 00 40 -> mapped to '1')
}

# --- Key Mappings ---
# NOTE: Key names are now for pynput!
# Standard keys: Key.up, Key.down, Key.left, Key.right, Key.enter, Key.esc, Key.home, Key.media_play_pause
# Character keys: '1', '2', 'v', 'n', 'h' (usually lowercase)

# Mapping for SHORT Press actions
COMMAND_TO_KEY_SHORT = {
    # (Decimal Byte 3, Decimal Byte 4) : pynput Key
    (1, 0):   'v',       # 0x01 0x00 -> Prev Track -> Key 'V'
    (2, 0):   'n',       # 0x02 0x00 -> Next Track -> Key 'N'
    (64, 0):  Key.up,    # 0x40 0x00 -> MMI Upper Left -> Arrow Up
    (128, 0): Key.down,  # 0x80 0x00 -> MMI Lower Left -> Arrow Down
    (0, 16):  Key.enter, # 0x00 0x10 -> MMI Knob Press -> Enter Key
    (0, 32):  '2',       # 0x00 0x20 -> MMI Knob Left -> Key '2' (Scroll command, also handled on press)
    (0, 64):  '1',       # 0x00 0x40 -> MMI Knob Right -> Key '1' (Scroll command, also handled on press)
    (0, 2):   Key.esc,   # 0x00 0x02 -> Return Button Press -> Escape Key
    (0, 1):   'h'        # 0x00 0x01 -> Setup Button Press -> Key 'H'
}

# !!! DEFINE YOUR LONG PRESS ACTIONS HERE !!!
# Mapping for LONG Press actions (holding the button)
# NOTE: Using pynput Key objects/strings
COMMAND_TO_KEY_LONG = {
    # (Decimal Byte 3, Decimal Byte 4) : pynput Key or None
    (1, 0):   None,      # Long press Prev Track -> No Action
    (2, 0):   None,      # Long press Next Track -> No Action
    (64, 0):  None,      # Long press MMI Upper Left -> No Action
    (128, 0): None,      # Long press MMI Lower Left -> No Action
    (0, 16):  Key.media_play_pause, # Long press MMI Knob -> Play/Pause
    (0, 32):  None,      # Long press MMI Knob Left -> Ignored (Scroll command)
    (0, 64):  None,      # Long press MMI Knob Right -> Ignored (Scroll command)
    (0, 2):   'p',       # Long press Return Button -> Phone Key 'p'
    (0, 1):   'm'        # Long press Setup Button -> Voice Cmd Key 'm'
}
# --- End Configuration ---

# Initialize pynput keyboard controller
try:
    keyboard = Controller()
except Exception as e:
    print(f"FATAL: Could not initialize pynput keyboard controller: {e}")
    print("Check your graphical environment / permissions. Exiting.")
    exit() # Exit if controller cannot be initialized

# Function to simulate a key press using pynput
def simulate_key(key_name):
    """ Simulates a key press using pynput. """
    if key_name is None:
        print("  No action defined for this long press.")
        return # Do nothing if action is None

    try:
        print(f"Simulating pynput key press: {key_name}")
        keyboard.press(key_name)
        time.sleep(0.05) # Short delay between press and release
        keyboard.release(key_name)
    except Exception as e:
        # Catch potential errors during press/release
        print(f"ERROR during pynput simulate_key: {e}")

# Main program loop
if __name__ == "__main__":
    print(f"Starting Audi RNS-E CAN Listener on {CAN_INTERFACE} for ID {hex(TARGET_CAN_ID)}...")
    print(f"Using Long Press detection and pynput.")
    print(f"Ensure '{CAN_INTERFACE}' is up (e.g., sudo ip link set {CAN_INTERFACE} up type can bitrate 100000)")

    bus = None
    # Variables for cooldown logic
    last_processed_command = None
    last_processed_time = 0.0
    # Dictionary to store press counters for long press detection
    press_counters = {}

    while True:
        try:
            # Initialize / Re-initialize CAN bus if needed
            if bus is None:
                 print("Attempting to initialize CAN Bus...")
                 try:
                     bus = can.interface.Bus(channel=CAN_INTERFACE, bustype='socketcan', receive_own_messages=False)
                     print(f"CAN Bus {CAN_INTERFACE} initialized successfully.")
                 except Exception as init_e:
                     print(f"Error initializing CAN Bus: {init_e}")
                     print("Waiting 10 seconds before retry...")
                     time.sleep(10)
                     continue # Go back to the start of the while loop

            # Wait for next CAN message
            message = bus.recv(timeout=1.0) # Timeout after 1 second

            if message:
                # Check if it's the target CAN ID
                if message.arbitration_id == TARGET_CAN_ID:
                    # Optional: Reduced logging for cleaner output once stable
                    # print(f"Recv: {message.data.hex()}")

                    # Check message length and process based on Byte 2 (Press/Release)
                    if message.dlc >= 5:
                        byte3 = message.data[3]
                        byte4 = message.data[4]
                        command_tuple = (byte3, byte4)

                        # --- Handle Key Press (Byte 2 == 1) ---
                        if message.data[2] == 1:
                            press_counters[command_tuple] = press_counters.get(command_tuple, 0) + 1
                            # Execute scroll commands immediately on press
                            if command_tuple in SCROLL_COMMANDS:
                                print(f"  Scroll Command Press: {command_tuple}")
                                key_to_simulate = COMMAND_TO_KEY_SHORT.get(command_tuple) # Scroll uses short press mapping
                                if key_to_simulate:
                                    simulate_key(key_to_simulate)
                                # else: # Optional: Log if scroll command has no mapping
                                #    print(f"  No scroll action defined for {command_tuple}.")

                        # --- Handle Key Release (Byte 2 == 4) ---
                        elif message.data[2] == 4:
                            if command_tuple in press_counters and press_counters[command_tuple] > 0:
                                count = press_counters[command_tuple]
                                press_counters[command_tuple] = 0 # Reset counter
                                # print(f"  Key Released: {command_tuple}, Count={count}") # Debugging

                                # Determine action type (ignore release for scroll commands)
                                if command_tuple not in SCROLL_COMMANDS:
                                    is_long_press = (count > LONG_PRESS_THRESHOLD)
                                    if is_long_press:
                                        print(f"  Long Press detected.")
                                        key_to_simulate = COMMAND_TO_KEY_LONG.get(command_tuple)
                                    else:
                                        print(f"  Short Press detected.")
                                        key_to_simulate = COMMAND_TO_KEY_SHORT.get(command_tuple)

                                    # Execute action if found, respecting cooldown
                                    if key_to_simulate:
                                        current_time = time.time()
                                        if command_tuple != last_processed_command or \
                                           current_time - last_processed_time > COOLDOWN_PERIOD:

                                            simulate_key(key_to_simulate)
                                            last_processed_command = command_tuple
                                            last_processed_time = current_time
                                        else:
                                            print(f"  Ignoring {command_tuple} (Cooldown). Last action {current_time - last_processed_time:.2f}s ago.")
                                    # else: # Optional: Log if no action mapped for this press type
                                    #    print(f"  No {'long' if is_long_press else 'short'} press action defined for {command_tuple}.")

        except can.CanError as e:
            # Handle CAN communication errors
            print(f"CAN Error occurred: {e}")
            if bus is not None:
                print("Shutting down CAN Bus...")
                bus.shutdown()
                bus = None # Signal to re-initialize
            print("Waiting 5 seconds before retry...")
            time.sleep(5)

        except KeyboardInterrupt:
            # Handle clean exit on Ctrl+C
            print("\nScript exiting (Ctrl+C received).")
            break # Exit the while loop

        except Exception as e:
            # Catch other unexpected errors
            print(f"An unexpected error occurred: {e}")
            if bus is not None:
                 bus.shutdown()
                 bus = None
            print("Waiting 10 seconds...")
            time.sleep(10)

    # Cleanup after loop exit
    if bus is not None:
        print("Closing CAN Bus.")
        bus.shutdown()

    print("Program terminated.")