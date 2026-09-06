# android-env.sh — source this before any Android/Gradle work.
#   source ~/tools/android-env.sh
# Installed user-space (no sudo) for the Flow widget project.
# Safe to source repeatedly: PATH entries are de-duplicated.

export JAVA_HOME="$HOME/tools/jdk-17"
export ANDROID_HOME="$HOME/tools/android-sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"

# Keep the Gradle cache on fast ext4, never on /mnt/d (Dropbox/Windows drive).
export GRADLE_USER_HOME="${GRADLE_USER_HOME:-$HOME/.gradle}"

_android_env_prepend() {
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1${PATH:+:$PATH}" ;;
    esac
}

# Bootstrap Gradle. Prefer the project's ./gradlew once it exists; this install is
# here so `gradle wrapper` can generate that wrapper in the first place.
export GRADLE_HOME="$HOME/tools/gradle-8.14.5"

_android_env_prepend "$GRADLE_HOME/bin"
_android_env_prepend "$ANDROID_HOME/build-tools/35.0.0"
_android_env_prepend "$ANDROID_HOME/platform-tools"
_android_env_prepend "$ANDROID_HOME/cmdline-tools/latest/bin"
_android_env_prepend "$JAVA_HOME/bin"
export PATH

# The emulator's bundled qemu-system-x86_64 links against libpulse.so.0, which this
# distro does not ship and which cannot be apt-installed here (no sudo). The .so files
# were extracted from stock Ubuntu noble .debs into ~/tools/extra-libs using
# `apt-get download` + `dpkg-deb -x` — neither needs root. The recipe is written out in
# flow-widget-support/emu.sh. Without this, the emulator dies at load time with
# "error while loading shared libraries: libpulse.so.0" and never reaches a boot.
#
# APPENDED, never prepended: a real system library must always win. These entries only
# fill genuine gaps, so sourcing this file cannot change which libc, libstdc++, or any
# other real system library Gradle, the JDK, or adb resolve.
if [ -d "$HOME/tools/extra-libs" ]; then
    case ":${LD_LIBRARY_PATH:-}:" in
        *":$HOME/tools/extra-libs:"*) ;;
        *) export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$HOME/tools/extra-libs" ;;
    esac
fi

unset -f _android_env_prepend
