package dev.phonebridge

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.telephony.SmsManager
import android.util.Base64
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File

/**
 * PC -> phone MMS (protocol/PROTOCOL.md section 4e). The phone builds an M-Send.req PDU
 * ([MmsPdu]) and hands it to the carrier through SmsManager.sendMultimediaMessage().
 *
 * Limitation: only the default SMS app may write to the SMS/MMS database, so a message sent
 * this way is delivered but does not appear in the phone's own Messages app.
 */
object Mms {
    /** Conservative size every carrier accepts (many allow only 300 KB). */
    const val MAX_TOTAL_BYTES = 300_000
    private const val TIMEOUT_MS = 3 * 60_000L
    // Packages that open the PDU through the content URI (AOSP's MmsService lives in the phone process).
    private val TELEPHONY_PACKAGES = listOf("com.android.mms.service", "com.android.phone", "com.android.messaging")

    fun send(svc: BridgeService, requestId: String, numbers: List<String>, text: String, attachments: JSONArray) {
        fun status(state: String, message: String = "") =
            svc.send("sms.mms_status", JSONObject().put("request_id", requestId).put("state", state).put("message", message))
        try {
            if (numbers.none { it.isNotBlank() }) return status("failed", "No recipient")
            val parts = buildParts(text, attachments)
            val total = parts.sumOf { it.data.size }
            if (total > MAX_TOTAL_BYTES) return status("failed", "Attachments are too large for MMS (${total / 1000} KB; the limit is ${MAX_TOTAL_BYTES / 1000} KB)")
            val pdu = MmsPdu.build("PB" + requestId.take(12), numbers.filter { it.isNotBlank() }, parts)

            val dir = File(svc.cacheDir, "mms").apply { mkdirs() }
            dir.listFiles()?.forEach { if (System.currentTimeMillis() - it.lastModified() > 3600_000) it.delete() }
            // requestId comes from the PC: keep only letters/digits so it can't name a path outside this folder.
            val safeId = requestId.filter { it.isLetterOrDigit() }.take(32).ifEmpty { "request" }
            val file = File(dir, "$safeId.pdu").apply { writeBytes(pdu) }
            val uri = FileProvider.getUriForFile(svc, "${svc.packageName}.clipshare", file)
            for (pkg in TELEPHONY_PACKAGES) {
                runCatching { svc.grantUriPermission(pkg, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            }

            val action = "dev.phonebridge.MMS_SENT.$requestId"
            val finished = java.util.concurrent.atomic.AtomicBoolean(false)
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(ctx: Context, intent: Intent) {
                    if (!finish(svc, finished, this, uri, file)) return
                    if (resultCode == Activity.RESULT_OK) status("sent")
                    else status("failed", "The carrier didn't accept the message (code $resultCode). Is mobile data on?")
                }
            }
            ContextCompat.registerReceiver(svc, receiver, IntentFilter(action), ContextCompat.RECEIVER_NOT_EXPORTED)
            svc.main.postDelayed({
                if (finish(svc, finished, receiver, uri, file)) status("failed", "Timed out waiting for the carrier")
            }, TIMEOUT_MS)

            val sent = PendingIntent.getBroadcast(svc, 0, Intent(action).setPackage(svc.packageName),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            status("sending")
            @Suppress("DEPRECATION")
            SmsManager.getDefault().sendMultimediaMessage(svc, uri, null, null, sent)
        } catch (e: SecurityException) {
            status("failed", "Allow the SMS permission for PhoneBridge on the phone")
        } catch (e: Exception) {
            status("failed", "Couldn't send: ${e.message ?: e.javaClass.simpleName}")
        }
    }

    /** Cleans up once per message (result and timeout race); true only for the caller that got there first. */
    private fun finish(svc: BridgeService, flag: java.util.concurrent.atomic.AtomicBoolean,
                       receiver: BroadcastReceiver, uri: Uri, file: File): Boolean {
        if (!flag.compareAndSet(false, true)) return false
        runCatching { svc.unregisterReceiver(receiver) }
        runCatching { svc.revokeUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
        file.delete()
        return true
    }

    private fun buildParts(text: String, attachments: JSONArray): List<MmsPart> {
        val raw = (0 until attachments.length()).map { attachments.getJSONObject(it) }
        val textBytes = text.toByteArray(Charsets.UTF_8)
        var budget = MAX_TOTAL_BYTES - textBytes.size - 400 * (raw.size + 1) // PDU headers
        val parts = ArrayList<MmsPart>()
        val images = raw.filter { it.optString("mime").startsWith("image/") }
        var imagesLeft = images.size
        for ((i, a) in raw.withIndex()) {
            val mime = a.optString("mime", "application/octet-stream")
            val name = a.optString("name", "attachment$i").replace(Regex("[^A-Za-z0-9._-]"), "_")
            val data = Base64.decode(a.optString("data"), Base64.DEFAULT)
            if (mime.startsWith("image/")) {
                val img = fitImage(data, mime, budget / maxOf(1, imagesLeft))
                imagesLeft--
                budget -= img.size
                parts.add(MmsPart(if (img === data) mime else "image/jpeg", if (img === data) name else name.substringBeforeLast('.') + ".jpg", img))
            } else {
                budget -= data.size
                parts.add(MmsPart(mime, name, data))
            }
        }
        if (textBytes.isNotEmpty()) parts.add(MmsPart("text/plain", "text.txt", textBytes))
        return parts
    }

    /** The original bytes if they already fit, else a downscaled JPEG that does. */
    private fun fitImage(data: ByteArray, mime: String, limit: Int): ByteArray {
        if (data.size <= limit && (mime == "image/jpeg" || mime == "image/png" || mime == "image/gif")) return data
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(data, 0, data.size, bounds)
        var maxDim = 1280
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / sample > maxDim * 2) sample *= 2
        var bmp = BitmapFactory.decodeByteArray(data, 0, data.size, BitmapFactory.Options().apply { inSampleSize = sample })
            ?: throw IllegalArgumentException("Couldn't read an attached picture")
        try {
            while (true) {
                val scale = minOf(1f, maxDim.toFloat() / maxOf(bmp.width, bmp.height))
                val scaled = if (scale < 1f) Bitmap.createScaledBitmap(bmp, (bmp.width * scale).toInt(), (bmp.height * scale).toInt(), true) else bmp
                for (q in intArrayOf(85, 70, 55, 40)) {
                    val out = ByteArrayOutputStream()
                    scaled.compress(Bitmap.CompressFormat.JPEG, q, out)
                    if (out.size() <= limit) return out.toByteArray()
                }
                if (scaled !== bmp) scaled.recycle()
                if (maxDim <= 200) throw IllegalArgumentException("A picture can't be made small enough for MMS")
                maxDim = maxDim * 3 / 4
            }
        } finally {
            bmp.recycle()
        }
    }
}
