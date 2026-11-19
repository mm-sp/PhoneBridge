package dev.phonebridge

import android.app.Activity
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.View

/** Screen mirroring dashboard (phone side): link, mirror and physical-display status, live stats, sync + Smart Screen Mode. */
class MirrorPanel(act: Activity) : PcPanel(act) {
    override val title = "Mirror"
    private val list = column()
    override val view: View = scroll(list)
    private val handler = Handler(Looper.getMainLooper())
    private val tick = object : Runnable { override fun run() { render(); handler.postDelayed(this, 1000) } }

    override fun onShow() {
        BridgeService.instance?.screenSync?.listener = { render() }
        render()
        handler.removeCallbacks(tick); handler.postDelayed(tick, 1000)
    }

    override fun onHide() {
        handler.removeCallbacks(tick)
        BridgeService.instance?.screenSync?.listener = null
    }

    private fun kv(parent: android.widget.LinearLayout, k: String, v: String) {
        val r = row()
        r.addView(weighted(text(k, 13f, color = Ui.MUTED)))
        r.addView(text(v, 13f, bold = true))
        parent.addView(r)
    }

    private fun toggle(label: String, hint: String, on: Boolean, set: (Boolean) -> Unit) {
        val c = card()
        val r = row()
        val col = column(0)
        col.addView(text(label, 14f, bold = true))
        if (hint.isNotEmpty()) col.addView(text(hint, 11f, color = Ui.MUTED))
        r.addView(weighted(col))
        r.addView(Ui.button(act, if (on) "On" else "Off", if (on) Ui.Kind.PRIMARY else Ui.Kind.SECONDARY) { set(!on); render() })
        c.addView(r)
        list.addView(c)
    }

    private fun render() {
        list.removeAllViews()
        val svc = BridgeService.instance
        val sync = svc?.screenSync
        if (svc == null || sync == null) {
            list.addView(text("PhoneBridge isn't running. Open the Home tab and connect to your PC.", 14f, color = Ui.MUTED))
            return
        }
        val pc = Store.load(act)
        list.addView(section("Screen mirroring"))
        val c = card()
        kv(c, "Connected device", pc?.name ?: "—")
        kv(c, "Connection", if (svc.isConnected()) "Connected (authenticated, encrypted)" else svc.stateDetail.ifEmpty { "Not connected" })
        kv(c, "Mirroring", when (sync.sm.state) {
            SyncState.DISCONNECTED -> "Idle"; SyncState.CONNECTING -> "Connecting…"; SyncState.AUTHENTICATING -> "Authenticating…"
            SyncState.APP_LAUNCHING -> "Opening app…"; SyncState.MIRROR_STARTING -> "Starting… (accept the prompt)"
            SyncState.MIRROR_ACTIVE -> "Mirroring"; SyncState.DISPLAY_OFF -> "Mirroring · display off"
            SyncState.RECONNECTING -> "Reconnecting…"; SyncState.STOPPING -> "Stopping…"; SyncState.ERROR -> "Error"
        })
        kv(c, "Physical display", when (sync.displayName()) { "off" -> "Off"; "dimmed" -> "Dimmed (not off)"; else -> "On" })
        val mirroring = sync.sm.state in SyncStateMachine.MIRRORING
        kv(c, "Frame rate", if (mirroring && sync.peerFps > 0) "%.0f fps".format(sync.peerFps) else "—")
        kv(c, "Resolution", if (mirroring && sync.width > 0) "${sync.width}×${sync.height}" else "—")
        kv(c, "Network latency", sync.latencyMs?.let { "$it ms" } ?: "—")
        val msg = sync.note.ifEmpty { if (sync.sm.state == SyncState.ERROR) sync.sm.lastError else "" }
        if (msg.isNotEmpty()) c.addView(text(msg, 12f, color = Ui.MUTED))
        list.addView(c)

        val btns = row()
        btns.addView(Ui.button(act, if (mirroring) "Stop mirroring" else "Start mirroring", if (mirroring) Ui.Kind.SECONDARY else Ui.Kind.PRIMARY) {
            if (mirroring) sync.requestStop() else sync.requestStart()
        }.apply { layoutParams = android.widget.LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = dp(6) } })
        val dispOn = sync.displayName() == "on"
        btns.addView(Ui.button(act, if (dispOn) "Turn screen off" else "Turn screen on") { sync.requestDisplay(!dispOn) }
            .apply { isEnabled = mirroring || !dispOn; layoutParams = android.widget.LinearLayout.LayoutParams(0, -2, 1f) })
        list.addView(btns)

        list.addView(section("Synchronisation"))
        toggle("Automatic app synchronisation", "Opening PhoneBridge here opens the viewer on your PC and starts mirroring", sync.prefs.autoAppSync) { sync.prefs.autoAppSync = it }
        toggle("Automatic screen-off", "Turn this display off once your PC confirms it is receiving the screen", sync.prefs.autoScreenOff) { sync.prefs.autoScreenOff = it }
        val sc = card()
        sc.addView(text("Smart Screen Mode", 14f, bold = true))
        sc.addView(text(when (sync.prefs.smartMode) {
            "enabled" -> "Turns the display off while mirroring. Without ADB on the PC it can only dim (not off)."
            "disabled" -> "Your display always stays on."
            else -> "Turns the display off only when a real screen-off is available and verified (needs ADB on the PC)."
        }, 11f, color = Ui.MUTED))
        val r = row()
        for (m in SyncPrefs.MODES) {
            r.addView(Ui.button(act, m.replaceFirstChar { it.uppercase() }, if (sync.prefs.smartMode == m) Ui.Kind.PRIMARY else Ui.Kind.SECONDARY) {
                sync.prefs.smartMode = m; render()
            }.apply { layoutParams = android.widget.LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = dp(6); topMargin = dp(6) } })
        }
        sc.addView(r)
        list.addView(sc)

        list.addView(section("Permissions this needs"))
        val pcard = card()
        val remote = InputService.instance != null
        kv(pcard, "Remote control from the PC", if (remote) "Allowed (Accessibility on)" else "Off — PC can only view")
        if (!remote) pcard.addView(Ui.button(act, "Turn on in Accessibility settings") { act.startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) })
        kv(pcard, "Display over other apps", if (Settings.canDrawOverlays(act)) "Allowed" else "Not allowed")
        if (!Settings.canDrawOverlays(act)) {
            pcard.addView(text("Needed so the PC can open this app and start the capture while the screen is locked or the app is closed.", 11f, color = Ui.MUTED))
            pcard.addView(Ui.button(act, "Allow") { act.startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, android.net.Uri.parse("package:${act.packageName}"))) })
        }
        list.addView(pcard)
    }
}
