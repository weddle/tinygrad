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

## Current Status

Short version:
- **device bring-up is materially working**
- **SDMA is working**
- **compute queue activation is now Linux-faithful and no longer faults**
- **the UTCL2 instruction-fetch fault wall has been broken** (2026-04-08, v7 arange run)
- **actual compute queue PM4 fetch is still blocked, but the wall is narrower**

The 2026-04-08 offline investigation pass decomposed the prior wall into seven independent framing assumptions, and a literal port of Linux's `gfx_v10_0_kiq_init_register` (Investigation 3 fix skeleton) eliminated the deterministic UTCL2 faults that had been present in every prior hardware run.

### 2026-04-08 (v7 arange run) — EOP hypothesis confirmed

Changes landed:
- `_setup_kiq()` rewritten as a literal port of Linux `gfx_v10_0_kiq_init_register`
- KIQ placement moved to `me=2 / pipe=0 / queue=0` (Linux default, matches Sienna Cichlid `RLC_CP_SCHEDULERS = 0x58504840` cold-boot pre-config)
- KIQ EOP buffer allocated (`0x1000` bytes, `eop_size=9`)
- MQD field corrections: `cp_hqd_active=1` set at build time, `cp_mqd_control` uses the correct `vmid` field (was misusing `priv_state`), `compute_static_thread_mgmt_se[0..3]` only (was `range(8)`), `persistent_state.preload_size=0x53` (was `0x55` plus a spurious `preload_req=1`), dropped unknown `cp_hqd_quantum=0x111`
- The 58-register bulk write loop was replaced by an explicit Linux-shaped register write sequence terminated by `regCP_HQD_ACTIVE.write` as the final doorbell-then-active handoff
- The same field corrections were applied to the KCQ MQD build in `setup_ring()`
- The spurious `regCP_PQ_WPTR_POLL_CNTL1.write` in the default `MAP_QUEUES` path was removed

Results on hardware:
- The UTCL2 instruction-fetch faults at VA `0x10000`, `0x0`, and `0x48000000000` that were deterministic across v1-v6 are **all gone**
- `GCVM_L2_PROTECTION_FAULT_STATUS = 0x00000000` (was `0x9b3` / `0x933`)
- KCQ HQD register state now correctly preserves every MQD-encoded flag: `CP_HQD_PQ_BASE = 0x00010000` (was garbage `0x111`), `CP_HQD_PQ_CONTROL = 0xd0008915` (was `0x4000800c`, with `kmd_queue`, `priv_state`, `unord_dispatch`, and `rptr_block_size=9` all preserved for the first time)
- KIQ HQD activates cleanly via the Linux-shaped sequence

Residual wall (narrower):
- `compute_queue: put_value=133 write_ptr[0]=133 read_ptr[0]=0`
- MEC activates the KCQ cleanly and no longer faults, but does not consume host-submitted PM4 packets
- `CP_MEC_ME1_HEADER_DUMP = 0xdef0def0` — MEC1 (where the KCQ lives) has not processed a packet
- `CP_MEC_ME2_HEADER_DUMP = 0x00000000` — MEC2 (where the KIQ now lives) shows a new state, different from prior runs

Interpretation: the entire "VA 0x10000 / 0x0 / EOP" stack of v1-v6 walls was one category of bug (missing EOP buffer + mis-encoded MQD fields + out-of-order register writes), and fixing all three simultaneously eliminated all three symptoms. The new residual is a distinct, narrower problem. It is the first confirmed category-fix of the compute arc.

## What Is Implemented

This fork includes a substantial RDNA2-specific AM-runtime patch line, including:
- RDNA2 firmware filename/codename handling
- PSP v1.3 firmware header parsing
- RDNA2 register-loading compatibility work
- RDNA2 SMU / GFX / SDMA init-path compatibility fixes
- gfx10 `AUTOLOAD_RLC` enablement
- explicit RDNA2 CP firmware descriptor handling
- Linux-shaped `amdgpu_gfx_enable_kcq()`-style control-path batching
- KCQ/KIQ doorbell metadata parity fixes
- **Linux-faithful `_setup_kiq()` port of `gfx_v10_0_kiq_init_register`** (EOP buffer, MQD field corrections, explicit Linux-shaped register write sequence, me=2/pipe=0/queue=0 placement)
- bring-up diagnostics used to narrow the remaining compute-path issue

The active patch surface for this fork is listed in the README.

## What Is Working

These are the strongest current positive facts:

1. **`AMDDevice()` initialization works on this hardware path.**
   PSP, SMU, GFX, and SDMA all come up far enough for real runtime testing.

2. **The SDMA path is genuinely working.**
   The Linux-shaped SDMA ring smoke test now retires correctly.

3. **The crucial gfx10 autoload gap was fixed.**
   Widening `AUTOLOAD_RLC` to include RDNA2/gfx10 was the missing step for the SDMA milestone.

4. **The KIQ/KCQ control path materially advanced.**
   Porting Linux-shaped batched `SET_RESOURCES + MAP_QUEUES` semantics moved the compute branch beyond the old queue-activation wall.

5. **The KCQ can reach `ACTIVE=1`.**
   Queue activation is no longer the main problem.

6. **The KCQ MQD contents in memory are Linux-shaped and look correct.**
   The queue base, side-buffer addresses, doorbell metadata, and core control fields are being written correctly into the MQD in VRAM/system memory.

7. **The KCQ MQD-referenced virtual addresses all walk validly in the GPU page tables.**
   The current evidence does not support a simple "the queue points at unmapped memory" explanation.

8. **The KIQ/KCQ HQD bring-up is now Linux-faithful and no longer faults.** (New as of 2026-04-08 v7.)
   `_setup_kiq()` is now a literal port of `gfx_v10_0_kiq_init_register` with a real EOP buffer, corrected MQD fields, and an explicit Linux-shaped register write sequence. The deterministic UTCL2 instruction-fetch faults at VA `0x10000`, `0x0`, and `0x48000000000` that were present across every v1-v6 run are gone. `GCVM_L2_PROTECTION_FAULT_STATUS` reads `0` post-run. The KCQ HQD register state now correctly preserves every flag encoded into the MQD (`kmd_queue`, `priv_state`, `unord_dispatch`, `rptr_block_size=9`).

## What Is Not Working

The current blocker is:

**The MEC activates the KCQ cleanly and no longer faults, but still does not fetch host-submitted PM4 packets.**

Current observed failure shape (2026-04-08 v7):
- KCQ shows `ACTIVE=1`
- the KCQ HQD register state is Linux-faithful for the first time
- no UTCL2 or GCVM faults during the run
- host-side queue publish advances: `put_value=133 write_ptr[0]=133`
- KCQ-side `read_ptr[0]` stays `0`
- `CP_MEC_ME1_HEADER_DUMP = 0xdef0def0` (MEC1, where the KCQ lives — not executed)
- `CP_MEC_ME2_HEADER_DUMP = 0x00000000` (MEC2, where the KIQ now lives — new state)
- `Tensor.arange(16, device='AMD').realize()` still hangs waiting for completion

So the unresolved question is now much narrower:

**why does MEC1 not pick up the KCQ runlist, given that the queue is correctly mapped, non-faulting, and host-published?**

Leading candidates:
1. **Cross-MEC scheduling gap.** KIQ now lives on MEC2, KCQ on MEC1. MEC2 processes `SET_RESOURCES` and `MAP_QUEUES` from the KIQ ring, but that has to propagate via RLC scheduler routing to MEC1's runlist. A missing RLC scheduler refresh between SET_RESOURCES and MAP_QUEUES would match the symptom.
2. **System-memory ring coherence.** The KCQ ring is GTT/system-backed. MEC reads via GFXHUB -> UTCL2 -> PCIe. A missing host-side flush (DMB SY + PCIe write-combining drain) before the doorbell could leave ring contents invisible to the MEC fetcher even though `write_ptr[0]` is published.
3. **Stale MEC1 instruction-cache base.** MEC1's `CP_CPC_IC_BASE` state may have been inherited from an earlier bring-up attempt and never refreshed, leaving MEC1's scheduler loop unable to advance past its boot-time HEADER_DUMP.

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

### KCQ doorbell-offset normalization as the active blocker

Closed as the active blocker, but the fix stays.

Why:
- there was a real doorbell-index parity bug
- fixing it was correct
- it did not cause the queue to start fetching

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
- A line-by-line audit (Investigation 3 of the 2026-04-08 offline investigation queue) identified a concrete category of bugs: missing EOP buffer, multiple mis-encoded MQD fields, and an out-of-order bulk register write loop
- A literal Linux-faithful rewrite eliminated every deterministic UTCL2 instruction-fetch fault that v1-v6 had hit (at VA 0x10000, 0x0, and 0x48000000000)
- GCVM fault status is now `0` post-run
- The fix stays and will not be reverted

### `memory_barrier()` being a no-op on Darwin ARM64

Closed as a real concern (Investigation 4).

Why:
- An empirical ARM64 disassembly trace of `atomic_thread_fence` on the live Darwin libSystem confirmed the dispatch path reaches `DMB SY`
- This assumption is refuted and no longer needs to be reinvestigated

### IH faults being orphaned / secondary

Closed (Investigation 2).

Why:
- The IH ring fault decoder (`SOC15_IH_CLIENTID_UTCL2 = 0x1b`) confirmed the faults were first-order, not secondary fallout
- The faults identified the real root cause (the KIQ HQD EOP / MQD bug) and that root cause is now fixed
- Post-fix, the IH ring is clean

## Current Read Of The Situation

The current state (as of 2026-04-08 v7) is:
- queue creation works
- queue activation is now Linux-faithful and reaches `ACTIVE=1`
- the MQD in memory is correct and every flag is preserved in the live HQD register state
- the relevant virtual addresses are mapped and walk validly
- the entire UTCL2 / GCVM fault wall is gone
- but MEC1 still does not fetch host-submitted PM4 from the KCQ

That leaves three concrete next-step candidates (see "What Is Not Working"):
- cross-MEC scheduling gap between the KIQ on MEC2 and the KCQ on MEC1
- system-memory ring coherence / host-side flush before the doorbell
- stale MEC1 `CP_CPC_IC_BASE` programming left over from earlier bring-up attempts

## Current Open Areas For Investigation

### 1. Co-locate KIQ and KCQ on MEC2 as a cross-MEC discriminator

Highest-value next discriminator.

Question:
- if the KCQ is placed on MEC2 alongside the KIQ, does the fetch block go away?

Interpretation:
- if co-located KCQ works: the missing piece is cross-MEC scheduler routing (RLC refresh between SET_RESOURCES and MAP_QUEUES)
- if co-located KCQ still fails: the blocker is below MEC scheduling (coherence, doorbell semantics, IC base)

### 2. RLC scheduler refresh between SET_RESOURCES and MAP_QUEUES

Question:
- is there a Linux sequence between `SET_RESOURCES` and `MAP_QUEUES` that forces MEC1 to pick up the new queue, which tinygrad is missing?

### 3. PCIe write-combining / host-side flush before doorbell

Question:
- does an explicit DMB SY + PCIe store-drain (e.g. read-back of a device BAR register) before the doorbell ring cause MEC1 to start fetching?

### 4. MEC1 instruction-cache base refresh

Question:
- is `CP_CPC_IC_BASE` for MEC1 still stale from an earlier partial bring-up, and would re-programming it before KCQ activation change MEC1's `HEADER_DUMP` off `0xdef0def0`?

## Where Community Feedback Would Be Most Useful

Feedback would be especially useful from anyone who knows gfx10/gfx10.3 compute-queue bring-up in Linux `amdgpu`, KFD, firmware, or low-level queue-management code.

The specific questions we would most like help with are:

1. On gfx10.3 Sienna Cichlid, is there a normal sequence between `SET_RESOURCES` on the KIQ and the first `MAP_QUEUES` targeting a KCQ on a different MEC that forces the target MEC to enter its scheduler loop? (Specifically, is RLC scheduler re-routing required, or does MAP_QUEUES alone propagate via `RLC_CP_SCHEDULERS` state?)
2. Is placing the KIQ on MEC2 and the KCQ on MEC1 a supported configuration on Sienna Cichlid, or is the normal Linux assumption that they share a scheduler?
3. What does `CP_MEC_ME1_HEADER_DUMP = 0xdef0def0` actually mean post-autoload on RDNA2? Is it "MEC1 has not entered its scheduler loop since reset" or something weaker?
4. On a GTT/system-backed compute ring over PCIe, are there known host-side flush requirements beyond a plain `DMB SY` before ringing the doorbell?

## Notes On Public Prior Work

This effort has overlapped with public RDNA2-on-mac bring-up work from **@m0dm0d on X**.

Their public posts were useful, especially around:
- the general RDNA2 PSP/boot chain on Apple Silicon
- the `C2PMSG_39` bootloader post-code breadcrumb
- confirming parallel investigation on very similar hardware

That work helped sharpen early PSP-side reasoning even though this fork has since moved well beyond the original PSP/SOS boot blocker.

## Roadmap

Near-term roadmap (post-v7):

1. **Co-locate KIQ and KCQ on MEC2 as a single-variable discriminator** for the cross-MEC scheduling hypothesis.

2. **If co-location unblocks fetch:** port Linux's RLC scheduler refresh path between `SET_RESOURCES` and `MAP_QUEUES` so the split KIQ/KCQ placement can also work.

3. **If co-location does not unblock fetch:** instrument the doorbell path with an explicit PCIe store-drain (BAR read-back) before the doorbell write and re-run. This discriminates coherence from scheduling.

4. **If neither co-location nor host-side flush unblocks fetch:** audit `CP_CPC_IC_BASE` programming for MEC1 vs MEC2 and confirm MEC1 has a freshly programmed instruction-cache base.

5. **Keep already-closed branches closed unless new evidence contradicts them.**
   In particular:
   - do not reopen the cleaner-shader branch
   - do not reopen the "ring must be VRAM-backed" branch
   - do not reopen `CP_PQ_WPTR_POLL_CNTL1` as the main theory
   - do not reopen the `_setup_kiq` field-level parity questions — they are fixed
   - do not reopen MEC2 firmware-load theories without new evidence (Linux itself does not load MEC2 as a separate ucode on gfx10)

## Practical Caveat

This branch remains a bring-up fork.

That means:
- hardware wedges are still possible
- power cycles are still part of the workflow
- some commits are investigation-grade rather than polished
- no one should assume this branch is safe for general use

If you want stable tinygrad, use upstream.

If you want the preserved state of the RDNA2-on-mac bring-up effort on an RX 6900 XT, this fork is that reference line.
