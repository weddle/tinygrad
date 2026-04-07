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
- **compute queue activation is partially working**
- **actual compute queue PM4 fetch is still blocked**

That means the project is no longer stuck in broad firmware/init territory. The current blocker is much narrower than that.

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

## What Is Not Working

The current blocker is:

**The compute queue still does not fetch host-submitted PM4 reliably enough to execute a minimal direct scratch-register test or a tinygrad compute primitive.**

Current observed failure shape:
- KCQ shows `ACTIVE=1`
- host-side queue publish advances
- host-side queue `write_ptr` advances
- KCQ-side `WPTR_LO` does not latch in the direct KCQ test
- KCQ-side `RPTR` stays `0`
- direct KCQ scratch-register PM4 test does not execute
- `Tensor.arange(16, device='AMD').realize()` still hangs waiting for completion

So the unresolved question is no longer "can we activate a queue?" It is:

**why does an activated KCQ still not become fetch-ready?**

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

## Current Read Of The Situation

The current state is:
- queue creation works
- queue activation gets far enough to show `ACTIVE=1`
- the MQD in memory looks right
- the relevant virtual addresses are mapped
- but the queue still does not actually fetch PM4 in the direct KCQ test

That leaves a narrower and more interesting class of possibilities:
- `MAP_QUEUES` activation is not equivalent to a fetch-ready queue on this path
- direct-HQD queue activation may behave differently than the MEC-mediated `MAP_QUEUES` path
- there is still a missing queue-load / activation semantic detail between "ACTIVE=1" and "fetch-ready"
- some of the live HQD state visible via `GRBM_SELECT` may not faithfully represent what MEC is actually using

## Current Open Areas For Investigation

These are the leading active branches:

### 1. `MAP_QUEUES` activation vs direct-HQD activation

This is the highest-value next discriminator.

Question:
- does the same minimal KCQ PM4 scratch test behave differently if the queue is materialized by direct HQD activation instead of the normal `MAP_QUEUES` path?

Interpretation:
- if direct-HQD works and `MAP_QUEUES` does not, the missing piece is in MEC queue-load / activation semantics
- if both fail, the remaining blocker is lower-level queue publish / doorbell semantics below activation

### 2. What does `ACTIVE=1` really mean on gfx10.3 here?

We are explicitly curious about whether:
- `ACTIVE=1` only means "queue is registered"
- or whether it should imply "doorbell-consuming and fetch-ready"

Right now the evidence says those are not the same thing.

### 3. How trustworthy is live HQD register readback under `GRBM_SELECT`?

We have enough evidence to be suspicious of over-reading those registers.

Open question:
- are we seeing true live queue state, a partial shadow, or MEC-managed state that is not a direct replay of the MQD?

### 4. IH / GCVM fault interpretation

There have been GCVM faults during some of the compute-path experiments, but they have not yet cleanly lined up as the single primary explanation for the no-fetch state.

Open question:
- which faults are first-order causes, and which are secondary fallout once the run is already wedged?

## Where Community Feedback Would Be Most Useful

Feedback would be especially useful from anyone who knows gfx10/gfx10.3 compute-queue bring-up in Linux `amdgpu`, KFD, firmware, or low-level queue-management code.

The specific questions we would most like help with are:

1. On gfx10.3, should `MAP_QUEUES` activation be expected to yield a fetch-ready queue immediately, or is there another normal post-map step that matters in practice?
2. How much trust should we place in live HQD readback for a MEC-managed compute queue on this generation?
3. Is there a known semantic gap between the normal `MAP_QUEUES` path and direct-HQD/KFD-style activation that could explain `ACTIVE=1` without PM4 fetch?
4. Are there known pitfalls around the first direct KCQ PM4 test on Navi21 that would not show up as a simple unmapped-VA failure?

## Notes On Public Prior Work

This effort has overlapped with public RDNA2-on-mac bring-up work from **@m0dm0d on X**.

Their public posts were useful, especially around:
- the general RDNA2 PSP/boot chain on Apple Silicon
- the `C2PMSG_39` bootloader post-code breadcrumb
- confirming parallel investigation on very similar hardware

That work helped sharpen early PSP-side reasoning even though this fork has since moved well beyond the original PSP/SOS boot blocker.

## Roadmap

Near-term roadmap:

1. **Power-cycle before the next privileged rerun.**
   Some of the latest KCQ probes left the GPU in a bad state.

2. **Run the minimal direct KCQ scratch-register PM4 test under both activation paths.**
   Compare:
   - normal `MAP_QUEUES`
   - direct-HQD activation fallback

3. **Use only a small comparison surface for that A/B.**
   Compare:
   - KCQ `WPTR_LO`
   - KCQ `RPTR`
   - scratch register result

4. **Branch based on that result.**
   - if direct-HQD works: focus on MEC queue-load / activation semantics
   - if both fail: focus on lower-level compute queue publish / doorbell semantics

5. **Keep already-closed branches closed unless new evidence contradicts them.**
   In particular:
   - do not reopen the cleaner-shader branch
   - do not reopen the "ring must be VRAM-backed" branch
   - do not reopen `CP_PQ_WPTR_POLL_CNTL1` as the main theory

## Practical Caveat

This branch remains a bring-up fork.

That means:
- hardware wedges are still possible
- power cycles are still part of the workflow
- some commits are investigation-grade rather than polished
- no one should assume this branch is safe for general use

If you want stable tinygrad, use upstream.

If you want the preserved state of the RDNA2-on-mac bring-up effort on an RX 6900 XT, this fork is that reference line.
