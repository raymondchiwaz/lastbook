# Lastbook for Linux with a 2 GB graphics card

This version adds a small local AI profile. The normal library, search, PDFs, videos and maps use the existing Linux interface.
The optional AI defaults to Qwen3.5 2B Q4_K_M (1.28 GB), rather than Gemma's 16.95 GB file.
The Qwen profiles use 4,096 tokens of context and a 768-token answer limit; the MiniCPM agent uses 3,072 and 512 respectively.
Prompt batches are 128/64 tokens. These compact models' answers are less
capable than the default large model. Check the linked library sources.

## Install and start

Extract the complete source archive to a folder you will keep. Open a terminal in that folder and run:

```bash
bash install-low-vram.sh
bash run-low-vram.sh
```

The installer supports apt-based Linux (including Ubuntu 24.04 and Debian 13) and Fedora with dnf. It asks sudo to install
system dependencies, creates its own Python environment, compiles the pipe-only AI runner and the video plugin. Build jobs
default to two to keep memory use down. A C++ build can take several minutes. It never replaces the system Python.
On another distribution, install equivalent dependencies and use `bash install-low-vram.sh --skip-system-deps`.
Python 3.11+, GTK 3, WebKitGTK **4.1** with Soup 3 and PyGObject are required. Fedora's optional H.264/AAC codecs may
require the distribution's additional multimedia packages; this installer does not add third-party repositories.

CPU mode is the default: it uses **no GPU memory for AI**, so an older card without Vulkan still works.
Use at least **6 GB system RAM; 8 GB or more is recommended**, plus disk space for the chosen library. A 2 GB graphics card
does not mean a computer with 2 GB total RAM. Setup estimates memory conservatively, then measures the downloaded model.
Close other applications if system RAM is tight. Library storage can be much larger than the model; check setup's sizes.
The included catalog lists Small at about **7.7 GB to download and 7.7 GB installed**; setup also keeps free space for
unpacking and the system. Allow roughly **25 GB free disk space** to start with Small, the model and build files.

In the first-run window select the **Small** library and **Qwen3.5 local AI**. A US state map is optional; skip it if it is
not useful where you live. The initial downloads require internet. After setup the desktop app, its library and its AI work
offline, without an AI server or API key. Setup downloads the model at a pinned revision and verifies its size and SHA-256.
An existing LastbookData folder can be selected instead of downloading another library.

## Optional GPU acceleration

If your card has a working Vulkan driver, build this way instead:

```bash
bash install-low-vram.sh --gpu
bash run-low-vram.sh
```

Install your distribution's GPU driver first and confirm `vulkaninfo --summary` lists the real GPU. Some old 2 GB NVIDIA
cards do not support a usable Vulkan compute backend. The build does not install or replace GPU drivers.
llama.cpp fits layers to available VRAM and leaves a 512 MiB margin for Qwen profiles or 256 MiB for the MiniCPM profile;
remaining layers use system RAM. The larger MiniCPM file may need some CPU offload on a card driving the desktop.
This is a fit policy, not a guarantee that a particular driver/GPU can allocate successfully. If AI fails to load, use:

```bash
bash run-low-vram.sh --cpu
```

This fallback works with a Vulkan build too. To make CPU mode the saved default, run `bash install-low-vram.sh --cpu`.

## Model choices

| Launch option | Model file | Download | Use |
| --- | --- | --- | --- |
| default or `--model qwen3.5-2b` | Qwen3.5 2B Q4_K_M | 1.28 GB | Default for library answers |
| `--model qwen3.5-0.8b` | Qwen3.5 0.8B Q8_0 | 0.83 GB | Extra VRAM headroom and a smaller download |
| `--model qwen2.5-1.5b` | Qwen2.5 1.5B Instruct Q4_K_M | 1.12 GB | Older architecture for compatibility |
| `--abliterated` or `--model qwen3.5-2b-abliterated` | Huihui Qwen3.5 2B Abliterated Q4_K_M | 1.27 GB | Abliterated model option |
| `--agent` or `--model minicpm5-2b` | MiniCPM5 2B Q4_K_M | 1.56 GB | Recommended compact agent candidate |

```bash
bash run-low-vram.sh --model qwen3.5-0.8b
bash run-low-vram.sh --abliterated
bash run-low-vram.sh --agent
```

Run one model at a time. Quit Lastbook before switching profiles; the desktop app otherwise focuses the already running
instance. Only the selected model is offered for download. Use the corresponding launch option every time, then
add that model in **Library and local AI**. Existing model files are kept. Qwen3.5 and MiniCPM thinking are disabled to conserve time and
context. These are text-only configurations: no vision projector is loaded, even though Qwen3.5 can support images elsewhere.
All five use Apache License 2.0. Quantized weights are separately downloaded from their publishers, with pinned checksums.
No small model is universally best, and this build does not claim benchmark superiority.

### Why MiniCPM5 for the agent option

OpenBMB's current [MiniCPM5-2B model card](https://huggingface.co/openbmb/MiniCPM5-2B) reports BFCL v4 at 66.6, versus
43.6 for Qwen3.5-2B in the same published comparison, and reports stronger agent results across several tasks. This makes
it the strongest general agent candidate among the compact models checked for this update. Those are publisher-reported
results, not a result measured with this Q4 quantization, shorter context or thinking disabled. We test Lastbook's actual
tool protocol separately. Its official model name is 2B; it has about 2.52 billion total parameters including embeddings.

The agent uses MiniCPM's native `<function name="..."><param name="...">...</param></function>` format, including CDATA,
tool-result continuation and hidden call markup while streaming. The abliterated model uses Qwen3.5's own tool format.
Lastbook's tools remain its bounded, read-only library operations; these profiles add no terminal or operating-system tools.
A 2 GB card can use CPU offload as needed. File size alone does not prove a complete model/context/desktop fits in VRAM.

## Existing libraries and model files

```bash
bash run-low-vram.sh --data /path/to/LastbookData
```

If the library is already installed, use **Library and local AI** to add the small AI, or put the pinned file under
`LastbookData/models/` and press **Look again** in the local AI controls. The default file is:

* Repository: `unsloth/Qwen3.5-2B-GGUF`
* Revision: `f6d5376be1edb4d416d56da11e5397a961aca8ae`
* File: `Qwen3.5-2B-Q4_K_M.gguf`
* Bytes: `1280835840`
* SHA-256: `aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223`

Pins for the other choices are in `llm_local.py`.

Always start this version through `run-low-vram.sh` to select the matching model and prompt format. Running `app/main.py`
directly keeps the upstream standard profile unless `LASTBOOK_AI_PROFILE=low-vram` is set before Python starts.
The idle timeout is five minutes; the model then unloads and starts again on the next question.
Logs live in the selected data folder under `derived/logs/lastbook.log` and `local-ai.log`.

## Validation limits

See `VALIDATION-LOW-VRAM.md` for the checks performed on this source snapshot. Native desktop playback, Fedora dependency
installation and real 2 GB GPU performance must be checked on the target machine. This is a source installer, not a signed
binary release. No models or third-party library packs are included in the archive.
