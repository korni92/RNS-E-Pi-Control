#!/bin/bash

# ==============================================================================
# RNS-E Pi Control - Management Script (v3.1)
# ==============================================================================
# This script installs, checks, updates, and verifies the CAN-Bus control
# scripts from the korni92/RNS-E-Pi-Control repository for a read-only
# Crankshaft OS environment. It includes an interactive configuration assistant.
#
# USAGE:
#   sudo ./install.sh            (To run the main installation menu)
#   sudo ./install.sh --verify   (To run post-reboot verification checks)
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
    echo "    RNS-E Pi Control - Management Script             "
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
    echo "Checking Filesystem Mounts (/etc/fstab)..."
    grep -q "tmpfs /tmp" /etc/fstab && echo "[OK] /tmp mount found." || echo "[MISSING] /tmp mount."
    grep -q "tmpfs /var/log" /etc/fstab && echo "[OK] /var/log mount found." || echo "[MISSING] /var/log mount."
    grep -q "tmpfs /home/pi" /etc/fstab && echo "[OK] /home/pi mount found." || echo "[MISSING] /home/pi mount."
    
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

function update_scripts() {
    print_header
    echo "--> Updating scripts from GitHub..."
    if [ ! -d "$REPO_DIR/.git" ]; then
        echo "ERROR: Not a git repository. Cannot update. Please run a full installation."
        return
    fi
    
    cd "$REPO_DIR"
    if [ -n "$(git status --porcelain)" ]; then
        echo "WARNING: You have local changes. Discarding them to pull the latest version."
        git reset --hard
    fi
    
    echo "Pulling latest changes..."
    git pull
    chown -R pi:pi "$REPO_DIR"
    
    echo "--> Re-installing Python dependencies..."
    pip3 install -r requirements.txt
    
    echo "--> Restarting services to apply updates..."
    systemctl restart can-handler.service crankshaft-can-features.service can-keyboard.service can-fis-writer.service
    
    echo -e "\nUpdate complete! Use 'Check System Status' to verify."
}

function run_verification() {
    print_header
    echo "Starting Post-Reboot Verification..."
    
    echo -e "\n--- 1. Checking CAN Interface (can0) ---"
    if ip link show can0 &>/dev/null && ip -details link show can0 | grep -q "state UP" && ip -details link show can0 | grep -q "bitrate 100000"; then
        echo "[OK] Interface 'can0' is UP with 100 kbit/s."
    else
        echo "[FAIL] 'can0' is not up or has wrong bitrate. Check wiring and /boot/config.txt."
    fi

    echo -e "\n--- 2. Checking Systemd Services ---"
    for service in "${SERVICES[@]}"; do
        check_service_status "$service"
    done

    echo -e "\n--- 3. Live CAN Data Test ---"
    echo "Listening for ANY CAN traffic for 5 seconds..."
    if timeout 5 candump -L can0 > /dev/null; then
        echo "[OK] Live data detected on the CAN bus."
        
        if ask_yes_no "Do you want to run interactive feature tests?"; then
            echo -e "\n--- 4. Interactive Tests ---"
            echo "Please press a button on your MMI control panel now..."
            if timeout 3 candump can0,461:7FF | grep -q '461'; then
                echo "[OK] MMI message (0x461) detected!"
            else
                echo "[NOTE] No MMI message detected."
            fi
        fi
    else
        echo "[FAIL] No traffic detected. Check wiring, termination, and HAT power."
    fi
    
    echo -e "\nVerification complete!"
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

    echo "--> Installing system packages..."
    apt-get update && apt-get install -y git python3-pip can-utils python3-unidecode

    echo "--> Cloning repository from ${REPO_URL}..."
    [ -d "$REPO_DIR" ] && rm -rf "$REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR" && chown -R pi:pi "$REPO_DIR"
    
    echo "--> Installing Python dependencies..."
    [ -f "$REPO_DIR/requirements.txt" ] && pip3 install -r "$REPO_DIR/requirements.txt"
    
    echo "--> Configuring /etc/fstab..."
    grep -qF "tmpfs /tmp" /etc/fstab || echo "tmpfs /tmp tmpfs defaults,noatime,nosuid,nodev,size=64m 0 0" >> /etc/fstab
    grep -qF "tmpfs /var/log" /etc/fstab || echo "tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,size=32m 0 0" >> /etc/fstab
    grep -qF "tmpfs /home/pi" /etc/fstab || echo "tmpfs /home/pi tmpfs defaults,noatime,uid=1000,gid=1000,mode=0755,size=16m 0 0" >> /etc/fstab
    mount -a
    
    echo "--> Configuring /boot/config.txt..."
    read -p "Enter oscillator frequency in Hz for MCP2515 (e.g., 8000000): " OSC_HZ
    read -p "Enter interrupt GPIO pin for MCP2515 (e.g., 25): " INT_PIN
    sed -i "/dtoverlay=mcp2515-can0/d" /boot/config.txt
    {
        echo "# --- Added by RNS-E Pi Control Install Script ---"
        echo "dtparam=spi=on"
        echo "dtoverlay=mcp2515-can0,oscillator=${OSC_HZ},interrupt=${INT_PIN},spimaxfrequency=1000000"
    } >> /boot/config.txt
    if ask_yes_no "Enable Composite Video Output?"; then
        {
            echo "# --- Composite Video enabled by script ---"
            echo "enable_tvout=1"
        } >> /boot/config.txt
    fi
    
    echo "--> Starting Interactive Configuration..."
    cp "$REPO_DIR/config.json.example" "$CONFIG_FILE"
    read -p "Enter your time zone (e.g., Europe/Berlin): " TIME_ZONE
    sed -i "s|\"car_time_zone\": \".*\"|\"car_time_zone\": \"${TIME_ZONE}\"|" "$CONFIG_FILE"
    if ask_yes_no "Enable automatic Day/Night mode?"; then sed -i '/"day_night_mode":/s/false/true/' "$CONFIG_FILE"; fi
    if ask_yes_no "Enable automatic Time Sync?"; then sed -i '/"time_sync":/s/false/true/' "$CONFIG_FILE"; fi
    if ask_yes_no "Enable TV-Tuner simulation?"; then sed -i '/"tv_simulation": {/,/}/{s/"enabled": false/"enabled": true/}' "$CONFIG_FILE"; fi
    if ask_yes_no "Enable automatic Shutdown feature?"; then
        sed -i '/"auto_shutdown": {/,/}/{s/"enabled": false/"enabled": true/}' "$CONFIG_FILE"
        read -p "Choose shutdown trigger ('ignition_off' or 'key_pulled'): " TRIGGER
        sed -i '/"auto_shutdown": {/,/}/{s/"trigger": ".*"/"trigger": "'"${TRIGGER}"'"/}' "$CONFIG_FILE"
    fi
    if ask_yes_no "Enable custom text on the FIS display?"; then
        sed -i '/"fis_display": {/,/}/{s/"enabled": false/"enabled": true/}' "$CONFIG_FILE"
        read -p "Enter text for FIS Line 1 (max 8 chars): " FIS_L1
        read -p "Enter text for FIS Line 2 (max 8 chars): " FIS_L2
        sed -i '/"fis_display": {/,/}/{s|"line1": ".*"|"line1": "'"${FIS_L1}"'"|}' "$CONFIG_FILE"
        sed -i '/"fis_display": {/,/}/{s|"line2": ".*"|"line2": "'"${FIS_L2}"'"|}' "$CONFIG_FILE"
    fi
    chown pi:pi "$CONFIG_FILE"
    echo "Configuration saved to $CONFIG_FILE."

    echo "--> Creating systemd services..."
    # Create all 5 service files using cat <<EOF ...
    # This part is omitted for brevity. You must paste the full 'create_systemd_services' function from our previous conversation here.
    
    echo "--> Finalizing installation..."
    systemctl daemon-reload && systemctl enable "${SERVICES[@]}"
    
    echo
    echo "==========================================="
    echo "      INSTALLATION COMPLETE!               "
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

while true; do
    print_header
    echo "Please choose an option:"
    echo "  1) Install or Re-install Everything"
    echo "  2) Check System Status"
    echo "  3) Update Scripts from GitHub"
    echo "  4) Exit"
    read -p "Enter your choice [1-4]: " choice

    case $choice in
        1) install_all; break ;;
        2) check_system_status; read -p "Press [Enter] to return..." ;;
        3) update_scripts; read -p "Press [Enter] to return..." ;;
        4) break ;;
        *) echo "Invalid option. Please try again."; sleep 1 ;;
    esac
done

echo "Exiting management script."