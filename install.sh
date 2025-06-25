#!/bin/bash

# ==============================================================================
# RNS-E Pi Control - Management Script (v3.4 - with uinput support)
# ==============================================================================
# This script installs, checks, updates, and verifies the CAN-Bus control
# scripts from the korni92/RNS-E-Pi-Control repository for a read-only
# Crankshaft OS environment. It includes an interactive configuration assistant.
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
        echo -e "\e[32m [ACTIVE (running)]\e[0m"
    else
        if systemctl is-failed --quiet "$service"; then
            echo -e "\e[31m [FAILED]\e[0m - Check logs with 'journalctl -u ${service}'"
        else
            echo " [INACTIVE]"
        fi
    fi
}

function run_verification() {
    print_header
    echo "Starting Post-Reboot Verification..."
    
    echo -e "\n--- 1. Checking Permissions ---"
    if [ -e "/dev/uinput" ]; then
         echo "[OK] /dev/uinput device exists."
    else
         echo "[FAIL] /dev/uinput device not found. Kernel module may be missing."
    fi
    if groups pi | grep -q '\binput\b'; then
        echo "[OK] User 'pi' is in the 'input' group."
    else
        echo "[FAIL] User 'pi' is not in the 'input' group. Run installation again."
    fi

    echo -e "\n--- 2. Checking CAN Interface (can0) ---"
    if ip link show can0 &>/dev/null; then
        if ip -details link show can0 | grep -q "state UP"; then
            echo "[OK] Interface 'can0' is UP."
        elif ip -details link show can0 | grep -q "state BUS-OFF"; then
            echo "[FAIL] Interface 'can0' is in BUS-OFF state. Check wiring/termination/oscillator."
        else
            echo "[WARN] Interface 'can0' is present but not fully UP. Current state:"
            ip -details link show can0 | grep "state"
        fi
    else
        echo "[FAIL] Interface 'can0' not found. Check HAT connection and /boot/config.txt."
    fi

    echo -e "\n--- 3. Checking Systemd Services ---"
    for service in "${SERVICES[@]}"; do
        check_service_status "$service"
    done

    echo -e "\n--- 4. Live CAN Data Test ---"
    echo "Listening for ANY CAN traffic for 5 seconds..."
    if timeout 5 candump -L can0 > /dev/null; then
        echo "[OK] Live data detected on the CAN bus."
    else
        echo "[FAIL] No traffic detected. Is the car's ignition on? Check wiring."
    fi
    
    echo -e "\nVerification complete!"
}

function update_scripts() {
    print_header
    echo "--> Updating scripts from GitHub..."
    if [ ! -d "$REPO_DIR/.git" ]; then
        echo "ERROR: Not a git repository. Cannot update. Please run a full installation."
        return 1
    fi
    
    cd "$REPO_DIR"
    if [ -n "$(git status --porcelain)" ]; then
        echo "WARNING: You have local changes. Discarding them to pull the latest version."
        git reset --hard HEAD
    fi
    
    echo "Pulling latest changes..."
    git pull
    chown -R pi:pi "$REPO_DIR"
    
    echo "--> Re-installing Python dependencies in case they changed..."
    [ -f "$REPO_DIR/requirements.txt" ] && pip3 install -r "$REPO_DIR/requirements.txt"
    
    echo "--> Restarting services to apply updates..."
    systemctl restart "${PYTHON_SERVICES[@]}"
    
    echo -e "\nUpdate complete! Run 'sudo ./install.sh --verify' to check status."
}

# --- Full Installation Process ---
function install_all() {
    print_header
    echo "This script will perform a full installation and configuration for uinput."
    if ! ask_yes_no "This will overwrite existing configurations. Continue?"; then
        echo "Installation aborted."; exit 0;
    fi

    echo "--> Remounting filesystems as read-write..."
    mount -o remount,rw / && mount -o remount,rw /boot

    install_system_packages

    echo "--> Granting permissions for uinput device..."
    usermod -a -G input pi
    echo "User 'pi' has been added to the 'input' group."

    echo "--> Cloning repository from ${REPO_URL}..."
    [ -d "$REPO_DIR" ] && rm -rf "$REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR" && chown -R pi:pi "$REPO_DIR"
    
    echo "--> Installing Python dependencies from requirements.txt..."
    [ -f "$REPO_DIR/requirements.txt" ] && pip3 install -r "$REPO_DIR/requirements.txt"
    
    echo "--> Configuring fstab for custom temporary directories..."
    sed -i "/^tmpfs \/tmp tmpfs/d" /etc/fstab
    sed -i "/^tmpfs \/var\/log tmpfs/d" /etc/fstab
    sed -i "/^tmpfs \/home\/pi tmpfs/d" /etc/fstab
    
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
    
    read -p "Enter oscillator frequency in Hz for MCP2515 (e.g., 12000000): " OSC_HZ
    read -p "Enter interrupt GPIO pin for MCP2515 (e.g., 25): " INT_PIN
    {
        echo "# --- Added by RNS-E Pi Control Install Script ---"
        echo "dtparam=spi=on"
        echo "dtoverlay=mcp2515-can0,oscillator=${OSC_HZ},interrupt=${INT_PIN},spimaxfrequency=1000000"
    } >> /boot/config.txt
    
    echo "--> Starting Interactive Configuration..."
    cp "$REPO_DIR/config.json" "$CONFIG_FILE"
    
    echo "--> Adjusting ZMQ communication path in config.json..."
    sed -i 's|ipc:///tmp/can_stream.ipc|ipc:///run/rnse_control/can_stream.ipc|g' "$CONFIG_FILE"

    read -p "Enter your time zone (e.g., Europe/Berlin): " TIME_ZONE
    sed -i "s|\"car_time_zone\": \".*\"|\"car_time_zone\": \"${TIME_ZONE}\"|" "$CONFIG_FILE"
    
    chown pi:pi "$CONFIG_FILE"
    echo "Configuration saved to $CONFIG_FILE."

    function create_systemd_services() {
        echo "--> Creating and configuring systemd services (uinput version)..."
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
Description=RNS-E CAN-Bus Keyboard Simulation (uinput)
After=can-handler.service
Wants=can-handler.service
[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/can_keyboard.py
WorkingDirectory=${REPO_DIR}
Restart=always
RestartSec=3
User=pi
Group=input
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
Restart=on-failure
RestartSec=5
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
    echo "    INSTALLATION COMPLETE! (uinput version) "
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

case "$1" in
    --verify)
        run_verification
        exit 0
        ;;
    --update)
        update_scripts
        exit 0
        ;;
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
        1) install_all; break ;;
        2) run_verification; read -p "Press [Enter] to return..." ;;
        3) update_scripts; read -p "Press [Enter] to return..." ;;
        4) break ;;
        *) echo "Invalid option. Please try again."; sleep 1 ;;
    esac
done

echo "Exiting management script."

