// Compile-time stand-ins for classes the audit/permission code references but the JVM test never calls.
package dev.phonebridge

import android.content.Context

object Store {
    fun identity(ctx: Context): SecureSession.Identity = throw NotImplementedError("device only")
}

class BridgeService {
    companion object { const val CHANNEL_ALERTS = "alerts" }
}
