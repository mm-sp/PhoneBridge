package dev.phonebridge

import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONObject

/** Phone-side screen-sync settings (SharedPreferences). Smart Screen Mode: "automatic" | "enabled" | "disabled". */
class SyncPrefs(ctx: Context) {
    private val p = ctx.getSharedPreferences("screensync", Context.MODE_PRIVATE)
    var autoAppSync: Boolean get() = p.getBoolean("auto_app_sync", true); set(v) = p.edit().putBoolean("auto_app_sync", v).apply()
    var autoScreenOff: Boolean get() = p.getBoolean("auto_screen_off", true); set(v) = p.edit().putBoolean("auto_screen_off", v).apply()
    var smartMode: String
        get() = p.getString("smart_mode", "automatic")?.takeIf { it in MODES } ?: "automatic"
        set(v) = p.edit().putString("smart_mode", if (v in MODES) v else "automatic").apply()
    companion object { val MODES = listOf("automatic", "enabled", "disabled") }
}

/**
 * Phone side of the screen-sync session. The PC orchestrates (it decides when frames are confirmed and runs the ADB
 * display-off); the phone validates every command, performs what only it can (open the app, start the capture, restore
 * the display) and drives its own dashboard. Mirrors linux/phonebridge/screensync/controller.py.
 *
 * Safety: the display is restored whenever the link drops, the capture stops, the service is destroyed or this process
 * restarts after a kill ([DisplayPower.recoverIfStale]).
 */
class ScreenSync(private val svc: BridgeService) {
    val prefs = SyncPrefs(svc)
    val display = DisplayPower(svc)
    val sm = SyncStateMachine { Log.i(TAG, it) }
    private val validator = SyncValidator()
    private val main = Handler(Looper.getMainLooper())

    @Volatile var sessionId = ""
        private set
    @Volatile var latencyMs: Long? = null
        private set
    @Volatile var peerFps = 0.0
        private set
    @Volatile var width = 0
        private set
    @Volatile var height = 0
        private set
    @Volatile var note = ""
        private set
    @Volatile var listener: (() -> Unit)? = null
    private val probes = HashMap<Long, Long>()
    @Volatile private var pendingUserLaunch = false
    @Volatile private var lastStatusAt = 0L

    val launch = AppLaunchSyncManager(
        send = { sendCmd(it) },
        launchLocal = ::openApp,
        isLocalOpen = { MainActivity.resumed },
        syncEnabled = { prefs.autoAppSync },
        linkUp = { svc.isConnected() },
    )

    init { sm.addListener { _, _, _ -> changed() } }

    // ---------------------------------------------------------------- lifecycle
    fun onServiceCreated() { display.recoverIfStale() }

    fun onServiceDestroyed() { display.restore("PhoneBridge service stopped") }

    fun onLinkUp() {
        // The user opened the app before the link was up: forward that now (once).
        if (pendingUserLaunch || pendingOpen) { pendingUserLaunch = false; pendingOpen = false; onUserOpenedApp(Origin.USER) }
        sendStatus(probe = true)
        changed()
    }

    /** Connection lost (Wi-Fi change, PC closed, process death on the PC...): the PC may be unable to turn the screen back on. */
    fun onLinkDown() {
        display.restore("connection to the PC was lost")
        if (sm.state in SyncStateMachine.MIRRORING) sm.transition(SyncState.RECONNECTING, "connection lost")
        latencyMs = null
        probes.clear()
        changed()
    }

    fun onVideoStarted(w: Int, h: Int) {
        width = w; height = h; note = ""
        if (sm.state == SyncState.DISCONNECTED || sm.state == SyncState.RECONNECTING || sm.state == SyncState.ERROR) {
            if (sm.state == SyncState.ERROR) sm.transition(SyncState.DISCONNECTED, "new capture")
            if (sessionId.isEmpty()) sessionId = SyncWire.newId()
            sm.transition(SyncState.MIRROR_STARTING, "capture started")
        }
        changed()
    }

    fun onVideoStopped() {
        display.restore("mirroring stopped")
        when (sm.state) {
            SyncState.DISCONNECTED -> {}
            SyncState.STOPPING -> sm.transition(SyncState.DISCONNECTED, "stopped")
            else -> { if (sm.transition(SyncState.STOPPING, "capture stopped")) sm.transition(SyncState.DISCONNECTED, "stopped") else sm.fail("capture stopped unexpectedly") }
        }
        width = 0; height = 0; peerFps = 0.0
        changed()
    }

    fun onMirrorDenied(message: String) {
        if (sm.state in setOf(SyncState.MIRROR_STARTING, SyncState.APP_LAUNCHING)) sm.fail(message)
        note = message
        changed()
    }

    // ---------------------------------------------------------------- explicit user actions (phone -> PC)
    /** The user opened PhoneBridge on this phone (origin USER) or it was opened by a sync command / system (AUTO). */
    fun onUserOpenedApp(origin: Origin) {
        if (origin != Origin.USER) { Log.i(TAG, "app opened by a sync command/system (auto): not forwarded"); launch.onLocalAppOpened(origin, sessionId.ifEmpty { SyncWire.newId() }); return }
        if (!prefs.autoAppSync) return
        if (!svc.isConnected()) { pendingUserLaunch = true; return }   // forwarded as soon as the link is up
        if (sessionId.isEmpty() || sm.state == SyncState.DISCONNECTED) sessionId = SyncWire.newId()
        val d = launch.onLocalAppOpened(Origin.USER, sessionId)
        Log.i(TAG, "user opened the app: launch decision $d")
        // The PC viewer is already open (nothing to launch), but nothing is being mirrored: ask it to start now.
        if (d == AppLaunchSyncManager.Decision.SKIP_PEER_OPEN && sm.state !in SyncStateMachine.MIRRORING) requestStart()
    }

    fun requestStart() {
        if (!svc.isConnected()) { note = "Not connected to the PC"; changed(); return }
        if (sm.state in SyncStateMachine.MIRRORING) return
        sessionId = SyncWire.newId()
        sendCmd(SyncWire.make(Cmd.START_MIRRORING, sessionId, Origin.USER))
        note = "Asked the PC to start mirroring"
        changed()
    }

    fun requestStop() {
        if (sessionId.isNotEmpty()) sendCmd(SyncWire.make(Cmd.STOP_MIRRORING, sessionId, Origin.USER, JSONObject().put("reason", "phone")))
        svc.main.post { svc.video.stopMirror() }
        display.restore("stopped on the phone")
    }

    fun requestDisplay(on: Boolean) {
        if (on) {
            display.restore("user turned the screen on")
            if (sessionId.isNotEmpty()) sendCmd(SyncWire.make(Cmd.DISPLAY_ON, sessionId, Origin.USER))
            if (sm.state == SyncState.DISPLAY_OFF) sm.transition(SyncState.MIRROR_ACTIVE, "screen on")
        } else if (sessionId.isNotEmpty() && sm.state in setOf(SyncState.MIRROR_ACTIVE, SyncState.DISPLAY_OFF)) {
            sendCmd(SyncWire.make(Cmd.DISPLAY_OFF, sessionId, Origin.USER, JSONObject().put("performed", false)))
        }
        changed()
    }

    // ---------------------------------------------------------------- inbound (connection thread)
    fun onEnvelope(type: String, payload: JSONObject) {
        if (type == SyncWire.TYPE_ACK) { Log.i(TAG, "ack ${payload.optString("command_id")} ${payload.optString("result")}"); return }
        val v = validator.validate(payload, sessionId.ifEmpty { null })
        val cid = payload.optString("command_id")
        if (!v.ok) {
            Log.w(TAG, "rejected sync.command: ${v.reason?.wire} (${v.detail})")
            when (v.reason) {
                Reject.DUPLICATE -> ack(cid, true, "duplicate")   // idempotent: already handled
                else -> if (SyncWire.ID_RE.matches(cid)) ack(cid, false, "", v.reason?.wire ?: "rejected")
            }
            return
        }
        val c = v.command!!
        val (ok, result) = try { execute(c) } catch (e: Exception) { Log.w(TAG, "sync command failed", e); false to "internal_error" }
        ack(c.commandId, ok, result)
    }

    private fun execute(c: SyncCommand): Pair<Boolean, String> {
        when (c.cmd) {
            Cmd.OPEN_PHONE_BRIDGE -> return launch.onRemoteCommand(c)
            Cmd.START_MIRRORING -> {
                if (sessionId.isEmpty() || sm.state == SyncState.DISCONNECTED || sm.state == SyncState.ERROR) sessionId = c.sessionId
                if (!prefs.autoAppSync && c.origin != Origin.USER) return true to "sync_disabled"
                if (sm.state in SyncStateMachine.MIRRORING) return true to "already_active"
                // The "screen" permission applies to this route too. With "Ask", the mirror.start the PC sends alongside
                // raises the prompt and starts the capture once allowed.
                when (PcAccess.mode(svc, "screen")) {
                    PcAccess.Mode.DENY -> return false to "permission_denied"
                    PcAccess.Mode.ASK -> return true to "awaiting_permission"
                    PcAccess.Mode.ALLOW -> {}
                }
                main.post { svc.video.requestMirror() }   // idempotent while a consent prompt is pending (VideoManager)
                return true to "starting"
            }
            Cmd.STOP_MIRRORING -> {
                main.post { svc.video.stopMirror() }
                display.restore("PC stopped mirroring")
                return true to "stopping"
            }
            Cmd.MIRROR_READY -> {
                if (sm.state == SyncState.MIRROR_STARTING) sm.transition(SyncState.MIRROR_ACTIVE, "PC confirmed frames")
                peerFps = c.args.optDouble("fps", peerFps)
                changed()
                return true to "ok"
            }
            Cmd.DISPLAY_OFF -> {
                val performed = c.args.optBoolean("performed", false)
                if (sm.state != SyncState.MIRROR_ACTIVE && sm.state != SyncState.DISPLAY_OFF) return false to "not_mirroring"
                if (prefs.smartMode == "disabled" && c.origin != Origin.USER) {
                    // Phone owner said "keep my display on": undo what the PC just did, or refuse.
                    if (performed) sendCmd(SyncWire.make(Cmd.DISPLAY_ON, sessionId, Origin.AUTO))
                    return true to "refused_by_phone_setting"
                }
                val done = if (performed) { display.markOffByPc(); true } else display.dim()
                if (!done) return false to "dim_unavailable"
                sm.transition(SyncState.DISPLAY_OFF, if (performed) "panel off (adb on PC)" else "dimmed")
                changed()
                return true to if (performed) "off" else "dimmed"
            }
            Cmd.DISPLAY_ON -> {
                display.restore("PC asked for the display")
                if (sm.state == SyncState.DISPLAY_OFF) sm.transition(SyncState.MIRROR_ACTIVE, "display on")
                changed()
                return true to "on"
            }
            Cmd.CONNECTION_STATUS -> {
                val a = c.args
                if (a.has("echo_ts")) probes.remove(a.getLong("echo_ts"))?.let { latencyMs = System.nanoTime() / 1_000_000 - it }
                if (a.has("fps")) peerFps = a.optDouble("fps")
                if (a.has("width") && a.has("height")) { width = a.optInt("width"); height = a.optInt("height") }
                if (a.has("app_open")) launch.notePeerStatus(a.optBoolean("app_open"))
                if (a.has("probe_ts")) sendStatus(echo = a.getLong("probe_ts"))
                changed()
                if (a.optBoolean("probe_content", false)) return true to (if (display.contentProbe()) "probing" else "probe_unavailable")
                return true to "ok"
            }
            Cmd.OPEN_PHONE_BRIDGE_VIEWER -> return false to "not_allowed_here"
        }
    }

    // ---------------------------------------------------------------- helpers
    /** Opens MainActivity as an AUTO launch (it must not be forwarded back). Needs "Display over other apps" to start from the background. */
    private fun openApp(origin: Origin): Boolean {
        val i = Intent(svc, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
            .putExtra(MainActivity.EXTRA_ORIGIN, origin.wire)
        if (!android.provider.Settings.canDrawOverlays(svc)) {
            // Android 10+ blocks background activity starts without this permission; be honest and ask for a tap.
            svc.alert(4, "PhoneBridge", "Tap to open PhoneBridge (your PC asked)", i)
            note = "Android blocked opening the app automatically — grant 'Display over other apps' or tap the notification"
            changed()
            return false
        }
        return try { svc.startActivity(i); true } catch (e: Exception) { Log.w(TAG, "openApp failed", e); false }
    }

    fun sendStatus(probe: Boolean = false, echo: Long? = null) {
        if (!svc.isConnected()) return
        val a = JSONObject().put("state", sm.state.name).put("display", displayName())
            .put("app_open", MainActivity.resumed).put("mirroring", sm.state in SyncStateMachine.MIRRORING)
            .put("smart", prefs.smartMode).put("dim_ok", display.canDim())
        if (width > 0) { a.put("width", width); a.put("height", height) }
        if (probe) { val t = System.currentTimeMillis(); a.put("probe_ts", t); probes[t] = System.nanoTime() / 1_000_000; if (probes.size > 32) probes.remove(probes.keys.first()) }
        if (echo != null) a.put("echo_ts", echo)
        sendCmd(SyncWire.make(Cmd.CONNECTION_STATUS, sessionId.ifEmpty { SyncWire.newId() }, Origin.AUTO, a))
    }

    /** Called about every 5 s while connected (BridgeService scheduler). */
    fun tick() { if (svc.isConnected()) { sendStatus(probe = true); lastStatusAt = System.currentTimeMillis() } }

    fun displayName() = when (display.mode) {
        DisplayPower.Mode.OFF_BY_PC -> "off"; DisplayPower.Mode.DIMMED -> "dimmed"
        DisplayPower.Mode.ON -> if (display.panelInteractive()) "on" else "off"
    }

    private fun sendCmd(c: SyncCommand) = svc.send(SyncWire.TYPE_COMMAND, c.toPayload())
    private fun ack(id: String, ok: Boolean, result: String, reason: String = "") = svc.send(SyncWire.TYPE_ACK, SyncWire.ack(id, ok, result, reason))
    private fun changed() { main.post { listener?.invoke() } }

    companion object {
        const val TAG = "PhoneBridge.sync"
        /** The user opened the app before the service existed; forwarded to the PC when the link comes up. */
        @Volatile var pendingOpen = false
    }
}
