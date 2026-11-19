package dev.phonebridge

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.DisplayMetrics
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.Surface
import android.view.WindowManager
import org.json.JSONObject

/**
 * H.264 encoder whose output goes over a dedicated video connection to the PC
 * (PROTOCOL.md section 1b): one encrypted raw frame per access unit. SPS/PPS are
 * re-sent in front of every keyframe so the PC decoder can join at any point.
 */
class H264Stream(private val pc: PairedPc, val width: Int, val height: Int, bitrate: Int, staticContent: Boolean) {
    // --- bitrate adaptation: a slow socket write means the link (or the PC) can't keep up.
    private val maxBitrate = bitrate
    private val minBitrate = maxOf(300_000, bitrate / 12)
    @Volatile var currentBitrate = bitrate
        private set
    private var slowFrames = 0
    private var fastFrames = 0
    private var lastChange = 0L

    private val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
    val inputSurface: Surface
    @Volatile private var running = true
    private var conn: Connection? = null
    private var udp: UdpVideoSender? = null
    private var goodReports = 0
    private var config: ByteArray? = null
    private val thread: Thread

    init {
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, 30)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
            // Baseline keeps every PC-side decoder (incl. openh264) happy.
            setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline)
            setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel31)
            // A still screen produces no new frames; repeat so the PC view doesn't stall.
            if (staticContent) setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 100_000L)
        }
        try {
            codec.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        } catch (e: Exception) {
            // Some encoders reject the explicit profile/level — retry without them.
            if (android.os.Build.VERSION.SDK_INT >= 29) {
                fmt.removeKey(MediaFormat.KEY_PROFILE)
                fmt.removeKey(MediaFormat.KEY_LEVEL)
            }
            codec.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        }
        inputSurface = codec.createInputSurface()
        codec.start()
        thread = Thread(::drain, "pb-h264")
        thread.start()
    }

    private fun drain() {
        try {
            // UDP first (PROTOCOL.md 1f); the TLS video connection (1b) if the PC doesn't answer.
            udp = if (Relay.active) null  // UDP can't go through the relay; TCP video does
                  else UdpVideoSender.tryOpen(pc, onKeyframeRequest = ::requestKeyframe, onReport = ::onLossReport)
            if (udp == null) conn = Connection.open(pc, video = true)
            Log.i(BridgeService.TAG, "video transport: ${if (udp != null) "udp" else "tcp"}")
            val info = MediaCodec.BufferInfo()
            while (running) {
                val idx = codec.dequeueOutputBuffer(info, 100_000)
                if (idx < 0) continue
                val buf = codec.getOutputBuffer(idx)!!
                val data = ByteArray(info.size)
                buf.position(info.offset)
                buf.get(data, 0, info.size)
                codec.releaseOutputBuffer(idx, false)
                val t0 = System.nanoTime()
                var sent = true
                when {
                    info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0 -> { config = data; sent = false }
                    info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0 -> emit((config ?: ByteArray(0)) + data, true)
                    data.isNotEmpty() -> emit(data, false)
                    else -> sent = false
                }
                // UDP sends never block on the link, so adaptation there is driven by the PC's loss reports.
                if (sent && udp == null) adapt((System.nanoTime() - t0) / 1_000_000, data.size)
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) break
            }
        } catch (e: Exception) {
            if (running) Log.i(BridgeService.TAG, "video stream ended: $e")
            if (running) BridgeService.instance?.main?.post { BridgeService.instance?.video?.onStreamFailed(this) }
        } finally {
            conn?.close()
            udp?.close()
        }
    }

    private fun emit(data: ByteArray, keyframe: Boolean) {
        val u = udp
        if (u != null) u.sendFrame(data, keyframe) else conn!!.sendRaw(data)
    }

    /** PC's once-a-second receive report: back off fast on loss, creep back up after a clean stretch. */
    private fun onLossReport(delivered: Int, lost: Int) {
        val total = delivered + lost
        if (total == 0) return
        val now = System.currentTimeMillis()
        if (lost * 20 > total) { // > 5 % of frames lost
            goodReports = 0
            if (now - lastChange > 400) setBitrate(maxOf(minBitrate, (currentBitrate * 0.7).toInt()), now)
        } else if (lost == 0 && ++goodReports >= 5 && now - lastChange > 4000 && currentBitrate < maxBitrate) {
            goodReports = 0
            setBitrate(minOf(maxBitrate, (currentBitrate * 1.15).toInt()), now)
        }
    }

    private fun setBitrate(target: Int, now: Long) {
        currentBitrate = target
        lastChange = now
        runCatching {
            codec.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, target) })
        }
        if (target < maxBitrate / 2) requestKeyframe()
        Log.i(BridgeService.TAG, "video bitrate -> ${target / 1000} kbps")
    }

    /**
     * Called per frame with how long the socket write blocked. A write that takes longer than
     * the time the frame "should" need at the current bitrate means the TCP send buffer is
     * full: back off quickly (x0.7) and ask for a keyframe so the PC can resync; creep back up
     * (x1.15) after a few hundred consistently fast frames.
     */
    private fun adapt(writeMs: Long, bytes: Int) {
        val now = System.currentTimeMillis()
        if (writeMs > 40 || (bytes > 20_000 && writeMs > 25)) { slowFrames++; fastFrames = 0 }
        else if (writeMs < 8) { fastFrames++; slowFrames = maxOf(0, slowFrames - 1) }
        var target = currentBitrate
        if (slowFrames >= 3 && now - lastChange > 400) {
            target = maxOf(minBitrate, (currentBitrate * 0.7).toInt()); slowFrames = 0
        } else if (fastFrames >= 240 && now - lastChange > 4000 && currentBitrate < maxBitrate) {
            target = minOf(maxBitrate, (currentBitrate * 1.15).toInt()); fastFrames = 0
        }
        if (target != currentBitrate) setBitrate(target, now)
    }

    fun requestKeyframe() = runCatching {
        codec.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
    }

    fun stop() {
        running = false
        conn?.close()
        udp?.close()
        runCatching { codec.signalEndOfInputStream() }
        runCatching { thread.join(500) }
        runCatching { codec.stop() }
        runCatching { codec.release() }
        runCatching { inputSurface.release() }
    }
}

class VideoManager(private val svc: BridgeService) {
    private var mirrorStream: H264Stream? = null
    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var projectionResult: Pair<Int, Intent>? = null
    /** A consent prompt is on screen: a second request (e.g. sync START_MIRRORING + mirror.start) must not stack another. */
    @Volatile private var consentPending = false
    /** Bumped for every consent request; a result from an older prompt (stale activity) is ignored. */
    @Volatile var consentGen = 0
        private set

    private var camStream: H264Stream? = null
    private var camera: CameraDevice? = null
    private var camSession: CameraCaptureSession? = null
    private var camFacing = "back"
    private val camThread = HandlerThread("pb-camera").apply { start() }
    private val camHandler = Handler(camThread.looper)

    // ------------------------------------------------------------- screen mirroring

    // Held only while Android's capture prompt is waiting for an answer. A locked or sleeping phone otherwise goes back to
    // sleep 10 s after the lock screen wakes (keyguard timeout), which silently cancels the prompt before anyone can unlock.
    private var consentWake: android.os.PowerManager.WakeLock? = null

    @Suppress("DEPRECATION")
    private fun holdConsentWake() {
        releaseConsentWake()
        consentWake = runCatching {
            (svc.getSystemService(Context.POWER_SERVICE) as android.os.PowerManager).newWakeLock(
                android.os.PowerManager.SCREEN_BRIGHT_WAKE_LOCK or android.os.PowerManager.ACQUIRE_CAUSES_WAKEUP, "phonebridge:consent",
            ).apply { setReferenceCounted(false); acquire(60_000) }
        }.getOrNull()
    }

    private fun releaseConsentWake() {
        runCatching { if (consentWake?.isHeld == true) consentWake?.release() }
        consentWake = null
    }

    fun requestMirror() {
        stopWebcam()
        if (mirrorStream != null || consentPending) return
        // A grant is never reused: once stopped, a projection is dead, and on current Android a reused grant yields a
        // virtual display that sends no frames at all (seen on Android 17) while also skipping the user's consent.
        // Every capture start shows Android's own prompt.
        projectionResult = null
        consentGen++
        // CLEAR_TASK: an unanswered prompt from an earlier request must not be reused (its result would be reported as "declined").
        val intent = Intent(svc, ScreenCaptureActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        consentPending = true
        svc.main.postDelayed({ consentPending = false; releaseConsentWake() }, 90_000)   // never wedge if the prompt is dismissed silently
        holdConsentWake()
        if (android.provider.Settings.canDrawOverlays(svc)) {
            svc.startActivity(intent)
            svc.send("mirror.status", JSONObject().put("state", "waiting_consent")
                .put("message", "Tap \"Start now\" on your phone to allow screen sharing"))
        } else {
            svc.alert(3000, "Screen mirroring requested", "Tap to allow your PC to see this screen", intent)
            svc.send("mirror.status", JSONObject().put("state", "waiting_consent")
                .put("message", "Tap the PhoneBridge notification on your phone to allow screen sharing"))
        }
    }

    /**
     * Streams ONE app: it runs on a private virtual display (not shown on the phone's own screen)
     * whose output feeds the encoder. No screen-capture consent is needed. Best effort — the app
     * must allow being resized, and the phone needs "Display over other apps" to start it.
     */
    @Volatile var appDisplayId = -1
        private set

    fun requestAppStream(pkg: String) {
        stopWebcam()
        stopMirror()
        fun fail(msg: String) = svc.send("mirror.status", JSONObject().put("state", "error").put("message", msg))
        val pc = Store.load(svc) ?: return
        val launch = svc.packageManager.getLaunchIntentForPackage(pkg) ?: return fail("That app can't be opened")
        if (!android.provider.Settings.canDrawOverlays(svc)) {
            return fail("Allow \"Display over other apps\" for PhoneBridge on the phone so it can start the app")
        }
        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        (svc.getSystemService(Context.WINDOW_SERVICE) as WindowManager).defaultDisplay.getRealMetrics(metrics)
        val scale = minOf(1f, 1280f / maxOf(metrics.widthPixels, metrics.heightPixels))
        val w = ((metrics.widthPixels * scale).toInt() / 16) * 16
        val h = ((metrics.heightPixels * scale).toInt() / 16) * 16
        val stream = try {
            H264Stream(pc, w, h, 5_000_000, staticContent = true)
        } catch (e: Exception) {
            return fail("Encoder failed: $e")
        }
        try {
            val dm = svc.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
            // Scaling density with size keeps the app's layout identical to the phone's.
            val display = dm.createVirtualDisplay("PhoneBridge app", w, h, (metrics.densityDpi * scale).toInt().coerceAtLeast(120),
                stream.inputSurface,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC or DisplayManager.VIRTUAL_DISPLAY_FLAG_OWN_CONTENT_ONLY)
                ?: throw IllegalStateException("the phone refused a virtual display")
            virtualDisplay = display
            mirrorStream = stream
            appDisplayId = display.display.displayId
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_MULTIPLE_TASK)
            svc.startActivity(launch, android.app.ActivityOptions.makeBasic().setLaunchDisplayId(appDisplayId).toBundle())
        } catch (e: Exception) {
            Log.w(BridgeService.TAG, "app stream failed", e)
            mirrorStream = stream  // so stopMirror() releases everything it created
            stopMirror()
            return fail("Couldn't stream that app: ${e.message ?: e.javaClass.simpleName}")
        }
        svc.holdWake()
        svc.send("video.started", JSONObject().put("kind", "mirror").put("app", pkg).put("width", w).put("height", h)
            .put("rotation", 0).put("input_available", InputService.instance != null))
        svc.screenSync.onVideoStarted(w, h)
        svc.main.postDelayed({ mirrorStream?.requestKeyframe() }, 800)
    }

    fun onProjectionResult(code: Int, data: Intent?) {
        consentPending = false
        releaseConsentWake()
        Log.i(BridgeService.TAG, "screen-capture consent result code=$code (RESULT_OK=${Activity.RESULT_OK})")
        if (code != Activity.RESULT_OK || data == null) {
            svc.send("mirror.status", JSONObject().put("state", "denied").put("message", "Screen sharing was declined on the phone"))
            svc.screenSync.onMirrorDenied("Screen sharing was declined on the phone")
            return
        }
        projectionResult = code to data
        try {
            startMirror(code, data)
        } catch (e: Exception) {
            Log.w(BridgeService.TAG, "mirror start failed", e)
            svc.send("mirror.status", JSONObject().put("state", "error").put("message", "Couldn't start mirroring: $e"))
            svc.screenSync.onMirrorDenied("Couldn't start mirroring: $e")
        }
    }

    private fun startMirror(code: Int, data: Intent) {
        val pc = Store.load(svc) ?: return
        val mpm = svc.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val proj = mpm.getMediaProjection(code, Intent(data)) ?: throw IllegalStateException("projection refused")
        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        (svc.getSystemService(Context.WINDOW_SERVICE) as WindowManager).defaultDisplay.getRealMetrics(metrics)
        val scale = minOf(1f, 1280f / maxOf(metrics.widthPixels, metrics.heightPixels))
        val w = ((metrics.widthPixels * scale).toInt() / 16) * 16
        val h = ((metrics.heightPixels * scale).toInt() / 16) * 16
        val stream = H264Stream(pc, w, h, 5_000_000, staticContent = true)
        proj.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { svc.main.post { stopMirror() } }
        }, svc.main)
        virtualDisplay = proj.createVirtualDisplay("PhoneBridge", w, h, metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, stream.inputSurface, null, null)
        projection = proj
        mirrorStream = stream
        svc.holdWake()
        svc.send("video.started", JSONObject().put("kind", "mirror").put("width", w).put("height", h).put("rotation", 0)
            .put("input_available", InputService.instance != null))
        svc.screenSync.onVideoStarted(w, h)
        svc.main.postDelayed({ mirrorStream?.requestKeyframe() }, 500)
    }

    fun stopMirror() {
        releaseConsentWake()   // a cancelled request must not keep the screen on
        val wasRunning = mirrorStream != null
        if (wasRunning) svc.releaseWake()
        virtualDisplay?.release(); virtualDisplay = null
        appDisplayId = -1
        mirrorStream?.stop(); mirrorStream = null
        projection?.let { p -> projection = null; runCatching { p.stop() } }
        if (wasRunning) svc.send("video.stopped", JSONObject().put("kind", "mirror"))
        if (wasRunning) svc.screenSync.onVideoStopped()   // always restores the display
    }

    // ------------------------------------------------------------- webcam

    fun startWebcam(facing: String) {
        stopMirror()
        stopWebcam()
        camFacing = if (facing == "front") "front" else "back"
        openCamera()
    }

    fun switchWebcam(facing: String) {
        val next = when (facing) {
            "front", "back" -> facing
            else -> if (camFacing == "front") "back" else "front"
        }
        startWebcam(next)
    }

    @SuppressLint("MissingPermission")
    private fun openCamera() {
        val pc = Store.load(svc) ?: return
        val cm = svc.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val wanted = if (camFacing == "front") CameraCharacteristics.LENS_FACING_FRONT else CameraCharacteristics.LENS_FACING_BACK
        val id = cm.cameraIdList.firstOrNull { cm.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == wanted }
            ?: cm.cameraIdList.firstOrNull()
        if (id == null) {
            svc.send("webcam.status", JSONObject().put("state", "error").put("message", "No camera found on the phone"))
            return
        }
        val chars = cm.getCameraCharacteristics(id)
        val sizes = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)!!.getOutputSizes(MediaCodec::class.java)
        val size = pickSize(sizes)
        val sensor = chars.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
        val actualFacing = if (chars.get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_FRONT) "front" else "back"
        val fpsRange = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
            ?.filter { it.upper <= 30 }?.maxWithOrNull(compareBy<Range<Int>>({ it.upper }, { it.lower }))

        val stream = try {
            H264Stream(pc, size.width, size.height, 4_000_000, staticContent = false)
        } catch (e: Exception) {
            svc.send("webcam.status", JSONObject().put("state", "error").put("message", "Encoder failed: $e"))
            return
        }
        camStream = stream
        svc.holdWake()
        try {
            cm.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(device: CameraDevice) {
                    if (camStream !== stream) { device.close(); return }
                    camera = device
                    // The camera can be closed (switch/stop) between open and here — that
                    // throws IllegalStateException on this camera thread and killed the app.
                    try {
                    @Suppress("DEPRECATION")
                    device.createCaptureSession(listOf(stream.inputSurface), object : CameraCaptureSession.StateCallback() {
                        override fun onConfigured(session: CameraCaptureSession) {
                            if (camStream !== stream) { session.close(); return }
                            camSession = session
                            val req = runCatching {
                                device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                                    addTarget(stream.inputSurface)
                                    fpsRange?.let { set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
                                    set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                                }.build()
                            }.getOrNull() ?: return
                            runCatching { session.setRepeatingRequest(req, null, camHandler) }
                            svc.send("video.started", JSONObject().put("kind", "webcam").put("facing", actualFacing)
                                .put("width", size.width).put("height", size.height).put("rotation", sensor))
                        }

                        override fun onConfigureFailed(session: CameraCaptureSession) {
                            if (camStream === stream) {
                                svc.send("webcam.status", JSONObject().put("state", "error").put("message", "Camera session failed"))
                            }
                        }
                    }, camHandler)
                    } catch (e: Exception) {
                        Log.i(BridgeService.TAG, "camera closed while starting: $e")
                        runCatching { device.close() }
                    }
                }

                override fun onDisconnected(device: CameraDevice) { device.close() }

                override fun onError(device: CameraDevice, error: Int) {
                    device.close()
                    svc.send("webcam.status", JSONObject().put("state", "error")
                        .put("message", "Camera error $error — is another app using the camera?"))
                }
            }, camHandler)
        } catch (e: SecurityException) {
            stopWebcam()
            svc.send("webcam.status", JSONObject().put("state", "error")
                .put("message", "Allow the Camera permission for PhoneBridge (open the app on the phone once)"))
        } catch (e: android.hardware.camera2.CameraAccessException) {
            // e.g. CAMERA_DISABLED: the phone's camera privacy switch or a device policy blocks the camera. Must not kill the app.
            Log.w(BridgeService.TAG, "camera unavailable", e)
            stopWebcam()
            svc.send("webcam.status", JSONObject().put("state", "error")
                .put("message", "The phone's camera is blocked (privacy switch or policy) — allow camera access on the phone"))
        } catch (e: Exception) {
            Log.w(BridgeService.TAG, "camera open failed", e)
            stopWebcam()
            svc.send("webcam.status", JSONObject().put("state", "error").put("message", "Couldn't open the camera: ${e.message ?: e.javaClass.simpleName}"))
        }
    }

    private fun pickSize(sizes: Array<Size>): Size {
        val preferred = listOf(Size(1280, 720), Size(1920, 1080), Size(960, 540), Size(640, 480))
        preferred.firstOrNull { p -> sizes.any { it.width == p.width && it.height == p.height } }?.let { return it }
        return sizes.filter { it.width <= 1280 }.maxByOrNull { it.width * it.height } ?: sizes.first()
    }

    fun stopWebcam() {
        val wasRunning = camStream != null
        if (wasRunning) svc.releaseWake()
        runCatching { camSession?.close() }; camSession = null
        runCatching { camera?.close() }; camera = null
        camStream?.stop(); camStream = null
        if (wasRunning) svc.send("video.stopped", JSONObject().put("kind", "webcam"))
    }

    fun onStreamFailed(stream: H264Stream) {
        if (stream === mirrorStream) stopMirror()
        if (stream === camStream) stopWebcam()
    }

    fun stopAll() {
        stopMirror()
        stopWebcam()
    }
}

/** Shows Android's own screen-capture consent dialog, then hands the grant to the service. */
class ScreenCaptureActivity : Activity() {
    private var gen = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        gen = BridgeService.instance?.video?.consentGen ?: 0
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        @Suppress("DEPRECATION")
        startActivityForResult(mpm.createScreenCaptureIntent(), 1)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        val video = BridgeService.instance?.video
        // Only the newest prompt may report; an older one (replaced or timed out) must not look like a "declined" answer.
        if (video != null && video.consentGen == gen) video.onProjectionResult(resultCode, data)
        else Log.i(BridgeService.TAG, "ignored a stale screen-capture consent result ($resultCode)")
        finish()
    }
}
