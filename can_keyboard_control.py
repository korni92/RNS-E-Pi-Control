#!/usr/bin/env python3
# Import necessary libraries
import can
import os
import subprocess
import time

# --- Configuration ---
CAN_INTERFACE = 'can0'
# CAN ID for RNS-E MMI Controls (TV Mode)
TARGET_CAN_ID = 0x461

# Mapping: Tuple (Value_Byte_3, Value_Byte_4) -> xdotool Key Name
# Tuple values are DECIMAL (corresponding to Hex values from CAN message)
# Script only reacts when Byte 2 == 1 (Key Press) Byte 2 == 4 (Key release)
# ----- FINAL MAPPING (Beta 1 / 2025-04-30) -----
COMMAND_TO_KEY = {
    # (Decimal Byte 3, Decimal Byte 4) : Keyboard Key
    (1, 0):   'V',       # Hex 01 00 -> Prev Track -> Key 'V'
    (2, 0):   'N',       # Hex 02 00 -> Next Track -> Key 'N'
    (64, 0):  'Up',      # Hex 40 00 -> MMI Upper Left -> Arrow Up
    (128, 0): 'Down',    # Hex 80 00 -> MMI Lower Left -> Arrow Down
    (0, 16):  'Return',  # Hex 00 10 -> MMI Knob Press -> Return
    (0, 32):  '1',       # Hex 00 20 -> MMI Knob Left -> Key '1'
    (0, 64):  '2',       # Hex 00 40 -> MMI Knob Right -> Key '2'
    (0, 2):   'Escape',  # Hex 00 02 -> Return Button Press -> Escape Key
    (0, 1):   'H'        # Hex 00 01 -> Setup Button Press -> Key 'H'
}
# --- End Configuration ---

# Function to simulate a key press using xdotool
def simulate_key(key_name):
    """ Calls xdotool to simulate a key press. """
    try:
        print(f"Simulating key press: {key_name}")
        # 'key' simulates a short press (Down + Up)
        subprocess.run(['xdotool', 'key', key_name], check=True)
    except FileNotFoundError:
        print("ERROR: 'xdotool' not found. Please install (sudo apt install xdotool)")
    except subprocess.CalledProcessError as e:
        print(f"ERROR running xdotool: {e}")
    except Exception as e:
        print(f"General error in simulate_key: {e}")

# Main program loop
if __name__ == "__main__":
    print(f"Starting Audi RNS-E CAN Listener on {CAN_INTERFACE} for ID {hex(TARGET_CAN_ID)}...")
    print(f"Ensure '{CAN_INTERFACE}' is configured and UP (e.g., sudo ip link set {CAN_INTERFACE} up type can bitrate 100000)")
    print(f"Ensure xdotool is installed (sudo apt install xdotool)")

    bus = None
    while True: # Loop forever for continuous operation
        try:
            # Initialize/re-initialize CAN bus if not connected
            if bus is None:
                 print("Attempting to initialize CAN Bus...")
                 try:
                     # Attempt to initialize the bus interface
                     bus = can.interface.Bus(channel=CAN_INTERFACE, bustype='socketcan', receive_own_messages=False)
                     print(f"CAN Bus {CAN_INTERFACE} initialized successfully.")
                 except Exception as init_e:
                     # Handle initialization errors
                     print(f"Error initializing CAN Bus: {init_e}")
                     print("Waiting 10 seconds before retry...")
                     time.sleep(10)
                     continue # Go back to the start of the while loop

            # Wait for the next CAN message with a timeout
            message = bus.recv(timeout=1.0)

            if message:
                # Check if it's the target CAN ID
                if message.arbitration_id == TARGET_CAN_ID:
                    # Log received message for diagnostics (as Hex)
                    print(f"Received: ID={hex(message.arbitration_id)}, DLC={message.dlc}, Data={message.data.hex()}")

                    # Check if message is long enough (min. 5 bytes for index 4)
                    # AND if Byte 2 indicates a key press (value 1)
                    if message.dlc >= 5 and message.data[2] == 1:
                        # Extract relevant bytes (3 and 4) as INTEGERS
                        byte3 = message.data[3]
                        byte4 = message.data[4]
                        # Create tuple for dictionary lookup
                        command_tuple = (byte3, byte4)
                        print(f"  Key Press Detected (Byte 2 = 1). Checking command code (Byte 3, Byte 4): {command_tuple}")

                        # Find corresponding key name in the mapping
                        key_to_simulate = COMMAND_TO_KEY.get(command_tuple)

                        if key_to_simulate:
                            # Simulate the key press
                            simulate_key(key_to_simulate)
                        else:
                            # Log if the received code tuple is not defined in the mapping
                            print(f"  No action defined for code tuple {command_tuple}.")
                    # Optional: Handle key release (Byte 2 == 4) if needed
                    # elif message.dlc >= 5 and message.data[2] == 4:
                    #     print("  Key Released (Byte 2 = 4). No action taken.")

            # If bus.recv() returns None (timeout), the loop continues.

        except can.CanError as e:
            # Handle CAN communication errors (e.g., bus problems, interface down)
            print(f"CAN Error occurred: {e}")
            if bus is not None:
                print("Shutting down CAN Bus...")
                bus.shutdown()
                bus = None # Signal to re-initialize in the next loop iteration
            print("Waiting 5 seconds before retry...")
            time.sleep(5) # Wait briefly before restarting the loop

        except KeyboardInterrupt:
            # Handle clean exit on Ctrl+C
            print("\nScript exiting (Ctrl+C received).")
            break # Exit the while True loop

        except Exception as e:
            # Catch any other unexpected errors
            print(f"An unexpected error occurred: {e}")
            if bus is not None:
                 # Clean shutdown on unexpected error
                 bus.shutdown()
                 bus = None
            print("Waiting 10 seconds...")
            time.sleep(10)

    # Cleanup after exiting the loop (only on KeyboardInterrupt)
    if bus is not None:
        print("Closing CAN Bus.")
        bus.shutdown()

    print("Program terminated.")