package dev.phonebridge

import java.io.File

/**
 * Which partial-download file each running PC -> phone transfer writes. Two transfers must never write one partial: each
 * checks only the bytes it received itself, so the mix would be saved as a good file. No Android classes: also run on a
 * plain JVM by the tests.
 */
class PartFiles {
    /** One transfer's hold on a partial file. [resumable]: a later attempt at the same file would continue from it. */
    class Claim(val file: File, val resumable: Boolean, val transferId: String, val session: String)

    /** [claim] to use, and the transfer of an earlier connection it took the partial from (to be stopped), if any. */
    class Result(val claim: Claim, val displaced: Claim?)

    private val holders = HashMap<File, Claim>()

    /**
     * The partial for a new transfer: the one named by [resumeKey], or, while a transfer of this connection is writing
     * that one (the same file sent twice), one of its own. A holder from another connection is a leftover of a link that
     * was replaced, and this is the retry: it takes the partial over. Pair with [release].
     */
    @Synchronized
    fun claim(dir: File, resumeKey: String, transferId: String, session: String): Result {
        fun safe(s: String) = s.filter { it.isLetterOrDigit() }.take(40)
        val id = safe(transferId)
        val key = safe(resumeKey).ifEmpty { id }
        var file = File(dir, ".pb-$key.part")
        val holder = holders[file]
        val shared = holder != null && holder.session == session
        if (shared) file = File(dir, ".pb-$key-$id.part")
        val claim = Claim(file, !shared, transferId, session)
        holders[file] = claim
        return Result(claim, if (shared) null else holder)
    }

    @Synchronized fun holds(claim: Claim) = holders[claim.file] === claim

    /** Runs [block], which changes the partial, only while [claim] still holds it; nobody can take it over meanwhile. */
    @Synchronized
    fun <T> whileHeld(claim: Claim, block: () -> T): T {
        if (!holds(claim)) throw java.io.IOException("replaced by a newer attempt")
        return block()
    }

    /**
     * Moves a complete, verified partial out of every transfer's reach and returns it: a newer attempt claiming the same
     * name while this one is being saved starts a fresh file. Throws if [claim] was taken over.
     */
    fun seal(claim: Claim): File = whileHeld(claim) {
        val id = claim.transferId.filter { it.isLetterOrDigit() }.take(40)
        val sealed = File(claim.file.parentFile, "${claim.file.name}-$id.done")
        sealed.delete()
        if (!claim.file.renameTo(sealed)) throw java.io.IOException("couldn't finish the download")
        sealed
    }

    @Synchronized
    fun release(claim: Claim) {
        if (holds(claim)) holders.remove(claim.file)
    }

    companion object {
        /**
         * Copies [source] into [dir] as [name], or "name (n).ext" if that is taken. The name is reserved by creating the
         * file, so two transfers finishing together never share one. A failed copy leaves nothing behind.
         */
        fun saveCopy(source: File, dir: File, name: String): File {
            val dot = name.lastIndexOf('.')
            val stem = if (dot < 0) name else name.substring(0, dot)
            val ext = if (dot < 0) "" else name.substring(dot)
            var dest = File(dir, name)
            var n = 1
            while (!dest.createNewFile()) dest = File(dir, "$stem (${n++})$ext")
            var saved = false
            try {
                source.inputStream().use { input -> dest.outputStream().use { input.copyTo(it, 1 shl 20) } }
                saved = true
            } finally { if (!saved) dest.delete() }
            return dest
        }
    }
}
