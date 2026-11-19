"""Bidirectional screen-mirroring session sync + smart display-off (see docs/SCREEN_SYNC.md).

Modules (all GUI-free and unit-testable; the GTK dashboard lives in ui/sync_page.py):
    states        session state machine
    commands      sync.command wire format, validation, replay protection
    launch_sync   AppLaunchSyncManager: who opens whom, without loops or duplicates
    adb_bridge    thin, injectable wrapper around `adb`
    display_power DisplayPowerManager: turn the phone's panel off while mirroring, always restore it
    settings      persisted user toggles (Smart Screen Mode, auto-sync, auto screen-off)
    controller    ScreenSyncController: ties the above to the existing mirror/transport code
"""
