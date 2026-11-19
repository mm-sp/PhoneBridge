package dev.phonebridge

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Finds the PC through mDNS / DNS-SD (`_phonebridge._tcp`, advertised by the Linux app via Avahi),
 * which works on networks where UDP broadcast is filtered. Only services whose `cert` TXT value
 * matches the start of the pinned certificate fingerprint are considered; TLS pinning and the
 * session key still do the real authentication.
 */
object Mdns {
    private const val TYPE = "_phonebridge._tcp."

    @Suppress("DEPRECATION")
    fun find(ctx: Context, pc: PairedPc, timeoutMs: Long = 3000): Pair<String, Int>? {
        val nsd = ctx.getSystemService(Context.NSD_SERVICE) as? NsdManager ?: return null
        val result = AtomicReference<Pair<String, Int>?>(null)
        val done = CountDownLatch(1)
        val wanted = pc.certFingerprint.lowercase().take(16)

        val resolveListener = object : NsdManager.ResolveListener {
            override fun onResolveFailed(info: NsdServiceInfo, code: Int) {}
            override fun onServiceResolved(info: NsdServiceInfo) {
                val cert = info.attributes["cert"]?.toString(Charsets.UTF_8)?.lowercase()
                val host = info.host?.hostAddress
                if (host != null && (cert == null || cert == wanted)) {
                    result.compareAndSet(null, host to info.port)
                    done.countDown()
                }
            }
        }
        val discovery = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onServiceFound(info: NsdServiceInfo) {
                runCatching { nsd.resolveService(info, resolveListener) }
            }
            override fun onServiceLost(info: NsdServiceInfo) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, code: Int) { done.countDown() }
            override fun onStopDiscoveryFailed(serviceType: String, code: Int) {}
        }
        return try {
            nsd.discoverServices(TYPE, NsdManager.PROTOCOL_DNS_SD, discovery)
            done.await(timeoutMs, TimeUnit.MILLISECONDS)
            result.get()
        } catch (e: Exception) {
            Log.i(BridgeService.TAG, "mDNS discovery failed: $e")
            null
        } finally {
            runCatching { nsd.stopServiceDiscovery(discovery) }
        }
    }
}
