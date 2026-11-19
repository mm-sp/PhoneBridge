package dev.phonebridge

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Wraps small secrets (the Ed25519 identity key, the pairing key) with an AES-256-GCM key that lives in the
 * Android Keystore (hardware-backed where the phone supports it) and never leaves it. Only ciphertext is written
 * to app storage. Android's Keystore can't hold Ed25519 keys before Android 13, hence wrapping rather than storing.
 */
object KeystoreVault {
    private const val ALIAS = "phonebridge-vault-v1"

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }

    /** -> base64(iv || ciphertext || tag). [context] is authenticated (binds the blob to what it protects). */
    fun seal(plain: ByteArray, context: String): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, key())          // the Keystore picks a fresh IV
        c.updateAAD(context.toByteArray())
        return Base64.encodeToString(c.iv + c.doFinal(plain), Base64.NO_WRAP)
    }

    fun open(sealed: String, context: String): ByteArray {
        val raw = Base64.decode(sealed, Base64.DEFAULT)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, raw, 0, 12))
        c.updateAAD(context.toByteArray())
        return c.doFinal(raw, 12, raw.size - 12)
    }
}
