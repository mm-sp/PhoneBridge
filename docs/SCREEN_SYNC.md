# Screen sync: bidirectional app launch + smart display-off

Opening PhoneBridge on either device opens it on the other, starts mirroring, and (when it can) turns the phone's
physical display off while the PC keeps showing and controlling it. Everything rides on the existing authenticated
control channel (pinned TLS + v4 handshake + AES-GCM); nothing new is listening.

## What Android allows (and what we do about it)

| Need | Mechanism | Needs | Verified |
|---|---|---|---|
| Panel powered off, capture continues | PC runs `adb shell cmd display power-off 0`; restore = `cmd display power-reset 0` (there is **no** `power-on`) | USB debugging / Wireless Debugging authorised for this PC. No root. | Mi A3, LineageOS, Android 17 (see below) |
| No ADB | **Dim only**: transparent 1-window overlay asking minimum brightness. Reported as *Dimmed*, never *Off* | "Display over other apps" | Not hardware-verified |
| Lock via accessibility (`GLOBAL_ACTION_LOCK_SCREEN`) | Not used: on this device a sleep turned the MediaProjection virtual display off and ended the capture within 5 s | | observed |
| Open the phone app from the PC | Phone service `startActivity` (allowed in the background by "Display over other apps"), else `adb shell am start`, else a notification asking for a tap | | launch verified |
| Screen-capture consent | Always Android's own "Start now" prompt. Never bypassed, never reused (a reused grant yields no frames on current Android) | a person | |

`MediaProjection` alone cannot power the panel off. scrcpy's `--turn-screen-off` does it from a process running as the
`shell` user (`SurfaceControl.setDisplayPowerMode(…, POWER_MODE_OFF)`; on Android 14+ via `DisplayControl`), and restores
it from a detached cleanup process so a killed client does not leave the phone dark. We cannot make that call from the
app (hidden API, shell privilege), but `cmd display power-off/power-reset` is the same display-power request exposed by
the system shell, so the PC does it over ADB, and we copy scrcpy's safety idea: a **pending-restore marker** on disk
(`~/.config/phonebridge/screensync_pending.json`) that the next start honours, plus a phone-side watchdog.

### Hardware result (Xiaomi Mi A3, LineageOS, Android 17 / API 37, 2026-11-15) - author-reported, not re-run in the 2026-11-19 audit
* `cmd display power-off 0` → `mState=OFF` while `mWakefulness=Awake`; `power-reset 0` → `mState=ON`.
* With mirroring running and the panel forced off, opening Settings on the dark phone produced +55 frames
  (~120 kB/s) on the PC: **the capture keeps streaming real content with the panel off.**
* Auto policy (Automatic mode, ADB verified): frames confirmed (41) → `DISPLAY_OFF`; 45 s of a still screen stayed
  `DISPLAY_OFF` thanks to the liveness probe; stopping from the PC restored the panel.
* PC crash with the panel off (`kill -9`) **without** the watchdog: panel stayed dark until a fresh PC process restored it
  from the marker (0.7 s). With the watchdog (below) the panel came back on its own 1.4 s after the kill and the marker was
  gone 2.0 s after it (`tools/hw_screensync_check.py watchdog`).
* **Touch is dropped while the panel is powered off** — even `adb shell input tap` does nothing — whereas HOME/BACK and
  accessibility node actions still work. With the panel off the phone therefore presses controls through accessibility
  (click / long-click on the node under the tap, scroll for a swipe). Hardware: HOME, tap (+36 mirror frames) and BACK pass
  with the panel off (`hw_screensync_check.py control`); scrolling moved the list and showed +7 frames in one run, 0 in another.
* Android re-sleeps a locked phone ~10 s after its lock screen wakes, which cancelled the capture prompt before anyone could
  unlock. The phone now holds a screen wake lock while the prompt is pending (60 s max). Same-harness A/B on a sleeping,
  locked phone: display ON in 0/19 samples without it, 19/19 with it (`hw_screensync_check.py consent`).
* A natural sleep (screen timeout) is different: the virtual display went OFF and the capture stopped ~5 s later. Only
  the forced power-off (device stays *Awake*) keeps streaming.
* `cmd display help` exits **255** on this device even though it prints the help; probes must not require exit 0.

## Architecture

```
Ubuntu                                              Android
ui/sync_page.py  dashboard                          MirrorPanel.kt  dashboard
screensync/                                         SyncCore.kt     states · commands/validator · AppLaunchSyncManager
  states.py        10-state session machine           ScreenSync.kt   phone-side controller + SyncPrefs
  commands.py      wire format, validation, replay    DisplayPower.kt dim overlay, restore, liveness probe, watchdog flag
  launch_sync.py   AppLaunchSyncManager             (hooks in BridgeService, Video.kt, MainActivity)
  display_power.py DisplayPowerManager + strategies
  adb_bridge.py    injectable adb wrapper
  controller.py    ScreenSyncController (orchestrator)
  glue.py          builds it for the GTK app
  settings.py      Smart Screen Mode, toggles
```
The PC orchestrates (it knows when frames arrive and holds ADB); the phone validates every command, does what only it
can (open the app, start capture, restore the display) and mirrors the state machine for its dashboard.

### Messages (control channel)
`sync.command {cmd, session_id, command_id, ts, origin, args}` and `sync.ack {command_id, ok, result, reason}`.
Commands: `OPEN_PHONE_BRIDGE` (PC→phone), `OPEN_PHONE_BRIDGE_VIEWER` (phone→PC), `START_MIRRORING`, `STOP_MIRRORING`,
`DISPLAY_OFF`, `DISPLAY_ON` (`args.performed=true` = already done by the sender over ADB; `false`+`mode=dim` = please do it),
`MIRROR_READY` (PC→phone, frames confirmed), `CONNECTION_STATUS` (state, display, app_open, fps, resolution, `probe_ts`/`echo_ts`
latency, `smart`, `dim_ok`, `probe_content`). The existing `mirror.start/stop`, `video.*`, `mirror.input` messages are reused
for the actual capture and remote control.

### Flows
* **PC→phone:** window opened by the user → `OPEN_PHONE_BRIDGE` (adb `am start` fallback) → mirror starts (Android's prompt)
  → ≥3 frames → `MIRROR_READY` → display off → input from the PC.
* **Phone→PC:** user opens the app → `OPEN_PHONE_BRIDGE_VIEWER` (or `START_MIRRORING` if the viewer is already open) → viewer
  opens, mirror starts → same as above.
* **Loops:** only `origin=user` launches are forwarded; a launch caused by a command carries `origin=auto` (intent extra
  `pb_origin` / `PHONEBRIDGE_ORIGIN=auto`) and is never echoed; already-open is acknowledged, not relaunched; cooldown (5 s) and
  circuit breaker (4/min send, 3/30 s execute) bound the rate both ways.

### Display safety
Display is touched only after frames are confirmed. After power-off the PC **proves** the stream is live by asking the phone
to flicker a 1-pixel overlay and waiting for a frame (a still screen sends no frames, so silence proves nothing). Failure →
panel restored, device not retried this session. Restored on: stop, error, video stopped on phone, link loss (PC via ADB *and*
the phone by itself via `DisplayPower.restore`), window close, process restart (marker / persisted flag). While dark it re-probes
every 20 s; two failures restore the panel.

**Watchdog (`screensync/display_watchdog.py`).** Right after a real power-off the PC starts a tiny detached process (own
session, so it outlives a hard kill of the app). It watches the marker: if the marker disappears (normal restore) it exits; if
the owning PC process is dead while the marker is still there it runs `power-reset`, wakes the phone, verifies the panel and
removes the marker (retrying for 45 s if adb is briefly unreachable). It never deletes a marker a newer off-cycle wrote.
`PHONEBRIDGE_NO_WATCHDOG=1` disables it. The restart-time marker recovery stays as the second line of defence.

## Security
Commands are accepted only from the authenticated peer (existing handshake) and only if the device's **Screen** permission is
Allow/granted (`sync.*` maps to `screen`). On the phone, `START_MIRRORING` starts a capture only while the phone's own
**Screen** permission is Allow: with Deny it is refused (`permission_denied`), with Ask it only acknowledges
(`awaiting_permission`) and the accompanying `mirror.start` raises the prompt. Per-command allow-list by direction, strict argument schemas, `command_id` replay
cache (2048 / 15 min, duplicate → idempotent ack), ±5 min timestamp window, session binding, never raises on junk. Remote input
needs the PhoneBridge Accessibility service the user enabled on the phone; the capture needs Android's own prompt each time.
ADB use is limited to `cmd display power-*`/`help`, `dumpsys display|power`, `getprop ro.build.version.sdk`, `ip -o -4 addr` (to match the phone), `adb connect <your Wireless Debugging address>`, `am start -n dev.phonebridge/.MainActivity`, `input keyevent
KEYCODE_WAKEUP`; serials/components are validated and never shell-interpolated.

## Settings
Smart Screen Mode — *Automatic* (real power-off only, when ADB is verified), *Enabled* (dim fallback allowed, labelled), *Disabled*;
automatic app sync; automatic screen-off; reconnect. Either device's *Disabled* wins for automatic actions; an explicit "Turn
screen off" click overrides. Defaults: Automatic, sync on, screen-off on, reconnect on. Opening the PC app with sync on starts a
mirroring session (the phone shows Android's prompt): turn off *Automatic app synchronisation* to stop that.

## Known limitations
* **Remote control with the panel off** uses accessibility actions, so it works for standard controls, lists and text fields but
  not for custom-drawn surfaces (games, maps, canvas views): those need the screen on. The phone tells the PC when a tap found
  nothing to press.
* Real power-off needs ADB. Without it only dimming is possible. Wireless Debugging pairing is done by the user.
* Verified on one device/ROM (above). `cmd display power-off` exists on Android 14+/LineageOS 21+ builds; others get a clear
  "not supported" note and the display stays on. Vendor ROMs may behave differently: the liveness probe is what protects you.
* Android's prompt must be accepted by a person every session and needs the phone unlocked (it appears over the lock screen
  and asks for the fingerprint/PIN). If it is not accepted, the PC times out after 60 s with a clear message.
* Screen rotation: the existing capture is created at one size and not resized on rotation, so rotation sync is **not**
  implemented; the PC renderer already accepts a rotation value but the phone always reports 0.
* Latency shown is control-channel round trip, not video glass-to-glass.
* Phone-side dim overlay and the probe need "Display over other apps".
