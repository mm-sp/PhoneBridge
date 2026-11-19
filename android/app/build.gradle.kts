import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.phonebridge"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.phonebridge"
        minSdk = 26
        // Deliberately 29: keeps legacy (full) external-storage access for the Files tab
        // and avoids Android 11's package-visibility filtering for the Apps tab. This is a
        // sideloaded personal app, never published to Play, so the Play target floor
        // doesn't apply.
        targetSdk = 29
        versionCode = 14
        versionName = "1.4.0"
    }

    // Release signing: credentials live in android/keystore.properties (untracked). Without it the release build
    // falls back to the debug key and says so, so a missing key can never be mistaken for a real release.
    val keystoreProps = Properties().apply {
        val f = rootProject.file("keystore.properties")
        if (f.exists()) f.inputStream().use { load(it) }
    }
    signingConfigs {
        if (keystoreProps.containsKey("storeFile")) {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: run {
                logger.warn("WARNING: android/keystore.properties missing - release APK is signed with the DEBUG key")
                signingConfigs.getByName("debug")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    lint {
        checkReleaseBuilds = false
        abortOnError = false
        disable += "ExpiredTargetSdkVersion"
    }

    packaging {
        resources {
            excludes += setOf("META-INF/versions/9/OSGI-INF/MANIFEST.MF", "META-INF/*.kotlin_module")
        }
    }
}

dependencies {
    implementation("org.bouncycastle:bcprov-jdk18on:1.87")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    // zxing-android-embedded calls ContextCompat but only declares androidx.core as optional.
    implementation("androidx.core:core:1.13.1")
}
