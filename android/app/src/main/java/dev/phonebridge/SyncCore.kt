package dev.phonebridge

import org.json.JSONObject
import java.util.LinkedHashMap
import java.util.UUID

/*
 * Screen-sync core (no Android classes: compiled and unit-tested on a plain JVM, see linux/tests/test_android_sync.py).
 * Mirrors linux/phonebridge/screensync/{states,commands,launch_sync}.py — keep the two in step; the interop test
 * feeds each side's output to the other.
 */

enum class SyncState { DISCONNECTED, CONNECTING, AUTHENTICATING, APP_LAUNCHING, MIRROR_STARTING, MIRROR_ACTIVE, DISPLAY_OFF, RECONNECTING, STOPPING, ERROR }

/** Session state machine; an illegal transition is refused and logged, never silently applied. */
class SyncStateMachine(private val log: (String) -> Unit = {}) {
    @Volatile var state = SyncState.DISCONNECTED
        private set
    @Volatile var lastError = ""
        private set
    private val listeners = ArrayList<(SyncState, SyncState, String) -> Unit>()

    fun addListener(l: (SyncState, SyncState, String) -> Unit) { listeners += l }

    @Synchronized
    fun transition(to: SyncState, reason: String = ""): Boolean {
        val old = state
        if (to == old) return true
        if (to !in TABLE.getValue(old)) { log("refused illegal transition $old -> $to ($reason)"); return false }
        state = to
        if (to == SyncState.ERROR) lastError = reason
        log("session $old -> $to $reason")
        listeners.toList().forEach { runCatching { it(old, to, reason) } }
        return true
    }

    /** Any state may fail. */
    @Synchronized
    fun fail(reason: String) {
        val old = state
        state = SyncState.ERROR
        lastError = reason
        log("session $old -> ERROR ($reason)")
        listeners.toList().forEach { runCatching { it(old, SyncState.ERROR, reason) } }
    }

    companion object {
        private val S = SyncState.values()
        val TABLE: Map<SyncState, Set<SyncState>> = mapOf(
            SyncState.DISCONNECTED to setOf(SyncState.CONNECTING, SyncState.AUTHENTICATING, SyncState.APP_LAUNCHING, SyncState.MIRROR_STARTING, SyncState.ERROR),
            SyncState.CONNECTING to setOf(SyncState.AUTHENTICATING, SyncState.DISCONNECTED, SyncState.ERROR, SyncState.RECONNECTING),
            SyncState.AUTHENTICATING to setOf(SyncState.APP_LAUNCHING, SyncState.MIRROR_STARTING, SyncState.DISCONNECTED, SyncState.ERROR, SyncState.RECONNECTING),
            SyncState.APP_LAUNCHING to setOf(SyncState.MIRROR_STARTING, SyncState.STOPPING, SyncState.RECONNECTING, SyncState.DISCONNECTED, SyncState.ERROR),
            SyncState.MIRROR_STARTING to setOf(SyncState.MIRROR_ACTIVE, SyncState.STOPPING, SyncState.RECONNECTING, SyncState.DISCONNECTED, SyncState.ERROR),
            SyncState.MIRROR_ACTIVE to setOf(SyncState.DISPLAY_OFF, SyncState.STOPPING, SyncState.RECONNECTING, SyncState.DISCONNECTED, SyncState.ERROR),
            SyncState.DISPLAY_OFF to setOf(SyncState.MIRROR_ACTIVE, SyncState.STOPPING, SyncState.RECONNECTING, SyncState.DISCONNECTED, SyncState.ERROR),
            SyncState.RECONNECTING to setOf(SyncState.CONNECTING, SyncState.AUTHENTICATING, SyncState.APP_LAUNCHING, SyncState.MIRROR_STARTING, SyncState.STOPPING, SyncState.DISCONNECTED, SyncState.ERROR),
            SyncState.STOPPING to setOf(SyncState.DISCONNECTED, SyncState.ERROR),
            SyncState.ERROR to setOf(SyncState.DISCONNECTED, SyncState.CONNECTING, SyncState.RECONNECTING, SyncState.STOPPING),
        )
        val MIRRORING = setOf(SyncState.MIRROR_STARTING, SyncState.MIRROR_ACTIVE, SyncState.DISPLAY_OFF)
    }
}

enum class Cmd { OPEN_PHONE_BRIDGE, OPEN_PHONE_BRIDGE_VIEWER, START_MIRRORING, STOP_MIRRORING, DISPLAY_OFF, DISPLAY_ON, MIRROR_READY, CONNECTION_STATUS }
enum class Origin(val wire: String) { USER("user"), AUTO("auto") }
enum class Reject(val wire: String) {
    MALFORMED("malformed"), UNKNOWN_COMMAND("unknown_command"), NOT_ALLOWED("not_allowed_here"), BAD_ARGS("bad_args"),
    STALE("stale"), DUPLICATE("duplicate"), WRONG_SESSION("wrong_session")
}

data class SyncCommand(val cmd: Cmd, val sessionId: String, val commandId: String, val ts: Long, val origin: Origin, val args: JSONObject) {
    fun toPayload(): JSONObject = JSONObject().put("cmd", cmd.name).put("session_id", sessionId).put("command_id", commandId)
        .put("ts", ts).put("origin", origin.wire).put("args", args)
}

class Verdict(val command: SyncCommand?, val reason: Reject?, val detail: String = "") { val ok get() = command != null }

object SyncWire {
    const val TYPE_COMMAND = "sync.command"
    const val TYPE_ACK = "sync.ack"
    const val MAX_SKEW_MS = 5 * 60 * 1000L
    const val REPLAY_CACHE = 2048
    const val REPLAY_TTL_MS = 15 * 60 * 1000L
    val ID_RE = Regex("^[A-Za-z0-9_-]{8,64}$")

    /** What the phone may receive from the PC (the PC accepts its own, narrower set). */
    val PHONE_ACCEPTS = setOf(Cmd.OPEN_PHONE_BRIDGE, Cmd.START_MIRRORING, Cmd.STOP_MIRRORING, Cmd.DISPLAY_OFF, Cmd.DISPLAY_ON, Cmd.MIRROR_READY, Cmd.CONNECTION_STATUS)
    val SESSION_STARTERS = setOf(Cmd.OPEN_PHONE_BRIDGE, Cmd.OPEN_PHONE_BRIDGE_VIEWER, Cmd.START_MIRRORING)
    val LAUNCH = setOf(Cmd.OPEN_PHONE_BRIDGE, Cmd.OPEN_PHONE_BRIDGE_VIEWER)
    private val STATUS_TYPES = mapOf("state" to "s", "display" to "s", "link" to "s", "app_open" to "b", "mirroring" to "b", "probe_ts" to "i",
        "echo_ts" to "i", "fps" to "n", "width" to "i", "height" to "i", "rotation" to "i", "note" to "s", "smart" to "s", "dim_ok" to "b", "probe_content" to "b")

    fun newId(): String = UUID.randomUUID().toString().replace("-", "")

    fun make(cmd: Cmd, sessionId: String, origin: Origin = Origin.AUTO, args: JSONObject = JSONObject(), nowMs: Long = System.currentTimeMillis()) =
        SyncCommand(cmd, sessionId, newId(), nowMs, origin, args)

    fun ack(commandId: String, ok: Boolean, result: String = "", reason: String = ""): JSONObject =
        JSONObject().put("command_id", commandId).put("ok", ok).put("result", result.take(60)).put("reason", reason.take(120))

    // org.json yields Double on Android but BigDecimal/BigInteger in newer desktop builds: accept any Number (Boolean is not one)
    private fun isNumber(v: Any?) = v is Number
    private fun isInt(v: Any?) = v is Int || v is Long || v is java.math.BigInteger

    fun checkArgs(cmd: Cmd, args: JSONObject): String? {
        if (args.length() > 16) return "args must be a small object"
        val keys = args.keys().asSequence().toList()
        when (cmd) {
            Cmd.DISPLAY_OFF, Cmd.DISPLAY_ON -> {
                keys.firstOrNull { it !in setOf("mode", "performed", "reason") }?.let { return "unknown arg '$it'" }
                if (args.has("mode") && args.get("mode") !in listOf("adb", "dim", "")) return "mode must be adb|dim"
                if (args.has("performed") && args.get("performed") !is Boolean) return "performed must be boolean"
            }
            Cmd.CONNECTION_STATUS -> for (k in keys) {
                val t = STATUS_TYPES[k] ?: return "unknown status field '$k'"
                val v = args.get(k)
                val ok = when (t) { "s" -> v is String && v.length <= 200; "b" -> v is Boolean; "i" -> isInt(v); else -> isNumber(v) }
                if (!ok) return "bad type for '$k'"
            }
            else -> for (k in keys) {
                val v = args.get(k)
                when (k) {
                    "reason", "app" -> if (v !is String || v.length > 200) return "bad '$k'"
                    "fps", "width", "height" -> if (!isNumber(v) || (v as Number).toDouble() < 0 || v.toDouble() > 100000) return "bad '$k'"
                    else -> return "unknown arg '$k'"
                }
            }
        }
        return null
    }
}

/** One per connection. Never throws on hostile input. */
class SyncValidator(private val nowMs: () -> Long = System::currentTimeMillis, private val mono: () -> Long = System::currentTimeMillis) {
    private val seen = object : LinkedHashMap<String, Long>(256, 0.75f, false) {
        override fun removeEldestEntry(e: MutableMap.MutableEntry<String, Long>?) = size > SyncWire.REPLAY_CACHE
    }

    @Synchronized fun seen(id: String): Boolean = seen[id]?.let { mono() - it <= SyncWire.REPLAY_TTL_MS } ?: false

    @Synchronized
    fun validate(payload: JSONObject?, currentSession: String?): Verdict {
        try {
            if (payload == null) return Verdict(null, Reject.MALFORMED, "payload is not an object")
            val name = payload.opt("cmd") as? String ?: return Verdict(null, Reject.MALFORMED, "cmd missing")
            val cmd = runCatching { Cmd.valueOf(name) }.getOrNull() ?: return Verdict(null, Reject.UNKNOWN_COMMAND, name.take(40))
            val sid = payload.opt("session_id") as? String
            val cid = payload.opt("command_id") as? String
            if (sid == null || cid == null || !ID_OK(sid) || !ID_OK(cid)) return Verdict(null, Reject.MALFORMED, "bad session_id/command_id")
            val tsAny = payload.opt("ts")
            if (tsAny !is Int && tsAny !is Long) return Verdict(null, Reject.MALFORMED, "ts must be integer ms")
            val ts = (tsAny as Number).toLong()
            val originName = payload.opt("origin") ?: "auto"
            val origin = Origin.values().firstOrNull { it.wire == originName } ?: return Verdict(null, Reject.MALFORMED, "bad origin")
            if (cmd !in SyncWire.PHONE_ACCEPTS) return Verdict(null, Reject.NOT_ALLOWED, cmd.name)
            val args = if (payload.has("args")) (payload.opt("args") as? JSONObject ?: return Verdict(null, Reject.BAD_ARGS, "args must be an object")) else JSONObject()
            SyncWire.checkArgs(cmd, args)?.let { return Verdict(null, Reject.BAD_ARGS, it) }
            if (Math.abs(nowMs() - ts) > SyncWire.MAX_SKEW_MS) return Verdict(null, Reject.STALE, "timestamp outside freshness window")
            if (seen(cid)) return Verdict(null, Reject.DUPLICATE, cid)
            if (cmd !in SyncWire.SESSION_STARTERS && cmd != Cmd.CONNECTION_STATUS && sid != currentSession) return Verdict(null, Reject.WRONG_SESSION, "no such active session")
            seen[cid] = mono()
            return Verdict(SyncCommand(cmd, sid, cid, ts, origin, args), null)
        } catch (e: Exception) {
            return Verdict(null, Reject.MALFORMED, e.javaClass.simpleName)
        }
    }

    @Suppress("FunctionName")
    private fun ID_OK(s: String) = SyncWire.ID_RE.matches(s)
}

/**
 * Who opens whom, once, without loops. Same rules as the Python AppLaunchSyncManager:
 * only USER launches are forwarded; a launch caused by a remote command is AUTO and never echoed; already-open is
 * acknowledged, not re-launched; cooldown + breaker bound the rate both ways.
 */
class AppLaunchSyncManager(
    private val send: (SyncCommand) -> Unit,
    private val launchLocal: (Origin) -> Boolean,
    private val isLocalOpen: () -> Boolean,
    private val syncEnabled: () -> Boolean,
    private val linkUp: () -> Boolean,
    private val clock: () -> Long = System::currentTimeMillis,   // ms
) {
    enum class Decision { SENT, SKIP_AUTO, SKIP_ECHO, SKIP_DISABLED, SKIP_LINK_DOWN, SKIP_PEER_OPEN, SKIP_COOLDOWN, SKIP_BREAKER }

    private var peerOpenValue: Boolean? = null
    private var peerTs = Long.MIN_VALUE / 2
    private val sent = ArrayDeque<Long>()
    private val executed = ArrayDeque<Long>()
    private var echoUntil = 0L

    val peerOpen: Boolean? get() = if (clock() - peerTs <= PEER_TTL_MS) peerOpenValue else null
    fun notePeerStatus(open: Boolean?) { peerOpenValue = open; peerTs = clock() }

    @Synchronized
    fun onLocalAppOpened(origin: Origin, sessionId: String): Decision {
        val now = clock()
        if (origin != Origin.USER) return Decision.SKIP_AUTO
        if (now < echoUntil) return Decision.SKIP_ECHO
        if (!syncEnabled()) return Decision.SKIP_DISABLED
        if (!linkUp()) return Decision.SKIP_LINK_DOWN
        if (peerOpen == true) return Decision.SKIP_PEER_OPEN
        while (sent.isNotEmpty() && now - sent.first() > BREAKER_WINDOW_MS) sent.removeFirst()
        if (sent.isNotEmpty() && now - sent.last() < SEND_COOLDOWN_MS) return Decision.SKIP_COOLDOWN
        if (sent.size >= BREAKER_MAX) return Decision.SKIP_BREAKER
        sent.addLast(now)
        send(SyncWire.make(Cmd.OPEN_PHONE_BRIDGE_VIEWER, sessionId, Origin.USER, JSONObject(), now))
        return Decision.SENT
    }

    /** Handles OPEN_PHONE_BRIDGE from the PC. Returns (ok, result) for the ack. */
    @Synchronized
    fun onRemoteCommand(cmd: SyncCommand): Pair<Boolean, String> {
        if (cmd.cmd !in SyncWire.LAUNCH) return false to "not_a_launch_command"
        val now = clock()
        if (cmd.origin != Origin.USER) return true to "ignored_auto_origin"
        if (!syncEnabled()) return true to "sync_disabled"
        if (isLocalOpen()) { notePeerStatus(true); return true to "already_open" }
        while (executed.isNotEmpty() && now - executed.first() > RECV_WINDOW_MS) executed.removeFirst()
        if (executed.size >= RECV_MAX) return false to "rate_limited"
        executed.addLast(now)
        echoUntil = now + ECHO_SUPPRESS_MS
        val launched = runCatching { launchLocal(Origin.AUTO) }.getOrDefault(false)
        if (!launched) echoUntil = 0L
        return launched to (if (launched) "launched" else "launch_failed")
    }

    companion object {
        const val SEND_COOLDOWN_MS = 5_000L
        const val BREAKER_MAX = 4
        const val BREAKER_WINDOW_MS = 60_000L
        const val RECV_MAX = 3
        const val RECV_WINDOW_MS = 30_000L
        const val ECHO_SUPPRESS_MS = 15_000L
        const val PEER_TTL_MS = 30_000L
    }
}
