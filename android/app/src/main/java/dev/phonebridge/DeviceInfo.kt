package dev.phonebridge

import android.annotation.SuppressLint
import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.provider.Settings
import android.telephony.TelephonyManager
import org.json.JSONObject

/** Builds the `device.info` payload — see PROTOCOL.md section 4. */
object DeviceInfo {
    @SuppressLint("MissingPermission")
    fun collect(ctx: Context): JSONObject {
        val o = JSONObject()
        o.put("name", Pairing.deviceName())
        o.put("model", Pairing.deviceName())
        o.put("os", "Android ${Build.VERSION.RELEASE}")
        o.put("android_version", Build.VERSION.RELEASE)
        o.put("sdk", Build.VERSION.SDK_INT)
        runCatching {
            Settings.Global.getString(ctx.contentResolver, Settings.Global.DEVICE_NAME)
        }.getOrNull()?.let { o.put("device_name", it) }

        // Battery
        ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))?.let { b ->
            val level = b.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
            val scale = b.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
            if (level >= 0) o.put("battery_pct", level * 100 / scale.coerceAtLeast(1))
            val status = b.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
            o.put("charging", status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL)
            o.put("battery_temp_c", b.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0)
            o.put("battery_health", when (b.getIntExtra(BatteryManager.EXTRA_HEALTH, 0)) {
                BatteryManager.BATTERY_HEALTH_GOOD -> "Good"
                BatteryManager.BATTERY_HEALTH_OVERHEAT -> "Overheating"
                BatteryManager.BATTERY_HEALTH_DEAD -> "Dead"
                BatteryManager.BATTERY_HEALTH_COLD -> "Cold"
                else -> "Unknown"
            })
        }

        // Mobile network
        val tm = ctx.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
        runCatching { o.put("carrier", tm.networkOperatorName ?: "") }
        if (Build.VERSION.SDK_INT >= 28) {
            runCatching {
                tm.signalStrength?.let { s ->
                    o.put("signal_level", s.level)          // 0..4 bars
                    o.put("signal_pct", s.level * 25)
                    if (Build.VERSION.SDK_INT >= 29) s.cellSignalStrengths.firstOrNull()?.let { o.put("signal_dbm", it.dbm) }
                }
            }
        }
        runCatching { o.put("network_type", networkTypeName(tm.dataNetworkType)) }
        runCatching { o.put("sim_state", if (tm.simState == TelephonyManager.SIM_STATE_READY) "Ready" else "No SIM") }

        // Wi-Fi
        val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        o.put("on_wifi", caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true)
        o.put("hotspot_on", Net.hotspotOn(ctx))
        runCatching {
            val wm = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val info = wm.connectionInfo
            val ssid = info.ssid?.trim('"')
            if (!ssid.isNullOrEmpty() && ssid != "<unknown ssid>") o.put("wifi_ssid", ssid)
            if (info.rssi > -127) {
                o.put("wifi_rssi", info.rssi)
                @Suppress("DEPRECATION")
                o.put("wifi_signal_pct", WifiManager.calculateSignalLevel(info.rssi, 5) * 25)
            }
            if (info.linkSpeed > 0) o.put("wifi_link_mbps", info.linkSpeed)
        }

        // Storage + memory
        runCatching {
            val st = StatFs(Environment.getDataDirectory().path)
            o.put("storage_total", st.totalBytes)
            o.put("storage_free", st.availableBytes)
        }
        runCatching {
            val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            val mi = ActivityManager.MemoryInfo().also { am.getMemoryInfo(it) }
            o.put("ram_total", mi.totalMem)
            o.put("ram_free", mi.availMem)
        }
        return o
    }

    private fun networkTypeName(t: Int): String = when (t) {
        TelephonyManager.NETWORK_TYPE_NR -> "5G"
        TelephonyManager.NETWORK_TYPE_LTE -> "4G LTE"
        TelephonyManager.NETWORK_TYPE_HSPAP, TelephonyManager.NETWORK_TYPE_HSPA,
        TelephonyManager.NETWORK_TYPE_HSDPA, TelephonyManager.NETWORK_TYPE_HSUPA,
        TelephonyManager.NETWORK_TYPE_UMTS -> "3G"
        TelephonyManager.NETWORK_TYPE_EDGE, TelephonyManager.NETWORK_TYPE_GPRS -> "2G"
        TelephonyManager.NETWORK_TYPE_UNKNOWN -> "None"
        else -> "Other"
    }
}
