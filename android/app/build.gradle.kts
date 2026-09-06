// The jpfoodmap shell. One activity around https://jpfoodmap.com/ plus a settings screen.
//
// The toolchain pins are in the root build.gradle.kts; the fallback ladder for everything
// in this file is docs/PLAN.md §5.3.
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.fredhli.jpfoodmap"
    // 36 (Android 16). The shell wants the platform's own behaviour on Fred's One UI 9 /
    // Android 17 Fold 8 rather than a compatibility path: predictive back on by default,
    // mandatory edge-to-edge (which is what the insets policy in docs/STANDARDS.md §1.2
    // is written against), and the current WebView/insets contract.
    compileSdk = 36

    defaultConfig {
        // PERMANENT IDENTITY. Once this is installed on the phone it cannot change without
        // a fresh install that loses nothing (the shell stores no user data) but does lose
        // the App Links verification and the launcher position. docs/PLAN.md D1.
        applicationId = "com.fredhli.jpfoodmap"
        minSdk = 31
        targetSdk = 36
        // major*10000 + minor*100 + patch, so the code can be read back off the name and
        // is strictly increasing for as long as the version number is. 2.1.0 = 20100, and
        // it moves with the site's APP_VERSION (docs/STANDARDS.md §14.1/§14.8).
        versionCode = 20100
        versionName = "2.1.0"
    }

    buildFeatures {
        // The shell reads BuildConfig.VERSION_NAME (the UA suffix, the About line, the
        // metrics reply to the page) and BuildConfig.DEBUG (WebView contents debugging).
        // AGP 8 generates no BuildConfig unless asked, and the failure is a compile error
        // in code that looks obviously correct.
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    sourceSets {
        getByName("main").java.srcDirs("src/main/kotlin")
        getByName("test").java.srcDirs("src/test/kotlin")
    }

    lint {
        // MissingTranslation is a WARNING here, not the build-stopping error it is by
        // default, and the reason is the locale layout rather than laziness: values/ is
        // ALREADY Simplified Chinese (docs/PLAN.md D17 — Fred reads Chinese, so his
        // language is the default), values-en/ carries the English, and values-zh-rCN/
        // exists to override exactly ONE string: the launcher name, which STANDARDS §1.6
        // wants as 日本美食地图 on a zh-CN device while everyone else sees "Japan Foodmap".
        // Lint sees that one-string folder and calls every other string "not translated in
        // zh" — but a zh-CN device already resolves them from values/. Copying the whole
        // table into values-zh-rCN just to satisfy the check would create two Chinese
        // copies to keep in step, which is a real bug source in exchange for a false one.
        // Left as a warning rather than disabled so a genuinely missing values-en/ string
        // still shows up in the report.
        warning += "MissingTranslation"
    }

    buildTypes {
        debug {
            // A DIFFERENT applicationId from the sideloaded build. The debug variant is
            // what goes on the emulator; with the same id it would install straight over
            // the phone's copy on any `adb install -r`. With the suffix the two coexist.
            // Class names are NOT suffixed — the namespace is still com.fredhli.jpfoodmap
            // — so `am start -n` needs the fully qualified component.
            applicationIdSuffix = ".debug"
        }
        release {
            // The shipped artifact. R8 tree-shakes what Credential Manager and Play
            // services auth pull in but this app never calls; proguard-rules.pro turns
            // obfuscation off, so nothing that resolves a class by name can break.
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            // Signed with the debug key ON PURPOSE: this is sideloaded from Dropbox, never
            // published, it shares the certificate with the dashboard APK (same
            // assetlinks fingerprint), and sharing it with previous builds is what lets a
            // new version install over the old one. Without a signingConfig,
            // assembleRelease emits an unsigned APK the phone refuses outright.
            // docs/STANDARDS.md §14.3.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    // core-ktx: ViewCompat.setOnApplyWindowInsetsListener + WindowInsetsCompat (the insets
    //   policy, STANDARDS §1.2) and WindowInsetsControllerCompat (light system-bar icons,
    //   which this app pins on because the site is light-only).
    //   1.18.0, not 1.19.0: core 1.19 declares compileSdk 37 + AGP 9.1 as its floor and
    //   checkDebugAarMetadata fails against 36 / AGP 8.11.
    implementation("androidx.core:core-ktx:1.18.0")
    // activity-ktx: ComponentActivity, enableEdgeToEdge(), the back-pressed dispatcher and
    //   registerForActivityResult (the location permission). Back has to be a dispatcher
    //   callback, not an onBackPressed override, or predictive back (default at targetSdk
    //   36) closes the app instead of popping the page's overlay stack.
    implementation("androidx.activity:activity-ktx:1.13.0")
    // webkit: WebViewCompat.addWebMessageListener — the ONLY origin-scoped bridge into the
    //   page (addJavascriptInterface is not one, and is not used anywhere here). Plus
    //   addDocumentStartJavaScript, getCurrentWebViewPackage (the IME decision and the
    //   About line) and WebSettingsCompat.setAlgorithmicDarkeningAllowed(false).
    implementation("androidx.webkit:webkit:1.17.0")
    // browser: CustomTabsIntent — the default outbound-link policy (docs/PLAN.md D7).
    implementation("androidx.browser:browser:1.10.0")
    // core-splashscreen: the cold-start splash held until the page's first paint.
    implementation("androidx.core:core-splashscreen:1.2.0")
    // credentials + googleid: native Google sign-in. Google refuses GIS/OAuth inside a
    //   WebView (disallowed_useragent), so the id_token has to come from Credential
    //   Manager and be handed to the page (docs/PLAN.md D4). credentials-play-services-auth
    //   is the provider half; without it getCredential finds no provider at run time.
    implementation("androidx.credentials:credentials:1.5.0")
    implementation("androidx.credentials:credentials-play-services-auth:1.5.0")
    implementation("com.google.android.libraries.identity.googleid:googleid:1.1.1")
    // coroutines: Credential Manager's Kotlin API is suspend-only.
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    testImplementation("junit:junit:4.13.2")
    // org.json is a stub in local unit tests; this puts the real implementation on the
    // test classpath so the bridge's parser can be tested off-device.
    testImplementation("org.json:json:20240303")
}
