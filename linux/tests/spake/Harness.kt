// Plays the PHONE side of SPAKE2 with the real Android Spake2.kt (test_spake2.py compares it with the Python PC side).
import dev.phonebridge.Spake2
import java.math.BigInteger

fun hex(s: String) = ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
fun toHex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }

fun main(args: Array<String>) {
    val pin = args[0]; val context = hex(args[1]); val scalar = BigInteger(args[2], 16); val peerShare = hex(args[3])
    val a = Spake2(pin, context, scalar)
    println("SHARE " + toHex(a.share))
    val k = a.finish(peerShare)
    println("KE " + toHex(k.ke))
    println("MACPHONE " + toHex(k.macPhone))
    println("MACPC " + toHex(k.macPc))
    println("W " + Spake2.passwordToW(pin).toString(16))
}
