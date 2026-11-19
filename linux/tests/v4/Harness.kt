// Plays the PHONE using the real Android SecureSession.kt / Crypto.kt against the Python PC (test_android_interop.py).
import dev.phonebridge.Crypto
import dev.phonebridge.SecureSession
import org.json.JSONObject
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.Socket

fun hex(s: String) = ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
fun toHex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }

fun main(args: Array<String>) {
    val port = args[0].toInt(); val psk = hex(args[1]); val fp = args[2]; val tlsFp = args[3]
    val identity = SecureSession.Identity.fromRaw(hex(args[4])); val pcPub = args[5]
    Socket("127.0.0.1", port).use { sock ->
        val out = DataOutputStream(sock.getOutputStream()); val inp = DataInputStream(sock.getInputStream())
        fun send(blob: ByteArray) { Crypto.writeFrame(out, blob); out.flush() }
        fun recv() = Crypto.readFrame(inp)

        val (hello, state) = SecureSession.clientHello(psk, fp, tlsFp, identity)
        send(hello)
        val s = SecureSession.clientFinish(state, recv(), pcPub)
        println("SESSION " + s.sessionId)
        println("EXPORTER " + toHex(s.exporter))
        println("UDP " + toHex(s.udpKey))

        // control frames both ways under the per-direction keys
        send(s.cipher.encrypt("""{"ch":"control","type":"ping","seq":1,"payload":{}}""".toByteArray()))
        println("GOT " + String(s.cipher.decrypt(recv())))
        // a file data connection key and a video one
        send(s.dataCipher("file|abc").encrypt("file-data-from-android".toByteArray()))
        send(s.dataCipher("video").encrypt("video-from-android".toByteArray()))
        println("DATA " + String(s.dataCipher("file|abc").decrypt(recv())))

        // rotation, initiated by the PC
        val rk = JSONObject(String(s.cipher.decrypt(recv())))
        require(rk.getString("type") == "rekey") { "expected rekey, got $rk" }
        val eph = s.respondRekey(rk.getJSONObject("payload").getString("eph"))
        send(s.cipher.encrypt(JSONObject().put("ch", "control").put("type", "rekey.ack").put("seq", 2)
            .put("payload", JSONObject().put("eph", eph)).toString().toByteArray()))
        s.commitSend()
        val done = JSONObject(String(s.cipher.decrypt(recv())))
        require(done.getString("type") == "rekey.done") { "expected rekey.done, got $done" }
        s.commitRecv()
        println("GEN " + s.generation)
        send(s.cipher.encrypt("""{"ch":"control","type":"ping","seq":3,"payload":{}}""".toByteArray()))
        println("AFTER " + String(s.cipher.decrypt(recv())))
        println("EXPORTER2 " + toHex(s.exporter))
        // identity rotation, both directions (args: new PC pub, Python's signature over it, a new phone pub)
        println("ROT_VERIFY " + SecureSession.verifyRotation(pcPub, fp, args[6], args[7]))
        println("ROT_FORGED " + SecureSession.verifyRotation(identity.pubB64, fp, args[6], args[7]))
        println("ROT_SIGN " + SecureSession.signRotation(identity, fp, args[8]))
    }
}
