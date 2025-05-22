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
LONG_PRESS_THRESHOLD = 5 # Number of 'press' CAN messages received to count as a long press

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
    (0, 32):  '2',       # 0x00 0x20 -> MMI Knob Left -> Key '2' (Scroll)
    (0, 64):  '1',       # 0x00 0x40 -> MMI Knob Right -> Key '1' (Scroll)
    (0, 2):   Key.esc,   # 0x00 0x02 -> Return Button Press -> Escape Key
    (0, 1):   'h'        # 0x00 0x01 -> Setup Button Press -> Key 'H'
}
# Mapping for MMI LONG Press actions (holding the button)
# !!! USER: REVIEW AND DEFINE YOUR MMI LONG PRESS ACTIONS HERE !!!
COMMAND_TO_KEY_LONG_MMI = {
    # (Decimal Byte 3, Decimal Byte 4) : pynput Key or None
    (1, 0):   None,      # Long press Prev Track -> No Action
    (2, 0):   None,      # Long press Next Track -> No Action
    (64, 0):  None,      # Long press MMI Upper Left -> No Action (WAR VORHER 'm' oder 'p')
    (128, 0): 'm',       # Long press MMI Lower Left -> Voice Command 'm' (NEU HIER!)
    (0, 16):  None,      # Long press MMI Knob -> No Action
    (0, 32):  None,      # Long press MMI Knob Left -> Ignored (Scroll command)
    (0, 64):  None,      # Long press MMI Knob Right -> Ignored (Scroll command)
    (0, 2):   Key.home,  # Long press Return Button -> Home Key (Example)
    (0, 1):   None       # Long press Setup Button -> No Action
}

# -- MFSW Controls (0x5C3) -- (Using Audi A4 B6/8E logic as base - VERIFY FOR YOUR CAR!)
# !!! USER: DEFINE YOUR DESIRED MFSW KEY ACTIONS HERE !!!
MFSW_SCROLL_UP_KEY = Key.media_volume_up    # MFSW Wheel Up -> Volume Up (EXAMPLE)
MFSW_SCROLL_DOWN_KEY = Key.media_volume_down  # MFSW Wheel Down -> Volume Down (EXAMPLE)
MFSW_MODE_SHORT_PRESS_KEY = Key.enter       # MFSW Mode short press -> Enter (EXAMPLE)
MFSW_MODE_LONG_PRESS_KEY = Key.media_next   # MFSW Mode long press -> Next Track (EXAMPLE)

# --- RNS-E Source Data for Auto Pause/Play ---
# (Verify these byte sequences for your RNS-E TV/Video input)
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
    print(f"Using refined Long Press detection and pynput.") # Updated message
    print(f"Ensure '{CAN_INTERFACE}' is up (e.g., sudo ip link set {CAN_INTERFACE} up type can bitrate 100000)")

    bus = None
    # State variables for MMI cooldown logic
    last_processed_command_mmi = None
    last_processed_time_mmi = 0.0
    # State variables for MMI long press detection
    press_counters_mmi = {}
    long_press_action_triggered_mmi = {} # Stores if long press already fired for current MMI hold

    # State variables for MFSW long press detection
    mfsw_mode_press_count = 0 # Counter for the MFSW mode button
    long_press_action_triggered_mfsw_mode = False # Stores if long press fired for MFSW mode

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
                     print(f"Error initializing CAN Bus: {init_e}\nWaiting 10s...")
                     time.sleep(10)
                     continue

            message = bus.recv(timeout=1.0)
            current_time_for_actions = time.time() # Get time once for all actions in this iteration

            if message:
                # --- MMI Handler (ID 0x461) ---
                if message.arbitration_id == TARGET_CAN_ID_MMI:
                    if message.dlc >= 5: # Ensure data length
                        byte3 = message.data[3]
                        byte4 = message.data[4]
                        command_tuple = (byte3, byte4)

                        if message.data[2] == 1: # Key Press
                            press_counters_mmi[command_tuple] = press_counters_mmi.get(command_tuple, 0) + 1
                            count = press_counters_mmi[command_tuple]
                            # print(f"  MMI Press: {command_tuple}, Count: {count}") # Debug

                            if command_tuple in MMI_SCROLL_COMMANDS:
                                key_to_sim = COMMAND_TO_KEY_SHORT_MMI.get(command_tuple)
                                simulate_key(key_to_sim) # Scroll commands trigger immediately
                            # Check for long press trigger during hold for non-scroll commands
                            elif not long_press_action_triggered_mmi.get(command_tuple, False) and \
                                 count == LONG_PRESS_THRESHOLD + 1:
                                
                                key_to_sim = COMMAND_TO_KEY_LONG_MMI.get(command_tuple)
                                print(f"  MMI Long Press detected during hold: {command_tuple} -> {key_to_sim}")
                                if key_to_sim: # Check Cooldown for this first long press action
                                    if command_tuple != last_processed_command_mmi or \
                                       current_time_for_actions - last_processed_time_mmi > COOLDOWN_PERIOD:
                                        simulate_key(key_to_sim)
                                        last_processed_command_mmi = command_tuple
                                        last_processed_time_mmi = current_time_for_actions
                                        long_press_action_triggered_mmi[command_tuple] = True
                                    else:
                                        print(f"  MMI Long Press {command_tuple} ignored (Cooldown).")
                                else: # No action defined for long press, but mark as triggered to prevent short press
                                     long_press_action_triggered_mmi[command_tuple] = True

                        elif message.data[2] == 4: # Key Release
                            count_on_release = press_counters_mmi.get(command_tuple, 0)
                            # print(f"  MMI Key Released: {command_tuple}, Count on release={count_on_release}") # Debug
                            
                            was_long_press_triggered = long_press_action_triggered_mmi.pop(command_tuple, False)
                            press_counters_mmi[command_tuple] = 0 # Reset counter

                            # Only trigger short press if not a scroll command, no long press was triggered, and count is in short press range
                            if command_tuple not in MMI_SCROLL_COMMANDS and \
                               not was_long_press_triggered and \
                               0 < count_on_release <= LONG_PRESS_THRESHOLD:
                                
                                key_to_sim = COMMAND_TO_KEY_SHORT_MMI.get(command_tuple)
                                print(f"  MMI Short Press on release: {command_tuple} -> {key_to_sim}")
                                if key_to_sim: # Check Cooldown for short press
                                    if command_tuple != last_processed_command_mmi or \
                                       current_time_for_actions - last_processed_time_mmi > COOLDOWN_PERIOD:
                                        simulate_key(key_to_sim)
                                        last_processed_command_mmi = command_tuple
                                        last_processed_time_mmi = current_time_for_actions
                                    else:
                                        print(f"  MMI Short Press {command_tuple} ignored (Cooldown).")
                
                # --- MFSW Handler (ID 0x5C3) ---
                elif message.arbitration_id == TARGET_CAN_ID_MFSW:
                    if message.dlc >= 2:
                        msg_hex_short = message.data[:2].hex().upper()
                        
                        if msg_hex_short == '3904': # Scroll Wheel Up
                            simulate_key(MFSW_SCROLL_UP_KEY)
                            mfsw_mode_press_count = 0 
                            long_press_action_triggered_mfsw_mode = False
                        elif msg_hex_short == '3905': # Scroll Wheel Down
                            simulate_key(MFSW_SCROLL_DOWN_KEY)
                            mfsw_mode_press_count = 0
                            long_press_action_triggered_mfsw_mode = False
                        elif msg_hex_short == '3908': # Mode Button Press
                            mfsw_mode_press_count += 1
                            # print(f"  MFSW Mode press count: {mfsw_mode_press_count}") # Debug
                            if not long_press_action_triggered_mfsw_mode and \
                               mfsw_mode_press_count == LONG_PRESS_THRESHOLD + 1:
                                print("  MFSW Mode Long Press detected during hold.")
                                simulate_key(MFSW_MODE_LONG_PRESS_KEY)
                                long_press_action_triggered_mfsw_mode = True
                        elif (msg_hex_short == '3900' or msg_hex_short == '3A00'): # Mode Release or Idle
                            count_on_release = mfsw_mode_press_count
                            
                            if not long_press_action_triggered_mfsw_mode and \
                               0 < count_on_release <= LONG_PRESS_THRESHOLD:
                                print("  MFSW Mode Short Press on release.")
                                simulate_key(MFSW_MODE_SHORT_PRESS_KEY)
                            
                            mfsw_mode_press_count = 0 
                            long_press_action_triggered_mfsw_mode = False

                # --- RNS-E Source Handler (ID 0x661) ---
                elif message.arbitration_id == TARGET_CAN_ID_SOURCE:
                    if message.dlc >= 8:
                        is_now_video_src = (message.data[:8] == VIDEO_SOURCE_DATA_1 or message.data[:8] == VIDEO_SOURCE_DATA_2)
                        if is_now_video_src != is_pi_source_active or is_pi_source_active is None:
                            if is_now_video_src:
                                print("RNS-E Source: TV/Video -> Simulating PLAY (Taste X)")
                                simulate_key('x') # Play key
                            elif is_pi_source_active is not None: 
                                print("RNS-E Source: Other -> Simulating PAUSE (Taste C)")
                                simulate_key('c') # Pause key
                            is_pi_source_active = is_now_video_src

        except can.CanError as e:
            print(f"CAN Error: {e}\nBus shutting down. Waiting 5s...")
            if bus: bus.shutdown(); bus = None
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nScript exiting (Ctrl+C).")
            break
        except Exception as e:
            print(f"Unexpected error: {e}\nWaiting 10s...")
            if bus: bus.shutdown(); bus = None
            time.sleep(10)

    if bus: bus.shutdown()
    print("Program terminated.")