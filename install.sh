#!/bin/bash
# USB audio card test installer. Run on the Pi: sudo bash install.sh
# MiLa Test Rig - Aaron Simo, with Claude (Anthropic) - Copyright (c) 2026 KRUU US INC. All rights reserved.

set -e

INSTALL_DIR="/opt/usb_audio_test"
SERVICE_NAME="usb-audio-test"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "USB Audio Test installer"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root (sudo bash install.sh)"
    exit 1
fi

echo "[1/6] Installing dependencies..."
apt-get update -qq
apt-get install -y -qq alsa-utils python3 python3-rpi.gpio

echo "[2/6] Copying files to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/usb_audio_test.py" "$SCRIPT_DIR/analysis.py" "$SCRIPT_DIR/devices.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/usb_audio_test.py"

echo "[3/6] Enabling I2S for the reference mic..."
# The reference mic (INMP441-class I2S MEMS -- see README "Reference mic") needs
# I2S enabled in the Pi's boot config. Idempotent: re-running install.sh must not
# duplicate these lines, so every append below is grep-guarded first, and each
# guard is independent so a partially-configured file (e.g. dtparam=i2s=on already
# present from something else) still ends up correct rather than duplicated.
CONFIG_TXT=""
if [ -f /boot/firmware/config.txt ]; then
    CONFIG_TXT="/boot/firmware/config.txt"   # Bookworm
elif [ -f /boot/config.txt ]; then
    CONFIG_TXT="/boot/config.txt"            # older Pi OS
fi

I2S_CONFIG_CHANGED=0
I2S_BLOCK_MARKER="MiLa reference mic (INMP441 I2S)"

if [ -z "$CONFIG_TXT" ]; then
    echo "      WARNING: no boot config found (checked /boot/firmware/config.txt and"
    echo "      /boot/config.txt) -- skipping I2S setup. Not running on Pi OS? Enable"
    echo "      dtparam=i2s=on plus the reference-mic overlay by hand if this box needs it."
else
    echo "      Using ${CONFIG_TXT}"

    if grep -q '^dtparam=i2s=on' "$CONFIG_TXT"; then
        echo "      dtparam=i2s=on already present"
    else
        printf '\n# MiLa reference mic: enable the I2S interface\ndtparam=i2s=on\n' >> "$CONFIG_TXT"
        echo "      Added dtparam=i2s=on"
        I2S_CONFIG_CHANGED=1
    fi

    if grep -qF "$I2S_BLOCK_MARKER" "$CONFIG_TXT"; then
        echo "      Reference-mic overlay block already present"
    else
        # OPEN BENCH ITEM: this overlay is NOT yet confirmed against a real INMP441
        # (see README "Open bench items"). It lives in its own clearly-marked block
        # so it is easy to find and swap on the Pi if the bench needs a different
        # overlay -- do not treat this line as final/verified.
        cat >> "$CONFIG_TXT" <<'EOF'

# --- MiLa reference mic (INMP441 I2S) --- confirm/adjust overlay on the bench ---
# OPEN BENCH ITEM (UNVERIFIED): INMP441 works with this generic I2S-mic overlay on
# most Pi OS builds. VERIFY the capture card appears with `arecord -l` after reboot;
# swap the overlay if it does not.
dtoverlay=googlevoicehat-soundcard
# --- end MiLa reference mic block ---
EOF
        echo "      Added reference-mic overlay block (UNVERIFIED -- see README)"
        I2S_CONFIG_CHANGED=1
    fi
fi

echo "[4/6] Installing systemd services..."
# Clear any prior mask first. A masked unit is a symlink to /dev/null; copying
# onto it would write THROUGH the symlink into /dev/null (the file silently
# vanishes and the mask stays), which leaves the service dead. Unmask and delete
# the destinations so the cp below always writes real, fresh unit files.
systemctl unmask "${SERVICE_NAME}.service" "${SERVICE_NAME}-button.service" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}-button.service"
cp "$SCRIPT_DIR/usb-audio-test.service" "/etc/systemd/system/${SERVICE_NAME}.service"
cp "$SCRIPT_DIR/usb-audio-test-button.service" "/etc/systemd/system/${SERVICE_NAME}-button.service"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}-button.service"
systemctl restart "${SERVICE_NAME}-button.service"

echo "[5/6] Installing USB hotplug rule..."
cp "$SCRIPT_DIR/99-usb-audio-test.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --subsystem-match=sound --action=add
echo "      (This fires a test right now. If the reference mic isn't enabled/plugged"
echo "       in yet, or no unit is in the box, a station error or unlit LEDs are"
echo "       expected -- see the REBOOT note below.)"

echo "[6/6] Done!"

HOSTNAME_VAL=$(hostname)
IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo "Installation complete."
echo "Hostname: ${HOSTNAME_VAL}"
echo "IP:       ${IP_ADDR:-(no network)}"
echo ""
echo "Both LEDs blink together at boot = station up and ready to test."
echo "The test runs whenever a USB mic is plugged in (including at boot)."
echo "Unplug and re-plug a mic to test it."
echo "  Green LED (solid) + 2 high beeps = PASS"
echo "  Red LED (solid)   + 3 low beeps  = FAIL (this specific mic)"
echo "  Red LED flashing, NO beep        = STATION error (rig/reference problem,"
echo "                                      not a verdict on the mic) -- clears on"
echo "                                      the next test or calibration"
echo ""
echo "Hold the button 2s (good mic in the box) to calibrate."
echo "  Both LEDs on while holding = release now to calibrate."
echo "  3 green blinks = calibration saved."
echo ""
if [ "$I2S_CONFIG_CHANGED" -eq 1 ]; then
    echo "*** REBOOT REQUIRED to enable the reference mic (I2S config changed). ***"
    echo "    sudo reboot"
    echo "    Then confirm it enumerated:  arecord -l"
    echo "    And run the diagnostic:      sudo ${INSTALL_DIR}/usb_audio_test.py --selftest"
    echo ""
fi
echo "Live output:  sudo journalctl -u ${SERVICE_NAME} -f"
echo "Run manually: sudo systemctl start ${SERVICE_NAME}"
