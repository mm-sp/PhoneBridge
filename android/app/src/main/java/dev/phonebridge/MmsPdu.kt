package dev.phonebridge

import java.io.ByteArrayOutputStream

class MmsPart(val mime: String, val name: String, val data: ByteArray)

/**
 * Encoder for an MMS "M-Send.req" PDU (OMA-TS-MMS-ENC-V1.2, WAP-230 WSP encoding) — the file
 * that SmsManager.sendMultimediaMessage() hands to the carrier. Pure Kotlin with no Android
 * dependencies so linux/tests/test_mms_pdu.py can compile and decode it.
 *
 * Layout: message-type, transaction-id, version, from, to..., class, priority, reports,
 * content-type (multipart/mixed, must be the last header), then the multipart body.
 */
object MmsPdu {
    private const val H_DELIVERY_REPORT = 0x86
    private const val H_FROM = 0x89
    private const val H_MESSAGE_CLASS = 0x8A
    private const val H_MESSAGE_TYPE = 0x8C
    private const val H_VERSION = 0x8D
    private const val H_PRIORITY = 0x8F
    private const val H_READ_REPORT = 0x90
    private const val H_TO = 0x97
    private const val H_TRANSACTION_ID = 0x98
    private const val H_CONTENT_TYPE = 0x84
    private const val H_CONTENT_LOCATION = 0x8E

    private const val MSG_SEND_REQ = 0x80
    private const val VERSION_1_2 = 0x92
    private const val CLASS_PERSONAL = 0x80
    private const val PRIORITY_NORMAL = 0x81
    private const val NO = 0x81
    private const val FROM_INSERT_ADDRESS = 0x81
    private const val CT_MULTIPART_MIXED = 0xA3

    // WSP well-known content types (short-integer form, high bit set).
    private val WELL_KNOWN = mapOf(
        "text/plain" to 0x83, "text/html" to 0x82, "image/gif" to 0x9D, "image/jpeg" to 0x9E,
        "image/png" to 0xA0, "image/vnd.wap.wbmp" to 0xA1,
    )
    private const val PARAM_CHARSET = 0x81
    private const val CHARSET_UTF8 = 0xEA // MIBenum 106 | 0x80

    fun build(transactionId: String, recipients: List<String>, parts: List<MmsPart>): ByteArray {
        require(recipients.isNotEmpty()) { "no recipient" }
        val out = ByteArrayOutputStream()
        out.write(H_MESSAGE_TYPE); out.write(MSG_SEND_REQ)
        out.write(H_TRANSACTION_ID); text(out, transactionId)
        out.write(H_VERSION); out.write(VERSION_1_2)
        // From: value-length 1, "insert-address-token" — the MMSC fills in our number.
        out.write(H_FROM); out.write(1); out.write(FROM_INSERT_ADDRESS)
        for (r in recipients) { out.write(H_TO); text(out, address(r)) }
        out.write(H_MESSAGE_CLASS); out.write(CLASS_PERSONAL)
        out.write(H_PRIORITY); out.write(PRIORITY_NORMAL)
        out.write(H_DELIVERY_REPORT); out.write(NO)
        out.write(H_READ_REPORT); out.write(NO)
        out.write(H_CONTENT_TYPE); out.write(CT_MULTIPART_MIXED)

        uintvar(out, parts.size)
        for (p in parts) {
            val headers = ByteArrayOutputStream()
            contentType(headers, p.mime)
            if (p.name.isNotEmpty()) { headers.write(H_CONTENT_LOCATION); text(headers, p.name) }
            uintvar(out, headers.size())
            uintvar(out, p.data.size)
            out.write(headers.toByteArray())
            out.write(p.data)
        }
        return out.toByteArray()
    }

    /** "+1 (555) 010-2030" -> "+15550102030/TYPE=PLMN"; e-mail addresses are passed through. */
    fun address(raw: String): String {
        val s = raw.trim()
        if (s.contains('@')) return s + "/TYPE=EMAIL"
        val cleaned = (if (s.startsWith("+")) "+" else "") + s.filter { it.isDigit() }
        return "$cleaned/TYPE=PLMN"
    }

    private fun contentType(out: ByteArrayOutputStream, mime: String) {
        val m = mime.lowercase()
        val known = WELL_KNOWN[m]
        when {
            m == "text/plain" -> { out.write(3); out.write(known!!); out.write(PARAM_CHARSET); out.write(CHARSET_UTF8) }
            known != null -> out.write(known)
            else -> text(out, mime)
        }
    }

    private fun text(out: ByteArrayOutputStream, s: String) {
        out.write(s.toByteArray(Charsets.UTF_8)); out.write(0)
    }

    /** WSP variable-length unsigned integer: 7 bits per byte, high bit = "more follows". */
    fun uintvar(out: ByteArrayOutputStream, value: Int) {
        var v = value
        val groups = ArrayList<Int>()
        do { groups.add(v and 0x7F); v = v ushr 7 } while (v != 0)
        for (i in groups.indices.reversed()) out.write(groups[i] or (if (i != 0) 0x80 else 0))
    }
}
