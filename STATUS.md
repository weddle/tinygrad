# RDNA2 Bring-Up Status

This file is the current project readout for this fork.

It is written for people landing here cold and trying to answer:
- what this fork is for
- what state the RDNA2 bring-up is in
- what is working right now
- what is still broken
- what has already been tried
- what the next investigation steps are

This fork is still **reference-only / use at your own risk**.

## Scope

This fork exists to bring up tinygrad's AM runtime on **RDNA2 on macOS**, specifically:
- **GPU:** Radeon RX 6900 XT
- **ASIC:** Navi21 / Sienna Cichlid / `gfx1030`
- **Host:** Mac mini M4
- **Transport path:** mac mini M4 -> USB4/TB4 -> ASM2464PD-based Ugreen NVMe enclosure -> M.2 to OCuLink adapter -> OCuLink eGPU setup -> RX 6900 XT

This is not a general AMD-on-mac support branch. It is an active bring-up line for one hardware path.

## Methodology

The working method on this fork is deliberate and source-driven:
- use upstream Linux `amdgpu` as the ground truth
- read the Linux source line by line
- identify one concrete semantic difference at a time
- port that one behavior into tinygrad's AM runtime
- run the narrowest hardware test that exercises that difference
- restart or power-cycle between meaningful probes when hardware state becomes suspect

In practice this work has been driven by iterative CLI-agent review and implementation passes, with Linux `amdgpu` behavior used as the arbiter rather than guesswork.

## Workflow Note

For the documented macOS TinyGPU PCI path, routine test runs do **not** need `sudo`.

The correct workflow is:

```bash
AMD_IFACE=PCI DEV=AMD:LLVM AMD_KIQ_BOOTSTRAP=1 .venv/bin/python3 -c "..."
```

The repeated `sudo` pattern that appeared during bring-up was a false requirement caused by:
- older USB BOT helper scripts that really did need root
- root-owned TinyGPU socket/lock files created by earlier sudo runs
- root-owned firmware cache blobs under `~/Library/Caches/tinygrad`

If the environment was previously poisoned by sudo runs, the one-time cleanup is:

```bash
sudo pkill -f "TinyGPU.app.*server"
sudo rm -f /tmp/am_usb4.lock /tmp/tinygpu.sock
sudo chown -R "$USER:staff" ~/Library/Caches/tinygrad
```

After that cleanup, compute runs on this fork work as the normal user.

## Current Status

Short version: **the backend now runs real tinygrad compute and has reached the first bounded real-model checkpoint.** Specifically:

```
Tensor.arange(16, device='AMD').realize().tolist()
# returns [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
```

and:

```bash
PYTHONPATH=. AMD_IFACE=PCI DEV=AMD:LLVM AMD_KIQ_BOOTSTRAP=1 \
/Users/ryan/Projects/Lab/asm2464pd-firmware/.venv/bin/python3 \
examples/gpt2.py --model_size gpt2 --count 1 --temperature 0 --prompt 'Hello'
```

completed and generated:

```text
Hello,
```

That is the entire claim. A single 16-element `arange` kernel dispatches through the AM PM4 path, executes on MEC1, writes back, and the host copyout returns the correct values. This is meaningful because every prior run on this hardware path either hung on compute queue activation or hung on compute queue fetch.

It is **not** a claim that:
- general LLM support is complete
- larger GPT-style generation loops are stable
- training works
- all examples work unmodified
- the fork is stable or safe for general use
- performance is tuned

But it is now stronger than a one-kernel-once claim. The verified envelope includes:
- repeated eager compute
- transformer primitives
- TinyJit replay
- tiny transformer forward
- first bounded GPT-2 generation

### 2026-04-08 — what actually landed

Two separate bugs were fixed, in order:

1. **KIQ bring-up parity (v7).** `_setup_kiq()` was rewritten as a literal port of Linux `gfx_v10_0_kiq_init_register`, adding a real EOP buffer, correcting multiple mis-encoded MQD fields, and replacing a bulk 58-register write loop with an explicit Linux-shaped write sequence. This eliminated the deterministic UTCL2 instruction-fetch faults at VA `0x10000`, `0x0`, and `0x48000000000` that had been present in every prior hardware run. It did **not** by itself make compute work.

2. **KCQ doorbell byte-offset (v11).** A one-line fix in `ip.py::setup_ring`: `return doorbell >> 1`. The local `doorbell` variable was being computed as `(AMDGPU_NAVI10_DOORBELL_MEC_RING0 + idx) << 1 = 6`, a literal port of Linux's `gfx_v10_0.c:4696`. Linux uses that same value both (a) as `DOORBELL_OFFSET` in the MQD, which MEC hardware interprets as a dword index, and (b) as the argument to `WDOORBELL64`, which calls `atomic64_set((atomic64_t *)(adev->doorbell.cpu_addr + index), v)` where `cpu_addr` is `uint32_t *` — so `cpu_addr + 6` advances by `6 * sizeof(u32) = 24 bytes`. Tinygrad's host doorbell write uses `doorbell64.view(doorbell_index * 8, ...)`, treating each slot as 8 bytes wide, so for the same pre-shifted value of 6 it was writing at byte offset 48. MEC1 was listening at byte 24; tinygrad was writing at byte 48. MEC1 literally never saw a doorbell ring. Returning `doorbell >> 1` from `setup_ring` fixes the host-side offset without changing the MQD field (which stays at 6 to match Linux/MEC hardware).

The other paths (KIQ, SDMA) coincidentally wrote to the correct byte offsets for unrelated reasons — KIQ because `idx=0` makes every byte-offset arithmetic collapse to `0`, and SDMA because its own `doorbell` variable is not pre-shifted so tinygrad's `* 8` math happens to match Linux's pre-shift-then-`* 4` math. KCQ was the only path that combined Linux's `<< 1` pre-shift *and* tinygrad's `* 8` view math, double-counting the factor of 2.

**Why it took a long time to find.** Fixing the v7 KIQ bug exposed the v11 doorbell bug. Before v7 the symptom was "UTCL2 faults on KIQ bring-up"; after v7 the symptom was "KCQ ACTIVE=1 but `read_ptr=0` and `MEC1 HEADER_DUMP` stuck in the idle-loop pattern." Every hypothesis tested against the post-v7 symptom (cross-MEC scheduling, RLC refresh, system-memory coherence, direct-HQD, PCIe store-drain) was reasoning about the wrong side of the wall. The doorbell ring either reaches MEC1 or it does not, and that distinction was invisible until a source-level audit of `WDOORBELL64` against `doorbell64.view(index * 8, ...)` made the factor-of-2 mismatch explicit.

## What Is Implemented

This fork includes a RDNA2-specific AM-runtime patch line:
- RDNA2 firmware filename/codename handling
- PSP v1.3 firmware header parsing
- RDNA2 register-loading compatibility work
- RDNA2 SMU / GFX / SDMA init-path compatibility fixes
- gfx10 `AUTOLOAD_RLC` enablement
- explicit RDNA2 CP firmware descriptor handling
- Linux-shaped `amdgpu_gfx_enable_kcq()`-style control-path batching
- KCQ/KIQ doorbell metadata parity fixes
- Linux-faithful `_setup_kiq()` port of `gfx_v10_0_kiq_init_register` (EOP buffer, MQD field corrections, explicit Linux-shaped register write sequence, `me=2/pipe=0/queue=0` placement)
- KCQ doorbell host-side byte-offset fix (the one-liner that made the first kernel execute)
- bring-up diagnostics (HEADER_DUMP delta, KCQ MQD/page-table walker, IH fault decoder)
- debug env knobs: `AMD_KIQ_BOOTSTRAP=1`, `AMD_KCQ_DIRECT_HQD=1`, `AMD_DOORBELL_DRAIN=1` (for A/B testing; not required for the successful path)

The active patch surface for this fork is listed in the README.

## What Is Working

These are the *verified* positive facts. Anything not on this list is untested.

1. **`AMDDevice()` initialization works on this hardware path.** PSP, SMU, GFX, and SDMA all come up and the subsequent runtime test can submit to both SDMA and the compute queue.

2. **SDMA ring submission works.** The Linux-shaped SDMA ring smoke test retires correctly, and every compute readback (`copyout`) rides the SDMA path.

3. **Compute dispatch works on the KCQ via KIQ-mediated MAP_QUEUES.** End-to-end: KIQ comes up (Linux-faithful `_setup_kiq` on `me=2/pipe=0/queue=0`), KCQ gets mapped via batched SET_RESOURCES + MAP_QUEUES through the KIQ, the host publishes PM4, MEC1 fetches, the kernel runs, the result is written back via SDMA, and the host copyout returns correct values.

4. **LLM ladder Phase 1 (compute stability) passes.** The ladder script at `scripts/diag/llm_ladder_phase1_compute.py` (in the companion `tiny-egpu` repo) runs unprivileged end-to-end and passes every step:
   - `Tensor.arange(16).realize()` — single dispatch
   - `arange(16)` twice in one process — multi-dispatch in one session
   - `ones(16) + ones(16)` — elementwise add
   - `(arange(16) * 2) + 3` — chained elementwise
   - `ones(4,4) @ ones(4,4)` — small matmul
   - `arange(256).sum() == 32640` — reduction + larger shape
   - Matmul sweep `16x16`, `64x64`, `128x128` — all return correct values, all dispatch-latency-bound at ~25ms (not compute-bound)

5. **LLM ladder Phase 2 (transformer primitive coverage) passes.** `scripts/diag/llm_ladder_phase2_primitives.py` verifies every primitive used by transformer inference against a CPU-computed reference:
   - `softmax` 1D + 2D with `axis=-1`
   - `gelu` (tanh approximation, matching tinygrad's implementation)
   - `silu` (= `x * sigmoid(x)`)
   - `layernorm` unweighted, with all-zero and all-same-value edge cases
   - `nn.Embedding(8,4)` lookup with manually-seeded weights
   - Reductions: full `sum()`, `sum(axis=0)`, multi-axis `sum(axis=(0,1))`, `sum(axis=-1, keepdim=True)`, `mean(axis=1)`, `max(axis=2)`
   - Shape ops: `reshape`, `permute(2,0,1)`, composed `permute().transpose(0,1)`
   - Attention-shape dress rehearsal: `(Q @ K.transpose(-1,-2) / sqrt(d_k)).softmax(axis=-1) @ V` at `(B=1, H=4, S=8, D=16)`, compiled as a fused kernel, output finite

   17 checks total, all pass. First time an attention-shape kernel has been compiled and dispatched on this path; first on-device `Tensor.randn` and `manual_seed`; first multi-axis reductions.

6. **LLM ladder Phase 3 (TinyJit) passes.** `scripts/diag/llm_ladder_phase3_tinyjit.py` exercises tinygrad's `TinyJit` capture-and-replay state machine across six rungs:
   - `@TinyJit` elementwise × 5 (eager, capture, 3× replay)
   - `@TinyJit` matmul `16x16 @ 16x16` × 5 (requires `.contiguous()` on `.ones()` inputs — lazy CONSTs are not valid JIT inputs)
   - `@TinyJit` softmax + reduction × 5 (`softmax(axis=-1).sum(axis=-1)` yields `[1.0, 1.0, 1.0]`)
   - `@TinyJit` attention kernel `(B=1, H=4, S=8, D=16)` × 5 with cross-call stability check (`sum=21.160675` stable across replays)
   - Alternating-input cache correctness × 10 (A/B/A/B with different value sets — catches "baked-in captured constants" failure mode)
   - `Context(JIT=0)` pass-through + `.reset()` + `JIT=default` replay

   41 checks total, all pass. The fused attention kernel (matmul + divide + softmax + matmul) captures and replays correctly under TinyJit on this backend — this is the exact kernel `examples/gpt2.py` uses for every attention layer.

7. **LLM ladder Phase 4 (tiny transformer forward) passes.** `scripts/diag/llm_ladder_phase4_transformer.py` defines a `TinyBlock` class from `tinygrad.nn.Linear` and `tinygrad.nn.LayerNorm` (pre-norm block: 2× LayerNorm + 6× Linear for QKV/output/MLP + scaled dot-product attention + GELU MLP + 2 residual connections). Block shape: `d_model=32, n_heads=4, head_dim=8, mlp_hidden=128`, input `(B=1, S=16, D=32)`:
   - Random-weight single-block forward: shape correct, output finite
   - Single-block repeated forward × 10: sum/mean bitwise stable (`tol=1e-6`)
   - Two-layer stack forward: shape correct, output finite
   - Two-layer repeated forward × 10: sum/mean bitwise stable
   - Two-layer JIT-wrapped × 5: output matches eager (`tol=1e-4`)

   52 checks total, all pass. First complete transformer block forward compiled and dispatched on this path; multi-layer stacking produces finite results; cross-call stability is bitwise-identical; full 2-layer forward captures as a single JIT graph and replays correctly.

8. **LLM ladder Phase 5 (in-tree example validation) passes as a bounded claim.** Using the upstream `examples/gpt2.py` as shipped, with no modifications:
   - `examples/gpt2.py --model_size gpt2 --count 1 --temperature 0 --prompt 'Hello'` loads GPT-2 small weights from HuggingFace on the RDNA2 path and produces `"Hello,"`
   - `examples/gpt2.py --model_size gpt2 --count 3 --temperature 0 --prompt 'Hello'` produces `"Hello, I'm"`
   - The AMD backend correctly executes every kernel the example dispatches.

   **Scope limitation: this is a bounded claim for the upstream-intended single-`generate()`-per-process usage pattern.** Multiple `generate()` calls in the same process trigger a latent upstream tinygrad `TinyJit` bug where `Variable.val`-read Python branches in `examples/gpt2.py` bake in as constants at capture time. The bug was isolated via cross-backend discriminator runs: the same failure reproduces bit-exact on METAL (unrelated backend, same Mac mini), and disabling JIT (`JIT=0`) on the AMD backend makes the matrix pattern pass. Neither of those results implicates the AMD backend. Full trace and possible fix candidates at `learnings/upstream-tinyjit-var-val-capture-baking.md` in the companion `tiny-egpu` repo. The upstream bug has been noted but not yet filed.

9. **LLM ladder Phase 6 (first controlled LLM claim) passes.** `scripts/diag/llm_ladder_phase6_gpt2_inference.py` runs a formal single-generate test with exact-equality assertion and post-generation backend health smoke tests:
   - `GPT2.build("gpt2")` loads GPT-2 small weights (~500 MB) on the RDNA2 path
   - `gpt2.generate("Hello", count=3, temperature=0.0, batch_size=1)[0]` returns **exactly** `"Hello, I'm"` (exact string equality, not prefix match)
   - Three post-generation smoke tests all pass: `Tensor.arange(16).realize().tolist() == [0..15]`, `4×4 ones @ 4×4 ones == [[4.0]*4]*4`, `Tensor.arange(256).sum().item() == 32640`

   **The first controlled LLM claim on this hardware path is:**

   > `gpt2.generate("Hello", count=3, temperature=0.0)` on the RDNA2 path via tinygrad AM over USB4/TB4 → ASM2464PD → OCuLink → PCIe on macOS returns **exactly** `"Hello, I'm"`, and the backend survives the workload — a subsequent `Tensor.arange(16)`, 4×4 matmul, and `arange(256).sum()` all produce correct values in the same process.

   The post-generation health checks are the critical new signal: after loading ~500 MB of model weights and running the full 12-layer GPT-2 small forward through JIT capture + replay for 3 iterations (softmax + layernorm + 48 matmuls + KV cache writes), the simple kernel dispatch path still works correctly against the same expected values Phase 1 verified on a fresh device. The backend is not left in a degraded state by the real-model workload.

   Scoped to single-generate-per-process per the Phase 5 upstream bug finding.

10. **Llama 3.2 1B Instruct GGUF runs on RDNA2 (post-ladder extension).** `python3 examples/llama3.py --size 1B --benchmark --temperature 0 --seed 42` on the upstream example as shipped, no modifications:
    - Fetches `bartowski/Llama-3.2-1B-Instruct-GGUF` Q6_K quantization (~1 GB) + tokenizer from `bofenghuang/Meta-Llama-3-8B`
    - Loads 4.94 GB of dequantized weights in 1.72s (**2.88 GB/s**) — ~10× faster than the GPT-2 small `torch_load` path
    - Runs a deterministic 20-token generation on the hardcoded `"Hello."` prompt at **3.5–3.7 tok/s**
    - Sustained memory bandwidth: **17–22 GB/s** global, ~21 GB/s param-bandwidth
    - Output: `"Hello! How can I assist you today?assistant\n\nI can provide information and entertainment"` (benchmark runs 20 iters regardless of stop tokens)

    **New primitives and code paths exercised (not covered by Phases 1–6):**
    - RMSNorm (vs LayerNorm)
    - SwiGLU MLP gating: `w2(silu(w1(x)) * w3(x))`
    - Rotary Position Embeddings (RoPE) via `freqs_cis` parameter
    - Grouped-query attention (`n_heads=32, n_kv_heads=8`, asymmetric K/V head count)
    - Q6_K quantized weight load + inline dequantization under JIT
    - Llama 3 chat-template tokenization with BOS + `<|start_header_id|>` / `<|end_header_id|>` / `<|eot_id|>` special tokens via sentencepiece
    - `prefill` path (consume prompt in one shot, then single-token generation)
    - 16-layer transformer forward (vs 12 for GPT-2 small)

    **This is the first run on this path that is compute/weight-bandwidth bound rather than dispatch-latency bound.** GPT-2 small was dispatch-bound at ~25 ms per op regardless of size; Llama 3.2 1B at ~280 ms per token reads ~5 GB of weights each pass at realistic bandwidth rates. The tunnel (USB4/TB4 → ASM2464PD → OCuLink → PCIe) is moving data at its link speed for the first time.

    Not impacted by the Phase 5 upstream TinyJit bug — `--benchmark` is a single continuous generation loop within one script invocation, not a multi-call `generate()` pattern.

5. **Multi-process re-entry works without a cable replug.** A new Python process immediately after a clean exit of a previous one takes the `partial_boot` path: `AM_GFX.init_hw` calls `reset_mec()` and then re-runs `_setup_kiq()` (with the Step 0 dequeue-if-active check matching Linux `gfx_v10_0.c:7036-7046`) to rebuild per-process KIQ state. Verified for at least 5 consecutive processes in a row across three kernel shapes (arange, matmul, elementwise).

6. **Clean process exit.** `amdev.py::fini()` wraps the SMU `set_clocks(level=0)` call in a `try/except TimeoutError` matching the pre-existing init-path pattern. Process exit is clean; no noisy traceback; GPU stays enumerated to macOS IOKit so the next process can open it.

7. **Unprivileged TinyGPU PCI workflow.** `AMD_IFACE=PCI DEV=AMD:LLVM AMD_KIQ_BOOTSTRAP=1 .venv/bin/python3 ...` runs as a normal user — no `sudo` — through the TinyGPU.app + dext flow. See the Workflow Note section above for the one-time cleanup if stale root-owned state from an earlier `sudo` run needs clearing.

## What Is Not Yet Verified

These things have **not** been tested on this fork yet, and should not be assumed to work:

- **Larger GPT-2 variants** (`gpt2-medium`, `gpt2-large`, `gpt2-xl`) — only `gpt2` small (124M params) has been run on this path
- **Longer generations** — the verified generation length is `count=3`; longer sequences have not been tested and would eventually hit the `MAX_CONTEXT` KV cache limit
- **Prompts other than `"Hello"`** for multi-token generation (expected values not verified against a reference implementation)
- **Llama, Llama 3, other transformer families** — not tested; out of scope for the current ladder
- **Multiple `generate()` calls in the same process on `examples/gpt2.py`** — blocked by a known upstream tinygrad `TinyJit` bug (see item 8 in "What Is Working" and `learnings/upstream-tinyjit-var-val-capture-baking.md`). Cross-backend and JIT-off discriminators confirm this is not an AMD backend issue.
- **`examples/transformer.py` training smoke test** — it's a real training script, not a short inference check; not wrapped for this ladder.
- **`examples/transformer.py` training smoke test** — it's a real training script, not a short inference check; not yet wrapped for this ladder.
- **Prompts other than `"Hello"` for multi-token generation** — expected values not verified against a reference implementation.
- Matmul sizes beyond `128x128` — dispatch cost dominates at the sizes tested, so no compute-throughput claim has been measured.
- Convolution (`conv2d`, etc.) — not exercised.
- Training-shaped workloads (autograd, optimizer step, `backward()`) — not exercised.
- Larger tensor allocations and multi-tensor memory pressure beyond what Phases 1-5 touch.
- Any performance claim at all (no benchmarking).
- `mode1_reset` on Sienna Cichlid — still known-broken on this ASIC, but no longer a practical blocker because `partial_boot` re-entry handles warm re-open.

## What Is Not Working

- **Larger compute workloads are untested.** Not a known-broken case — just unverified. Moving up the LLM ladder will surface any size-scaling or op-coverage bugs as they appear.

## What We Have Tried And Mostly Ruled Out

These branches have been pushed down or closed by direct testing:

### SDMA-local initialization differences

This branch is effectively closed.

Why:
- SDMA now works
- the Linux-shaped SDMA ring test passes
- the remaining blocker is downstream on the compute path

### Missing gfx10 `AUTOLOAD_RLC`

Closed as a solved issue.

Why:
- it was the missing step for the SDMA milestone
- widening it for RDNA2/gfx10 materially moved the whole bring-up forward

### "MEC firmware is simply not loaded"

Demoted / reframed.

Why:
- older header-dump heuristics turned out to be too strong a stop gate
- current evidence says those values are not a clean binary "firmware absent" signal on this path

### Cleaner shader zero stub

Closed as an active fix candidate.

Why:
- replacing the zero stub with Linux's real `gfx_10_3_0_cleaner_shader_hex` did not change the failure

### KCQ MQD control-bit parity alone

Closed as the main lead.

Why:
- trying Linux-parity `KMD_QUEUE`, `PRIV_STATE`, and `RPTR_BLOCK_SIZE` values did not unblock fetch
- some HQD-visible state is also clearly managed/rewritten by MEC, so direct HQD readback is not a faithful mirror of the MQD input

### KCQ doorbell-offset MQD field normalization (earlier fix, separate from v11)

Closed.

Why:
- There was a real MQD-field doorbell parity bug earlier in the arc
- Fixing it was correct
- It did not by itself make compute work
- The v11 fix is a *different* doorbell bug — in the host-side byte offset when writing to the doorbell BAR, not in the MQD field — see the "2026-04-08 — what actually landed" section above

### `CP_PQ_WPTR_POLL_CNTL1` as the primary missing ingredient

Deprioritized.

Why:
- the write can land
- it does not cause fetch to start
- Linux's normal amdgpu KCQ path does not appear to rely on it the way KFD/direct-HQD paths do

### "The compute ring must be VRAM-backed"

Deprioritized / likely wrong.

Why:
- Linux gfx10 compute rings are normally GTT/system-backed
- the KCQ page-table walk already showed valid mappings

### Simple page-table invalidity

Closed as the main explanation.

Why:
- the KCQ ring, `wptr_poll`, `rptr_report`, and `eop_base` all walk validly

### `_setup_kiq()` silently deviating from Linux `gfx_v10_0_kiq_init_register`

Closed as of 2026-04-08 v7.

Why:
- A line-by-line audit against Linux `gfx_v10_0_kiq_init_register` identified a concrete category of bugs: missing EOP buffer, multiple mis-encoded MQD fields, and an out-of-order bulk register write loop
- A literal Linux-faithful rewrite eliminated every deterministic UTCL2 instruction-fetch fault that earlier runs had hit (at VA 0x10000, 0x0, and 0x48000000000)
- GCVM fault status is now `0` post-run
- The fix stays

### `memory_barrier()` being a no-op on Darwin ARM64

Closed.

Why:
- An empirical ARM64 disassembly trace of `atomic_thread_fence` on the live Darwin libSystem confirmed the dispatch path reaches `DMB SY`
- Refuted and not worth reinvestigating

### IH faults being orphaned / secondary

Closed.

Why:
- The IH ring fault decoder (`SOC15_IH_CLIENTID_UTCL2 = 0x1b`) confirmed the faults were first-order
- Post-v11 the IH ring is clean

### Cross-MEC scheduling gap / RLC refresh / `CP_CPC_IC_BASE` refresh

All closed.

Why:
- Every one of these hypotheses was chasing the post-v7 "KCQ ACTIVE=1, `read_ptr=0`" symptom, which turned out to be caused by the KCQ doorbell host-side byte-offset bug (v11 fix), not by any of these downstream mechanisms
- A source-level cross-MEC audit (`docs/rdna2-cross-mec-audit-2026-04-08.md` in the companion tiny-egpu repo) established that KIQ-on-MEC2 + KCQ-on-MEC1 is the Linux default for Sienna Cichlid, not a split configuration, and that Linux has no RLC refresh between SET_RESOURCES and MAP_QUEUES
- A hardware run with `AMD_DOORBELL_DRAIN=1` (forcing a PCIe store-drain before the doorbell MMIO write) did not change the symptom, refuting the "host write-combining holds ring contents" version of the coherence hypothesis
- The actual bug was a factor-of-2 mismatch in the host-side doorbell write byte offset. The doorbell ring was going to the wrong address. MEC1 was never notified of host publish, regardless of coherence, scheduling, or IC base state

## What's Next

No explicit "open investigation" list at this point — the immediate next work is just to exercise the path and see what breaks. In rough order of interest:

1. Rerun `arange(16)` multiple times in the same process to see whether a second dispatch works.
2. Try other small shapes (`Tensor.zeros`, `Tensor.ones`, `arange` of different sizes, a trivial `a + b`).
3. Try a `matmul` of some size to exercise a non-trivial kernel.
4. Fix the `SMU msg 0x1f timeout` in the fini path so that clean shutdown works.
5. Stability testing under repeated runs without power cycles.
6. Eventually, split the fork's bring-up changes into focused upstream PRs.

Each of these may surface new bugs. The claim of this status doc is limited to the one run that has been observed.

## Notes On Public Prior Work

This effort has overlapped with public RDNA2-on-mac bring-up work from **@m0dm0d on X**.

Their public posts were useful, especially around:
- the general RDNA2 PSP/boot chain on Apple Silicon
- the `C2PMSG_39` bootloader post-code breadcrumb
- confirming parallel investigation on very similar hardware

That work helped sharpen early PSP-side reasoning even though this fork has since moved well beyond the original PSP/SOS boot blocker.

## Practical Caveat

This branch remains a bring-up fork.

That means:
- hardware wedges are still possible
- power cycles are still part of the workflow
- some commits are investigation-grade rather than polished
- no one should assume this branch is safe for general use

If you want stable tinygrad, use upstream.

If you want the preserved state of the RDNA2-on-mac bring-up effort on an RX 6900 XT, this fork is that reference line.
