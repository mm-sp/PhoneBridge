package dev.phonebridge

import android.content.Context
import android.net.Uri
import android.provider.ContactsContract
import android.provider.Telephony
import android.telephony.SmsManager
import org.json.JSONArray
import org.json.JSONObject

/**
 * SMS read/send through the system SMS provider. PhoneBridge is *not* the default SMS
 * app (the stock Messages app keeps working); since Android 4.4 the system itself stores
 * texts that a non-default app sends, so they show up in both places.
 */
object Sms {
    /**
     * One page of conversations, newest first. Reads Android's conversation index
     * (mms-sms/conversations) so only the requested page is touched — scanning every SMS
     * to group them is what made big inboxes slow.
     */
    fun threads(ctx: Context, offset: Int, limit: Int, onProgress: (Int, Int) -> Unit = { _, _ -> }): JSONObject {
        val names = ContactNames(ctx)
        val arr = JSONArray()
        var total = 0
        val uri = Uri.parse("content://mms-sms/conversations?simple=true")
        val cursor = runCatching {
            ctx.contentResolver.query(uri, arrayOf("_id", "date", "message_count", "recipient_ids", "snippet", "read"),
                null, null, "date DESC")
        }.getOrNull()
        if (cursor == null) return threadsByScan(ctx, offset, limit, onProgress)
        cursor.use { c ->
            total = c.count
            val page = minOf(limit, (total - offset).coerceAtLeast(0))
            onProgress(0, page)
            if (offset < total && c.moveToPosition(offset)) {
                do {
                    val tid = c.getString(0) ?: continue
                    val address = c.getString(3)?.split(" ")?.firstOrNull()?.let { recipientAddress(ctx, it) } ?: ""
                    arr.put(JSONObject()
                        .put("id", tid).put("address", address).put("name", names.lookup(address) ?: "")
                        .put("last_text", c.getString(4) ?: "").put("ts", c.getLong(1))
                        .put("unread", if (c.getInt(5) == 0) unreadCount(ctx, tid) else 0)
                        .put("count", c.getInt(2)))
                    onProgress(arr.length(), page)
                } while (arr.length() < limit && c.moveToNext())
            }
        }
        return JSONObject().put("threads", arr).put("offset", offset).put("total", total)
            .put("has_more", offset + arr.length() < total)
    }

    private fun recipientAddress(ctx: Context, recipientId: String): String? = runCatching {
        ctx.contentResolver.query(Uri.parse("content://mms-sms/canonical-address/$recipientId"),
            null, null, null, null)?.use { c -> if (c.moveToFirst()) c.getString(c.getColumnIndexOrThrow("address")) else null }
    }.getOrNull()

    private fun unreadCount(ctx: Context, threadId: String): Int = runCatching {
        ctx.contentResolver.query(Telephony.Sms.CONTENT_URI, arrayOf(Telephony.Sms._ID),
            "${Telephony.Sms.THREAD_ID} = ? AND ${Telephony.Sms.READ} = 0", arrayOf(threadId), null)?.use { it.count }
    }.getOrNull() ?: 0

    /** Fallback for phones without the conversations index: group SMS rows ourselves. */
    private fun threadsByScan(ctx: Context, offset: Int, limit: Int, onProgress: (Int, Int) -> Unit): JSONObject {
        data class T(val id: String, val address: String, val lastText: String, val ts: Long, var unread: Int, var count: Int)
        val byThread = LinkedHashMap<String, T>()
        ctx.contentResolver.query(
            Telephony.Sms.CONTENT_URI,
            arrayOf(Telephony.Sms.THREAD_ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE, Telephony.Sms.READ),
            null, null, "${Telephony.Sms.DATE} DESC",
        )?.use { c ->
            val total = c.count
            while (c.moveToNext()) {
                onProgress(c.position + 1, total)
                val tid = c.getString(0) ?: continue
                val t = byThread.getOrPut(tid) { T(tid, c.getString(1) ?: "", c.getString(2) ?: "", c.getLong(3), 0, 0) }
                t.count++
                if (c.getInt(4) == 0) t.unread++
            }
        }
        val names = ContactNames(ctx)
        val all = byThread.values.toList()
        val arr = JSONArray()
        for (t in all.drop(offset).take(limit)) {
            arr.put(JSONObject()
                .put("id", t.id).put("address", t.address).put("name", names.lookup(t.address) ?: "")
                .put("last_text", t.lastText).put("ts", t.ts).put("unread", t.unread).put("count", t.count))
        }
        return JSONObject().put("threads", arr).put("offset", offset).put("total", all.size)
            .put("has_more", offset + arr.length() < all.size)
    }

    /** One page of a conversation: `offset` messages back from the newest, `limit` of them, oldest first. */
    fun thread(ctx: Context, threadId: String, offset: Int, limit: Int, onProgress: (Int, Int) -> Unit = { _, _ -> }): JSONObject {
        val msgs = ArrayList<JSONObject>()
        var address = ""
        var total = 0
        ctx.contentResolver.query(
            Telephony.Sms.CONTENT_URI,
            arrayOf(Telephony.Sms._ID, Telephony.Sms.TYPE, Telephony.Sms.BODY, Telephony.Sms.DATE, Telephony.Sms.ADDRESS),
            "${Telephony.Sms.THREAD_ID} = ?", arrayOf(threadId), "${Telephony.Sms.DATE} DESC",
        )?.use { c ->
            total = c.count
            val page = minOf(limit, (total - offset).coerceAtLeast(0))
            onProgress(0, page)
            if (offset < total && c.moveToPosition(offset)) {
                do {
                    val type = c.getInt(1)
                    if (address.isEmpty()) address = c.getString(4) ?: ""
                    msgs.add(JSONObject()
                        .put("id", c.getString(0))
                        .put("from_me", type != Telephony.Sms.MESSAGE_TYPE_INBOX)
                        .put("status", when (type) {
                            Telephony.Sms.MESSAGE_TYPE_FAILED -> "failed"
                            Telephony.Sms.MESSAGE_TYPE_OUTBOX, Telephony.Sms.MESSAGE_TYPE_QUEUED -> "sending"
                            else -> "ok"
                        })
                        .put("text", c.getString(2) ?: "")
                        .put("ts", c.getLong(3)))
                    onProgress(msgs.size, page)
                } while (msgs.size < limit && c.moveToNext())
            }
        }
        // MMS (pictures, group texts) live in a separate table: merge the ones that fall into
        // this page's time window. `sms_count` lets the PC page by SMS rows only.
        val smsCount = msgs.size
        val lo = if (msgs.isNotEmpty() && offset + smsCount < total) msgs.last().getLong("ts") else 0L
        val hi = if (offset == 0) Long.MAX_VALUE else (msgs.firstOrNull()?.getLong("ts")?.plus(1) ?: 0L)
        if (offset == 0 || msgs.isNotEmpty()) {
            runCatching { msgs.addAll(mms(ctx, threadId, lo, hi)) }
            msgs.sortByDescending { it.getLong("ts") }
        }
        msgs.reverse()
        return JSONObject().put("thread_id", threadId).put("address", address).put("sms_count", smsCount)
            .put("name", ContactNames(ctx).lookup(address) ?: "")
            .put("offset", offset).put("total", total).put("has_more", offset + smsCount < total)
            .put("messages", JSONArray(msgs))
    }

    /** MMS rows of a conversation with ts (ms) in [loMs, hiMs): text parts + image thumbnails. */
    private fun mms(ctx: Context, threadId: String, loMs: Long, hiMs: Long): List<JSONObject> {
        val out = ArrayList<JSONObject>()
        val maxSec = if (hiMs == Long.MAX_VALUE) Long.MAX_VALUE / 1000 else hiMs / 1000 + 1
        ctx.contentResolver.query(
            Telephony.Mms.CONTENT_URI, arrayOf(Telephony.Mms._ID, Telephony.Mms.DATE, Telephony.Mms.MESSAGE_BOX),
            "${Telephony.Mms.THREAD_ID} = ? AND ${Telephony.Mms.DATE} >= ? AND ${Telephony.Mms.DATE} <= ?",
            arrayOf(threadId, (loMs / 1000).toString(), maxSec.toString()), "${Telephony.Mms.DATE} DESC",
        )?.use { c ->
            while (c.moveToNext() && out.size < 60) {
                val id = c.getString(0)
                val ts = c.getLong(1) * 1000
                if (ts < loMs || ts >= hiMs) continue
                val text = StringBuilder()
                val atts = JSONArray()
                ctx.contentResolver.query(Uri.parse("content://mms/part"), arrayOf("_id", "ct", "text", "name"),
                    "mid = ?", arrayOf(id), "seq ASC")?.use { p ->
                    while (p.moveToNext()) {
                        val ct = p.getString(1) ?: continue
                        when {
                            ct == "text/plain" -> p.getString(2)?.let { text.append(it) }
                            ct.startsWith("image/") && atts.length() < 6 -> atts.put(JSONObject().put("mime", ct)
                                .put("name", p.getString(3) ?: "").put("thumb", mmsThumb(ctx, p.getString(0))))
                            ct.startsWith("video/") || ct.startsWith("audio/") -> atts.put(JSONObject().put("mime", ct)
                                .put("name", p.getString(3) ?: ""))
                        }
                    }
                }
                out.add(JSONObject().put("id", "mms-$id").put("mms", true)
                    .put("from_me", c.getInt(2) != Telephony.Mms.MESSAGE_BOX_INBOX)
                    .put("status", "ok").put("text", text.toString()).put("ts", ts).put("attachments", atts))
            }
        }
        return out
    }

    private fun mmsThumb(ctx: Context, partId: String): String = runCatching {
        val uri = Uri.parse("content://mms/part/$partId")
        val bounds = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
        ctx.contentResolver.openInputStream(uri)?.use { android.graphics.BitmapFactory.decodeStream(it, null, bounds) }
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / sample > 960) sample *= 2
        val opts = android.graphics.BitmapFactory.Options().apply { inSampleSize = sample }
        val bmp = ctx.contentResolver.openInputStream(uri)?.use { android.graphics.BitmapFactory.decodeStream(it, null, opts) }
        val o = java.io.ByteArrayOutputStream()
        bmp?.compress(android.graphics.Bitmap.CompressFormat.JPEG, 72, o)
        android.util.Base64.encodeToString(o.toByteArray(), android.util.Base64.NO_WRAP)
    }.getOrDefault("")

    /** Address of the other party of an MMS (for the PC's thread header). */
    fun mmsAddress(ctx: Context, mmsId: String, incoming: Boolean): String = runCatching {
        ctx.contentResolver.query(Uri.parse("content://mms/$mmsId/addr"), arrayOf("address", "type"), null, null, null)?.use { c ->
            var found = ""
            while (c.moveToNext()) {
                val type = c.getInt(1)
                val a = c.getString(0) ?: continue
                if (a == "insert-address-token") continue
                if ((incoming && type == 137) || (!incoming && type == 151)) { found = a; break }
                if (found.isEmpty()) found = a
            }
            found
        } ?: ""
    }.getOrDefault("")

    fun send(svc: BridgeService, number: String, text: String) {
        if (number.isBlank() || text.isBlank()) return
        @Suppress("DEPRECATION")
        val sms = SmsManager.getDefault()
        val parts = sms.divideMessage(text)
        if (parts.size > 1) sms.sendMultipartTextMessage(number, null, parts, null, null)
        else sms.sendTextMessage(number, null, text, null, null)
        // Give the system a moment to write it to the provider, then refresh the PC's view.
        Thread.sleep(1500)
        val tid = Telephony.Threads.getOrCreateThreadId(svc, number).toString()
        svc.send("sms.sent", JSONObject().put("thread_id", tid).put("address", number))
        svc.send("sms.thread_messages", thread(svc, tid, 0, 20))
    }

    fun incoming(ctx: Context, address: String, body: String, ts: Long): JSONObject {
        val tid = runCatching { Telephony.Threads.getOrCreateThreadId(ctx, address).toString() }.getOrDefault("")
        return JSONObject()
            .put("thread_id", tid).put("address", address).put("name", ContactNames(ctx).lookup(address) ?: "")
            .put("message", JSONObject().put("id", "new-$ts").put("from_me", false).put("text", body).put("ts", ts))
    }
}

/** Number -> contact display name, cached per instance. */
class ContactNames(private val ctx: Context) {
    private val cache = HashMap<String, String?>()

    fun lookup(number: String): String? {
        if (number.isBlank()) return null
        return cache.getOrPut(number) {
            runCatching {
                val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number))
                ctx.contentResolver.query(uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null)
                    ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
            }.getOrNull()
        }
    }
}
