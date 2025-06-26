# RNS-E Pi Control for Crankshaft-NG

**Last Updated: June 26, 2025**

A comprehensive suite of `systemd` services designed to deeply integrate a Raspberry Pi (running Crankshaft or a similar OS) with an Audi RNS-E head unit and the car's CAN bus.

This project goes beyond simple button presses, providing robust, persistent features that make the Raspberry Pi feel like a native part of the vehicle's electronics ecosystem. It is designed for stability, efficiency, and safety, with a focus on protecting your SD card and ensuring predictable behavior.

  
*(Suggestion: Replace with a photo of your project in action\!)*

## Key Features

  - **Seamless Multimedia Control:** Translate MMI and steering wheel controls (MFSW) into virtual keyboard presses (`uinput`) to control your media player.
  - **Automatic Day/Night Mode:** Listens to the car's light sensor to automatically switch the Crankshaft UI theme. Includes a configurable cooldown period to prevent UI "flickering" from bouncing CAN signals.
  - **Safe Auto-Shutdown:** Intelligently detects when the ignition is off or the key is pulled, waits for a configurable delay, and then safely shuts down the Raspberry Pi to prevent battery drain.
  - **System Time Synchronization:** Automatically sets the Raspberry Pi's system clock based on the time broadcast by the car's instrument cluster.
  - **Custom FIS Display Text:** Send custom two-line messages to the instrument cluster display (FIS).
  - **TV-Tuner Simulation:** Emulates the presence of a factory TV Tuner, unlocking the video input on the RNS-E for sources like a backup camera.
  - **Robust Service Management:** Uses a hardened, multi-service `systemd` setup with strict dependencies, ensuring services start in the correct order and are managed properly.
  - **SD Card Protection:** Utilizes RAM-based temporary directories (`tmpfs`) for logs and communication sockets to minimize writes to the SD card.

## Prerequisites

### Hardware

  * Raspberry Pi (3B+ or newer recommended).
  * An operating system like Crankshaft NG, or a base Raspberry Pi OS.
  * A CAN-HAT based on the MCP2515 chipset.
  * A quality SD Card.

### Software

  * Python 3
  * `git` and `pip`

## Manual Installation Guide

This guide provides the step-by-step commands to install and configure the entire project, giving you full control and insight into the setup.

### Step 1: System Preparation

If your filesystem is read-only, make it writable to install software and create configuration files.

```bash
sudo mount -o remount,rw /
sudo mount -o remount,rw /boot
```

### Step 2: Install System Dependencies

Install the core packages required by the operating system via `apt-get`.

```bash
# Update the package list first
sudo apt-get update

# Install all required packages
sudo apt-get install -y git python3-pip can-utils python3-unidecode python3-zmq python3-uinput
```

### Step 3: Grant Permissions for Virtual Keyboard

The `uinput` library needs special permissions to create a virtual keyboard.

1.  **Add `pi` User to `input` Group:** This gives the user the right to handle input devices.

    ```bash
    sudo usermod -a -G input pi
    ```

    **Note:** A full reboot is required for this group change to apply.

2.  **Create a `udev` Rule:** This ensures the `/dev/uinput` device has the correct permissions every time the system boots.

    ```bash
    sudo nano /etc/udev/rules.d/99-uinput.rules
    ```

    Paste the following single line into the file:

    ```
    KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
    ```

    Save and exit (`Ctrl+X`, `Y`, `Enter`).

### Step 4: Download Project Files

Clone the project repository from GitHub into the `pi` user's home directory.

```bash
cd /home/pi
git clone https://github.com/korni92/RNS-E-Pi-Control.git
sudo chown -R pi:pi /home/pi/RNS-E-Pi-Control
```

### Step 5: Install Python Dependencies

Install the remaining Python libraries using `pip`.

```bash
sudo pip3 install python-can pyserial pytz
```

### Step 6: Configure CAN-HAT (`/boot/config.txt`)

Tell the Raspberry Pi how to communicate with your CAN-HAT.

```bash
sudo nano /boot/config.txt
```

Add the following lines. **You must replace `12000000` and `25` with the correct `oscillator` and `interrupt` values for your specific CAN-HAT\!**

```ini
# --- RNS-E Pi Control CAN HAT ---
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25,spimaxfrequency=1000000
```

Save and exit.

### Step 7: Create Custom Temporary Directories

To protect your SD card, create directories in RAM for logs and the communication socket.

1.  **Create the mount points:**
    ```bash
    sudo mkdir -p /var/log/rnse_control /run/rnse_control
    sudo chown pi:pi /run/rnse_control
    ```
2.  **Open `/etc/fstab`** to make these directories permanent RAM-disks:
    ```bash
    sudo nano /etc/fstab
    ```
    Add these two lines at the end. Note the `uid` and `gid` options, which are crucial for giving the `pi` user permission to write logs.
    ```fstab
    tmpfs   /var/log/rnse_control   tmpfs   defaults,noatime,nosuid,nodev,uid=pi,gid=pi,size=16m 0 0
    tmpfs   /run/rnse_control       tmpfs   defaults,noatime,nosuid,uid=pi,gid=pi,mode=0755,size=2m 0 0
    ```
    Save and exit.

### Step 8: Create Project Configuration

Copy the example configuration file and edit it to match your setup.

```bash
# Copy the template to the user's home directory
cp /home/pi/RNS-E-Pi-Control/config.json /home/pi/config.json

# Open the new configuration file for editing
nano /home/pi/config.json
```

**Crucially, adjust the following settings:**

  - `can_interface`: Should be `can0`.
  - `car_time_zone`: Set to your local time zone (e.g., `"Europe/Berlin"`).
  - `thresholds.daynight_cooldown_seconds`: Set to `10` to prevent screen flickering.
  - Review other features and enable/disable them as desired.

### Step 9: Create Hardened Systemd Services

Create the five service files with robust dependencies. These have been optimized for reliability.

**File 1: `/etc/systemd/system/configure-can0.service`**

```bash
sudo nano /etc/systemd/system/configure-can0.service
```

\<details\>
\<summary\>Click to view content\</summary\>

```ini
[Unit]
Description=Configure can0 Interface
Wants=network.target
After=network.target

[Service]
Type=oneshot
ExecStart=/sbin/ip link set can0 up type can bitrate 100000
RemainAfterExit=true

[Install]
WantedBy=multi-user.target
```

\</details\>

**File 2: `/etc/systemd/system/can-handler.service`**

```bash
sudo nano /etc/systemd/system/can-handler.service
```

\<details\>
\<summary\>Click to view content\</summary\>

```ini
[Unit]
Description=RNS-E CAN-Bus Handler
Requires=configure-can0.service
After=configure-can0.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/RNS-E-Pi-Control/can_handler.py
WorkingDirectory=/home/pi/RNS-E-Pi-Control
User=pi
Group=pi
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

\</details\>

**File 3: `/etc/systemd/system/crankshaft-features.service`**

```bash
sudo nano /etc/systemd/system/crankshaft-features.service
```

\<details\>
\<summary\>Click to view content\</summary\>

```ini
[Unit]
Description=RNS-E Crankshaft CAN Features
Requires=can-handler.service
After=can-handler.service
BindsTo=can-handler.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/RNS-E-Pi-Control/crankshaft_can_features.py
WorkingDirectory=/home/pi/RNS-E-Pi-Control
User=pi
Group=pi
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

\</details\>

**File 4: `/etc/systemd/system/keyboard-control.service`**

```bash
sudo nano /etc/systemd/system/keyboard-control.service
```

\<details\>
\<summary\>Click to view content\</summary\>

```ini
[Unit]
Description=RNS-E CAN-Bus Keyboard Simulation (uinput)
Requires=can-handler.service
After=can-handler.service
BindsTo=can-handler.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/RNS-E-Pi-Control/can_keyboard_control.py
WorkingDirectory=/home/pi/RNS-E-Pi-Control
User=pi
Group=input
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

\</details\>

**File 5: `/etc/systemd/system/fis-writer.service`**

```bash
sudo nano /etc/systemd/system/fis-writer.service
```

\<details\>
\<summary\>Click to view content\</summary\>

```ini
[Unit]
Description=RNS-E FIS Display Writer
Requires=can-handler.service
After=can-handler.service
BindsTo=can-handler.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/RNS-E-Pi-Control/can_fis_writer.py
WorkingDirectory=/home/pi/RNS-E-Pi-Control
User=pi
Group=pi
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

\</details\>

### Step 10: Finalize and Reboot

Enable the new services to start on boot and reboot the system for all changes to take effect.

```bash
# Reload the systemd manager to read the new service files
sudo systemctl daemon-reload

# Enable all 5 services to start on boot
sudo systemctl enable configure-can0.service can-handler.service crankshaft-features.service keyboard-control.service fis-writer.service

# Reboot the Raspberry Pi
sudo reboot
```

After the reboot, your system is fully installed and operational\!

## Usage and Management

Here are the essential commands for managing your new services.

**Check the status of all services at once:**

```bash
systemctl status configure-can0.service can-handler.service crankshaft-features.service keyboard-control.service fis-writer.service
```

**Start or stop all services manually:**

```bash
# To Start
sudo systemctl start can-handler.service crankshaft-features.service keyboard-control.service fis-writer.service

# To Stop
sudo systemctl stop can-handler.service crankshaft-features.service keyboard-control.service fis-writer.service
```

*(Note: `configure-can0` is a one-shot service and doesn't need to be started/stopped manually after the first boot.)*

## Troubleshooting

If a service fails to start, the first place to look is its detailed log using `journalctl`.

**Example:** To debug the keyboard service:

```bash
journalctl -u keyboard-control.service
```

Look for `Traceback` errors, `Permission denied`, or `No such file or directory`. These will tell you exactly what is wrong. Common issues include typos in file paths or incorrect permissions.
