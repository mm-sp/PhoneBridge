// Exercises the real Android ConnectAttempt.kt on a JVM (test_android_connect.py checks the output).
import dev.phonebridge.BusyException
import dev.phonebridge.UnpairedException
import dev.phonebridge.tryRoute

fun main() {
    val failures = mutableListOf<String>()
    fun outcome(connect: () -> String): String = try {
        "returned " + tryRoute({ failures.add(it.message ?: "?") }, connect)
    } catch (e: UnpairedException) {
        "unpaired"
    } catch (e: BusyException) {
        "busy " + e.otherPhone
    }
    println("SUCCESS " + outcome { "link" })
    println("UNREACHABLE " + outcome { throw java.net.ConnectException("refused") })
    println("BAD_HANDSHAKE " + outcome { throw IllegalStateException("handshake") })
    println("UNPAIRED " + outcome { throw UnpairedException() })
    println("BUSY " + outcome { throw BusyException("Other Phone") })
    println("REPORTED " + failures.joinToString(","))

    // Several routes in turn, as the service tries them: a refusal from the PC ends the search.
    for ((name, refusal) in listOf("UNPAIRED" to UnpairedException(), "BUSY" to BusyException("B"))) {
        val tried = mutableListOf<String>()
        val end = try {
            tryRoute<String> { tried.add("usb"); throw java.io.IOException("no tunnel") }
                ?: tryRoute { tried.add("wifi"); throw refusal }
                ?: tryRoute { tried.add("relay"); "link" }
            "continued"
        } catch (e: Exception) {
            e.javaClass.simpleName
        }
        println("ROUTES_$name ${tried.joinToString(",")} $end")
    }
}
