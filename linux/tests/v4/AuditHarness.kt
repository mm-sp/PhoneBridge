// Exercises the real Android AuditChain + PcAccess.featureFor on a JVM (test_android_audit.py checks the output).
import dev.phonebridge.AuditChain
import dev.phonebridge.PcAccess
import dev.phonebridge.Privacy
import dev.phonebridge.SecureSession
import org.json.JSONObject
import java.io.File

fun main(args: Array<String>) {
    val dir = File(args[0]); dir.mkdirs()
    val id = SecureSession.Identity.generate()
    val file = File(dir, "audit.log")
    val chain = AuditChain(file) { id }
    for (i in 0 until 5) chain.log("event.$i", JSONObject().put("n", i), now = 1_700_000_000_000L + i)
    println("PUB " + id.pubB64)
    println("CLEAN " + chain.verify().ok + " " + chain.verify().records)

    val lines = file.readLines()
    fun check(label: String, mutated: List<String>): String {
        val f = File(dir, "t.log"); f.writeText(mutated.joinToString("\n") + "\n")
        val v = AuditChain(f) { id }.verify()
        return "$label ${v.ok}"
    }
    // edit record 2's body
    val rec = JSONObject(lines[2]); rec.put("body", rec.getString("body").replace("event.2", "event.X"))
    println(check("TAMPER_EDIT", lines.take(2) + rec.toString() + lines.drop(3)))
    println(check("TAMPER_DELETE", lines.take(2) + lines.drop(3)))
    println(check("TAMPER_REORDER", listOf(lines[0], lines[2], lines[1]) + lines.drop(3)))
    val forged = JSONObject(lines[1]); forged.put("sig", JSONObject(lines[0]).getString("sig"))
    println(check("TAMPER_SIG", listOf(lines[0], forged.toString()) + lines.drop(2)))

    for ((i, c) in listOf(
        "com.example.chat" to "Your verification code is 123456", "com.example.sms" to "G-482913 is your Google verification code",
        "com.example.bank" to "OTP 8842 for your transfer", "com.example.mail" to "Reset your password now",
        "com.google.android.apps.authenticator2" to "Account", "com.bitwarden.mobile" to "Vault unlocked",
        "com.example.chat" to "Lunch at 1?", "com.example.maps" to "Meeting at 12345 Main St",
        "com.example.shop" to "Order 123456 has shipped", "com.example.chat" to "see you in 5",
    ).withIndex()) {
        println("SENSITIVE $i ${Privacy.isSensitive(c.first, c.second)}")
    }
    for (t in listOf("sms.send", "sms.send_mms", "call.dial", "call.action", "sms.get_thread", "file.offer", "files.get", "photos.list", "sms.send", "sms.list_threads", "call.dial", "contacts.list_request",
        "mirror.start", "webcam.start", "mirror.input", "hotspot.request", "lock", "apps.launch", "clipboard.update", "notifications.sync",
        "ping", "device.info_request", "relay.config", "link.disconnect", "rekey", "identity.rotate", "session.end")) {
        println("FEATURE $t ${PcAccess.featureFor(t)}")
    }
}
