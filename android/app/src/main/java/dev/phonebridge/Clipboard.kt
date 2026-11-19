package dev.phonebridge

import android.app.Activity
import android.annotation.SuppressLint
import android.content.ClipboardManager
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.os.Bundle
import android.widget.Toast
import org.json.JSONObject

/**
 * Phone -> PC clipboard. Android 10+ only lets the app that currently has focus read the
 * clipboard, so a background service can't watch it. Instead the text is read whenever a
 * PhoneBridge window is focused: the main screen, or the invisible
 * [ClipboardSendActivity] behind the "Send clipboard to PC" notification button.
 */
object Clipboard {
    /** Returns true if something new was sent. */
    fun sendIfNew(ctx: Context): Boolean {
        val svc = BridgeService.instance ?: return false
        if (!allowed(ctx)) return false
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        // Password managers mark what they copy as sensitive: never forward those.
        if (cm.primaryClipDescription?.extras?.getBoolean(Privacy.EXTRA_IS_SENSITIVE, false) == true) return false
        if (sendImageIfNew(ctx, svc, cm)) return true
        val text = runCatching { cm.primaryClip?.getItemAt(0)?.coerceToText(ctx)?.toString() }.getOrNull()
        if (text.isNullOrEmpty() || text == svc.lastClipText) return false
        svc.lastClipText = text
        svc.worker.execute { svc.send("clipboard.update", JSONObject().put("text", text)) }
        return true
    }

    /** Clipboard sync is on in the privacy settings and not denied in the permission manager. */
    fun allowed(ctx: Context) = Privacy.clipboardEnabled(ctx) && PcAccess.mode(ctx, "clipboard") != PcAccess.Mode.DENY

    /** Label on clips we set from the PC, so we can recognise (and later clear) our own, and nothing else. */
    const val SYNCED_LABEL = "PhoneBridge synced"

    /** Removes a clip we placed from the phone's clipboard after the configured time, unless something else was copied since. */
    fun scheduleClear(ctx: Context) {
        val seconds = Privacy.clipboardExpiry(ctx)
        if (seconds <= 0) return
        val app = ctx.applicationContext
        Handler(Looper.getMainLooper()).postDelayed({
            val cm = app.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            // Only the clip's description is readable in the background, and it is enough: our own clips carry our label.
            if (cm.primaryClipDescription?.label == SYNCED_LABEL) {
                @Suppress("DEPRECATION")
                runCatching { if (android.os.Build.VERSION.SDK_INT >= 28) cm.clearPrimaryClip() else cm.setPrimaryClip(android.content.ClipData.newPlainText("", "")) }
                PhoneAudit.log(app, "clipboard.expired")
            }
        }, seconds * 1000L)
    }

    private const val MAX_IMAGE_BYTES = 4 * 1024 * 1024
    @Volatile private var lastImageHash: Int = 0

    /** An image on the clipboard (a content:// URI) goes to the PC as base64 PNG, max 4 MiB. */
    private fun sendImageIfNew(ctx: Context, svc: BridgeService, cm: ClipboardManager): Boolean {
        val clip = cm.primaryClip ?: return false
        val desc = clip.description
        if (desc.mimeTypeCount == 0 || (0 until desc.mimeTypeCount).none { desc.getMimeType(it).startsWith("image/") }) return false
        val uri = clip.getItemAt(0)?.uri ?: return false
        val bytes = runCatching {
            val bmp = ctx.contentResolver.openInputStream(uri)?.use { android.graphics.BitmapFactory.decodeStream(it) }
                ?: return@runCatching null
            val out = java.io.ByteArrayOutputStream()
            bmp.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out)
            out.toByteArray()
        }.getOrNull() ?: return false
        if (bytes.size > MAX_IMAGE_BYTES || bytes.contentHashCode() == lastImageHash) return false
        lastImageHash = bytes.contentHashCode()
        svc.lastClipText = null
        svc.worker.execute {
            svc.send("clipboard.update", JSONObject().put("image", android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP)).put("mime", "image/png"))
        }
        return true
    }

    /** PC -> phone: put an image on the phone clipboard via the FileProvider. */
    fun setImageFromPc(ctx: Context, b64: String) {
        if (!allowed(ctx)) return
        val bytes = runCatching { android.util.Base64.decode(b64, android.util.Base64.DEFAULT) }.getOrNull() ?: return
        if (bytes.size > MAX_IMAGE_BYTES) return
        lastImageHash = bytes.contentHashCode()  // don't echo it straight back
        val dir = java.io.File(ctx.cacheDir, "clip").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() }
        val f = java.io.File(dir, "pc_${System.currentTimeMillis()}.png").apply { writeBytes(bytes) }
        val uri = androidx.core.content.FileProvider.getUriForFile(ctx, "${ctx.packageName}.clipshare", f)
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(android.content.ClipData.newUri(ctx.contentResolver, SYNCED_LABEL, uri))
        scheduleClear(ctx)
    }

    /** Best effort from the service — succeeds only on Android 9 and older. */
    fun sendCurrentIfReadable(ctx: Context) {
        if (android.os.Build.VERSION.SDK_INT < 29) sendIfNew(ctx)
    }

    // Lint (StaticFieldLeak) flags this static View. Not a leak: the view is built from the *application* context
    // (showReader receives ctx.applicationContext), it exists only while the 1x1 reader window is attached, and it is
    // always cleared by remove() - on focus, or by the 800 ms fallback timer in showReader.
    @SuppressLint("StaticFieldLeak")
    private var overlay: View? = null

    /**
     * Something was just copied on the phone (seen by the accessibility service). Android
     * 10+ only lets the focused app read the clipboard, so briefly add an invisible 1×1
     * focusable overlay window, read the clipboard when it gains focus, and remove it.
     * Needs "Display over other apps"; takes well under a second.
     */
    fun readSoon(ctx: Context) {
        if (android.os.Build.VERSION.SDK_INT < 29) { sendIfNew(ctx); return }
        if (!android.provider.Settings.canDrawOverlays(ctx) || BridgeService.instance?.isConnected() != true) return
        val main = Handler(Looper.getMainLooper())
        // Let the copying app finish writing the clip first.
        main.postDelayed({ showReader(ctx.applicationContext, main) }, 250)
    }

    private fun showReader(ctx: Context, main: Handler) {
        if (overlay != null) return
        val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val view = object : View(ctx) {
            override fun onWindowFocusChanged(hasWindowFocus: Boolean) {
                super.onWindowFocusChanged(hasWindowFocus)
                if (hasWindowFocus) {
                    sendIfNew(ctx)
                    remove(wm)
                }
            }
        }
        val params = WindowManager.LayoutParams(
            1, 1,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
            android.graphics.PixelFormat.TRANSPARENT,
        ).apply { gravity = Gravity.TOP or Gravity.START }
        try {
            wm.addView(view, params)
            overlay = view
        } catch (e: Exception) {
            return
        }
        main.postDelayed({ remove(wm) }, 800)  // never linger if focus doesn't arrive
    }

    private fun remove(wm: WindowManager) {
        val v = overlay ?: return
        overlay = null
        runCatching { wm.removeView(v) }
    }
}

class ClipboardSendActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Collapse the notification shade so this window actually gains focus.
        @Suppress("DEPRECATION")
        sendBroadcast(android.content.Intent(android.content.Intent.ACTION_CLOSE_SYSTEM_DIALOGS))
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus) return
        val svc = BridgeService.instance
        val msg = when {
            svc == null || !svc.isConnected() -> "Not connected to your PC"
            Clipboard.sendIfNew(this) -> "Clipboard sent to PC"
            else -> "Clipboard already sent (or empty)"
        }
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
        finish()
    }
}
