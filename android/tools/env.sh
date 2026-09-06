# Source this from a WSL shell before any Android work on jpfoodmap. It layers this
# project's two locations over the machine's toolchain (~/tools/android-env.sh: JDK 17,
# Android SDK, Gradle 8.14.5):
#
#   JPFM_ANDROID_SRC    this repo's android/ — the sources, on /mnt/d, tracked by git
#   JPFM_ANDROID_BUILD  an ext4 scratch copy where Gradle actually runs (build.sh syncs it)
#
# Gradle never runs on /mnt/d: a build tree over 9p is slow, its file watching is
# unreliable, and Dropbox would sync every intermediate. So edits happen here, builds happen
# there, and build.sh is the bridge. NOTHING under JPFM_ANDROID_BUILD is a source — it is
# deleted and re-synced freely.
#
# Parallel work (docs/PLAN.md §10): set JPFM_ANDROID_BUILD to your own directory
#   JPFM_ANDROID_BUILD=$HOME/.cache/jpfoodmap-android-<task> ./build.sh assembleDebug
# and this file will respect it. Sharing ~/.gradle between them is safe.
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
export JPFM_ANDROID_TOOLS="$_here"
export JPFM_ANDROID_SRC="$(cd "$_here/.." && pwd -P)"
export JPFM_ANDROID_BUILD="${JPFM_ANDROID_BUILD:-$HOME/.cache/jpfoodmap-android}"
unset _here
if [ -r "$HOME/tools/android-env.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/tools/android-env.sh"
else
    echo "env.sh: ~/tools/android-env.sh is missing — the toolchain is not installed on this box" >&2
    echo "        (tools/android-env.sh in this folder is the reference copy of what it sets)" >&2
fi
