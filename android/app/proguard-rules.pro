# R8 rules for the jpfoodmap shell.
#
# Why shrink at all: Credential Manager drags in Play services auth, which is most of the
# APK, and only a sliver of it is reachable from this app. Unminified the APK is several
# MB bigger for no gain, and every one of those MB is pushed through Dropbox to every
# synced device on every rebuild.

# Shrink, but never rename. Every reflective lookup here is by class name — the manifest's
# component names, AndroidX Startup's initializers, Play services' provider discovery —
# and keeping the original names means all of them still resolve without a rule per
# library. The size win is tree-shaking, not obfuscation, so this costs essentially
# nothing. It also keeps stack traces from the phone readable.
-dontobfuscate

# Our own classes: entry points reached by name from the manifest and from the WebView
# bridge. There are a dozen of them and the whole package is a rounding error against what
# R8 removes.
-keep class com.fredhli.jpfoodmap.** { *; }
