package dev.phonebridge

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.provider.Telephony
import android.util.Log
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Holds the live control connection to the paired PC, reconnects when it drops, and
 * routes incoming PC messages to the feature handlers.
 */
class BridgeService : Service() {

    enum class State { NOT_PAIRED, CONNECTING, CONNECTED, DISCONNECTED }

    @Volatile var state = State.DISCONNECTED
        private set
    @Volatile var stateDetail = ""
        private set
    @Volatile private var conn: Connection? = null
    @Volatile private var running = true
    private val wakeSignal = Object()

    val worker = Executors.newFixedThreadPool(3)
    private val scheduler = Executors.newSingleThreadScheduledExecutor()
    private var infoTask: ScheduledFuture<*>? = null
    private var pingTask: ScheduledFuture<*>? = null
    private var syncTask: ScheduledFuture<*>? = null
    @Volatile private var networkAddrs: Set<String> = emptySet()
    val main = Handler(Looper.getMainLooper())

    lateinit var calls: Calls
    lateinit var video: VideoManager
    lateinit var screenSync: ScreenSync
    private lateinit var screenReceiver: ScreenControl.Receiver
    private var wakeLock: PowerManager.WakeLock? = null

    /** Last clipboard text exchanged, to avoid echoing a PC->phone update straight back. */
    @Volatile var lastClipText: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        Transfer.purgeStale(this)   // abandoned partial downloads older than a week
        createChannels()
        startForeground(NOTIF_ID, buildNotification())
        // No permanent wakelock: the foreground service + open socket keep the link alive
        // while idle; the CPU lock is only held during transfers and video (holdWake/releaseWake).
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "phonebridge:link").apply {
                setReferenceCounted(false)
            }
        calls = Calls(this)
        video = VideoManager(this)
        screenSync = ScreenSync(this)
        screenSync.onServiceCreated()   // a kill while the display was off must not leave it dark
        Relay.load(this)
        registerReceivers()
        screenReceiver = ScreenControl.Receiver(this).also { it.register() }
        calls.startListening()
        Thread(::connectionLoop, "pb-connection").start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        lastStopReason = null
        when (intent?.action) {
            ACTION_RECONNECT -> wake()
            ACTION_CONNECT -> { Store.setPaused(this, false); wake() }
            ACTION_UNPAIR -> worker.execute {
                // Tell the PC to forget us too, so neither side keeps a dead pairing.
                PhoneAudit.log(this, "device.unpaired")
                conn?.let { c -> runCatching { c.send("device.unpair") }; c.endSession("unpaired") }
                wake()
            }
        }
        // Not sticky: after it stops itself (disconnected) Android mustn't revive it.
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        running = false
        instance = null
        conn?.endSession("app_stopped")
        video.stopAll()
        screenSync.onServiceDestroyed()
        calls.stopListening()
        runCatching { unregisterReceiver(batteryReceiver) }
        runCatching { unregisterReceiver(smsReceiver) }
        runCatching { unregisterReceiver(screenReceiver) }
        runCatching { unregisterReceiver(hotspotReceiver) }
        runCatching { contentResolver.unregisterContentObserver(mmsObserver) }
        Hotspot.stopLocal()
        BleWake.stop(this)
        runCatching { (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).unregisterNetworkCallback(netCallback) }
        runCatching { if (wakeLock?.isHeld == true) wakeLock?.release() }
        scheduler.shutdownNow()
        worker.shutdownNow()
        super.onDestroy()
    }

    // ---------------------------------------------------------------- connection

    private fun connectionLoop() {
        var backoffMs = 2000L
        // When the link went down; null while connected. After GIVE_UP_MS of failing to
        // reconnect the service stops itself, so nothing keeps running in the background.
        var downSince: Long? = System.currentTimeMillis()
        while (running) {
            val pc = Store.load(this)
            if (pc == null) {
                stopQuietly("Not paired. Scan the QR code on your PC to pair.")
                return
            }
            if (Store.isPaused(this)) {
                stopQuietly("Disconnected from ${pc.name}. Tap Connect to PC to link again.")
                return
            }
            if (downSince != null && System.currentTimeMillis() - downSince > GIVE_UP_MS) {
                stopQuietly((if (pcBusy) "${pc.name} is still connected to another phone" else "Couldn't reach ${pc.name}") +
                    ", so PhoneBridge stopped. Tap Connect to PC to try again.")
                return
            }
            setState(State.CONNECTING, "Connecting to ${pc.name}…")
            val c = try {
                tryOpen(pc)
            } catch (e: BusyException) {
                waitWhileBusy(pc, e)
                continue
            } catch (e: UnpairedException) {
                stopForgotten(pc)
                return
            }
            if (c == null) {
                pcBusy = false
                setState(State.DISCONNECTED, "Can't reach ${pc.name} — is PhoneBridge open on the PC and on the same Wi-Fi?")
                sleepUntilWoken(backoffMs)
                backoffMs = (backoffMs * 2).coerceAtMost(30_000)
                continue
            }
            backoffMs = 2000L
            conn = c
            PhoneAudit.log(this, "session.start", "session" to (c.session?.sessionId ?: ""), "relayed" to Relay.active)
            setState(State.CONNECTED, "Connected to ${pc.name}" + if (Relay.active) " (through the relay)" else "")
            onConnected()
            try {
                while (running && !c.closed) {
                    val env = c.recv()
                    downSince = null  // the PC is really talking to us (not busy/unpaired)
                    pcBusy = false
                    dispatch(c, env)
                }
            } catch (e: BusyException) {
                c.close()
                conn = null
                waitWhileBusy(pc, e)
                continue
            } catch (e: UnpairedException) {
                c.close()
                conn = null
                stopForgotten(pc)
                return
            } catch (e: Exception) {
                Log.i(TAG, "connection lost: $e")
            } finally {
                PhoneAudit.log(this, "session.end", "session" to (c.session?.sessionId ?: ""),
                    "how" to (if (c.peerEnded) "peer" else "abrupt"))
                c.close()
                conn = null
                infoTask?.cancel(false)
                pingTask?.cancel(false)
                syncTask?.cancel(false)
                video.stopAll()
                screenSync.onLinkDown()   // restores the display first: the PC may be gone
            }
            if (downSince == null) downSince = System.currentTimeMillis()
            if (running) setState(State.DISCONNECTED, "Disconnected from ${pc.name}, retrying…")
            sleepUntilWoken(1500)
        }
    }

    /** Ends the service and its notification; opening the app (Connect to PC) starts it again. */
    private fun stopQuietly(reason: String) {
        Log.i(TAG, "stopping: $reason")
        lastStopReason = reason
        stateDetail = reason
        state = State.DISCONNECTED
        running = false
        main.post {
            stateListener?.invoke()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    /** null = no route reached the PC. Throws [BusyException] / [UnpairedException] when the PC answered with a refusal. */
    private fun tryOpen(pc: PairedPc): Connection? {
        Relay.active = false
        // USB first: if the PC set up `adb reverse`, localhost reaches it through the cable (nothing on any network).
        // With no tunnel this is refused instantly and costs nothing.
        tryRoute { Connection.open(pc, host = Connection.USB_HOST, port = pc.lanPort) }?.let {
            Log.i(TAG, "connected over USB")
            return it
        }
        tryRoute({ e ->
            Log.i(TAG, "direct connect to ${pc.lanIp} failed: $e")
            if (e is SecureSession.HandshakeException) PhoneAudit.log(this, "auth.failed", "reason" to (e.message ?: "handshake"))
        }) { Connection.open(pc) }?.let { return it }
        val found = Mdns.find(this, pc) ?: Connection.discover(pc)
        if (found != null) {
            tryRoute({ e -> Log.i(TAG, "connect to discovered ${found.first} failed: $e") }) {
                Connection.open(pc, host = found.first, port = found.second)
            }?.let {
                Store.updateLan(this, found.first, found.second)
                return it
            }
        }
        // Not on the PC's network: meet it at the relay, if the PC gave us one.
        if (Relay.host != null) {
            Relay.active = true
            tryRoute({ e ->
                Log.i(TAG, "relay connect failed: $e")
                Relay.active = false
            }) { Connection.open(pc) }?.let { return it }
        }
        return null
    }

    /** The PC is serving another phone: say so and look again a little later. */
    private fun waitWhileBusy(pc: PairedPc, e: BusyException) {
        pcBusy = true
        setState(State.DISCONNECTED, "${pc.name} is connected to another phone (${e.otherPhone}). " +
            "Trying again for up to 2 minutes.")
        sleepUntilWoken(20_000)
    }

    /** The PC's last answer was "busy" (connection thread only). */
    private var pcBusy = false

    /** The PC no longer knows this phone: retrying cannot help, so stop and ask for a new pairing. */
    private fun stopForgotten(pc: PairedPc) =
        stopQuietly("${pc.name} forgot this phone. Tap \"Scan QR code on PC\" to pair again.")

    private fun sleepUntilWoken(ms: Long) {
        synchronized(wakeSignal) { runCatching { wakeSignal.wait(ms) } }
    }

    private var wakeHolds = 0

    /** Keeps the CPU awake for a transfer/stream. Always pair with [releaseWake]. */
    fun holdWake() = synchronized(this) {
        if (wakeHolds++ == 0) wakeLock?.acquire(60 * 60 * 1000L)  // 1 h safety timeout
    }

    fun releaseWake() = synchronized(this) {
        if (wakeHolds > 0 && --wakeHolds == 0) runCatching { wakeLock?.release() }
    }

    fun wake() = synchronized(wakeSignal) { wakeSignal.notifyAll() }

    private fun onConnected() {
        infoTask = scheduler.scheduleWithFixedDelay({ sendDeviceInfo() }, 0, 30, TimeUnit.SECONDS)
        pingTask = scheduler.scheduleWithFixedDelay({ send("ping") }, 30, 30, TimeUnit.SECONDS)
        syncTask = scheduler.scheduleWithFixedDelay({ runCatching { screenSync.tick() } }, 5, 5, TimeUnit.SECONDS)
        screenSync.onLinkUp()
        worker.execute { NotificationRelay.instance?.sendAll() }
        worker.execute { Clipboard.sendCurrentIfReadable(this) }
        worker.execute {
            send("screen.state", ScreenControl.state(this))
            send("volume.state", ScreenControl.volumeState(this))
        }
    }

    fun isConnected() = conn != null

    /** Fire-and-forget send; silently dropped when not connected. Safe from any thread. */
    /** A rotated identity key waiting for the PC's acknowledgement (the old key keeps working meanwhile). */
    @Volatile private var pendingIdentity: SecureSession.Identity? = null

    /** Starts rotating this phone's Ed25519 identity: the old key signs for the new one; the switch happens on the PC's ack. */
    fun rotateIdentity(): Boolean {
        val pc = Store.load(this) ?: return false
        val old = pc.identity ?: return false
        val fresh = SecureSession.Identity.generate()
        pendingIdentity = fresh
        send("identity.rotate", JSONObject().put("new_pub", fresh.pubB64)
            .put("sig", SecureSession.signRotation(old, pc.fingerprint, fresh.pubB64)))
        return true
    }

    fun send(type: String, payload: JSONObject = JSONObject()) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            // Android forbids network I/O on the main thread (NetworkOnMainThreadException),
            // and callbacks like camera/projection/call-state all arrive there.
            worker.execute { send(type, payload) }
            return
        }
        val c = conn ?: return
        try {
            c.send(type, payload)
        } catch (e: Exception) {
            Log.i(TAG, "send $type failed: $e")
            c.close()
        }
    }

    fun connection(): Connection? = conn

    private val progressSent = HashMap<String, Pair<Long, Int>>()

    /**
     * `load.progress` for the PC's loading bars: at most ~6 per second per `what`, always
     * including 0% and 100% (PROTOCOL.md section 4b).
     */
    fun progress(what: String, done: Int, total: Int) {
        if (total <= 0) return
        val pct = (done.toLong() * 100 / total).toInt().coerceIn(0, 100)
        val now = System.currentTimeMillis()
        synchronized(progressSent) {
            val last = progressSent[what]
            val due = last == null || pct == 100 || (pct != last.second && now - last.first >= 150)
            if (!due) return
            progressSent[what] = now to pct
        }
        send("load.progress", JSONObject().put("what", what).put("done", done).put("total", total))
    }

    fun sendDeviceInfo() = worker.execute { send("device.info", DeviceInfo.collect(this)) }

    // ---------------------------------------------------------------- dispatch

    private val askPool = java.util.concurrent.Executors.newCachedThreadPool()
    private val askAnswers = PcAccess.SessionAnswers()
    private val askWaiting = PcAccess.Waiting<JSONObject>()

    /** Zero-trust gate: a paired PC may only do what the permission manager (PcAccess) allows for each feature. */
    private fun dispatch(c: Connection, env: JSONObject) {
        val type = env.optString("type")
        val p = env.optJSONObject("payload") ?: JSONObject()
        val feature = PcAccess.featureFor(type)
        if (feature == null) { dispatchAllowed(c, env); return }
        when (PcAccess.mode(this, feature)) {
            PcAccess.Mode.ALLOW -> dispatchAllowed(c, env)
            PcAccess.Mode.DENY -> deny(feature, env)
            PcAccess.Mode.ASK -> {
                val sid = c.session?.sessionId ?: ""
                val each = feature in PcAccess.ASK_EVERY_TIME
                val key = "$sid/$feature"
                // A prompt is already open for this connection: don't pile up prompts. Reads wait for its answer.
                if (!each && PcAccess.canWait(type) && askWaiting.hold(key, env)) return
                val earlier = askAnswers.get(sid, feature)
                when {
                    earlier == true -> dispatchAllowed(c, env)
                    earlier == false -> deny(feature, env)
                    !each && (askWaiting.isOpen(key) || PcAccess.isPending(feature)) -> deny(feature, env)
                    else -> {
                        if (!each) askWaiting.open(key)
                        askPool.execute {
                            // A prompt that can't be shown counts as no answer (the waiting requests must still be released).
                            val answer = runCatching { PcAccess.ask(this, feature, PcAccess.describe(type, p)) }.getOrNull()
                            askAnswers.record(sid, feature, answer)
                            val requests = listOf(env) + (if (each) emptyList() else askWaiting.close(key))
                            for (request in requests) {
                                if (answer == true) runApproved(c, request, feature, each) else deny(feature, request)
                            }
                        }
                    }
                }
            }
        }
    }

    /** The user said yes, possibly long after the request: it is only carried out if the connection that asked is still
     *  the live one (not lost, replaced or ended by "Disconnect now") and the feature wasn't switched off meanwhile. */
    private fun runApproved(c: Connection, env: JSONObject, feature: String, once: Boolean) {
        if (c !== conn || c.closed || PcAccess.mode(this, feature) == PcAccess.Mode.DENY) {
            PhoneAudit.log(this, "access.denied", "feature" to feature, "message" to env.optString("type"), "reason" to "expired")
            send("error", JSONObject().put("message", "The phone's approval came too late, so nothing was done. Try again."))
            return
        }
        PhoneAudit.log(this, "access.granted", "feature" to feature, "once" to once)
        dispatchAllowed(c, env)
    }

    private fun deny(feature: String, env: JSONObject) {
        val type = env.optString("type")
        PhoneAudit.log(this, "access.denied", "feature" to feature, "message" to type)
        send("permission.denied", PcAccess.denial(feature, type, env.optJSONObject("payload") ?: JSONObject()))
    }

    /** Emergency stop from the Security tab: end the session now and stay off until the user taps Connect again. */
    fun emergencyDisconnect() {
        PhoneAudit.log(this, "session.disconnect_all")
        Store.setPaused(this, true)
        worker.execute { conn?.endSession("emergency") }
        wake()
    }

    private fun dispatchAllowed(c: Connection, env: JSONObject) {
        val type = env.optString("type")
        val p = env.optJSONObject("payload") ?: JSONObject()
        when (type) {
            "file.offer" -> Transfer.pool.execute {
                runCatching { Files.receive(this, p) }.onFailure { Log.w(TAG, "file receive failed", it) }
            }
            "hotspot.request" -> main.post { runCatching { Hotspot.handle(this, p) } }
            "file.cancel" -> Transfer.cancel(p.optString("transfer_id"))
            "file.accept" -> Transfer.onAnswer(p, rejected = false)
            "file.reject" -> Transfer.onAnswer(p, rejected = true)
            "file.done", "pong" -> {}
            // The PC's permissions refused something sent to it. A refused file offer gets no file.reject.
            "permission.denied" -> if (p.optString("type") == "file.offer") {
                Transfer.onAnswer(p.put("reason", "the PC doesn't allow it (PhoneBridge on the PC, Security tab)"), rejected = true)
            }
            "session.end" -> {
                // The PC closed this session on purpose (authenticated, unlike a bare TCP close): it was not truncated.
                c.peerEnded = true
                Log.i(TAG, "PC ended the session: ${p.optString("reason")}")
                c.close()
            }
            "ping" -> send("pong")
            "link.disconnect" -> {
                // The PC asked us to step aside (e.g. to let another phone connect).
                Store.setPaused(this, true)
                c.close()
            }

            "identity.rotate" -> {
                // The PC announces a new identity key, vouched for by the one we already trust (PROTOCOL.md 1h).
                val pc = Store.load(this)
                val newPub = p.optString("new_pub")
                if (pc != null && SecureSession.verifyRotation(pc.pcIdentityPub, pc.fingerprint, newPub, p.optString("sig"))) {
                    Store.setPcIdentity(this, newPub)
                    PhoneAudit.log(this, "identity.rotated", "who" to "pc", "new_fp" to SecureSession.fingerprint(newPub))
                    send("identity.rotated", JSONObject().put("new_pub", newPub))
                } else {
                    Log.w(TAG, "ignored an identity rotation that the trusted PC key did not sign")
                }
            }
            "identity.rotated" -> {
                // The PC accepted our new identity key.
                val pending = pendingIdentity
                if (pending != null && pending.pubB64 == p.optString("new_pub")) {
                    Store.setIdentity(this, pending)
                    PhoneAudit.log(this, "identity.rotated", "who" to "phone", "new_fp" to SecureSession.fingerprint(pending.pubB64))
                    pendingIdentity = null
                }
            }
            "device.info_request" -> sendDeviceInfo()
            "notifications.sync" -> worker.execute { NotificationRelay.instance?.sendAll() ?: send("notifications.unavailable") }
            "notification.action" -> NotificationRelay.instance?.handleAction(p)
            "clipboard.update" -> {
                if (p.has("image")) main.post { Clipboard.setImageFromPc(this, p.optString("image")) }
                else setClipboardFromPc(p.optString("text"))
                main.post { pcListener?.invoke(type, p) }
            }
            // A frame of the PC's screen: the JPEG follows as one raw frame (read it now).
            "pc.screen_frame" -> {
                val jpeg = c.recvRaw()
                val listener = pcFrameListener ?: return
                android.graphics.BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)?.let { bmp -> main.post { listener(bmp) } }
            }
            "pc.info", "pc.ports", "pc.servers", "pc.apps", "pc.files", "pc.status", "pc.remote_status", "pc.screen_started",
            "pc.term_output", "pc.term_exit" ->
                main.post { pcListener?.invoke(type, p) }

            "sms.list_threads" -> bg {
                send("sms.thread_list", Sms.threads(this, p.optInt("offset", 0), p.optInt("limit", 20)) { d, t -> progress("sms.threads", d, t) })
            }
            "sms.get_thread" -> bg {
                send("sms.thread_messages", Sms.thread(this, p.optString("thread_id"), p.optInt("offset", 0), p.optInt("limit", 20)) { d, t ->
                    progress("sms.thread", d, t)
                })
            }
            "relay.config" -> Relay.save(this, p.optString("host"), p.optInt("port", 28200))
            "sms.send" -> bg { Sms.send(this, p.optString("number"), p.optString("text")) }
            "sms.send_mms" -> bg {
                val to = p.optJSONArray("numbers")?.let { arr -> (0 until arr.length()).map { arr.getString(it) } }
                    ?: listOf(p.optString("number"))
                Mms.send(this, p.optString("request_id"), to, p.optString("text"), p.optJSONArray("attachments") ?: org.json.JSONArray())
            }

            "calls.history_request" -> bg {
                send("calls.history", calls.history(p.optInt("offset", 0), p.optInt("limit", 20)) { d, t -> progress("calls.history", d, t) })
            }
            "contacts.list_request" -> bg {
                send("contacts.list", calls.contacts(p.optInt("offset", 0), p.optInt("limit", 50)) { d, t -> progress("contacts", d, t) })
            }
            "call.dial" -> main.post { calls.dial(p.optString("number")) }
            "call.action" -> main.post { calls.action(p.optString("action")) }

            "photos.list" -> bg {
                send("photos.list_result", Photos.list(this, p.optInt("offset", 0), p.optInt("limit", 60)) { d, t -> progress("photos", d, t) })
            }
            "photos.get" -> bgLong { Photos.sendPhoto(this, p.optString("id")) }
            "files.list" -> bg { send("files.list_result", Files.list(p.optString("path", "")) { d, t -> progress("files", d, t) }) }
            "files.get" -> bgLong { Files.sendFile(this, p.optString("path")) }

            "apps.list_request" -> bg { send("apps.list", Apps.list(this) { d, t -> progress("apps", d, t) }) }
            "apps.launch" -> main.post { Apps.launch(this, p.optString("pkg")) }

            "mirror.start" -> main.post {
                val app = p.optString("app")
                if (app.isNotEmpty()) video.requestAppStream(app) else video.requestMirror()
            }
            "mirror.stop" -> main.post { video.stopMirror() }
            "mirror.input" -> when (if (p.optString("kind") == "key") p.optString("key") else "") {
                // These work without the accessibility service (except lock).
                "wake", "unlock" -> main.post { ScreenControl.wakeAndUnlock(this) }
                "lock" -> main.post { ScreenControl.lock(this) }
                "volume_up", "volume_down", "volume_mute" -> main.post { ScreenControl.volume(this, p.optString("key")) }
                "copy" -> InputService.instance?.copySelection() ?: send("mirror.status", JSONObject().put("state", "no_input")
                    .put("message", "Turn on PhoneBridge remote control in the phone's Accessibility settings to copy from it"))
                else -> InputService.handle(p)
            }
            // Screen-sync session commands (validated, de-duplicated and session-bound inside ScreenSync).
            "sync.command", "sync.ack" -> screenSync.onEnvelope(type, p)
            "screen.state_request" -> bg {
                send("screen.state", ScreenControl.state(this))
                send("volume.state", ScreenControl.volumeState(this))
            }
            "webcam.start" -> main.post { video.startWebcam(p.optString("facing", "back")) }
            "webcam.switch" -> main.post { video.switchWebcam(p.optString("facing", "")) }
            "webcam.stop" -> main.post { video.stopWebcam() }
            else -> Log.i(TAG, "unhandled message type $type")
        }
    }

    private fun bgLong(block: () -> Unit) = bg(Transfer.pool, block)

    private fun bg(block: () -> Unit) = bg(worker, block)

    private fun bg(pool: java.util.concurrent.ExecutorService, block: () -> Unit) = pool.execute {
        try {
            block()
        } catch (e: SecurityException) {
            send("error", JSONObject().put("message", "Permission missing on phone: ${e.message}"))
        } catch (e: Exception) {
            Log.w(TAG, "handler failed", e)
            send("error", JSONObject().put("message", e.toString()))
        }
    }

    private fun setClipboardFromPc(text: String) = main.post {
        if (!Clipboard.allowed(this)) return@post
        lastClipText = text
        (getSystemService(CLIPBOARD_SERVICE) as ClipboardManager)
            .setPrimaryClip(ClipData.newPlainText(Clipboard.SYNCED_LABEL, text))
        Clipboard.scheduleClear(this)   // synced text doesn't linger on the clipboard
    }

    // ---------------------------------------------------------------- receivers

    private val batteryReceiver = object : BroadcastReceiver() {
        private var lastPct = -1
        private var lastPlugged = -1
        override fun onReceive(ctx: Context, intent: Intent) {
            val level = intent.getIntExtra("level", -1)
            val plugged = intent.getIntExtra("plugged", 0)
            if (level != lastPct || plugged != lastPlugged) {
                lastPct = level; lastPlugged = plugged
                if (isConnected()) sendDeviceInfo()
            }
        }
    }

    private val smsReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            val msgs = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
            if (msgs.isEmpty()) return
            val address = msgs[0].displayOriginatingAddress ?: ""
            val body = msgs.joinToString("") { it.displayMessageBody ?: "" }
            val ts = msgs[0].timestampMillis
            worker.execute { send("sms.new", Sms.incoming(this@BridgeService, address, body, ts)) }
        }
    }

    /** A picture/group message arrived or was stored: refresh that conversation on the PC. */
    private val mmsObserver = object : android.database.ContentObserver(Handler(Looper.getMainLooper())) {
        private val fire = Runnable { worker.execute { runCatching { announceNewestMms() } } }
        override fun onChange(selfChange: Boolean) {
            main.removeCallbacks(fire)
            main.postDelayed(fire, 2500)  // the MMS arrives in several writes; wait for them to settle
        }
    }

    private fun announceNewestMms() {
        if (!isConnected()) return
        contentResolver.query(android.provider.Telephony.Mms.CONTENT_URI,
            arrayOf(android.provider.Telephony.Mms._ID, android.provider.Telephony.Mms.THREAD_ID, android.provider.Telephony.Mms.MESSAGE_BOX),
            null, null, "${android.provider.Telephony.Mms.DATE} DESC LIMIT 1")?.use { c ->
            if (!c.moveToFirst()) return
            val tid = c.getString(1) ?: return
            val incoming = c.getInt(2) == android.provider.Telephony.Mms.MESSAGE_BOX_INBOX
            val address = Sms.mmsAddress(this, c.getString(0), incoming)
            send("sms.sent", JSONObject().put("thread_id", tid).put("address", address))
            send("sms.thread_messages", Sms.thread(this, tid, 0, 20))
        }
    }

    /** Hotspot switched on/off: the PC's address changes, so look for it again right away. */
    private val hotspotReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            val state = intent.getIntExtra("wifi_state", -1)
            if (state == 13 /* AP enabled */ || state == 11 /* AP disabled */) {
                main.postDelayed({ dropStaleLink("hotspot ${if (state == 13) "on" else "off"}") }, 2000)
            }
        }
    }

    private val netCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            if (conn == null) wake()
        }

        override fun onLost(network: Network) = dropStaleLink("network lost")

        override fun onLinkPropertiesChanged(network: Network, lp: android.net.LinkProperties) {
            val addrs = lp.linkAddresses.map { it.address.hostAddress ?: "" }.toSet()
            val previous = networkAddrs
            networkAddrs = addrs
            // Moved to another Wi-Fi / got a new IP: the old TCP link is silently dead.
            if (previous.isNotEmpty() && previous != addrs) dropStaleLink("network changed")
        }
    }

    private fun dropStaleLink(why: String) {
        val c = conn ?: run { wake(); return }
        Log.i(TAG, "dropping connection: $why")
        worker.execute { c.close() }
        wake()
    }

    @SuppressLint("MissingPermission")
    private fun registerReceivers() {
        registerReceiver(batteryReceiver, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        registerReceiver(smsReceiver, IntentFilter(Telephony.Sms.Intents.SMS_RECEIVED_ACTION))
        registerReceiver(hotspotReceiver, IntentFilter(Net.ACTION_AP_STATE_CHANGED))
        runCatching { contentResolver.registerContentObserver(android.net.Uri.parse("content://mms"), true, mmsObserver) }
        runCatching {
            (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).registerDefaultNetworkCallback(netCallback)
        }
    }

    // ---------------------------------------------------------------- notification / state

    private fun setState(s: State, detail: String) {
        state = s
        stateDetail = detail
        if (s == State.CONNECTED) BleWake.stop(this) else if (s == State.DISCONNECTED) BleWake.start(this)
        main.post {
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(NOTIF_ID, buildNotification())
            stateListener?.invoke()
        }
    }

    private fun createChannels() {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(NotificationChannel(CHANNEL_STATUS, "Connection status", NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CHANNEL_ALERTS, "PC requests", NotificationManager.IMPORTANCE_HIGH))
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val clip = PendingIntent.getActivity(
            this, 1, Intent(this, ClipboardSendActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_STATUS)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(if (state == State.CONNECTED) "Linked to PC" else "PhoneBridge")
            .setContentText(stateDetail.ifEmpty { "Starting…" })
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Send clipboard to PC", clip).build())
            .build()
    }

    /** Heads-up notification the user taps when Android blocks us from opening a screen directly. */
    fun alert(id: Int, title: String, text: String, intent: Intent) {
        val pi = PendingIntent.getActivity(this, id, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(this, CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(title)
            .setContentText(text)
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(id, n)
    }

    companion object {
        const val TAG = "PhoneBridge"
        const val NOTIF_ID = 1
        const val CHANNEL_STATUS = "status"
        const val CHANNEL_ALERTS = "alerts"
        const val ACTION_RECONNECT = "dev.phonebridge.RECONNECT"
        const val ACTION_UNPAIR = "dev.phonebridge.UNPAIR"
        const val ACTION_CONNECT = "dev.phonebridge.CONNECT"

        @Volatile var instance: BridgeService? = null
        /** The phone app's PC-control tabs listen here (main thread). */
        @Volatile var pcListener: ((String, JSONObject) -> Unit)? = null
        @Volatile var pcFrameListener: ((android.graphics.Bitmap) -> Unit)? = null
        /** Why the service last stopped itself, shown on the main screen. */
        @Volatile var lastStopReason: String? = null
        private const val GIVE_UP_MS = 2 * 60_000L
        var stateListener: (() -> Unit)? = null

        fun start(ctx: Context, action: String? = null) {
            val i = Intent(ctx, BridgeService::class.java).setAction(action)
            ctx.startForegroundService(i)   // minSdk is 26
        }
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        // Only the two system broadcasts this receiver is registered for; anything else (an app sending us a look-alike
        // intent) is ignored.
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        if (Store.load(ctx) != null) BridgeService.start(ctx)
    }
}
