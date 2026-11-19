package dev.phonebridge

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView

/** The app's dark theme: one palette and a few view builders every screen uses. */
object Ui {
    val BG = Color.parseColor("#0E131C")
    val RAIL = Color.parseColor("#121925")
    val SURFACE = Color.parseColor("#1A2230")
    val SURFACE2 = Color.parseColor("#253045")
    val STROKE = Color.parseColor("#2C3850")
    val TEXT = Color.parseColor("#E8EDF5")
    val TEXT2 = Color.parseColor("#B7C1D3")
    val MUTED = Color.parseColor("#7F8BA0")
    val ACCENT = Color.parseColor("#4C8DFF")
    val ACCENT_SOFT = Color.parseColor("#1E3358")
    val GREEN = Color.parseColor("#3DD68C")
    val WARN = Color.parseColor("#F5A524")
    val RED = Color.parseColor("#FF6363")

    fun dp(ctx: Context, v: Int) = (v * ctx.resources.displayMetrics.density).toInt()

    fun rounded(color: Int, radius: Float, stroke: Int? = null, strokeW: Int = 0) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = radius
        if (stroke != null) setStroke(strokeW, stroke)
    }

    private fun ripple(base: GradientDrawable, ctx: Context) =
        RippleDrawable(ColorStateList.valueOf(Color.argb(60, 255, 255, 255)), base, null)

    fun card(ctx: Context) = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(ctx, 16), dp(ctx, 14), dp(ctx, 16), dp(ctx, 14))
        background = rounded(SURFACE, dp(ctx, 16).toFloat(), STROKE, dp(ctx, 1))
        layoutParams = LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(ctx, 10) }
    }

    enum class Kind { PRIMARY, SECONDARY, DANGER, GHOST }

    fun styleButton(b: Button, kind: Kind = Kind.SECONDARY) {
        val ctx = b.context
        b.isAllCaps = false
        b.stateListAnimator = null
        b.typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        b.textSize = 14f
        b.minHeight = dp(ctx, 48); b.minimumHeight = dp(ctx, 48)   // 48dp minimum touch target
        b.setPadding(dp(ctx, 16), 0, dp(ctx, 16), 0)
        val r = dp(ctx, 12).toFloat()
        val (bg, fg) = when (kind) {
            Kind.PRIMARY -> ACCENT to Color.WHITE
            Kind.SECONDARY -> SURFACE2 to TEXT
            Kind.DANGER -> Color.parseColor("#3A1E25") to RED
            Kind.GHOST -> Color.TRANSPARENT to ACCENT
        }
        b.setTextColor(fg)
        b.background = ripple(rounded(bg, r), ctx)
    }

    fun button(ctx: Context, label: String, kind: Kind = Kind.SECONDARY, onClick: () -> Unit) = Button(ctx).apply {
        text = label
        styleButton(this, kind)
        setOnClickListener { onClick() }
    }

    fun text(ctx: Context, s: String, size: Float = 14f, bold: Boolean = false, color: Int = TEXT) = TextView(ctx).apply {
        text = s
        textSize = size
        setTextColor(color)
        if (bold) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setLineSpacing(0f, 1.1f)
    }

    fun sectionTitle(ctx: Context, s: String) = text(ctx, s.uppercase(), 12f, bold = true, color = MUTED).apply {
        letterSpacing = 0.08f
        setPadding(dp(ctx, 4), dp(ctx, 14), 0, dp(ctx, 8))
    }

    fun styleField(e: EditText) {
        val ctx = e.context
        e.setTextColor(TEXT)
        e.setHintTextColor(MUTED)
        e.textSize = 15f
        e.background = rounded(SURFACE2, dp(ctx, 12).toFloat())
        e.setPadding(dp(ctx, 14), dp(ctx, 10), dp(ctx, 14), dp(ctx, 10))
    }

    fun bar(ctx: Context, pct: Int, color: Int = ACCENT) = ProgressBar(ctx, null, android.R.attr.progressBarStyleHorizontal).apply {
        max = 100
        progress = pct.coerceIn(0, 100)
        progressTintList = ColorStateList.valueOf(color)
        progressBackgroundTintList = ColorStateList.valueOf(SURFACE2)
        layoutParams = LinearLayout.LayoutParams(-1, dp(ctx, 6)).apply { topMargin = dp(ctx, 8) }
    }

    /** Colour for a "how full" bar: green when low, amber above 70 %, red above 90 %. */
    fun usageColor(pct: Int) = when {
        pct >= 90 -> RED
        pct >= 70 -> WARN
        else -> ACCENT
    }

    /** Small rounded label, e.g. "● Connected". */
    fun chip(ctx: Context, s: String, fg: Int, bg: Int) = TextView(ctx).apply {
        text = s
        textSize = 12f
        setTextColor(fg)
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        background = rounded(bg, dp(ctx, 20).toFloat())
        setPadding(dp(ctx, 12), dp(ctx, 5), dp(ctx, 12), dp(ctx, 5))
        gravity = Gravity.CENTER
    }

    fun divider(ctx: Context) = View(ctx).apply {
        setBackgroundColor(STROKE)
        layoutParams = LinearLayout.LayoutParams(-1, 1).apply { topMargin = dp(ctx, 8); bottomMargin = dp(ctx, 8) }
    }
}
