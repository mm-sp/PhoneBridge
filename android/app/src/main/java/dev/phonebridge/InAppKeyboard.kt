package dev.phonebridge

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView

/**
 * PhoneBridge's own see-through keyboard, drawn inside the app (it is not a system input
 * method, so other apps keep the phone's normal keyboard). Used by the Terminal tab, the
 * full-screen PC screen and the Apps search box.
 *
 * Emits the same callbacks as [KeyCaptureView]: onText("a") for characters and
 * onKey("BackSpace" / "Return") for keys.
 */
@SuppressLint("ViewConstructor")
class InAppKeyboard(
    ctx: Context,
    private val onText: (String) -> Unit,
    private val onKey: (String) -> Unit,
    private val onHide: () -> Unit = {},
    numbersFirst: Boolean = false,
) : LinearLayout(ctx) {

    private enum class Page { LETTERS, SYMBOLS, MORE }

    private var page = if (numbersFirst) Page.SYMBOLS else Page.LETTERS
    private var shift = 0  // 0 off, 1 next letter, 2 caps lock
    private var lastShiftTap = 0L
    private val repeat = Handler(Looper.getMainLooper())

    init {
        orientation = VERTICAL
        setBackgroundColor(BACKDROP)
        setPadding(dp(4), dp(2), dp(4), dp(4))
        // Swallow touches on the gaps so they don't fall through to what's behind.
        isClickable = true
        render()
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun rows(): List<List<String>> = when (page) {
        Page.LETTERS -> listOf(
            "qwertyuiop".map { it.toString() },
            "asdfghjkl".map { it.toString() },
            listOf(SHIFT) + "zxcvbnm".map { it.toString() } + listOf(BACK),
            listOf(SYM, ",", SPACE, ".", ENTER, HIDE),
        )
        Page.SYMBOLS -> listOf(
            "1234567890".map { it.toString() },
            listOf("@", "#", "$", "_", "&", "-", "+", "(", ")", "/"),
            listOf(MORE_SYM, "*", "\"", "'", ":", ";", "!", "?", BACK),
            listOf(ABC, ",", SPACE, ".", ENTER, HIDE),
        )
        Page.MORE -> listOf(
            listOf("~", "`", "|", "\\", "{", "}", "[", "]", "<", ">"),
            listOf("^", "%", "=", "€", "£", "₹", "°", "•", "…", "§"),
            listOf(SYM, "©", "®", "™", "¶", "¿", "¡", "÷", BACK),
            listOf(ABC, ",", SPACE, ".", ENTER, HIDE),
        )
    }

    private fun render() {
        removeAllViews()
        for (r in rows()) {
            val line = LinearLayout(context).apply { orientation = HORIZONTAL; gravity = Gravity.CENTER }
            // Compact rows: the keyboard (plus the Terminal's extra keys) stays within half the screen.
            for (k in r) line.addView(keyView(k), LayoutParams(0, dp(KEY_HEIGHT_DP), weightOf(k)).apply {
                setMargins(dp(2), dp(2), dp(2), dp(2))
            })
            addView(line, LayoutParams(-1, -2))
        }
    }

    private fun weightOf(k: String) = when (k) {
        SPACE -> 4.5f
        SHIFT, BACK, SYM, ABC, MORE_SYM, ENTER -> 1.5f
        else -> 1f
    }

    private fun label(k: String) = when (k) {
        SHIFT -> if (shift == 2) "⇪" else "⇧"
        BACK -> "⌫"
        ENTER -> "⏎"
        SPACE -> "space"
        SYM -> "?123"
        ABC -> "ABC"
        MORE_SYM -> "=\\<"
        HIDE -> "▾"
        else -> if (page == Page.LETTERS && shift != 0) k.uppercase() else k
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun keyView(k: String): TextView = TextView(context).apply {
        text = label(k)
        gravity = Gravity.CENTER
        setTextColor(Color.WHITE)
        textSize = if (k.length == 1) 15f else 12f
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setShadowLayer(3f, 0f, 1f, Color.argb(160, 0, 0, 0))
        val special = k.length > 1
        val idle = if (k == SHIFT && shift != 0) KEY_ACTIVE else if (special) KEY_SPECIAL else KEY
        background = Ui.rounded(idle, dp(8).toFloat(), Color.argb(40, 255, 255, 255), 1)
        val backRepeat = object : Runnable {
            override fun run() {
                onKey("BackSpace")
                repeat.postDelayed(this, 55)
            }
        }
        setOnTouchListener { v, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    v.background = Ui.rounded(KEY_PRESSED, dp(8).toFloat(), Color.argb(80, 255, 255, 255), 1)
                    v.performHapticFeedback(android.view.HapticFeedbackConstants.KEYBOARD_TAP)
                    if (k == BACK) {
                        onKey("BackSpace")
                        repeat.postDelayed(backRepeat, 400)
                    }
                }
                MotionEvent.ACTION_UP -> {
                    repeat.removeCallbacks(backRepeat)
                    v.background = Ui.rounded(idle, dp(8).toFloat(), Color.argb(40, 255, 255, 255), 1)
                    if (k != BACK) press(k)
                }
                MotionEvent.ACTION_CANCEL -> {
                    repeat.removeCallbacks(backRepeat)
                    v.background = Ui.rounded(idle, dp(8).toFloat(), Color.argb(40, 255, 255, 255), 1)
                }
            }
            true
        }
    }

    private fun press(k: String) {
        when (k) {
            SHIFT -> {
                val now = System.currentTimeMillis()
                shift = when {
                    now - lastShiftTap < 350 -> 2   // double-tap: caps lock
                    shift == 0 -> 1
                    else -> 0
                }
                lastShiftTap = now
                render()
            }
            SYM -> { page = Page.SYMBOLS; render() }
            MORE_SYM -> { page = Page.MORE; render() }
            ABC -> { page = Page.LETTERS; render() }
            ENTER -> onKey("Return")
            HIDE -> onHide()
            SPACE -> onText(" ")
            else -> {
                onText(if (page == Page.LETTERS && shift != 0) k.uppercase() else k)
                if (shift == 1) { shift = 0; render() }
            }
        }
    }

    companion object {
        private const val SHIFT = "SHIFT"
        private const val BACK = "BACK"
        private const val ENTER = "ENTER"
        private const val SPACE = "SPACE"
        private const val SYM = "SYM"
        private const val ABC = "ABC"
        private const val MORE_SYM = "MORE"
        private const val HIDE = "HIDE"
        const val KEY_HEIGHT_DP = 26

        // See-through: a light dim behind, frosted keys on top.
        val BACKDROP = Color.argb(70, 0, 0, 0)
        val KEY = Color.argb(46, 255, 255, 255)
        val KEY_SPECIAL = Color.argb(30, 255, 255, 255)
        val KEY_ACTIVE = Color.argb(150, 76, 141, 255)
        val KEY_PRESSED = Color.argb(110, 255, 255, 255)
    }
}
