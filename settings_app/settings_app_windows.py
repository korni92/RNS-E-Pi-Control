#!/usr/bin/env python3
#
# settings_app_windows.py
#
import json
import os
import shutil
from flask import Flask, jsonify, render_template, request

# --- Configuration for Windows Test ADD YOUR config.json PATH C:/Users/... ---
CONFIG_PATH = 'config.json'
CONFIG_BACKUP_PATH = 'config.json.bak'
app = Flask(__name__, template_folder='.')

def mock_linux_command(command_name, *args):
    print(f"[WINDOWS MOCK] Would execute: {command_name} {' '.join(args)}")

# --- API Endpoints ---
@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        new_config = request.json
        mock_linux_command("mount", "-o", "remount,rw", "/")
        if os.path.exists(CONFIG_PATH):
            shutil.copy(CONFIG_PATH, CONFIG_BACKUP_PATH)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(new_config, f, indent=2)
        mock_linux_command("mount", "-o", "remount,ro", "/")
        mock_linux_command("systemctl", "restart", "services...")
        return jsonify({"success": True, "message": "Configuration saved (mocked)."})
    else: # GET
        with open(CONFIG_PATH, 'r') as f:
            return jsonify(json.load(f))

@app.route('/api/reset', methods=['POST'])
def reset_config():
    if not os.path.exists(CONFIG_BACKUP_PATH): return jsonify({"error": "No backup found."}), 404
    mock_linux_command("mount", "-o", "remount,rw", "/")
    shutil.copy(CONFIG_BACKUP_PATH, CONFIG_PATH)
    mock_linux_command("mount", "-o", "remount,ro", "/")
    mock_linux_command("systemctl", "restart", "services...")
    return jsonify({"success": True, "message": "Configuration restored (mocked)."})

@app.route('/api/valid_keys', methods=['GET'])
def get_valid_keys():
    mock_keys = [
        'KEY_A', 'KEY_B', 'KEY_C', 'KEY_UP', 'KEY_DOWN', 'KEY_LEFT', 'KEY_RIGHT',
        'KEY_ENTER', 'KEY_ESC', 'KEY_M', 'KEY_H', 'KEY_V', 'KEY_N', 'KEY_X',
        'KEY_VOLUMEDOWN', 'KEY_VOLUMEUP', 'KEY_MUTE', 'KEY_NEXTSONG',
        'KEY_PREVIOUSSONG', 'KEY_PLAYPAUSE', 'KEY_0', 'KEY_1', 'KEY_2'
    ]
    return jsonify(sorted(mock_keys))

@app.route('/api/timezones', methods=['GET'])
def get_timezones():
    timezones = [
        "UTC", "Europe/London", "Europe/Berlin", "Europe/Paris", "Europe/Lisbon",
        "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
        "Asia/Tokyo", "Asia/Dubai", "Asia/Kolkata", "Australia/Sydney"
    ]
    return jsonify(sorted(timezones))

# --- Frontend Serving ---
@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    print("--- Running in WINDOWS test mode ---")
    app.run(host='127.0.0.1', port=5000, debug=True)
