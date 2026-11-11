#!/usr/bin/env bash
# Rebuild Loopcut's full Blender executable with ONE glog implementation.
# Deliberately does not modify or install /usr/bin/blender.
set -Eeuo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BUILD="${LOOPCUT_GLOG_BUILD_DIR:-$ROOT/build-full-glogfix}"
JOBS="${LOOPCUT_BUILD_JOBS:-16}"
ROCM="${ROCM_PATH:-/opt/rocm}"
LIBDIR="${LOOPCUT_LIBDIR:-}"
HIP="${LOOPCUT_HIP:-auto}"
GFLAGS="${LOOPCUT_SYSTEM_GFLAGS:-ON}"

fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '\n[loopcut-glog] %s\n' "$*" >&2; }

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || fail 'LOOPCUT_BUILD_JOBS must be a positive integer.'
[[ "$GFLAGS" == ON || "$GFLAGS" == OFF ]] || fail 'LOOPCUT_SYSTEM_GFLAGS must be ON or OFF.'
[[ -f "$ROOT/CMakeLists.txt" ]] || fail "Missing $ROOT/CMakeLists.txt; place this script in the Loopcut repository root."
[[ -f "$ROOT/build_files/cmake/config/blender_full.cmake" ]] || fail 'Missing Blender full build preset.'
for prog in cmake ninja; do command -v "$prog" >/dev/null 2>&1 || fail "Missing $prog"; done

if [[ "$HIP" == auto ]]; then
  if [[ -x "$ROCM/bin/hipcc" ]] || command -v hipcc >/dev/null 2>&1; then
    HIP=ON
  else
    HIP=OFF
  fi
elif [[ "$HIP" == on || "$HIP" == ON ]]; then
  HIP=ON
elif [[ "$HIP" == off || "$HIP" == OFF ]]; then
  HIP=OFF
else
  fail 'LOOPCUT_HIP must be auto, on, or off.'
fi

# Keep the new build completely separate from build-full, which may still have
# cached linkage to the bundled glog from earlier builds.
args=(
  -S "$ROOT" -B "$BUILD" -G Ninja
  -C "$ROOT/build_files/cmake/config/blender_full.cmake"
  -DCMAKE_BUILD_TYPE=Release
  -DWITH_SYSTEM_GLOG=ON
  -DWITH_SYSTEM_GFLAGS="$GFLAGS"
  -DWITH_CYCLES_DEVICE_HIP="$HIP"
  -DWITH_CYCLES_DEVICE_HIPRT=OFF
  -DWITH_INSTALL_PORTABLE=OFF
  -DCMAKE_INSTALL_PREFIX=/usr
)
if [[ -z "$LIBDIR" && -d "$ROOT/lib/linux_x64" ]]; then
  if find "$ROOT/lib/linux_x64" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    LIBDIR="$ROOT/lib/linux_x64"
  fi
fi
if [[ -n "$LIBDIR" ]]; then
  [[ -d "$LIBDIR" ]] || fail "LOOPCUT_LIBDIR does not exist: $LIBDIR"
  args+=(-DLIBDIR="$LIBDIR")
fi
if [[ "$HIP" == ON && -d "$ROCM" ]]; then
  args+=(-DHIP_ROOT_DIR="$ROCM")
fi

log "Source: $ROOT"
log "Isolated build: $BUILD"
log "Glog=system; Gflags=system:$GFLAGS; HIP:$HIP; Jobs:$JOBS"
log 'Configuring a separate build (the existing Blender binary is untouched).'
cmake "${args[@]}"

# The default all target also builds blender-thumbnailer and other install prerequisites.
log 'Building all targets required for a complete installation.'
cmake --build "$BUILD" --parallel "$JOBS"

BLENDER="$BUILD/bin/blender"
[[ -x "$BLENDER" ]] || fail "Built binary not found at $BLENDER"
# Prevent the previous install failure: --target blender alone does not produce this.
if grep -Eq '^WITH_BLENDER_THUMBNAILER:BOOL=ON$' "$BUILD/CMakeCache.txt"; then
  [[ -x "$BUILD/bin/blender-thumbnailer" ]] || fail \
    "Missing $BUILD/bin/blender-thumbnailer; build the default all target before installing."
fi

log 'Checking new Blender executable for startup and duplicate-glog errors.'
logfile="$(mktemp)"
trap 'rm -f -- "$logfile"' EXIT
if ! "$BLENDER" --help > /dev/null 2>"$logfile"; then
  cat "$logfile" >&2
  fail 'Candidate binary failed --help. Do NOT install. Check glog dependencies (ldd, CMakeCache).'
fi
if ! "$BLENDER" --version > /dev/null 2>"$logfile"; then
  cat "$logfile" >&2
  fail 'Candidate binary failed --version. Do NOT install.'
fi

log 'PASS: candidate Blender starts successfully with --help and --version.'
printf '\nCandidate: %s\n' "$BLENDER"
printf 'No system files have been replaced. Test a real Blender session before installing.\n'
printf 'After confirming it works, install only if you intentionally want to replace your system Blender:\n'
printf '  sudo cmake --install %q\n' "$BUILD"
