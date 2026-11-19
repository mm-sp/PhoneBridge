package dev.phonebridge

import android.app.Activity
import android.app.KeyguardManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioManager
import android.os.Bundle
import android.os.PowerManager
import org.json.JSONObject

/**
 * Screen wake/unlock and media volume, driven from the PC's mirror page.
 *
 * Unlock wakes the screen and asks Android to dismiss the lock screen. With swipe/no
 * lock that unlocks fully; with a PIN/pattern Android shows its PIN pad instead, which
 * the user completes through the mirror (apps can't bypass a secure lock, by design).
 */
object ScreenControl {
    fun state(ctx: Context): JSONObject {
        val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = ctx.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        return JSONObject()
            .put("on", pm.isInteractive)
            .put("locked", km.isKeyguardLocked)
            .put("secure", km.isKeyguardSecure)
    }

    fun volumeState(ctx: Context): JSONObject {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        val level = am.getStreamVolume(AudioManager.STREAM_MUSIC)
        return JSONObject().put("level", level).put("max", max)
            .put("pct", if (max > 0) level * 100 / max else 0)
            .put("muted", am.isStreamMute(AudioManager.STREAM_MUSIC))
            .put("ringer", when (am.ringerMode) {
                AudioManager.RINGER_MODE_SILENT -> "silent"
                AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
                else -> "normal"
            })
    }

    fun volume(svc: BridgeService, key: String) {
        val am = svc.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val dir = when (key) {
            "volume_up" -> AudioManager.ADJUST_RAISE
            "volume_down" -> AudioManager.ADJUST_LOWER
            else -> AudioManager.ADJUST_TOGGLE_MUTE
        }
        // FLAG_SHOW_UI shows the phone's own volume slider, which the mirror then displays.
        am.adjustStreamVolume(AudioManager.STREAM_MUSIC, dir, AudioManager.FLAG_SHOW_UI)
        svc.send("volume.state", volumeState(svc))
    }

    @Suppress("DEPRECATION")
    fun wakeAndUnlock(svc: BridgeService) {
        val pm = svc.getSystemService(Context.POWER_SERVICE) as PowerManager
        pm.newWakeLock(
            PowerManager.FULL_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP or PowerManager.ON_AFTER_RELEASE,
            "phonebridge:wake",
        ).acquire(3000)
        val intent = Intent(svc, UnlockActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION)
        try {
            svc.startActivity(intent)
        } catch (e: Exception) {
            svc.send("error", JSONObject().put("message", "Couldn't open the lock screen: $e"))
        }
    }

    fun lock(svc: BridgeService) {
        val input = InputService.instance
        if (input == null || android.os.Build.VERSION.SDK_INT < 28) {
            svc.send("mirror.status", JSONObject().put("state", "no_input")
                .put("message", "Turn on PhoneBridge remote control in the phone's Accessibility settings to lock it from the PC"))
            return
        }
        input.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN)
    }

    /** Reports screen on/off/unlock to the PC so its Lock/Unlock button stays right. */
    class Receiver(private val svc: BridgeService) : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            svc.send("screen.state", state(ctx))
        }

        fun register() {
            svc.registerReceiver(this, IntentFilter().apply {
                addAction(Intent.ACTION_SCREEN_ON)
                addAction(Intent.ACTION_SCREEN_OFF)
                addAction(Intent.ACTION_USER_PRESENT)
            })
        }
    }
}

/** Invisible activity that turns the screen on and asks Android to dismiss the keyguard. */
class UnlockActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (android.os.Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        val km = getSystemService(KEYGUARD_SERVICE) as KeyguardManager
        if (!km.isKeyguardLocked) {
            finish()
            return
        }
        km.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
            override fun onDismissSucceeded() = done()
            override fun onDismissCancelled() = done()
            override fun onDismissError() = done()
        })
        // Secure lock: the PIN pad is now up; finish so it isn't covered by us.
        window.decorView.postDelayed({ if (!isFinishing) finish() }, 1500)
    }

    private fun done() {
        BridgeService.instance?.send("screen.state", ScreenControl.state(this))
        finish()
    }
}
