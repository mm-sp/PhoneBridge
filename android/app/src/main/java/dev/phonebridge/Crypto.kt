package dev.phonebridge

import android.util.Base64
import org.bouncycastle.crypto.agreement.X25519Agreement
import org.bouncycastle.crypto.digests.SHA256Digest
import org.bouncycastle.crypto.generators.HKDFBytesGenerator
import org.bouncycastle.crypto.params.HKDFParameters
import org.bouncycastle.crypto.params.X25519PrivateKeyParameters
import org.bouncycastle.crypto.params.X25519PublicKeyParameters
import java.io.DataInputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** Mirrors linux/phonebridge/crypto.py — change one, change the other. */
object Crypto {
    private const val NONCE_LEN = 12
    private const val TAG_BITS = 128
    private val random = SecureRandom()

    class Keypair(val private: X25519PrivateKeyParameters) {
        val publicRaw: ByteArray = private.generatePublicKey().encoded
        val publicB64: String get() = b64(publicRaw)
    }

    fun generateKeypair(): Keypair = Keypair(X25519PrivateKeyParameters(random))

    fun b64(data: ByteArray): String = Base64.encodeToString(data, Base64.NO_WRAP)
    fun unb64(s: String): ByteArray = Base64.decode(s, Base64.DEFAULT)

    /** Pairing v4: HKDF-SHA256(X25519(my, their), salt=SPAKE2 key Ke, info="phonebridge-v4|pairing") — crypto.derive_session_key_v4(). */
    fun deriveSessionKeyV4(mine: Keypair, theirPubRaw: ByteArray, spakeKey: ByteArray): ByteArray {
        val agreement = X25519Agreement()
        agreement.init(mine.private)
        val shared = ByteArray(agreement.agreementSize)
        agreement.calculateAgreement(X25519PublicKeyParameters(theirPubRaw, 0), shared, 0)
        val hkdf = HKDFBytesGenerator(SHA256Digest())
        hkdf.init(HKDFParameters(shared, spakeKey, "phonebridge-v4|pairing".toByteArray()))
        val out = ByteArray(32)
        hkdf.generateBytes(out, 0, 32)
        return out
    }

    /** SHA256(sorted(pc_pub, phone_pub))[:8] as hex — same as crypto.fingerprint(). */
    fun fingerprint(a: ByteArray, b: ByteArray): String {
        val (first, second) = if (compareUnsigned(a, b) <= 0) a to b else b to a
        val md = MessageDigest.getInstance("SHA-256")
        md.update(first)
        md.update(second)
        return md.digest().copyOf(8).toHex()
    }

    /** Rotating proof for the UDP discovery broadcast — same as crypto.discovery_tag(). */
    fun discoveryTag(sessionKey: ByteArray, bucket: Long): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(sessionKey, "HmacSHA256"))
        return mac.doFinal("phonebridge-discover-v2|$bucket".toByteArray(Charsets.UTF_8)).toHex().take(32)
    }

    /** Room name on a relay server — same as crypto.relay_room(): HMAC(session_key, "phonebridge-relay-v1")[:32 hex]. */
    fun relayRoom(sessionKey: ByteArray): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(sessionKey, "HmacSHA256"))
        return mac.doFinal("phonebridge-relay-v1".toByteArray(Charsets.UTF_8)).toHex().take(32)
    }

    fun proofMatches(expectedHex: String, given: String?): Boolean =
        given != null && MessageDigest.isEqual(expectedHex.toByteArray(), given.toByteArray())

    /** 6-digit code shown on both screens — same as crypto.confirmation_code(). */
    fun confirmationCode(a: ByteArray, b: ByteArray): String {
        val (first, second) = if (compareUnsigned(a, b) <= 0) a to b else b to a
        val md = MessageDigest.getInstance("SHA-256")
        md.update("phonebridge-confirm-v2".toByteArray())
        md.update(first)
        md.update(second)
        val d = md.digest()
        val n = (((d[0].toLong() and 0xff) shl 24) or ((d[1].toLong() and 0xff) shl 16) or
            ((d[2].toLong() and 0xff) shl 8) or (d[3].toLong() and 0xff)) % 1_000_000
        return "%06d".format(n)
    }

    private fun compareUnsigned(a: ByteArray, b: ByteArray): Int {
        for (i in 0 until minOf(a.size, b.size)) {
            val d = (a[i].toInt() and 0xff) - (b[i].toInt() and 0xff)
            if (d != 0) return d
        }
        return a.size - b.size
    }

    /** Direction labels authenticated as AES-GCM associated data (PROTOCOL.md section 1, v3): the phone seals
     *  with phone>pc and only opens pc>phone, so a reflected frame fails to decrypt. */
    private val AAD_PHONE_TO_PC = "phonebridge-v3|phone>pc".toByteArray(Charsets.US_ASCII)
    private val AAD_PC_TO_PHONE = "phonebridge-v3|pc>phone".toByteArray(Charsets.US_ASCII)

    /** [recvKey] (protocol v4) gives each direction its own key; without it both share [sendKey]. Keys can be swapped
     *  while the connection lives (rekeying, see SecureSession.Session). */
    class SessionCipher(sendKey: ByteArray, recvKey: ByteArray = sendKey) {
        @Volatile private var sendSpec = SecretKeySpec(sendKey, "AES")
        @Volatile private var recvSpec = SecretKeySpec(recvKey, "AES")

        fun setSendKey(key: ByteArray) { sendSpec = SecretKeySpec(key, "AES") }
        fun setRecvKey(key: ByteArray) { recvSpec = SecretKeySpec(key, "AES") }

        fun encrypt(plain: ByteArray): ByteArray {
            val nonce = ByteArray(NONCE_LEN).also { random.nextBytes(it) }
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.ENCRYPT_MODE, sendSpec, GCMParameterSpec(TAG_BITS, nonce))
            c.updateAAD(AAD_PHONE_TO_PC)
            return nonce + c.doFinal(plain)
        }

        fun decrypt(blob: ByteArray): ByteArray {
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, recvSpec, GCMParameterSpec(TAG_BITS, blob, 0, NONCE_LEN))
            c.updateAAD(AAD_PC_TO_PHONE)
            return c.doFinal(blob, NONCE_LEN, blob.size - NONCE_LEN)
        }
    }

    fun writeFrame(out: OutputStream, blob: ByteArray) {
        out.write(ByteBuffer.allocate(4).putInt(blob.size).array())
        out.write(blob)
    }

    fun readFrame(input: DataInputStream): ByteArray {
        val n = input.readInt()
        require(n in 0..(64 * 1024 * 1024)) { "bad frame length $n" }
        val buf = ByteArray(n)
        input.readFully(buf)
        return buf
    }
}

fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }
fun String.hexToBytes(): ByteArray = chunked(2).map { it.toInt(16).toByte() }.toByteArray()
