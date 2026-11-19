package dev.phonebridge

import org.json.JSONObject
import java.io.DataInputStream
import java.io.BufferedOutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.security.MessageDigest
import java.security.cert.X509Certificate
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.ReentrantLock
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager
import kotlin.concurrent.withLock

/**
 * One TLS connection to the PC (protocol/PROTOCOL.md sections 2-3). The control
 * connection identifies with "<fingerprint>\n"; the on-demand video connection with
 * "<fingerprint>:video\n" (section 1b).
 *
 * TLS is pinned to the cert fingerprint learned during pairing (no CA); every frame
 * inside it is additionally AES-GCM encrypted with the pairing session key.
 */
class Connection private constructor(private val socket: SSLSocket, pc: PairedPc, initialCipher: Crypto.SessionCipher) {
    private var cipher = initialCipher

    /** True once the PC sent an authenticated session.end: this close was deliberate, not a reset or truncation. */
    @Volatile var peerEnded = false

    /** Set once the v4 handshake has run on a control connection (see SecureSession). */
    @Volatile var session: SecureSession.Session? = null
        private set
    private val input = DataInputStream(socket.inputStream.buffered(256 * 1024))
    private val output = BufferedOutputStream(socket.outputStream, 256 * 1024)
    private val seq = AtomicLong(0)

    /** Held for a whole file transfer so no other message lands between its raw chunks. */
    val writeLock = ReentrantLock()

    @Volatile var closed = false
        private set

    fun send(type: String, payload: JSONObject = JSONObject(), ch: String = "control") {
        val env = JSONObject()
            .put("ch", ch).put("type", type).put("seq", seq.incrementAndGet()).put("payload", payload)
        writeLock.withLock { writeBlob(env.toString().toByteArray(Charsets.UTF_8)) }
    }

    /** Caller must hold [writeLock] when this is part of a multi-frame sequence. */
    fun sendRaw(data: ByteArray) = writeLock.withLock { writeBlob(data) }

    private fun writeBlob(plain: ByteArray) {
        Crypto.writeFrame(output, cipher.encrypt(plain))
        output.flush()
    }

    /** A frame that is already sealed (the v4 handshake hello) — must not be encrypted again. */
    private fun writePlainFrame(frame: ByteArray) {
        Crypto.writeFrame(output, frame)
        output.flush()
    }

    /** One length-prefixed frame exactly as it came off the wire (handshake frames aren't under the session keys yet). */
    private fun readFramePlain(): ByteArray {
        input.mark(4)
        val head = ByteArray(4)
        input.readFully(head)
        // The PC sends a plain "PBUNPAIRED" instead of frames when it has forgotten us.
        if (String(head, Charsets.US_ASCII) == "PBUN") throw UnpairedException()
        // "PBBUSY <name>\n": the PC already has another phone connected (one at a time).
        if (String(head, Charsets.US_ASCII) == "PBBU") {
            val rest = java.io.ByteArrayOutputStream()
            while (true) {
                val b = input.read()
                if (b < 0 || b == '\n'.code) break
                rest.write(b)
            }
            throw BusyException(rest.toString("UTF-8").removePrefix("SY ").trim())
        }
        input.reset()
        return Crypto.readFrame(input)
    }

    fun recvRaw(): ByteArray = cipher.decrypt(readFramePlain())

    /** Control messages. Key rotation (PROTOCOL.md section 1h) is handled here and never surfaces to callers. */
    fun recv(): JSONObject {
        while (true) {
            val obj = JSONObject(String(recvRaw(), Charsets.UTF_8))
            val s = session
            when (obj.optString("type")) {
                "rekey" -> if (s != null) {
                    val eph = s.respondRekey(obj.getJSONObject("payload").getString("eph"))
                    // The ack is the last frame under the old send key; then we switch to the new one (in order, under the lock).
                    writeLock.withLock {
                        val env = JSONObject().put("ch", "control").put("type", "rekey.ack").put("seq", seq.incrementAndGet())
                            .put("payload", JSONObject().put("eph", eph))
                        writeBlob(env.toString().toByteArray(Charsets.UTF_8))
                        s.commitSend()
                    }
                    continue
                }
                // The PC announces (under the old key) that everything after this uses the new keys.
                "rekey.done" -> if (s != null) { s.commitRecv(); continue }
            }
            return obj
        }
    }

    /** Runs the client side of the v4 handshake on a freshly connected control socket. */
    private fun handshake(pc: PairedPc) {
        val identity = pc.identity ?: throw SecureSession.HandshakeException("no device identity")
        val (hello, state) = SecureSession.clientHello(pc.sessionKey, pc.fingerprint, pc.certFingerprint, identity)
        writePlainFrame(hello)   // sealed only by the handshake layer
        val s = SecureSession.clientFinish(state, readFramePlain(), pc.pcIdentityPub)
        session = s
        cipher = s.cipher
    }

    /** Says goodbye with an authenticated message (so a forged reset/truncation can be told apart), then closes and
     *  drops the session. Used when the app disconnects on purpose. */
    fun endSession(reason: String) {
        runCatching { send("session.end", JSONObject().put("reason", reason)) }
        close()
        session = null
    }

    fun close() {
        closed = true
        if (control === this) { control = null; controlRoute = null }
        runCatching { socket.close() }
    }

    companion object {
        const val DISCOVERY_PORT = 28101
        /** `adb reverse` makes the PC reachable at this address through the USB cable only. */
        const val USB_HOST = "127.0.0.1"

        /** The live, authenticated control connection: file/video data connections and UDP video derive their keys from it. */
        @Volatile var control: Connection? = null
            private set

        /** Address the live control connection uses; file/video data connections follow the same route (USB has no LAN IP). */
        @Volatile var controlRoute: Pair<String, Int>? = null
            private set

        /**
         * Opens a pinned-TLS connection. A control connection (no [dataLabel]) runs the v4 handshake; a data connection
         * ("video", "file|<transfer id>") gets a key derived from the live control session and fails without one.
         */
        fun open(pc: PairedPc, video: Boolean = false, host: String = pc.lanIp, port: Int = pc.lanPort, ident: String? = null,
                 dataLabel: String? = if (video) "video" else null): Connection {
            if (pc.pcIdentityPub.isEmpty() && dataLabel == null) throw UnpairedException()   // pre-v4 pairing: pair again
            val route = if (dataLabel != null) controlRoute else null
            val useHost = route?.first ?: host
            val usePort = route?.second ?: port
            val dataCipher = dataLabel?.let {
                (control?.session ?: throw java.io.IOException("no authenticated control session")).dataCipher(it)
            }
            val ctx = SSLContext.getInstance("TLS")
            ctx.init(null, arrayOf(PinningTrustManager(pc.certFingerprint)), null)
            val relayHost = Relay.host
            val socket: SSLSocket
            if (Relay.active && relayHost != null) {
                // Away from the PC's network: meet it at the relay (PROTOCOL.md section 1g), then run the very
                // same pinned TLS handshake through the tunnel.
                val raw = java.net.Socket()
                raw.connect(InetSocketAddress(relayHost, Relay.port), 5000)
                raw.tcpNoDelay = true
                raw.keepAlive = true
                raw.soTimeout = 15_000
                try {
                    raw.getOutputStream().apply { write("PBRELAY phone ${Crypto.relayRoom(pc.sessionKey)}\n".toByteArray()); flush() }
                    val answer = StringBuilder()
                    while (true) {  // byte by byte: nothing past the newline may be consumed (TLS follows)
                        val b = raw.getInputStream().read()
                        if (b < 0 || b == '\n'.code) break
                        answer.append(b.toChar())
                    }
                    if (answer.toString() != "OK") {
                        throw java.io.IOException(if (answer.toString() == "NOPC") "The PC isn't connected to the relay"
                        else "The relay refused the connection")
                    }
                    socket = ctx.socketFactory.createSocket(raw, relayHost, Relay.port, true) as SSLSocket
                    socket.startHandshake()
                    socket.soTimeout = 0
                } catch (e: Exception) {
                    runCatching { raw.close() }
                    throw e
                }
            } else {
                socket = ctx.socketFactory.createSocket() as SSLSocket
                socket.connect(InetSocketAddress(useHost, usePort), 3000)
                socket.tcpNoDelay = true
                socket.keepAlive = true
                socket.startHandshake()
            }
            // The control link gets a pong every 30 s; silence for this long means it's dead
            // (network changed under us) and recv() throws so the service reconnects.
            if (!video) socket.soTimeout = 90_000
            val identLine = ident ?: if (video) "${pc.fingerprint}:video\n" else "${pc.fingerprint}\n"
            socket.outputStream.write(identLine.toByteArray())
            socket.outputStream.flush()
            val conn = Connection(socket, pc, dataCipher ?: Crypto.SessionCipher(pc.sessionKey))
            if (dataLabel == null) {
                try {
                    conn.handshake(pc)
                } catch (e: Exception) {
                    conn.close()
                    throw e
                }
                control = conn
                controlRoute = host to port
            }
            return conn
        }

        /**
         * Asks the LAN "where is the PC I paired with?" — the PC answers only for a
         * fingerprint it knows. Used when the stored IP stops working (new DHCP lease).
         */
        fun discover(pc: PairedPc, timeoutMs: Int = 2500): Pair<String, Int>? {
            return try {
                DatagramSocket().use { sock ->
                    sock.broadcast = true
                    sock.soTimeout = timeoutMs
                    sock.soTimeout = timeoutMs / 3
                    val bucket = System.currentTimeMillis() / 1000 / 300
                    val msg = JSONObject().put("phonebridge", "discover")
                        .put("tag", Crypto.discoveryTag(pc.sessionKey, bucket)).toString().toByteArray()
                    val buf = ByteArray(1024)
                    var found: Pair<String, Int>? = null
                    for (attempt in 0 until 3) {
                        // Every interface's broadcast address, so a PC on this phone's own
                        // hotspot is reached too (see Net.kt).
                        Net.broadcast(sock, msg, DISCOVERY_PORT)
                        val reply = DatagramPacket(buf, buf.size)
                        try {
                            sock.receive(reply)
                        } catch (_: SocketTimeoutException) {
                            continue
                        }
                        val obj = JSONObject(String(reply.data, 0, reply.length))
                        if (obj.optString("phonebridge") == "here") {
                            found = reply.address.hostAddress!! to obj.optInt("port", pc.lanPort)
                            break
                        }
                    }
                    found
                }
            } catch (_: SocketTimeoutException) {
                null
            } catch (_: Exception) {
                null
            }
        }
    }
}

class UnpairedException : Exception("this PC no longer has this phone paired")

class BusyException(val otherPhone: String) : Exception("PC is connected to $otherPhone")

/**
 * Certificate pinning: the PC's self-signed certificate is trusted if, and only if, its SHA-256 equals the fingerprint
 * handed over (inside the PIN-authenticated channel) at pairing. There is no CA, no hostname check and no expiry check by
 * design — the pin IS the identity. This is a client-side manager only; it never accepts client certificates.
 */
private class PinningTrustManager(private val expectedSha256Hex: String) : X509TrustManager {
    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw java.security.cert.CertificateException("this side never authenticates peers by client certificate")
    }

    override fun checkServerTrusted(chain: Array<out X509Certificate>, authType: String?) {
        if (chain.isEmpty()) throw java.security.cert.CertificateException("the PC sent no certificate")
        val actual = MessageDigest.getInstance("SHA-256").digest(chain[0].encoded).toHex()
        if (!MessageDigest.isEqual(actual.lowercase().toByteArray(), expectedSha256Hex.lowercase().toByteArray())) {
            throw java.security.cert.CertificateException("PC certificate doesn't match the one from pairing")
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}

/**
 * Optional relay server (PROTOCOL.md section 1g). The PC pushes its address with `relay.config` while the phone
 * is on the same network; [active] is true while the current connection goes through it.
 */
object Relay {
    @Volatile var host: String? = null
    @Volatile var port: Int = 28200
    @Volatile var active = false

    fun load(ctx: android.content.Context) {
        val p = ctx.getSharedPreferences("relay", android.content.Context.MODE_PRIVATE)
        host = p.getString("host", null)?.takeIf { it.isNotBlank() }
        port = p.getInt("port", 28200)
    }

    fun save(ctx: android.content.Context, newHost: String, newPort: Int) {
        host = newHost.takeIf { it.isNotBlank() }
        port = if (newPort in 1..65535) newPort else 28200
        ctx.getSharedPreferences("relay", android.content.Context.MODE_PRIVATE).edit()
            .putString("host", host ?: "").putInt("port", port).apply()
    }
}
