// jpfoodmap Android shell — root build file. The version pins live here and in
// app/build.gradle.kts, and they are the SAME SET the dashboard shell in
// ../../dashboard/android has been building and running on this box (and on Fred's
// Fold 8) for weeks. Copied rather than re-chosen on purpose: the four numbers are
// coupled, and re-deriving them is how a working Android build breaks.
//
//  * AGP 8.11.1 — compileSdk 36 (Android 16) needs AGP >= 8.10; the shell wants the
//    platform's own predictive-back and mandatory edge-to-edge at targetSdk 36 rather
//    than a compatibility path. 8.11.1 is the newest AGP the rest of this set allows.
//  * Kotlin 2.2.21 — the Kotlin Gradle Plugin compatibility matrix puts 2.2.20-2.2.21
//    at Gradle <= 8.14 and AGP <= 8.11.1. The wrapper is Gradle 8.14.5 on JDK 17, which
//    sits inside that box.
//
// No Compose plugin and no google-services here: this app draws no Compose (there is no
// widget) and talks to no Firebase (there is no push source) — see docs/PLAN.md D10.
//
// Fallback ladder if this set will not resolve (docs/PLAN.md §5.3; record which rung was
// taken in docs/STATUS.md):
//   1. AGP 8.10.1 + Kotlin 2.1.0
//   2. credentials 1.3.0 + googleid 1.1.1  (app/build.gradle.kts)
//   3. compileSdk 35 + core-ktx 1.16.0 / activity 1.10.1 / webkit 1.14.0 /
//      browser 1.8.0 / core-splashscreen 1.0.1
plugins {
    id("com.android.application") version "8.11.1" apply false
    id("org.jetbrains.kotlin.android") version "2.2.21" apply false
}
