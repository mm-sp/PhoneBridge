package dev.phonebridge

import android.content.Context
import android.graphics.PixelFormat
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.View
import android.view.WindowManager

/**
 * Phone-side display state for screen sync. What an ordinary app can and cannot do (docs/SCREEN_SYNC.md):
 *
 *  - It cannot power the panel off. That is done by the PC over ADB (`cmd display power-off 0`, shell privilege); this
 *    class then only RECORDS that fact ([markOffByPc]) so it can be undone without the PC — by [restore], by the link-loss
 *    watchdog, or after this process was killed ([recoverIfStale], persisted flag).
 *  - Without ADB the one thing it can do is DIM: a transparent, untouchable overlay asking for minimum brightness. It
 *    needs "Display over other apps" (already used for starting the capture), does not black out the screen, and is
 *    reported as [Mode.DIMMED], never as off. Mirroring is unaffected because the overlay is transparent.
 *  - Locking through accessibility turns the panel off but ends the capture on most devices, so it is not used here.
 */
class DisplayPower(private val ctx: Context) {
    enum class Mode { ON, OFF_BY_PC, DIMMED }

    @Volatile var mode = Mode.ON
        private set
    private var overlay: View? = null
    private var keepAwake: PowerManager.WakeLock? = null
    private val prefs get() = ctx.getSharedPreferences("screensync", Context.MODE_PRIVATE)

    fun canDim() = Settings.canDrawOverlays(ctx)

    /** The PC turned the panel off through ADB. Keep the device "interactive" for PowerManager and remember to undo it. */
    @Suppress("DEPRECATION")
    fun markOffByPc() {
        persist(true)
        if (keepAwake == null) {
            keepAwake = (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.SCREEN_DIM_WAKE_LOCK, "phonebridge:mirror-awake").apply { setReferenceCounted(false) }
        }
        runCatching { if (keepAwake?.isHeld != true) keepAwake?.acquire(MAX_OFF_MS) }
        mode = Mode.OFF_BY_PC
    }

    /** Fallback: transparent overlay at minimum brightness. Returns false if the phone doesn't allow it. */
    fun dim(): Boolean {
        if (!canDim()) return false
        if (overlay != null) { mode = Mode.DIMMED; return true }
        return try {
            val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
            val v = View(ctx)   // fully transparent: the screen capture still sees the real content
            val lp = WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                    WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT,
            ).apply { screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_OFF }
            wm.addView(v, lp)
            overlay = v
            persist(true)
            mode = Mode.DIMMED
            true
        } catch (e: Exception) {
            Log.w(BridgeService.TAG, "dim overlay failed", e)
            false
        }
    }

    /** Undo whatever we did and make sure the screen is on. Safe to call any time, any number of times. */
    @Suppress("DEPRECATION")
    fun restore(reason: String) {
        val had = mode != Mode.ON || prefs.getBoolean(KEY_OFF, false)
        overlay?.let { v -> runCatching { (ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager).removeView(v) }; overlay = null }
        runCatching { if (keepAwake?.isHeld == true) keepAwake?.release() }
        mode = Mode.ON
        persist(false)
        if (had) {
            Log.i(BridgeService.TAG, "display restored: $reason")
            // Wakes the panel without touching the lock screen (ScreenControl.wakeAndUnlock also dismisses the keyguard).
            runCatching {
                (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager)
                    .newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP, "phonebridge:wake").acquire(3000)
            }
        }
    }

    /** After a process kill the flag survives; a phone must never be left dark by a dead app. */
    fun recoverIfStale() {
        if (prefs.getBoolean(KEY_OFF, false) && mode == Mode.ON) restore("recovered after the app was restarted")
    }

    /**
     * Liveness probe for the PC: makes a tiny, near-invisible content change (a 1-pixel overlay, 150 ms) so the encoder
     * has something to emit. A still screen sends no frames, so silence cannot tell a live stream from a dead one; a
     * frame arriving after this proves the virtual display is still being composed (e.g. with the panel powered off).
     * Returns false when the phone has no overlay permission (the PC then reports the stream as unverifiable).
     */
    fun contentProbe(): Boolean {
        if (!canDim()) return false
        android.os.Handler(android.os.Looper.getMainLooper()).post {
            try {
                val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
                val v = View(ctx).apply { setBackgroundColor(0x40FF0000) }
                val lp = WindowManager.LayoutParams(1, 1,
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                    PixelFormat.TRANSLUCENT).apply { gravity = android.view.Gravity.TOP or android.view.Gravity.START }
                wm.addView(v, lp)
                v.postDelayed({ runCatching { wm.removeView(v) } }, 150)
            } catch (e: Exception) { Log.w(BridgeService.TAG, "content probe failed", e) }
        }
        return true
    }

    /** Real panel state, as Android reports it (what the dashboard shows). */
    fun panelInteractive(): Boolean = (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager).isInteractive

    private fun persist(off: Boolean) { prefs.edit().putBoolean(KEY_OFF, off).apply() }

    companion object {
        private const val KEY_OFF = "display_off_pending"
        private const val MAX_OFF_MS = 4 * 60 * 60 * 1000L   // safety: never hold the screen lock longer than 4 h
    }
}
