package dev.phonebridge

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * Tamper-evident security log on the phone: every record contains the previous record's hash and is signed with this
 * phone's Ed25519 identity key. Holds security metadata only (event names, feature names, counts) — never message
 * content, numbers, file names or keys. Plain file + [identity] so it can be unit-tested on a JVM.
 */
class AuditChain(private val file: File, private val identity: () -> SecureSession.Identity) {
    data class Verdict(val ok: Boolean, val records: Int, val error: String? = null)

    private fun sha256Hex(s: String) = MessageDigest.getInstance("SHA-256").digest(s.toByteArray(Charsets.UTF_8)).toHex()

    @Synchronized fun log(type: String, meta: JSONObject = JSONObject(), now: Long = System.currentTimeMillis()): JSONObject {
        val id = identity()
        val prev = records().lastOrNull()?.optString("hash") ?: GENESIS
        // The signed text is stored verbatim as `body`, so verification never depends on JSON re-serialisation.
        val body = JSONObject().put("ts", now).put("type", type).put("meta", meta).put("prev", prev).put("pub", id.pubB64).toString()
        val hash = sha256Hex(body)
        val rec = JSONObject().put("body", body).put("hash", hash).put("sig", Crypto.b64(id.sign(hash.toByteArray())))
        file.parentFile?.mkdirs()
        file.appendText(rec.toString() + "\n")
        trim()
        return rec
    }

    fun records(): List<JSONObject> {
        if (!file.exists()) return emptyList()
        return file.readLines().mapNotNull { line -> runCatching { JSONObject(line) }.getOrNull() }
    }

    /** Parsed body of a record: ts, type, meta, prev, pub. */
    fun body(rec: JSONObject): JSONObject = JSONObject(rec.getString("body"))

    fun verify(): Verdict {
        var prev: String? = null
        var n = 0
        for (line in if (file.exists()) file.readLines() else emptyList()) {
            if (line.isBlank()) continue
            try {
                val rec = JSONObject(line)
                val bodyText = rec.getString("body")
                val hash = rec.getString("hash")
                if (sha256Hex(bodyText) != hash) return Verdict(false, n, "record $n was modified")
                val body = JSONObject(bodyText)
                // The first record kept after trimming may point at a record that was trimmed away.
                if (prev != null && body.getString("prev") != prev) return Verdict(false, n, "record $n does not follow the previous one (deleted or reordered)")
                if (!SecureSession.verify(body.getString("pub"), Crypto.unb64(rec.getString("sig")), hash.toByteArray())) {
                    return Verdict(false, n, "record $n has an invalid signature")
                }
                prev = hash
                n++
            } catch (e: Exception) {
                return Verdict(false, n, "record $n is malformed")
            }
        }
        return Verdict(true, n)
    }

    private fun trim() {
        if (file.length() <= MAX_BYTES) return
        val keep = file.readLines().takeLast(KEEP_RECORDS)
        file.writeText(keep.joinToString("\n") + "\n")
    }

    companion object {
        const val GENESIS = "0000000000000000000000000000000000000000000000000000000000000000"
        const val MAX_BYTES = 192 * 1024L
        const val KEEP_RECORDS = 300
    }
}

/** App-wide handle: logs under the app's private files dir, signed with the Keystore-protected identity. */
object PhoneAudit {
    private var chain: AuditChain? = null

    @Synchronized private fun chain(ctx: Context): AuditChain {
        chain?.let { return it }
        val app = ctx.applicationContext
        return AuditChain(File(app.filesDir, "security-audit.log")) { Store.identity(app) }.also { chain = it }
    }

    fun log(ctx: Context, type: String, vararg meta: Pair<String, Any>) {
        runCatching { chain(ctx).log(type, JSONObject().also { o -> meta.forEach { (k, v) -> o.put(k, v) } }) }
    }

    fun verify(ctx: Context) = chain(ctx).verify()
    fun recent(ctx: Context, n: Int = 12): List<JSONObject> =
        chain(ctx).records().takeLast(n).reversed().map { chain(ctx).body(it) }

    /** Failed handshakes in the last 24 h. */
    fun failedAuthLastDay(ctx: Context): Int {
        val since = System.currentTimeMillis() - 24 * 3600_000L
        return chain(ctx).records().map { chain(ctx).body(it) }.count { it.optString("type") == "auth.failed" && it.optLong("ts") > since }
    }
}
