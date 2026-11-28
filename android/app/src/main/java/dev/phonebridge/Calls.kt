package dev.phonebridge

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioManager
import android.net.Uri
import android.os.Bundle
import android.provider.CallLog
import android.provider.ContactsContract
import android.telecom.TelecomManager
import android.telephony.PhoneStateListener
import android.telephony.TelephonyManager
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject

/**
 * Call history, contacts, dialing and in-call control.
 *
 * The call's *audio* always stays on the phone (or whatever headset is connected to
 * it): Android gives no third-party app a way to route cellular call audio elsewhere.
 */
class Calls(private val svc: BridgeService) {
    private val tm = svc.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
    private val telecom = svc.getSystemService(Context.TELECOM_SERVICE) as TelecomManager
    private val audio = svc.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var lastState = TelephonyManager.CALL_STATE_IDLE
    private var currentNumber = ""
    private var listening = false

    @Suppress("DEPRECATION")
    private val listener = object : PhoneStateListener() {
        override fun onCallStateChanged(state: Int, number: String?) {
            if (!number.isNullOrBlank()) currentNumber = number
            val names = ContactNames(svc)
            when (state) {
                TelephonyManager.CALL_STATE_RINGING -> svc.later {
                    svc.send("call.ringing", JSONObject().put("number", currentNumber)
                        .put("caller_name", names.lookup(currentNumber) ?: ""))
                }
                TelephonyManager.CALL_STATE_OFFHOOK -> svc.later {
                    svc.send("call.state", JSONObject().put("state", "active").put("number", currentNumber)
                        .put("caller_name", names.lookup(currentNumber) ?: "")
                        .put("direction", if (lastState == TelephonyManager.CALL_STATE_RINGING) "incoming" else "outgoing"))
                }
                TelephonyManager.CALL_STATE_IDLE -> if (lastState != TelephonyManager.CALL_STATE_IDLE) {
                    svc.send("call.state", JSONObject().put("state", "ended").put("number", currentNumber))
                    currentNumber = ""
                    runCatching { audio.isMicrophoneMute = false }
                    // The call log row is written a moment after the call ends.
                    svc.main.postDelayed({ svc.later { runCatching { svc.send("calls.history", history(0, 20)) } } }, 2000)
                }
            }
            lastState = state
        }
    }

    @Suppress("DEPRECATION")
    fun startListening() {
        runCatching {
            tm.listen(listener, PhoneStateListener.LISTEN_CALL_STATE)
            listening = true
        }.onFailure { Log.w(BridgeService.TAG, "can't watch call state: $it") }
    }

    @Suppress("DEPRECATION")
    fun stopListening() {
        if (listening) tm.listen(listener, PhoneStateListener.LISTEN_NONE)
    }

    /** One page of the call log, newest first. */
    fun history(offset: Int, limit: Int, onProgress: (Int, Int) -> Unit = { _, _ -> }): JSONObject {
        val arr = JSONArray()
        var total = 0
        svc.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            arrayOf(CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME, CallLog.Calls.TYPE, CallLog.Calls.DATE, CallLog.Calls.DURATION),
            null, null, "${CallLog.Calls.DATE} DESC",
        )?.use { c ->
            total = c.count
            val page = minOf(limit, (total - offset).coerceAtLeast(0))
            onProgress(0, page)
            if (offset < total && c.moveToPosition(offset)) {
                do {
                    arr.put(JSONObject()
                        .put("number", c.getString(0) ?: "")
                        .put("name", c.getString(1) ?: "")
                        .put("type", when (c.getInt(2)) {
                            CallLog.Calls.INCOMING_TYPE -> "incoming"
                            CallLog.Calls.OUTGOING_TYPE -> "outgoing"
                            CallLog.Calls.MISSED_TYPE -> "missed"
                            CallLog.Calls.REJECTED_TYPE -> "rejected"
                            CallLog.Calls.BLOCKED_TYPE -> "blocked"
                            CallLog.Calls.VOICEMAIL_TYPE -> "voicemail"
                            else -> "other"
                        })
                        .put("ts", c.getLong(3))
                        .put("duration", c.getLong(4)))
                    onProgress(arr.length(), page)
                } while (arr.length() < limit && c.moveToNext())
            }
        }
        return JSONObject().put("calls", arr).put("offset", offset).put("total", total)
            .put("has_more", offset + arr.length() < total)
    }

    /** One page of contacts that have a phone number, A-Z, each with all its numbers. */
    fun contacts(offset: Int, limit: Int, onProgress: (Int, Int) -> Unit = { _, _ -> }): JSONObject {
        val byId = LinkedHashMap<Long, JSONObject>()
        var total = 0
        svc.contentResolver.query(
            ContactsContract.Contacts.CONTENT_URI,
            arrayOf(ContactsContract.Contacts._ID, ContactsContract.Contacts.DISPLAY_NAME, ContactsContract.Contacts.STARRED),
            "${ContactsContract.Contacts.HAS_PHONE_NUMBER} = 1", null,
            "${ContactsContract.Contacts.DISPLAY_NAME} COLLATE NOCASE ASC",
        )?.use { c ->
            total = c.count
            if (offset < total && c.moveToPosition(offset)) {
                do {
                    byId[c.getLong(0)] = JSONObject().put("id", c.getLong(0)).put("name", c.getString(1) ?: "")
                        .put("starred", c.getInt(2) == 1).put("numbers", JSONArray())
                } while (byId.size < limit && c.moveToNext())
            }
        }
        onProgress(0, byId.size)
        if (byId.isNotEmpty()) {
            svc.contentResolver.query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                arrayOf(ContactsContract.CommonDataKinds.Phone.CONTACT_ID, ContactsContract.CommonDataKinds.Phone.NUMBER,
                    ContactsContract.CommonDataKinds.Phone.TYPE),
                "${ContactsContract.CommonDataKinds.Phone.CONTACT_ID} IN (${byId.keys.joinToString(",")})", null, null,
            )?.use { c ->
                while (c.moveToNext()) {
                    val contact = byId[c.getLong(0)] ?: continue
                    val number = c.getString(1) ?: continue
                    val numbers = contact.getJSONArray("numbers")
                    val normalized = number.filter { it.isDigit() || it == '+' }
                    val dup = (0 until numbers.length()).any {
                        numbers.getJSONObject(it).getString("number").filter { ch -> ch.isDigit() || ch == '+' } == normalized
                    }
                    if (!dup) {
                        val label = ContactsContract.CommonDataKinds.Phone.getTypeLabel(svc.resources, c.getInt(2), "").toString()
                        numbers.put(JSONObject().put("number", number).put("label", label))
                    }
                }
            }
        }
        onProgress(byId.size, byId.size)
        return JSONObject().put("contacts", JSONArray(byId.values)).put("offset", offset).put("total", total)
            .put("has_more", offset + byId.size < total)
    }

    @SuppressLint("MissingPermission")
    fun dial(number: String) {
        if (number.isBlank()) return
        try {
            // placeCall works from the background (unlike startActivity(ACTION_CALL)).
            telecom.placeCall(Uri.fromParts("tel", number, null), Bundle())
        } catch (e: SecurityException) {
            svc.send("error", JSONObject().put("message", "Allow the Phone permission for PhoneBridge to place calls"))
        }
    }

    @SuppressLint("MissingPermission")
    fun action(action: String) {
        try {
            when (action) {
                "answer" -> telecom.acceptRingingCall()
                // endCall() both declines a ringing call and hangs up an active one (API 28+).
                "decline", "end" -> {
                    val ok = if (android.os.Build.VERSION.SDK_INT >= 28) telecom.endCall() else false
                    if (!ok) svc.send("error", JSONObject().put("message", "The phone refused to end the call"))
                }
                "mute" -> audio.isMicrophoneMute = true
                "unmute" -> audio.isMicrophoneMute = false
                "speaker_on" -> audio.isSpeakerphoneOn = true
                "speaker_off" -> audio.isSpeakerphoneOn = false
            }
        } catch (e: SecurityException) {
            svc.send("error", JSONObject().put("message", "Allow the Phone permission for PhoneBridge to control calls"))
        }
    }
}
