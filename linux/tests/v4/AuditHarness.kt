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
        "ping", "device.info_request", "relay.config", "link.disconnect", "rekey", "identity.rotate", "session.end",
        "mirror.stop", "webcam.stop", "webcam.switch")) {
        println("FEATURE $t ${PcAccess.featureFor(t)}")
    }

    for (t in listOf("sms.new", "sms.thread_list", "sms.thread_messages", "call.ringing", "call.state", "calls.history",
        "contacts.list", "sms.sent", "sms.mms_status", "permission.denied", "error", "ping")) {
        println("SHARED $t ${PcAccess.sharedFeature(t)}")
    }

    for (m in PcAccess.Mode.values()) {
        println("MAY_$m " + listOf(null, true, false).joinToString(" ") { PcAccess.mayShare(m, it).toString() })
    }

    val answers = PcAccess.SessionAnswers()
    answers.record("s1", "sms", true)
    answers.record("s1", "files", false)
    answers.record("s1", "calls", null)
    answers.record("s1", "sms_send", true)
    answers.record("s1", "call_dial", false)
    println("ANSWER_YES ${answers.get("s1", "sms")}")
    println("ANSWER_NO ${answers.get("s1", "files")}")
    println("ANSWER_NONE ${answers.get("s1", "calls")}")
    println("ANSWER_EVERY_TIME ${answers.get("s1", "sms_send")} ${answers.get("s1", "call_dial")}")
    println("ANSWER_OTHER_SESSION ${answers.get("s2", "sms")} ${answers.get("s2", "files")}")

    println("CAN_WAIT " + listOf("calls.history_request", "contacts.list_request", "sms.list_threads", "sms.get_thread", "notifications.sync",
        "photos.list", "files.list", "apps.list_request").all { PcAccess.canWait(it) })
    println("MUST_NOT_WAIT " + listOf("sms.send", "sms.send_mms", "call.dial", "call.action", "mirror.input", "mirror.start", "webcam.start",
        "file.offer", "files.get", "photos.get", "clipboard.update", "apps.launch", "notification.action", "hotspot.request", "lock")
        .none { PcAccess.canWait(it) })
    val waiting = PcAccess.Waiting<String>(limit = 2)
    println("WAIT_NO_PROMPT ${waiting.hold("s1/calls", "a")} ${waiting.isOpen("s1/calls")}")
    waiting.open("s1/calls")
    println("WAIT_HELD ${waiting.hold("s1/calls", "a")} ${waiting.hold("s1/calls", "b")} ${waiting.hold("s1/calls", "c")}")
    println("WAIT_OTHER_KEY ${waiting.hold("s2/calls", "x")}")
    waiting.open("s1/calls")   // opening again keeps what already waits
    println("WAIT_RELEASED ${waiting.close("s1/calls").joinToString(",")}")
    println("WAIT_CLOSED ${waiting.hold("s1/calls", "d")} ${waiting.close("s1/calls").size}")

    val denial = PcAccess.denial("files", "file.offer", JSONObject().put("transfer_id", "t1").put("filename", "secret.txt"))
    println("DENIAL_OFFER " + denial.keys().asSequence().sorted().joinToString(",") + " " + denial.getString("transfer_id"))
    val sms = PcAccess.denial("sms_send", "sms.send", JSONObject().put("number", "5550100").put("text", "private"))
    println("DENIAL_SMS " + sms.keys().asSequence().sorted().joinToString(","))
}
