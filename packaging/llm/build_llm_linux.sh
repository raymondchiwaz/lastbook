#!/bin/bash
# Builds lastbook-llm for 64-bit Linux (Ubuntu 24.04 LTS, x86_64 or aarch64): llama.cpp at the pinned tag with Vulkan (NVIDIA, AMD,
# Intel and Arm graphics through their drivers) and every CPU variant of that architecture as a fallback, linked to our runner with nothing that can
# open a socket (no RPC backend, no common library beyond the fitting and its log). Same layout as Windows: the program and
# the llama.cpp libraries beside it, which it finds through RUNPATH $ORIGIN. fit.cpp passes C++ objects across libllama.so,
# so one compiler and one C++ runtime (the system's libstdc++) build and run all of it.
# Run on Ubuntu 24.04 x86_64 or aarch64 with:
#   apt-get install build-essential git cmake ninja-build glslc libvulkan-dev spirv-headers
#   bash packaging/llm/build_llm_linux.sh
# W  Work folder (default derived/llm). llama.cpp is cloned into W/llama.cpp at the tag if it is not there.
# Output: W/bin-linux/lastbook-llm, the .so files beside it, LICENSE and notices, deps.txt (what each file needs), SHA256SUMS.txt
# VULKAN=OFF builds a CPU-only runner (no Vulkan SDK required). JOBS limits build RAM use.
# CPU_VARIANTS=OFF builds one portable CPU backend instead of every architecture variant.
set -euo pipefail
HERE="$(dirname "$(realpath "$0")")"
ROOT="$(dirname "$(dirname "$HERE")")"
TAG=b12032       # the build the small-model evaluation ran on
COMMIT=dde879bc4f954b809892fd0d660515f42536394a
W="$(realpath -m "${W:-$ROOT/derived/llm}")"
L="$W/llama.cpp"
B="$W/build-linux"
O="$W/bin-linux"
VULKAN="${VULKAN:-ON}"
CPU_VARIANTS="${CPU_VARIANTS:-ON}"
JOBS="${JOBS:-$(nproc)}"
case "$VULKAN:$CPU_VARIANTS" in ON:ON|ON:OFF|OFF:ON|OFF:OFF) ;; *) echo "VULKAN and CPU_VARIANTS must be ON or OFF" >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64 | aarch64) ;; *) echo "build on x86_64 or aarch64 Linux" >&2; exit 1 ;; esac
mkdir -p "$W" "$B" "$O"
rm -f "$O"/*

[ -d "$L/.git" ] || git -c advice.detachedHead=false clone -q --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp.git "$L"
[ "$(git -C "$L" rev-parse HEAD)" = "$COMMIT" ] || { echo "llama.cpp is not at $TAG ($COMMIT)" >&2; exit 1; }

MAP="-ffile-prefix-map=$L=llama.cpp -ffile-prefix-map=$HERE=lastbook-llm -ffile-prefix-map=$B=build"   # no folder of this machine inside
cmake -S "$L" -B "$B" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ \
  -DBUILD_SHARED_LIBS=ON -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS="$CPU_VARIANTS" -DGGML_NATIVE=OFF -DGGML_OPENMP=ON \
  -DGGML_VULKAN="$VULKAN" -DGGML_RPC=OFF -DGGML_CCACHE=OFF \
  -DLLAMA_BUILD_COMMON=OFF -DLLAMA_BUILD_TOOLS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_SERVER=OFF \
  -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF \
  -DCMAKE_SKIP_BUILD_RPATH=OFF -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON -DCMAKE_INSTALL_RPATH='$ORIGIN' -DCMAKE_SHARED_LINKER_FLAGS="-Wl,--enable-new-dtags" -DCMAKE_MODULE_LINKER_FLAGS="-Wl,--enable-new-dtags" \
  -DCMAKE_C_FLAGS="$MAP" -DCMAKE_CXX_FLAGS="$MAP" > "$W/cmake-linux.log"
# Only the libraries: the default target also builds llama.cpp's own app, which downloads. One library per CPU variant.
CPUS="$(cmake --build "$B" --target help | { grep -oE '^ggml-cpu-[a-z0-9_.]+' || true; } | sort -u | tr '\n' ' ')"
if [ "$CPU_VARIANTS" = OFF ]; then CPUS=ggml-cpu; fi
TARGETS=(llama)
if [ "$VULKAN" = ON ]; then TARGETS+=(ggml-vulkan); fi
cmake --build "$B" -j "$JOBS" --target "${TARGETS[@]}" $CPUS > "$W/build-linux.log"

# fit_unused_win.cpp: GNU ld, like the Windows linker, wants every reference in fit.cpp resolved.
g++ -std=c++17 -O3 -DNDEBUG $MAP \
  -I"$L/include" -I"$L/ggml/include" -I"$L/common" -I"$L/vendor" -I"$L/src" \
  "$HERE/lastbook_llm.cpp" "$L/common/fit.cpp" "$L/common/log.cpp" "$L/common/json.cpp" "$HERE/fit_unused_win.cpp" \
  -L"$B/bin" -lllama -lggml -lggml-base -pthread \
  -Wl,-rpath,'$ORIGIN' -Wl,--enable-new-dtags -Wl,--as-needed -s -o "$O/lastbook-llm"

# The program and the llama.cpp libraries it loads. The CPU variants are all kept: ggml picks the best one this processor runs,
# from SSE4.2 machines up. One real file each, under the name it is asked for by (the linked libraries by their SONAME).
for f in "$B"/bin/libllama.so "$B"/bin/libggml.so "$B"/bin/libggml-base.so; do
  cp -L "$f" "$O/$(objdump -p "$f" | sed -n 's/^ *SONAME *//p')"
done
if [ "$VULKAN" = ON ]; then cp "$B/bin/libggml-vulkan.so" "$O/"; fi
cp "$B"/bin/libggml-cpu*.so "$O/"
strip -s "$O"/*.so*
cp "$L/LICENSE" "$O/LICENSE-llama.cpp.txt"
cat > "$O/NOTICE.txt" <<EOF
lastbook-llm: Lastbook's pipe-only runner (packaging/llm/lastbook_llm.cpp) linked to llama.cpp $TAG ($COMMIT),
MIT licence, LICENSE-llama.cpp.txt. Built with $(gcc --version | head -1) on $(. /etc/os-release; echo "$PRETTY_NAME").
Uses the system's libstdc++, libgcc, libgomp (GCC runtime library exception) and libvulkan (Apache-2.0), not shipped here.
EOF

# What each file needs. Every library it names must be beside it or a package of the system (listed for the .deb Depends).
bad=0
: > "$O/deps.txt"
for f in "$O"/lastbook-llm "$O"/*.so*; do
  needs="$(objdump -p "$f" | sed -n 's/^ *NEEDED *//p' | sort -u)"
  echo "$(basename "$f"): $(echo $needs)  [runpath: $(objdump -p "$f" | sed -n 's/^ *RUNPATH *//p')]" >> "$O/deps.txt"
  # Nothing may reach a network: no socket calls, no name lookups.
  if nm -D --undefined-only "$f" | grep -Ew '(socket|connect|bind|listen|accept|accept4|getaddrinfo|gethostbyname|gethostbyname_r|getnameinfo|sendto|recvfrom|sendmsg|recvmsg|res_query|curl_easy_init)'; then
    echo "$(basename "$f") imports network calls" >&2; bad=1
  fi
  for d in $needs; do
    case "$d" in
      libc.so.6|libm.so.6|libstdc++.so.6|libgcc_s.so.1|libgomp.so.1|libvulkan.so.1|ld-linux-x86-64.so.2|ld-linux-aarch64.so.1|libpthread.so.0|libdl.so.2) ;;
      *) [ -f "$O/$d" ] || { echo "$(basename "$f") needs $d, which is not beside it" >&2; bad=1; } ;;
    esac
  done
done
if ls "$O" | grep -qi rpc; then echo "an RPC backend was built" >&2; bad=1; fi
if grep -aEq '/(Users|home|root)/' "$O"/lastbook-llm "$O"/*.so*; then
  grep -aEo '/(Users|home|root)/[^ "]{0,60}' "$O"/lastbook-llm "$O"/*.so* | sort -u | head >&2
  echo "a file names a folder of this machine" >&2; bad=1
fi
[ "$bad" = 0 ] || exit 1
# Web addresses inside are text in llama.cpp's log and error messages; with no network calls they go nowhere.
cat "$O"/lastbook-llm "$O"/*.so* | grep -aEio 'https?://[^ "<>]+' | sort -u > "$O/addresses-in-messages.txt" || true
( cd "$O" && sha256sum lastbook-llm *.so* > SHA256SUMS.txt )
ls -l "$O"
