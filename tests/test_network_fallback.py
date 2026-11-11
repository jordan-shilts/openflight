"""The Pi's home-Wi-Fi / own-hotspot fallback (scripts/setup/network-fallback).

The script drives NetworkManager through nmcli and iw. Here those are small
stand-ins that keep the Wi-Fi state in files, and `sleep`/`date` advance a fake
clock, so every case runs in milliseconds: boot at home, boot away, home Wi-Fi
dropping mid-session, a hotspot in use, an empty hotspot looking for home again,
and a refused 5 GHz hotspot falling back to 2.4 GHz.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "setup"
    / "network-fallback"
    / "openflight-netfallback"
)
HOTSPOT = "openflight-hotspot"

NMCLI = r"""#!/bin/bash
S="$STUB"
echo "nmcli $*" >>"$S/calls"
case "$*" in
  "-t -f GENERAL.CONNECTION device show wlan0") echo "GENERAL.CONNECTION:$(cat "$S/con")" ;;
  "-t -f GENERAL.STATE device show wlan0") echo "GENERAL.STATE:$(cat "$S/state") (x)" ;;
  "connection up openflight-hotspot")
      if [ "$(cat "$S/band")" = a ] && [ -f "$S/refuse_a" ]; then exit 4; fi
      echo openflight-hotspot >"$S/con"; echo 100 >"$S/state" ;;
  "connection down openflight-hotspot") echo -- >"$S/con"; echo 30 >"$S/state" ;;
  "-g 802-11-wireless.band connection show openflight-hotspot") cat "$S/band" ;;
  "connection modify openflight-hotspot 802-11-wireless.band bg"*) echo bg >"$S/band" ;;
  "device wifi rescan ifname wlan0") ;;
  *) echo "unexpected: $*" >&2; exit 9 ;;
esac
"""

IW = r"""#!/bin/bash
n=$(cat "$STUB/stations"); i=0
while [ "$i" -lt "$n" ]; do echo "Station aa:bb (on wlan0)"; i=$((i + 1)); done
"""

DATE = r"""#!/bin/bash
cat "$STUB/now"
"""

# Each second of sleep: if home Wi-Fi is in range and nothing holds the radio,
# NetworkManager's autoconnect brings it up after `home_delay` seconds.
SLEEP = r"""#!/bin/bash
S="$STUB"
echo $(( $(cat "$S/now") + 1 )) >"$S/now"
if [ -f "$S/home_in_range" ] && [ "$(cat "$S/con")" = "--" ]; then
  d=$(( $(cat "$S/home_delay") - 1 )); echo $d >"$S/home_delay"
  if [ $d -le 0 ]; then echo Home >"$S/con"; echo 100 >"$S/state"; fi
fi
"""


class Pi:
    def __init__(self, tmp_path: Path):
        self.stub = tmp_path / "stub"
        self.bin = tmp_path / "bin"
        self.state_dir = tmp_path / "state"
        self.stub.mkdir()
        self.bin.mkdir()
        for name, body in (("nmcli", NMCLI), ("iw", IW), ("date", DATE), ("sleep", SLEEP)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.set(con="--", state="30", stations="0", band="a", now="1000", home_delay="3")
        (self.stub / "calls").write_text("")

    def set(self, **files):
        for name, value in files.items():
            (self.stub / name).write_text(f"{value}\n")

    def flag(self, name, on=True):
        path = self.stub / name
        if on:
            path.write_text("1\n")
        elif path.exists():
            path.unlink()

    def get(self, name):
        return (self.stub / name).read_text().strip()

    def advance(self, seconds):
        self.set(now=int(self.get("now")) + seconds)

    @property
    def calls(self):
        return (self.stub / "calls").read_text().splitlines()

    def run(self, mode):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "STUB": str(self.stub),
            "OPENFLIGHT_NETFALLBACK_STATE": str(self.state_dir),
            "OPENFLIGHT_BOOT_WAIT_S": "30",
            "OPENFLIGHT_LOST_GRACE_S": "45",
            "OPENFLIGHT_IDLE_RETRY_S": "180",
            "OPENFLIGHT_SEARCH_S": "20",
        }
        result = subprocess.run(
            ["bash", str(SCRIPT), mode], env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        return result.stdout


@pytest.fixture
def pi(tmp_path):
    return Pi(tmp_path)


def _ups(pi):
    return [c for c in pi.calls if c == f"nmcli connection up {HOTSPOT}"]


def test_boot_at_home_never_starts_the_hotspot(pi):
    pi.flag("home_in_range")
    pi.run("--boot")
    assert pi.get("con") == "Home"
    assert _ups(pi) == []


def test_boot_away_from_home_starts_the_hotspot_after_the_wait(pi):
    out = pi.run("--boot")
    assert pi.get("con") == HOTSPOT
    assert _ups(pi) == [f"nmcli connection up {HOTSPOT}"]
    assert int(pi.get("now")) == 1000 + 30
    assert "starting the hotspot" in out


def test_boot_with_slow_home_wifi_waits_for_it(pi):
    pi.flag("home_in_range")
    pi.set(home_delay="25")
    pi.run("--boot")
    assert pi.get("con") == "Home" and _ups(pi) == []


def test_check_on_home_wifi_does_nothing(pi):
    pi.set(con="Home", state="100")
    pi.run("--check")
    assert [c for c in pi.calls if "connection" in c and "show" not in c] == []


def test_home_wifi_outage_must_last_the_grace_period(pi):
    pi.set(con="--", state="30")
    pi.run("--check")  # outage starts
    pi.advance(30)
    pi.run("--check")
    assert _ups(pi) == []
    pi.advance(15)
    pi.run("--check")
    assert pi.get("con") == HOTSPOT


def test_brief_outage_resets_when_home_wifi_comes_back(pi):
    pi.run("--check")
    pi.advance(40)
    pi.set(con="Home", state="100")
    pi.run("--check")  # back: the outage clock is cleared
    pi.set(con="--", state="30")
    pi.run("--check")
    pi.advance(40)
    pi.run("--check")
    assert _ups(pi) == []


def test_hotspot_in_use_is_never_touched(pi):
    pi.set(con=HOTSPOT, state="100", stations="2")
    for _ in range(30):
        pi.run("--check")
        pi.advance(15)
    assert not any("down" in c for c in pi.calls)


def test_empty_hotspot_goes_back_to_home_wifi_when_it_is_in_range(pi):
    pi.set(con=HOTSPOT, state="100", stations="0")
    pi.flag("home_in_range")
    pi.run("--check")
    pi.advance(179)
    pi.run("--check")
    assert not any("down" in c for c in pi.calls)
    pi.advance(1)
    pi.run("--check")
    assert pi.get("con") == "Home"
    assert f"nmcli connection down {HOTSPOT}" in pi.calls


def test_empty_hotspot_comes_back_when_home_wifi_is_still_away(pi):
    pi.set(con=HOTSPOT, state="100", stations="0")
    pi.run("--check")
    pi.advance(180)
    pi.run("--check")
    assert pi.get("con") == HOTSPOT
    # and it waits a full idle period before the next look
    calls_before = len(pi.calls)
    pi.advance(100)
    pi.run("--check")
    assert not any("down" in c for c in pi.calls[calls_before:])


def test_someone_joining_resets_the_idle_clock(pi):
    pi.set(con=HOTSPOT, state="100", stations="0")
    pi.run("--check")
    pi.advance(170)
    pi.set(stations="1")
    pi.run("--check")
    pi.set(stations="0")
    pi.run("--check")
    pi.advance(100)
    pi.run("--check")
    assert not any("down" in c for c in pi.calls)


def test_refused_5ghz_hotspot_falls_back_to_2_4ghz(pi):
    pi.flag("refuse_a")
    out = pi.run("--boot")
    assert pi.get("band") == "bg"
    assert pi.get("con") == HOTSPOT
    assert "2.4 GHz" in out


def test_garbled_state_file_is_treated_as_new(pi):
    pi.state_dir.mkdir()
    (pi.state_dir / "lost-since").write_text("garbage\n")
    pi.run("--check")
    assert _ups(pi) == []
    assert (pi.state_dir / "lost-since").read_text().strip() == "1000"


def test_status_reports_mode(pi):
    pi.set(con=HOTSPOT, state="100", stations="1")
    assert "hotspot (1 connected)" in pi.run("--status")
    pi.set(con="Home")
    assert "home Wi-Fi" in pi.run("--status")


# --- installer ---------------------------------------------------------------

INSTALLER = SCRIPT.parent / "install.sh"

INSTALL_NMCLI = r"""#!/bin/bash
echo "nmcli $*" >>"$STUB/calls"
case "$*" in
  "-t -f NAME connection show") cat "$STUB/profiles" ;;
esac
"""

INSTALL_IW = r"""#!/bin/bash
echo "iw $*" >>"$STUB/calls"
if [ "$*" = "reg get" ]; then printf 'global\ncountry %s: DFS-FCC\n' "$(cat "$STUB/country")"; fi
"""


def _installer(
    tmp_path, *args, profiles="Home\n", country="US", sim_host="Jordans-MacBook-Pro-2.local", with_iw=True
):
    stub = tmp_path / "stub"
    bin_dir = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    bin_dir.mkdir(exist_ok=True)
    (stub / "calls").write_text("")
    (stub / "profiles").write_text(profiles)
    (stub / "country").write_text(country)
    for name, body in (("nmcli", INSTALL_NMCLI), ("iw", INSTALL_IW)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    if not with_iw:
        (bin_dir / "iw").unlink()
    sim = tmp_path / "sim.json"
    sim.write_text(
        '{"connectors": [{"type": "gspro", "enabled": true, "host": "%s", "port": 921}]}'
        % sim_host
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB": str(stub),
        "OPENFLIGHT_SIM_CONFIG": str(sim),
        "HOME": str(tmp_path / "home"),
    }
    result = subprocess.run(
        ["bash", str(INSTALLER), "--dry-run", *args],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, (stub / "calls").read_text().splitlines()


def test_installer_adds_a_hotspot_without_touching_the_live_connection(tmp_path):
    out, calls = _installer(tmp_path)
    assert "would run: nmcli connection add type wifi con-name openflight-hotspot" in out
    assert "connection.autoconnect no" in out
    assert "802-11-wireless.band a 802-11-wireless.channel 36" in out
    assert "dhcp-host=Jordans-MacBook-Pro-2,10.42.0.10" in out
    assert "wifi.powersave = 2" in out
    assert "Password: " in out and "hotspot.txt" in out
    assert "systemctl enable --now openflight-netfallback.timer" in out
    # never brings a connection up or down, never edits home Wi-Fi
    assert not any(" up " in c or " down " in c for c in calls)
    assert not any("modify Home" in line for line in out.splitlines())


def test_installer_keeps_an_existing_password(tmp_path):
    out, _ = _installer(tmp_path, profiles="Home\nopenflight-hotspot\n")
    assert "would run: nmcli connection modify openflight-hotspot" in out
    assert "wifi-sec.psk" not in out
    assert "Password: " not in out


def test_installer_uses_2_4ghz_without_a_wifi_country(tmp_path):
    out, _ = _installer(tmp_path, country="00")
    assert "802-11-wireless.band bg 802-11-wireless.channel 6" in out


def test_installer_skips_the_reservation_for_an_address_host(tmp_path):
    out, _ = _installer(tmp_path, sim_host="10.0.0.132")
    assert "dhcp-host" not in out


def test_counts_hotspot_devices_without_iw(pi):
    (pi.bin / "iw").unlink()
    ip = pi.bin / "ip"
    ip.write_text(
        "#!/bin/bash\n"
        "printf '10.42.0.10 lladdr aa:bb REACHABLE\\n10.42.0.11 FAILED\\n' \n"
    )
    ip.chmod(0o755)
    pi.set(con=HOTSPOT, state="100")
    assert "hotspot (1 connected)" in pi.run("--status")


def test_installer_adds_iw_when_missing(tmp_path):
    out, _ = _installer(tmp_path)
    assert "would run: apt-get install -y iw" not in out  # the stub provides iw
    out, _ = _installer(tmp_path, with_iw=False)
    assert "would run: apt-get install -y iw" in out
    assert "802-11-wireless.band bg" in out
