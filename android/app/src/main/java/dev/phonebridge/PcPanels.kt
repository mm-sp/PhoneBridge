package dev.phonebridge

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs

/**
 * The phone app's "control this PC" tabs (protocol/PROTOCOL.md section 4c). Each panel
 * builds its own views and reacts to the PC's replies, which BridgeService forwards to
 * the visible panel via [BridgeService.pcListener].
 */
abstract class PcPanel(protected val act: Activity) {
    abstract val title: String
    abstract val view: View
    open fun onShow() {}
    open fun onHide() {}
    open fun onMessage(type: String, p: JSONObject) {}
    /** The PC refused a request (`pc.status` with `denied`): control is off or awaiting approval, or the feature is
     *  unavailable. Show [message] where the answer was going to appear, so nothing stays "Loading…". */
    open fun onDenied(message: String) {}

    /** Replaces a tab's content with the PC's explanation. */
    protected fun explain(list: LinearLayout, message: String) {
        list.removeAllViews()
        list.addView(text(message, 14f, color = Ui.WARN))
    }

    protected fun send(type: String, payload: JSONObject = JSONObject()): Boolean {
        val svc = BridgeService.instance
        if (svc == null || !svc.isConnected()) {
            toast("Not connected to the PC — open the Home tab")
            return false
        }
        svc.send(type, payload)
        return true
    }

    protected fun input(payload: JSONObject) = BridgeService.instance?.takeIf { it.isConnected() }?.send("pc.input", payload)

    protected fun toast(msg: String) = Toast.makeText(act, msg, Toast.LENGTH_SHORT).show()
    protected fun dp(v: Int) = (v * act.resources.displayMetrics.density).toInt()

    protected fun column(pad: Int = 16) = LinearLayout(act).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(pad), dp(pad), dp(pad), dp(pad))
    }

    protected fun row() = LinearLayout(act).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }

    protected fun card() = Ui.card(act)

    protected fun text(s: String, size: Float = 14f, bold: Boolean = false, color: Int = Ui.TEXT) =
        Ui.text(act, s, size, bold, color)

    protected fun button(label: String, primary: Boolean = false, onClick: () -> Unit) =
        Ui.button(act, label, if (primary) Ui.Kind.PRIMARY else Ui.Kind.SECONDARY, onClick).apply {
            layoutParams = LinearLayout.LayoutParams(-2, -2).apply { marginEnd = dp(6); topMargin = dp(4); bottomMargin = dp(4) }
        }

    protected fun section(s: String) = Ui.sectionTitle(act, s)

    protected fun weighted(v: View, w: Float = 1f) = v.apply { layoutParams = LinearLayout.LayoutParams(0, -2, w) }

    protected fun bar(pct: Int, color: Int = Ui.ACCENT) = Ui.bar(act, pct, color)

    protected fun scroll(child: View) = ScrollView(act).apply {
        setBackgroundColor(BG)
        addView(child)
    }

    companion object {
        val ACCENT = Ui.ACCENT
        val BG = Ui.BG

        fun human(bytes: Long): String {
            var v = bytes.toDouble()
            for (unit in listOf("B", "KB", "MB", "GB", "TB")) {
                if (v < 1024 || unit == "TB") return if (unit == "B") "${v.toLong()} B" else "%.1f %s".format(v, unit)
                v /= 1024
            }
            return "$bytes B"
        }
    }
}

// ----------------------------------------------------------------------------- Device

class DevicePanel(act: Activity) : PcPanel(act) {
    override val title = "Device"
    private val list = column()
    override val view: View = scroll(list)
    private val handler = Handler(Looper.getMainLooper())
    private val poll = object : Runnable {
        override fun run() {
            send("pc.info_request")
            handler.postDelayed(this, 5000)
        }
    }

    init { list.addView(text("Loading PC info…", color = Ui.MUTED)) }

    override fun onShow() { handler.removeCallbacks(poll); handler.post(poll) }
    override fun onHide() { handler.removeCallbacks(poll) }
    override fun onDenied(message: String) = explain(list, message)

    override fun onMessage(type: String, p: JSONObject) {
        if (type != "pc.info") return
        list.removeAllViews()
        val head = card()
        head.addView(text(p.optString("hostname"), 20f, bold = true))
        head.addView(text("${p.optString("os")} · ${p.optString("desktop")} (${p.optString("session")})", 13f, color = Ui.TEXT2))
        head.addView(text("Kernel ${p.optString("kernel")} · ${p.optString("arch")} · user ${p.optString("user")}", 12f, color = Ui.MUTED))
        list.addView(head)

        val cores = p.optInt("cpu_cores")
        val cpuPct = p.optDouble("cpu_pct", 0.0)
        stat("CPU", "${p.optString("cpu_model")}\n$cores cores · ${"%.0f".format(cpuPct)}% in use", cpuPct.toInt())

        val ramT = p.optLong("ram_total"); val ramF = p.optLong("ram_free")
        if (ramT > 0) stat("Memory (RAM)", "${human(ramT - ramF)} used of ${human(ramT)}", ((ramT - ramF) * 100 / ramT).toInt())
        val dT = p.optLong("disk_total"); val dF = p.optLong("disk_free")
        if (dT > 0) stat("Storage", "${human(dT - dF)} used of ${human(dT)} · ${human(dF)} free", ((dT - dF) * 100 / dT).toInt())
        if (p.has("battery_pct")) {
            stat("Battery", p.optString("battery_status"), p.optInt("battery_pct"), good = true)
        }
        val up = p.optLong("uptime_s")
        stat("Uptime", "${up / 86400}d ${(up % 86400) / 3600}h ${(up % 3600) / 60}m · load ${p.optJSONArray("load_avg")?.optDouble(0) ?: 0.0}", null)
        network(p)
    }

    /** Wi-Fi network + every real adapter with its IP and MAC addresses. */
    private fun network(p: JSONObject) {
        list.addView(section("Network"))
        if (p.has("wifi_ssid")) stat("Wi-Fi network", "${p.optString("wifi_ssid")} · ${p.optInt("wifi_signal_pct")}% signal",
            p.optInt("wifi_signal_pct"))
        val ifaces = p.optJSONArray("interfaces") ?: JSONArray()
        for (i in 0 until ifaces.length()) {
            val f = ifaces.getJSONObject(i)
            val wifi = f.optString("kind") == "wifi"
            val c = card()
            c.addView(text("${if (wifi) "Wi-Fi" else "Ethernet"} · ${f.optString("name")} (${f.optString("state")})", 15f, bold = true))
            c.addView(text("${if (wifi) "Wi-Fi MAC" else "MAC"}: ${f.optString("mac")}", 14f,
                color = if (wifi) ACCENT else Ui.TEXT2).apply { setTextIsSelectable(true) })
            val addrs = f.optJSONArray("addresses") ?: JSONArray()
            for (j in 0 until addrs.length()) {
                val a = addrs.getJSONObject(j)
                c.addView(text("${if (a.optString("family") == "inet") "IPv4" else "IPv6"}: ${a.optString("address")}", 13f,
                    color = Ui.TEXT2).apply { setTextIsSelectable(true) })
            }
            if (addrs.length() == 0) c.addView(text("Not connected", 13f, color = Ui.MUTED))
            list.addView(c)
        }
    }

    private fun stat(title: String, detail: String, pct: Int?, good: Boolean = false) {
        val c = card()
        val head = row()
        head.addView(weighted(text(title, 15f, bold = true)))
        if (pct != null) head.addView(text("$pct%", 15f, bold = true, color = if (good) Ui.GREEN else Ui.usageColor(pct)))
        c.addView(head)
        c.addView(text(detail, 13f, color = Ui.TEXT2))
        if (pct != null) c.addView(bar(pct, if (good) Ui.GREEN else Ui.usageColor(pct)))
        list.addView(c)
    }
}

// ----------------------------------------------------------------------------- Files

class FilesPanel(act: Activity) : PcPanel(act) {
    override val title = "Files"
    private var path = "/"
    private val pathLabel = text("PC › Home", 15f, bold = true)
    private val status = text("", 12f, color = Ui.MUTED)
    private val list = column(0)
    override val view: View

    init {
        val root = column()
        val top = row()
        top.addView(button("⬆ Up") { if (path != "/") load(path.substringBeforeLast('/').ifEmpty { "/" }) })
        top.addView(weighted(pathLabel).apply { setPadding(dp(8), 0, 0, 0) })
        top.addView(button("Send to PC", primary = true) { pick() })
        root.addView(top)
        root.addView(status)
        root.addView(list)
        view = scroll(root)
    }

    override fun onShow() = load(path)

    override fun onDenied(message: String) {
        status.text = message
        list.removeAllViews()
    }

    private fun load(p: String) {
        if (send("pcfiles.list", JSONObject().put("path", p))) status.text = "Loading…"
    }

    override fun onMessage(type: String, p: JSONObject) {
        when (type) {
            "pc.files" -> show(p)
            "pc.file_saved" -> {
                status.text = "Saved to phone: ${p.optString("path")}"
                toast("Downloaded ${p.optString("filename")}")
            }
            "pc.status" -> status.text = p.optString("message")
        }
    }

    private fun show(p: JSONObject) {
        path = p.optString("path", "/")
        pathLabel.text = "PC › Home" + if (path == "/") "" else path.replace("/", " › ")
        status.text = p.optString("error").ifEmpty {
            if (p.has("free")) "${human(p.optLong("free"))} free on PC · tap a file to download it" else ""
        }
        list.removeAllViews()
        val entries = p.optJSONArray("entries") ?: JSONArray()
        for (i in 0 until entries.length()) {
            val e = entries.getJSONObject(i)
            val name = e.optString("name")
            val isDir = e.optBoolean("is_dir")
            val c = card()
            c.addView(text((if (isDir) "📁  " else "📄  ") + name, 15f))
            c.addView(text(if (isDir) "${e.optLong("size")} items" else human(e.optLong("size")), 12f, color = Ui.MUTED))
            val full = (if (path == "/") "" else path) + "/" + name
            c.setOnClickListener {
                if (isDir) load(full)
                else if (send("pcfiles.get", JSONObject().put("path", full))) {
                    status.text = "Downloading $name to Download/PhoneBridge…"
                }
            }
            list.addView(c)
        }
        if (entries.length() == 0) list.addView(text("Empty folder", color = Ui.MUTED))
    }

    private fun pick() {
        val i = Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
            .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        @Suppress("DEPRECATION")
        act.startActivityForResult(i, REQUEST_PICK)
    }

    /** Called by MainActivity.onActivityResult with the files the user picked. */
    fun sendPicked(uris: List<Uri>) {
        val svc = BridgeService.instance ?: return
        for (uri in uris) {
            var name = "file"
            var size = -1L
            act.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use {
                if (it.moveToFirst()) { name = it.getString(0) ?: name; size = it.getLong(1) }
            }
            if (size < 0) continue
            status.text = "Sending $name to the PC (Downloads/PhoneBridge)…"
            val mime = act.contentResolver.getType(uri) ?: "application/octet-stream"
            Transfer.uploads.execute {   // not svc.worker: a few big files would stall everything else the phone answers
                val failure = runCatching {
                    Transfer.send(svc, act.contentResolver.openInputStream(uri)!!, name, size, mime)
                }.exceptionOrNull()
                svc.main.post {
                    status.text = if (failure == null) "Sent $name to the PC's Downloads/PhoneBridge"
                                  else "Couldn't send $name: ${failure.message ?: failure.javaClass.simpleName}"
                }
            }
        }
    }

    companion object { const val REQUEST_PICK = 42 }
}

// ----------------------------------------------------------------------------- Screen

class ScreenPanel(act: Activity) : PcPanel(act) {
    override val title = "Screen"
    private val status = text("Tap Start. The first time, the laptop asks to share its screen — turn ON " +
        "\"Allow remote interaction\" there, then click Share.", 12f, color = Ui.TEXT2)
    private val label = text("", 13f, bold = true).apply {
        gravity = Gravity.CENTER
        maxLines = 1
        ellipsize = android.text.TextUtils.TruncateAt.END
    }
    private val image = ImageView(act).apply {
        scaleType = ImageView.ScaleType.FIT_CENTER
        setBackgroundColor(Color.BLACK)
        contentDescription = "Live view of the PC screen. Touch to click, drag to scroll or move."
    }
    private var streaming = false
    private var noInput = false
    private val startBtn: Button
    private var full: FullScreenView? = null
    override val view: View

    init {
        val root = column(10)
        val top = row()
        startBtn = button("Start", primary = true) { toggle() }
        top.addView(startBtn)
        top.addView(button("⛶ Full screen") { enterFullScreen() })
        top.addView(weighted(label))
        root.addView(top)
        root.addView(status)
        root.addView(image, LinearLayout.LayoutParams(-1, 0, 1f).apply { topMargin = dp(6) })
        view = root.apply { setBackgroundColor(BG) }
        TouchControl(act, image) { input(it) }.attach()
    }

    private fun startStream(type: String, payload: JSONObject = JSONObject()): Boolean {
        if (!send(type, payload)) return false
        streaming = true
        startBtn.text = "Stop"
        BridgeService.pcFrameListener = { bmp ->
            image.setImageBitmap(bmp)
            full?.show(bmp)
        }
        act.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        return true
    }

    private fun toggle() {
        if (streaming) stop() else if (startStream("pc.screen_start")) status.text = "Starting…"
    }

    private fun stop(tellPc: Boolean = true) {
        if (tellPc) BridgeService.instance?.send("pc.screen_stop")
        BridgeService.pcFrameListener = null
        streaming = false
        startBtn.text = "Start"
        label.text = ""
        exitFullScreen()
        act.window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    override fun onHide() { if (streaming) stop() }

    override fun onDenied(message: String) {
        if (full != null) toast(message)
        // The PC is not streaming, and would treat a "stop" now as one more request to approve.
        if (streaming) stop(tellPc = false)
        status.text = message
    }

    /** Back button: leave full screen first. */
    fun handleBack(): Boolean {
        if (full == null) return false
        exitFullScreen()
        return true
    }

    override fun onMessage(type: String, p: JSONObject) {
        when (type) {
            "pc.screen_started" -> {
                label.text = "${p.optInt("width")}×${p.optInt("height")}"
                if (!noInput) status.text = "Tap = click · long-press = right-click · drag = move cursor · two fingers = scroll"
            }
            "pc.remote_status" -> {
                val state = p.optString("state")
                noInput = state == "no_input"
                status.text = p.optString("message")
                if (noInput && full != null) toast(p.optString("message"))
                if (state == "denied") stop()
            }
        }
    }

    // ------------------------------------------------------------- full screen

    private fun enterFullScreen() {
        if (!streaming && !startStream("pc.screen_start")) return
        if (full != null) return
        val f = FullScreenView(act, onExit = { exitFullScreen() }, input = { input(it) })
        (image.drawable as? android.graphics.drawable.BitmapDrawable)?.bitmap?.let { f.show(it) }
        if (noInput) toast(status.text.toString())
        full = f
        (act.window.decorView as ViewGroup).addView(f, ViewGroup.LayoutParams(-1, -1))
        @Suppress("DEPRECATION")
        act.window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
    }

    private fun exitFullScreen() {
        val f = full ?: return
        f.hideKeyboard()
        (act.window.decorView as ViewGroup).removeView(f)
        full = null
        @Suppress("DEPRECATION")
        act.window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_VISIBLE
    }
}

/**
 * The PC window filling the phone screen, controlled by touch, with floating buttons:
 * ⌨ opens the phone keyboard (typing goes live to the PC) and ✕ leaves full screen.
 */
@SuppressLint("ViewConstructor")
class FullScreenView(
    private val act: Activity,
    onExit: () -> Unit,
    private val input: (JSONObject) -> Unit,
) : FrameLayout(act) {
    private val image = ImageView(act).apply {
        scaleType = ImageView.ScaleType.FIT_CENTER
        setBackgroundColor(Color.BLACK)
        contentDescription = "Live view of the PC screen. Touch to click, drag to scroll or move."
    }
    private val keys = KeyCaptureView(act,
        onText = { t -> input(JSONObject().put("kind", "text").put("text", t)) },
        onKey = { k -> input(JSONObject().put("kind", "key").put("key", k).put("mods", JSONArray())) })
    private val keyboard = InAppKeyboard(act,
        onText = { t -> input(JSONObject().put("kind", "text").put("text", t)) },
        onKey = { k -> input(JSONObject().put("kind", "key").put("key", if (k == "Return") "enter" else "backspace")
            .put("mods", JSONArray())) },
        onHide = { hideKeyboard() }).apply { visibility = View.GONE }

    init {
        setBackgroundColor(Color.BLACK)
        addView(image, LayoutParams(-1, -1))
        addView(keys, LayoutParams(1, 1))
        addView(keyboard, LayoutParams(-1, -2, Gravity.BOTTOM))
        TouchControl(act, image, input).attach()
        val d = act.resources.displayMetrics.density
        val corner = LinearLayout(act).apply { orientation = LinearLayout.VERTICAL }
        corner.addView(fab("⌨") { toggleKeyboard() }, LinearLayout.LayoutParams((52 * d).toInt(), (52 * d).toInt()).apply {
            bottomMargin = (10 * d).toInt()
        })
        corner.addView(fab("✕", onExit), LinearLayout.LayoutParams((52 * d).toInt(), (52 * d).toInt()))
        // Top-right, so they never sit on the keyboard.
        addView(corner, LayoutParams(-2, -2, Gravity.END or Gravity.TOP).apply {
            rightMargin = (16 * d).toInt(); topMargin = (16 * d).toInt()
        })
    }

    private fun fab(text: String, onClick: () -> Unit) = Button(act).apply {
        this.text = text
        textSize = 18f
        setTextColor(Color.WHITE)
        setPadding(0, 0, 0, 0)
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(Color.argb(170, 47, 111, 237))
        }
        setOnClickListener { onClick() }
    }

    fun show(bmp: Bitmap) = image.setImageBitmap(bmp)

    private fun toggleKeyboard() {
        if (keyboard.visibility == View.VISIBLE) hideKeyboard() else {
            keyboard.visibility = View.VISIBLE
            keys.requestFocus()  // hardware keyboards too
        }
    }

    fun hideKeyboard() {
        keyboard.visibility = View.GONE
    }
}

/**
 * Touch on the picture of the PC screen -> pointer actions there:
 * tap = left click, long-press = right click, one-finger drag = move the cursor only
 * (no button held, so nothing gets selected), two-finger drag = scroll.
 */
class TouchControl(private val act: Activity, private val image: ImageView, private val input: (JSONObject) -> Unit) {
    private val handler = Handler(Looper.getMainLooper())
    private val slop = 10 * act.resources.displayMetrics.density

    /** Touch point -> [0,1] position in the PC window, through the ImageView's fit matrix. */
    private fun toPc(x: Float, y: Float): Pair<Float, Float>? {
        val d = image.drawable ?: return null
        val inv = Matrix()
        if (!image.imageMatrix.invert(inv)) return null
        val pts = floatArrayOf(x - image.paddingLeft, y - image.paddingTop)
        inv.mapPoints(pts)
        val fx = pts[0] / d.intrinsicWidth
        val fy = pts[1] / d.intrinsicHeight
        return if (fx in -0.02f..1.02f && fy in -0.02f..1.02f) fx.coerceIn(0f, 1f) to fy.coerceIn(0f, 1f) else null
    }

    private fun abs(x: Float, y: Float, down: Boolean? = null) {
        val (fx, fy) = toPc(x, y) ?: return
        val o = JSONObject().put("kind", "abs").put("x", fx).put("y", fy)
        if (down != null) o.put("down", down)
        input(o)
    }

    @SuppressLint("ClickableViewAccessibility")
    fun attach() {
        var downX = 0f; var downY = 0f
        var longFired = false; var dragging = false; var multi = false
        var lastScrollY = 0f; var scrollAcc = 0f; var lastMoveSent = 0L
        val longPress = Runnable {
            longFired = true
            toPc(downX, downY)?.let { (fx, fy) ->
                input(JSONObject().put("kind", "tap_abs").put("x", fx).put("y", fy).put("button", "right"))
            }
        }
        image.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = e.x; downY = e.y
                    longFired = false; dragging = false; multi = false; scrollAcc = 0f
                    handler.postDelayed(longPress, 550)
                }
                MotionEvent.ACTION_POINTER_DOWN -> {
                    handler.removeCallbacks(longPress)
                    dragging = false
                    multi = true
                    abs(e.getX(0), e.getY(0))  // scroll whatever is under the fingers
                    lastScrollY = (0 until e.pointerCount).map { e.getY(it) }.average().toFloat()
                }
                MotionEvent.ACTION_MOVE -> {
                    if (multi && e.pointerCount >= 2) {
                        val y = (0 until e.pointerCount).map { e.getY(it) }.average().toFloat()
                        scrollAcc += y - lastScrollY
                        lastScrollY = y
                        val stepPx = 28 * act.resources.displayMetrics.density
                        while (kotlin.math.abs(scrollAcc) >= stepPx) {
                            input(JSONObject().put("kind", "scroll").put("dy", if (scrollAcc < 0) 1 else -1))
                            scrollAcc -= if (scrollAcc > 0) stepPx else -stepPx
                        }
                    } else if (!multi && !longFired) {
                        if (!dragging && (kotlin.math.abs(e.x - downX) > slop || kotlin.math.abs(e.y - downY) > slop)) {
                            handler.removeCallbacks(longPress)
                            dragging = true  // pointer follows the finger; no button press
                        }
                        if (dragging && e.eventTime - lastMoveSent >= 30) {
                            lastMoveSent = e.eventTime
                            abs(e.x, e.y)
                        }
                    }
                }
                MotionEvent.ACTION_UP -> {
                    handler.removeCallbacks(longPress)
                    when {
                        dragging -> abs(e.x, e.y)  // just leave the cursor here
                        !multi && !longFired -> toPc(e.x, e.y)?.let { (fx, fy) ->
                            input(JSONObject().put("kind", "tap_abs").put("x", fx).put("y", fy))
                        }
                    }
                    dragging = false
                }
                MotionEvent.ACTION_CANCEL -> {
                    handler.removeCallbacks(longPress)
                    dragging = false
                }
            }
            true
        }
    }
}

// ----------------------------------------------------------------------------- Apps

class AppsPanel(act: Activity) : PcPanel(act) {
    override val title = "Apps"
    private val search = EditText(act).apply {
        hint = "Search PC apps"; isSingleLine = true; Ui.styleField(this)
        showSoftInputOnFocus = false  // PhoneBridge's own keyboard, not the phone's
    }
    private val keyboard = InAppKeyboard(act,
        onText = { t -> search.text.replace(search.selectionStart.coerceAtLeast(0), search.selectionEnd.coerceAtLeast(0), t) },
        onKey = { k ->
            if (k == "BackSpace") {
                val a = search.selectionStart; val b = search.selectionEnd
                if (a != b) search.text.delete(minOf(a, b), maxOf(a, b))
                else if (a > 0) search.text.delete(a - 1, a)
            } else hideKeyboard()
        },
        onHide = { hideKeyboard() }).apply { visibility = View.GONE }
    private val grid = column(0)
    private val status = text("", 12f, color = Ui.MUTED)
    private var apps = JSONArray()
    override val view: View

    init {
        val root = column()
        val r = row()
        r.addView(weighted(search))
        r.addView(button("↻") { load() })
        root.addView(r)
        root.addView(status)
        root.addView(grid)
        view = FrameLayout(act).apply {
            addView(scroll(root), FrameLayout.LayoutParams(-1, -1))
            addView(keyboard, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        }
        search.setOnClickListener { keyboard.visibility = View.VISIBLE }
        search.setOnFocusChangeListener { _, has -> if (has) keyboard.visibility = View.VISIBLE }
        search.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun afterTextChanged(s: Editable?) = render()
        })
    }

    override fun onShow() { if (apps.length() == 0) load() }

    override fun onDenied(message: String) { status.text = message }

    override fun onHide() = hideKeyboard()

    private fun hideKeyboard() {
        keyboard.visibility = View.GONE
        search.clearFocus()
    }

    private fun load() { if (send("pc.apps_request")) status.text = "Loading PC apps…" }

    override fun onMessage(type: String, p: JSONObject) {
        when (type) {
            "pc.apps" -> { apps = p.optJSONArray("apps") ?: JSONArray(); status.text = "${apps.length()} apps · tap to open on the PC"; render() }
            "pc.status" -> toast(p.optString("message"))
        }
    }

    private fun render() {
        grid.removeAllViews()
        val q = search.text.toString().trim().lowercase()
        var line: LinearLayout? = null
        var n = 0
        for (i in 0 until apps.length()) {
            val a = apps.getJSONObject(i)
            if (q.isNotEmpty() && !a.optString("name").lowercase().contains(q)) continue
            if (n % 4 == 0) { line = row(); grid.addView(line) }
            val cell = LinearLayout(act).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER_HORIZONTAL
                setPadding(dp(4), dp(10), dp(4), dp(10))
            }
            val icon = ImageView(act).apply { importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO }   // the name below says it
            val b64 = a.optString("icon")
            if (b64.isNotEmpty()) runCatching {
                val raw = Crypto.unb64(b64)
                icon.setImageBitmap(android.graphics.BitmapFactory.decodeByteArray(raw, 0, raw.size))
            }
            cell.addView(icon, LinearLayout.LayoutParams(dp(44), dp(44)))
            cell.addView(text(a.optString("name"), 11f).apply { gravity = Gravity.CENTER; maxLines = 2 })
            val id = a.optString("id")
            cell.contentDescription = "Open ${a.optString("name")} on the PC"
            cell.setOnClickListener { if (send("pc.app_launch", JSONObject().put("id", id))) toast("Opening ${a.optString("name")}…") }
            line!!.addView(weighted(cell))
            n++
        }
        // pad the last row so cells keep equal widths
        while (n % 4 != 0) { line?.addView(weighted(View(act))); n++ }
    }
}

// ----------------------------------------------------------------------------- Keyboard capture

/**
 * A text-less view the soft keyboard types into: every committed character, backspace,
 * Enter etc. is reported immediately instead of being collected in a text box.
 */
@SuppressLint("ViewConstructor")
class KeyCaptureView(ctx: Context, private val onText: (String) -> Unit, private val onKey: (String) -> Unit) : View(ctx) {
    private var composing = ""

    init {
        isFocusable = true
        isFocusableInTouchMode = true
    }

    // Not a text editor: PhoneBridge uses its own InAppKeyboard, so the phone's keyboard
    // must never pop up for this view. It still receives hardware-keyboard keys.
    override fun onCheckIsTextEditor() = false

    /** Hardware keyboards (and injected key events) arrive here, not via the IME. */
    override fun onKeyDown(keyCode: Int, event: android.view.KeyEvent): Boolean {
        if (keyCode == android.view.KeyEvent.KEYCODE_BACK) return super.onKeyDown(keyCode, event)
        return handleKey(event)
    }

    private fun handleKey(event: android.view.KeyEvent): Boolean {
        when (event.keyCode) {
            android.view.KeyEvent.KEYCODE_DEL -> onKey("BackSpace")
            android.view.KeyEvent.KEYCODE_FORWARD_DEL -> onKey("Delete")
            android.view.KeyEvent.KEYCODE_ENTER, android.view.KeyEvent.KEYCODE_NUMPAD_ENTER -> onKey("Return")
            android.view.KeyEvent.KEYCODE_TAB -> onKey("Tab")
            android.view.KeyEvent.KEYCODE_ESCAPE -> onKey("Escape")
            android.view.KeyEvent.KEYCODE_DPAD_LEFT -> onKey("Left")
            android.view.KeyEvent.KEYCODE_DPAD_RIGHT -> onKey("Right")
            android.view.KeyEvent.KEYCODE_DPAD_UP -> onKey("Up")
            android.view.KeyEvent.KEYCODE_DPAD_DOWN -> onKey("Down")
            else -> {
                val ch = event.unicodeChar
                if (ch == 0) return false
                emitText(String(Character.toChars(ch)))
            }
        }
        return true
    }

    /** Keyboards may rewrite the word being composed; send only the difference. */
    private fun replaceComposing(new: String) {
        var common = 0
        while (common < composing.length && common < new.length && composing[common] == new[common]) common++
        repeat(composing.length - common) { onKey("BackSpace") }
        val added = new.substring(common)
        if (added.isNotEmpty()) emitText(added)
        composing = new
    }

    private fun emitText(t: String) {
        // Newlines from the keyboard mean Enter.
        val parts = t.split("\n")
        parts.forEachIndexed { i, part ->
            if (part.isNotEmpty()) onText(part)
            if (i < parts.size - 1) onKey("Return")
        }
    }

    override fun onCreateInputConnection(out: android.view.inputmethod.EditorInfo): android.view.inputmethod.InputConnection {
        // A visible-password field: a full keyboard with numbers/symbols, but no prediction
        // or autocorrect, so every key press is sent as typed.
        out.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD or
            InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        out.imeOptions = android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI or
            android.view.inputmethod.EditorInfo.IME_FLAG_NO_FULLSCREEN or android.view.inputmethod.EditorInfo.IME_ACTION_NONE
        return object : android.view.inputmethod.BaseInputConnection(this, false) {
            override fun commitText(text: CharSequence, newCursorPosition: Int): Boolean {
                replaceComposing(text.toString())
                composing = ""
                return true
            }

            override fun setComposingText(text: CharSequence, newCursorPosition: Int): Boolean {
                replaceComposing(text.toString())
                return true
            }

            override fun finishComposingText(): Boolean {
                composing = ""
                return true
            }

            override fun deleteSurroundingText(beforeLength: Int, afterLength: Int): Boolean {
                repeat(beforeLength) { onKey("BackSpace") }
                repeat(afterLength) { onKey("Delete") }
                return true
            }

            override fun performEditorAction(actionCode: Int): Boolean {
                onKey("Return")
                return true
            }

            override fun sendKeyEvent(event: android.view.KeyEvent): Boolean {
                if (event.action == android.view.KeyEvent.ACTION_DOWN) handleKey(event)
                return true
            }
        }
    }
}

// ----------------------------------------------------------------------------- Ports

class PortsPanel(act: Activity) : PcPanel(act) {
    override val title = "Ports"
    private val list = column()
    override val view: View = scroll(list)

    init { list.addView(text("Loading…", color = Ui.MUTED)) }

    override fun onShow() { send("pc.ports_request") }
    override fun onDenied(message: String) = explain(list, message)

    override fun onMessage(type: String, p: JSONObject) {
        if (type != "pc.ports") return
        list.removeAllViews()
        val top = row()
        top.addView(weighted(text("Open ports", 18f, bold = true)))
        top.addView(button("↻ Refresh") { send("pc.ports_request") })
        list.addView(top)
        list.addView(text("IP and MAC addresses are on the Device tab.", 12f, color = Ui.MUTED).apply { setPadding(0, 0, 0, dp(6)) })
        val ports = p.optJSONArray("ports") ?: JSONArray()
        for (i in 0 until ports.length()) {
            val o = ports.getJSONObject(i)
            val c = card()
            val svc = o.optString("service").ifEmpty { "unknown service" }
            c.addView(text("${o.optInt("port")}/${o.optString("proto")}  ·  $svc", 15f, bold = true))
            val where = if (o.optBoolean("exposed")) "open to the network (${o.optString("address")})" else "only this PC (${o.optString("address")})"
            val proc = o.optString("process").ifEmpty { "system service" }
            c.addView(text("$proc · $where", 12f, color = if (o.optBoolean("exposed")) Ui.WARN else Ui.MUTED))
            list.addView(c)
        }
    }
}

// ----------------------------------------------------------------------------- Servers

class ServersPanel(act: Activity) : PcPanel(act) {
    override val title = "Servers"
    private val list = column()
    override val view: View = scroll(list)

    init { list.addView(text("Loading…", color = Ui.MUTED)) }

    override fun onShow() { send("pc.servers_request") }
    override fun onDenied(message: String) = explain(list, message)

    override fun onMessage(type: String, p: JSONObject) {
        when (type) {
            "pc.status" -> toast(p.optString("message"))
            "pc.servers" -> render(p)
        }
    }

    private fun render(p: JSONObject) {
        list.removeAllViews()
        val top = row()
        top.addView(weighted(text("Docker containers", 18f, bold = true)))
        top.addView(button("↻ Refresh") { send("pc.servers_request") })
        list.addView(top)

        val docker = p.optJSONObject("docker") ?: JSONObject()
        if (!docker.optBoolean("available")) {
            list.addView(text(docker.optString("error", "Docker not available"), 13f, color = Ui.MUTED))
        }
        val containers = docker.optJSONArray("containers") ?: JSONArray()
        if (docker.optBoolean("available") && containers.length() == 0) list.addView(text("No containers", 13f, color = Ui.MUTED))
        for (i in 0 until containers.length()) {
            val c = containers.getJSONObject(i)
            val running = c.optString("state") == "running"
            val card = card()
            card.addView(text((if (running) "● " else "○ ") + c.optString("name"), 15f, bold = true,
                color = if (running) Ui.GREEN else Ui.TEXT2))
            card.addView(text("${c.optString("image")} · ${c.optString("status")}", 12f, color = Ui.TEXT2))
            val ports = c.optString("ports")
            if (ports.isNotEmpty()) card.addView(text("Ports: $ports", 12f, color = Ui.MUTED))
            if (c.optString("project").isNotEmpty()) card.addView(text("Compose project: ${c.optString("project")}", 12f, color = Ui.MUTED))
            val r = row()
            val id = c.optString("id")
            val name = c.optString("name")
            if (running) {
                r.addView(button("Stop") { confirm("Stop $name?") { action(id, "stop") } })
                r.addView(button("Restart") { confirm("Restart $name?") { action(id, "restart") } })
            } else {
                r.addView(button("Start", primary = true) { action(id, "start") })
            }
            card.addView(r)
            list.addView(card)
        }

        list.addView(section("System services"))
        val services = p.optJSONArray("services") ?: JSONArray()
        if (services.length() == 0) list.addView(text("No server services found", 13f, color = Ui.MUTED))
        for (i in 0 until services.length()) {
            val sv = services.getJSONObject(i)
            val running = sv.optString("active") == "active"
            val card = card()
            card.addView(text((if (running) "● " else "○ ") + sv.optString("name") + "  —  " +
                (if (running) "running" else sv.optString("active")), 15f, bold = true,
                color = if (running) Ui.GREEN else Ui.TEXT2))
            card.addView(text(sv.optString("description"), 12f, color = Ui.MUTED))
            list.addView(card)
        }
        list.addView(text("Starting/stopping system services needs the PC's admin password, so it's view-only here.",
            11f, color = Ui.MUTED))

    }

    private fun action(id: String, action: String) {
        if (send("pc.server_action", JSONObject().put("kind", "docker").put("id", id).put("action", action))) {
            toast("${action.replaceFirstChar { it.uppercase() }}ing…")
        }
    }

    private fun confirm(message: String, onYes: () -> Unit) {
        android.app.AlertDialog.Builder(act).setMessage(message)
            .setPositiveButton("Yes") { _, _ -> onYes() }
            .setNegativeButton("Cancel", null).show()
    }
}

// ----------------------------------------------------------------------------- Terminal

/**
 * A shell on the PC. The phone keyboard types straight into it (so bash's own editing,
 * history and Tab completion work); output is shown as plain text. Full-screen programs
 * (vim, htop) aren't supported — the PC runs the shell with TERM=dumb.
 */
class TerminalPanel(act: Activity) : PcPanel(act) {
    private companion object {
        /** What a screen reader says for the symbol-only extra keys. */
        val SPOKEN_KEYS = mapOf("↑" to "Up arrow", "↓" to "Down arrow", "←" to "Left arrow", "→" to "Right arrow",
            "ESC" to "Escape", "PGUP" to "Page up", "PGDN" to "Page down", "/" to "Slash", "-" to "Dash")
    }

    override val title = "Terminal"
    private val screen = TerminalBuffer()
    private val output = TextView(act).apply {
        typeface = Typeface.MONOSPACE
        textSize = 12f
        setTextColor(Color.parseColor("#D7F5DD"))
        // Not selectable/focusable: focus must stay on the key-capture view or the
        // phone keyboard disconnects and keys get lost.
        isFocusable = false
        setPadding(dp(8), dp(6), dp(8), dp(6))
    }
    private val scroller = ScrollView(act).apply {
        background = Ui.rounded(Color.parseColor("#090D13"), dp(12).toFloat(), Ui.STROKE, 1)
        isFocusable = false
        descendantFocusability = ViewGroup.FOCUS_BLOCK_DESCENDANTS
        addView(output)
    }
    // Declared before init: the key bar built there registers its CTRL/ALT buttons here.
    /** 0 = off, 1 = applies to the next key only, 2 = locked on (double-tap / long-press). */
    private var ctrl = 0
    private var alt = 0
    private val modButtons = HashMap<String, Button>()
    private val repeatHandler = Handler(Looper.getMainLooper())

    private val keys = KeyCaptureView(act, onText = { t -> type(t) }, onKey = { k -> key(k) })
    private val keyboard = InAppKeyboard(act, onText = { t -> type(t) }, onKey = { k -> key(k) },
        onHide = { hideKeyboard() })
    private val keyArea = LinearLayout(act).apply { orientation = LinearLayout.VERTICAL }
    private var opened = false
    override val view: View

    init {
        // The terminal fills the tab; Termux's extra keys and PhoneBridge's keyboard float
        // over its bottom, see-through, so the output stays visible behind them.
        for (line in listOf(
            listOf("ESC", "/", "-", "HOME", "↑", "END", "PGUP"),
            listOf("TAB", "CTRL", "ALT", "←", "↓", "→", "PGDN"),
        )) {
            val r = row().apply { setBackgroundColor(InAppKeyboard.BACKDROP); setPadding(dp(4), dp(2), dp(4), 0) }
            for (label in line) r.addView(extraKey(label), LinearLayout.LayoutParams(0, dp(if (act.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_PORTRAIT) 36 else 22), 1f).apply {
                setMargins(dp(2), 0, dp(2), dp(2))
            })
            keyArea.addView(r)
        }
        keyArea.addView(keyboard)
        keyboard.visibility = View.GONE
        // Keep the newest output line just above the overlay.
        keyArea.addOnLayoutChangeListener { _, _, top, _, bottom, _, _, _, _ ->
            output.setPadding(dp(8), dp(6), dp(8), (bottom - top) + dp(6))
            scroller.post { scroller.scrollTo(0, output.height) }
        }
        val frame = FrameLayout(act).apply {
            setPadding(dp(4), dp(4), dp(4), dp(4))
            addView(scroller, FrameLayout.LayoutParams(-1, -1))
            addView(keyArea, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
            addView(keys, FrameLayout.LayoutParams(1, 1))
        }
        scroller.setOnClickListener { onTerminalTapped() }
        output.setOnClickListener { onTerminalTapped() }
        view = frame.apply { setBackgroundColor(BG) }
        render()
    }

    // ------------------------------------------------------------- Termux-style extra keys

    @SuppressLint("ClickableViewAccessibility")
    private fun extraKey(label: String): Button = Button(act).apply {
        text = label
        isAllCaps = false
        stateListAnimator = null
        textSize = 11f
        typeface = Typeface.MONOSPACE
        setTextColor(Color.WHITE)
        setShadowLayer(3f, 0f, 1f, Color.argb(160, 0, 0, 0))
        minHeight = 0; minimumHeight = 0; minWidth = 0; minimumWidth = 0
        setPadding(0, 0, 0, 0)
        background = Ui.rounded(InAppKeyboard.KEY, dp(7).toFloat(), Color.argb(40, 255, 255, 255), 1)
        contentDescription = SPOKEN_KEYS[label] ?: label
        // Some keys act on touch-down/up (key repeat), which TalkBack's double-tap never produces: route its click here.
        isClickable = true
        accessibilityDelegate = object : View.AccessibilityDelegate() {
            override fun performAccessibilityAction(host: View, action: Int, args: Bundle?): Boolean {
                if (action == android.view.accessibility.AccessibilityNodeInfo.ACTION_CLICK && label != "CTRL" && label != "ALT") {
                    pressExtra(label); return true
                }
                return super.performAccessibilityAction(host, action, args)
            }
        }
        when (label) {
            "CTRL", "ALT" -> {
                modButtons[label] = this
                var lastTap = 0L
                setOnClickListener {
                    val now = System.currentTimeMillis()
                    val double = now - lastTap < 350
                    lastTap = now
                    setMod(label, when {
                        double -> 2
                        mod(label) == 0 -> 1
                        else -> 0
                    })
                }
                setOnLongClickListener { setMod(label, 2); true }
            }
            "↑", "↓", "←", "→", "PGUP", "PGDN" -> {
                // Held arrow/page keys repeat, like Termux.
                val repeat = object : Runnable {
                    override fun run() {
                        pressExtra(label, keepMods = true)
                        repeatHandler.postDelayed(this, 60)
                    }
                }
                setOnTouchListener { v, e ->
                    when (e.actionMasked) {
                        MotionEvent.ACTION_DOWN -> {
                            v.isPressed = true
                            pressExtra(label, keepMods = true)
                            repeatHandler.postDelayed(repeat, 400)
                        }
                        MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                            v.isPressed = false
                            repeatHandler.removeCallbacks(repeat)
                            consumeOneShot()
                        }
                    }
                    true
                }
            }
            else -> setOnClickListener { pressExtra(label) }
        }
    }

    private fun mod(name: String) = if (name == "CTRL") ctrl else alt

    private fun setMod(name: String, state: Int) {
        if (name == "CTRL") ctrl = state else alt = state
        val b = modButtons[name] ?: return
        b.background = Ui.rounded(when (state) {
            0 -> InAppKeyboard.KEY
            1 -> InAppKeyboard.KEY_ACTIVE
            else -> Ui.ACCENT
        }, dp(7).toFloat(), Color.argb(40, 255, 255, 255), 1)
    }

    /** One-shot modifiers switch off after a key; locked ones stay on. */
    private fun consumeOneShot() {
        if (ctrl == 1) setMod("CTRL", 0)
        if (alt == 1) setMod("ALT", 0)
    }

    private fun pressExtra(label: String, keepMods: Boolean = false) {
        when (label) {
            "ESC" -> key("Escape", keepMods)
            "TAB" -> key("Tab", keepMods)
            "HOME" -> key("Home", keepMods)
            "END" -> key("End", keepMods)
            "PGUP" -> key("Page_Up", keepMods)
            "PGDN" -> key("Page_Down", keepMods)
            "↑" -> key("Up", keepMods)
            "↓" -> key("Down", keepMods)
            "←" -> key("Left", keepMods)
            "→" -> key("Right", keepMods)
            else -> type(label)  // "/" and "-"
        }
    }

    private fun onTerminalTapped() {
        if (!opened) { screen.clear(); render(); sendOpen(new = true) }
        showKeyboard()
    }

    // Keys typed with no session open go nowhere: the PC would only refuse each one again.
    private fun write(data: String) { if (opened) send("pc.term_input", JSONObject().put("data", data)) }

    override fun onDenied(message: String) {
        opened = false
        screen.clear()
        screen.feed(message)
        render()
    }

    /** Characters from the phone keyboard (or "/" "-"), with CTRL/ALT applied like Termux. */
    private fun type(t: String) {
        val out = StringBuilder()
        for (raw in t.replace("\n", "\r")) {
            var ch = raw.toString()
            if (ctrl != 0) {
                val c = raw
                ch = when {
                    c.isLetter() && c.code < 128 -> (c.uppercaseChar().code - 64).toChar().toString()
                    c in "@[\\]^_" -> (c.code and 0x1f).toChar().toString()
                    c == ' ' || c == '2' -> "\u0000"
                    c == '?' || c == '8' -> "\u007f"
                    c == '3' -> "\u001b"
                    c == '4' -> "\u001c"
                    c == '5' -> "\u001d"
                    c == '6' -> "\u001e"
                    c == '7' || c == '-' || c == '/' -> "\u001f"
                    else -> ch
                }
            }
            if (alt != 0) ch = "\u001b" + ch
            out.append(ch)
            consumeOneShot()
        }
        write(out.toString())
    }

    private fun key(k: String, keepMods: Boolean = false) {
        // xterm modifier parameter: 1 + Shift(1) + Alt(2) + Ctrl(4)
        val m = 1 + (if (alt != 0) 2 else 0) + (if (ctrl != 0) 4 else 0)
        fun csi(final: Char) = if (m > 1) "\u001b[1;$m$final" else "\u001b[$final"
        fun tilde(n: Int) = if (m > 1) "\u001b[$n;$m~" else "\u001b[$n~"
        val seq = when (k) {
            "BackSpace" -> if (ctrl != 0) "\u0008" else if (alt != 0) "\u001b\u007f" else "\u007f"
            "Return" -> if (alt != 0) "\u001b\r" else "\r"
            "Tab" -> if (alt != 0) "\u001b\t" else "\t"
            "Escape" -> "\u001b"
            "Delete" -> tilde(3)
            "Up" -> csi('A')
            "Down" -> csi('B')
            "Right" -> csi('C')
            "Left" -> csi('D')
            "Home" -> csi('H')
            "End" -> csi('F')
            "Page_Up" -> tilde(5)
            "Page_Down" -> tilde(6)
            else -> ""
        }
        if (seq.isNotEmpty()) write(seq)
        if (!keepMods) consumeOneShot()
    }

    private fun size(): Pair<Int, Int> {
        val charW = output.paint.measureText("M").coerceAtLeast(1f)
        val portrait = act.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_PORTRAIT
        val w = (scroller.width.takeIf { it > 0 } ?: (act.resources.displayMetrics.widthPixels * (if (portrait) 1f else 0.75f)).toInt()) - dp(16)
        val h = (scroller.height.takeIf { it > 0 } ?: act.resources.displayMetrics.heightPixels / 2)
        return (w / charW).toInt().coerceIn(20, 300) to (h / output.lineHeight).coerceIn(5, 200)
    }

    private fun sendOpen(new: Boolean = false) {
        val (cols, rows) = size()
        if (send("pc.term_open", JSONObject().put("cols", cols).put("rows", rows).put("new", new))) opened = true
    }

    override fun onShow() {
        scroller.post {
            if (!opened) sendOpen() else {
                val (cols, rows) = size()
                send("pc.term_open", JSONObject().put("cols", cols).put("rows", rows))  // replays recent output
                screen.clear()
            }
            showKeyboard()
        }
    }

    override fun onHide() = hideKeyboard()

    private fun showKeyboard() {
        keyboard.visibility = View.VISIBLE
        keys.requestFocus()  // hardware keyboards too
    }

    private fun hideKeyboard() {
        keyboard.visibility = View.GONE
    }

    override fun onMessage(type: String, p: JSONObject) {
        when (type) {
            "pc.term_output" -> {
                if (p.optBoolean("replay")) screen.clear()
                screen.feed(p.optString("data"))
                render()
            }
            "pc.term_exit" -> {
                screen.feed("\n[shell exited — tap New session]\n")
                opened = false
                render()
            }
        }
    }

    private fun render() {
        output.text = screen.text()
        // scrollTo, not fullScroll: fullScroll moves focus and would cut the keyboard off.
        scroller.post { scroller.scrollTo(0, output.height) }
    }
}

/**
 * Minimal line-based terminal model: enough for a shell with TERM=dumb (and the common
 * cursor/erase sequences readline still emits). Unknown escape sequences are dropped.
 */
class TerminalBuffer(private val maxLines: Int = 2000) {
    private val lines = ArrayList<StringBuilder>().apply { add(StringBuilder()) }
    private var col = 0
    private var esc: StringBuilder? = null

    private val line get() = lines.last()

    fun clear() {
        lines.clear(); lines.add(StringBuilder()); col = 0; esc = null
    }

    fun feed(data: String) {
        for (ch in data) {
            val e = esc
            if (e != null) {
                e.append(ch)
                if (e.length == 1 && ch != '[' && ch != ']') { esc = null; continue }  // two-char escape
                if (e.length > 1 && (ch in '@'..'~') && e[0] == '[') { csi(e.substring(1, e.length - 1), ch); esc = null }
                else if (e[0] == ']' && (ch == '\u0007' || e.endsWith("\u001b\\"))) esc = null  // OSC (window title)
                else if (e.length > 64) esc = null
                continue
            }
            when (ch) {
                '\u001b' -> esc = StringBuilder()
                '\n' -> newLine()
                '\r' -> col = 0
                '\b' -> if (col > 0) col--
                '\u0007' -> {}
                '\t' -> repeat(8 - col % 8) { put(' ') }
                else -> if (ch >= ' ') put(ch)
            }
        }
    }

    private fun put(ch: Char) {
        val l = line
        if (col < l.length) l.setCharAt(col, ch) else {
            while (l.length < col) l.append(' ')
            l.append(ch)
        }
        col++
    }

    private fun newLine() {
        lines.add(StringBuilder())
        col = 0
        while (lines.size > maxLines) lines.removeAt(0)
    }

    private fun csi(params: String, cmd: Char) {
        val n = params.trimStart('?').split(';').firstOrNull()?.toIntOrNull() ?: 0
        when (cmd) {
            'K' -> when (n) {               // erase in line
                0 -> if (col < line.length) line.setLength(col)
                1 -> for (i in 0 until minOf(col, line.length)) line.setCharAt(i, ' ')
                2 -> line.setLength(0)
            }
            'D' -> col = (col - maxOf(1, n)).coerceAtLeast(0)
            'C' -> col += maxOf(1, n)
            'G' -> col = (maxOf(1, n) - 1)
            'J' -> if (n == 2 || n == 3) clear()
            'H' -> if (params.isEmpty()) col = 0
            'P' -> repeat(maxOf(1, n)) { if (col < line.length) line.deleteCharAt(col) }
            '@' -> repeat(maxOf(1, n)) { if (col <= line.length) line.insert(col, ' ') }
        }
    }

    /** Everything, with a block cursor shown at the typing position. */
    fun text(): String {
        val sb = StringBuilder()
        for (i in 0 until lines.size - 1) sb.append(lines[i]).append('\n')
        val last = StringBuilder(line)
        while (last.length < col) last.append(' ')
        if (col < last.length) last.setCharAt(col, '█') else last.append('█')
        return sb.append(last).toString()
    }
}
