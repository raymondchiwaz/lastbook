#!/usr/bin/env bash
# Install this source checkout on Linux. No model or library is downloaded until setup is opened.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VULKAN=OFF
SYSTEM_DEPS=1
for arg in "$@"; do
  case "$arg" in
    --cpu) VULKAN=OFF ;;
    --gpu) VULKAN=ON ;;
    --skip-system-deps) SYSTEM_DEPS=0 ;;
    --help|-h)
      echo 'Usage: bash install-low-vram.sh [--cpu|--gpu] [--skip-system-deps]'
      echo 'Default: CPU mode (works without GPU memory). --gpu builds optional Vulkan acceleration.'
      echo 'JOBS=2 limits build memory; PYTHON=/usr/bin/python3 selects the system Python.'
      exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done
case "$(uname -s):$(uname -m)" in Linux:x86_64|Linux:aarch64) ;; *) echo 'Requires x86_64 or aarch64 Linux.' >&2; exit 1 ;; esac
if [ "$SYSTEM_DEPS" = 1 ]; then
  ADMIN=()
  if [ "$(id -u)" != 0 ]; then
    command -v sudo >/dev/null || { echo 'Install the system dependencies in docs/LINUX-2GB.md, then use --skip-system-deps.' >&2; exit 1; }
    ADMIN=(sudo)
  fi
  if command -v apt-get >/dev/null; then
    "${ADMIN[@]}" apt-get update
    packages=(python3 python3-venv python3-pip python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
              build-essential git cmake ninja-build pkg-config binutils libgstreamer-plugins-base1.0-dev
              gstreamer1.0-libav gstreamer1.0-plugins-good)
    if [ "$VULKAN" = ON ]; then packages+=(glslc libvulkan-dev spirv-headers vulkan-tools); fi
    "${ADMIN[@]}" apt-get install -y "${packages[@]}"
  elif command -v dnf >/dev/null; then
    packages=(python3 python3-pip python3-gobject gtk3 webkit2gtk4.1 gcc gcc-c++ git cmake ninja-build
              pkgconf-pkg-config binutils gstreamer1-plugins-base-devel gstreamer1-plugins-good)
    if [ "$VULKAN" = ON ]; then packages+=(glslc vulkan-loader-devel spirv-headers vulkan-tools); fi
    "${ADMIN[@]}" dnf install -y "${packages[@]}"
  else
    echo 'Install the system dependencies in docs/LINUX-2GB.md, then use --skip-system-deps.' >&2
    exit 1
  fi
fi
PYTHON="${PYTHON:-/usr/bin/python3}"
"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11 or newer is required.')
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
gi.require_version('Soup', '3.0')
PY
"$PYTHON" -m venv --system-site-packages "$ROOT/.venv-low-vram"
"$ROOT/.venv-low-vram/bin/python" -m pip install --only-binary=:all: -r "$ROOT/packaging/linux/requirements_linux.txt"
VULKAN="$VULKAN" CPU_VARIANTS=OFF JOBS="${JOBS:-2}" bash "$ROOT/packaging/llm/build_llm_linux.sh"
mkdir -p "$ROOT/derived/gst"
cc -shared -fPIC -O2 -Wall -Wextra -o "$ROOT/derived/gst/libgstlastbooksrc.so" \
  "$ROOT/packaging/linux/gst/lastbooksrc.c" $(pkg-config --cflags --libs gstreamer-base-1.0)
# Remember the chosen build so a plain launch uses the corresponding memory policy.
echo "$VULKAN" > "$ROOT/derived/llm/low-vram-backend"
echo
echo "Ready. Start Lastbook with: bash \"$ROOT/run-low-vram.sh\""
echo 'In setup, download the Small library and select Qwen3.5 local AI. Setup checks the model checksum.'
echo 'No state map is required. Map detail is mainly for the United States.'
