---
icon: lucide/activity
---

# Accuracy & Reliability Report

`openflight-accuracy report` reads your session logs and answers three
questions without a reference launch monitor:

- **Reliability.** How often did each stage produce a measurement: a trigger,
  a launch angle, an IWR6843 capture, a shot delivered to the simulator?
- **Consistency.** Do the sensors agree with each other and with physics?
- **Drift.** Did anything shift from one session to the next?

It only reads logs. It never changes a live number.

!!! note "What it cannot see"

    A bias shared by every reading, such as ball speed 1.5 mph low on every
    shot, looks exactly like the truth from inside the system. Finding and
    correcting bias needs shots paired with a reference instrument. This
    report tells you whether the system is working as designed and whether
    something changed.

## Run it

```bash
uv run openflight-accuracy report
```

That reads every `session_*.jsonl` in `~/openflight_sessions`. You can also
pass files or directories, write the Markdown to a file, and keep a JSON copy
for scripts:

```bash
uv run openflight-accuracy report ~/openflight_sessions/session_20261106_*.jsonl \
  --output report.md --json report.json
```

Mock sessions are left out unless you add `--include-mock`.

## What it reports

### Reliability

| Column | Source | Expect |
| --- | --- | --- |
| Triggers accepted | `trigger_event.accepted` | Close to every trigger; rejection reasons are listed under Details |
| Vertical / horizontal angle measured | `launch_angle_*_source` is `radar` or `camera`, not the `estimated` fallback | Close to every shot with an angle radar fitted |
| IWR6843 accepted | `iwr6843_capture` LCMF status starts with `accepted` | Close to every capture |
| Single-channel angles | `single_channel` on accepted LCMF angles | Few; each rests on one model, see [OPS-guided recovery](../iwr6843/low-confidence-recovery.md) |
| Sim forwarded | Distinct `sim_send` shot numbers per shot | Every shot while a simulator is connected |
| Errors | `error` entries, grouped by `context.component` under Details | None |

Rates show a 95% Wilson interval in the JSON output, so a 9-of-10 session and a
90-of-100 session are not mistaken for the same evidence.

### Consistency

| Column | Check |
| --- | --- |
| IWR6843/OPS243 speed | IWR6843 range-track speed over the OPS243 speed it was matched to. Outside ±15% is the rule the runtime uses to start [OPS-guided recovery](../iwr6843/low-confidence-recovery.md). |
| LCMF disagreement | Standard deviation between the LCMF channels behind a corroborated angle. |
| Club speed implies smash > 1.50 | A measured club speed that would make the smash factor physically impossible. The server already drops the smash factor for these shots. |
| Enclosure pitch range | Inclinometer pitch across the session. A spread means the mount moved. |

### Compensation in effect

Which corrections were active for each session: the ball-speed cosine
correction (shots that logged `ball_speed_raw_mph`), where spin came from, and
whether inclinometer tilt was applied to the IWR6843.

## Drift across sessions

Each session is compared with the pooled sessions before it, back to the most
recent shift: Fisher's exact test for rates and the Mann-Whitney U test for
measurements. Mann-Whitney only ranks values, so a few wild readings cannot
decide it. A series is reported as shifted when p < 0.001 **and** the change is
at least:

| Series | Minimum change |
| --- | --- |
| Triggers accepted, vertical angle measured, IWR6843 accepted, single-channel angles | 10 percentage points |
| IWR6843 track speed / OPS243 ball speed | 0.02 |
| LCMF channel disagreement | 0.5° |
| Enclosure pitch | 0.25° |

The minimum keeps a long history from flagging changes too small to matter.
The shifted session then starts the next reference, so a lasting change is
reported once. Sessions with fewer than 10 samples of a series are left out of
that series' comparisons.

On simulated data (1,000 histories of 30 sessions with 30 shots each and no
real change), the speed ratio was never flagged and a 95% rate was flagged in
0.9% of histories. With a change halfway through, a speed ratio moving from
1.00 to 0.97 was caught every time, and a rate falling from 95% to 80% in 81%
of histories.

### When a series shifts

| Series | Look at |
| --- | --- |
| IWR6843 track speed / OPS243 ball speed | Installation geometry: tee distance, radar height, mount. The ball-speed cosine correction uses the same geometry. |
| Enclosure pitch | The mount moved. IWR6843 tilt follows the inclinometer live; the OPS243 geometry and aim do not. |
| LCMF disagreement, single-channel angles | Floor multipath and antenna height; see [calibration](../iwr6843/calibration.md). |
| IWR6843 accepted, vertical angle measured | The IWR6843 status counts under Details for the shifted session. |
| Triggers accepted | Sound-trigger sensitivity and room noise; see [sound trigger](../build/sound-trigger.md). |

## Limits

- No reference instrument, so no bias correction. Paired sessions with a
  reference are what that needs.
- Club and player mix matter. A session of drivers can legitimately differ
  from a session of wedges in angle coverage or LCMF agreement.
- Drift is located to a session, not to a shot.
- Logs from builds without an IWR6843 or inclinometer have no data for those
  checks; the report says which series it could not compare.

## Related

- [Session log schema](../reference/session-log.md)
- [Measurement pipeline](../how-it-works/pipeline.md)
- [IWR6843 calibration and replay](../iwr6843/calibration.md)
