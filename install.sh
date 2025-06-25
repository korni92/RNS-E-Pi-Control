#!/bin/bash

# ==============================================================================
# RNS-E Pi Control - Management Script (v3.7 - Hardened Services)
# ==============================================================================
# This script installs, checks, updates, and verifies the CAN-Bus control
# scripts. This version includes hardened systemd dependencies and fixes
# for verification logic and boot-time race conditions.
#
# USAGE:
#   sudo ./install.sh         (To run the main installation menu)
#   sudo ./install.sh --verify  (To run post-reboot verification checks)
#   sudo ./install.sh --update  (To update scripts from GitHub)
# ==============================================================================

# --- Script Setup ---
set -e

# --- Variables ---
REPO_URL="https://github.com/korni92/RNS-E-Pi-Control.git"
REPO_DIR="/home/pi/RNS-E-Pi-Control"
CONFIG_FILE="/home/pi/config.json"
SERVICES=("configure-can0" "can-handler" "crankshaft-can-features" "can-keyboard" "can-fis-writer")
PYTHON_SERVICES=("can-handler" "crankshaft-can-features" "can-keyboard" "can-fis-writer")

# --- Helper Functions ---
function print_header() {
    echo "====================================================="
    echo "    RNS-E Pi Control - Management Script           "
    echo "====================================================="
    echo
}

function ask_yes_no() {
    while true; do
        read -p "$1 [y/n]: " yn
        case $yn in
            [Yy]* ) return 0;;
            [Nn]* ) return 1;;
            * ) echo "Please answer yes or no.";;
        esac
    done
}

# --- Function to check and install system packages ---
function install_system_packages() {
    local packages_to_install=()
    local required_packages=("git" "python3-pip" "can-utils" "python3-unidecode" "python3-zmq" "python3-uinput")
    echo "--> Checking for required system packages..."
    for pkg in "${required_packages[@]}"; do
        if ! dpkg -s "$pkg" &> /dev/null; then
            packages_to_install+=("$pkg")
        fi
    done
    if [ ${#packages_to_install[@]} -ne 0 ]; then
        echo "--> Installing missing system packages: ${packages_to_install[*]}"
        apt-get update
        apt-get install -y "${packages_to_install[@]}"
    else
        echo "--> All required system packages are already installed."
    fi
}

# --- Smart function to handle /boot/config.txt ---
function configure_boot_config() {
    echo "--> Configuring /boot/config.txt..."
    local config_path="/boot/config.txt"
    local proceed_mcp=true
    if grep -q "dtoverlay=mcp2515-can0" "$config_path"; then
        echo "An existing MCP2515 configuration was found:"
        grep --color=always "dtoverlay=mcp2515-can0" "$config_path"
        if ! ask_yes_no "Do you want this script to manage and potentially overwrite it?"; then
            proceed_mcp=false
        fi
    fi
    if [[ "$proceed_mcp" = true ]]; then
        echo "--> Configuring MCP2515 settings..."
        sed -i "/# --- RNS-E Pi Control CAN HAT ---/,/dtoverlay=mcp2515-can0/d" "$config_path"
        if ! grep -q "^dtparam=spi=on" "$config_path"; then
            sed -i "/^\[all\]/i dtparam=spi=on\n" "$config_path"
        fi
        read -p "Enter oscillator frequency in Hz for your CAN-HAT (e.g., 12000000): " OSC_HZ
        read -p "Enter the interrupt GPIO pin for your CAN-HAT (e.g., 25): " INT_PIN
        local new_overlay_line="dtoverlay=mcp2515-can0,oscillator=${OSC_HZ},interrupt=${INT_PIN},spimaxfrequency=1000000"
        if grep -q "^\[all\]" "$config_path"; then
            sed -i "/^\[all\]/i # --- RNS-E Pi Control CAN HAT ---\n${new_overlay_line}\n" "$config_path"
        else
            { echo ""; echo "# --- RNS-E Pi Control CAN HAT ---"; echo "${new_overlay_line}"; } >> "$config_path"
        fi
        echo "CAN-HAT configuration has been written."
    else
        echo "--> Skipping MCP2515 configuration as requested."
    fi
}

# --- Core Management Functions ---

function check_service_status() {
    local service=$1
    local feature_name=""
    local feature_enabled="true"

    case "$service" in
        "can-keyboard") feature_name="mmi_controls";;
        "crankshaft-can-features") feature_name="day_night_mode";;
        "can-fis-writer") feature_name="fis_display";;
    esac

    if [[ -n "$feature_name" && -f "$CONFIG_FILE" ]]; then
        feature_enabled=$(python3 -c "
import json, sys
try:
    with open('$CONFIG_FILE') as f:
        config = json.load(f)
    features = config.get('features', {})
    feature_value = features.get('$feature_name')
    if isinstance(feature_value, dict):
        print(feature_value.get('enabled', False))
    elif isinstance(feature_value, bool):
        print(feature_value)
    else:
        print(False)
except (json.JSONDecodeError, FileNotFoundError):
    print(False)
")
    fi

    printf "%-30s" "$service:"
    if [ ! -f "/etc/systemd/system/${service}.service" ]; then
        echo -e "\e[31m [FILE NOT FOUND]\e[0m"; return;
    fi
    if ! systemctl is-enabled --quiet "$service"; then
        echo -e "\e[33m [DISABLED]\e[0m"; return;
    fi
    printf "[ENABLED]"

    if [[ "$feature_enabled" != "True" && "$feature_name" != "" ]]; then
         echo -e "\e[34m [INACTIVE (Feature Disabled)]\e[0m"
    elif systemctl is-active --quiet "$service"; then
        echo -e "\e[32m [ACTIVE (running)]\e[0m"
    else
        if systemctl is-failed --quiet "$service"; then
            echo -e "\e[31m [FAILED]\e[0m - Check logs with 'journalctl -u ${service}'"
        else
            echo -e "\e[33m [INACTIVE]\e[0m"
        fi
    fi
}

function run_verification() {
    print_header
    echo "Starting Post-Reboot Verification..."
    echo -e "\n--- 1. Checking Permissions ---"
    if [ -c "/dev/uinput" ]; then echo "[OK] /dev/uinput device exists."; else echo "[FAIL] /dev/uinput device not found."; fi
    if getent group input | grep -q "\bpi\b"; then echo "[OK] User 'pi' is in the 'input' group."; else echo "[FAIL] User 'pi' not in 'input' group."; fi
    echo -e "\n--- 2. Checking CAN Interface (can0) ---"
    if ip link show can0 &>/dev/null; then
        if ip -details link show can0 | grep -q "state UP"; then echo "[OK] Interface 'can0' is UP.";
        elif ip -details link show can0 | grep -q "state BUS-OFF"; then echo "[FAIL] Interface 'can0' is in BUS-OFF state.";
        else echo "[WARN] Interface 'can0' not fully UP."; fi
    else echo "[FAIL] Interface 'can0' not found."; fi
    echo -e "\n--- 3. Checking Systemd Services ---"
    for service in "${SERVICES[@]}"; do check_service_status "$service"; done
    echo -e "\n--- 4. Live CAN Data Test ---"
    echo "Listening for ANY CAN traffic for 5 seconds..."
    if timeout 5 candump -L can0 > /dev/null; then echo "[OK] Live data detected on the CAN bus.";
    else echo "[FAIL] No traffic detected. Is the car's ignition on? Check wiring."; fi
    echo -e "\nVerification complete!"
}

function update_scripts() {
    print_header
    echo "--> Updating scripts from GitHub..."
    if [ ! -d "$REPO_DIR/.git" ]; then echo "ERROR: Not a git repository."; return 1; fi
    cd "$REPO_DIR"
    if [ -n "$(git status --porcelain)" ]; then echo "WARNING: Discarding local changes."; git reset --hard HEAD; fi
    git pull && chown -R pi:pi "$REPO_DIR"
    [ -f "$REPO_DIR/requirements.txt" ] && pip3 install -r "$REPO_DIR/requirements.txt"
    echo "--> Restarting services to apply updates..."
    systemctl restart "${PYTHON_SERVICES[@]}"
    echo -e "\nUpdate complete! Run 'sudo ./install.sh --verify' to check status."
}

# --- Full Installation Process ---
function install_all() {
    print_header
    if ! ask_yes_no "This will perform a full installation. Continue?"; then echo "Installation aborted."; exit 0; fi
    echo "--> Remounting filesystems as read-write..."
    mount -o remount,rw / && mount -o remount,rw /boot
    install_system_packages
    echo "--> Granting permissions for uinput device..."
    usermod -a -G input pi && echo "User 'pi' added to 'input' group."
    echo "--> Creating udev rule for /dev/uinput..."
    cat <<EOF > /etc/udev/rules.d/99-uinput.rules
KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
EOF
    echo "--> Cloning repository..."
    [ -d "$REPO_DIR" ] && rm -rf "$REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR" && chown -R pi:pi "$REPO_DIR"
    echo "--> Installing Python dependencies..."
    [ -f "$REPO_DIR/requirements.txt" ] && pip3 install -r "$REPO_DIR/requirements.txt"
    echo "--> Configuring fstab for custom temporary directories..."
    sed -i "/^tmpfs \( \/tmp \| \/var\/log \| \/home\/pi \) tmpfs/d" /etc/fstab
    grep -qF "tmpfs /var/log/rnse_control" /etc/fstab || echo "tmpfs /var/log/rnse_control tmpfs defaults,noatime,nosuid,nodev,size=16m 0 0" >> /etc/fstab
    grep -qF "tmpfs /run/rnse_control" /etc/fstab || echo "tmpfs /run/rnse_control tmpfs defaults,noatime,nosuid,uid=pi,gid=pi,mode=0755,size=2m 0 0" >> /etc/fstab
    echo "--> Creating custom temporary directories..."
    mkdir -p /var/log/rnse_control /run/rnse_control && chown pi:pi /run/rnse_control
    echo "--> Activating new fstab mounts..."
    mount /var/log/rnse_control && mount /run/rnse_control
    configure_boot_config
    echo "--> Starting Interactive Configuration..."
    cp "$REPO_DIR/config.json" "$CONFIG_FILE"
    echo "--> Adjusting ZMQ communication path in config.json..."
    sed -i 's|ipc:///tmp/can_stream.ipc|ipc:///run/rnse_control/can_stream.ipc|g' "$CONFIG_FILE"
    read -p "Enter your time zone (e.g., Europe/Berlin): " TIME_ZONE
    sed -i "s|\"car_time_zone\": \".*\"|\"car_time_zone\": \"${TIME_ZONE}\"|" "$CONFIG_FILE"
    chown pi:pi "$CONFIG_FILE" && echo "Configuration saved to $CONFIG_FILE."

    function create_systemd_services() {
        echo "--> Creating and configuring systemd services (uinput version)..."
        # Main CAN interface setup
        cat <<EOF > /etc/systemd/system/configure-can0.service
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
EOF
        # Main CAN data publisher
        cat <<EOF > /etc/systemd/system/can-handler.service
[Unit]
Description=RNS-E CAN-Bus Handler
Requires=configure-can0.service
After=configure-can0.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/can_handler.py
Restart=always
RestartSec=10
User=pi
[Install]
WantedBy=multi-user.target
EOF
        # Subscriber services with hardened dependencies
        cat <<EOF > /etc/systemd/system/crankshaft-can-features.service
[Unit]
Description=RNS-E Crankshaft CAN Features
Requires=can-handler.service
After=can-handler.service
BindsTo=can-handler.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/crankshaft_can_features.py
Restart=always
RestartSec=10
User=pi
[Install]
WantedBy=multi-user.target
EOF
        cat <<EOF > /etc/systemd/system/can-keyboard.service
[Unit]
Description=RNS-E CAN-Bus Keyboard Simulation (uinput)
Requires=can-handler.service
After=can-handler.service
BindsTo=can-handler.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/can_keyboard_control.py
Restart=always
RestartSec=10
User=pi
Group=input
[Install]
WantedBy=multi-user.target
EOF
        cat <<EOF > /etc/systemd/system/can-fis-writer.service
[Unit]
Description=RNS-E FIS Display Writer
Requires=can-handler.service
After=can-handler.service
BindsTo=can-handler.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/can_fis_writer.py
Restart=on-failure
RestartSec=10
User=pi
[Install]
WantedBy=multi-user.target
EOF
    }

    create_systemd_services
    echo "--> Finalizing installation..."
    systemctl daemon-reload && systemctl enable "${SERVICES[@]}"
    echo -e "\n===========================================\n    INSTALLATION COMPLETE! (uinput version)\n===========================================\n"
    if ask_yes_no "A reboot is required for all changes to take effect. Reboot now?"; then
        echo "Rebooting..." && reboot
    else
        echo "Please reboot manually ('sudo reboot'). After reboot, run 'sudo ./install.sh --verify' to test."
    fi
}

# --- Script Entry Point ---
if [ "$EUID" -ne 0 ]; then echo "ERROR: This script must be run as root."; exit 1; fi
case "$1" in
    --verify) run_verification; exit 0;;
    --update) update_scripts; exit 0;;
esac
while true; do
    print_header
    echo "Please choose an option:"
    echo "  1) Install or Re-install Everything"
    echo "  2) Check System Status (Verification)"
    echo "  3) Update Scripts from GitHub"
    echo "  4) Exit"
    read -p "Enter your choice [1-4]: " choice
    case $choice in
        1) install_all; break;;
        2) run_verification; read -p "Press [Enter] to return...";;
        3) update_scripts; read -p "Press [Enter] to return...";;
        4) break;;
        *) echo "Invalid option.";;
    esac
done
echo "Exiting management script."
