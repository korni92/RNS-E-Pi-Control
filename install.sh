#!/bin/bash

# ==============================================================================
# RNS-E Pi Control - Management Script (v3.3 - Final with custom tmpfs)
# ==============================================================================
# This script installs, checks, updates, and verifies the CAN-Bus control
# scripts from the korni92/RNS-E-Pi-Control repository for a read-only
# Crankshaft OS environment. It includes an interactive configuration assistant.
#
# USAGE:
#   sudo ./install.sh         (To run the main installation menu)
#   sudo ./install.sh --verify  (To run post-reboot verification checks)
# ==============================================================================

# --- Script Setup ---
set -e

# --- Variables ---
REPO_URL="https://github.com/korni92/RNS-E-Pi-Control.git"
REPO_DIR="/home/pi/RNS-E-Pi-Control"
CONFIG_FILE="/home/pi/config.json"
SERVICES=("configure-can0" "can-handler" "crankshaft-can-features" "can-keyboard" "can-fis-writer")

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
    local required_packages=("git" "python3-pip" "can-utils" "python3-unidecode" "python3-zmq")

    echo "--> Checking for required system packages..."
    for pkg in "${required_packages[@]}"; do
        if ! dpkg -s "$pkg" &> /dev/null; then
            echo "    - Package '$pkg' is missing."
            packages_to_install+=("$pkg")
        else
            echo "    - Package '$pkg' is already installed."
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

# --- Core Management Functions ---

function check_service_status() {
    local service=$1
    printf "%-30s" "$service:"
    
    if [ ! -f "/etc/systemd/system/${service}.service" ]; then
        echo "[FILE NOT FOUND]"
        return
    fi

    if systemctl is-enabled --quiet "$service"; then
        printf "[ENABLED]"
    else
        printf "[DISABLED]"
    fi

    if systemctl is-active --quiet "$service"; then
        echo " [ACTIVE (running)]"
    else
        if systemctl is-failed --quiet "$service"; then
            echo " [FAILED] - Check logs with 'journalctl -u ${service}'"
        else
            echo " [INACTIVE (dead)]"
        fi
    fi
}

function check_system_status() {
    print_header
    echo "Performing system health check..."
    echo "-----------------------------------"
    echo "Checking Custom Mounts (/etc/fstab)..."
    grep -q "tmpfs /var/log/rnse_control" /etc/fstab && echo "[OK] /var/log/rnse_control mount found." || echo "[MISSING] /var/log/rnse_control mount."
    grep -q "tmpfs /run/rnse_control" /etc/fstab && echo "[OK] /run/rnse_control mount found." || echo "[MISSING] /run/rnse_control mount."
    
    echo -e "\nChecking Boot Configuration (/boot/config.txt)..."
    grep -q "dtparam=spi=on" /boot/config.txt && echo "[OK] SPI is enabled." || echo "[MISSING] SPI is not enabled."
    grep -q "dtoverlay=mcp2515-can0" /boot/config.txt && echo "[OK] MCP2515 overlay is configured." || echo "[MISSING] MCP2515 overlay."
    
    echo -e "\nChecking Script Files..."
    [ -d "$REPO_DIR" ] && echo "[OK] Repository directory exists." || echo "[MISSING] Repository directory."
    [ -f "$CONFIG_FILE" ] && echo "[OK] config.json exists." || echo "[MISSING] config.json."

    echo -e "\nChecking Systemd Services Status..."
    for service in "${SERVICES[@]}"; do
        check_service_status "$service"
    done
    
    echo -e "\nCheck complete."
}

# --- Full Installation Process ---
function install_all() {
    print_header
    echo "This script will perform a full installation and configuration."
    if ! ask_yes_no "This will overwrite existing configurations. Continue?"; then
        echo "Installation aborted."; exit 0;
    fi

    echo "--> Remounting filesystems as read-write..."
    mount -o remount,rw / && mount -o remount,rw /boot

    install_system_packages

    echo "--> Cloning repository from ${REPO_URL}..."
    [ -d "$REPO_DIR" ] && rm -rf "$REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR" && chown -R pi:pi "$REPO_DIR"
    
    echo "--> Installing Python dependencies from requirements.txt..."
    [ -f "$REPO_DIR/requirements.txt" ] && pip3 install -r "$REPO_DIR/requirements.txt"
    
    echo "--> Configuring fstab for custom temporary directories..."
    # Remove old, broad tmpfs mounts if they exist, just in case
    sed -i "/^tmpfs \/tmp tmpfs/d" /etc/fstab
    sed -i "/^tmpfs \/var\/log tmpfs/d" /etc/fstab
    sed -i "/^tmpfs \/home\/pi tmpfs/d" /etc/fstab
    
    # Add new, specific tmpfs mounts for our project only
    grep -qF "tmpfs /var/log/rnse_control" /etc/fstab || echo "tmpfs /var/log/rnse_control tmpfs defaults,noatime,nosuid,nodev,size=16m 0 0" >> /etc/fstab
    grep -qF "tmpfs /run/rnse_control" /etc/fstab || echo "tmpfs /run/rnse_control tmpfs defaults,noatime,nosuid,uid=pi,gid=pi,mode=0755,size=2m 0 0" >> /etc/fstab
    
    echo "--> Creating custom temporary directories..."
    mkdir -p /var/log/rnse_control
    mkdir -p /run/rnse_control
    chown pi:pi /run/rnse_control
    
    echo "--> Activating new fstab mounts..."
    mount /var/log/rnse_control
    mount /run/rnse_control
    
    echo "--> Configuring /boot/config.txt..."
    sed -i "/^# --- Added by RNS-E Pi Control Install Script ---.*/d" /boot/config.txt
    sed -i "/^dtparam=spi=on.*/d" /boot/config.txt
    sed -i "/^dtoverlay=mcp2515-can0.*/d" /boot/config.txt
    
    read -p "Enter oscillator frequency in Hz for MCP2515 (e.g., 8000000): " OSC_HZ
    read -p "Enter interrupt GPIO pin for MCP2515 (e.g., 25): " INT_PIN
    {
        echo "# --- Added by RNS-E Pi Control Install Script ---"
        echo "dtparam=spi=on"
        echo "dtoverlay=mcp2515-can0,oscillator=${OSC_HZ},interrupt=${INT_PIN},spimaxfrequency=1000000"
    } >> /boot/config.txt
    
    echo "--> Starting Interactive Configuration..."
    cp "$REPO_DIR/config.json" "$CONFIG_FILE"
    
    # IMPORTANT: Update ZMQ path in the new config file to use the new tmpfs directory
    echo "--> Adjusting ZMQ communication path in config.json..."
    sed -i 's|ipc:///tmp/can_stream.ipc|ipc:///run/rnse_control/can_stream.ipc|' "$CONFIG_FILE"

    read -p "Enter your time zone (e.g., Europe/Berlin): " TIME_ZONE
    sed -i "s|\"car_time_zone\": \".*\"|\"car_time_zone\": \"${TIME_ZONE}\"|" "$CONFIG_FILE"
    if ask_yes_no "Enable automatic Day/Night mode?"; then sed -i '/"day_night_mode":/s/false/true/' "$CONFIG_FILE"; fi
    if ask_yes_no "Enable automatic Time Sync?"; then sed -i '/"time_sync":/s/false/true/' "$CONFIG_FILE"; fi
    
    chown pi:pi "$CONFIG_FILE"
    echo "Configuration saved to $CONFIG_FILE."

    function create_systemd_services() {
        echo "--> Creating and configuring systemd services..."
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
        cat <<EOF > /etc/systemd/system/can-handler.service
[Unit]
Description=RNS-E CAN-Bus Handler
After=configure-can0.service
BindsTo=configure-can0.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/can_handler.py
WorkingDirectory=${REPO_DIR}
Restart=always
RestartSec=3
User=pi
[Install]
WantedBy=multi-user.target
EOF
        cat <<EOF > /etc/systemd/system/crankshaft-can-features.service
[Unit]
Description=RNS-E Crankshaft CAN Features
After=can-handler.service
Wants=can-handler.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/crankshaft_can_features.py
WorkingDirectory=${REPO_DIR}
Restart=always
RestartSec=3
User=pi
[Install]
WantedBy=multi-user.target
EOF
        cat <<EOF > /etc/systemd/system/can-keyboard.service
[Unit]
Description=RNS-E CAN-Bus Keyboard Simulation
After=can-handler.service
Wants=can-handler.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/can_keyboard.py
WorkingDirectory=${REPO_DIR}
Restart=always
RestartSec=3
User=pi
[Install]
WantedBy=multi-user.target
EOF
        cat <<EOF > /etc/systemd/system/can-fis-writer.service
[Unit]
Description=RNS-E FIS Display Writer
After=can-handler.service
Wants=can-handler.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/can_fis_writer.py
WorkingDirectory=${REPO_DIR}
Restart=always
RestartSec=3
User=pi
[Install]
WantedBy=multi-user.target
EOF
    }

    create_systemd_services
    
    echo "--> Finalizing installation..."
    systemctl daemon-reload && systemctl enable "${SERVICES[@]}"
    
    echo
    echo "==========================================="
    echo "        INSTALLATION COMPLETE!             "
    echo "==========================================="
    echo "A reboot is required for all changes to take effect."
    if ask_yes_no "Do you want to reboot now?"; then
        echo "Rebooting..." && reboot
    else
        echo "Please reboot manually ('sudo reboot')."
        echo "After reboot, run 'sudo ./install.sh --verify' to test."
    fi
}

# --- Script Entry Point ---
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root. Please use 'sudo ./install.sh'"
    exit 1
fi

if [ "$1" == "--verify" ]; then
    run_verification
    exit 0
fi

# We assume a one-shot install is the primary goal now.
# The menu has been commented out but can be restored if needed.
install_all

echo "Exiting management script."
