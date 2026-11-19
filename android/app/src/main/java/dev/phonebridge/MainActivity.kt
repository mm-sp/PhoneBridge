package dev.phonebridge

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.google.zxing.integration.android.IntentIntegrator
import org.json.JSONObject

class MainActivity : Activity() {
    private lateinit var statusText: TextView
    private lateinit var pairedText: TextView
    private lateinit var checklist: LinearLayout
    private lateinit var unpairBtn: Button
    private lateinit var connectBtn: Button

    private val runtimePermissions = arrayOf(
        Manifest.permission.READ_SMS, Manifest.permission.SEND_SMS, Manifest.permission.RECEIVE_SMS,
        Manifest.permission.READ_CONTACTS, Manifest.permission.READ_CALL_LOG, Manifest.permission.READ_PHONE_STATE,
        Manifest.permission.CALL_PHONE, Manifest.permission.ANSWER_PHONE_CALLS,
        Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.WRITE_EXTERNAL_STORAGE,
        Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION,
    ) + (if (android.os.Build.VERSION.SDK_INT >= 31) arrayOf(Manifest.permission.BLUETOOTH_SCAN) else emptyArray())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.BG
        // Landscape screens are short: hide the status bar so the tabs (and the terminal
        // above the keyboard) get that space.
        @Suppress("DEPRECATION")
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_FULLSCREEN)
        setContentView(buildShell())
        // An explicit launch (launcher icon, notification tap) is origin USER and is forwarded to the PC; a launch caused
        // by a sync command carries EXTRA_ORIGIN=auto and is not (no ping-pong). A restore after process death is not a launch.
        if (savedInstanceState == null) reportLaunch(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        reportLaunch(intent)
    }

    private fun reportLaunch(intent: Intent?) {
        val origin = if (intent?.getStringExtra(EXTRA_ORIGIN) == Origin.AUTO.wire) Origin.AUTO else Origin.USER
        val sync = BridgeService.instance?.screenSync
        if (sync != null) sync.onUserOpenedApp(origin)
        else if (origin == Origin.USER) ScreenSync.pendingOpen = true   // service not up yet: forwarded when the link is
    }

    override fun onResume() {
        super.onResume()
        resumed = true
        BridgeService.stateListener = { runOnUiThread { refresh() } }
        BridgeService.pcListener = ::routePcMessage
        current?.onShow()
        // Opening the app (re)starts the link; it stops itself again if it can't connect.
        if (Store.load(this) != null && !Store.isPaused(this)) {
            BridgeService.instance?.let { if (!it.isConnected()) it.wake() } ?: BridgeService.start(this)
        }
        refresh()
    }

    override fun onPause() {
        resumed = false
        BridgeService.stateListener = null
        BridgeService.pcListener = null
        current?.onHide()
        super.onPause()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        // The only moment Android 10+ lets us read the clipboard: while we're focused.
        if (hasFocus) Clipboard.sendIfNew(this)
    }

    // ------------------------------------------------------------------ UI

    // ------------------------------------------------------------------ tabs

    private lateinit var panels: List<PcPanel>
    private lateinit var filesPanel: FilesPanel
    private lateinit var content: android.widget.FrameLayout
    private lateinit var homeView: View
    private val tabButtons = ArrayList<Button>()
    private lateinit var headerTitle: TextView
    private lateinit var headerChip: TextView
    private var currentIndex = 0
    private var current: PcPanel? = null

    /** Home (pairing/setup) + the "control this PC" tabs. */
    private fun buildShell(): View {
        filesPanel = FilesPanel(this)
        panels = listOf(DevicePanel(this), filesPanel, ScreenPanel(this), AppsPanel(this),
            TerminalPanel(this), PortsPanel(this), ServersPanel(this), SecurityPanel(this))
        homeView = buildUi()
        content = android.widget.FrameLayout(this)

        // Landscape layout: a scrollable column of tabs on the left (1/4 of the width),
        // the selected tab (with a header bar) in the remaining 3/4.
        val bar = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(10), dp(12), dp(10), dp(12)) }
        bar.addView(Ui.text(this, "PhoneBridge", 18f, bold = true, color = Ui.TEXT).apply {
            setPadding(dp(10), dp(4), 0, dp(14))
        })
        val titles = listOf("Home") + panels.map { it.title }
        titles.forEachIndexed { i, t ->
            val b = Button(this).apply {
                text = t
                // Line icons in the laptop app's style, tinted with the label colour.
                TAB_ICONS[t]?.let { res ->
                    setCompoundDrawablesRelativeWithIntrinsicBounds(getDrawable(res)?.mutate(), null, null, null)
                    compoundDrawablePadding = dp(14)
                }
                isAllCaps = false
                stateListAnimator = null
                textSize = 14f
                gravity = android.view.Gravity.START or android.view.Gravity.CENTER_VERTICAL
                setPadding(dp(14), 0, dp(8), 0)
                setOnClickListener { selectTab(i) }
            }
            tabButtons.add(b)
            bar.addView(b, LinearLayout.LayoutParams(-1, dp(44)).apply { bottomMargin = dp(4) })
        }
        val right = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(18), dp(10), dp(14), dp(6))
        }
        headerTitle = Ui.text(this, "", 20f, bold = true)
        header.addView(headerTitle, LinearLayout.LayoutParams(0, -2, 1f))
        headerChip = Ui.chip(this, "", Ui.MUTED, Ui.SURFACE2)
        header.addView(headerChip)
        right.addView(header)
        right.addView(content, LinearLayout.LayoutParams(-1, 0, 1f))
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(Ui.BG)
        }
        root.addView(ScrollView(this).apply {
            isVerticalScrollBarEnabled = false
            setBackgroundColor(Ui.RAIL)
            addView(bar)
        }, LinearLayout.LayoutParams(0, -1, 1f))
        root.addView(right, LinearLayout.LayoutParams(0, -1, 3f))
        selectTab(0)
        return root
    }

    private fun selectTab(index: Int) {
        current?.onHide()
        content.removeAllViews()
        currentIndex = index
        tabButtons.forEachIndexed { i, b ->
            b.background = Ui.rounded(if (i == index) Ui.ACCENT_SOFT else Color.TRANSPARENT, dp(12).toFloat())
            b.setTextColor(if (i == index) Ui.TEXT else Ui.TEXT2)
            b.compoundDrawableTintList = android.content.res.ColorStateList.valueOf(if (i == index) Ui.TEXT else Ui.MUTED)
            b.typeface = Typeface.create(if (i == index) "sans-serif-medium" else "sans-serif", Typeface.NORMAL)
        }
        headerTitle.text = if (index == 0) "Home" else panels[index - 1].title
        updateHeaderChip()
        if (index == 0) {
            current = null
            content.addView(homeView)
            refresh()
            return
        }
        val panel = panels[index - 1]
        current = panel
        content.addView(panel.view)
        panel.onShow()
    }

    /** The "● Connected to …" chip in the header bar. */
    private fun updateHeaderChip() {
        val svc = BridgeService.instance
        val pc = Store.load(this)
        when {
            svc?.isConnected() == true -> {
                headerChip.text = "●  ${pc?.name ?: "PC"}"
                headerChip.setTextColor(Ui.GREEN)
                headerChip.background = Ui.rounded(Color.parseColor("#163327"), dp(20).toFloat())
            }
            pc == null -> {
                headerChip.text = "○  Not paired"
                headerChip.setTextColor(Ui.MUTED)
                headerChip.background = Ui.rounded(Ui.SURFACE2, dp(20).toFloat())
            }
            else -> {
                headerChip.text = "○  Disconnected"
                headerChip.setTextColor(Ui.WARN)
                headerChip.background = Ui.rounded(Color.parseColor("#3A2E14"), dp(20).toFloat())
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if ((current as? ScreenPanel)?.handleBack() == true) return
        @Suppress("DEPRECATION")
        super.onBackPressed()
    }

    private fun routePcMessage(type: String, p: org.json.JSONObject) {
        // Clipboard history and file-saved notices are kept even when their tab isn't open.
        if (type == "pc.file_saved" && current !== filesPanel) filesPanel.onMessage(type, p)
        current?.onMessage(type, p)
    }

    private fun buildUi(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(4), dp(18), dp(24))
        }
        root.addView(Ui.text(this, "Link this phone to your Linux PC — no account, no cloud.", 13f, color = Ui.MUTED).apply {
            setPadding(dp(2), 0, 0, dp(12))
        })

        val card = card()
        pairedText = Ui.text(this, "", 18f, bold = true)
        statusText = Ui.text(this, "", 13f, color = Ui.TEXT2).apply { setPadding(0, dp(4), 0, dp(10)) }
        card.addView(pairedText)
        card.addView(statusText)
        connectBtn = button("Connect to PC", primary = true) {
            BridgeService.start(this, BridgeService.ACTION_CONNECT)
            refresh()
        }
        card.addView(connectBtn)
        card.addView(button("Scan QR code on PC", primary = true) { scanQr() })
        card.addView(button("Pair with PIN instead") { pinDialog() })
        unpairBtn = button("Unpair") { confirmUnpair() }.also { Ui.styleButton(it, Ui.Kind.DANGER) }
        card.addView(unpairBtn)
        root.addView(card)

        root.addView(section("Clipboard"))
        val clipCard = card()
        clipCard.addView(Ui.text(this, "Copy on the PC → paste on this phone, automatically. Copy on this phone → it's sent " +
            "to the PC when you tap Copy/Cut or an app says \"Copied\" (needs \"Control phone from PC\" and " +
            "\"Open apps & screen mirror\" allowed below). This button sends it by hand.",
            13f, color = Ui.TEXT2))
        clipCard.addView(button("Send clipboard to PC now") {
            val sent = Clipboard.sendIfNew(this)
            toast(if (BridgeService.instance?.isConnected() != true) "Not connected" else if (sent) "Sent" else "Already sent or empty")
        })
        root.addView(clipCard)

        root.addView(section("Setup"))
        checklist = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(checklist)

        return ScrollView(this).apply { setBackgroundColor(Ui.BG); addView(root) }
    }

    private fun refresh() {
        val pc = Store.load(this)
        val svc = BridgeService.instance
        pairedText.text = if (pc == null) "Not paired" else "Paired with ${pc.name}"
        statusText.text = svc?.stateDetail?.ifEmpty { null } ?: BridgeService.lastStopReason
            ?: if (pc == null) "Open PhoneBridge on your PC, click \"Pair a device\" and scan its QR code." else "Not connected."
        unpairBtn.visibility = if (pc == null) View.GONE else View.VISIBLE
        connectBtn.visibility = if (pc != null && svc?.isConnected() != true) View.VISIBLE else View.GONE
        if (::headerChip.isInitialized) updateHeaderChip()

        checklist.removeAllViews()
        val missingRuntime = runtimePermissions.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        addCheck("Messages, calls, contacts, photos, files, camera", missingRuntime.isEmpty(),
            if (missingRuntime.isEmpty()) "All allowed" else "${missingRuntime.size} permission(s) still needed") {
            requestPermissions(runtimePermissions, 10)
        }
        val nm = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        addCheck("PhoneBridge notifications", nm.areNotificationsEnabled(),
            "Needed for \"Send clipboard to PC\" and screen-mirroring/app prompts") {
            startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
        }
        addCheck("Notifications on PC", notificationAccess(), "Shows your phone's notifications on the PC") {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        addCheck("Control phone from PC", InputService.instance != null,
            "Settings › Accessibility › PhoneBridge remote control — needed to tap/type while mirroring") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        addCheck("Open apps & screen mirror from PC", Settings.canDrawOverlays(this),
            "\"Display over other apps\" lets the PC launch apps and start mirroring") {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        }
        if (android.os.Build.VERSION.SDK_INT >= 33 && (!notificationAccess() || InputService.instance == null)) {
            // Android 13+ greys out Notification access / Accessibility for sideloaded apps
            // until the user allows "restricted settings" from App info.
            addCheck("Android blocked a setting above?", false,
                "If a toggle is greyed out or says \"Restricted setting\": tap Allow → ⋮ (top right) → " +
                    "\"Allow restricted settings\", then come back and enable it.") {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
            }
        }
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        addCheck("Stay connected in background", pm.isIgnoringBatteryOptimizations(packageName),
            "Stops Android from cutting the connection to save battery") {
            @Suppress("BatteryLife")
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }
    }

    private fun notificationAccess(): Boolean {
        val flat = Settings.Secure.getString(contentResolver, "enabled_notification_listeners") ?: return false
        val me = ComponentName(this, NotificationRelay::class.java).flattenToString()
        return flat.split(":").any { it == me }
    }

    private fun addCheck(title: String, ok: Boolean, detail: String, onAllow: () -> Unit) {
        val row = card()
        val top = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        top.addView(Ui.chip(this, if (ok) "✓" else "!", if (ok) Ui.GREEN else Ui.WARN,
            Color.parseColor(if (ok) "#163327" else "#3A2E14")).apply { setPadding(dp(10), dp(4), dp(10), dp(4)) })
        val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(12), 0, dp(8), 0) }
        texts.addView(Ui.text(this, title, 15f, bold = true, color = if (ok) Ui.TEXT else Ui.TEXT))
        texts.addView(Ui.text(this, detail, 12f, color = Ui.MUTED))
        top.addView(texts, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        if (!ok) top.addView(Ui.button(this, "Allow", Ui.Kind.PRIMARY) { onAllow() })
        row.addView(top)
        checklist.addView(row)
    }

    // ------------------------------------------------------------------ pairing

    private fun scanQr() {
        IntentIntegrator(this).apply {
            setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
            setPrompt("Scan the QR code shown in PhoneBridge on your PC")
            setBeepEnabled(false)
            setOrientationLocked(false)
        }.initiateScan()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == FilesPanel.REQUEST_PICK) {
            if (resultCode == RESULT_OK && data != null) {
                val uris = ArrayList<Uri>()
                data.clipData?.let { cd -> for (i in 0 until cd.itemCount) uris.add(cd.getItemAt(i).uri) }
                data.data?.let { if (uris.isEmpty()) uris.add(it) }
                filesPanel.sendPicked(uris)
            }
            return
        }
        val result = IntentIntegrator.parseActivityResult(requestCode, resultCode, data)
        if (result == null) {
            super.onActivityResult(requestCode, resultCode, data)
            return
        }
        val contents = result.contents ?: return
        val qr = runCatching { JSONObject(contents) }.getOrNull()
        if (qr == null || !qr.has("token") || !qr.has("pub")) {
            toast("That isn't a PhoneBridge pairing code")
            return
        }
        doPair(qr, null)
    }

    /** Full-screen PIN entry using PhoneBridge's own keyboard (numbers page first). */
    private fun pinDialog() {
        val root = window.decorView as ViewGroup
        val panel = android.widget.FrameLayout(this).apply { setBackgroundColor(Ui.BG); isClickable = true }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(24), dp(16), dp(24), 0)
        }
        box.addView(Ui.text(this, "Pair with PIN", 20f, bold = true))
        box.addView(Ui.text(this, "Enter the 6-digit number shown under the QR code on your PC. " +
            "The phone finds the PC on your Wi-Fi.", 13f, color = Ui.TEXT2).apply {
            gravity = Gravity.CENTER; setPadding(0, dp(6), 0, dp(12))
        })
        val pinView = Ui.text(this, "", 30f, bold = true).apply {
            gravity = Gravity.CENTER
            letterSpacing = 0.4f
            background = Ui.rounded(Ui.SURFACE2, dp(14).toFloat(), Ui.ACCENT, dp(2))
            setPadding(dp(24), dp(10), dp(24), dp(10))
            minWidth = dp(260)
        }
        box.addView(pinView)
        val digits = StringBuilder()
        fun show() { pinView.text = digits.toString().padEnd(6, '·').chunked(1).joinToString("") }
        show()
        fun close() = root.removeView(panel)
        fun submit() {
            if (digits.length == 6) { close(); doPair(null, digits.toString()) } else toast("Enter all 6 digits")
        }
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER }
        buttons.addView(Ui.button(this, "Cancel") { close() })
        buttons.addView(View(this), LinearLayout.LayoutParams(dp(12), 1))
        buttons.addView(Ui.button(this, "Pair", Ui.Kind.PRIMARY) { submit() })
        box.addView(buttons, LinearLayout.LayoutParams(-2, -2).apply { topMargin = dp(12) })
        val keyboard = InAppKeyboard(this,
            onText = { t -> t.filter { it.isDigit() }.forEach { if (digits.length < 6) digits.append(it) }; show() },
            onKey = { k -> if (k == "BackSpace") { if (digits.isNotEmpty()) digits.setLength(digits.length - 1); show() } else submit() },
            onHide = { close() }, numbersFirst = true)
        panel.addView(box, android.widget.FrameLayout.LayoutParams(-1, -2, Gravity.TOP))
        panel.addView(keyboard, android.widget.FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        root.addView(panel, ViewGroup.LayoutParams(-1, -1))
    }

    /** Asks the user (on the UI thread; blocks the pairing thread) whether the PC shows the same code. */
    private fun confirmCode(code: String): Boolean {
        val answer = java.util.concurrent.CompletableFuture<Boolean>()
        runOnUiThread {
            AlertDialog.Builder(this)
                .setTitle("Check the code on your PC")
                .setMessage("Your PC should be asking to allow this phone and showing:\n\n${code.substring(0, 3)} ${code.substring(3)}\n\n" +
                    "Pair only if it is exactly the same. If your PC shows nothing or a different code, someone else may be answering — cancel.")
                .setCancelable(false)
                .setPositiveButton("Same code — pair") { _, _ -> answer.complete(true) }
                .setNegativeButton("Cancel") { _, _ -> answer.complete(false) }
                .show()
        }
        return try { answer.get(180, java.util.concurrent.TimeUnit.SECONDS) } catch (_: Exception) { false }
    }

    private fun doPair(qr: JSONObject?, pin: String?) {
        val missing = runtimePermissions.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) requestPermissions(runtimePermissions, 10)
        val progress = AlertDialog.Builder(this).setMessage("Pairing…").setCancelable(false).show()
        Thread {
            val error = try {
                Pairing.pair(this, qr, pin, { step -> runOnUiThread { progress.setMessage(step) } }) { code -> confirmCode(code) }
                null
            } catch (e: Exception) {
                e.message ?: e.toString()
            }
            runOnUiThread {
                progress.dismiss()
                if (error == null) {
                    toast("Paired!")
                    BridgeService.start(this, BridgeService.ACTION_RECONNECT)
                } else {
                    AlertDialog.Builder(this).setTitle("Pairing failed").setMessage(error).setPositiveButton("OK", null).show()
                }
                refresh()
            }
        }.start()
    }

    private fun confirmUnpair() {
        AlertDialog.Builder(this)
            .setMessage("Forget this PC? You'll need to scan its QR code again.")
            .setPositiveButton("Unpair") { _, _ ->
                Store.clear(this)
                BridgeService.start(this, BridgeService.ACTION_UNPAIR)
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        refresh()
        BridgeService.instance?.sendDeviceInfo()
    }

    // ------------------------------------------------------------------ helpers

    private fun card() = Ui.card(this)

    private fun section(title: String) = Ui.sectionTitle(this, title)

    private fun button(label: String, primary: Boolean = false, onClick: () -> Unit) =
        Ui.button(this, label, if (primary) Ui.Kind.PRIMARY else Ui.Kind.SECONDARY, onClick).apply {
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
                .apply { topMargin = dp(8) }
        }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    companion object {
        private val ACCENT = Ui.ACCENT
        /** True while the activity is in the foreground (what "app already open" means for sync). */
        @Volatile var resumed = false
        const val EXTRA_ORIGIN = "pb_origin"

        private val TAB_ICONS = mapOf(
            "Home" to R.drawable.ic_tab_home, "Device" to R.drawable.ic_tab_device, "Files" to R.drawable.ic_tab_files,
            "Screen" to R.drawable.ic_tab_screen, "Apps" to R.drawable.ic_tab_apps,
            "Terminal" to R.drawable.ic_tab_terminal, "Ports" to R.drawable.ic_tab_ports, "Servers" to R.drawable.ic_tab_servers, "Security" to R.drawable.ic_tab_security,
        )
    }
}
