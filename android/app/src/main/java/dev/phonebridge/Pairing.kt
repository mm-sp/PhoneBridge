package dev.phonebridge

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothSocket
import android.content.Context
import android.os.Build
import org.json.JSONObject
import java.io.DataInputStream
import java.security.MessageDigest
import java.util.UUID

/** The phone's half of a pairing record (PC keeps the other half in ~/.config/phonebridge). */
data class PairedPc(
    val fingerprint: String,
    val name: String,
    val sessionKey: ByteArray,
    val lanIp: String,
    val lanPort: Int,
    val certFingerprint: String,
    val btAddr: String,
    /** The PC's Ed25519 identity public key (base64), learned in pairing; "" = a pre-v4 pairing that must be redone. */
    val pcIdentityPub: String = "",
    /** This phone's identity (private key held only in Android-Keystore-wrapped storage). */
    val identity: SecureSession.Identity? = null,
)

object Store {
    private const val PREFS = "pairing"

    fun load(ctx: Context): PairedPc? {
        val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val fp = p.getString("fingerprint", null) ?: return null
        // The pairing key is stored only as Android-Keystore-wrapped ciphertext. A pre-wrapping install still has
        // it as plain hex: wrap it now and delete the plaintext.
        val sessionKey = p.getString("session_key_enc", null)?.let { KeystoreVault.open(it, "session-key:$fp") }
            ?: p.getString("session_key", "")!!.hexToBytes().also { legacy ->
                if (legacy.isNotEmpty()) p.edit().putString("session_key_enc", KeystoreVault.seal(legacy, "session-key:$fp"))
                    .remove("session_key").apply()
            }
        return PairedPc(
            fingerprint = fp,
            name = p.getString("name", "PC")!!,
            sessionKey = sessionKey,
            pcIdentityPub = p.getString("pc_identity_pub", "")!!,
            identity = identity(ctx),
            lanIp = p.getString("lan_ip", "")!!,
            lanPort = p.getInt("lan_port", 28100),
            certFingerprint = p.getString("cert_fingerprint", "")!!,
            btAddr = p.getString("bt_addr", "")!!,
        )
    }

    fun save(ctx: Context, pc: PairedPc) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("fingerprint", pc.fingerprint)
            .putString("name", pc.name)
            .putString("session_key_enc", KeystoreVault.seal(pc.sessionKey, "session-key:${pc.fingerprint}"))
            .remove("session_key")
            .putString("pc_identity_pub", pc.pcIdentityPub)
            .putString("lan_ip", pc.lanIp)
            .putInt("lan_port", pc.lanPort)
            .putString("cert_fingerprint", pc.certFingerprint)
            .putString("bt_addr", pc.btAddr)
            .apply()
    }

    /** This phone's Ed25519 identity, created on first use. Private key: Keystore-wrapped ciphertext only. */
    @Synchronized fun identity(ctx: Context): SecureSession.Identity {
        val p = ctx.getSharedPreferences("identity", Context.MODE_PRIVATE)
        p.getString("ed25519_enc", null)?.let { return SecureSession.Identity.fromRaw(KeystoreVault.open(it, "identity-ed25519")) }
        val fresh = SecureSession.Identity.generate()
        p.edit().putString("ed25519_enc", KeystoreVault.seal(fresh.priv.encoded, "identity-ed25519")).apply()
        return fresh
    }

    /** The PC rotated its identity key (and the old key vouched for it): remember the new one. */
    fun setPcIdentity(ctx: Context, newPub: String) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString("pc_identity_pub", newPub).apply()
    }

    /** This phone rotated its own identity key and the PC acknowledged: switch to the new one. */
    @Synchronized fun setIdentity(ctx: Context, fresh: SecureSession.Identity) {
        ctx.getSharedPreferences("identity", Context.MODE_PRIVATE).edit()
            .putString("ed25519_enc", KeystoreVault.seal(fresh.priv.encoded, "identity-ed25519")).apply()
    }

    /** Called when discovery finds the PC at a new address (DHCP gave it another IP). */
    fun updateLan(ctx: Context, ip: String, port: Int) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("lan_ip", ip).putInt("lan_port", port).apply()
    }

    /** Set when the PC sends link.disconnect: stay off until the user taps Connect. */
    fun isPaused(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean("paused", false)

    fun setPaused(ctx: Context, paused: Boolean) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean("paused", paused).apply()
    }

    fun clear(ctx: Context) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply()
    }
}

class PairingException(msg: String) : Exception(msg)

/**
 * Phone side of protocol/PROTOCOL.md section 1.
 *
 * Wi-Fi first: the QR code lists the PC's LAN addresses + pairing port; with only a PIN
 * the phone broadcasts a pair_discover to find a PC whose Pair dialog is open. Bluetooth
 * RFCOMM (channel 4 — the PC publishes no SDP record) is the fallback, and is switched on
 * automatically if needed. The handshake bytes are identical on both routes.
 */
object Pairing {
    private const val RFCOMM_CHANNEL = 4
    private const val PROTOCOL_VERSION = 4   // v4: the PIN is verified with SPAKE2 (see Spake2.kt), not an HMAC proof
    private val SERVICE_UUID: UUID = UUID.fromString("b43c1e00-35ee-4b8a-9e3f-6c3f2a1b9d10")

    /** Pair from a scanned QR payload, or from just the PIN (`qr == null`). */
    /**
     * @param confirm shown the 6-digit code AFTER the PC accepted and BEFORE anything is trusted or saved; returning false
     * cancels. A PIN-only pairing has no QR code to bind the PC's key, so without this a rogue device on the Wi-Fi that
     * answers the discovery broadcast (and cracks the 6-digit PIN offline from the proof) would be paired silently.
     * The user compares the code with the one the REAL PC shows when it asks to allow this phone.
     */
    fun pair(ctx: Context, qr: JSONObject?, pin: String?, progress: (String) -> Unit = {}, confirm: (String) -> Boolean = { true }): PairedPc {
        val token = qr?.optString("token")?.takeIf { it.isNotEmpty() } ?: pin?.trim()
            ?: throw PairingException("no PIN")
        val qrPub = qr?.optString("pub")?.takeIf { it.isNotEmpty() }

        // 1. Wi-Fi
        val targets = ArrayList<Pair<String, Int>>()
        // USB first: if the PC set up `adb reverse` for the pairing port (it does while its Pair dialog is open and the phone
        // is plugged in with USB debugging on), 127.0.0.1 reaches the PC over the cable only. Without the tunnel nothing
        // listens here and the connection is refused immediately. (No probe connection: the PC accepts just one.)
        targets.add("127.0.0.1" to (qr?.optInt("pport", 28102) ?: 28102))
        qr?.optJSONArray("lan")?.let { arr ->
            val port = qr.optInt("pport", 28102)
            for (i in 0 until arr.length()) targets.add(arr.getString(i) to port)
        }
        if (targets.none { it.first != "127.0.0.1" }) {
            progress("Looking for your PC on Wi-Fi…")
            targets.addAll(discoverPairingPcs())
        }
        var wifiError: String? = null
        for ((host, port) in targets) {
            progress(if (host == "127.0.0.1") "Connecting to PC over USB…" else "Connecting to PC over Wi-Fi ($host)…")
            val sock = try {
                java.net.Socket().apply { connect(java.net.InetSocketAddress(host, port), 2500); soTimeout = 30_000 }
            } catch (e: Exception) {
                wifiError = e.message
                continue
            }
            try {
                return handshake(ctx, sock, sock.getInputStream(), sock.getOutputStream(), token, qrPub,
                    btAddr = qr?.optString("bt_addr") ?: "", progress = progress, confirm = confirm)
            } catch (e: PairingException) {
                // An `adb reverse` tunnel accepts locally even when the PC isn't listening behind it: that's "no USB", not an error.
                if (host == "127.0.0.1" && e.message == "PC closed the connection") { wifiError = e.message; continue }
                throw e
            } finally {
                runCatching { sock.close() }
            }
        }

        // 2. Bluetooth fallback
        val btAddr = qr?.optString("bt_addr")?.takeIf { it.isNotEmpty() && it != "null" }
            ?: throw PairingException(
                if (targets.none { it.first != "127.0.0.1" }) "Couldn't find a PC with the Pair dialog open. Connect the PC and phone to the same Wi-Fi, or connect the PC to this phone's hotspot."
                else "Couldn't reach the PC over Wi-Fi (${wifiError ?: "no answer"}). Is it on the same network?")
        progress("Wi-Fi didn't work, trying Bluetooth…")
        val sock = connectBluetooth(btAddr)
        try {
            return handshake(ctx, null, sock.inputStream, sock.outputStream, token, qrPub, btAddr, progress, confirm)
        } finally {
            runCatching { sock.close() }
        }
    }

    /**
     * Protocol v2: the PIN is never sent. The phone proves it knows the PIN (HMAC over both
     * keys + the PC's nonce), the PC user allows the phone after comparing the confirmation
     * code shown on both screens, and the PC proves the PIN back before we trust lan_info.
     */
    private fun handshake(
        ctx: Context, tcp: java.net.Socket?, rawIn: java.io.InputStream, out: java.io.OutputStream,
        token: String, qrPub: String?, btAddr: String, progress: (String) -> Unit, confirm: (String) -> Boolean,
    ): PairedPc {
        val input = DataInputStream(rawIn)
        val offer = JSONObject(readLine(input))
        if (offer.optInt("v") != PROTOCOL_VERSION) {
            throw PairingException("PhoneBridge on the PC is a different version — update it on the PC and phone")
        }
        val pcPub = offer.getString("pub")
        val nonce = offer.getString("nonce")
        if (qrPub != null && qrPub != pcPub) throw PairingException("a different device answered — scan the QR again")

        val mine = Crypto.generateKeypair()
        // SPAKE2: the PIN is the password. Nothing sent here lets anyone test PIN guesses offline.
        val context = listOf("phonebridge-pair-v4", pcPub, mine.publicB64, nonce).joinToString("|").toByteArray(Charsets.UTF_8)
        val pake = Spake2(token, context)
        val hello = JSONObject()
            .put("v", PROTOCOL_VERSION).put("pub", mine.publicB64)
            .put("spake", Crypto.b64(pake.share))
            .put("name", deviceName())
        out.write((hello.toString() + "\n").toByteArray())
        out.flush()

        val shareLine = JSONObject(readLine(input))
        if (!shareLine.optBoolean("ok", true)) throw PairingException(shareLine.optString("error", "PC rejected pairing"))
        val keys = try {
            pake.finish(Crypto.unb64(shareLine.getString("spake")))
        } catch (e: Exception) {
            throw PairingException("the PC sent an invalid key — pairing stopped")
        }
        // The phone proves the PIN first; a wrong PIN gets a refusal and the PC counts the attempt.
        out.write((JSONObject().put("confirm", keys.macPhone.toHex()).toString() + "\n").toByteArray())
        out.flush()
        val pcConfirm = JSONObject(readLine(input))
        if (!pcConfirm.optBoolean("ok", true)) throw PairingException(pcConfirm.optString("error", "PC rejected pairing"))
        val pcMac = pcConfirm.optString("confirm", "")
        if (!MessageDigest.isEqual(keys.macPc, runCatching { pcMac.hexToBytes() }.getOrDefault(ByteArray(0)))) {
            throw PairingException("this PC didn't prove it knows the PIN — pairing stopped")
        }

        val pcPubRaw = Crypto.unb64(pcPub)
        val code = Crypto.confirmationCode(pcPubRaw, mine.publicRaw)
        progress("On the PC, check the code is ${code.substring(0, 3)} ${code.substring(3)} and click Allow")
        // The PC waits for the user to click Allow, so give the answer up to 2 minutes.
        tcp?.soTimeout = 130_000

        val ack = JSONObject(readLine(input))
        if (!ack.optBoolean("ok")) throw PairingException(ack.optString("error", "PC rejected pairing"))

        // Nothing is trusted or saved until the user confirms the code against the REAL PC's screen (see pair()).
        if (!confirm(code)) throw PairingException("Pairing cancelled: the code didn't match the one on your PC")

        val key = Crypto.deriveSessionKeyV4(mine, pcPubRaw, keys.ke)
        val fp = Crypto.fingerprint(pcPubRaw, mine.publicRaw)

        val cipher = Crypto.SessionCipher(key)
        val lanInfo = JSONObject(String(cipher.decrypt(Crypto.readFrame(input))))
        val pcIdPub = lanInfo.optString("id_pub")
        if (pcIdPub.isEmpty()) throw PairingException("the PC app is too old (no device identity) — update PhoneBridge on the PC")
        // Our identity goes back inside the same PIN-authenticated, encrypted channel, so no one in the middle can swap it.
        val identity = Store.identity(ctx)
        Crypto.writeFrame(out, cipher.encrypt(JSONObject().put("id_pub", identity.pubB64).toString().toByteArray()))
        out.flush()
        val pc = PairedPc(
            fingerprint = fp,
            name = ack.optString("name", offer.optString("name", "PC")),
            sessionKey = key,
            lanIp = lanInfo.getString("lan_ip"),
            lanPort = lanInfo.getInt("lan_port"),
            certFingerprint = lanInfo.getString("cert_fingerprint"),
            btAddr = btAddr.uppercase(),
            pcIdentityPub = pcIdPub,
            identity = identity,
        )
        Store.save(ctx, pc)
        Store.setPaused(ctx, false)
        return pc
    }

    /** Broadcasts pair_discover; PCs with the Pair dialog open answer with their pairing port. */
    private fun discoverPairingPcs(): List<Pair<String, Int>> {
        val found = LinkedHashMap<String, Int>()
        runCatching {
            java.net.DatagramSocket().use { sock ->
                sock.broadcast = true
                sock.soTimeout = 700
                val msg = JSONObject().put("phonebridge", "pair_discover").toString().toByteArray()
                val buf = ByteArray(1024)
                repeat(3) {
                    Net.broadcast(sock, msg, Connection.DISCOVERY_PORT)
                    val deadline = System.currentTimeMillis() + 700
                    while (System.currentTimeMillis() < deadline) {
                        val reply = java.net.DatagramPacket(buf, buf.size)
                        try { sock.receive(reply) } catch (_: java.net.SocketTimeoutException) { break }
                        val obj = runCatching { JSONObject(String(reply.data, 0, reply.length)) }.getOrNull() ?: continue
                        if (obj.optString("phonebridge") == "pairing_here") {
                            found[reply.address.hostAddress!!] = obj.optInt("port", 28102)
                        }
                    }
                    if (found.isNotEmpty()) return@use
                }
            }
        }
        return found.map { it.key to it.value }
    }

    fun deviceName(): String {
        val model = Build.MODEL ?: "Android"
        val maker = Build.MANUFACTURER ?: ""
        return if (model.startsWith(maker, ignoreCase = true)) model else "$maker $model".trim()
    }

    @SuppressLint("MissingPermission", "DiscouragedPrivateApi")
    private fun connectBluetooth(btAddr: String): BluetoothSocket {
        val adapter = BluetoothAdapter.getDefaultAdapter()
            ?: throw PairingException("Couldn't reach the PC over Wi-Fi, and this phone has no Bluetooth")
        if (!adapter.isEnabled) {
            @Suppress("DEPRECATION")
            adapter.enable()  // turn Bluetooth on for the user (allowed for targetSdk < 33)
            val deadline = System.currentTimeMillis() + 8000
            while (!adapter.isEnabled && System.currentTimeMillis() < deadline) Thread.sleep(200)
            if (!adapter.isEnabled) throw PairingException("Couldn't turn on Bluetooth — turn it on and try again")
        }
        adapter.cancelDiscovery()
        val device = adapter.getRemoteDevice(btAddr.uppercase())
        val attempts = listOf<() -> BluetoothSocket>(
            { device.javaClass.getMethod("createInsecureRfcommSocket", Int::class.javaPrimitiveType)
                .invoke(device, RFCOMM_CHANNEL) as BluetoothSocket },
            { device.javaClass.getMethod("createRfcommSocket", Int::class.javaPrimitiveType)
                .invoke(device, RFCOMM_CHANNEL) as BluetoothSocket },
            { device.createInsecureRfcommSocketToServiceRecord(SERVICE_UUID) },
        )
        var last: Exception? = null
        for (make in attempts) {
            try {
                val s = make()
                s.connect()
                return s
            } catch (e: Exception) {
                last = e
            }
        }
        throw PairingException("couldn't reach the PC over Wi-Fi or Bluetooth (${last?.message}). Is the Pair dialog open on the PC?")
    }

    private fun readLine(input: DataInputStream): String {
        val buf = java.io.ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b < 0) throw PairingException("PC closed the connection")
            if (b == '\n'.code) break
            buf.write(b)
        }
        return buf.toString("UTF-8")
    }
}
