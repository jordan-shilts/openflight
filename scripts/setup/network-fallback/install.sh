#!/bin/bash
#
# Let OpenFlight and the sim work the same with or without home Wi-Fi.
#
# Installs, on the Pi (NetworkManager, Raspberry Pi OS Bookworm or later):
#   * a hotspot profile "openflight-hotspot" (WPA2, Pi at 10.42.0.1). It is
#     never started by NetworkManager itself, only by the fallback below, so
#     home Wi-Fi always wins when it's in range.
#   * the fallback (openflight-netfallback): at boot, home Wi-Fi or else the
#     hotspot; every 15 s, hotspot after a 45 s home Wi-Fi outage, and back to
#     home Wi-Fi when the hotspot has sat empty for 3 minutes.
#   * a fixed hotspot address for the sim's computer (10.42.0.10), by the
#     name it gives when it joins.
#   * Wi-Fi power saving off on every network: the Pi's Wi-Fi otherwise
#     naps between packets and adds tens to hundreds of ms of jitter.
#
# The connection in use is never touched: installing over Wi-Fi is safe.
#
# Usage (on the Pi, from the repo):
#   sudo scripts/setup/network-fallback/install.sh [--sim-host NAME] [--ssid NAME]
#        [--band auto|a|bg] [--dry-run]
#   sudo scripts/setup/network-fallback/install.sh --uninstall
#
# --sim-host defaults to the host in config/sim.json (".local" dropped).
# The hotspot password is generated once and written to
#   ~/.config/openflight/hotspot.txt  (readable by the invoking user only)
# Re-running keeps the password and is otherwise idempotent.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
CON="openflight-hotspot"
IFACE="${OPENFLIGHT_WIFI_IFACE:-wlan0}"
PI_ADDR="10.42.0.1"
SIM_ADDR="10.42.0.10"
SSID="OpenFlight"
BAND="auto"
SIM_HOST=""
DRY_RUN=0
UNINSTALL=0

NM_DIR="${NM_DIR:-/etc/NetworkManager}"
SBIN_DIR="${SBIN_DIR:-/usr/local/sbin}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$USER_NAME" 2>/dev/null | cut -d: -f6 || true)"
USER_HOME="${USER_HOME:-$HOME}"
CRED_FILE="$USER_HOME/.config/openflight/hotspot.txt"
SIM_CONFIG="${OPENFLIGHT_SIM_CONFIG:-$REPO/config/sim.json}"

while [ $# -gt 0 ]; do
    case "$1" in
        --sim-host) SIM_HOST="$2"; shift 2 ;;
        --ssid) SSID="$2"; shift 2 ;;
        --band) BAND="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h | --help) sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

run() {
    if [ "$DRY_RUN" = 1 ]; then
        echo "would run: $*"
    else
        "$@"
    fi
}

write_file() { # path mode, content on stdin
    if [ "$DRY_RUN" = 1 ]; then
        echo "would write $1:"
        sed 's/^/    /'
        return
    fi
    mkdir -p "$(dirname "$1")"
    cat >"$1"
    chmod "$2" "$1"
}

UNITS="openflight-netfallback-boot.service openflight-netfallback.service openflight-netfallback.timer"

if [ "$UNINSTALL" = 1 ]; then
    run systemctl disable --now openflight-netfallback.timer openflight-netfallback-boot.service || true
    for u in $UNITS; do run rm -f "$UNIT_DIR/$u"; done
    run rm -f "$SBIN_DIR/openflight-netfallback"
    run rm -f "$NM_DIR/dnsmasq-shared.d/openflight.conf" "$NM_DIR/conf.d/openflight-wifi-powersave.conf"
    if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$CON"; then
        run nmcli connection delete "$CON"
    fi
    run systemctl daemon-reload
    echo "Removed. Home Wi-Fi is unchanged; $CRED_FILE was left in place."
    exit 0
fi

if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then
    echo "run with sudo" >&2
    exit 1
fi
command -v nmcli >/dev/null || { echo "NetworkManager (nmcli) is required" >&2; exit 1; }
# iw counts who is on the hotspot and turns power saving off right away. Not
# on every image; the fallback copes without it, so a failed install only warns.
if ! command -v iw >/dev/null 2>&1; then
    run apt-get install -y iw || echo "could not install iw; carrying on without it" >&2
fi

if [ -z "$SIM_HOST" ] && [ -f "$SIM_CONFIG" ]; then
    SIM_HOST="$(python3 -c '
import json, sys
hosts = [c.get("host", "") for c in json.load(open(sys.argv[1])).get("connectors", []) if c.get("enabled")]
print(next((h for h in hosts if h not in ("", "127.0.0.1", "localhost")), ""))
' "$SIM_CONFIG" 2>/dev/null || true)"
fi
SIM_HOST="${SIM_HOST%.local}"
case "$SIM_HOST" in
    *[!A-Za-z0-9-]* ) echo "skipping the fixed address: '$SIM_HOST' is not a plain host name" >&2; SIM_HOST="" ;;
esac

if [ "$BAND" = auto ]; then
    country="$( (iw reg get 2>/dev/null || true) | sed -n 's/^country \([A-Z0-9]*\):.*/\1/p' | head -n 1)"
    if [ -z "$country" ] && command -v raspi-config >/dev/null 2>&1; then
        country="$(raspi-config nonint get_wifi_country 2>/dev/null || true)"
    fi
    if [ -n "$country" ] && [ "$country" != "00" ]; then BAND=a; else BAND=bg; fi
fi
if [ "$BAND" = a ]; then CHANNEL=36; else CHANNEL=6; fi

# --- hotspot profile (password generated once, then kept) ---
SETTINGS=(
    connection.interface-name "$IFACE"
    connection.autoconnect no
    802-11-wireless.ssid "$SSID"
    802-11-wireless.mode ap
    802-11-wireless.band "$BAND"
    802-11-wireless.channel "$CHANNEL"
    802-11-wireless.powersave 2
    ipv4.method shared
    ipv4.addresses "$PI_ADDR/24"
    ipv6.method disabled
    wifi-sec.key-mgmt wpa-psk
    wifi-sec.proto rsn
    wifi-sec.pairwise ccmp
    wifi-sec.group ccmp
)
if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$CON"; then
    run nmcli connection modify "$CON" "${SETTINGS[@]}"
    PSK=""
else
    # 16 characters, no look-alikes (0/O, 1/l/I), easy to type on a phone.
    PSK="$(python3 -c 'import secrets; a = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789"; print("".join(secrets.choice(a) for _ in range(16)))')"
    run nmcli connection add type wifi con-name "$CON" "${SETTINGS[@]}" wifi-sec.psk "$PSK"
fi
if [ -n "$PSK" ]; then
    write_file "$CRED_FILE" 600 <<CRED
OpenFlight hotspot (used when there is no home Wi-Fi)
Network:  $SSID
Password: $PSK
Pi:       $PI_ADDR
Sim computer: $SIM_ADDR${SIM_HOST:+ ($SIM_HOST)}
CRED
    [ "$DRY_RUN" = 1 ] || chown "$USER_NAME" "$CRED_FILE" "$(dirname "$CRED_FILE")" 2>/dev/null || true
fi

# --- fixed address for the sim's computer on the hotspot ---
if [ -n "$SIM_HOST" ]; then
    write_file "$NM_DIR/dnsmasq-shared.d/openflight.conf" 644 <<CONF
# The sim's computer always gets the same address on the OpenFlight hotspot.
dhcp-host=$SIM_HOST,$SIM_ADDR
CONF
fi

# --- Wi-Fi power saving off, on every network ---
write_file "$NM_DIR/conf.d/openflight-wifi-powersave.conf" 644 <<'CONF'
# Pi Wi-Fi power saving adds tens to hundreds of ms of latency jitter.
[connection]
wifi.powersave = 2
CONF
run iw dev "$IFACE" set power_save off || true

# --- the fallback ---
run install -m 755 "$HERE/openflight-netfallback" "$SBIN_DIR/openflight-netfallback"
for u in $UNITS; do run install -m 644 "$HERE/$u" "$UNIT_DIR/$u"; done
run systemctl daemon-reload
run systemctl enable openflight-netfallback-boot.service
run systemctl enable --now openflight-netfallback.timer

if [ "$BAND" = a ]; then BAND_NAME="5 GHz"; else BAND_NAME="2.4 GHz"; fi
echo
echo "Done. Hotspot: '$SSID' on $BAND_NAME channel $CHANNEL, Pi at $PI_ADDR."
if [ -n "$SIM_HOST" ]; then echo "The sim's computer ($SIM_HOST) gets $SIM_ADDR on it."; fi
if [ -n "$PSK" ]; then echo "Password saved to $CRED_FILE"; fi
echo "Status: $SBIN_DIR/openflight-netfallback --status"
