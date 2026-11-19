package dev.phonebridge

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.ParcelUuid
import android.util.Log

/**
 * Listens (low-power BLE scan) for the PC's PhoneBridge beacon while this phone is NOT connected, so the
 * moment the PC app starts the phone reconnects instead of waiting out its retry back-off. The beacon
 * carries only the public service UUID; the connection that follows is still pinned + encrypted.
 */
object BleWake {
    private val SERVICE = ParcelUuid.fromString("b43c1e00-35ee-4b8a-9e3f-6c3f2a1b9d10")
    private var callback: ScanCallback? = null
    private var lastWake = 0L

    @SuppressLint("MissingPermission")
    @Synchronized
    fun start(svc: BridgeService) {
        if (callback != null) return
        try {
            val scanner = (svc.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)
                ?.adapter?.takeIf { it.isEnabled }?.bluetoothLeScanner ?: return
            val cb = object : ScanCallback() {
                override fun onScanResult(callbackType: Int, result: ScanResult) {
                    val now = System.currentTimeMillis()
                    if (now - lastWake < 5000) return
                    lastWake = now
                    Log.i(BridgeService.TAG, "PC beacon seen — reconnecting now")
                    svc.wake()
                }
                override fun onScanFailed(errorCode: Int) { Log.i(BridgeService.TAG, "BLE scan failed: $errorCode") }
            }
            scanner.startScan(listOf(ScanFilter.Builder().setServiceUuid(SERVICE).build()),
                ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_POWER).build(), cb)
            callback = cb
        } catch (e: SecurityException) {
            // Bluetooth-scan permission not granted: the normal retry loop still works.
        } catch (e: Exception) {
            Log.i(BridgeService.TAG, "BLE wake unavailable: $e")
        }
    }

    @SuppressLint("MissingPermission")
    @Synchronized
    fun stop(svc: Context) {
        val cb = callback ?: return
        callback = null
        runCatching {
            (svc.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter?.bluetoothLeScanner?.stopScan(cb)
        }
    }
}
