package dev.phonebridge

import org.bouncycastle.asn1.nist.NISTNamedCurves
import org.bouncycastle.crypto.generators.SCrypt
import org.bouncycastle.math.ec.ECPoint
import java.math.BigInteger
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * SPAKE2 (RFC 9382 construction, P-256 / SHA-256 / HKDF / HMAC): the phone's half of PIN pairing. Mirrors
 * linux/phonebridge/spake2.py byte for byte (tests/test_spake2.py compiles this file and checks it against the Python).
 * Role A = the phone (share X = x*G + w*M); the PC is role B (Y = y*G + w*N).
 */
class Spake2(pin: String, private val context: ByteArray, scalar: BigInteger? = null) {
    class Keys(val ke: ByteArray, val macPhone: ByteArray, val macPc: ByteArray)

    private val w: BigInteger = passwordToW(pin)
    private val x: BigInteger = scalar ?: BigInteger(320, SecureRandom()).mod(ORDER.subtract(BigInteger.ONE)).add(BigInteger.ONE)
    val share: ByteArray = G.multiply(x).add(M.multiply(w)).getEncoded(false)

    /** Combines with the PC's share (throws for an invalid point). A wrong PIN is only visible through the MACs. */
    fun finish(peerShare: ByteArray): Keys {
        val peer = decode(peerShare)
        val unblinded = peer.subtract(N.multiply(w))
        val z = unblinded.multiply(x)
        val v = unblinded.multiply(w)
        require(!z.isInfinity && !v.isInfinity) { "degenerate share" }
        val xShare = share
        val yShare = peer.getEncoded(false)
        val tt = len8(context) + len8(ID_PHONE) + len8(ID_PC) + len8(xShare) + len8(yShare) +
            len8(z.normalize().getEncoded(false)) + len8(v.normalize().getEncoded(false)) + len8(fixed32(w))
        val digest = MessageDigest.getInstance("SHA-256").digest(tt)
        val ke = digest.copyOfRange(0, 16)
        val ka = digest.copyOfRange(16, 32)
        val kc = hkdf(ka, "ConfirmationKeys".toByteArray(), 32)
        return Keys(ke, hmac(kc.copyOfRange(0, 16), yShare), hmac(kc.copyOfRange(16, 32), xShare))
    }

    companion object {
        private val params = NISTNamedCurves.getByName("P-256")
        private val G: ECPoint = params.g
        private val ORDER: BigInteger = params.n
        private val ID_PHONE = "phonebridge-phone".toByteArray()
        private val ID_PC = "phonebridge-pc".toByteArray()
        private val SALT = "phonebridge-spake2-v1".toByteArray()
        private val M = decode("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f".hexToBytes())
        private val N = decode("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49".hexToBytes())

        /** SEC1 point; refuses anything that is not a valid, non-identity point on the curve. */
        fun decode(data: ByteArray): ECPoint {
            val p = params.curve.decodePoint(data).normalize()
            require(!p.isInfinity && p.isValid) { "bad point" }
            return p
        }

        fun passwordToW(pin: String): BigInteger {
            val raw = SCrypt.generate(pin.toByteArray(Charsets.UTF_8), SALT, 1 shl 14, 8, 1, 48)
            return BigInteger(1, raw).mod(ORDER.subtract(BigInteger.ONE)).add(BigInteger.ONE)
        }

        private fun len8(b: ByteArray): ByteArray {
            val out = ByteArray(8 + b.size)
            var n = b.size.toLong()
            for (i in 0 until 8) { out[i] = (n and 0xff).toByte(); n = n shr 8 }
            System.arraycopy(b, 0, out, 8, b.size)
            return out
        }

        private fun fixed32(v: BigInteger): ByteArray {
            val raw = v.toByteArray().let { if (it.size > 1 && it[0] == 0.toByte()) it.copyOfRange(1, it.size) else it }
            return ByteArray(32 - raw.size) + raw
        }

        private fun hmac(key: ByteArray, msg: ByteArray): ByteArray =
            Mac.getInstance("HmacSHA256").apply { init(SecretKeySpec(key, "HmacSHA256")) }.doFinal(msg)

        private fun hkdf(ikm: ByteArray, info: ByteArray, length: Int): ByteArray {
            val prk = hmac(ByteArray(32), ikm)
            var out = ByteArray(0)
            var block = ByteArray(0)
            var i = 1
            while (out.size < length) {
                block = hmac(prk, block + info + byteArrayOf(i.toByte()))
                out += block
                i++
            }
            return out.copyOf(length)
        }
    }
}
