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

Short version: **one tinygrad compute workload has executed end-to-end on this hardware path for the first time.** Specifically:

```
Tensor.arange(16, device='AMD').realize().tolist()
# returns [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
```

That is the entire claim. A single 16-element `arange` kernel dispatches through the AM PM4 path, executes on MEC1, writes back, and the host copyout returns the correct values. This is meaningful because every prior run on this hardware path either hung on compute queue activation or hung on compute queue fetch.

It is **not** a claim that:
- other workloads work (none have been tested)
- multiple dispatches per session work (untested)
- larger tensors or more complex ops work (untested)
- training or graph execution work (untested)
- the fork is stable or safe for general use (it is not)
- the driver is performant (untested, likely far from it)
- the fini/shutdown path is clean (it still raises `SMU msg 0x1f timeout` at atexit — pre-existing, unrelated to compute)

The only verified fact is: one specific minimal compute kernel ran correctly in one run, once.

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

2. **SDMA ring submission works.** The Linux-shaped SDMA ring smoke test retires correctly.

3. **A single `Tensor.arange(16, device='AMD').realize()` kernel executes and returns the correct values.** End-to-end: KIQ comes up, KCQ gets mapped via SET_RESOURCES + MAP_QUEUES through the KIQ, the host publishes PM4, MEC1 fetches, the kernel runs, the result is written back, and the host copyout returns `[0..15]`. This is the only compute workload that has been verified to work on this path.

## What Is Not Yet Verified

These things have **not** been tested on this fork yet, and should not be assumed to work:

- Multiple dispatches in a single session (only a single-dispatch run has been verified)
- Tensors other than a 16-element `arange` (no other shapes tested)
- Non-trivial ops (`matmul`, `conv`, `softmax`, `sum`, etc.) — untested
- `TinyJit`, graph execution, or any training-shaped workload — untested
- Stability under repeated runs — every prior hardware run on this path has required a power cycle after wedging, and we do not yet know whether the post-v11 path is wedge-free
- Multi-tensor memory allocations beyond the small buffers the arange test touches
- Any performance claim at all

## What Is Not Working

- **Device `fini`/shutdown path.** `SMU msg 0x1f timeout` fires at `atexit` finalize. This is the pre-existing `PPSMC_MSG_GetDpmFreqByIndex` issue (separate tracking item) and happens *after* the compute result has already returned to the user, but it is still a real bug and noise in the log.

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
