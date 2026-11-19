package dev.phonebridge

import android.content.Context
import android.net.wifi.WifiManager
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface

/**
 * LAN helpers that also cover the phone's *own* hotspot.
 *
 * When this phone shares a hotspot and the PC joins it, the PC sits on the hotspot's
 * subnet (e.g. 192.168.43.x via ap0/swlan0/wlan1). A broadcast to 255.255.255.255 leaves
 * through the default network (usually mobile data) and never reaches that subnet, so
 * discovery sends to every interface's own broadcast address as well.
 */
object Net {
    fun broadcastTargets(): List<InetAddress> {
        val out = LinkedHashSet<InetAddress>()
        runCatching {
            for (iface in NetworkInterface.getNetworkInterfaces()) {
                if (!iface.isUp || iface.isLoopback) continue
                if (iface.name.startsWith("rmnet") || iface.name.startsWith("ccmni") || iface.name.startsWith("dummy")) continue
                for (a in iface.interfaceAddresses) {
                    if (a.address is Inet4Address) a.broadcast?.let { out.add(it) }
                }
            }
        }
        out.add(InetAddress.getByName("255.255.255.255"))
        return out.toList()
    }

    /** Sends `msg` to every broadcast target on [port]. */
    fun broadcast(sock: DatagramSocket, msg: ByteArray, port: Int) {
        for (target in broadcastTargets()) {
            runCatching { sock.send(DatagramPacket(msg, msg.size, target, port)) }
        }
    }

    /** True while this phone is sharing a Wi-Fi hotspot. */
    fun hotspotOn(ctx: Context): Boolean = runCatching {
        val wm = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        wm.javaClass.getMethod("isWifiApEnabled").invoke(wm) as Boolean
    }.getOrDefault(false)

    const val ACTION_AP_STATE_CHANGED = "android.net.wifi.WIFI_AP_STATE_CHANGED"
}
