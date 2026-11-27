package dev.phonebridge

import android.app.Notification
import android.graphics.Bitmap
import android.graphics.Canvas
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/**
 * Mirrors the phone's notifications to the PC. Needs the one-time "Notification access"
 * toggle (Settings > Apps > Special access), which MainActivity links to.
 */
class NotificationRelay : NotificationListenerService() {
    private val iconCache = HashMap<String, String>()
    /** Ids of the notifications this phone has put into a message for the PC. */
    private val sentIds: MutableSet<String> = java.util.concurrent.ConcurrentHashMap.newKeySet()

    override fun onListenerConnected() {
        instance = this
        BridgeService.instance?.worker?.execute { sendAll() }
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!relevant(sbn)) return
        val svc = BridgeService.instance ?: return
        val out = outgoing(sbn) ?: return   // blocked / switched off: nothing leaves the phone
        svc.worker.execute { svc.send("notification.posted", out) }
    }

    /** False when the PC may learn nothing about this notification: sharing off, denied, or its app blocked. */
    private fun shareable(sbn: StatusBarNotification): Boolean =
        Privacy.notificationsEnabled(this) && PcAccess.mode(this, "notifications") != PcAccess.Mode.DENY &&
            sbn.packageName !in Privacy.blockedApps(this)

    /** What the privacy settings allow to be shared for this notification (null = nothing), with text hidden if required. */
    private fun outgoing(sbn: StatusBarNotification): JSONObject? {
        if (!shareable(sbn)) return null
        val json = toJson(sbn)
        val sensitive = Privacy.isSensitive(sbn.packageName, json.optString("title") + " " + json.optString("text"))
        if (Privacy.hideNotificationText(this) || (sensitive && Privacy.hideSensitive(this))) {
            json.put("title", json.optString("app")).put("text", "Content hidden").put("hidden", true)
            json.put("actions", JSONArray())   // no reply box for a notification whose text the PC may not see
        }
        sentIds.add(sbn.key)
        return json
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        val svc = BridgeService.instance ?: return
        // The id alone names the app, so it only goes out if the PC may know it, or already does (so a notification shown
        // there before sharing was switched off still disappears).
        val known = sentIds.remove(sbn.key)
        if (!known && !shareable(sbn)) return
        svc.send("notification.removed", JSONObject().put("id", sbn.key))
    }

    /** Sends the full current set in one message so the PC can replace its list. */
    fun sendAll() {
        val svc = BridgeService.instance ?: return
        val arr = JSONArray()
        runCatching { activeNotifications }.getOrNull()?.filter(::relevant)?.sortedByDescending { it.postTime }
            ?.forEach { sbn -> outgoing(sbn)?.let { arr.put(it) } }
        svc.send("notifications.all", JSONObject().put("notifications", arr))
    }

    fun handleAction(p: JSONObject) {
        val key = p.optString("id")
        when (p.optString("action_id")) {
            "dismiss" -> runCatching { cancelNotification(key) }
            "dismiss_all" -> runCatching { cancelAllNotifications() }
            "open" -> runCatching {
                activeNotifications.firstOrNull { it.key == key }?.notification?.contentIntent?.send()
            }
            "reply" -> runCatching { reply(key, p.optString("reply_text")) }
            else -> {
                // A notification's own action button, addressed by index.
                val idx = p.optString("action_id").removePrefix("action:").toIntOrNull() ?: return
                runCatching {
                    activeNotifications.firstOrNull { it.key == key }?.notification?.actions?.getOrNull(idx)?.actionIntent?.send()
                }
            }
        }
    }

    private fun reply(key: String, text: String) {
        val sbn = activeNotifications.firstOrNull { it.key == key } ?: return
        val action = sbn.notification.actions?.firstOrNull { a -> a.remoteInputs?.isNotEmpty() == true } ?: return
        val intent = android.content.Intent()
        val bundle = android.os.Bundle()
        action.remoteInputs.forEach { bundle.putCharSequence(it.resultKey, text) }
        android.app.RemoteInput.addResultsToIntent(action.remoteInputs, intent, bundle)
        action.actionIntent.send(this, 0, intent)
    }

    private fun relevant(sbn: StatusBarNotification): Boolean {
        if (sbn.packageName == packageName) return false
        val n = sbn.notification
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return false
        val extras = n.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)
        return !(title.isNullOrBlank() && text.isNullOrBlank())
    }

    private fun toJson(sbn: StatusBarNotification): JSONObject {
        val n = sbn.notification
        val e = n.extras
        val text = e.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: e.getCharSequence(Notification.EXTRA_TEXT) ?: ""
        val actions = JSONArray()
        n.actions?.forEachIndexed { i, a ->
            actions.put(JSONObject().put("id", "action:$i").put("label", a.title?.toString() ?: "")
                .put("is_reply", a.remoteInputs?.isNotEmpty() == true))
        }
        return JSONObject()
            .put("id", sbn.key)
            .put("pkg", sbn.packageName)
            .put("app", appLabel(sbn.packageName))
            .put("title", e.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: "")
            .put("text", text.toString())
            .put("ts", sbn.postTime)
            .put("ongoing", sbn.isOngoing)
            .put("clearable", sbn.isClearable)
            .put("icon", appIcon(sbn.packageName))
            .put("actions", actions)
    }

    private fun appLabel(pkg: String): String = runCatching {
        packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
    }.getOrDefault(pkg)

    private fun appIcon(pkg: String): String = iconCache.getOrPut(pkg) {
        runCatching { Apps.iconPng(packageManager.getApplicationIcon(pkg), 48) }.getOrDefault("")
    }

    companion object {
        @Volatile var instance: NotificationRelay? = null
    }
}

fun Bitmap.toBase64(format: Bitmap.CompressFormat = Bitmap.CompressFormat.PNG, quality: Int = 90): String {
    val out = ByteArrayOutputStream()
    compress(format, quality, out)
    return Crypto.b64(out.toByteArray())
}

fun android.graphics.drawable.Drawable.toBitmap(size: Int): Bitmap {
    val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
    val c = Canvas(bmp)
    setBounds(0, 0, size, size)
    draw(c)
    return bmp
}
