#!/usr/bin/env python3
# Import necessary libraries
import can
import os
import subprocess
import time # Needed for cooldown logic

# --- Configuration ---
CAN_INTERFACE = 'can0' # CAN interface name
# Target CAN ID for Audi RNS-E MMI controls (TV Mode)
TARGET_CAN_ID = 0x461
# Cooldown period in seconds to prevent rapid key repeats
COOLDOWN_PERIOD = 0.3 # 300 ms
# Commands exempt from cooldown (e.g., rotary knob for scrolling)
# Add command tuples (Byte 3, Byte 4) for knob left/right here
SCROLL_COMMANDS = {
    (0, 32), # MMI Knob Left (Hex 00 20)
    (0, 64)  # MMI Knob Right (Hex 00 40)
}

# Mapping: Tuple(Decimal_Byte_3, Decimal_Byte_4) -> xdotool key name
# Action is triggered only when Byte 2 == 1 (key press event)
# ----- Key Mapping (Adjust as needed - Beta 1.1 / 2025-05-01) -----
COMMAND_TO_KEY = {
    # (Decimal Byte 3, Decimal Byte 4) : Keyboard Key
    (1, 0):   'V',       # 0x01 0x00 -> Prev Track -> Key 'V'
    (2, 0):   'N',       # 0x02 0x00 -> Next Track -> Key 'N'
    (64, 0):  'Up',      # 0x40 0x00 -> MMI Upper Left -> Arrow Up
    (128, 0): 'Down',    # 0x80 0x00 -> MMI Lower Left -> Arrow Down
    (0, 16):  'Return',  # 0x00 0x10 -> MMI Knob Press -> Enter Key ('Return' is xdotool name)
    (0, 32):  '2',       # 0x00 0x20 -> MMI Knob Left -> Key '2'
    (0, 64):  '1',       # 0x00 0x40 -> MMI Knob Right -> Key '1'
    (0, 2):   'Escape',  # 0x00 0x02 -> Return Button Press -> Escape Key
    (0, 1):   'H'        # 0x00 0x01 -> Setup Button Press -> Key 'H'
    # --- Add more mappings if needed ---
}
# --- End Configuration ---

# Function to simulate a key press using xdotool
def simulate_key(key_name):
    """ Calls xdotool to simulate a key press. """
    try:
        # Note: Using German for output messages as per conversation history
        # Change to English if preferred: print(f"Simulating key press: {key_name}")
        print(f"Simuliere Tastendruck: {key_name}")
        # Note: 'key' simulates a brief press (down + up)
        subprocess.run(['xdotool', 'key', key_name], check=True)
    except FileNotFoundError:
        print("FEHLER: 'xdotool' nicht gefunden. Bitte installieren (sudo apt install xdotool)")
    except subprocess.CalledProcessError as e:
        print(f"FEHLER beim Ausführen von xdotool: {e}")
    except Exception as e:
        print(f"Allgemeiner Fehler bei simulate_key: {e}")

# Main program loop
if __name__ == "__main__":
    # Note: Using German for output messages as per conversation history
    # Change to English if preferred.
    print(f"Starte Audi RNS-E CAN Listener auf {CAN_INTERFACE} für ID {hex(TARGET_CAN_ID)}...")
    print(f"Achte darauf, dass '{CAN_INTERFACE}' konfiguriert und UP ist (z.B. sudo ip link set {CAN_INTERFACE} up type can bitrate 100000)")
    print(f"Stelle sicher, dass xdotool installiert ist (sudo apt install xdotool)")

    bus = None
    # Variables for cooldown logic
    last_processed_command = None
    last_processed_time = 0.0

    while True: # Loop forever for continuous operation
        try:
            # Initialize / Re-initialize CAN bus if needed
            if bus is None:
                 print("Versuche CAN Bus zu initialisieren...")
                 try:
                     # Attempt bus initialization
                     bus = can.interface.Bus(channel=CAN_INTERFACE, bustype='socketcan', receive_own_messages=False)
                     print(f"CAN Bus {CAN_INTERFACE} erfolgreich initialisiert.")
                 except Exception as init_e:
                     # Handle initialization error
                     print(f"Fehler bei CAN Bus Initialisierung: {init_e}")
                     print("Warte 10 Sekunden vor nächstem Versuch...")
                     time.sleep(10)
                     continue # Go back to the start of the while loop

            # Wait for next CAN message
            message = bus.recv(timeout=1.0) # Timeout after 1 second

            if message:
                # Check if it's the target CAN ID
                if message.arbitration_id == TARGET_CAN_ID:
                    # Log received message data (hex) for diagnostics
                    print(f"Empfangen: ID={hex(message.arbitration_id)}, DLC={message.dlc}, Daten={message.data.hex()}")

                    # Check if message length is sufficient (min 5 bytes for index 4)
                    # AND if Byte 2 indicates key press (value 1)
                    if message.dlc >= 5 and message.data[2] == 1:
                        # Extract relevant bytes (3 and 4) as INTEGERS
                        byte3 = message.data[3]
                        byte4 = message.data[4]
                        # Create tuple for dictionary lookup
                        command_tuple = (byte3, byte4)
                        print(f"  Tastendruck erkannt (Byte 2 = 1). Prüfe Befehlscode (Byte 3, Byte 4): {command_tuple}")

                        # Find corresponding key name in mapping
                        key_to_simulate = COMMAND_TO_KEY.get(command_tuple)
                        # Get current time for cooldown check
                        current_time = time.time()

                        if key_to_simulate:
                            # Check cooldown, unless it's an exempt scroll command
                            is_scroll_command = command_tuple in SCROLL_COMMANDS
                            if is_scroll_command or \
                               command_tuple != last_processed_command or \
                               current_time - last_processed_time > COOLDOWN_PERIOD:

                                # Cooldown inactive or exception -> simulate key
                                simulate_key(key_to_simulate)
                                # Store state for next cooldown check
                                last_processed_command = command_tuple
                                last_processed_time = current_time
                            else:
                                # Same command within cooldown period (and not a scroll command)
                                print(f"  Ignoriere Code-Tuple {command_tuple} (Cooldown aktiv). Letzte Ausführung vor {current_time - last_processed_time:.2f}s)")
                        else:
                            # Log if the command tuple is not defined in the mapping
                            print(f"  Keine Aktion für Code-Tuple {command_tuple} definiert.")
                    # Optional: Handle key release (Byte 2 == 4) here if needed
                    # elif message.dlc >= 5 and message.data[2] == 4:
                    #     print("  Key Released (Byte 2 = 4). No action taken.")

            # Timeout occurred, loop continues...

        except can.CanError as e:
            # Handle CAN communication errors (e.g., bus issues, interface down)
            print(f"CAN Fehler aufgetreten: {e}")
            if bus is not None:
                print("Fahre CAN Bus herunter...")
                bus.shutdown()
                bus = None # Signal to re-initialize in the next loop iteration
            print("Warte 5 Sekunden vor nächstem Versuch...")
            time.sleep(5) # Wait briefly before restarting the loop

        except KeyboardInterrupt:
            # Handle clean exit on Ctrl+C
            print("\nSkript wird beendet (Strg+C).")
            break # Exit the while True loop

        except Exception as e:
            # Catch other unexpected errors
            print(f"Ein unerwarteter Fehler ist aufgetreten: {e}")
            if bus is not None:
                 # Cleanup on unexpected error
                 bus.shutdown()
                 bus = None
                 print("Warte 10 Sekunden...")
            time.sleep(10)

    # Cleanup after loop exit (only on KeyboardInterrupt)
    if bus is not None:
        print("Schließe CAN-Bus.")
        bus.shutdown()

    print("Programm beendet.")