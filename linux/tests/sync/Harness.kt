// Runs the real Android SyncCore.kt on a plain JVM: unit checks, then validates Python-made commands and emits Kotlin-made ones.
import dev.phonebridge.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

var pass = 0
var fail = 0
fun check(label: String, cond: Boolean) { if (cond) { pass++; println("  ok  $label") } else { fail++; println(" FAIL $label") } }

fun main(args: Array<String>) {
    val vectors = JSONArray(File(args[0]).readText())
    val now = args[1].toLong()
    val sid = "a".repeat(16)

    // --- state machine
    val sm = SyncStateMachine()
    check("starts DISCONNECTED", sm.state == SyncState.DISCONNECTED)
    check("illegal DISCONNECTED->MIRROR_ACTIVE refused", !sm.transition(SyncState.MIRROR_ACTIVE) && sm.state == SyncState.DISCONNECTED)
    for (s in listOf(SyncState.CONNECTING, SyncState.AUTHENTICATING, SyncState.APP_LAUNCHING, SyncState.MIRROR_STARTING, SyncState.MIRROR_ACTIVE, SyncState.DISPLAY_OFF)) sm.transition(s)
    check("happy path reaches DISPLAY_OFF", sm.state == SyncState.DISPLAY_OFF)
    check("DISPLAY_OFF->MIRROR_ACTIVE allowed", sm.transition(SyncState.MIRROR_ACTIVE))
    check("MIRROR_ACTIVE->APP_LAUNCHING refused", !sm.transition(SyncState.APP_LAUNCHING))
    sm.fail("boom")
    check("fail() reaches ERROR with reason", sm.state == SyncState.ERROR && sm.lastError == "boom")
    check("ERROR->STOPPING->DISCONNECTED", sm.transition(SyncState.STOPPING) && sm.transition(SyncState.DISCONNECTED))
    sm.addListener { _, _, _ -> throw RuntimeException("listener bug") }
    check("a crashing listener does not wedge the machine", sm.transition(SyncState.CONNECTING))
    check("every state has an entry in the table", SyncState.values().all { it in SyncStateMachine.TABLE })

    // --- validator against vectors made by the Python side
    var clock = now
    val v = SyncValidator(nowMs = { clock }, mono = { clock })
    var i = 0
    for (k in 0 until vectors.length()) {
        val vec = vectors.getJSONObject(k)
        val verdict = v.validate(vec.opt("payload") as? JSONObject, if (vec.isNull("session")) null else vec.getString("session"))
        val expect = vec.getString("expect")
        val got = if (verdict.ok) "ok" else verdict.reason!!.wire
        check("python vector '${vec.getString("name")}': expect $expect, got $got", got == expect)
        i++
    }

    // --- Kotlin-made commands, written out for Python to validate
    val out = JSONArray()
    fun emit(name: String, c: SyncCommand) { out.put(JSONObject().put("name", name).put("payload", c.toPayload())) }
    emit("open_viewer", SyncWire.make(Cmd.OPEN_PHONE_BRIDGE_VIEWER, "k".repeat(16), Origin.USER, JSONObject(), now))
    emit("start", SyncWire.make(Cmd.START_MIRRORING, "k".repeat(16), Origin.USER, JSONObject(), now))
    emit("stop", SyncWire.make(Cmd.STOP_MIRRORING, "k".repeat(16), Origin.USER, JSONObject().put("reason", "phone"), now))
    emit("display_on", SyncWire.make(Cmd.DISPLAY_ON, "k".repeat(16), Origin.USER, JSONObject(), now))
    emit("display_off", SyncWire.make(Cmd.DISPLAY_OFF, "k".repeat(16), Origin.USER, JSONObject().put("performed", false), now))
    emit("status", SyncWire.make(Cmd.CONNECTION_STATUS, "k".repeat(16), Origin.AUTO,
        JSONObject().put("state", "MIRROR_ACTIVE").put("display", "off").put("app_open", true).put("mirroring", true).put("probe_ts", now).put("smart", "automatic").put("dim_ok", true).put("width", 720), now))
    File(args[2]).writeText(out.toString())
    val ack = SyncWire.ack("c".repeat(16), true, "launched")
    check("ack has the agreed shape", ack.getString("command_id") == "c".repeat(16) && ack.getBoolean("ok") && ack.getString("result") == "launched")

    // --- launch manager (same rules as Python)
    var t = 1_000_000L
    val sent = ArrayList<SyncCommand>()
    var open = false; var link = true; var enabled = true
    val launched = ArrayList<Origin>()
    val m = AppLaunchSyncManager({ sent.add(it) }, { launched.add(it); true }, { open }, { enabled }, { link }, { t })
    check("user launch forwarded as OPEN_PHONE_BRIDGE_VIEWER (user)", m.onLocalAppOpened(Origin.USER, sid) == AppLaunchSyncManager.Decision.SENT && sent[0].cmd == Cmd.OPEN_PHONE_BRIDGE_VIEWER && sent[0].origin == Origin.USER)
    check("second inside cooldown skipped", m.onLocalAppOpened(Origin.USER, sid) == AppLaunchSyncManager.Decision.SKIP_COOLDOWN)
    t += 6000
    check("auto-origin never forwarded", m.onLocalAppOpened(Origin.AUTO, sid) == AppLaunchSyncManager.Decision.SKIP_AUTO)
    m.notePeerStatus(true)
    check("peer already open skipped", m.onLocalAppOpened(Origin.USER, sid) == AppLaunchSyncManager.Decision.SKIP_PEER_OPEN)
    t += 31_000
    check("stale peer status becomes unknown", m.peerOpen == null)
    enabled = false
    check("sync disabled skipped", m.onLocalAppOpened(Origin.USER, sid) == AppLaunchSyncManager.Decision.SKIP_DISABLED)
    enabled = true; link = false
    check("link down skipped", m.onLocalAppOpened(Origin.USER, sid) == AppLaunchSyncManager.Decision.SKIP_LINK_DOWN)
    link = true
    val cmdAuto = SyncWire.make(Cmd.OPEN_PHONE_BRIDGE, sid, Origin.AUTO, JSONObject(), t)
    val cmdUser = SyncWire.make(Cmd.OPEN_PHONE_BRIDGE, sid, Origin.USER, JSONObject(), t)
    check("remote auto launch ignored", m.onRemoteCommand(cmdAuto) == (true to "ignored_auto_origin") && launched.isEmpty())
    check("remote user launch executes once with AUTO origin", m.onRemoteCommand(cmdUser) == (true to "launched") && launched == listOf(Origin.AUTO))
    check("loop guard: local user-launch right after a remote-caused one is not echoed", m.onLocalAppOpened(Origin.USER, sid) == AppLaunchSyncManager.Decision.SKIP_ECHO)
    t += 20_000
    open = true
    check("already open -> acknowledged, not relaunched", m.onRemoteCommand(cmdUser) == (true to "already_open") && launched.size == 1)
    open = false
    var ex = 0
    val m2 = AppLaunchSyncManager({}, { ex++; true }, { false }, { true }, { true }, { t })
    val res = (1..10).map { m2.onRemoteCommand(cmdUser) }
    check("remote launch flood is rate limited", ex == 3 && res.last() == (false to "rate_limited"))
    check("failed launch reported", AppLaunchSyncManager({}, { false }, { false }, { true }, { true }, { t }).onRemoteCommand(cmdUser) == (false to "launch_failed"))
    check("viewer command is not a phone launch", m.onRemoteCommand(SyncWire.make(Cmd.START_MIRRORING, sid, Origin.USER, JSONObject(), t)).second == "not_a_launch_command")

    println("KOTLIN $pass passed, $fail failed")
    System.exit(if (fail == 0) 0 else 1)
}
