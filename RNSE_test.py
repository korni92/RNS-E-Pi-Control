import tkinter as tk
from tkinter import messagebox
import can
import logging
import sys

# --- !!! USER CONFIGURATION REQUIRED !!! ---
# >> EDIT THE VALUES BELOW TO MATCH YOUR WINDOWS CAN ADAPTER <<

# 1. Set the 'bustype' based on your adapter brand/type.
#    Common options: 'pcan', 'kvaser', 'vector', 'slcan', 'usb2can', 'ics', 'canalystii'
CAN_INTERFACE = 'pcan'  # <--- EDIT THIS (e.g., 'pcan', 'kvaser', 'vector', 'slcan')

# 2. Set the 'channel'. This depends heavily on the 'bustype':
#    'pcan': Often found automatically. Can be None, or specific device ID like 'PCAN_USBBUS1'.
#            Check PCAN-View software for device names if needed.
#    'kvaser': Usually the channel index (integer), e.g., 0. Check Kvaser CanKing.
#    'vector': Channel index (integer), e.g., 0. Check Vector Hardware Config.
#              Also requires 'app_name' below.
#    'slcan': The COM port name (string), e.g., 'COM3'. Check Device Manager.
#    'usb2can': Often the COM port name (string), e.g., 'COM5'.
#    'ics': Usually the channel index (integer).
CAN_CHANNEL = None      # <--- EDIT THIS (e.g., None, 0, 'PCAN_USBBUS1', 'COM3')

# 3. Vector specific config (only needed if CAN_INTERFACE = 'vector'):
VECTOR_APP_NAME = 'MyCanApp' # <--- EDIT THIS: Required name for Vector apps

# 4. Set the CAN Bitrate (Ensure this matches your Audi's Infotainment CAN!)
#    100 kbit/s is common for RNS-E Infotainment CAN.
CAN_BITRATE = 100000       # <--- VERIFY THIS! (100000 = 100kbit/s)

# 5. Target CAN ID (Should be correct based on your info)
TARGET_CAN_ID = 0x461
# --- End User Configuration ---


# --- CAN Message Payloads (Hex values for the data bytes) ---
# Format: 'Button Label': [List of integer byte values]
BUTTON_MESSAGES = {
    "Prev Track (V)": [0x37, 0x30, 0x01, 0x01, 0x00, 0x00],
    "Next Track (N)": [0x37, 0x30, 0x01, 0x02, 0x00, 0x00],
    "MMI Up (Up)":    [0x37, 0x30, 0x01, 0x40, 0x00, 0x00],
    "MMI Down (Down)":[0x37, 0x30, 0x01, 0x80, 0x00, 0x00],
    "MMI Wheel Press (Enter)":[0x37, 0x30, 0x01, 0x00, 0x10, 0x00],
    "MMI Wheel Left (Left)":[0x37, 0x30, 0x01, 0x00, 0x20, 0x00],
    "MMI Wheel Right (Right)":[0x37, 0x30, 0x01, 0x00, 0x40, 0x00],
    "Return (Esc)":   [0x37, 0x30, 0x01, 0x00, 0x02, 0x00],
    "Setup (H)":      [0x37, 0x30, 0x01, 0x00, 0x01, 0x00],
}

# Global variable for the CAN bus instance
bus = None

# --- Functions ---
def initialize_can():
    """Initializes the CAN bus interface based on user configuration."""
    global bus
    if bus: # Already initialized
        return True

    try:
        # Prepare arguments for the CAN bus constructor
        # These arguments are passed directly to the backend interface
        kwargs = {'bitrate': CAN_BITRATE}
        if CAN_INTERFACE == 'vector':
            # Vector requires app_name and channel index
            if CAN_CHANNEL is None or not isinstance(CAN_CHANNEL, int):
                 raise ValueError("Vector interface requires an integer 'CAN_CHANNEL'.")
            kwargs['app_name'] = VECTOR_APP_NAME
            kwargs['channel'] = CAN_CHANNEL
        elif CAN_INTERFACE == 'slcan':
             # slcan requires the serial port name as channel
            if CAN_CHANNEL is None or not isinstance(CAN_CHANNEL, str):
                 raise ValueError("slcan interface requires a string 'CAN_CHANNEL' (e.g., 'COM3').")
            kwargs['channel'] = CAN_CHANNEL
            # kwargs['rtscts'] = True # Uncomment if your slcan adapter needs hardware flow control
        elif CAN_INTERFACE in ['pcan', 'kvaser', 'usb2can', 'ics', 'canalystii']:
            # These interfaces might need channel (often int, sometimes str like PCAN)
            # If CAN_CHANNEL is None, python-can might auto-detect or use a default
             if CAN_CHANNEL is not None:
                 kwargs['channel'] = CAN_CHANNEL
        # Add other interface-specific kwargs here if needed based on python-can docs

        logging.info(f"Attempting to initialize CAN bus: type='{CAN_INTERFACE}', args={kwargs}")
        bus = can.interface.Bus(bustype=CAN_INTERFACE, **kwargs)

        # Log the actual channel info if available after initialization
        channel_info = getattr(bus, 'channel_info', f"Channel: {CAN_CHANNEL}")
        logging.info(f"Successfully initialized CAN bus. {channel_info}")
        update_status(f"CAN Bus OK: {CAN_INTERFACE} ({channel_info})")
        return True

    except ImportError as e:
         logging.error(f"Import Error initializing CAN bus: {e}. Driver or library might be missing.")
         messagebox.showerror("CAN Initialization Error",
                              f"Failed to import required library for '{CAN_INTERFACE}'.\n"
                              f"Error: {e}\n\n"
                              f"Ensure the correct drivers (e.g., PCAN, Kvaser, Vector) AND any necessary "
                              f"Python packages (e.g., install vector library for 'vector' bustype) "
                              f"are installed.")
         update_status(f"CAN Error: Driver/Lib missing for {CAN_INTERFACE}?")
         return False

    except (OSError, can.CanError, ValueError, TypeError) as e: # Catch common errors
        logging.error(f"Error initializing CAN bus: {e}")
        messagebox.showerror("CAN Initialization Error",
                             f"Could not initialize CAN bus (Type: '{CAN_INTERFACE}').\n"
                             f"Error: {e}\n\n"
                             f"Troubleshooting Tips:\n"
                             f"- Is the CAN adapter plugged in?\n"
                             f"- Are the correct drivers installed?\n"
                             f"- Is the 'CAN_INTERFACE' correct?\n"
                             f"- Is the 'CAN_CHANNEL' correct for the interface "
                             f" (e.g., None, 0, 'PCAN_USBBUS1', 'COM3')?\n"
                             f"- Is the 'CAN_BITRATE' ({CAN_BITRATE} bps) correct?\n"
                             f"- Is the device already in use by another program (e.g., PCAN-View)?")
        update_status(f"CAN Error: Check HW/Drivers/Config")
        return False
    except Exception as e: # Catch any other unexpected errors
        logging.error(f"An unexpected error occurred during CAN initialization: {e}", exc_info=True)
        messagebox.showerror("CAN Initialization Error", f"An unexpected error occurred:\n{e}")
        update_status(f"CAN Error: Unexpected")
        return False

def send_can_message(label):
    """Sends the CAN message associated with the button label."""
    global bus
    if not bus:
        # Try to initialize if not already done or if previous attempt failed
        update_status("CAN bus not ready. Trying to init...")
        if not initialize_can():
            update_status("CAN Init Failed. Cannot send.")
            return # Stop if initialization failed

    if label not in BUTTON_MESSAGES:
        messagebox.showerror("Internal Error", f"No message defined for button '{label}'")
        return

    data_payload = BUTTON_MESSAGES[label]
    message = can.Message(
        arbitration_id=TARGET_CAN_ID,
        data=data_payload,
        is_extended_id=False, # Standard ID for 0x461
        dlc=len(data_payload) # Set Data Length Code based on payload size
    )

    try:
        bus.send(message)
        hex_data = ' '.join(f'{b:02X}' for b in message.data) # Format data as hex string
        logging.info(f"Sent: ID={hex(message.arbitration_id)}, DLC={message.dlc}, Data=[{hex_data}]")
        update_status(f"Sent: {label}")
    except can.CanError as e:
        logging.error(f"Error sending CAN message: {e}")
        messagebox.showerror("CAN Send Error", f"Failed to send message for '{label}'.\nError: {e}\n"
                                              f"Bus state may be invalid. Check connection/drivers.")
        update_status(f"Error Sending: {label}")
        # Optional: You might want to try shutting down and re-initializing the bus
        # if bus:
        #    bus.shutdown()
        # bus = None
    except Exception as e:
        logging.error(f"An unexpected error occurred during sending: {e}", exc_info=True)
        messagebox.showerror("Send Error", f"An unexpected error occurred during sending:\n{e}")
        update_status(f"Error Sending: Unexpected")


def on_closing():
    """Shuts down the CAN bus cleanly when the window is closed."""
    global bus
    if bus:
        try:
            bus.shutdown()
            logging.info("CAN bus shut down.")
        except Exception as e:
            logging.error(f"Error shutting down CAN bus: {e}", exc_info=True)
            # Don't prevent window close, just log
    root.destroy() # Close the tkinter window

def update_status(text):
    """Updates the status bar label."""
    if root: # Check if root window exists
        status_var.set(text)
        root.update_idletasks() # Force GUI update

# --- GUI Setup ---
# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Main window
root = tk.Tk()
root.title("RNS-E CAN Sender (TV Mode) - Windows")

# Frame for buttons
button_frame = tk.Frame(root, padx=10, pady=10)
button_frame.pack(fill=tk.X, expand=False)

# Create buttons dynamically
row_num = 0
col_num = 0
max_cols = 2 # Arrange buttons in 2 columns
for i, (label, data) in enumerate(BUTTON_MESSAGES.items()):
    # Use lambda to capture the correct 'label' for each button's command
    button = tk.Button(button_frame, text=label, width=25, height=2, # Make buttons a bit larger
                       command=lambda l=label: send_can_message(l))
    button.grid(row=row_num, column=col_num, padx=5, pady=5, sticky="ew")

    # Configure column weights for resizing if needed (optional)
    button_frame.grid_columnconfigure(col_num, weight=1)

    col_num += 1
    if col_num >= max_cols:
        col_num = 0
        row_num += 1

# Status bar
status_var = tk.StringVar()
status_label = tk.Label(root, textvariable=status_var, relief=tk.SUNKEN, anchor=tk.W, padx=5)
status_label.pack(side=tk.BOTTOM, fill=tk.X)
update_status("Initializing...")

# Attempt initial CAN connection when GUI starts
initialize_can()

# Set the window close behavior
root.protocol("WM_DELETE_WINDOW", on_closing)

# Start the GUI event loop
root.mainloop()