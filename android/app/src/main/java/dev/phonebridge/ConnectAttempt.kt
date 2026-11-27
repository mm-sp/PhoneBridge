package dev.phonebridge

/** The PC answered that it has no pairing for this phone (or the pairing predates the current protocol). */
class UnpairedException : Exception("this PC no longer has this phone paired")

/** The PC answered that another phone is connected to it (one at a time). */
class BusyException(val otherPhone: String) : Exception("PC is connected to $otherPhone")

/**
 * One way of reaching the PC (USB, Wi-Fi, discovered address, relay): the result of [connect], or null if this route
 * didn't work and the next one should be tried.
 *
 * [UnpairedException] and [BusyException] are not route failures. They are the PC's own answer (it is sent inside the
 * certificate-pinned TLS link), so they are passed on: no other route is tried and the user is told what is wrong.
 */
inline fun <T : Any> tryRoute(onFailure: (Exception) -> Unit = {}, connect: () -> T): T? =
    try {
        connect()
    } catch (e: UnpairedException) {
        throw e
    } catch (e: BusyException) {
        throw e
    } catch (e: Exception) {
        onFailure(e)
        null
    }
