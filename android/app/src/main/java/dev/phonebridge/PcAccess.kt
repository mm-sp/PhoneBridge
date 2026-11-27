package dev.phonebridge

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import org.json.JSONObject
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * The phone's permission manager: what the paired PC may ask this phone to do. Pairing grants nothing by itself —
 * every PC request is mapped to a feature and checked here, per feature: Allow / Ask / Deny. "Ask" shows a
 * notification with Allow/Deny and the answer holds for that connection only. Android's own runtime permissions
 * still apply on top; this never bypasses them.
 */
object PcAccess {
    enum class Mode { ALLOW, ASK, DENY }

    val FEATURES = linkedMapOf(
        "files" to "Files", "photos" to "Photos and videos", "notifications" to "Notifications",
        "sms" to "Messages (read)", "sms_send" to "Sending messages", "calls" to "Calls and contacts",
        "call_dial" to "Placing calls", "clipboard" to "Clipboard sync",
        "screen" to "Screen mirroring and camera", "remote_control" to "Remote control of this phone", "apps" to "Apps",
    )

    /** Sensitive features ask by default; the rest keep working as before. */
    private val DEFAULTS = mapOf("sms" to Mode.ASK, "sms_send" to Mode.ASK, "calls" to Mode.ASK, "call_dial" to Mode.ASK,
        "remote_control" to Mode.ASK)

    /** Features where "Ask" means ask for EVERY request (with what is being requested), not once per connection:
     *  sending a message or placing a call costs money and can't be undone. */
    val ASK_EVERY_TIME = setOf("sms_send", "call_dial")

    /** Which feature a PC->phone message belongs to (null = housekeeping or a reply, needs no permission). */
    fun featureFor(type: String): String? = when {
        type == "file.offer" || type == "files.list" || type == "files.get" -> "files"
        type.startsWith("photos.") -> "photos"
        type == "notifications.sync" || type == "notification.action" -> "notifications"
        type == "sms.send" || type == "sms.send_mms" -> "sms_send"
        type.startsWith("sms.") -> "sms"
        type == "call.dial" -> "call_dial"
        type == "calls.history_request" || type == "contacts.list_request" || type == "call.action" -> "calls"
        type == "clipboard.update" -> "clipboard"
        // Stopping a stream only takes access away, so it is never blocked and never prompts.
        type == "mirror.stop" || type == "webcam.stop" -> null
        type == "mirror.start" || type.startsWith("webcam.") -> "screen"
        type == "mirror.input" || type == "hotspot.request" || type == "screen.state_request" ||
            type in setOf("wake", "unlock", "lock", "volume_up", "volume_down", "volume_mute", "copy") -> "remote_control"
        type.startsWith("apps.") -> "apps"
        else -> null
    }

    private fun prefs(ctx: Context) = ctx.applicationContext.getSharedPreferences("pc_access", Context.MODE_PRIVATE)

    fun mode(ctx: Context, feature: String): Mode =
        prefs(ctx).getString(feature, null)?.let { runCatching { Mode.valueOf(it) }.getOrNull() } ?: DEFAULTS[feature] ?: Mode.ALLOW

    fun setMode(ctx: Context, feature: String, mode: Mode) {
        prefs(ctx).edit().putString(feature, mode.name).apply()
        PhoneAudit.log(ctx, "permission.changed", "feature" to feature, "mode" to mode.name.lowercase())
    }

    /** What "Ask" remembers about each connection: an answer the user gave, yes or no, holds until it ends. Nothing is
     *  remembered for [ASK_EVERY_TIME] features, or when nobody answered. */
    class SessionAnswers {
        private val answers = ConcurrentHashMap<String, Boolean>()
        fun get(sessionId: String, feature: String): Boolean? = answers["$sessionId/$feature"]
        fun record(sessionId: String, feature: String, answer: Boolean?) {
            if (answer != null && feature !in ASK_EVERY_TIME) answers["$sessionId/$feature"] = answer
        }
    }

    /** Requests that only read something and that the PC sends in bursts (history and contacts together, say). While the
     *  prompt for their permission is open they wait for its answer. Nothing else does: input, calls, messages and other
     *  actions must not run later than the user expects, and are refused until there is an answer. */
    private val CAN_WAIT = setOf("sms.list_threads", "sms.get_thread", "calls.history_request", "contacts.list_request",
        "notifications.sync", "photos.list", "files.list", "apps.list_request")

    fun canWait(type: String) = type in CAN_WAIT

    /** The requests waiting for one open prompt, per key (connection and permission). */
    class Waiting<T>(private val limit: Int = 16) {
        private val held = HashMap<String, MutableList<T>>()
        /** A prompt for [key] is about to be shown. */
        @Synchronized fun open(key: String) { held.getOrPut(key) { mutableListOf() } }
        @Synchronized fun isOpen(key: String) = key in held
        /** False if no prompt for [key] is open, or too many requests are waiting for it already. */
        @Synchronized fun hold(key: String, item: T): Boolean {
            val list = held[key] ?: return false
            if (list.size >= limit) return false
            return list.add(item)
        }
        /** The prompt is over: what waited for it, oldest first. */
        @Synchronized fun close(key: String): List<T> = held.remove(key) ?: emptyList()
    }

    /** The `permission.denied` payload for a request: names it by the ids the PC chose (never its content), so the PC
     *  knows which of its requests failed. */
    fun denial(feature: String, type: String, p: JSONObject): JSONObject {
        val out = JSONObject().put("feature", feature).put("type", type)
        for (id in listOf("transfer_id", "request_id", "path")) if (p.has(id)) out.put(id, p.optString(id))
        return out
    }

    // ---- "Ask": a notification with Allow / Deny, answered through PcAccessReceiver
    private val pending = ConcurrentHashMap<String, CompletableFuture<Boolean>>()
    private const val ACTION = "dev.phonebridge.PC_ACCESS_ANSWER"

    private val requestSeq = java.util.concurrent.atomic.AtomicInteger()

    /** Describes WHAT is being requested for the prompt (a recipient or number, never a message body). */
    fun describe(type: String, p: JSONObject): String = when (type) {
        "sms.send" -> "to ${p.optString("number")}"
        "sms.send_mms" -> "to " + (p.optJSONArray("numbers")?.let { a -> (0 until a.length()).joinToString(", ") { a.optString(it) } } ?: p.optString("number"))
        "call.dial" -> "to ${p.optString("number")}"
        else -> ""
    }

    /** Blocks (never call on the connection's reader thread) until the user answers or 45 s pass. Returns the answer,
     *  or null if there was none; only `true` allows anything. */
    fun ask(ctx: Context, feature: String, detail: String = ""): Boolean? {
        // Per-request features get their own prompt every time; others share one prompt per feature.
        val key = if (feature in ASK_EVERY_TIME) "$feature#${requestSeq.incrementAndGet()}" else feature
        val fut = pending.computeIfAbsent(key) { CompletableFuture() }
        val app = ctx.applicationContext
        val nm = app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        fun action(allow: Boolean) = PendingIntent.getBroadcast(
            app, key.hashCode() * 2 + (if (allow) 1 else 0),
            Intent(app, PcAccessReceiver::class.java).setAction(ACTION).putExtra("feature", key).putExtra("allow", allow),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val label = FEATURES[feature] ?: feature
        val notification = Notification.Builder(app, BridgeService.CHANNEL_ALERTS)
            .setSmallIcon(android.R.drawable.ic_lock_lock)
            .setContentTitle(if (feature in ASK_EVERY_TIME) "Allow the PC to do this? ($label $detail)".trim() else "Allow the PC to use “$label”?")
            .setContentText(if (feature in ASK_EVERY_TIME) "Your paired PC is asking. You will be asked again next time."
                            else "Your paired PC is asking. This lasts until it disconnects.")
            .setAutoCancel(true)
            .addAction(Notification.Action.Builder(null, "Allow", action(true)).build())
            .addAction(Notification.Action.Builder(null, "Deny", action(false)).build())
            .build()
        val id = 7000 + (key.hashCode() and 0xfff)
        nm.notify(id, notification)
        PhoneAudit.log(app, "access.asked", "feature" to feature)
        return try {
            fut.get(45, TimeUnit.SECONDS)
        } catch (_: Exception) {
            null
        } finally {
            pending.remove(key, fut)
            nm.cancel(id)
        }
    }

    fun isPending(feature: String) = pending.containsKey(feature)

    fun answer(feature: String, allow: Boolean) { pending[feature]?.complete(allow) }
}

class PcAccessReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val feature = intent.getStringExtra("feature") ?: return
        PcAccess.answer(feature, intent.getBooleanExtra("allow", false))
    }
}
