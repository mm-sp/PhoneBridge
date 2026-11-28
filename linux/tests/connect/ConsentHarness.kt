// Exercises the real Android ConsentPrompt.kt on a JVM (test_android_connect.py checks the output).
import dev.phonebridge.ConsentPrompt

fun main() {
    var p = ConsentPrompt()
    val first = p.open()!!
    println("OPEN $first ${p.generation} ${p.open()}")
    println("ANSWER ${p.answer(first)} ${p.answer(first)}")
    val second = p.open()!!
    println("OLD ${p.answer(first)} ${p.answer(second)}")

    p = ConsentPrompt()
    val stopped = p.open()!!
    println("CANCEL ${p.cancel()} ${p.cancel()} ${p.answer(stopped)}")
    println("AFTER_CANCEL ${p.open()}")

    p = ConsentPrompt()
    val slow = p.open()!!
    println("TIMED_OUT ${p.stopWaiting(slow)} ${p.stopWaiting(slow)} ${p.answer(slow)}")
    val late = p.open()!!
    p.stopWaiting(late)
    println("LATE_CANCEL ${p.cancel()} ${p.answer(late)}")

    p = ConsentPrompt()
    val a = p.open()!!
    p.answer(a)
    val b = p.open()!!
    println("OLD_TIMER ${p.stopWaiting(a)} ${p.open()}")
    println("REPLACED ${p.stopWaiting(b)} ${p.open()} ${p.answer(b)}")
}
