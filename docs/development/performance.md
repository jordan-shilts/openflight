---
icon: lucide/timer
---

# Shot latency

What happens between the club meeting the ball and the shot appearing on the
screen and in the simulator, how long each step takes, and how to measure it
on your own rig.

## Where the time goes

A shot is not limited by the Pi's processor. It is limited by how fast the
radars can hand over what they recorded, and by fixed waits around the radar's
serial port.

| Step | Time | What sets it |
| --- | --- | --- |
| Sound trigger to the OPS243 | ~10 µs | Hardware (`GATE` to `HOST_INT`) |
| OPS243 buffer dump | ~1.8 s | 40,556 bytes at 230,400 baud |
| Clock sync with the OPS243 | 0.36 s, or up to ~1.6 s | Fixed waits: 36 reads 10 ms apart. Firmware that reports its clock in whole seconds is read until the second rolls over |
| Processing the capture | tens of ms | CPU: FFT timeline, spin, impact |
| Carry and trajectory | ~15 ms | CPU: the [ballistic simulator](../how-it-works/ballistics.md) |
| Sending to a simulator | ~1 ms | Network |
| Re-arming the OPS243, after the shot is out | 0.55 s | Fixed waits: 0.2 s of quiet line, then `PA`, `S#n`, `PA` |
| IWR6843 dump, alongside all of the above | several seconds | UART; see [the IWR6843 pages](../iwr6843/index.md) |
| IWR6843 launch angle (LCMF-v1) | ~0.4 s | CPU |

The clock sync runs after the last byte of the dump has arrived and **before
the capture is processed**, so its wait is part of the time to the first
numbers on every shot: at least 0.36 s on top of the 1.8 s dump. It has to be
there: it dates the impact, which the second radar's frames are matched on,
and the radar can only be talked to while it sits idle after its dump.

The re-arm used to run there too, another 0.55 s before a shot's first
numbers. It now follows the shot: the capture is processed and published, and
the radar is re-armed as the last thing done on the serial port before the
monitor goes back to reading (`SoundTrigger.finish_capture` in
`rolling_buffer/trigger.py`). The radar is as idle while the shot goes out as
it was during the clock sync, and the next swing is seconds away.

!!! warning "Not yet confirmed on hardware"

    The new order is covered by tests against a scripted radar: the shot is
    published before the re-arm, and the radar is re-armed exactly once after
    every capture, whatever becomes of it. It has not been run on an OPS243.
    Before relying on it, hit a dozen shots and check that every one is
    caught, that `[OPS] Rolling buffer re-armed` follows each `[SHOT]` line in
    the log, and that `shot_latency_report.py` shows "after the dump" about
    half a second shorter than before.

With an IWR6843 fitted, the UI shows the OPS243 numbers first and fills in the
launch angle when the second radar's dump has been read and processed. A
simulator is sent the shot once, when it is finished, so it waits for the
slower radar.

## Measured so far

**None of these numbers are from a Raspberry Pi.** They were taken on
2026-11-12 on a cloud Linux VM (Intel Xeon at 2.1 GHz, 4 threads, Python 3.11,
numpy 2.4). A Pi's processor is slower, so expect the CPU rows to be a few
times larger there; the fixed waits are the same on any machine.

| Stage (`bench_shot_pipeline.py`) | Median |
| --- | --- |
| Radar: clock sync, before the shot (fractional clock) | 360 ms |
| Radar: re-arm, after the shot | 551 ms |
| Parse the dump text | 0.3 ms |
| Process: speeds, spin, impact (24 recorded captures) | 34 ms |
| Write the raw capture to the session log | 0.4 ms |
| Carry: driver flight | 16 ms |
| Carry: 7-iron flight | 14 ms |
| Carry: wedge flight | 12 ms |
| Shot to the UI's JSON | 0.1 ms |
| Shot to OpenConnect bytes | 0.04 ms |

- Processing a capture splits roughly 60/40 between the multitaper spin
  estimate (mostly its multipath-fade fit, 61 least-squares solves a shot) and
  the 125-window FFT timeline.
- The carry simulation used to take 1.7 times as long (a drive: 26 ms, now
  15 ms). Its integrator now steps in plain numbers instead of building
  tuples; the results are bit-for-bit the same (checked over 6,912 launches)
  and pinned by `tests/test_ballistics.py::TestSimulatePinnedFlights`.
- LCMF-v1 on a synthetic 24-frame capture in the production (windowed
  snapshot) format took about 0.42 s. About a quarter of that is some 4,700
  small `np.median` calls and a fifth is 200 pseudo-inverses.
- Against Open-Birdie on the same VM, through the real connector: a shot was
  acknowledged in ~1 ms and on Open-Birdie's screen feed 10–17 ms after it was
  sent; a club chosen in Open-Birdie reached OpenFlight in ~1 ms.

## Measuring your own rig

All three scripts run without changing anything on the rig.

### The CPU stages, without hardware

```bash
uv run python scripts/analysis/bench_shot_pipeline.py
uv run python scripts/analysis/bench_shot_pipeline.py ~/openflight_sessions/session_*.jsonl
```

Times every stage in the table above. Given session logs it uses their
recorded captures; without any it uses a synthetic shot, so two machines can
be compared on the same input. `--json FILE` saves the report.

### Real shots, from the session logs

```bash
uv run python scripts/analysis/shot_latency_report.py ~/openflight_sessions/session_*.jsonl
```

Lays each recorded shot out as a timeline from the trigger: first byte of the
dump, radar ready (parsed, synced and processed), first numbers on the UI, the
finished shot, and the send to the simulator. "After the dump" is the
radar-ready time minus the dump's own transfer time: what the clock sync and
the processing add on your hardware (and, in logs from before the re-arm was
moved, the re-arm too). `--shots` prints every shot.

The server also logs a line per shot that needs no script:

```text
[SERVER] Initial OPS metrics emitted 2950ms after impact; hardware enrichment continues in background
[SHOT] #12: ball=148.2 mph, club=101.3, carry=251 yds | trigger=..., process=41ms, callback=3ms, total=...
```

### The link to the simulator

```bash
uv run python scripts/analysis/bench_sim_link.py --host <simulator address>
```

Connects with the connector the server uses and reports how long the
simulator took to accept the connection, what it calls itself, the club it
says is selected, and the round trip of a heartbeat. Run it on the Pi to
include the network between the two.

!!! warning "`--shots N` plays real shots"

    With `--shots` the script sends test shots and times each
    acknowledgement. The simulator plays every one of them: each counts as a
    stroke in whatever round or practice session is open.

## Related

- [Measurement pipeline](../how-it-works/pipeline.md)
- [Session log reference](../reference/session-log.md)
