#!/usr/bin/env bash
#
# user-service.sh — run `dankbot listen` as a systemd *user* service.
#
# The desk robot is an appliance: it should come up by itself after a reboot and
# stay up, not need a terminal and a login. This installs the listener as a user
# unit and gives you one place to drive it:
#
#     bash rpi/user-service.sh              # same as `install`
#     bash rpi/user-service.sh install      # write the unit, enable + start it
#     bash rpi/user-service.sh status       # is it up? which paths?
#     bash rpi/user-service.sh logs         # follow this service's journal
#     bash rpi/user-service.sh restart
#     bash rpi/user-service.sh stop
#     bash rpi/user-service.sh start
#     bash rpi/user-service.sh enable       # autostart at boot, do not start now
#     bash rpi/user-service.sh disable      # no autostart, leave it running
#     bash rpi/user-service.sh uninstall    # stop, disable, delete the unit
#
# Why a *user* unit rather than a system one:
#
#   * it runs as your login user, so it inherits the audio / plugdev / dialout
#     access rpi/install-rpi.sh grants that account — the mic and the touchy-pad
#     are both ordinary-user devices here;
#   * `loginctl enable-linger` (done by `install`) starts that user's manager at
#     boot, so the service comes up with nobody logged in;
#   * the per-user state it needs — the Moonshine STT model cache in
#     ~/.cache/moonshine_voice, the pad's udev rule (OWNER=<you>) — is already
#     per-user anyway.
#
# Logs go to the *journal*, not a file: `StandardOutput=journal` is the default,
# and this is deliberate. Raspberry Pi OS keeps no /var/log/journal, so journald
# stores in /run/log/journal — RAM, wiped at reboot, and size-capped by journald
# itself, so there is no log-rotation chore and no SD-card writes (which matters
# once the root filesystem goes read-only). One place holds everything: the quips
# and the unit's own start/stop/exit events. Read it with
# `bash rpi/user-service.sh logs` (i.e. `journalctl --user-unit=dankbot-listen -f`).
# See docs/plans/rpi-service.md for the alternatives that were considered.
#
# Settings come from ~/.config/dankbot/listen.env (a commented template is
# written on install). Every DANKBOT_* variable there follows the CLI's
# flag -> env -> default rule, so it overrides the built-in default. Edit it,
# then `bash rpi/user-service.sh restart`.
#
# Run this ONCE per user on the Pi; it is idempotent. Do rpi/install-rpi.sh
# first, and log out and back in once so the audio/plugdev group changes apply.

set -euo pipefail

unit_name=dankbot-listen.service
target_user="$(id -un)"

# Resolve our own location first: the unit's WorkingDirectory is the repo root
# (where `just listen` runs), so `--fake sim-overheard.txt` and a relative
# --persona-file behave the same under systemd as they do interactively.
self="$(readlink -f -- "${BASH_SOURCE[0]}")"
repo_root="$(cd -P -- "$(dirname -- "$self")/.." && pwd)"

config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
unit_path="$config_home/systemd/user/$unit_name"
env_file="$config_home/dankbot/listen.env"

log() { printf '\n==> %s\n' "$*"; }
die() {
    printf 'user-service.sh: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Run `dankbot listen` as a systemd user service.

Usage: bash rpi/user-service.sh [command]

Commands (default: install):
  install     Write the unit + env template, enable and start the service, and
              enable linger so it comes up at boot with nobody logged in.
  start       Start it now (no change to autostart).
  stop        Stop it.
  restart     Restart it.
  enable      Autostart at boot, do not start it now.
  disable     Do not autostart (leaves it running).
  status      systemd status plus the unit/env paths.
  logs        Follow this service's journal (journalctl --user-unit=dankbot-listen -f).
  uninstall   Stop, disable and delete the unit (the env file is kept).

Settings live in ~/.config/dankbot/listen.env (DANKBOT_* overrides). Logs — the
quips and the unit's start/stop/exit events — are in the journal, not a file. The
header of this script (or docs/plans/rpi-service.md) has the reasoning.
EOF
}

# Everything here drives a real user manager: without one, `systemctl --user`
# cannot do anything, and each command would fail with a bus error instead of a
# sentence a human can act on.
require_user_manager() {
    systemctl --user show-environment >/dev/null 2>&1 ||
        die "no systemd user manager here — run this on the Pi (or with XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS set)"
}

# The absolute path to the `dankbot` console script, baked into ExecStart: a user
# unit gets a minimal PATH, so a bare `dankbot` would not resolve. Order: an
# explicit override, a real install on PATH (pipx / pip), a Poetry venv inside
# the repo (virtualenvs.in-project), then Poetry's cache venv.
resolve_dankbot() {
    local bin venv
    if [ -n "${DANKBOT_BIN:-}" ]; then
        [ -x "$DANKBOT_BIN" ] || die "DANKBOT_BIN=$DANKBOT_BIN is not an executable file"
        printf '%s\n' "$DANKBOT_BIN"
        return 0
    fi
    if bin="$(command -v dankbot 2>/dev/null)" && [ -n "$bin" ]; then
        printf '%s\n' "$bin"
        return 0
    fi
    if [ -x "$repo_root/.venv/bin/dankbot" ]; then
        printf '%s\n' "$repo_root/.venv/bin/dankbot"
        return 0
    fi
    if command -v poetry >/dev/null 2>&1; then
        if venv="$(cd -- "$repo_root" && poetry env info --path 2>/dev/null)" &&
            [ -x "$venv/bin/dankbot" ]; then
            printf '%s\n' "$venv/bin/dankbot"
            return 0
        fi
    fi
    return 1
}

# A commented template, written once: every DANKBOT_* line is an override of a
# built-in default, so the file is a no-op until the user uncomments something.
write_env_file() {
    if [ -f "$env_file" ]; then
        return 0
    fi
    mkdir -p -- "$(dirname -- "$env_file")"
    cat >"$env_file" <<'EOF'
# Settings for the `dankbot-listen` systemd user service (rpi/user-service.sh).
# Each DANKBOT_* variable follows the CLI's flag -> env -> default rule, so a
# line here overrides the built-in default. Uncomment what you need, then run
# `bash rpi/user-service.sh restart`.
#
# Ollama server (default: a local `ollama serve`).
#DANKBOT_URL=http://localhost:11434
#
# Model (default: the jokemark bake-off winner).
#DANKBOT_MODEL=hf.co/TheDrummer/Rocinante-X-12B-v1-GGUF:Q4_K_M
#
# DEBUG log records (HTTP retries, display rendering, cleanup failures).
#DANKBOT_DEBUG=1
#
# Mic / STT / gate / display knobs (see the README's tables):
#DANKBOT_MIC=respeaker
#DANKBOT_MODEL_ARCH=small
#DANKBOT_MIC_BLOCKSIZE=1024
#DANKBOT_MIN_WORDS=3
#DANKBOT_MIN_DURATION=0.6
#DANKBOT_DEBOUNCE=5.0
#DANKBOT_DISPLAY_MODE=auto
#DANKBOT_CLEAR_AFTER=120
EOF
    echo "   wrote $env_file (all settings commented out — the defaults are in effect)"
}

# The unit itself. Deliberately plain: no sandboxing that would cut the mic or
# the pad off (no ProtectHome/PrivateDevices), and no `%`-specifiers — the paths
# are resolved here so what systemd runs is exactly what this script printed.
write_unit() {
    local bin="$1"
    mkdir -p -- "$(dirname -- "$unit_path")"
    cat >"$unit_path" <<EOF
# Generated by rpi/user-service.sh — edit the script (or the env file), not
# this: a later "install" overwrites it.
#
# A *user* unit: it runs as $target_user, so it inherits the audio/plugdev/dialout
# group access rpi/install-rpi.sh grants that account (the mic and the pad are
# both ordinary-user devices). "loginctl enable-linger" (done by install) starts
# the user manager at boot, so this comes up with nobody logged in.
[Unit]
Description=touchy-dankbot — listen to the mic, scroll quips on the touchy-pad
Documentation=https://github.com/geeksville/touchy-dankbot
# The Ollama server is a *system* unit and a user manager cannot order against
# it (After=ollama.service is ignored across managers), so a server that is not
# ready yet is absorbed by the client's best-effort model fetch and by
# Restart=on-failure below.

[Service]
Type=simple
WorkingDirectory=$repo_root
# Settings: DANKBOT_* overrides (flag -> env -> default). The leading "-" makes
# the file optional — without it the built-in defaults apply.
EnvironmentFile=-$env_file
# Line-buffer the output so a quip reaches the journal as it is said, not in
# blocks once the buffer fills.
Environment=PYTHONUNBUFFERED=1
ExecStart=$bin listen
# An appliance should keep trying: a mic plugged in *after* boot, or an Ollama
# server still loading, must not need a manual "start". A permanent failure
# retries every RestartSec — that is the intent, not a bug.
Restart=on-failure
RestartSec=5
# Logs go to the journal. Stated explicitly (it is also the default) so that a
# future edit cannot quietly point the service at a file: Raspberry Pi OS keeps
# no /var/log/journal, so journald stores in /run/log/journal — RAM, wiped at
# reboot, size-capped by journald itself, and no SD-card writes once the root
# filesystem is read-only. The quips and this unit's own start/stop/exit events
# all land in one place:
# journalctl --user-unit=$unit_name -f
StandardOutput=journal
StandardError=inherit

[Install]
WantedBy=default.target
EOF
}

# Autostart at boot with nobody logged in needs a lingering user. That is a
# one-off privilege escalation: try the unprivileged polkit path first (allowed
# for your own account on most systems), then passwordless sudo, and otherwise
# tell the user the exact command to run — never block on a sudo password
# prompt from inside a script.
enable_linger() {
    if loginctl show-user "$target_user" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
        echo "   linger already enabled for $target_user (the service starts at boot)"
        return 0
    fi
    if loginctl enable-linger "$target_user" 2>/dev/null; then
        echo "   enabled linger for $target_user (the service starts at boot)"
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null &&
        sudo -n loginctl enable-linger "$target_user"; then
        echo "   enabled linger for $target_user (the service starts at boot)"
    else
        echo "   WARNING: could not enable linger — it will not start at boot" >&2
        echo "            run: sudo loginctl enable-linger $target_user" >&2
    fi
}

# The service runs as $target_user, so the groups rpi/install-rpi.sh grants that
# account are what let it open the mic (audio) and the pad (plugdev). A missing
# group is the usual reason a hand-started service cannot see the hardware, so
# say so at install time rather than at the first quip.
check_groups() {
    local groups missing=""
    groups=" $(id -nG "$target_user") "
    for grp in audio plugdev; do
        case "$groups" in
        *" $grp "*) ;;
        *) missing="$missing $grp" ;;
        esac
    done
    if [ -n "$missing" ]; then
        echo "   note: $target_user is not in:$missing — run rpi/install-rpi.sh," >&2
        echo "         then log out and back in before the service will see the hardware" >&2
    fi
}

# The journal command for a user unit. `journalctl --user -u` reads the per-user
# journal file (which journald ACLs to its owner), while `--user-unit=` matches
# the unit in the journal the system keeps. The latter is what works here and on
# Raspberry Pi OS (whose default user is in `adm`, which can read the journal);
# if `--user-unit` ever comes back empty, `journalctl --user -u dankbot-listen`
# is the alternative.
print_paths() {
    cat <<EOF

   unit:    $unit_path
   env:     $env_file   (DANKBOT_* settings)
   logs:    journalctl --user-unit=$unit_name -f   (quips + start/stop/exit)
EOF
}

cmd_install() {
    require_user_manager
    local bin
    bin="$(resolve_dankbot)" || die "cannot find the 'dankbot' command — install it (pipx install touchy-dankbot), run 'poetry install' in $repo_root, or set DANKBOT_BIN"

    log "Installing $unit_name for $target_user"
    write_env_file
    write_unit "$bin"
    echo "   ExecStart=$bin listen"
    echo "   WorkingDirectory=$repo_root"
    check_groups
    systemctl --user daemon-reload
    systemctl --user enable --now "$unit_name"
    enable_linger

    log "Done."
    print_paths
    cat <<EOF

   bash rpi/user-service.sh status     # is it up?
   bash rpi/user-service.sh logs       # watch the quips
EOF
}

# start / stop / restart / enable / disable are thin: systemd already owns the
# state, and `--no-pager` keeps the output from opening a pager in a pipe. The
# installed check is here so that a `start` before `install` says so, instead of
# leaving systemd's bare "Unit … not found" for the reader to decode.
cmd_simple() {
    require_user_manager
    if [ ! -f "$unit_path" ]; then
        die "$unit_name is not installed — run: bash rpi/user-service.sh install"
    fi
    systemctl --user "$1" --no-pager "$unit_name"
}

cmd_status() {
    require_user_manager
    if [ ! -f "$unit_path" ]; then
        die "$unit_name is not installed — run: bash rpi/user-service.sh install"
    fi
    local rc=0
    systemctl --user status --no-pager "$unit_name" || rc=$?
    print_paths
    return "$rc"
}

cmd_logs() {
    require_user_manager
    if [ ! -f "$unit_path" ]; then
        die "$unit_name is not installed — run: bash rpi/user-service.sh install"
    fi
    echo "following the journal for $unit_name — Ctrl-C to stop" >&2
    echo "(the same stream holds the unit's own start/stop/exit events)" >&2
    exec journalctl --user-unit="$unit_name" -n 200 -f --no-pager
}

cmd_uninstall() {
    require_user_manager
    if [ ! -f "$unit_path" ]; then
        die "$unit_name is not installed"
    fi
    log "Removing $unit_name"
    systemctl --user disable --now "$unit_name" || true
    rm -f -- "$unit_path"
    systemctl --user daemon-reload
    cat <<EOF

Removed $unit_path
Kept $env_file (settings) — delete it for a clean slate.
The service's journal entries stay until they age out (a volatile journal is
wiped at reboot anyway); "sudo journalctl --vacuum-time=1d" drops old entries
wholesale, not just this unit's.
EOF
    if loginctl show-user "$target_user" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
        cat <<EOF
Linger is still enabled for $target_user; turn it off with:
    sudo loginctl disable-linger $target_user
EOF
    fi
}

main() {
    case "${1:-install}" in
    install) cmd_install ;;
    start | stop | restart | enable | disable) cmd_simple "$1" ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
    uninstall | remove) cmd_uninstall ;;
    -h | --help | help) usage ;;
    *)
        echo "user-service.sh: unknown command '$1'" >&2
        usage >&2
        exit 2
        ;;
    esac
}

main "$@"

