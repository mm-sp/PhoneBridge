package dev.phonebridge

import android.app.Activity
import android.view.View
import android.widget.LinearLayout
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Security dashboard + permission manager (what the PC may ask this phone to do). */
class SecurityPanel(act: Activity) : PcPanel(act) {
    override val title = "Security"
    private val list = column()
    override val view: View = scroll(list)

    override fun onShow() = render()

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

    private fun renderPrivacy() {
        list.addView(section("Privacy: notifications"))
        toggle("Share notifications with the PC", "Master switch. Off = nothing is sent.", Privacy.notificationsEnabled(act)) {
            Privacy.setNotificationsEnabled(act, it); NotificationRelay.instance?.let { n -> BridgeService.instance?.worker?.execute { n.sendAll() } }
        }
        toggle("Hide all notification text", "The PC sees the app name only.", Privacy.hideNotificationText(act)) {
            Privacy.setHideNotificationText(act, it); NotificationRelay.instance?.let { n -> BridgeService.instance?.worker?.execute { n.sendAll() } }
        }
        toggle("Hide sensitive notifications", "One-time codes, password-manager and authenticator apps.", Privacy.hideSensitive(act)) {
            Privacy.setHideSensitive(act, it); NotificationRelay.instance?.let { n -> BridgeService.instance?.worker?.execute { n.sendAll() } }
        }
        val apps = NotificationRelay.instance?.let { n -> runCatching { n.activeNotifications }.getOrNull() }
            ?.map { it.packageName }?.filter { it != act.packageName }?.distinct().orEmpty() + Privacy.blockedApps(act)
        val shown = apps.distinct().take(12)
        if (shown.isNotEmpty()) {
            list.addView(text("Apps (tap to block or unblock)", 12f, color = Ui.MUTED).apply { setPadding(dp(4), dp(6), 0, dp(4)) })
            for (pkg in shown) {
                val blocked = pkg in Privacy.blockedApps(act)
                val label = runCatching { act.packageManager.getApplicationLabel(act.packageManager.getApplicationInfo(pkg, 0)).toString() }.getOrDefault(pkg)
                toggle(label, if (blocked) "Blocked" else "Shared", !blocked) {
                    Privacy.setAppBlocked(act, pkg, !it); NotificationRelay.instance?.let { n -> BridgeService.instance?.worker?.execute { n.sendAll() } }
                }
            }
        }

        list.addView(section("Privacy: clipboard"))
        toggle("Clipboard sync", "Items password managers mark as sensitive are never shared.", Privacy.clipboardEnabled(act)) { Privacy.setClipboardEnabled(act, it) }
        val c = card()
        c.addView(text("Clear text received from the PC after", 14f, bold = true))
        val r = row()
        for ((label, secs) in listOf("30 s" to 30, "2 min" to 120, "10 min" to 600, "Never" to 0)) {
            val b = Ui.button(act, label, if (Privacy.clipboardExpiry(act) == secs) Ui.Kind.PRIMARY else Ui.Kind.SECONDARY) {
                Privacy.setClipboardExpiry(act, secs); render()
            }
            b.layoutParams = LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = dp(6); topMargin = dp(6) }
            r.addView(b)
        }
        c.addView(r)
        list.addView(c)
    }

    private fun pretty(fp: String) = fp.chunked(4).joinToString(" ")

    private fun render() {
        list.removeAllViews()
        val pc = Store.load(act)
        val svc = BridgeService.instance
        val session = svc?.connection()?.session

        list.addView(section("Security status"))
        val status = card()
        if (pc == null) {
            status.addView(text("Not paired", 16f, bold = true))
        } else {
            val connected = svc?.isConnected() == true
            status.addView(text(if (connected) "Connected to ${pc.name}" else "Not connected to ${pc.name}", 16f, bold = true,
                color = if (connected) Ui.GREEN else Ui.WARN))
            status.addView(text("TLS 1.3 (pinned certificate) + AES-256-GCM, per-direction keys, forward secrecy", 12f, color = Ui.MUTED))
            if (session != null) {
                status.addView(text("Session ${session.sessionId}…  ·  key generation ${session.generation}", 12f, color = Ui.MUTED))
            }
            if (pc.pcIdentityPub.isEmpty()) {
                status.addView(text("This pairing predates device identity keys — pair again.", 12f, color = Ui.WARN))
            } else {
                status.addView(text("PC identity  ${pretty(SecureSession.fingerprint(pc.pcIdentityPub))}", 12f, color = Ui.TEXT2))
            }
            pc.identity?.let { status.addView(text("This phone  ${pretty(SecureSession.fingerprint(it.pubB64))}", 12f, color = Ui.TEXT2)) }
        }
        val verdict = PhoneAudit.verify(act)
        status.addView(
            if (verdict.ok) text("Audit log verified: ${verdict.records} signed, hash-chained records", 12f, color = Ui.GREEN)
            else text("AUDIT LOG FAILED VERIFICATION: ${verdict.error}", 12f, bold = true, color = Ui.RED)
        )
        status.addView(text("Failed authentications (24 h): ${PhoneAudit.failedAuthLastDay(act)}", 12f, color = Ui.MUTED))
        list.addView(status)

        list.addView(section("Emergency"))
        val emergency = card()
        emergency.addView(text("Ends the session now and keeps this phone off the PC until you tap Connect on the Home tab.", 12f, color = Ui.MUTED))
        val r = row()
        r.addView(Ui.button(act, "DISCONNECT NOW", Ui.Kind.DANGER) {
            BridgeService.instance?.emergencyDisconnect(); toast("Disconnected"); render()
        }.apply { layoutParams = LinearLayout.LayoutParams(-2, -2).apply { topMargin = dp(6) } })
        r.addView(button("Rotate identity key") {
            toast(if (BridgeService.instance?.rotateIdentity() == true) "New identity key sent to the PC" else "Not connected")
        })
        emergency.addView(r)
        list.addView(emergency)

        list.addView(section("What the PC may do on this phone"))
        list.addView(text("Pairing grants nothing by itself. “Ask” shows a notification; your answer lasts until the PC disconnects.",
            12f, color = Ui.MUTED).apply { setPadding(dp(4), 0, 0, dp(8)) })
        for ((feature, label) in PcAccess.FEATURES) {
            val c = card()
            c.addView(text(label, 15f, bold = true))
            val modes = row()
            val current = PcAccess.mode(act, feature)
            for (m in PcAccess.Mode.values()) {
                val b = Ui.button(act, m.name.lowercase().replaceFirstChar { it.uppercase() },
                    if (m == current) Ui.Kind.PRIMARY else Ui.Kind.SECONDARY) {
                    PcAccess.setMode(act, feature, m); render()
                }
                b.layoutParams = LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = dp(6); topMargin = dp(6) }
                modes.addView(b)
            }
            c.addView(modes)
            list.addView(c)
        }

        renderPrivacy()

        list.addView(section("Recent security events"))
        val fmt = SimpleDateFormat("MMM d HH:mm:ss", Locale.getDefault())
        val events = PhoneAudit.recent(act, 15)
        if (events.isEmpty()) list.addView(text("Nothing yet", 12f, color = Ui.MUTED))
        for (e in events) {
            val c = card()
            val type = e.optString("type")
            val bad = type.startsWith("auth.failed") || type == "access.denied"
            c.addView(text(type, 14f, bold = true, color = if (bad) Ui.WARN else Ui.TEXT))
            val meta = e.optJSONObject("meta")?.let { m -> m.keys().asSequence().joinToString("  ") { "$it=${m.opt(it)}" } } ?: ""
            c.addView(text("${fmt.format(Date(e.optLong("ts")))}  $meta", 11f, color = Ui.MUTED))
            list.addView(c)
        }
    }
}
