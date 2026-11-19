package dev.phonebridge

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.nio.ByteBuffer
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * UDP video sender (protocol/PROTOCOL.md section 1f). Mirrors linux/phonebridge/udp_video.py —
 * change both together.
 *
 * Datagram: fp(8) | dir(1) | epoch(3) | counter(8) | AES-GCM(type(1) | body) + tag(16);
 * nonce = dir|epoch|counter, AAD = fp. This side sends dir 0 and accepts dir 1.
 */
class UdpVideoSender private constructor(
    private val socket: DatagramSocket,
    sessionKey: ByteArray,
    fingerprintHex: String,
    private val onKeyframeRequest: () -> Unit,
    private val onReport: (delivered: Int, lost: Int) -> Unit,
) {
    private val key = SecretKeySpec(sessionKey, "AES")
    private val fp = ByteArray(8) { fingerprintHex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    private val epoch = ByteArray(3).also { SecureRandom().nextBytes(it) }
    private var counter = 0L
    private var framesSent = 0L
    private var datagramsSent = 0L
    private var frameId = 0
    @Volatile private var open = true
    private var receiver: Thread? = null

    private fun seal(type: Int, body: ByteArray): ByteArray {
        val meta = ByteBuffer.allocate(12).put(DIR_PHONE.toByte()).put(epoch).putLong(++counter).array()
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(128, meta))
        c.updateAAD(fp)
        return fp + meta + c.doFinal(byteArrayOf(type.toByte()) + body)
    }

    /** -> (type, body) for an authentic PC->phone datagram, else null. */
    private fun open(d: ByteArray, len: Int): Pair<Int, ByteArray>? {
        if (len < HEADER_LEN + 16 + 1 || !d.copyOfRange(0, 8).contentEquals(fp)) return null
        if (d[8].toInt() != DIR_PC) return null
        return try {
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, d, 8, 12))
            c.updateAAD(fp)
            val plain = c.doFinal(d, HEADER_LEN, len - HEADER_LEN)
            plain[0].toInt() to plain.copyOfRange(1, plain.size)
        } catch (_: Exception) {
            null
        }
    }

    private fun sendDatagram(type: Int, body: ByteArray) {
        val d = seal(type, body)
        socket.send(DatagramPacket(d, d.size))
    }

    /** One access unit -> N datagrams. Paced in small bursts so a big keyframe doesn't overflow a router queue. */
    fun sendFrame(data: ByteArray, keyframe: Boolean) {
        val count = maxOf(1, (data.size + MAX_FRAGMENT - 1) / MAX_FRAGMENT)
        val id = frameId++
        framesSent++; datagramsSent += count
        // First frames and then every 100th: enough to tell "the encoder produced nothing" from "the network dropped it".
        if (framesSent <= 3 || framesSent % 100 == 0L) {
            android.util.Log.i(BridgeService.TAG, "udp video: frame #$framesSent (${data.size} B, key=$keyframe) -> $count datagram(s); total $datagramsSent")
        }
        for (i in 0 until count) {
            val chunk = data.copyOfRange(i * MAX_FRAGMENT, minOf(data.size, (i + 1) * MAX_FRAGMENT))
            val body = ByteBuffer.allocate(9 + chunk.size)
                .putInt(id).putShort(i.toShort()).putShort(count.toShort())
                .put((if (keyframe) 1 else 0).toByte()).put(chunk).array()
            sendDatagram(T_VIDEO, body)
            if (i % BURST == BURST - 1) Thread.sleep(1)
        }
    }

    private fun startReceiver() {
        receiver = Thread({
            val buf = ByteArray(2048)
            socket.soTimeout = 0
            while (open) {
                try {
                    val p = DatagramPacket(buf, buf.size)
                    socket.receive(p)
                    val (type, body) = open(buf, p.length) ?: continue
                    when (type) {
                        T_KEYREQ -> onKeyframeRequest()
                        T_REPORT -> if (body.size >= 8) {
                            val bb = ByteBuffer.wrap(body)
                            onReport(bb.int, bb.int)
                        }
                    }
                } catch (_: Exception) {
                    if (!open) break
                }
            }
        }, "pb-udp-rx").also { it.isDaemon = true; it.start() }
    }

    fun close() {
        open = false
        runCatching { socket.close() }
    }

    companion object {
        const val HEADER_LEN = 20
        const val MAX_DATAGRAM = 1200
        const val MAX_FRAGMENT = MAX_DATAGRAM - HEADER_LEN - 16 - 1 - 9
        const val BURST = 8
        const val DIR_PHONE = 0
        const val DIR_PC = 1
        const val T_VIDEO = 0
        const val T_PROBE = 1
        const val T_PROBE_ACK = 2
        const val T_KEYREQ = 3
        const val T_REPORT = 4

        /** Probes the PC's UDP port; null (-> use the TCP video connection) if no authentic ack arrives. */
        fun tryOpen(
            pc: PairedPc,
            onKeyframeRequest: () -> Unit,
            onReport: (Int, Int) -> Unit,
        ): UdpVideoSender? {
            // UDP video only works inside an authenticated control session: its key is derived from that session.
            val udpKey = Connection.control?.session?.udpKey ?: return null
            // Over the USB tunnel (adb reverse is TCP only) there is no UDP path: use the TCP video connection.
            if (Connection.controlRoute?.first == Connection.USB_HOST) return null
            val socket = DatagramSocket()
            try {
                socket.connect(InetSocketAddress(pc.lanIp, pc.lanPort))
                socket.soTimeout = 300
                val sender = UdpVideoSender(socket, udpKey, pc.fingerprint, onKeyframeRequest, onReport)
                val nonce = ByteArray(8).also { SecureRandom().nextBytes(it) }
                val buf = ByteArray(2048)
                repeat(4) {
                    sender.sendDatagram(T_PROBE, nonce)
                    try {
                        val p = DatagramPacket(buf, buf.size)
                        socket.receive(p)
                        val reply = sender.open(buf, p.length)
                        if (reply != null && reply.first == T_PROBE_ACK && reply.second.contentEquals(nonce)) {
                            sender.startReceiver()
                            return sender
                        }
                    } catch (_: java.net.SocketTimeoutException) {
                    }
                }
            } catch (_: Exception) {
            }
            socket.close()
            return null
        }
    }
}
