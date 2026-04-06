from __future__ import annotations
import ctypes, collections, dataclasses, functools, hashlib, array
from tinygrad.helpers import mv_address, getenv, DEBUG, fetch, lo32, hi32
from tinygrad.runtime.autogen import pci
from tinygrad.runtime.autogen.am import am
# struct_v10_compute_mqd is hand-coded in v10_extra.py because the clang2py autogen pipeline produces
# empty struct stubs (SIZE=0, no _real_fields_) for v10_structs.h when invoked post-install — the
# install-time generation of am.py works but our regen consistently produces broken output regardless
# of how the autogen case is set up. Rather than chase the toolchain non-determinism, we mirror the
# clang2py format by hand. Attach to the `am` namespace so the existing
# `getattr(am, f"struct_v{ver}_compute_mqd")` lookup in setup_ring works uniformly for RDNA2.
if not hasattr(am, 'struct_v10_compute_mqd'):
  from tinygrad.runtime.autogen.am import v10_extra as _v10
  am.struct_v10_compute_mqd = _v10.struct_v10_compute_mqd
from tinygrad.runtime.support.amd import AMDReg, import_module, import_asic_regs
from tinygrad.runtime.support.memory import TLSFAllocator, MemoryManager, AddrSpace
from tinygrad.runtime.support.system import PCIDevice
from tinygrad.runtime.support.am.ip import AM_IP, AM_SOC, AM_GMC, AM_IH, AM_PSP, AM_SMU, AM_GFX, AM_SDMA

AM_DEBUG = getenv("AM_DEBUG", 0)

@dataclasses.dataclass
class AMRegister(AMDReg):
  adev:AMDev

  def read(self, inst=0): return self.adev.rreg(self.addr[inst])
  def read_bitfields(self, inst=0) -> dict[str, int]: return self.decode(self.read(inst=inst))

  def write(self, _am_val:int=0, inst=0, **kwargs): self.adev.wreg(self.addr[inst], _am_val | self.encode(**kwargs))

  def update(self, inst=0, **kwargs): self.write(self.read(inst=inst) & ~self.fields_mask(*kwargs.keys()), inst=inst, **kwargs)

class AMFirmware:
  # RDNA2 firmware uses codenames instead of version-based filenames
  FW_CODENAMES = {
    (11, 0, 7):  "sienna_cichlid",   # Navi 21 (RX 6800/6800 XT/6900 XT)
    (11, 0, 11): "navy_flounder",     # Navi 22 (RX 6700 XT)
    (11, 0, 12): "dimgrey_cavefish",  # Navi 23 (RX 6600 XT)
    (11, 0, 13): "beige_goby",        # Navi 24 (RX 6500 XT)
  }
  # Maps version-based firmware names to codename-based names for RDNA2
  FW_NAME_MAP = {}  # populated in __init__

  def __init__(self, adev):
    self.adev = adev
    mp0_ver = adev.ip_ver.get(am.MP0_HWIP, (0,0,0))
    codename = self.FW_CODENAMES.get(mp0_ver)

    def fmt_ver(hwip): return '_'.join(map(str, adev.ip_ver[hwip]))

    if codename:
      # Build the name mapping: version-based → codename-based
      self.FW_NAME_MAP = {
        f"psp_{fmt_ver(am.MP0_HWIP)}_sos.bin": f"{codename}_sos.bin",
        f"smu_{fmt_ver(am.MP1_HWIP)}.bin": f"{codename}_smc.bin",
        f"sdma_{fmt_ver(am.SDMA0_HWIP)}.bin": f"{codename}_sdma.bin",
        f"gc_{fmt_ver(am.GC_HWIP)}_mec.bin": f"{codename}_mec.bin",
        f"gc_{fmt_ver(am.GC_HWIP)}_mec2.bin": f"{codename}_mec2.bin",
        f"gc_{fmt_ver(am.GC_HWIP)}_rlc.bin": f"{codename}_rlc.bin",
        f"gc_{fmt_ver(am.GC_HWIP)}_pfp.bin": f"{codename}_pfp.bin",
        f"gc_{fmt_ver(am.GC_HWIP)}_me.bin": f"{codename}_me.bin",
        f"gc_{fmt_ver(am.GC_HWIP)}_ce.bin": f"{codename}_ce.bin",
        f"gc_{fmt_ver(am.GC_HWIP)}_imu.bin": f"{codename}_imu.bin",
      }

    # Load SOS firmware
    self.sos_fw = {}

    blob, sos_hdr = self.load_fw(f"psp_{fmt_ver(am.MP0_HWIP)}_sos.bin", versioned_header='struct_psp_firmware_header')

    if hasattr(sos_hdr, 'psp_fw_bin'):
      # v2_0 format (RDNA3+): array of fw_bin_desc with explicit fw_type
      fw_bin = sos_hdr.psp_fw_bin
      for fw_i in range(sos_hdr.psp_fw_bin_count):
        fw_bin_desc = am.struct_psp_fw_bin_desc.from_address(ctypes.addressof(fw_bin) + fw_i * ctypes.sizeof(am.struct_psp_fw_bin_desc))
        ucode_start_offset = fw_bin_desc.offset_bytes + sos_hdr.header.ucode_array_offset_bytes
        self.sos_fw[fw_bin_desc.fw_type] = blob[ucode_start_offset:ucode_start_offset+fw_bin_desc.size_bytes]
    else:
      # v1_3 legacy format (RDNA2): fixed named fields in nested structs
      base_off = sos_hdr.v1_1.v1_0.header.ucode_array_offset_bytes
      for fw_type, desc in [
        (am.PSP_FW_TYPE_PSP_SOS, sos_hdr.v1_1.v1_0.sos),
        (am.PSP_FW_TYPE_PSP_TOC, sos_hdr.v1_1.toc),
        (am.PSP_FW_TYPE_PSP_KDB, sos_hdr.v1_1.kdb),
        (am.PSP_FW_TYPE_PSP_SPL, sos_hdr.spl),
        (am.PSP_FW_TYPE_PSP_RL, sos_hdr.rl),
      ]:
        if desc is not None and desc.size_bytes > 0:
          self.sos_fw[fw_type] = blob[base_off + desc.offset_bytes:base_off + desc.offset_bytes + desc.size_bytes]
      # SYS_DRV has no dedicated descriptor in v1_x — by Linux kernel convention (psp_init_sos_base_fw),
      # SYS_DRV is the leading region of the ucode array up to where SOS begins, i.e. length == sos.offset_bytes.
      sos_off = sos_hdr.v1_1.v1_0.sos.offset_bytes
      if sos_off > 0:
        self.sos_fw[am.PSP_FW_TYPE_PSP_SYS_DRV] = blob[base_off:base_off + sos_off]

    # Load other fw
    self.ucode_start: dict[str, int] = {}
    self.descs: list[tuple[list[int], memoryview]] = []

    # SMU firmware
    if adev.ip_ver[am.MP1_HWIP] != (13,0,12):
      blob, hdr = self.load_fw(f"smu_{fmt_ver(am.MP1_HWIP)}.bin", versioned_header="struct_smc_firmware_header")
      # The main SMU firmware blob always needs to be loaded via PSP regardless of GC version. Both v1_0 and v2_x
      # SMC headers expose v1_0.header — v2_0/v2_1 nest v1_0 at offset 0. Without this, SMU stays dark on RDNA2
      # (the previous code only set smu_psp_desc when GC>=11.0.0, so SMU was never loaded on Navi 21 etc.).
      v1 = hdr.v1_0 if hasattr(hdr, 'v1_0') else hdr
      self.smu_psp_desc = self.desc(blob, v1.header.ucode_array_offset_bytes, v1.header.ucode_size_bytes, am.GFX_FW_TYPE_SMU)
      # RDNA2 (GC < 11.0.0) v2_1 SMC firmware also carries per-state pptables to be loaded as separate descs.
      if self.adev.ip_ver[am.GC_HWIP] < (11,0,0) and hasattr(hdr, 'pptable_count'):
        p2stables = (am.struct_smc_soft_pptable_entry * hdr.pptable_count).from_buffer(blob[hdr.pptable_entry_offset:])
        for p2stable in p2stables:
          if p2stable.id == (__P2S_TABLE_ID_X:=0x50325358):
            self.descs += [self.desc(blob, p2stable.ppt_offset_bytes, p2stable.ppt_size_bytes, am.GFX_FW_TYPE_P2S_TABLE)]

    # SDMA firmware
    blob, hdr = self.load_fw(f"sdma_{fmt_ver(am.SDMA0_HWIP)}.bin", versioned_header="struct_sdma_firmware_header")
    if hdr.header.header_version_major == 1:
      # RDNA2 (sdma_v5_2 on Sienna Cichlid) has 2 SDMA engines but PSP only accepts a single SDMA0 load —
      # the engines share the firmware internally. Loading SDMA1/2/3 with the same blob causes PSP to
      # reject with status 0xFFFF0006. Restrict to SDMA0 only for v1 sdma headers.
      self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.header.ucode_size_bytes, am.GFX_FW_TYPE_SDMA0)]
    elif hdr.header.header_version_major == 2:
      self.descs += [self.desc(blob, hdr.ctl_ucode_offset, hdr.ctl_ucode_size_bytes, am.GFX_FW_TYPE_SDMA_UCODE_TH1)]
      self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.ctx_ucode_size_bytes, am.GFX_FW_TYPE_SDMA_UCODE_TH0)]
    else: self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.ucode_size_bytes, am.GFX_FW_TYPE_SDMA_UCODE_TH0)]

    # PFP, ME, MEC firmware
    for (fw_name, fw_cnt) in ([('PFP', 1), ('ME', 1)] if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else []) + [('MEC', 1)]:
      blob, hdr = self.load_fw(f"gc_{fmt_ver(am.GC_HWIP)}_{fw_name.lower()}.bin", versioned_header="struct_gfx_firmware_header")

      ucode_off = hdr.header.ucode_array_offset_bytes
      if hdr.header.header_version_major == 1:
        # Legacy CP firmware (RDNA2 sienna_cichlid_mec.bin etc.): submit the full ucode blob (code + JT inline)
        # under just the main CP_{fw_name} type. Sienna Cichlid PSP rejects GFX_FW_TYPE_CP_*_ME1 (status 0xFFFF0006)
        # and the inline JT in the same buffer is sufficient for the engine bringup.
        self.descs += [self.desc(blob, ucode_off, hdr.header.ucode_size_bytes, getattr(am, f'GFX_FW_TYPE_CP_{fw_name}'))]
      else:
        # Code
        self.descs += [self.desc(blob, ucode_off, hdr.ucode_size_bytes, getattr(am, f'GFX_FW_TYPE_RS64_{fw_name}'))]
        # Stack
        stack_fws = [getattr(am, f'GFX_FW_TYPE_RS64_{fw_name}_P{fwnum}_STACK') for fwnum in range(fw_cnt)]
        self.descs += [self.desc(blob, hdr.data_offset_bytes, hdr.data_size_bytes, *stack_fws)]
        self.ucode_start[fw_name] = hdr.ucode_start_addr_lo | (hdr.ucode_start_addr_hi << 32)

    # IMU firmware
    if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0):
      blob, hdr = self.load_fw(f"gc_{fmt_ver(am.GC_HWIP)}_imu.bin", am.struct_imu_firmware_header_v1_0)
      imu_i_off, imu_i_sz, imu_d_sz = hdr.header.ucode_array_offset_bytes, hdr.imu_iram_ucode_size_bytes, hdr.imu_dram_ucode_size_bytes
      self.descs += [self.desc(blob, imu_i_off, imu_i_sz, am.GFX_FW_TYPE_IMU_I), self.desc(blob, imu_i_off+imu_i_sz, imu_d_sz, am.GFX_FW_TYPE_IMU_D)]

    # RLC firmware
    blob, hdr0, hdr1, hdr2, hdr3 = self.load_fw(f"gc_{fmt_ver(am.GC_HWIP)}_rlc.bin", am.struct_rlc_firmware_header_v2_0,
      am.struct_rlc_firmware_header_v2_1, am.struct_rlc_firmware_header_v2_2, am.struct_rlc_firmware_header_v2_3)

    if hdr0.header.header_version_minor == 1:
      for mem,fmem in [('LIST_SRM_CNTL', 'list_cntl'), ('LIST_GPM_MEM', 'list_gpm'), ('LIST_SRM_MEM', 'list_srm')]:
        off, sz = getattr(hdr1, f'save_restore_{fmem}_offset_bytes'), getattr(hdr1, f'save_restore_{fmem}_size_bytes')
        self.descs += [self.desc(blob, off, sz, getattr(am, f'GFX_FW_TYPE_RLC_RESTORE_{mem}'))]

    if hdr0.header.header_version_minor >= 2:
      for mem,fmem in [('IRAM', 'iram'), ('DRAM_BOOT', 'dram')]:
        off, sz = getattr(hdr2, f'rlc_{fmem}_ucode_offset_bytes'), getattr(hdr2, f'rlc_{fmem}_ucode_size_bytes')
        self.descs += [self.desc(blob, off, sz, getattr(am, f'GFX_FW_TYPE_RLC_{mem}'))]

    if hdr0.header.header_version_minor == 3:
      for mem in ['P', 'V']:
        off, sz = getattr(hdr3, f'rlc{mem.lower()}_ucode_offset_bytes'), getattr(hdr3, f'rlc{mem.lower()}_ucode_size_bytes')
        self.descs += [self.desc(blob, off, sz, getattr(am, f'GFX_FW_TYPE_RLC_{mem}'))]

    self.descs += [self.desc(blob, hdr0.header.ucode_array_offset_bytes, hdr0.header.ucode_size_bytes, am.GFX_FW_TYPE_RLC_G)]

  def load_fw(self, fname:str, *headers, versioned_header:str|None=None):
    fname = self.FW_NAME_MAP.get(fname, fname)  # Remap RDNA2 codename firmware
    fpath = fetch(f"https://gitlab.com/kernel-firmware/linux-firmware/-/raw/1e2c15348485939baf1b6d1f5a7a3b799d80703d/amdgpu/{fname}", subdir="fw")
    blob = memoryview(bytearray(fpath.read_bytes()))
    if AM_DEBUG >= 1: print(f"am {self.adev.devfmt}: loading firmware {fname}: {hashlib.sha256(blob).hexdigest()}")
    if versioned_header:
      chdr = am.struct_common_firmware_header.from_address(mv_address(blob))
      headers += (getattr(am, versioned_header + f"_v{chdr.header_version_major}_{chdr.header_version_minor}"),)
    return tuple([blob] + [hdr.from_address(mv_address(blob)) for hdr in headers])

  def desc(self, blob:memoryview, offset:int, size:int, *types:int) -> tuple[list[int], memoryview]: return (list(types), blob[offset:offset+size])

class AMPageTableEntry:
  def __init__(self, adev, paddr, lv): self.adev, self.paddr, self.lv, self.entries = adev, paddr, lv, adev.vram.view(paddr, 0x1000, fmt='Q')

  def set_entry(self, entry_id:int, paddr:int, table=False, uncached=False, aspace=AddrSpace.PHYS, snooped=False, frag=0, valid=True):
    is_sys = aspace is AddrSpace.SYS
    if aspace is AddrSpace.PHYS: paddr = self.adev.paddr2xgmi(paddr)
    assert paddr & self.adev.gmc.address_space_mask == paddr, f"Invalid physical address {paddr:#x}"
    self.entries[entry_id] = self.adev.gmc.get_pte_flags(self.lv, table, frag, uncached, is_sys, snooped, valid) | (paddr & 0x0000FFFFFFFFF000)

  def entry(self, entry_id:int) -> int: return self.entries[entry_id]
  def valid(self, entry_id:int) -> bool: return (self.entries[entry_id] & am.AMDGPU_PTE_VALID) != 0
  def address(self, entry_id:int) -> int:
    assert self.entries[entry_id] & am.AMDGPU_PTE_SYSTEM == 0, "should not be system address"
    return self.adev.xgmi2paddr(self.entries[entry_id] & 0x0000FFFFFFFFF000)
  def is_page(self, entry_id:int) -> bool: return self.lv == am.AMDGPU_VM_PTB or self.adev.gmc.is_pte_huge_page(self.lv, self.entries[entry_id])
  def supports_huge_page(self, paddr:int): return self.lv >= am.AMDGPU_VM_PDB2

class AMMemoryManager(MemoryManager):
  va_allocator = TLSFAllocator((1 << 44), base=0x200000000000) # global for all devices.

  def on_range_mapped(self):
    # Invalidate TLB after mappings.
    self.dev.gmc.flush_tlb(ip='GC', vmid=0)
    self.dev.gmc.flush_tlb(ip='MM', vmid=0)

class AMDev:
  Version = 0xA0000008

  def __init__(self, pci_dev:PCIDevice, reset_mode=False):
    self.pci_dev, self.devfmt = pci_dev, pci_dev.pcibus
    self.vram, self.doorbell64, self.mmio = self.pci_dev.map_bar(0), self.pci_dev.map_bar(2, fmt='Q'), self.pci_dev.map_bar(5, fmt='I')

    self._run_discovery()
    self._build_regs()

    # AM boot Process:
    # The GPU being passed can be in one of several states: 1. Not initialized. 2. Initialized by amdgpu. 3. Initialized by AM.
    # The 1st and 2nd states require a full GPU setup since their states are unknown. The 2nd state also requires a mode1 reset to
    # reinitialize all components.
    #
    # The 3rd state can be set up partially to optimize boot time. In this case, only the GFX and SDMA IPs need to be initialized.
    # To enable this, AM uses a separate boot memory that is guaranteed not to be overwritten. This physical memory is utilized for
    # all blocks that are initialized only during the initial AM boot.
    # To determine if the GPU is in the third state, AM uses regSCRATCH_REG7 as a flag.
    # To determine if the previous AM session finalized correctly, AM uses regSCRATCH_REG6 as a flag.
    self.is_booting = True # During boot only boot memory can be allocated. This flag is to validate this.
    self.init_sw(smi_dev=False)

    self.partial_boot = (self.reg("regSCRATCH_REG7").read() == AMDev.Version) and (getenv("AM_RESET", 0) != 1)
    if self.partial_boot and (self.reg("regSCRATCH_REG6").read() != 0 or self.reg(self.gmc.pf_status_reg("GC")).read() != 0):
      if DEBUG >= 2: print(f"am {self.devfmt}: Malformed state. Issuing a full reset.")
      self.partial_boot = False

    # Init hw for IP blocks where it is needed
    if not self.partial_boot:
      if self.psp.is_sos_alive() and self.smu.is_smu_alive():
        if self.is_hive():
          if reset_mode: return # in reset mode, do not raise
          raise RuntimeError("Malformed state. Use extra/amdpci/hive_reset.py to reset the hive")
        self.smu.mode1_reset()
      self.init_hw(self.soc, self.gmc, self.ih, self.psp, self.smu)

    # Booting done
    self.is_booting = False

    # Re-initialize main blocks
    self.init_hw(self.gfx, self.sdma)
    self.pci_dev.write_config(pci.PCI_COMMAND, self.pci_dev.read_config(pci.PCI_COMMAND, 2) | pci.PCI_COMMAND_MASTER, 2)

    # set_clocks queries the DPM frequency table via PPSMC_MSG_GetDpmFreqByIndex and forces min/max
    # clocks to the highest level. This is a performance optimization, not a correctness requirement —
    # the GPU runs kernels fine at default clocks. On RDNA2 (Sienna Cichlid, MP1 11.0.7) the SMU message
    # IDs in our smu_v11_0_7 autogen don't match what the firmware expects (GetDpmFreqByIndex returns
    # 0xFF "unknown message"), so we currently skip this on a query failure rather than blocking init.
    # See tiny-egpu task #8 for the proper fix (likely a different ppsmc.h or sibling autogen module).
    try: self.smu.set_clocks(level=-1) # last level, max perf.
    except TimeoutError as e:
      if DEBUG >= 2: print(f"am {self.devfmt}: skipping set_clocks(level=-1), running at default clocks ({e})")
    # set_clockgating_state is power management policy (RLC safe mode + CGCG/CGLS clock gating
    # + perfmon overrides). None of it affects kernel execution correctness; it's pure perf/power
    # tuning. On RDNA2 (Sienna Cichlid) the RLC_SAFE_MODE write doesn't ack — bit 0 stays at 1,
    # which probably means either RLC's safe-mode handshake works differently here or our field
    # encoding is wrong for the v10 RLC. Skipping on failure rather than blocking init; the GPU
    # runs at default clock-gating settings. Proper fix is the same shape as the set_clocks one.
    for ip in [self.soc, self.gfx]:
      try: ip.set_clockgating_state()
      except TimeoutError as e:
        if DEBUG >= 2: print(f"am {self.devfmt}: skipping {ip.__class__.__name__}.set_clockgating_state ({e})")
    self.reg("regSCRATCH_REG7").write(AMDev.Version)
    self.reg("regSCRATCH_REG6").write(1) # set initialized state.
    if DEBUG >= 2: print(f"am {self.devfmt}: boot done")

  def init_sw(self, smi_dev=False):
    self.smi_dev, self.is_err_state = smi_dev, False

    # Memory manager & firmware
    self.mm = AMMemoryManager(self, self.vram_size - self.reserved_vram_size, boot_size=(32 << 20), pt_t=AMPageTableEntry, va_shifts=[12, 21, 30, 39],
      va_bits=48, first_lv=am.AMDGPU_VM_PDB2, va_base=AMMemoryManager.va_allocator.base, reserve_ptable=not self.large_bar,
      palloc_ranges=[(1 << (i + 12), (2 << 20) if i >= 9 else 0x1000) for i in range(9 * (3 - am.AMDGPU_VM_PDB2), -1, -1)])
    self.fw = AMFirmware(self)

    # Initialize IP blocks
    self.soc:AM_SOC = AM_SOC(self)
    self.gmc:AM_GMC = AM_GMC(self)
    self.ih:AM_IH = AM_IH(self)
    self.psp:AM_PSP = AM_PSP(self)
    self.smu:AM_SMU = AM_SMU(self)
    self.gfx:AM_GFX = AM_GFX(self)
    self.sdma:AM_SDMA = AM_SDMA(self)

    # Init sw for all IP blocks
    for ip in [self.soc, self.gmc, self.ih, self.psp, self.smu, self.gfx, self.sdma]: ip.init_sw()

  def init_hw(self, *blocks:AM_IP):
    for ip in blocks:
      ip.init_hw()
      if DEBUG >= 2: print(f"am {self.devfmt}: {ip.__class__.__name__} initialized")

  def fini(self):
    if DEBUG >= 2: print(f"am {self.devfmt}: Finalizing")
    for ip in [self.sdma, self.gfx]: ip.fini_hw()
    self.smu.set_clocks(level=0)
    self.ih.interrupt_handler()
    self.reg("regSCRATCH_REG6").write(self.is_err_state) # set finalized state.

  def recover(self, force=False) -> bool:
    if not force and not self.is_err_state: return False
    if DEBUG >= 3: print(f"am {self.devfmt}: Start recovery")
    self.ih.interrupt_handler()
    self.gfx.reset_mec()
    self.is_err_state = False
    if DEBUG >= 3: print(f"am {self.devfmt}: Recovery complete")
    return True

  def is_hive(self) -> bool: return self.gmc.xgmi_seg_sz > 0

  def paddr2mc(self, paddr:int) -> int: return self.gmc.mc_base + paddr
  def paddr2xgmi(self, paddr:int) -> int: return self.gmc.paddr_base + paddr
  def xgmi2paddr(self, xgmi_paddr:int) -> int: return xgmi_paddr - self.gmc.paddr_base

  # RDNA3 NBIO uses regBIF_BX0_/regBIF_BX_PF0_ naming. RDNA2 NBIO 2.3 uses bare regBIF_/reg* or regBIF_BX_PF_.
  _NBIO_RDNA2_ALIASES = (
    ('regBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL', 'regREMAP_HDP_MEM_FLUSH_CNTL'),
    ('regBIF_BX0_REMAP_HDP_REG_FLUSH_CNTL', 'regREMAP_HDP_REG_FLUSH_CNTL'),
    ('regBIF_BX0_BIF_DOORBELL_INT_CNTL',    'regBIF_DOORBELL_INT_CNTL'),
    ('regBIF_BX0_PCIE_INDEX2',              'regPCIE_INDEX2'),
    ('regBIF_BX0_PCIE_DATA2',               'regPCIE_DATA2'),
  )

  def reg(self, reg:str) -> AMRegister:
    if reg in self.__dict__: return self.__dict__[reg]
    for new, old in self._NBIO_RDNA2_ALIASES:
      if reg == new and old in self.__dict__: return self.__dict__[old]
    raise KeyError(reg)

  def __getattr__(self, name:str):
    if name.startswith('reg'):
      for new, old in self._NBIO_RDNA2_ALIASES:
        if name == new and old in self.__dict__: return self.__dict__[old]
    # Some IP code paths (notably AM_SMU) use direct self.adev.mm* attribute access. Our import_asic_regs
    # normalized RDNA2's 'mm' prefix to 'reg' at load time, so fall back accordingly.
    if name.startswith('mm') and (alt:='reg' + name[2:]) in self.__dict__: return self.__dict__[alt]
    raise AttributeError(name)

  def rreg(self, reg:int) -> int:
    val = self.indirect_rreg(reg) if reg >= len(self.mmio) else self.mmio[reg]
    if AM_DEBUG >= 4 and getattr(self, '_prev_rreg', None) != (reg, val): print(f"am {self.devfmt}: Reading register {reg:#x} with value {val:#x}")
    self._prev_rreg = (reg, val)
    return val

  def wreg(self, reg:int, val:int):
    if AM_DEBUG >= 4: print(f"am {self.devfmt}: Writing register {reg:#x} with value {val:#x}")
    if reg >= len(self.mmio): self.indirect_wreg(reg, val)
    else: self.mmio[reg] = val

  def wreg_pair(self, reg_base:str, lo_suffix:str, hi_suffix:str, val:int, inst:int=0):
    self.reg(f"{reg_base}{lo_suffix}").write(lo32(val), inst=inst)
    self.reg(f"{reg_base}{hi_suffix}").write(hi32(val), inst=inst)

  def indirect_rreg(self, reg:int) -> int:
    self.reg("regBIF_BX_PF0_RSMU_INDEX").write(reg * 4)
    return self.reg("regBIF_BX_PF0_RSMU_DATA").read()

  def indirect_wreg(self, reg:int, val:int):
    self.reg("regBIF_BX_PF0_RSMU_INDEX").write(reg * 4)
    self.reg("regBIF_BX_PF0_RSMU_DATA").write(val)

  def indirect_wreg_pcie(self, reg:int, val:int, aid:int=0):
    reg_addr = reg * 4 + ((((aid & 0b11) << 32) | (1 << 34)) if aid > 0 else 0)
    self.reg("regBIF_BX0_PCIE_INDEX2").write(lo32(reg_addr))
    if hi32(reg_addr) > 0: self.reg("regBIF_BX0_PCIE_INDEX2_HI").write(hi32(reg_addr) & 0xff)
    self.reg("regBIF_BX0_PCIE_DATA2").write(val)
    if hi32(reg_addr) > 0: self.reg("regBIF_BX0_PCIE_INDEX2_HI").write(0)

  def _read_vram(self, addr, size) -> bytes:
    assert addr % 4 == 0 and size % 4 == 0, f"Invalid address {addr:#x} or size {size:#x}"
    res = []
    for caddr in range(addr, addr + size, 4):
      self.wreg(0x06, caddr >> 31)
      self.wreg(0x00, (caddr & 0x7FFFFFFF) | 0x80000000)
      res.append(self.rreg(0x01))
    return bytes(array.array('I', res))

  def _run_discovery(self):
    # NOTE: Fixed register to query memory size without known ip bases to find the discovery table.
    #       The table is located at the end of VRAM - 64KB and is 10KB in size.
    mmRCC_CONFIG_MEMSIZE = 0xde3
    self.vram_size = self.rreg(mmRCC_CONFIG_MEMSIZE) << 20
    self.large_bar = self.vram.nbytes >= self.vram_size
    tmr_offset, tmr_size = self.vram_size - (64 << 10), (10 << 10)

    disc_tbl = self.vram.view(tmr_offset, tmr_size)[:] if self.large_bar else self._read_vram(tmr_offset, tmr_size)
    self.bhdr = am.struct_binary_header.from_buffer(bytearray(disc_tbl))
    ihdr = am.struct_ip_discovery_header.from_address(ctypes.addressof(self.bhdr) + self.bhdr.table_list[am.IP_DISCOVERY].offset)
    assert self.bhdr.binary_signature == am.BINARY_SIGNATURE and ihdr.signature == am.DISCOVERY_TABLE_SIGNATURE, "discovery signatures mismatch"

    self.regs_offset:dict[int, dict[int, tuple]] = collections.defaultdict(dict)
    self.ip_ver:dict[int, tuple[int, int, int]] = {}

    for num_die in range(ihdr.num_dies):
      dhdr = am.struct_die_header.from_address(ctypes.addressof(self.bhdr) + ihdr.die_info[num_die].die_offset)

      ip_offset = ctypes.addressof(self.bhdr) + ctypes.sizeof(dhdr) + ihdr.die_info[num_die].die_offset
      for _ in range(dhdr.num_ips):
        ip = am.struct_ip_v4.from_address(ip_offset)
        ba = ((ctypes.c_uint64 if ihdr.base_addr_64_bit else ctypes.c_uint32) * ip.num_base_address).from_address(ip_offset + 8)
        for hw_ip in range(1, am.MAX_HWIP):
          if hw_ip in am.hw_id_map and am.hw_id_map[hw_ip] == ip.hw_id:
            self.regs_offset[hw_ip][ip.instance_number] = tuple(list(ba))
            self.ip_ver[hw_ip] = (ip.major, ip.minor, ip.revision)

        ip_offset += 8 + (8 if ihdr.base_addr_64_bit else 4) * ip.num_base_address

    gc_info = am.struct_gc_info_v1_0.from_address(gc_addr:=ctypes.addressof(self.bhdr) + self.bhdr.table_list[am.GC].offset)
    self.gc_info = getattr(am, f"struct_gc_info_v{gc_info.header.version_major}_{gc_info.header.version_minor}").from_address(gc_addr)
    self.reserved_vram_size = (384 << 20) if self.ip_ver[am.GC_HWIP][:2] in {(9,4), (9,5)} else (64 << 20)

  @functools.cached_property
  def hwid_names(self) -> dict[int, str]: return {v:k.removesuffix('_HWID') for k,v in vars(am).items() if k.endswith('_HWID') and isinstance(v, int)}

  def _ip_module(self, prefix:str, hwip, prever_prefix:str=""): return import_module(prefix, self.ip_ver[hwip], prever_prefix)

  def _build_regs(self):
    mods = [("mp", am.MP0_HWIP), ("hdp", am.HDP_HWIP), ("gc", am.GC_HWIP), ("mmhub", am.MMHUB_HWIP), ("osssys", am.OSSSYS_HWIP),
      ("nbio" if self.ip_ver[am.GC_HWIP] < (12,0,0) else "nbif", am.NBIO_HWIP)]
    if self.ip_ver[am.SDMA0_HWIP] in {(4,4,2), (4,4,4)}: mods += [("sdma", am.SDMA0_HWIP)]

    for prefix, hwip in mods:
      self.__dict__.update(import_asic_regs(prefix, self.ip_ver[hwip], cls=functools.partial(AMRegister, adev=self, bases=self.regs_offset[hwip])))
    self.__dict__.update(import_asic_regs('mp', (11, 0), cls=functools.partial(AMRegister, adev=self, bases=self.regs_offset[am.MP1_HWIP])))
