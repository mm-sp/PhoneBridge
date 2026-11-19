package android.util

/** JVM stand-in for android.util.Base64 so Crypto.kt compiles off-device. */
object Base64 {
    const val DEFAULT = 0
    const val NO_WRAP = 2
    fun encodeToString(input: ByteArray, flags: Int): String = java.util.Base64.getEncoder().encodeToString(input)
    fun decode(str: String, flags: Int): ByteArray = java.util.Base64.getMimeDecoder().decode(str)
}
