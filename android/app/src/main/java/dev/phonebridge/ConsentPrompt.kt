package dev.phonebridge

/**
 * Which screen-capture prompt, if any, still counts. Prompts are numbered: only an answer to the newest one that nobody
 * called off may start a capture. No Android classes: also run on a plain JVM by the tests.
 */
class ConsentPrompt {
    /** Number of the newest prompt. */
    var generation = 0
        private set

    /** A prompt is on screen and young enough that another one must not be stacked on it. */
    private var waiting = false

    /** An answer to the newest prompt would still start the capture. */
    private var answerable = false

    /** A new prompt: its number, or null while one is waiting. */
    @Synchronized
    fun open(): Int? {
        if (waiting) return null
        waiting = true
        answerable = true
        return ++generation
    }

    /** Prompt [gen] has waited long enough that a new request may replace it (a late answer still counts until then).
     *  False if it isn't the waiting one. */
    @Synchronized
    fun stopWaiting(gen: Int): Boolean {
        if (gen != generation || !waiting) return false
        waiting = false
        return true
    }

    /** The user answered prompt [gen]: true if that answer counts (once). */
    @Synchronized
    fun answer(gen: Int): Boolean {
        if (gen != generation || !answerable) return false
        waiting = false
        answerable = false
        return true
    }

    /** The capture isn't wanted any more: the open prompt's answer counts for nothing. True if one could still count. */
    @Synchronized
    fun cancel(): Boolean {
        val open = answerable
        waiting = false
        answerable = false
        return open
    }
}
