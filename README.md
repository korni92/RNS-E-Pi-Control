# Crankshaft CAN Control for Audi RNS-E MMI & MFSW

## Purpose

This project allows controlling a Raspberry Pi running Crankshaft NG (an Android Auto & CarPlay head unit software - see [Crankshaft NG GitHub](https://github.com/opencardev/crankshaft)) using:
1.  MMI (Multi Media Interface) controls connected to an Audi RNS-E navigation unit.
2.  MFSW (Multi-Function Steering Wheel) controls.

Interactions are captured from the vehicle's Infotainment CAN bus (100 kbit/s) and translated into simulated keyboard presses using `pynput`. Crankshaft then interprets these key presses for navigation and control within Android Auto. This version also includes logic for automatic media pause/play based on RNS-E source changes.

This guide documents the steps to replicate the setup (Status: Beta 1.2 - MFSW & Auto Pause/Play added, using `pynput`).

## How it Works

1.  **MMI/MFSW Input:** User interacts with RNS-E MMI or MFSW controls.
2.  **CAN Message:** The RNS-E or MFSW interface sends specific CAN messages (e.g., ID `0x461` for MMI, `0x5C3` for MFSW).
3.  **CAN HAT Reception:** A Raspberry Pi equipped with an MCP2515-based CAN HAT receives these messages via the `can0` interface.
4.  **Python Script:** `can_keyboard_control.py` listens for these specific CAN IDs.
5.  **Message Parsing:** The script decodes data bytes to identify the specific action, supporting short and long presses for MMI buttons and the MFSW "Mode" button.
6.  **Keyboard Simulation:** Uses the `pynput` library to simulate corresponding keyboard presses.
7.  **Crankshaft Control:** Crankshaft (and the underlying Android Auto session) receives the simulated key press and performs the associated action.
8.  **Auto Pause/Play:** The script also listens to RNS-E source changes (ID `0x661`) to automatically simulate media play/pause commands.

## Requirements

### Hardware

* Raspberry Pi (3B+ or 4 recommended) running Crankshaft NG.
* MCP2515-based CAN HAT (e.g., PiCAN2, Waveshare CAN HAT, or similar).
* Audi RNS-E Navigation Unit and/or MFSW.
* Connection to the Audi Infotainment CAN bus (100 kbit/s). Typically available at the RNS-E Quadlock Connector D, Pin 9 (CAN-H) and Pin 10 (CAN-L).
* Appropriate wiring and power supplies.

### Software

* Crankshaft NG distribution installed on the Raspberry Pi SD card (configured to run under an **X11 server environment** for `pynput` to work).
* SSH access or a terminal on the Raspberry Pi.
* Required system packages: `can-utils`, `python3`, `python3-pip`.
* Required Python libraries: `python-can`, `pynput`.

## Setup Instructions

**Step 1: Connect Hardware & Enable SPI**

1.  Shut down the Raspberry Pi and correctly install the CAN HAT onto the GPIO pins.
2.  Connect the CAN HAT's CAN-H and CAN-L terminals to the vehicle's/RNS-E's Infotainment CAN bus (100 kbit/s). Ensure correct polarity. Verify bus termination (usually, the termination jumper/switch on the HAT should be **OFF** as the RNS-E and Instrument Cluster often provide termination).
3.  Power up the Raspberry Pi.
4.  Open a terminal and run `sudo raspi-config`.
5.  Navigate to `Interface Options` -> `SPI`.
6.  Select `Yes` to enable the SPI interface.
7.  Exit `raspi-config`.

**Step 2: Configure Device Tree Overlay (DTO)**

1.  Make the boot partition writable. The mount point is usually `/boot/firmware` on newer RPi OS (like Bullseye or later) or `/boot` on older ones.
    ```bash
    # Try this first for newer systems:
    sudo mount -o remount,rw /boot/firmware
    # If the above fails with "not mounted" or similar, try:
    # sudo mount -o remount,rw /boot
    ```
    *Only one of these commands will typically succeed and be necessary, depending on your OS setup.*
2.  Edit the boot configuration file (use the path that corresponds to your system, usually the one from the successful mount command):
    ```bash
    sudo nano /boot/firmware/config.txt
    # OR, for older systems:
    # sudo nano /boot/config.txt
    ```
3.  Add the following lines at the end of the file.
    **IMPORTANT:** Replace `YOUR_OSCILLATOR_HZ` with the frequency of the crystal oscillator on your specific HAT in Hz (e.g., `8000000` for 8MHz, `12000000` for 12MHz, or `16000000` for 16MHz) and `YOUR_INTERRUPT_PIN` with the GPIO pin number your HAT uses for interrupts (often `25`). **Consult your HAT's documentation for these values! Incorrect values will cause CAN errors.**
    ```text
    # --- MCP2515 CAN HAT ---
    dtparam=spi=on
    dtoverlay=mcp2515-can0,oscillator=YOUR_OSCILLATOR_HZ,interrupt=YOUR_INTERRUPT_PIN,spimaxfrequency=1000000
    ```
4.  Save the file and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).
5.  Make the boot partition read-only again:
    ```bash
    # Use the same mount point as in substep 1
    sudo mount -o remount,ro /boot/firmware
    # OR
    # sudo mount -o remount,ro /boot
    ```
6.  Reboot the Raspberry Pi for the changes to take effect:
    ```bash
    sudo reboot
    ```

**Step 3: Install Software Dependencies**

1.  Enable Write Access to the root filesystem (Crankshaft NG often uses a read-only rootfs):
    ```bash
    sudo mount -o remount,rw /
    ```
2.  Update Package Lists:
    ```bash
    sudo apt update
    ```
3.  Install Required System Packages:
    ```bash
    sudo apt install can-utils python3-pip
    ```
4.  Install Python Libraries (system-wide):
    ```bash
    sudo python3 -m pip install --upgrade pip
    sudo python3 -m pip install --force-reinstall python-can pynput
    ```

**Step 4: Create Python Control Script**

1.  Create the script file in the `pi` user's home directory:
    ```bash
    nano /home/pi/can_keyboard_control.py
    ```
2.  Paste the Python code into the editor:
    
3.  Save (`Ctrl+O`, `Enter`) and exit (`Ctrl+X`).

**Step 5: Correct Script Ownership**

```bash
sudo chown pi:pi /home/pi/can_keyboard_control.py
```

**Step 6: Create/Verify systemd Service for CAN Interface (`configure-can0.service`)**

1.  File: `/etc/systemd/system/configure-can0.service`
    ```bash
    # sudo mount -o remount,rw / # If needed
    sudo nano /etc/systemd/system/configure-can0.service
    ```
2.  Content:
    ```ini
    [Unit]
    Description=Configure can0 Interface (100kbit/s)
    After=network.target network-online.target
    Wants=network-online.target

    [Service]
    Type=oneshot
    RemainAfterExit=yes
    ExecStart=/sbin/ip link set can0 up type can bitrate 100000

    [Install]
    WantedBy=multi-user.target
    ```
3.  Save and exit.

**Step 7: Create/Verify systemd Service for Python Script (`can-keyboard.service`)**

1.  File: `/etc/systemd/system/can-keyboard.service`
    ```bash
    # sudo mount -o remount,rw / # If needed
    sudo nano /etc/systemd/system/can-keyboard.service
    ```
2.  Content (paths for `pi` user):
    ```ini
    [Unit]
    Description=CAN Keyboard Control Service (Audi RNS-E & MFSW)
    Requires=configure-can0.service
    After=configure-can0.service graphical.target

    [Service]
    User=pi
    Group=pi
    WorkingDirectory=/home/pi/
    Environment="DISPLAY=:0"
    Environment="XAUTHORITY=/home/pi/.Xauthority"
    ExecStart=/usr/bin/python3 /home/pi/can_keyboard_control.py
    Restart=on-failure
    RestartSec=5

    [Install]
    WantedBy=graphical.target
    ```
3.  Save and exit.

**Step 8: Finalize systemd Configuration**

```bash
sudo systemctl daemon-reload
sudo systemctl enable configure-can0.service
sudo systemctl enable can-keyboard.service
# Optional: sudo systemctl is-enabled configure-can0.service can-keyboard.service
```

**Step 9: Reboot & Verify**

```bash
sudo reboot
```
After reboot:
* Check CAN interface: `ip -details link show can0` (expect UP, 100kbit/s, minimal errors).
* Check Python service: `systemctl status can-keyboard.service` (expect active/running).
* Check script logs for detailed messages: `journalctl -u can-keyboard.service` or `journalctl -f -u can-keyboard.service` for live view.

**Step 10: Functional Test**

* Test MMI controls (short/long press, scroll).
* Test MFSW controls (scroll, mode short/long press).
* Test Auto Pause/Play by changing RNS-E source to/from TV/Video.

**Step 11: Set Filesystem Read-Only (Crucial for in-car stability)**

Once everything is confirmed working:
```bash
sudo mount -o remount,ro /
# If you also remounted /boot or /boot/firmware for config.txt changes:
# sudo mount -o remount,ro /boot/firmware
```

**Step 12: Composite Video Output (TV-Out)**

To use the 3.5mm jack for composite video output, ensure your `/boot/firmware/config.txt` (or `/boot/config.txt`) includes the following. These settings are configured for PAL.
**Remember to replace `YOUR_OSCILLATOR_HZ` and `YOUR_INTERRUPT_PIN` in the CAN HAT DTO line (Step 2) with values matching your HAT.**
```text
# --- Composite Video Output Settings (for RNS-E PAL) ---
enable_tvout=1
sdtv_mode=2         # PAL for Europe/RNS-E
sdtv_aspect=3       # 16:9 for RNS-E widescreen
# This overlay helps prioritize composite output with the FKMS driver
dtoverlay=vc4-fkms-v3d,composite=1
# Optional: To prevent HDMI from interfering if a cable is connected
# hdmi_ignore_hotplug=1
```
*A reboot is required after changes to `config.txt`.*

**Step 13: Adjust Overscan Settings (Video Positioning)**

If the composite video image is shifted or has incorrect borders:
1.  Make the boot partition writable (see Step 2.1).
2.  Edit `/boot/firmware/config.txt` (or `/boot/config.txt`):
    ```bash
    sudo nano /boot/firmware/config.txt # Or /boot/config.txt
    ```
3.  Adjust overscan settings:
    * Ensure `disable_overscan=1` is **commented out** or set to `disable_overscan=0`.
    * Experiment with `overscan_left`, `overscan_right`, `overscan_top`, `overscan_bottom`.
        * **Positive values** increase the black border on that side (pushes image away from edge).
        * **Negative values** decrease the black border (pulls image towards edge).
        * Example: If picture is shifted left, try `overscan_left=16` or `overscan_right=-16`.
        * Start with increments of 8 or 16.
4.  **Save and Reboot** after each change to see the effect. Repeat until centered.
5.  Set boot partition back to read-only (see Step 2.5).

## Configuration (in `/home/pi/can_keyboard_control.py`)

The Python script is configurable via constants at the top:

* **CAN IDs:**
    * `TARGET_CAN_ID_MMI`: For RNS-E MMI knob/buttons (default `0x461`).
    * `TARGET_CAN_ID_MFSW`: For Steering Wheel Controls (default `0x5C3` - **verify for your car model!**).
    * `TARGET_CAN_ID_SOURCE`: For RNS-E source status (default `0x661` - **verify for your RNS-E!**).
* **Timings:**
    * `COOLDOWN_PERIOD`: Prevents MMI button (non-scroll) repeats if held or pressed rapidly.
    * `LONG_PRESS_THRESHOLD`: Number of "press" CAN messages received to distinguish short vs. long press for MMI and MFSW Mode button.
* **Key Mappings (using `pynput` syntax):**
    * `MMI_SCROLL_COMMANDS`: MMI command tuples that are exempt from cooldown/long-press logic (for knob rotation).
    * `COMMAND_TO_KEY_SHORT_MMI`: Actions for short MMI button presses.
    * `COMMAND_TO_KEY_LONG_MMI`: Actions for long MMI button presses (**USER MUST DEFINE THESE**).
    * `MFSW_SCROLL_UP_KEY`, `MFSW_SCROLL_DOWN_KEY`, `MFSW_MODE_SHORT_PRESS_KEY`, `MFSW_MODE_LONG_PRESS_KEY`: Actions for MFSW buttons (**USER MUST DEFINE THESE** and verify MFSW CAN data).
* **RNS-E Source Data:**
    * `VIDEO_SOURCE_DATA_1`, `VIDEO_SOURCE_DATA_2`: Byte sequences identifying the RNS-E's TV/Video input for auto pause/play. (**VERIFY FOR YOUR RNS-E!**).

## Troubleshooting / Important Notes

* **X11 Dependency for `pynput`:** This script, due to `pynput`'s keyboard controller backend on Linux, **requires an X11 server to be running** (e.g., Crankshaft configured in X11 mode, not pure EGL/KMS). The `DISPLAY=:0` environment variable in the service file must point to this active X server. If Crankshaft runs without X11, the Python script will likely fail during `pynput` initialization with an "failed to acquire X connection" error. For non-X11 environments, input simulation via the kernel's `uinput` module (e.g., with the `python-uinput` library) would be a more robust alternative but requires significant script changes.
* **CAN Errors (`ERROR-ACTIVE`/`ERROR-PASSIVE` on `can0`):** If `ip -details link show can0` shows persistent error states, double-check:
    * The `oscillator` and `interrupt` values in `/boot/firmware/config.txt` **exactly** match your CAN HAT specifications. This is critical for correct bit timing.
    * The CAN bus **termination** is correct (~60 Ohms between CAN-H and CAN-L when the system is powered down). Ensure the Pi HAT's termination jumper/switch is disabled unless it's at a physical end of the bus.
    * The **bitrate** (100000) is correct for all devices on the Infotainment CAN bus.
    * Wiring, especially **CAN-H/CAN-L polarity** and a **common ground (GND)** connection between the Pi's CAN HAT and the vehicle/RNS-E system.
* **MFSW & RNS-E Source CAN Data:** The specific CAN messages (IDs and data bytes) for MFSW buttons and RNS-E source status can vary between car models, model years, and RNS-E firmware versions. Use `candump can0` while operating these controls/changing sources to identify the correct patterns for *your* specific vehicle and RNS-E unit. Adjust the constants and logic in the Python script accordingly.
* **Permissions:** The `can-keyboard.service` runs as user `pi`. Ensure this user has necessary permissions if any paths or resources outside its home directory are accessed by the script. The `pi` user typically needs to be in the `input` group for `uinput`-based solutions (not currently used, but relevant if changing input method).

## Disclaimer

Modifying vehicle CAN bus systems carries inherent risks. Ensure you fully understand the implications before connecting any custom hardware or sending messages. Incorrect connections or CAN messages could potentially interfere with vehicle operation or damage components. This project is for experimental and educational purposes. Use at your own risk.

---
