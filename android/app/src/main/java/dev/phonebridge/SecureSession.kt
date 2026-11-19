package dev.phonebridge

import org.bouncycastle.crypto.agreement.X25519Agreement
import org.bouncycastle.crypto.digests.SHA256Digest
import org.bouncycastle.crypto.generators.HKDFBytesGenerator
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import org.bouncycastle.crypto.params.Ed25519PublicKeyParameters
import org.bouncycastle.crypto.params.HKDFParameters
import org.bouncycastle.crypto.params.X25519PrivateKeyParameters
import org.bouncycastle.crypto.params.X25519PublicKeyParameters
import org.bouncycastle.crypto.signers.Ed25519Signer
import org.json.JSONObject
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Protocol v4 — mirrors linux/phonebridge/secure_session.py (change one, change the other).
 * Ed25519 device identities + a per-connection ephemeral X25519 handshake: forward secrecy, mutual authentication
 * that the pairing key alone can't forge, per-direction keys, in-band rekeying, and data keys derived from the session.
 */
object SecureSession {
    const val VERSION = 4
    private val random = SecureRandom()
    private val AAD_CLIENT = "phonebridge-v4|hs|phone>pc".toByteArray(Charsets.US_ASCII)
    private val AAD_SERVER = "phonebridge-v4|hs|pc>phone".toByteArray(Charsets.US_ASCII)

    class HandshakeException(message: String) : Exception(message)

    /** The phone's long-term Ed25519 identity. The private key is only ever stored wrapped by an Android Keystore key. */
    class Identity(val priv: Ed25519PrivateKeyParameters) {
        val pubRaw: ByteArray = priv.generatePublicKey().encoded
        val pubB64: String get() = Crypto.b64(pubRaw)
        fun sign(msg: ByteArray): ByteArray {
            val s = Ed25519Signer(); s.init(true, priv); s.update(msg, 0, msg.size); return s.generateSignature()
        }
        companion object {
            fun generate() = Identity(Ed25519PrivateKeyParameters(random))
            fun fromRaw(raw: ByteArray) = Identity(Ed25519PrivateKeyParameters(raw, 0))
        }
    }

    fun verify(pubB64: String, sig: ByteArray, msg: ByteArray): Boolean = try {
        val s = Ed25519Signer(); s.init(false, Ed25519PublicKeyParameters(Crypto.unb64(pubB64), 0))
        s.update(msg, 0, msg.size); s.verifySignature(sig)
    } catch (_: Exception) { false }

    /** Identity rotation: the OLD key vouches for the NEW one — same bytes as secure_session.rotation_message(). */
    fun rotationMessage(fp: String, newPubB64: String) = "phonebridge-v4|rotate|$fp|$newPubB64".toByteArray(Charsets.UTF_8)
    fun signRotation(old: Identity, fp: String, newPubB64: String): String = Crypto.b64(old.sign(rotationMessage(fp, newPubB64)))
    fun verifyRotation(oldPubB64: String, fp: String, newPubB64: String, sigB64: String): Boolean = try {
        Ed25519PublicKeyParameters(Crypto.unb64(newPubB64), 0)   // throws unless it is a valid key
        verify(oldPubB64, Crypto.unb64(sigB64), rotationMessage(fp, newPubB64))
    } catch (_: Exception) { false }

    fun hkdf(ikm: ByteArray, salt: ByteArray?, info: String, n: Int = 32): ByteArray = hkdf(ikm, salt, info.toByteArray(Charsets.UTF_8), n)

    fun hkdf(ikm: ByteArray, salt: ByteArray?, info: ByteArray, n: Int = 32): ByteArray {
        val g = HKDFBytesGenerator(SHA256Digest())
        g.init(HKDFParameters(ikm, salt, info))
        return ByteArray(n).also { g.generateBytes(it, 0, n) }
    }

    /** Human-checkable fingerprint of an identity public key — same as secure_session.fingerprint_of(). */
    fun fingerprint(pubB64: String): String = MessageDigest.getInstance("SHA-256").digest(Crypto.unb64(pubB64)).copyOf(16).toHex()

    private fun sha256(b: ByteArray) = MessageDigest.getInstance("SHA-256").digest(b)
    private fun hsKey(psk: ByteArray) = hkdf(psk, null, "phonebridge-v4|handshake")

    private fun seal(psk: ByteArray, aad: ByteArray, body: JSONObject): ByteArray {
        val nonce = ByteArray(12).also { random.nextBytes(it) }
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, SecretKeySpec(hsKey(psk), "AES"), GCMParameterSpec(128, nonce))
        c.updateAAD(aad)
        return nonce + c.doFinal(body.toString().toByteArray(Charsets.UTF_8))
    }

    private fun open(psk: ByteArray, aad: ByteArray, blob: ByteArray): JSONObject = try {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, SecretKeySpec(hsKey(psk), "AES"), GCMParameterSpec(128, blob, 0, 12))
        c.updateAAD(aad)
        JSONObject(String(c.doFinal(blob, 12, blob.size - 12), Charsets.UTF_8))
    } catch (e: Exception) { throw HandshakeException("handshake did not authenticate") }

    private fun clientMsg(fp: String, tlsFp: String, eph: String, nonce: String) =
        listOf("phonebridge-v4|client", fp, tlsFp.lowercase(), eph, nonce).joinToString("|").toByteArray(Charsets.UTF_8)

    private fun serverMsg(clientEph: String, clientN: String, fp: String, tlsFp: String, eph: String, nonce: String) =
        listOf("phonebridge-v4|server", fp, tlsFp.lowercase(), clientEph, clientN, eph, nonce).joinToString("|").toByteArray(Charsets.UTF_8)

    class ClientState(val psk: ByteArray, val fp: String, val tlsFp: String, val eph: X25519PrivateKeyParameters,
                      val ephB64: String, val nonce: String)

    /** Step 1: the first frame of a control connection (sealed with a key derived from the pairing key). */
    fun clientHello(psk: ByteArray, fp: String, tlsFp: String, identity: Identity): Pair<ByteArray, ClientState> {
        val eph = X25519PrivateKeyParameters(random)
        val ephB64 = Crypto.b64(eph.generatePublicKey().encoded)
        val nonce = Crypto.b64(ByteArray(16).also { random.nextBytes(it) })
        val sig = identity.sign(clientMsg(fp, tlsFp, ephB64, nonce))
        val body = JSONObject().put("v", VERSION).put("eph", ephB64).put("n", nonce).put("sig", Crypto.b64(sig))
        return seal(psk, AAD_CLIENT, body) to ClientState(psk, fp, tlsFp, eph, ephB64, nonce)
    }

    /** Step 2: verifies the PC (pairing key AND its Ed25519 signature, bound to the TLS cert) and derives the session. */
    fun clientFinish(st: ClientState, frame: ByteArray, pcIdentityPubB64: String): Session {
        if (pcIdentityPubB64.isEmpty()) throw HandshakeException("this pairing has no PC identity key; pair again")
        val body = open(st.psk, AAD_SERVER, frame)
        if (body.optInt("v") != VERSION) throw HandshakeException("unsupported protocol version")
        val eph = body.optString("eph"); val n = body.optString("n")
        val signed = serverMsg(st.ephB64, st.nonce, st.fp, st.tlsFp, eph, n)
        if (!verify(pcIdentityPubB64, Crypto.unb64(body.optString("sig")), signed)) {
            throw HandshakeException("the PC's signature is invalid (wrong or impersonated PC)")
        }
        val agreement = X25519Agreement(); agreement.init(st.eph)
        val shared = ByteArray(agreement.agreementSize)
        agreement.calculateAgreement(X25519PublicKeyParameters(Crypto.unb64(eph), 0), shared, 0)
        // info = label || raw SHA-256 bytes of the signed server message (same as secure_session._master)
        val master = hkdf(shared, st.psk, "phonebridge-v4|master|".toByteArray(Charsets.UTF_8) + sha256(signed))
        return Session(master, sha256(signed).toHex().take(16))
    }

    /** Keys of one authenticated connection (phone role). */
    class Session(master: ByteArray, val sessionId: String) {
        private var master = master
        val exporter: ByteArray = hkdf(master, null, "phonebridge-v4|exporter")
        val cipher: Crypto.SessionCipher
        @Volatile var generation = 0
            private set
        private var pendingMaster: ByteArray? = null
        private var pendingC2s: ByteArray? = null
        private var pendingS2c: ByteArray? = null
        private var sentSwitched = false
        private var recvSwitched = false

        init {
            cipher = Crypto.SessionCipher(hkdf(master, null, "phonebridge-v4|c2s"), hkdf(master, null, "phonebridge-v4|s2c"))
        }

        /** File/video data connections: same key both ways (direction AAD tells them apart). */
        fun dataCipher(label: String) = Crypto.SessionCipher(hkdf(exporter, null, "phonebridge-v4|data|$label"))
        val udpKey: ByteArray get() = hkdf(exporter, null, "phonebridge-v4|udp")

        /** The PC started a rekey: derive the next keys; the caller sends the ack under the OLD keys, then [commitSend]. */
        @Synchronized fun respondRekey(peerEphB64: String): String {
            val mine = X25519PrivateKeyParameters(random)
            val agreement = X25519Agreement(); agreement.init(mine)
            val shared = ByteArray(agreement.agreementSize)
            agreement.calculateAgreement(X25519PublicKeyParameters(Crypto.unb64(peerEphB64), 0), shared, 0)
            val next = hkdf(shared, master, "phonebridge-v4|rekey|${generation + 1}")
            pendingMaster = next
            pendingC2s = hkdf(next, null, "phonebridge-v4|c2s")
            pendingS2c = hkdf(next, null, "phonebridge-v4|s2c")
            return Crypto.b64(mine.generatePublicKey().encoded)
        }

        @Synchronized fun commitSend() { cipher.setSendKey(pendingC2s!!); sentSwitched = true; maybeFinish() }
        @Synchronized fun commitRecv() { cipher.setRecvKey(pendingS2c!!); recvSwitched = true; maybeFinish() }

        private fun maybeFinish() {
            if (sentSwitched && recvSwitched) {
                master = pendingMaster!!; pendingMaster = null; pendingC2s = null; pendingS2c = null
                sentSwitched = false; recvSwitched = false; generation++
            }
        }
    }
}
