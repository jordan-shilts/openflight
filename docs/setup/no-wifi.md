---
icon: lucide/wifi-off
---

# With or without Wi-Fi

OpenFlight and the simulator work the same on home Wi-Fi and with no Wi-Fi at
all. When home Wi-Fi isn't in range, the Pi makes its own network, the sim's
computer joins it, and shots go to the sim exactly as before.

## What it does

| Situation | What happens |
|---|---|
| Home Wi-Fi in range at boot | The Pi joins it. Nothing else changes. |
| No home Wi-Fi at boot | After 30 s the Pi starts the **OpenFlight** hotspot (Pi at `10.42.0.1`). |
| Home Wi-Fi drops mid-session | After 45 s without it, the hotspot starts. |
| Hotspot empty for 3 min | The Pi looks for home Wi-Fi for 20 s, and goes back to the hotspot if it isn't there. |
| Anyone on the hotspot | Left alone. |

Shots find the sim by name (`config/sim.json`, e.g. `my-mac.local`), which works
on either network. The sim's computer also always gets `10.42.0.10` on the
hotspot.

A shot taken while the link is down (the network switching, the sim's computer
rejoining) is held for up to 20 s and sent as soon as the sim is back. A sim that
acknowledges shots (Open-Birdie, GSPro) gets a shot it didn't acknowledge within
2 s again on a fresh connection; Open-Birdie plays a repeated shot only once.

Wi-Fi power saving is turned off on every network. On the Pi it adds tens to
hundreds of milliseconds of jitter between packets.

## Install (once, on the Pi)

```bash
sudo scripts/setup/network-fallback/install.sh
```

It is safe over Wi-Fi: the connection in use is never touched. Options:
`--sim-host NAME` (defaults to the host in `config/sim.json`), `--ssid NAME`,
`--band auto|a|bg` (auto picks 5 GHz when a Wi-Fi country is set, else 2.4 GHz;
a refused 5 GHz hotspot falls back to 2.4 GHz on its own), `--dry-run`.

The hotspot password is generated once and saved to
`~/.config/openflight/hotspot.txt`. Join **OpenFlight** with it once from the
sim's computer and any phone; after that they rejoin on their own when home
Wi-Fi isn't around.

```bash
/usr/local/sbin/openflight-netfallback --status   # what the Pi is on now
sudo scripts/setup/network-fallback/install.sh --uninstall
```

## Without internet

Nothing in a shot needs the internet. On the hotspot there is none, so:

- remote access (e.g. Tailscale) and the cloud uploader wait until the Pi is back
  on home Wi-Fi;
- after a reboot with no internet, the Pi's clock starts from the last time it
  knew (a Pi 5 keeps time across power-off only with an RTC battery), so
  session timestamps can be off until it next sees the internet.
