# R8 rules for the release build.

# Crypto: BouncyCastle is used for SPAKE2/Ed25519 and looks up some classes by name; keep it whole (size cost is small
# next to the risk of breaking pairing).
-keep class org.bouncycastle.** { *; }
-dontwarn org.bouncycastle.**

# QR scanner (zxing-android-embedded) resolves its activity/views from XML and reflection.
-keep class com.journeyapps.barcodescanner.** { *; }
-keep class com.google.zxing.** { *; }
-dontwarn com.google.zxing.**

# Android components referenced only from the manifest are kept automatically; the accessibility/notification services
# are bound by the system, keep them and their entry points explicitly.
-keep class dev.phonebridge.InputService { *; }
-keep class dev.phonebridge.NotificationRelay { *; }
-keep class dev.phonebridge.BridgeService { *; }

# Remove verbose/debug/info logging from the release build (warnings and errors stay).
-assumenosideeffects class android.util.Log {
    public static int v(...);
    public static int d(...);
    public static int i(...);
}
