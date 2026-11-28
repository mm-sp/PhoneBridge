package dev.phonebridge

import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.drawable.Drawable
import android.media.MediaScannerConnection
import android.os.Environment
import android.provider.MediaStore
import android.util.Size
import android.webkit.MimeTypeMap
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.util.UUID
import kotlin.concurrent.withLock

/**
 * File transfer v2 (PROTOCOL.md section 1c): every file travels on its own TLS connection so it
 * never blocks the control link; sha256-verified, and resumable from a partial file.
 */
object Transfer {
    private const val CHUNK = 256 * 1024
    private const val AUTH = "PBFILE:"

    /** Transfers block for a long time; keep them off the small general worker pool. */
    val pool: java.util.concurrent.ExecutorService = java.util.concurrent.Executors.newCachedThreadPool()

    /** Files the user picks on the phone: two at a time, the rest wait their turn. */
    val uploads: java.util.concurrent.ExecutorService = java.util.concurrent.Executors.newFixedThreadPool(2)

    /** file.accept / file.reject answers for offers this phone made, by transfer id. */
    val pending = java.util.concurrent.ConcurrentHashMap<String, java.util.concurrent.CompletableFuture<JSONObject>>()

    /** Transfers the user/PC cancelled, and the data connections to close when that happens. */
    private val cancelled: MutableSet<String> = java.util.concurrent.ConcurrentHashMap.newKeySet()
    private val active = java.util.concurrent.ConcurrentHashMap<String, Connection>()

    /** Stops a running transfer in either direction; the partial download is deleted by the receiving loop. */
    fun cancel(id: String, notifyPc: BridgeService? = null) {
        if (id.isEmpty()) return
        cancelled.add(id)
        pending[id]?.complete(JSONObject().put("rejected", true).put("reason", "cancelled"))
        active[id]?.close()
        notifyPc?.send("file.cancel", JSONObject().put("transfer_id", id))
    }

    /** Which partial file each incoming transfer writes. */
    val parts = PartFiles()

    /** Closes a transfer's data connection without cancelling it (its partial file belongs to a newer attempt now). */
    fun drop(id: String) { active[id]?.close() }

    fun isCancelled(id: String) = id in cancelled
    fun register(id: String, dc: Connection) { active[id] = dc }
    fun unregister(id: String) { active.remove(id); cancelled.remove(id) }

    /** Partial downloads live in app-private storage (not the shared Download folder other apps can read). */
    fun partDir(ctx: Context): java.io.File = java.io.File(ctx.filesDir, "transfers").apply { mkdirs() }

    /** Deletes abandoned partials older than a week. */
    fun purgeStale(ctx: Context) {
        val cutoff = System.currentTimeMillis() - 7L * 24 * 3600 * 1000
        partDir(ctx).listFiles()?.filter { it.lastModified() < cutoff }?.forEach { it.delete() }
    }

    fun hex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }

    fun resumeKey(name: String, size: Long, mtime: Long): String =
        hex(java.security.MessageDigest.getInstance("SHA-1").digest("$name|$size|$mtime".toByteArray())).take(24)

    /** Sends one file to the PC; throws IOException if the PC rejects it or the transfer fails. */
    fun send(svc: BridgeService, input: InputStream, filename: String, size: Long, mime: String,
             extra: JSONObject? = null, mtime: Long = 0) {
        val c = svc.connection()
        val pc = Store.load(svc)
        if (c == null || pc == null) { input.close(); throw java.io.IOException("not connected to the PC") }
        val id = UUID.randomUUID().toString().replace("-", "")
        val offer = JSONObject().put("transfer_id", id).put("filename", filename).put("size", size).put("mime", mime)
            .put("resume_key", resumeKey(filename, size, mtime)).put("data_conn", true)
        extra?.keys()?.forEach { offer.put(it, extra.get(it)) }
        val answer = java.util.concurrent.CompletableFuture<JSONObject>()
        pending[id] = answer
        try {
            c.send("file.offer", offer)
            val ans = try {
                answer.get(120, java.util.concurrent.TimeUnit.SECONDS)
            } catch (e: java.util.concurrent.TimeoutException) {
                throw java.io.IOException("the PC didn't answer")
            }
            if (ans.optBoolean("rejected")) throw java.io.IOException(ans.optString("reason", "the PC rejected the file"))
            val offset = ans.optLong("offset", 0).coerceIn(0, size)
            svc.holdWake()
            try {
                val dc = Connection.open(pc, ident = "${pc.fingerprint}:file:$id:$offset\n", dataLabel = "file|$id")
                try {
                    active[id] = dc
                    dc.sendRaw((AUTH + id).toByteArray())
                    val md = java.security.MessageDigest.getInstance("SHA-256")
                    val buf = ByteArray(CHUNK)
                    var skip = offset
                    while (skip > 0) {  // the hash covers the whole file, including the part the PC already has
                        val n = input.read(buf, 0, minOf(CHUNK.toLong(), skip).toInt())
                        if (n < 0) throw java.io.IOException("file shrank while sending")
                        md.update(buf, 0, n); skip -= n
                    }
                    var remaining = size - offset
                    while (remaining > 0) {
                        if (id in cancelled) throw java.io.IOException("cancelled")
                        val n = input.read(buf, 0, minOf(CHUNK.toLong(), remaining).toInt())
                        if (n < 0) throw java.io.IOException("file shrank while sending")
                        md.update(buf, 0, n)
                        dc.sendRaw(buf.copyOf(n))
                        remaining -= n
                    }
                    dc.sendRaw(("SHA256:" + hex(md.digest())).toByteArray())
                    if (String(dc.recvRaw()) != "OK") throw java.io.IOException("the PC says the file arrived damaged")
                } finally { dc.close() }
            } finally { svc.releaseWake() }
        } finally {
            pending.remove(id)
            active.remove(id)
            cancelled.remove(id)
            runCatching { input.close() }
        }
    }

    /** Answer to an offer this phone made (dispatched from the connection reader). */
    fun onAnswer(p: JSONObject, rejected: Boolean) {
        val f = pending[p.optString("transfer_id")] ?: return
        if (rejected) p.put("rejected", true)
        f.complete(p)
    }
}

object Photos {
    fun list(ctx: Context, offset: Int, limit: Int, onProgress: (Int, Int) -> Unit = { _, _ -> }): JSONObject {
        val arr = JSONArray()
        var total = 0
        ctx.contentResolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            arrayOf(MediaStore.Images.Media._ID, MediaStore.Images.Media.DATE_TAKEN, MediaStore.Images.Media.DATE_ADDED,
                MediaStore.Images.Media.DISPLAY_NAME, MediaStore.Images.Media.SIZE),
            null, null, "${MediaStore.Images.Media.DATE_ADDED} DESC",
        )?.use { c ->
            total = c.count
            val pageSize = minOf(limit, (total - offset).coerceAtLeast(0))
            onProgress(0, pageSize)
            if (offset < total && c.moveToPosition(offset)) {
                do {
                    onProgress(arr.length() + 1, pageSize)
                    val id = c.getLong(0)
                    val uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id)
                    val thumb = if (android.os.Build.VERSION.SDK_INT < 29) "" else runCatching {
                        ctx.contentResolver.loadThumbnail(uri, Size(180, 180), null)
                            .toBase64(Bitmap.CompressFormat.JPEG, 70)
                    }.getOrDefault("")
                    val taken = c.getLong(1).takeIf { it > 0 } ?: (c.getLong(2) * 1000)
                    arr.put(JSONObject().put("id", id.toString()).put("taken_ts", taken)
                        .put("name", c.getString(3) ?: "").put("size", c.getLong(4)).put("thumb", thumb))
                } while (arr.length() < limit && c.moveToNext())
            }
        }
        return JSONObject().put("photos", arr).put("offset", offset).put("total", total)
    }

    fun sendPhoto(svc: BridgeService, id: String) {
        val uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id.toLong())
        var name = "photo_$id.jpg"
        var size = -1L
        var mime = "image/jpeg"
        svc.contentResolver.query(uri, arrayOf(MediaStore.Images.Media.DISPLAY_NAME, MediaStore.Images.Media.SIZE,
            MediaStore.Images.Media.MIME_TYPE), null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                name = c.getString(0) ?: name
                size = c.getLong(1)
                mime = c.getString(2) ?: mime
            }
        }
        fun unreadable(): Nothing = throw java.io.IOException("the phone can't read that photo (was it deleted?)")
        if (size <= 0) {
            size = svc.contentResolver.openFileDescriptor(uri, "r")?.use { it.statSize } ?: unreadable()
        }
        val input = svc.contentResolver.openInputStream(uri) ?: unreadable()
        Transfer.send(svc, input, name, size, mime, JSONObject().put("photo_id", id))
    }
}

object Files {
    private val root: File get() = Environment.getExternalStorageDirectory()
    val inbox: File get() = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "PhoneBridge")

    /** Resolves a PC-supplied path inside shared storage; refuses anything that escapes it. */
    private fun resolve(rel: String): File {
        val f = File(root, rel.trimStart('/')).canonicalFile
        val base = root.canonicalPath
        require(f.path == base || f.path.startsWith("$base/")) { "path outside phone storage" }
        return f
    }

    fun list(rel: String, onProgress: (Int, Int) -> Unit = { _, _ -> }): JSONObject {
        val dir = resolve(rel)
        val out = JSONObject().put("path", dir.path.removePrefix(root.canonicalPath).ifEmpty { "/" })
        val entries = dir.listFiles()
        if (entries == null) {
            return out.put("entries", JSONArray()).put("error", "Can't read this folder — allow Storage permission for PhoneBridge on the phone")
        }
        val arr = JSONArray()
        val visible = entries.filter { !it.name.startsWith(".") }
            .sortedWith(compareBy<File>({ !it.isDirectory }, { it.name.lowercase() }))
        onProgress(0, visible.size)
        visible.forEachIndexed { i, f ->
                onProgress(i + 1, visible.size)
                arr.put(JSONObject().put("name", f.name).put("is_dir", f.isDirectory)
                    .put("size", if (f.isDirectory) (f.list()?.size ?: 0).toLong() else f.length())
                    .put("mtime", f.lastModified()))
            }
        runCatching {
            val st = android.os.StatFs(root.path)
            out.put("free", st.availableBytes).put("total", st.totalBytes)
        }
        return out.put("entries", arr)
    }

    fun sendFile(svc: BridgeService, rel: String) {
        val f = resolve(rel)
        if (!f.isFile) throw java.io.IOException("${f.name} is not a file, or the phone can't read it")
        val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(f.extension.lowercase()) ?: "application/octet-stream"
        Transfer.send(svc, f.inputStream(), f.name, f.length(), mime, JSONObject().put("source_path", rel), f.lastModified())
    }

    /** PC -> phone: answers a file.offer by opening a data connection (PROTOCOL.md section 1c).
     *  Runs on a Transfer.pool thread, never on the connection's reader. */
    fun receive(svc: BridgeService, offer: JSONObject) {
        val pc = Store.load(svc) ?: return
        val tid = offer.optString("transfer_id")
        val size = offer.optLong("size", -1)
        val name = File(offer.optString("filename", "file")).name.ifEmpty { "file" }
        fun reject(reason: String) {
            svc.send("file.reject", JSONObject().put("transfer_id", tid).put("reason", reason))
        }
        if (!offer.optBoolean("data_conn") || tid.isEmpty() || size < 0) {
            reject("unsupported transfer; update PhoneBridge on the phone"); return
        }
        val destDir = offer.optString("dest_path", "").takeIf { it.isNotEmpty() }
            ?.let { runCatching { resolve(it) }.getOrNull() }?.takeIf { it.isDirectory } ?: inbox
        destDir.mkdirs()
        // The partial lives in app-private storage and is then copied into the shared folder, so both need room.
        // Compared by subtraction: a hostile huge `size` must not overflow the addition and slip past the check.
        val reserve = 64L * 1024 * 1024
        if (size > destDir.usableSpace - reserve || size > Transfer.partDir(svc).usableSpace - reserve) {
            reject("not enough free space on the phone"); return
        }
        val parts = Transfer.parts
        val claimed = parts.claim(Transfer.partDir(svc), offer.optString("resume_key"), tid,
            svc.connection()?.session?.sessionId ?: "")
        claimed.displaced?.let { Transfer.drop(it.transferId) }
        val claim = claimed.claim
        val part = claim.file
        var offset = if (part.exists()) part.length() else 0L
        if (offset >= size) { part.delete(); offset = 0L }

        var dest: File? = null
        svc.holdWake()
        try {
            val dc = Connection.open(pc, ident = "${pc.fingerprint}:file:$tid:$offset\n", dataLabel = "file|$tid")
            Transfer.register(tid, dc)
            try {
                dc.sendRaw(("PBFILE:$tid").toByteArray())
                val md = java.security.MessageDigest.getInstance("SHA-256")
                if (offset > 0) part.inputStream().use { ins ->
                    val buf = ByteArray(1 shl 20)
                    while (true) { val n = ins.read(buf); if (n < 0) break; md.update(buf, 0, n) }
                }
                var received = offset
                java.io.FileOutputStream(part, true).use { out ->
                    while (received < size) {
                        val chunk = dc.recvRaw()
                        if (Transfer.isCancelled(tid)) throw java.io.IOException("cancelled")
                        if (received + chunk.size > size) throw java.io.IOException("PC sent more than it offered")
                        md.update(chunk)
                        parts.whileHeld(claim) { out.write(chunk) }
                        received += chunk.size
                    }
                }
                val ok = String(dc.recvRaw()) == "SHA256:" + Transfer.hex(md.digest())
                dc.sendRaw((if (ok) "OK" else "BAD").toByteArray())
                if (!ok) { parts.whileHeld(claim) { part.delete() }; throw java.io.IOException("checksum mismatch") }
            } finally { dc.close() }
            // The partial is app-private, so moving it into the shared folder is a copy; only then is it removed.
            val verified = parts.seal(claim)
            dest = try {
                PartFiles.saveCopy(verified, destDir, name)
            } catch (e: Exception) {
                throw java.io.IOException("couldn't save $name — allow Storage permission for PhoneBridge")
            } finally { verified.delete() }
        } catch (e: Exception) {
            if (!parts.holds(claim)) return   // a newer attempt took this one over and reports its own outcome
            val cancelled = Transfer.isCancelled(tid)
            // Cancelled on purpose, or a partial no later attempt would look for: nothing to resume. Otherwise the
            // partial file stays so the next attempt resumes where this one stopped.
            if (cancelled || !claim.resumable) part.delete()
            if (!cancelled) {
                svc.send("error", JSONObject().put("message", "Transfer of $name failed: ${e.message}").put("transfer_id", tid))
            }
            return
        } finally { parts.release(claim); svc.releaseWake(); Transfer.unregister(tid) }
        val saved = dest ?: return
        MediaScannerConnection.scanFile(svc, arrayOf(saved.path), null, null)
        svc.main.post {
            BridgeService.pcListener?.invoke("pc.file_saved", JSONObject().put("filename", saved.name).put("path", saved.path))
        }
        svc.send("file.saved", JSONObject().put("transfer_id", tid)
            .put("filename", saved.name).put("path", saved.path.removePrefix(root.canonicalPath)))
    }
}

object Apps {
    fun list(ctx: Context, onProgress: (Int, Int) -> Unit = { _, _ -> }): JSONObject {
        val pm = ctx.packageManager
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val arr = JSONArray()
        val apps = pm.queryIntentActivities(launcher, 0)
            .distinctBy { it.activityInfo.packageName }
            .filter { it.activityInfo.packageName != ctx.packageName }
            .map { it to it.loadLabel(pm).toString() }
            .sortedBy { it.second.lowercase() }
        onProgress(0, apps.size)
        apps.forEachIndexed { i, (ri, label) ->
                onProgress(i + 1, apps.size)
                val pkg = ri.activityInfo.packageName
                val system = (ri.activityInfo.applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_SYSTEM) != 0
                arr.put(JSONObject().put("pkg", pkg).put("label", label).put("system", system)
                    .put("icon", runCatching { iconPng(ri.loadIcon(pm), 64) }.getOrDefault("")))
            }
        return JSONObject().put("apps", arr)
    }

    fun iconPng(d: Drawable, size: Int): String = d.toBitmap(size).toBase64()

    fun launch(svc: BridgeService, pkg: String) {
        val intent = svc.packageManager.getLaunchIntentForPackage(pkg)?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) ?: return
        if (android.provider.Settings.canDrawOverlays(svc)) {
            svc.startActivity(intent)
        } else {
            // Android blocks background services from opening apps unless "Display over
            // other apps" is allowed — fall back to a tap-to-open notification.
            svc.alert(2000 + (pkg.hashCode() and 0xfff), "Open app from PC", "Tap to open $pkg", intent)
            svc.send("error", JSONObject().put("message",
                "Allow \"Display over other apps\" for PhoneBridge on the phone so the PC can open apps directly"))
        }
    }
}
