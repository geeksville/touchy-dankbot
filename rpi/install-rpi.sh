#!/usr/bin/env bash
#
# install-rpi.sh — Raspberry Pi OS system prerequisites for touchy-dankbot.
#
# `dankbot listen` needs two things a stock Raspberry Pi OS image does not give
# a normal login user:
#
#   * PortAudio + ALSA, so `sounddevice` can open the reSpeaker XVF3800 mic —
#     without `libportaudio2` the audio layer dies with
#     "could not start the microphone: PortAudio library not found"; and
#   * read/write access to the touchy-pad's USB vendor interface, which the
#     host library opens through libusb — without the udev rule / `plugdev`
#     group the display logs "[Errno 13] Access denied (insufficient
#     permissions)".
#
# Run this ONCE on the Pi as your normal login user (not root — it uses sudo
# where it needs to). It is idempotent, so re-running it is safe.
#
#     bash rpi/install-rpi.sh
#
# Group memberships do not apply to shells that are already running: log out
# and back in (or reboot) afterwards, then check `id -nG`.
#
# This does NOT install Poetry / just / dankbot itself, and it does not clone
# the repo — see rpi/README.md for those steps.

set -euo pipefail

log() { printf '\n==> %s\n' "$*"; }

# Bless the invoking user. Tolerate `sudo ./install-rpi.sh` by preferring
# SUDO_USER; refuse a bare root shell (there is no "current user" to bless).
if [ "$(id -u)" -eq 0 ]; then
    if [ -z "${SUDO_USER:-}" ]; then
        echo "install-rpi.sh: run as your normal login user, not root" >&2
        exit 1
    fi
    target_user="$SUDO_USER"
else
    target_user="$(id -un)"
fi
target_uid="$(id -u "$target_user")"

log "Blessing $target_user (uid $target_uid) for the microphone and the touchy-pad"

# --- userspace libraries -----------------------------------------------------
#   libportaudio2      - what python `sounddevice` loads at runtime; the fix for
#                        "PortAudio library not found". Its ALSA dependency
#                        (libasound2 / libasound2t64, depending on the release)
#                        is pulled in automatically — naming it here would break
#                        on whichever of bookworm/trixie we are not on.
#   alsa-utils         - `arecord -l` / `aplay -l`, to verify the reSpeaker.
#   libusb-1.0-0       - pyusb (the touchy-pad host library) dlopens it.
#   usbutils           - `lsusb`, to see whether the pad enumerated.
#   python3-venv       - Poetry's own venv + `just install` need it on Debian.
#   python3-dev + build-essential - for the occasional sdist / native wheel.
log "Installing userspace libraries (apt)"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    libportaudio2 alsa-utils \
    libusb-1.0-0 usbutils \
    python3-venv python3-dev build-essential

# --- udev rule: touchy-pad USB access ----------------------------------------
# Adapted from touchy-pad's udev/99-touchy-pad.rules. 303a:8369 enumerates as
# HID (kernel-owned), CDC-ACM (/dev/ttyACM*, the dialout group) and a vendor
# 0xFF host-API interface that libusb opens directly — so that interface needs
# this rule. Upstream hard-codes OWNER="1000"; we fill in the uid of the user
# being blessed instead, and keep GROUP="plugdev" as the durable grant (plus
# TAG+="uaccess" for a local seat).
log "Installing the touchy-pad udev rule (/etc/udev/rules.d/99-touchy-pad.rules)"
rule_tmp="$(mktemp)"
trap 'rm -f "$rule_tmp"' EXIT
cat >"$rule_tmp" <<EOF
# Touchy-Pad — regular-user access to the custom vendor USB interface.
# Installed by touchy-dankbot's rpi/install-rpi.sh; adapted from
# https://github.com/geeksville/touchy-pad/blob/main/udev/99-touchy-pad.rules

# The running pad (VID:PID 303a:8369, our PID from espressif).
SUBSYSTEM=="usb", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="8369", OWNER="$target_uid", MODE="0660", GROUP="plugdev", TAG+="uaccess"

# The ESP32-S3 USB bootloader, used by \`touchy update\`.
SUBSYSTEM=="usb", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="1001", OWNER="$target_uid", MODE="0660", GROUP="plugdev", TAG+="uaccess", ENV{ID_MM_DEVICE_IGNORE}="1", ENV{ID_MM_PORT_IGNORE}="1"

# CH341 UARTs, used on some ESP32 boards.
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", OWNER="$target_uid", MODE="0660", GROUP="plugdev", TAG+="uaccess", ENV{ID_MM_DEVICE_IGNORE}="1", ENV{ID_MM_PORT_IGNORE}="1"
EOF
sudo install -m 0644 -o root -g root "$rule_tmp" /etc/udev/rules.d/99-touchy-pad.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

# --- group membership --------------------------------------------------------
#   plugdev - the pad's vendor interface, per the rule above
#   dialout - /dev/ttyACM* (the pad's CDC-ACM serial; `touchy update`)
#   audio   - /dev/snd/* (direct ALSA capture from the mic)
log "Adding $target_user to plugdev, dialout and audio"
for grp in plugdev dialout audio; do
    getent group "$grp" >/dev/null || sudo groupadd "$grp"
done
sudo usermod -aG plugdev,dialout,audio "$target_user"

# --- verify ------------------------------------------------------------------
log "Verifying"
if python3 -c 'import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library("portaudio") else 1)'; then
    echo "   PortAudio: found (sounddevice can load it)"
else
    echo "   PortAudio: STILL MISSING — is libportaudio2 installed?" >&2
fi
if command -v arecord >/dev/null; then
    echo "   capture devices (arecord -l):"
    arecord -l 2>&1 | sed 's/^/     /' || true
fi
if command -v lsusb >/dev/null; then
    if lsusb -d 303a:8369 >/dev/null 2>&1; then
        echo "   touchy-pad: present on USB"
    else
        echo "   touchy-pad: not plugged in (fine — dankbot connects lazily)"
    fi
fi
echo "   groups on the account: $(id -nG "$target_user")"
echo "   groups in this shell:  $(id -nG)   <- updates after a re-login"

log "Done."
cat <<EOF
Log out and back in (or reboot) so the new group memberships take effect,
then from the repo root:

    poetry install
    poetry run dankbot models          # needs the Ollama server
    poetry run dankbot listen

If the touchy-pad was already plugged in, unplug and replug it once so the new
udev rule takes effect on the existing device node.
EOF