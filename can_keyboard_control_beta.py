#!/usr/bin/env python3
# Import necessary libraries
import can
import time # Needed for cooldown logic and sleep
from pynput.keyboard import Key, Controller # Use pynput for key simulation

# --- Configuration ---
CAN_INTERFACE = 'can0' # CAN interface name
# --- CAN IDs ---
TARGET_CAN_ID_MMI = 0x461   # CAN ID for RNS-E MMI controls (TV Mode)
TARGET_CAN_ID_MFSW = 0x5C3  # CAN ID for MFSW (Steering Wheel) controls (Verify for your car!)
TARGET_CAN_ID_SOURCE = 0x661 # CAN ID for RNS-E Audio Source Status
# --- Cooldown & Long Press ---
COOLDOWN_PERIOD = 0.3 # 300 ms, prevents rapid repeats for non-scroll MMI actions
LONG_PRESS_THRESHOLD = 5 # Number of 'press' messages received to count as a long press

# MMI Commands exempt from cooldown AND executed immediately on press (scrolling)
MMI_SCROLL_COMMANDS = {
    (0, 32), # MMI Knob left (Hex 00 20 -> mapped to '2' by default)
    (0, 64)  # MMI Knob right (Hex 00 40 -> mapped to '1' by default)
}

# --- Key Mappings (Using pynput syntax) ---
# -- MMI Controls (0x461) --
# Mapping for MMI SHORT Press actions
COMMAND_TO_KEY_SHORT_MMI = {
    # (Decimal Byte 3, Decimal Byte 4) : pynput Key
    (1, 0):   'v',       # 0x01 0x00 -> Prev Track -> Key 'V'
    (2, 0):   'n',       # 0x02 0x00 -> Next Track -> Key 'N'
    (64, 0):  Key.up,    # 0x40 0x00 -> MMI Upper Left -> Arrow Up
    (128, 0): Key.down,  # 0x80 0x00 -> MMI Lower Left -> Arrow Down
    (0, 16):  Key.enter, # 0x00 0x10 -> MMI Knob Press -> Enter Key
    (0, 32):  '2',       # 0x00 0x20 -> MMI Knob Left -> Key '2' 
    (0, 64):  '1',       # 0x00 0x40 -> MMI Knob Right -> Key '1'
    (0, 2):   Key.esc,   # 0x00 0x02 -> Return Button Press -> Escape Key
    (0, 1):   'h'        # 0x00 0x01 -> Setup Button Press -> Key 'H'
}
# Mapping for MMI LONG Press actions (holding the button)
# !!! USER: DEFINE YOUR DESIRED MMI LONG PRESS ACTIONS HERE !!!
COMMAND_TO_KEY_LONG_MMI = {
    (0, 16):  Key.media_play_pause, # MMI Knob Long Press -> Play/Pause (EXAMPLE)
    (0, 2):   Key.home,  # Return Button Long Press -> Home Key (EXAMPLE)
    (0, 1):   'm',       # Setup Button Long Press -> Voice Command 'm' (EXAMPLE)
    # Add other long press actions or set to None for no action
    (1,0): None, (2,0): None, (64,0): 'p', (128,0): None, (0,32): None, (0,64): None
}

# -- MFSW Controls (0x5C3) -- (Using Audi A4 B6/8E logic as base - VERIFY FOR YOUR CAR!)
# !!! USER: DEFINE YOUR DESIRED MFSW KEY ACTIONS HERE !!!
MFSW_SCROLL_UP_KEY = Key.up      # MFSW Wheel Up -> Arrow Up (EXAMPLE)
MFSW_SCROLL_DOWN_KEY = Key.down    # MFSW Wheel Down -> Arrow Down (EXAMPLE)
MFSW_MODE_SHORT_PRESS_KEY = Key.enter # MFSW Mode short press -> Enter (EXAMPLE)
MFSW_MODE_LONG_PRESS_KEY = Key.esc    # MFSW Mode long press -> Escape (EXAMPLE)

# --- RNS-E Source Data for Auto Pause/Play ---
VIDEO_SOURCE_DATA_1 = bytes.fromhex('8101123700000000')
VIDEO_SOURCE_DATA_2 = bytes.fromhex('8301123700000000')
# --- End Configuration ---

try:
    keyboard = Controller()
except Exception as e:
    print(f"FATAL: Could not initialize pynput keyboard controller: {e}\n"
          "Ensure an X11 server is running and DISPLAY environment variable is set. Exiting.")
    exit(1) # Exit if controller cannot be initialized

def simulate_key(key_name):
    """ Simulates a key press using pynput. """
    if key_name is None:
        # print("  No action defined for this press type.") # Optional: Log no-action
        return
    try:
        print(f"Simulating pynput key press: {key_name}")
        keyboard.press(key_name)
        time.sleep(0.05) # Short delay between press and release for compatibility
        keyboard.release(key_name)
    except Exception as e:
        print(f"ERROR during pynput simulate_key: {e}")

if __name__ == "__main__":
    print(f"Starting CAN Listener on {CAN_INTERFACE} for MMI ({hex(TARGET_CAN_ID_MMI)}), "
          f"MFSW ({hex(TARGET_CAN_ID_MFSW)}), Source ({hex(TARGET_CAN_ID_SOURCE)})...")
    print(f"Using Long Press detection and pynput.")
    print(f"Ensure '{CAN_INTERFACE}' is up (e.g., sudo ip link set {CAN_INTERFACE} up type can bitrate 100000)")

    bus = None
    # State variables for MMI cooldown logic
    last_processed_command_mmi = None
    last_processed_time_mmi = 0.0
    # State variables for MMI long press detection
    press_counters_mmi = {}
    # State variables for MFSW long press detection
    mfsw_mode_press_count = 0 # Counter for the MFSW mode button
    # State variable for Auto Pause/Play
    is_pi_source_active = None # Use None initially to trigger action on first valid message

    while True:
        try:
            if bus is None:
                 print("Attempting to initialize CAN Bus...")
                 try:
                     bus = can.interface.Bus(channel=CAN_INTERFACE, bustype='socketcan', receive_own_messages=False)
                     print(f"CAN Bus {CAN_INTERFACE} initialized successfully.")
                 except Exception as init_e:
                     print(f"Error initializing CAN Bus: {init_e}")
                     print("Waiting 10 seconds before retry...")
                     time.sleep(10)
                     continue

            message = bus.recv(timeout=1.0)

            if message:
                # --- MMI Handler (ID 0x461) ---
                if message.arbitration_id == TARGET_CAN_ID_MMI:
                    # print(f"MMI Recv: {message.data.hex()}") # Debug
                    if message.dlc >= 5: # Ensure data length
                        byte3 = message.data[3]
                        byte4 = message.data[4]
                        command_tuple = (byte3, byte4)

                        if message.data[2] == 1: # Key Press
                            press_counters_mmi[command_tuple] = press_counters_mmi.get(command_tuple, 0) + 1
                            if command_tuple in MMI_SCROLL_COMMANDS:
                                # print(f"  MMI Scroll Press: {command_tuple}") # Debug
                                key_to_sim = COMMAND_TO_KEY_SHORT_MMI.get(command_tuple)
                                simulate_key(key_to_sim)
                        elif message.data[2] == 4: # Key Release
                            if command_tuple in press_counters_mmi and press_counters_mmi[command_tuple] > 0:
                                count = press_counters_mmi[command_tuple]
                                press_counters_mmi[command_tuple] = 0 # Reset counter
                                # print(f"  MMI Key Released: {command_tuple}, Count={count}") # Debug

                                if command_tuple not in MMI_SCROLL_COMMANDS:
                                    is_long = (count > LONG_PRESS_THRESHOLD)
                                    key_map = COMMAND_TO_KEY_LONG_MMI if is_long else COMMAND_TO_KEY_SHORT_MMI
                                    key_to_sim = key_map.get(command_tuple)
                                    # print(f"  MMI {'Long' if is_long else 'Short'} Press: {command_tuple} -> {key_to_sim}") # Debug

                                    if key_to_sim: # Check Cooldown
                                        current_time = time.time()
                                        if command_tuple != last_processed_command_mmi or \
                                           current_time - last_processed_time_mmi > COOLDOWN_PERIOD:
                                            simulate_key(key_to_sim)
                                            last_processed_command_mmi = command_tuple
                                            last_processed_time_mmi = current_time
                                        # else: # Optional log for cooldown
                                        #    print(f"  MMI {command_tuple} ignored (Cooldown). Last action {current_time - last_processed_time_mmi:.2f}s ago.")

                # --- MFSW Handler (ID 0x5C3) ---
                elif message.arbitration_id == TARGET_CAN_ID_MFSW:
                    msg_hex_short = message.data.hex().upper()[:4] # Focus on first 2 bytes (4 hex chars)
                    # print(f"MFSW Recv: {msg_hex_short}, Full: {message.data.hex()}") # Debug

                    # Example MFSW logic (Audi A4 B6/8E style - VERIFY FOR YOUR CAR!)
                    if msg_hex_short == '3904': # Scroll Wheel Up
                        # print("  MFSW Scroll Up") # Debug
                        simulate_key(MFSW_SCROLL_UP_KEY)
                        mfsw_mode_press_count = 0 # Reset mode counter on other MFSW actions
                    elif msg_hex_short == '3905': # Scroll Wheel Down
                        # print("  MFSW Scroll Down") # Debug
                        simulate_key(MFSW_SCROLL_DOWN_KEY)
                        mfsw_mode_press_count = 0
                    elif msg_hex_short == '3908': # Mode Button Press
                        mfsw_mode_press_count += 1
                        # print(f"  MFSW Mode press count: {mfsw_mode_press_count}") # Debug
                    elif (msg_hex_short == '3900' or msg_hex_short == '3A00') and mfsw_mode_press_count > 0: # Mode Release
                        # print(f"  MFSW Mode Released, Count={mfsw_mode_press_count}") # Debug
                        key_to_sim = MFSW_MODE_LONG_PRESS_KEY if mfsw_mode_press_count > LONG_PRESS_THRESHOLD else MFSW_MODE_SHORT_PRESS_KEY
                        simulate_key(key_to_sim)
                        mfsw_mode_press_count = 0
                    # Optional: Reset counter if idle message received without prior press
                    # elif (msg_hex_short == '3900' or msg_hex_short == '3A00') and mfsw_mode_press_count == 0:
                    #    pass # Do nothing, already idle

                # --- RNS-E Source Handler (ID 0x661) ---
                elif message.arbitration_id == TARGET_CAN_ID_SOURCE:
                    # print(f"Source Recv: {message.data.hex()}") # Debug
                    is_now_video_src = (message.data == VIDEO_SOURCE_DATA_1 or message.data == VIDEO_SOURCE_DATA_2)

                    # Act only if state changes or it's the first valid message
                    if is_now_video_src != is_pi_source_active or is_pi_source_active is None:
                        if is_now_video_src:
                            print("RNS-E Source: TV/Video -> Simulating PLAY")
                            simulate_key(Key.media_play_pause) # Or Key.media_play
                        elif is_pi_source_active is not None: # Don't pause if it was never determined to be active
                            print("RNS-E Source: Other -> Simulating PAUSE")
                            simulate_key(Key.media_play_pause) # Or Key.media_pause
                        is_pi_source_active = is_now_video_src

            # Timeout occurred, loop continues...

        except can.CanError as e:
            print(f"CAN Error occurred: {e}")
            if bus: bus.shutdown(); bus = None
            print("Waiting 5s before retry...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nScript exiting (Ctrl+C).")
            break
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            if bus: bus.shutdown(); bus = None
            print("Waiting 10s...")
            time.sleep(10)

    if bus: bus.shutdown()
    print("Program terminated.")