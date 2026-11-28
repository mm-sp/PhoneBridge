// Exercises the real Android PartFiles.kt on a JVM (test_android_connect.py checks the output).
import dev.phonebridge.PartFiles
import java.io.File

fun main() {
    val parts = PartFiles()
    val dir = File("partials")
    fun name(r: PartFiles.Result) = "${r.claim.file.name} ${r.claim.resumable} ${r.displaced?.transferId}"
    fun written(c: PartFiles.Claim) = try { parts.whileHeld(c) { "yes" } } catch (e: java.io.IOException) { "no" }

    val first = parts.claim(dir, "key1", "t1", "s1")
    println("FIRST ${name(first)}")
    val twin = parts.claim(dir, "key1", "t2", "s1")
    println("TWIN ${name(twin)}")
    println("OTHER ${name(parts.claim(dir, "key2", "t3", "s1"))}")
    println("BOTH_WRITE ${written(first.claim)},${written(twin.claim)}")
    parts.release(twin.claim)
    println("TWIN_GONE ${name(parts.claim(dir, "key1", "t4", "s1"))}")
    val retry = parts.claim(dir, "key1", "t5", "s2")   // after a reconnect
    println("RETRY ${name(retry)}")
    println("AFTER_TAKEOVER ${written(first.claim)},${written(retry.claim)}")
    parts.release(first.claim)                          // the old one ending must not free the new one's hold
    println("OLD_RELEASE ${parts.holds(retry.claim)}")
    parts.release(retry.claim)
    println("FREE_AGAIN ${name(parts.claim(dir, "key1", "t6", "s2"))}")
    val hostile = parts.claim(dir, "../../k", "../../t", "s2")
    println("HOSTILE ${hostile.claim.file.parentFile == dir} ${hostile.claim.file.name}")
    println("HOSTILE_TWIN ${parts.claim(dir, "../../k", "../x/y", "s2").claim.file.name}")
    println("NO_KEY ${parts.claim(dir, "", "t7", "s2").claim.file.name}")

    // Saving a verified download.
    val work = File("saving").apply { deleteRecursively(); mkdirs() }
    val done = parts.claim(work, "k", "a1", "s1").claim.also { it.file.writeText("first") }
    val sealed = parts.seal(done)
    val later = parts.claim(work, "k", "a2", "s2").claim.also { it.file.writeText("second attempt") }
    println("SEALED ${sealed.name} ${sealed.readText()} ${later.file.name}")
    val resealed = try { parts.seal(done); "yes" } catch (e: java.io.IOException) { "no" }
    println("SEAL_TAKEN_OVER $resealed ${later.file.readText().length}")
    val inbox = File(work, "inbox").apply { mkdirs() }
    fun save(name: String) = PartFiles.saveCopy(sealed, inbox, name).let { "${it.name}=${it.readText()}" }
    println("SAVED ${save("a.tar.gz")}|${save("a.tar.gz")}|${save("a.tar.gz")}")
    println("SAVED_PLAIN ${save("notes")}|${save("notes")}")
    sealed.writeText("x".repeat(3_000_000))
    val together = (1..16)
        .map { java.util.concurrent.CompletableFuture.supplyAsync { PartFiles.saveCopy(sealed, inbox, "same.bin") } }
        .map { it.join() }
    println("TOGETHER ${together.map { it.name }.toSet().size} ${together.all { it.length() == 3_000_000L }}")
    val failed = try { PartFiles.saveCopy(File(work, "missing"), inbox, "gone.txt"); "saved" }
        catch (e: java.io.IOException) { "failed" }
    println("FAILED_COPY $failed ${File(inbox, "gone.txt").exists()}")
    work.deleteRecursively()
}
