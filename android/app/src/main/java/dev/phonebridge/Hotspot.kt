package dev.phonebridge

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Build
import android.provider.Settings
import android.util.Log
import org.json.JSONObject

/**
 * Hotspot help for the PC (`hotspot.request` -> `hotspot.state`).
 *
 * Android doesn't let ordinary apps switch the real tethering hotspot on, so there are two parts:
 *  - `start_local` / `stop_local`: a *local-only* hotspot (WifiManager.startLocalOnlyHotspot): a private
 *    Wi-Fi network with a random name and password that lets the PC and phone talk without any router.
 *    It gives the PC no internet. Needs Location permission (and Location switched on).
 *  - `open_settings`: shows a notification on the phone that opens the system hotspot screen in one tap
 *    (apps can't open it from the background, so the user taps it).
 */
object Hotspot {
    private var reservation: WifiManager.LocalOnlyHotspotReservation? = null

    @SuppressLint("MissingPermission")
    fun handle(svc: BridgeService, p: JSONObject) {
        when (p.optString("action")) {
            "start_local" -> startLocal(svc)
            "stop_local" -> { stopLocal(); svc.send("hotspot.state", JSONObject().put("on", false).put("kind", "local_only")) }
            "open_settings" -> openSettings(svc)
            else -> svc.send("hotspot.state", state(svc))
        }
    }

    fun state(svc: BridgeService): JSONObject {
        val r = reservation
        val o = JSONObject().put("kind", "local_only").put("tether_on", Net.hotspotOn(svc))
        if (r == null) return o.put("on", false)
        return credentials(r, o).put("on", true)
    }

    @Suppress("DEPRECATION")
    private fun credentials(r: WifiManager.LocalOnlyHotspotReservation, o: JSONObject): JSONObject {
        if (Build.VERSION.SDK_INT >= 30) {
            val cfg = r.softApConfiguration
            val ssid = if (Build.VERSION.SDK_INT >= 33) cfg.wifiSsid?.toString()?.trim('"') else cfg.ssid
            o.put("ssid", ssid ?: "").put("password", cfg.passphrase ?: "")
        } else {
            val cfg = r.wifiConfiguration
            o.put("ssid", cfg?.SSID?.trim('"') ?: "").put("password", cfg?.preSharedKey?.trim('"') ?: "")
        }
        return o
    }

    @SuppressLint("MissingPermission")
    private fun startLocal(svc: BridgeService) {
        if (reservation != null) { svc.send("hotspot.state", state(svc)); return }
        val wifi = svc.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        try {
            wifi.startLocalOnlyHotspot(object : WifiManager.LocalOnlyHotspotCallback() {
                override fun onStarted(res: WifiManager.LocalOnlyHotspotReservation) {
                    reservation = res
                    svc.send("hotspot.state", state(svc))
                }
                override fun onStopped() {
                    reservation = null
                    svc.send("hotspot.state", JSONObject().put("on", false).put("kind", "local_only"))
                }
                override fun onFailed(reason: Int) {
                    Log.i(BridgeService.TAG, "local hotspot failed: $reason")
                    svc.send("hotspot.state", JSONObject().put("on", false).put("kind", "local_only")
                        .put("error", when (reason) {
                            ERROR_NO_CHANNEL -> "No free Wi-Fi channel"
                            ERROR_TETHERING_DISALLOWED -> "Hotspot is disabled on this phone"
                            ERROR_INCOMPATIBLE_MODE -> "Turn off Wi-Fi tethering / Wi-Fi Direct first"
                            else -> "The phone couldn't start a hotspot"
                        }))
                }
            }, svc.main)
        } catch (e: SecurityException) {
            svc.send("hotspot.state", JSONObject().put("on", false).put("kind", "local_only")
                .put("error", "Allow Location permission for PhoneBridge (and turn Location on) to start a hotspot"))
        } catch (e: Exception) {
            svc.send("hotspot.state", JSONObject().put("on", false).put("kind", "local_only").put("error", e.message ?: "failed"))
        }
    }

    fun stopLocal() {
        runCatching { reservation?.close() }
        reservation = null
    }

    private fun openSettings(svc: BridgeService) {
        val intent = Intent().setClassName("com.android.settings", "com.android.settings.TetherSettings")
        val pm = svc.packageManager
        val target = if (intent.resolveActivity(pm) != null) intent else Intent(Settings.ACTION_WIRELESS_SETTINGS)
        target.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val pi = PendingIntent.getActivity(svc, 7, target, PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(svc, BridgeService.CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Your PC wants to use this phone's hotspot")
            .setContentText("Tap to open the hotspot settings and turn it on")
            .setContentIntent(pi).setAutoCancel(true).build()
        (svc.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).notify(7007, n)
        svc.send("hotspot.state", JSONObject().put("on", false).put("kind", "tether")
            .put("message", "Notification shown on the phone — tap it to open hotspot settings"))
    }
}
