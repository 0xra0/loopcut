#!/usr/bin/env bash
# Build the Loopcut Blender fork on Linux. No installation unless --install is passed.
# Uses isolated build-headless-glogfix/, build-py-glogfix/ and build-full-glogfix/.
set -Eeuo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
JOBS="${LOOPCUT_BUILD_JOBS:-16}"
HIP="${LOOPCUT_HIP:-auto}"
DRY_RUN=0
INSTALL=0
RUN_TESTS=0
LIBDIR="${LOOPCUT_LIBDIR:-}"
# Use an isolated cache: old build-full/ could retain bundled-glog linker artefacts.
BUILD_SUFFIX="${LOOPCUT_BUILD_SUFFIX:--glogfix}"
PROFILES=()

usage() {
  cat <<'EOF'
Usage: ./build-loopcut.sh [options]

Without profile flags: build all three (headless, bpy, full Blender).
  --all              Build all three profiles (default)
  --full             Build the GUI Blender fork (build-full-glogfix)
  --py, --bpy        Build the bpy Python module (build-py-glogfix)
  --headless         Build headless Blender (build-headless-glogfix)
  --jobs N, -j N     Concurrent compiler jobs (default: 2; 32 GiB RAM friendly)
  --hip auto|on|off  Cycles AMD HIP support (default: auto)
  --libdir PATH      Blender precompiled libraries directory (optional)
  --glog-build-dir-suffix SUFFIX  Build directory suffix (default: -glogfix)
  --tests            Run Python addon regression tests after compiling
  --install          Verify built executables, then SYSTEM install to /usr (sudo)
  --dry-run          Print commands without configuring, compiling or installing
  -h, --help         Show this help

Examples:
  ./build-loopcut.sh                         # Build all; do NOT install
  ./build-loopcut.sh --full -j 2             # GUI only
  ./build-loopcut.sh --py --full --tests     # bpy and GUI; test addon
  ./build-loopcut.sh --all --install         # Install to /usr: BE CAREFUL
  ./build-loopcut.sh --dry-run               # Show commands first
  ./build-loopcut.sh --full --install        # Verify then install GUI build

IMPORTANT: --install replaces OS files such as /usr/bin/blender. Never run
--install when Blender is open. Check distro-managed files before installing.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '\n[loopcut-build] %s\n' "$*" >&2; }
run() {
  printf '+ ' >&2
  printf '%q ' "$@" >&2
  printf '\n' >&2
  if (( ! DRY_RUN )); then
    "$@"
  fi
}

while (( $# )); do
  case "$1" in
    --all) PROFILES=(headless py full); shift ;;
    --full) PROFILES+=(full); shift ;;
    --headless) PROFILES+=(headless); shift ;;
    --py|--bpy) PROFILES+=(py); shift ;;
    --jobs|-j) (( $# >= 2 )) || fail "$1 requires an argument"; JOBS="$2"; shift 2 ;;
    --hip) (( $# >= 2 )) || fail '--hip requires auto, on or off'; HIP="$2"; shift 2 ;;
    --libdir) (( $# >= 2 )) || fail '--libdir requires a path'; LIBDIR="$2"; shift 2 ;;
    --glog-build-dir-suffix) (( $# >= 2 )) || fail '--glog-build-dir-suffix requires an argument'; BUILD_SUFFIX="$2"; shift 2 ;;
    --tests) RUN_TESTS=1; shift ;;
    --install) INSTALL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1 (see --help)" ;;
  esac
done

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || fail 'jobs must be a positive integer'
[[ "$HIP" == auto || "$HIP" == on || "$HIP" == off ]] || fail 'HIP must be auto, on or off'
[[ "$BUILD_SUFFIX" =~ ^[-_][a-zA-Z0-9_-]+$ ]] || fail 'Build directory suffix must start with - or _ and contain only alphanumerics, _ or -'
[[ -f "$ROOT/CMakeLists.txt" ]] || fail "CMakeLists.txt not found under $ROOT; copy script to the root of the Loopcut checkout"
[[ -f "$ROOT/build_files/cmake/config/blender_full.cmake" ]] || fail 'Blender CMake presets missing: incomplete source checkout'
if (( ${#PROFILES[@]} == 0 )); then PROFILES=(headless py full); fi
for cmd in cmake ninja; do command -v "$cmd" >/dev/null 2>&1 || fail "missing command: $cmd"; done

if [[ -z "$LIBDIR" && -d "$ROOT/lib/linux_x64" ]]; then
  if find "$ROOT/lib/linux_x64" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | grep -q .; then
    LIBDIR="$ROOT/lib/linux_x64"
  fi
fi
if [[ -n "$LIBDIR" ]]; then
  [[ -d "$LIBDIR" ]] || fail "--libdir does not exist: $LIBDIR"
fi

# A failed clone can leave git-lfs assets absent. Warn; never reset working-tree changes.
asset="$ROOT/assets/brushes/essentials_brushes-curve_sculpt.blend"
if [[ ! -f "$asset" ]] || head -c 100 "$asset" 2>/dev/null | grep -q 'version https://git-lfs.github.com/spec/v1'; then
  log 'WARNING: some Blender Git LFS assets may be missing (earlier clone reported LFS HTTP 404).'
  log 'If install later fails on missing assets, run make update; do not git reset --hard over your patches.'
fi

ROCM="${ROCM_PATH:-/opt/rocm}"
if [[ "$HIP" == auto ]]; then
  if [[ -x "$ROCM/bin/hipcc" ]] || command -v hipcc >/dev/null 2>&1; then HIP=on; else HIP=off; fi
fi
if [[ "$HIP" == on ]] && [[ ! -x "$ROCM/bin/hipcc" ]] && ! command -v hipcc >/dev/null 2>&1; then
  fail "HIP enabled but hipcc not found in $ROCM/bin or PATH; use --hip off if appropriate"
fi

log "root=$ROOT; jobs=$JOBS; HIP=$HIP; glog=system; gflags=system; profiles=${PROFILES[*]}"
if (( INSTALL )); then
  log 'WARNING: --install overwrites system Blender files under /usr; ensure Blender is closed.'
  command -v sudo >/dev/null 2>&1 || fail 'sudo required for --install'
fi

# Presets must be loaded before -D overrides. Blender has different installation rules
# for GUI, bpy and headless; WITH_INSTALL_PORTABLE=OFF allows intentional system
# installation at /usr via the explicit --install flag.
for profile in "${PROFILES[@]}"; do
  case "$profile" in
    headless) build="$ROOT/build-headless${BUILD_SUFFIX}"; preset=blender_headless.cmake ;;
    py)       build="$ROOT/build-py${BUILD_SUFFIX}";       preset=bpy_module.cmake ;;
    full)     build="$ROOT/build-full${BUILD_SUFFIX}";     preset=blender_full.cmake ;;
  esac
  args=(
    -S "$ROOT" -B "$build" -G Ninja
    -C "$ROOT/build_files/cmake/config/$preset"
    -DCMAKE_BUILD_TYPE=Release
    -DWITH_INSTALL_PORTABLE=OFF
    -DCMAKE_INSTALL_PREFIX=/usr
    -DWITH_SYSTEM_GLOG=ON
    -DWITH_SYSTEM_GFLAGS=ON
    -DWITH_CYCLES_DEVICE_HIP="$([[ "$HIP" == on ]] && printf ON || printf OFF)"
    -DWITH_CYCLES_DEVICE_HIPRT=OFF
  )
  if [[ -n "$LIBDIR" ]]; then args+=(-DLIBDIR="$LIBDIR"); fi
  if [[ "$HIP" == on && -d "$ROCM" ]]; then
    args+=(-DHIP_ROOT_DIR="$ROCM")
  fi
  log "Configuring $profile -> $build"
  run cmake "${args[@]}"
  log "Building $profile ($JOBS job(s))"
  # Build DEFAULT/all target, not only --target blender. Blender's installer also
  # needs blender-thumbnailer (if enabled) and other install dependencies.
  run cmake --build "$build" --parallel "$JOBS"
done

# Validate ALL requested executables and install prerequisites before changing
# anything in /usr. This prevents partial installs caused by missing thumbnailer
# or glog's duplicate flags crashing blender --help.
if (( ! DRY_RUN )); then
  for profile in "${PROFILES[@]}"; do
    [[ "$profile" == py ]] && continue
    build="$ROOT/build-$profile${BUILD_SUFFIX}"
    candidate="$build/bin/blender"
    [[ -x "$candidate" ]] || fail "built $profile executable missing: $candidate"
    logfile="$(mktemp)"
    if ! "$candidate" --help >/dev/null 2>"$logfile"; then
      cat "$logfile" >&2
      rm -f "$logfile"
      fail "$candidate --help failed (possible duplicate glog); nothing installed"
    fi
    if ! "$candidate" --version >/dev/null 2>"$logfile"; then
      cat "$logfile" >&2
      rm -f "$logfile"
      fail "$candidate --version failed; nothing installed"
    fi
    rm -f "$logfile"
    if [[ "$profile" == full ]] && grep -Eq '^WITH_BLENDER_THUMBNAILER:BOOL=(ON|TRUE|1)$' "$build/CMakeCache.txt"; then
      [[ -x "$build/bin/blender-thumbnailer" ]] || fail \
        "Missing $build/bin/blender-thumbnailer. Build the default/all target; nothing installed."
    fi
    log "Validated $profile Blender --help and --version; no glog startup collision."
  done
fi

if (( RUN_TESTS )); then
  command -v python3 >/dev/null 2>&1 || fail 'python3 required for --tests'
  log 'Running Loopcut unit tests (without Blender)'
  if (( DRY_RUN )); then
    run python3 -m unittest discover -s "$ROOT/loopcut/tests" -v
  else
    (cd "$ROOT" && python3 -m unittest discover -s loopcut/tests -v)
  fi
fi

if (( INSTALL )); then
  # Restore original user's sequence: headless executable, bpy, then full GUI.
  has_profile() { local wanted="$1" item; for item in "${PROFILES[@]}"; do [[ "$item" == "$wanted" ]] && return 0; done; return 1; }
  if has_profile headless; then
    bin="$ROOT/build-headless${BUILD_SUFFIX}/bin/blender"
    if (( DRY_RUN )) || [[ -f "$bin" ]]; then
      run sudo install -Dm755 "$bin" /usr/bin/blender-headless
    else
      fail "headless executable missing: $bin"
    fi
  fi
  if has_profile py; then run sudo cmake --install "$ROOT/build-py${BUILD_SUFFIX}"; fi
  if has_profile full; then run sudo cmake --install "$ROOT/build-full${BUILD_SUFFIX}"; fi
fi

log 'Finished successfully.'
log "Build directories: build-headless${BUILD_SUFFIX}/, build-py${BUILD_SUFFIX}/, build-full${BUILD_SUFFIX}/"
if (( ! INSTALL )); then
  log 'Nothing was installed. After checking the build, use --install explicitly if you intend a system replacement.'
fi
