package dev.phonebridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Bundle
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction
import org.json.JSONObject

/**
 * Executes `mirror.input` from the PC (PROTOCOL.md section 4): taps/swipes as
 * accessibility gestures, Back/Home/Recents as global actions, text into the focused
 * field. Requires the one-time Accessibility toggle that MainActivity links to.
 */
class InputService : AccessibilityService() {
    override fun onServiceConnected() { instance = this }
    override fun onDestroy() { if (instance === this) instance = null; super.onDestroy() }
    /** Text currently selected on the phone (tracked from selection events). */
    @Volatile private var lastSelection: String? = null

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        when (event.eventType) {
            AccessibilityEvent.TYPE_VIEW_TEXT_SELECTION_CHANGED -> {
                if (event.isPassword) { lastSelection = null; return }
                val text = event.text?.joinToString("")
                    ?.takeIf { it.isNotEmpty() } ?: event.source?.text?.toString() ?: return
                val from = minOf(event.fromIndex, event.toIndex)
                val to = maxOf(event.fromIndex, event.toIndex)
                lastSelection = if (from >= 0 && to > from && to <= text.length) text.substring(from, to) else null
            }
            AccessibilityEvent.TYPE_VIEW_CLICKED -> {
                // The phone's own text toolbar "Copy"/"Cut": Android 10+ won't let us read
                // the clipboard in the background, but we know what was selected — send that.
                val label = (event.text?.joinToString(" ") ?: "") + " " + (event.contentDescription ?: "")
                val sel = lastSelection
                if (COPY_WORDS.any { label.trim().equals(it, ignoreCase = true) }) {
                    if (sel != null) sendToPc(sel) else Clipboard.readSoon(this)
                    lastSelection = null
                }
            }
            // "Copied" toasts / the system's "Copied to clipboard" popup / app copy buttons:
            // something new is on the clipboard, read it (needs a moment of focus, see Clipboard).
            AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED, AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> {
                if (event.packageName == packageName) return
                val text = (event.text?.joinToString(" ") ?: "").lowercase()
                if (COPIED_HINTS.any { text.contains(it) }) Clipboard.readSoon(this)
            }
        }
    }

    /** Text/description of the smallest on-screen node under (x, y), across all windows. */
    private fun labelAt(x: Int, y: Int): String? {
        var best: AccessibilityNodeInfo? = null
        var bestArea = Long.MAX_VALUE
        val r = android.graphics.Rect()
        fun visit(n: AccessibilityNodeInfo?, depth: Int) {
            if (n == null || depth > 40) return
            n.getBoundsInScreen(r)
            if (!r.contains(x, y)) return
            val area = r.width().toLong() * r.height()
            if ((n.text != null || n.contentDescription != null) && area < bestArea) { best = n; bestArea = area }
            for (i in 0 until n.childCount) visit(n.getChild(i), depth + 1)
        }
        // Topmost windows first: the text toolbar is a popup above the app.
        for (w in windows.sortedByDescending { it.layer }) {
            visit(w.root, 0)
            if (best != null) break
        }
        val b = best ?: return null
        return (b.text ?: b.contentDescription)?.toString()
    }

    /** A mirror tap landing on the phone's "Copy"/"Cut" button: send the selection to the PC. */
    private fun maybeCopyAt(fx: Float, fy: Float) {
        val sel = lastSelection ?: return
        val (w, h) = screen()
        val label = labelAt((fx * w).toInt(), (fy * h).toInt())?.trim() ?: return
        if (COPY_WORDS.any { label.equals(it, ignoreCase = true) }) {
            sendToPc(sel)
            lastSelection = null
        }
    }

    private fun sendToPc(text: String) {
        val svc = BridgeService.instance ?: return
        svc.lastClipText = text
        svc.send("clipboard.update", JSONObject().put("text", text).put("copied_on_phone", true))
    }

    /** PC's "Copy" button: send whatever is selected on the phone right now. */
    fun copySelection() {
        val sel = lastSelection ?: rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.let { node ->
            val t = node.text?.toString() ?: return@let null
            val a = minOf(node.textSelectionStart, node.textSelectionEnd)
            val b = maxOf(node.textSelectionStart, node.textSelectionEnd)
            if (a >= 0 && b > a && b <= t.length && !node.isPassword) t.substring(a, b) else null
        }
        if (sel.isNullOrEmpty()) {
            BridgeService.instance?.send("mirror.status", JSONObject().put("state", "no_selection")
                .put("message", "Nothing selected on the phone — hold on the text first, then drag the handles"))
        } else {
            sendToPc(sel)
        }
    }
    override fun onInterrupt() {}

    /** Id of the private display an app is streaming on (per-app streaming), else -1. */
    private fun appDisplay(): Int = BridgeService.instance?.video?.appDisplayId ?: -1

    private fun screen(): Pair<Int, Int> {
        val m = DisplayMetrics()
        val id = appDisplay()
        val display = if (id >= 0) (getSystemService(DISPLAY_SERVICE) as android.hardware.display.DisplayManager).getDisplay(id) else null
        @Suppress("DEPRECATION")
        (display ?: (getSystemService(WINDOW_SERVICE) as WindowManager).defaultDisplay).getRealMetrics(m)
        return m.widthPixels to m.heightPixels
    }

    private fun appStatus(message: String) {
        BridgeService.instance?.send("mirror.status", JSONObject().put("state", "app_limit").put("message", message))
    }

    /**
     * True while the PC has powered the panel off. Android drops every touch event on an OFF display (even `adb shell input
     * tap`), so gestures do nothing then; HOME/BACK and accessibility node actions still work.
     */
    private fun panelOff(): Boolean = BridgeService.instance?.screenSync?.display?.mode == DisplayPower.Mode.OFF_BY_PC

    /** Deepest visible node containing (x, y); later siblings are drawn on top, so they are tried first. */
    private fun deepestAt(n: AccessibilityNodeInfo, x: Int, y: Int, r: android.graphics.Rect, depth: Int = 0): AccessibilityNodeInfo? {
        if (depth > 60) return null
        n.getBoundsInScreen(r)
        if (!r.contains(x, y)) return null
        for (i in n.childCount - 1 downTo 0) {
            val c = n.getChild(i) ?: continue
            if (!c.isVisibleToUser) continue
            deepestAt(c, x, y, r, depth + 1)?.let { return it }
        }
        return n
    }

    /** Walks from the node under (x, y) up to the first ancestor [accept] likes and performs [act] on it. */
    private fun onNodeAt(fx: Float, fy: Float, accept: (AccessibilityNodeInfo) -> Boolean, act: (AccessibilityNodeInfo) -> Boolean): Boolean {
        val (w, h) = screen()
        val x = (fx * w).toInt()
        val y = (fy * h).toInt()
        val r = android.graphics.Rect()
        for (win in windows.sortedByDescending { it.layer }) {
            val root = win.root ?: continue
            var n: AccessibilityNodeInfo? = deepestAt(root, x, y, r) ?: continue
            var hops = 0
            while (n != null && hops++ < 25) {
                if (accept(n)) return act(n)
                n = n.parent
            }
            return false   // the topmost window under the point has nothing to press: don't click through it
        }
        return false
    }

    private var lastOffNote = 0L

    /** Gesture input can't reach a powered-off panel; tell the user why a control did nothing (at most every 4 s). */
    private fun offLimitNote() {
        val now = android.os.SystemClock.elapsedRealtime()
        if (now - lastOffNote < 4000) return
        lastOffNote = now
        appStatus("Screen is off: buttons, lists and text fields work, but custom-drawn areas (games, maps) need the screen on")
    }

    private fun clickViaNodes(fx: Float, fy: Float, long: Boolean): Boolean {
        val ok = onNodeAt(fx, fy, { if (long) it.isLongClickable else it.isClickable }) {
            it.performAction(if (long) AccessibilityNodeInfo.ACTION_LONG_CLICK else AccessibilityNodeInfo.ACTION_CLICK)
        }
        if (!ok) offLimitNote()
        return ok
    }

    /** A swipe from (x1,y1) to (x2,y2) becomes a scroll of the list under the finger. Finger up = reveal content below. */
    private fun scrollViaNodes(x1: Float, y1: Float, x2: Float, y2: Float): Boolean {
        val dx = x2 - x1
        val dy = y2 - y1
        val vertical = Math.abs(dy) >= Math.abs(dx)
        val want = if (vertical) (if (dy < 0) AccessibilityAction.ACTION_SCROLL_DOWN else AccessibilityAction.ACTION_SCROLL_UP)
                   else (if (dx < 0) AccessibilityAction.ACTION_SCROLL_RIGHT else AccessibilityAction.ACTION_SCROLL_LEFT)
        val generic = if ((vertical && dy < 0) || (!vertical && dx < 0)) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        val ok = onNodeAt(x1, y1, { n -> n.isScrollable || n.actionList.any { it.id == want.id } }) { n ->
            val r1 = n.performAction(want.id)
            val r2 = if (r1) false else n.performAction(generic)
            Log.i(BridgeService.TAG, "scroll via ${n.className} scrollable=${n.isScrollable} directional=$r1 generic=$r2")
            r1 || r2
        }
        if (!ok) { Log.i(BridgeService.TAG, "scroll via nodes: nothing scrollable at ($x1, $y1)"); offLimitNote() }
        return ok
    }

    private fun gesture(x1: Float, y1: Float, x2: Float, y2: Float, durationMs: Long) {
        val (w, h) = screen()
        val path = Path().apply {
            moveTo((x1 * w).coerceIn(0f, w - 1f), (y1 * h).coerceIn(0f, h - 1f))
            if (x1 != x2 || y1 != y2) lineTo((x2 * w).coerceIn(0f, w - 1f), (y2 * h).coerceIn(0f, h - 1f))
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs.coerceIn(1, 10_000))
        val builder = GestureDescription.Builder().addStroke(stroke)
        val id = appDisplay()
        if (id >= 0) {
            if (android.os.Build.VERSION.SDK_INT < 32) {
                return appStatus("Controlling a streamed app needs Android 12L or newer — you can still watch it")
            }
            builder.setDisplayId(id)
        }
        dispatchGesture(builder.build(), null, null)
    }

    private fun focusedField(): AccessibilityNodeInfo? {
        val id = appDisplay()
        val root = if (id >= 0 && android.os.Build.VERSION.SDK_INT >= 30) {
            windowsOnAllDisplays.get(id)?.sortedByDescending { it.layer }?.firstNotNullOfOrNull { it.root }
        } else rootInActiveWindow
        val node = root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        if (node == null || !node.isEditable) {
            BridgeService.instance?.send("mirror.status", JSONObject().put("state", "no_field")
                .put("message", "Tap a text box on the phone first, then type"))
            return null
        }
        return node
    }

    private fun typeText(text: String) {
        val node = focusedField() ?: return
        val existing = node.text?.toString().takeUnless { node.isShowingHintText } ?: ""
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, existing + text)
        }
        node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    private fun pressEnter() {
        val node = focusedField() ?: return
        // API 30+: trigger the keyboard's action (Send / Search / Go / newline) properly.
        if (android.os.Build.VERSION.SDK_INT >= 30 &&
            node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)) return
        typeText("\n")
    }

    private fun backspace() {
        val node = focusedField() ?: return
        val existing = node.text?.toString().takeUnless { node.isShowingHintText } ?: return
        if (existing.isEmpty()) return
        node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, existing.dropLast(1))
        })
    }

    fun perform(p: JSONObject) {
        when (p.optString("kind")) {
            "tap" -> {
                runCatching { maybeCopyAt(p.f("x"), p.f("y")) }
                // Panel off (and not a streamed app): touches are dropped by Android, so press the control through accessibility.
                if (!(appDisplay() < 0 && panelOff() && runCatching { clickViaNodes(p.f("x"), p.f("y"), false) }.getOrDefault(false)))
                    gesture(p.f("x"), p.f("y"), p.f("x"), p.f("y"), 40)
            }
            "long_press" -> {
                if (!(appDisplay() < 0 && panelOff() && runCatching { clickViaNodes(p.f("x"), p.f("y"), true) }.getOrDefault(false)))
                    gesture(p.f("x"), p.f("y"), p.f("x"), p.f("y"), 700)
            }
            "swipe" -> {
                if (!(appDisplay() < 0 && panelOff() &&
                        runCatching { scrollViaNodes(p.f("x1"), p.f("y1"), p.f("x2"), p.f("y2")) }.getOrDefault(false)))
                    gesture(p.f("x1"), p.f("y1"), p.f("x2"), p.f("y2"), p.optLong("duration_ms", 250))
            }
            "text" -> typeText(p.optString("text"))
            "key" -> if (appDisplay() >= 0 && p.optString("key") in SYSTEM_KEYS) {
                appStatus("Back / Home / Recents act on the phone's own screen, not a streamed app — use the app's own buttons")
            } else when (p.optString("key")) {
                "back" -> performGlobalAction(GLOBAL_ACTION_BACK)
                "home" -> performGlobalAction(GLOBAL_ACTION_HOME)
                "recents" -> performGlobalAction(GLOBAL_ACTION_RECENTS)
                "notifications" -> performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS)
                "quick_settings" -> performGlobalAction(GLOBAL_ACTION_QUICK_SETTINGS)
                "power" -> performGlobalAction(GLOBAL_ACTION_POWER_DIALOG)
                "lock" -> if (android.os.Build.VERSION.SDK_INT >= 28) performGlobalAction(GLOBAL_ACTION_LOCK_SCREEN)
                "screenshot" -> if (android.os.Build.VERSION.SDK_INT >= 28) performGlobalAction(GLOBAL_ACTION_TAKE_SCREENSHOT)
                "backspace" -> backspace()
                "enter" -> pressEnter()
            }
        }
    }

    private fun JSONObject.f(k: String) = optDouble(k, 0.0).toFloat()

    companion object {
        @Volatile var instance: InputService? = null
        private val SYSTEM_KEYS = setOf("back", "home", "recents", "notifications", "quick_settings", "power", "screenshot")
        private val COPIED_HINTS = listOf("copied", "copiado", "copié", "kopiert", "copiato", "コピーしました", "已复制", "कॉपी किया")
        private val COPY_WORDS = listOf("Copy", "Cut", "Copiar", "Copier", "Kopieren", "Copia", "コピー", "复制", "कॉपी करें")

        fun handle(p: JSONObject) {
            val s = instance
            if (s == null) {
                BridgeService.instance?.send("mirror.status", JSONObject().put("state", "no_input")
                    .put("message", "Turn on PhoneBridge in the phone's Settings > Accessibility to control it from the PC"))
                return
            }
            s.perform(p)
        }
    }
}
