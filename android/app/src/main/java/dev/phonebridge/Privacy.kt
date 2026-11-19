package dev.phonebridge

import android.content.Context

/**
 * Privacy controls for what the phone shares with the PC: a master switch and per-app blocking for notifications,
 * hiding of notification text (all of it, or just sensitive things such as one-time codes), and clipboard sync with
 * automatic expiry and exclusion of items the source app marks as sensitive (password managers).
 *
 * The detection rules are plain functions so they can be tested off-device.
 */
object Privacy {
    /** Apps whose notifications are treated as sensitive regardless of their text. */
    val SENSITIVE_PACKAGES = setOf(
        "com.google.android.apps.authenticator2", "com.authy.authy", "com.azure.authenticator", "com.microsoft.authenticator",
        "org.fidoalliance.authenticator", "com.bitwarden.mobile", "com.x8bit.bitwarden", "com.agilebits.onepassword",
        "com.lastpass.lpandroid", "org.keepassdroid", "keepass2android.keepass2android", "com.proton.pass", "me.proton.android.pass",
        "com.google.android.gms.auth.api.credentials",
    )

    private val CODE_WORDS = Regex("(?i)\\b(otp|one[- ]time|verification|security code|passcode|login code|2fa|two[- ]factor|auth(entication)? code)\\b")
    private val SECRET_WORDS = Regex("(?i)\\b(password|passcode|passphrase|cvv|cvc)\\b")
    private val DIGIT_RUN = Regex("\\b\\d{4,8}\\b")

    /** True for notification content that carries a secret: a code from a sensitive app, "your code is 123456", a password. */
    fun isSensitive(pkg: String, text: String): Boolean =
        pkg in SENSITIVE_PACKAGES || SECRET_WORDS.containsMatchIn(text) ||
            (CODE_WORDS.containsMatchIn(text) && DIGIT_RUN.containsMatchIn(text))

    /** Clip description extra that password managers set (Android 13+: ClipDescription.EXTRA_IS_SENSITIVE). */
    const val EXTRA_IS_SENSITIVE = "android.content.extra.IS_SENSITIVE"

    // ---- settings
    private fun prefs(ctx: Context) = ctx.applicationContext.getSharedPreferences("privacy", Context.MODE_PRIVATE)

    fun notificationsEnabled(ctx: Context) = prefs(ctx).getBoolean("notif_enabled", true)
    fun setNotificationsEnabled(ctx: Context, on: Boolean) = log(ctx, prefs(ctx).edit().putBoolean("notif_enabled", on), "notifications_enabled", on)
    fun hideNotificationText(ctx: Context) = prefs(ctx).getBoolean("notif_hide_all", false)
    fun setHideNotificationText(ctx: Context, on: Boolean) = log(ctx, prefs(ctx).edit().putBoolean("notif_hide_all", on), "notifications_hide_text", on)
    fun hideSensitive(ctx: Context) = prefs(ctx).getBoolean("notif_hide_sensitive", true)
    fun setHideSensitive(ctx: Context, on: Boolean) = log(ctx, prefs(ctx).edit().putBoolean("notif_hide_sensitive", on), "notifications_hide_sensitive", on)

    fun blockedApps(ctx: Context): Set<String> = prefs(ctx).getStringSet("notif_blocked", emptySet()) ?: emptySet()
    fun setAppBlocked(ctx: Context, pkg: String, blocked: Boolean) {
        val set = blockedApps(ctx).toMutableSet().apply { if (blocked) add(pkg) else remove(pkg) }
        prefs(ctx).edit().putStringSet("notif_blocked", set).apply()
        PhoneAudit.log(ctx, "privacy.changed", "setting" to "notifications_blocked_apps", "count" to set.size)
    }

    fun clipboardEnabled(ctx: Context) = prefs(ctx).getBoolean("clip_enabled", true)
    fun setClipboardEnabled(ctx: Context, on: Boolean) = log(ctx, prefs(ctx).edit().putBoolean("clip_enabled", on), "clipboard_sync", on)

    /** Seconds after which text received from the PC is removed from the phone clipboard (0 = keep). */
    fun clipboardExpiry(ctx: Context) = prefs(ctx).getInt("clip_expiry_s", 120)
    fun setClipboardExpiry(ctx: Context, seconds: Int) {
        prefs(ctx).edit().putInt("clip_expiry_s", seconds).apply()
        PhoneAudit.log(ctx, "privacy.changed", "setting" to "clipboard_expiry_s", "value" to seconds)
    }

    private fun log(ctx: Context, e: android.content.SharedPreferences.Editor, setting: String, value: Boolean) {
        e.apply()
        PhoneAudit.log(ctx, "privacy.changed", "setting" to setting, "value" to value)
    }
}
