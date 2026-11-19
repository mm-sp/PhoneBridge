import dev.phonebridge.*
import java.io.File
fun main(args: Array<String>) {
    val parts = listOf(
        MmsPart("text/plain", "text.txt", "héllo MMS".toByteArray()),
        MmsPart("image/jpeg", "photo.jpg", ByteArray(300) { it.toByte() }),
        MmsPart("application/x-thing", "t.bin", byteArrayOf(1, 2, 3)))
    File(args[0]).writeBytes(MmsPdu.build("T1234", listOf("+1 (555) 010-2030", "5550109999"), parts))
}
