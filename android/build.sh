#!/usr/bin/env bash
#
# Build the jpfoodmap APK and publish it beside the sources.
#
#   android/build.sh                  # assembleRelease, then publish apk/jpfoodmap.apk
#   android/build.sh assembleDebug    # the unminified build, for the emulator; not published
#   android/build.sh testDebugUnitTest lintDebug   # any Gradle task(s), run in the scratch
#   DRY_RUN=1 android/build.sh        # sync + build, show what would be published
#
# The sources are this folder, on /mnt/d, tracked by git with the site they wrap. Gradle
# never runs here: a build tree over 9p is slow, its file watching is unreliable, and
# Dropbox would sync every intermediate. So the sources are rsynced to an ext4 scratch copy
# (JPFM_ANDROID_BUILD, tools/env.sh) and built there; only the finished APK comes back, to
# apk/jpfoodmap.apk, which Dropbox carries to the phone. Nothing in the scratch copy is a
# source — edit here, build there, this script is the bridge.
#
# There is no secret to guard on the way out (the dashboard's build.sh has a token tripwire;
# this app has no token — its Google client id is public and its session cookie is minted on
# the phone), so the publish step is a copy and a stamp and nothing else.
#
# Parallel builders: give each one its own scratch and serialise the publish —
#   JPFM_ANDROID_BUILD=$HOME/.cache/jpfoodmap-android-t3 ./build.sh assembleDebug
#   flock /tmp/jpfoodmap-android-build.lock ./build.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "$HERE/tools/env.sh"
APK_DST="$JPFM_ANDROID_SRC/apk/jpfoodmap.apk"

say() { printf '%s\n' "$*"; }
die() { printf 'build.sh: %s\n' "$*" >&2; exit 1; }

[ -f "$JPFM_ANDROID_SRC/settings.gradle.kts" ] || die "no settings.gradle.kts in $JPFM_ANDROID_SRC"
command -v rsync >/dev/null || die "rsync is not installed"
[ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ] || die "JAVA_HOME is not set — ~/tools/android-env.sh is missing on this box"

# Every argument is a Gradle task; with none, build the shipped artifact.
if [ "$#" -eq 0 ]; then set -- assembleRelease; fi

# ---- sync to the ext4 copy and build --------------------------------------------------
# --delete so a file removed here disappears there; the excludes are everything that is an
# output, a document or a tool rather than an input to the compiler.
mkdir -p "$JPFM_ANDROID_BUILD"
rsync -a --delete \
    --exclude=/build/ --exclude='/*/build/' --exclude=/.gradle/ --exclude=/.kotlin/ \
    --exclude=/apk/ --exclude=/docs/ --exclude=/tools/ --exclude=/local.properties \
    "$JPFM_ANDROID_SRC/" "$JPFM_ANDROID_BUILD/"
say "synced  $JPFM_ANDROID_SRC -> $JPFM_ANDROID_BUILD"
chmod +x "$JPFM_ANDROID_BUILD/gradlew"
( cd "$JPFM_ANDROID_BUILD" && ./gradlew --quiet "$@" )
say "built   $*"

# ---- publish, but only for the shipped artifact ---------------------------------------
case " $* " in *" assembleRelease "*) ;; *) say "not a release build, nothing published"; exit 0 ;; esac
APK_SRC="$JPFM_ANDROID_BUILD/app/build/outputs/apk/release/app-release.apk"
[ -f "$APK_SRC" ] || die "no APK at $APK_SRC after the build"

if [ -n "${DRY_RUN:-}" ]; then
    say "apk:    $APK_SRC -> $APK_DST   (dry run, not copied)"
    exit 0
fi

mkdir -p "$(dirname "$APK_DST")"
if [ -f "$APK_DST" ] && cmp -s "$APK_SRC" "$APK_DST"; then
    say "apk:    unchanged, not re-copied ($APK_DST)"
else
    # Copy to a temp name and rename: Dropbox uploads whatever it sees the moment it sees
    # it, and a half-written APK reaching the phone installs as "package appears to be
    # invalid". The rename is atomic.
    tmp="$(dirname "$APK_DST")/.$(basename "$APK_DST").part"
    cp -f "$APK_SRC" "$tmp"
    mv -f "$tmp" "$APK_DST"
    say "apk:    $APK_SRC -> $APK_DST"
fi

size=$(stat -c %s "$APK_DST")
built=$(date -r "$APK_SRC" '+%Y-%m-%d %H:%M:%S')
version=$(sed -n 's/.*versionName = "\([^"]*\)".*/\1/p' "$JPFM_ANDROID_SRC/app/build.gradle.kts" | head -1)
code=$(sed -n 's/.*versionCode = \([0-9]*\).*/\1/p' "$JPFM_ANDROID_SRC/app/build.gradle.kts" | head -1)
# A stamp beside the APK, so "did the new build reach the phone?" is answerable from the
# Dropbox app without a checksum tool. This file IS committed; the APK is not.
cat > "$(dirname "$APK_DST")/BUILD-INFO.txt" <<INFO
$(basename "$APK_DST")
  version   ${version:-?} (versionCode ${code:-?})
  built     $built  (local time on the build box)
  published $(date '+%Y-%m-%d %H:%M:%S')
  variant   release (R8-minified, debug-signed)
  size      $(numfmt --to=iec-i --suffix=B "$size" 2>/dev/null || echo "$size bytes")
  sha256    $(sha256sum "$APK_DST" | cut -d' ' -f1)
  from      $APK_SRC

Debug-signed, sideload only. Install: open this file in the Dropbox app on the phone
(proj_2026 > tabelog > android > apk), allow "install unknown apps" for Dropbox once,
install over the previous version. Same certificate as every earlier build, so it
upgrades in place and keeps its data.

Built by android/build.sh from the sources beside this folder.
INFO
say "stamped $(dirname "$APK_DST")/BUILD-INFO.txt"
