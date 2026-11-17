# Low-VRAM validation — 17 November 2026

Base source: `Immortal-Protocols/lastbook`, commit `b7300c70129058918af34bacf692916eab654ecc`.
Environment: Debian 13 x86_64, Python 3.13, approximately 10 GB system RAM, CPU execution. No physical GPU was available.

## Completed checks

* Built `lastbook-llm` against the repository's pinned llama.cpp b12032 commit
  `dde879bc4f954b809892fd0d660515f42536394a`, with `VULKAN=OFF CPU_VARIANTS=OFF JOBS=4`.
  Its build-time dependency, network-symbol and path scans passed.
* Downloaded all five actual GGUF files. Each matched its pinned byte count and SHA-256.
* All five loaded, answered a generated library question with a registered source key, passed Lastbook's citation-key
  validation, and emitted a parseable `search_library` call. Qwen profiles used 4,096 tokens; MiniCPM5 used 3,072.
* The new Huihui abliterated and MiniCPM5 profiles also consumed a scripted tool-result turn and answered with the correct
  archive label and a source key, using Lastbook's production instructions and source-block format. Simplified prompts
  initially produced answers with missing or differently formatted citation keys; use the actual app format for this check.
  These are short synthetic smoke checks, not broad answer-quality, agent-benchmark or refusal-behavior evaluations.
* Added native MiniCPM XML rendering/parsing, CDATA and malformed-call tests. Thinking is disabled for the small profiles.
* The required regression suite plus new profile tests completed: **326 tests, OK, 3 skipped**. Tests ran with `TMPDIR`
  on the workspace disk; the environment's `/tmp` filesystem is smaller than setup's required 5 GB free-space reserve.
* Updated the hardware test to accept Linux alongside macOS and Windows.
* `bash -n` passed for the installer, launcher and runner build; `node --check` passed for both modified interface scripts;
  `git diff --check` passed.
* Bundled the upstream public download catalog published on 13 November 2026 after validating its URLs and file checksums'
  format. Payloads are still verified by setup when downloaded. This catalog is metadata, not the library content.

| Model | GGUF bytes | CPU fit estimate, model plus working memory |
| --- | ---: | ---: |
| Qwen3.5 2B Q4_K_M | 1,280,835,840 | 1,418,612,256 |
| Qwen3.5 0.8B Q8_0 | 833,592,096 | 969,795,648 |
| Qwen2.5 1.5B Q4_K_M | 1,117,320,736 | 1,276,149,792 |
| Huihui Qwen3.5 2B Abliterated Q4_K_M | 1,270,809,024 | 1,408,585,440 |
| MiniCPM5 2B Q4_K_M | 1,561,318,368 | 1,729,879,520 |

Fit estimates include mapped weights accounted for by Lastbook. They are not measured peak RSS or GPU allocations, and
exclude Lastbook's separate 2.5 GiB reserve for the OS and other app components. CPU mode allocates no AI VRAM.

## Reproduce a model smoke check

After installation, with the selected pinned GGUF in a local folder:

```bash
.venv-low-vram/bin/python -B tools/smoke_low_vram.py --model-dir /path/to/models
.venv-low-vram/bin/python -B tools/smoke_low_vram.py --model-dir /path/to/models --model qwen3.5-0.8b
.venv-low-vram/bin/python -B tools/smoke_low_vram.py --model-dir /path/to/models --model qwen2.5-1.5b
.venv-low-vram/bin/python -B tools/smoke_low_vram.py --model-dir /path/to/models --model qwen3.5-2b-abliterated
.venv-low-vram/bin/python -B tools/smoke_low_vram.py --model-dir /path/to/models --model minicpm5-2b
```

The script creates and removes a tiny test library and uses the app's actual assistant, runner and source validation. It
does not download a model. Add `--gpu` only with a Vulkan-enabled runner and a working Vulkan driver.

## Remaining target-machine checks

The complete installer was not run here: system WebKitGTK 4.1 and development headers are absent, and no root package
installation was performed. Native window startup, PDF/video display, Fedora package installation, a complete multi-GB
library download, Vulkan compilation and physical 2 GB GPU behavior remain unverified. This archive supplies a source
installer, not a precompiled or signed Linux application. Use CPU mode if your GPU driver cannot run the model.
