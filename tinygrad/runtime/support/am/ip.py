import ctypes, time, contextlib, functools
from typing import cast, Literal
from tinygrad.helpers import to_mv, data64, lo32, hi32, DEBUG, wait_cond, pad_bytes, getbits, getenv
from tinygrad.runtime.autogen.am import am
from tinygrad.runtime.support.amd import import_soc
from tinygrad.runtime.support.memory import AddrSpace

class AM_IP:
  def __init__(self, adev): self.adev = adev
  def init_sw(self): pass # Prepare sw/allocations for this IP
  def init_hw(self): pass # Initialize hw for this IP
  def fini_hw(self): pass # Finalize hw for this IP
  def set_clockgating_state(self): pass # Set clockgating state for this IP

class AM_SOC(AM_IP):
  def init_sw(self):
    self.module = import_soc(self.adev.ip_ver[am.GC_HWIP])
    self.ih_clients = am.enum_soc21_ih_clientid if (ih_soc21:=self.adev.ip_ver[am.GC_HWIP][0] >= 11) else am.enum_soc15_ih_clientid

    self.gfx_ih_clients = [am.SOC21_IH_CLIENTID_GRBM_CP, am.SOC21_IH_CLIENTID_GFX] \
      if ih_soc21 else [am.SOC15_IH_CLIENTID_GRBM_CP] + [getattr(am, f'SOC15_IH_CLIENTID_SE{i}SH') for i in range(4)]
    self.sdma_ih_clients = [] if ih_soc21 else [getattr(am, f'SOC15_IH_CLIENTID_SDMA{i}') for i in range(8)]

    def _ih_srcs(pref:str, hwip:int) -> dict[int, str]:
      return {getattr(am, k): k[off+9:] for k in dir(am) if k.startswith(f'{pref}_{self.adev.ip_ver[hwip][0]}') and (off:=k.find('__SRCID__')) != -1}

    gfx_srcs, sdma_srcs = _ih_srcs('GFX', am.GC_HWIP), _ih_srcs('SDMA0', am.SDMA0_HWIP)
    self.ih_srcs_names:dict[int, dict[int, str]] = {**{k: gfx_srcs for k in self.gfx_ih_clients}, **{k: sdma_srcs for k in self.sdma_ih_clients}}

  def init_hw(self):
    if self.adev.ip_ver[am.NBIO_HWIP] in {(7,9,0), (7,9,1)}:
      self.adev.regXCC_DOORBELL_FENCE.write(0x0)
      for aid in range(1, self.adev.gmc.vmhubs):
        self.adev.indirect_wreg_pcie(self.adev.regXCC_DOORBELL_FENCE.addr[0], self.adev.regXCC_DOORBELL_FENCE.encode(shub_slv_mode=1), aid=aid)
      self.adev.regBIFC_GFX_INT_MONITOR_MASK.write(0x7ff)
      self.adev.regBIFC_DOORBELL_ACCESS_EN_PF.write(0xfffff)
    elif 'regRCC_DEV0_EPF2_STRAP2' in self.adev.__dict__:  # RDNA2 NBIO 2.3 has no EPF2
      self.adev.regRCC_DEV0_EPF2_STRAP2.update(strap_no_soft_reset_dev0_f2=0x0)
    self.adev.regRCC_DEV0_EPF0_RCC_DOORBELL_APER_EN.write(0x1)
  def set_clockgating_state(self):
    if self.adev.ip_ver[am.HDP_HWIP] >= (5,2,1): self.adev.regHDP_MEM_POWER_CTRL.update(atomic_mem_power_ctrl_en=1, atomic_mem_power_ds_en=1)

  def doorbell_enable(self, port, awid=0, awaddr_31_28_value=0, offset=0, size=0, aid=0):
    reg = self.adev.reg(f"{'regGDC_S2A0_S2A' if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else 'regS2A'}_DOORBELL_ENTRY_{port}_CTRL")
    val = reg.encode(**{f"s2a_doorbell_port{port}_enable":1, f"s2a_doorbell_port{port}_awid":awid,  f"s2a_doorbell_port{port}_range_size":size,
      f"s2a_doorbell_port{port}_awaddr_31_28_value":awaddr_31_28_value, f"s2a_doorbell_port{port}_range_offset":offset})

    if self.adev.ip_ver[am.NBIO_HWIP] in {(7,9,0), (7,9,1)}: self.adev.indirect_wreg_pcie(reg.addr[0], val, aid=aid)
    else: reg.write(val)

class AM_GMC(AM_IP):
  def init_sw(self):
    self.vmhubs = len(self.adev.regs_offset[am.MMHUB_HWIP])

    # XGMI (for supported systems)
    self.xgmi_phys_id = self.adev.regMMMC_VM_XGMI_LFB_CNTL.read_bitfields()['pf_lfb_region'] if hasattr(self.adev, 'regMMMC_VM_XGMI_LFB_CNTL') else 0
    self.xgmi_seg_sz = self.adev.regMMMC_VM_XGMI_LFB_SIZE.read_bitfields()['pf_lfb_size']<<24 if hasattr(self.adev, 'regMMMC_VM_XGMI_LFB_SIZE') else 0

    self.paddr_base = self.xgmi_phys_id * self.xgmi_seg_sz

    self.fb_base = (self.adev.regMMMC_VM_FB_LOCATION_BASE.read() & 0xFFFFFF) << 24
    self.fb_end = (self.adev.regMMMC_VM_FB_LOCATION_TOP.read() & 0xFFFFFF) << 24

    # Memory controller aperture
    self.mc_base = self.fb_base + self.paddr_base

    # VM aperture
    self.vm_base = self.adev.mm.va_base
    self.vm_end = min(self.vm_base + (1 << self.adev.mm.va_bits) - 1, 0x7fffffffffff)

    self.trans_futher = self.adev.ip_ver[am.GC_HWIP] < (10, 0, 0)

    # mi3xx has 48-bit, others have 44-bit address space
    self.address_space_mask = (1 << (48 if self.adev.ip_ver[am.GC_HWIP][:2] in {(9,4), (9,5)} else 44)) - 1

    self.memscratch_xgmi_paddr = self.adev.paddr2xgmi(self.adev.mm.palloc(0x1000, zero=False, boot=True))
    self.dummy_page_xgmi_paddr = self.adev.paddr2xgmi(self.adev.mm.palloc(0x1000, zero=False, boot=True))

    # MM hub is inited before any tlb flushes and is still valid during partial_boot, so set it to true
    self.hub_initted = {"MM": True, "GC": False}

    self.pf_status_reg = lambda ip: f"reg{ip}VM_L2_PROTECTION_FAULT_STATUS{'_LO32' if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else ''}"

  def init_hw(self): self.init_hub("MM", inst_cnt=self.vmhubs)

  def flush_hdp(self): self.adev.wreg(self.adev.reg("regBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL").read() // 4, 0x0)
  def flush_tlb(self, ip:Literal["MM", "GC"], vmid, flush_type=0):
    self.flush_hdp()

    # Can't issue TLB invalidation if the hub isn't initialized.
    if not self.hub_initted[ip]: return

    for inst in range(self.adev.gmc.vmhubs if ip == "MM" else self.adev.gfx.xccs):
      if ip == "MM": wait_cond(lambda: self.adev.regMMVM_INVALIDATE_ENG17_SEM.read(inst=inst) & 0x1, value=1, msg="mm flush_tlb timeout")

      self.adev.reg(f"reg{ip}VM_INVALIDATE_ENG17_REQ").write(flush_type=flush_type, per_vmid_invalidate_req=(1 << vmid), invalidate_l2_ptes=1,
        invalidate_l2_pde0=1, invalidate_l2_pde1=1, invalidate_l2_pde2=1, invalidate_l1_ptes=1, clear_protection_fault_status_addr=0, inst=inst)

      wait_cond(lambda: self.adev.reg(f"reg{ip}VM_INVALIDATE_ENG17_ACK").read(inst=inst) & (1 << vmid), value=(1 << vmid), msg="flush_tlb timeout")

      if ip == "MM": self.adev.regMMVM_INVALIDATE_ENG17_SEM.write(0x0, inst=inst)
      if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0) and ip == "MM":
        self.adev.regMMVM_L2_BANK_SELECT_RESERVED_CID2.update(reserved_cache_private_invalidation=1, inst=inst)

        # Read back the register to ensure the invalidation is complete
        self.adev.regMMVM_L2_BANK_SELECT_RESERVED_CID2.read(inst=inst)

  def enable_vm_addressing(self, page_table, ip:Literal["MM", "GC"], vmid, inst):
    self.adev.wreg_pair(f"reg{ip}VM_CONTEXT{vmid}_PAGE_TABLE_START_ADDR", "_LO32", "_HI32", self.vm_base >> 12, inst=inst)
    self.adev.wreg_pair(f"reg{ip}VM_CONTEXT{vmid}_PAGE_TABLE_END_ADDR", "_LO32", "_HI32", self.vm_end >> 12, inst=inst)
    self.adev.wreg_pair(f"reg{ip}VM_CONTEXT{vmid}_PAGE_TABLE_BASE_ADDR", "_LO32", "_HI32", self.adev.paddr2xgmi(page_table.paddr) | 1, inst=inst)

    fault_flags = {f'{x}_protection_fault_enable_interrupt':1 for x in ['pde0', 'dummy_page', 'range', 'valid', 'read', 'write', 'execute']}
    en_def_flags = {f'{x}_protection_fault_enable_default':1 for x in ['pde0', 'dummy_page', 'range', 'valid', 'read', 'write', 'execute']}
    self.adev.reg(f"reg{ip}VM_CONTEXT{vmid}_CNTL").write(0x1800000, **fault_flags, **en_def_flags, enable_context=1,
      page_table_depth=((2 if self.trans_futher else 3) - page_table.lv), page_table_block_size=9 if self.trans_futher else 0, inst=inst)

  def init_hub(self, ip:Literal["MM", "GC"], inst_cnt:int):
    # Init system apertures
    for inst in range(inst_cnt):
      self.adev.reg(f"reg{ip}MC_VM_AGP_BASE").write(0, inst=inst)
      self.adev.reg(f"reg{ip}MC_VM_AGP_BOT").write(0xffffffffffff >> 24, inst=inst) # disable AGP
      self.adev.reg(f"reg{ip}MC_VM_AGP_TOP").write(0, inst=inst)

      self.adev.reg(f"reg{ip}MC_VM_SYSTEM_APERTURE_LOW_ADDR").write(self.fb_base >> 18, inst=inst)
      self.adev.reg(f"reg{ip}MC_VM_SYSTEM_APERTURE_HIGH_ADDR").write(self.fb_end >> 18, inst=inst)
      self.adev.wreg_pair(f"reg{ip}MC_VM_SYSTEM_APERTURE_DEFAULT_ADDR", "_LSB", "_MSB", self.memscratch_xgmi_paddr >> 12, inst=inst)
      self.adev.wreg_pair(f"reg{ip}VM_L2_PROTECTION_FAULT_DEFAULT_ADDR", "_LO32", "_HI32", self.dummy_page_xgmi_paddr >> 12, inst=inst)

      self.adev.reg(f"reg{ip}VM_L2_PROTECTION_FAULT_CNTL2").update(active_page_migration_pte_read_retry=1, inst=inst)

      # Init TLB and cache
      self.adev.reg(f"reg{ip}MC_VM_MX_L1_TLB_CNTL").update(enable_l1_tlb=1, system_access_mode=3, enable_advanced_driver_model=1,
        system_aperture_unmapped_access=0, mtype=self.adev.soc.module.MTYPE_UC, inst=inst)

      self.adev.reg(f"reg{ip}VM_L2_CNTL").update(enable_l2_cache=1, enable_default_page_out_to_system_memory=1,
        l2_pde0_cache_tag_generation_mode=0, pde_fault_classification=0, context1_identity_access_mode=1, identity_mode_fragment_size=0,
        enable_l2_fragment_processing=int(self.adev.ip_ver[am.GC_HWIP] < (10,0,0)), inst=inst)
      self.adev.reg(f"reg{ip}VM_L2_CNTL2").update(invalidate_all_l1_tlbs=1, invalidate_l2_cache=1, inst=inst)
      self.adev.reg(f"reg{ip}VM_L2_CNTL3").write(l2_cache_4k_associativity=1, l2_cache_bigk_associativity=1,
        bank_select=12 if self.trans_futher else 9, l2_cache_bigk_fragment_size=9 if self.trans_futher else 6, inst=inst)
      self.adev.reg(f"reg{ip}VM_L2_CNTL4").write(l2_cache_4k_partition_count=1, inst=inst)
      if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0): self.adev.reg(f"reg{ip}VM_L2_CNTL5").write(walker_priority_client_id=0x1ff, inst=inst)

      self.enable_vm_addressing(self.adev.mm.root_page_table, ip, vmid=0, inst=inst)

      # Disable identity aperture
      self.adev.wreg_pair(f"reg{ip}VM_L2_CONTEXT1_IDENTITY_APERTURE_LOW_ADDR", "_LO32", "_HI32", 0xfffffffff, inst=inst)
      self.adev.wreg_pair(f"reg{ip}VM_L2_CONTEXT1_IDENTITY_APERTURE_HIGH_ADDR", "_LO32", "_HI32", 0x0, inst=inst)
      self.adev.wreg_pair(f"reg{ip}VM_L2_CONTEXT_IDENTITY_PHYSICAL_OFFSET", "_LO32", "_HI32", 0x0, inst=inst)

      for eng_i in range(18): self.adev.wreg_pair(f"reg{ip}VM_INVALIDATE_ENG{eng_i}_ADDR_RANGE", "_LO32", "_HI32", 0x1fffffffff, inst=inst)
    self.hub_initted[ip] = True

  @functools.cache  # pylint: disable=method-cache-max-size-none
  def get_pte_flags(self, pte_lv, is_table, frag, uncached, system, snooped, valid, extra=0):
    extra |= (am.AMDGPU_PTE_SYSTEM * system) | (am.AMDGPU_PTE_SNOOPED * snooped) | (am.AMDGPU_PTE_VALID * valid) | am.AMDGPU_PTE_FRAG(frag)
    if not is_table: extra |= (am.AMDGPU_PTE_WRITEABLE | am.AMDGPU_PTE_READABLE | am.AMDGPU_PTE_EXECUTABLE)
    if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0):
      extra |= am.AMDGPU_PTE_MTYPE_GFX12(0, self.adev.soc.module.MTYPE_UC if uncached else 0)
      extra |= (am.AMDGPU_PDE_PTE_GFX12 if not is_table and pte_lv != am.AMDGPU_VM_PTB else (am.AMDGPU_PTE_IS_PTE if not is_table else 0))
    elif self.adev.ip_ver[am.GC_HWIP] >= (10,0,0):
      extra |= am.AMDGPU_PTE_MTYPE_NV10(0, self.adev.soc.module.MTYPE_UC if uncached else 0)
      extra |= (am.AMDGPU_PDE_PTE if not is_table and pte_lv != am.AMDGPU_VM_PTB else 0)
    else:
      extra |= am.AMDGPU_PTE_MTYPE_VG10(0, self.adev.soc.module.MTYPE_UC if uncached else 0)
      if is_table and pte_lv == am.AMDGPU_VM_PDB1: extra |= am.AMDGPU_PDE_BFS(0x9)
      if is_table and pte_lv == am.AMDGPU_VM_PDB0: extra |= am.AMDGPU_PTE_TF
      if not is_table and pte_lv not in {am.AMDGPU_VM_PTB, am.AMDGPU_VM_PDB0}: extra |= am.AMDGPU_PDE_PTE
    return extra
  def is_pte_huge_page(self, pte_lv, pte):
    if self.adev.ip_ver[am.GC_HWIP] < (10,0,0): return (pte & am.AMDGPU_PDE_PTE) if pte_lv != am.AMDGPU_VM_PDB0 else not (pte & am.AMDGPU_PTE_TF)
    return pte & (am.AMDGPU_PDE_PTE_GFX12 if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else am.AMDGPU_PDE_PTE)

class AM_SMU(AM_IP):
  def init_sw(self):
    self.smu_mod = self.adev._ip_module("smu", am.MP1_HWIP, prever_prefix='v')
    self.driver_table_paddr = self.adev.mm.palloc(0x4000, zero=False, boot=True)

  def init_hw(self):
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrHigh, hi32(self.adev.paddr2mc(self.driver_table_paddr)))
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrLow, lo32(self.adev.paddr2mc(self.driver_table_paddr)))
    self._send_msg(self.smu_mod.PPSMC_MSG_EnableAllSmuFeatures, 0)

  def is_smu_alive(self):
    with contextlib.suppress(TimeoutError): self._send_msg(self.smu_mod.PPSMC_MSG_GetSmuVersion, 0, timeout=100)
    return self.adev.mmMP1_SMN_C2PMSG_90.read() != 0

  def mode1_reset(self):
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: mode1 reset")
    if self.adev.ip_ver[am.MP0_HWIP] >= (14,0,0): self._send_msg(__DEBUGSMC_MSG_Mode1Reset:=2, 0, debug=True)
    elif self.adev.ip_ver[am.MP0_HWIP] in {(13,0,6), (13,0,12)}: self._send_msg(self.smu_mod.PPSMC_MSG_GfxDriverReset, 1)
    else: self._send_msg(self.smu_mod.PPSMC_MSG_Mode1Reset, 0)

    if not self.adev.is_hive(): time.sleep(0.5) # 500ms

  def read_table(self, table_t, arg):
    if self.adev.ip_ver[am.MP0_HWIP] in {(13,0,6),(13,0,12)}: self._send_msg(self.smu_mod.PPSMC_MSG_GetMetricsTable, arg)
    else: self._send_msg(self.smu_mod.PPSMC_MSG_TransferTableSmu2Dram, arg)
    return table_t.from_buffer(bytearray(self.adev.vram.view(self.driver_table_paddr, ctypes.sizeof(table_t))[:]))

  @functools.cache  # pylint: disable=method-cache-max-size-none
  def read_clocks(self, clk_list:tuple[int]) -> dict[int, list[int]]:
    return {clck: [self._send_msg(self.smu_mod.PPSMC_MSG_GetDpmFreqByIndex, (clck<<16)|i, read_back_arg=True)&0x7fffffff for i in range(cnt)]
      for clck in clk_list if (cnt:=self._send_msg(self.smu_mod.PPSMC_MSG_GetDpmFreqByIndex, (clck<<16)|0xff, read_back_arg=True)&0x7fffffff)}

  def set_clocks(self, level:int):
    clks = tuple([self.smu_mod.PPCLK_UCLK, self.smu_mod.PPCLK_FCLK, self.smu_mod.PPCLK_SOCCLK])
    if self.adev.ip_ver[am.MP0_HWIP] not in {(13,0,6), (13,0,12)}: clks += (self.smu_mod.PPCLK_GFXCLK,)

    for clck, vals in self.read_clocks(clks).items():
      with contextlib.suppress(TimeoutError): self._send_msg(self.smu_mod.PPSMC_MSG_SetSoftMinByFreq, clck << 16 | (vals[level]), timeout=20)
      if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0): self._send_msg(self.smu_mod.PPSMC_MSG_SetSoftMaxByFreq, clck << 16 | (vals[level]))

  def _aca_read_reg(self, bank_idx:int, reg_idx:int, ue=True) -> int:
    msg = self.smu_mod.PPSMC_MSG_McaBankDumpDW if ue else self.smu_mod.PPSMC_MSG_McaBankCeDumpDW
    return (self._send_msg(msg, (bank_idx << 16) | (reg_idx * 8 + 4), read_back_arg=True) << 32) | \
            self._send_msg(msg, (bank_idx << 16) | (reg_idx * 8), read_back_arg=True)

  def _aca_read_banks(self, ue=True) -> list[list[int]]:
    if not hasattr(self.smu_mod, 'PPSMC_MSG_QueryValidMcaCount'): return []
    count_msg = self.smu_mod.PPSMC_MSG_QueryValidMcaCount if ue else self.smu_mod.PPSMC_MSG_QueryValidMcaCeCount
    return [[self._aca_read_reg(idx, reg_idx, ue=ue) for reg_idx in range(16)] for idx in range(self._send_msg(count_msg, 0, read_back_arg=True))]

  def _smu_cmn_send_msg(self, msg:int, param=0, debug=False):
    (self.adev.mmMP1_SMN_C2PMSG_90 if not debug else self.adev.mmMP1_SMN_C2PMSG_54).write(0) # resp reg
    (self.adev.mmMP1_SMN_C2PMSG_82 if not debug else self.adev.mmMP1_SMN_C2PMSG_53).write(param)
    (self.adev.mmMP1_SMN_C2PMSG_66 if not debug else self.adev.mmMP1_SMN_C2PMSG_75).write(msg)

  def _send_msg(self, msg:int, param:int, read_back_arg=False, timeout=10000, debug=False): # default timeout is 10 seconds
    self._smu_cmn_send_msg(msg, param, debug=debug)
    wait_cond((self.adev.mmMP1_SMN_C2PMSG_90 if not debug else self.adev.mmMP1_SMN_C2PMSG_54).read, value=1, timeout_ms=timeout,
      msg=f"SMU msg {msg:#x} timeout")
    return (self.adev.mmMP1_SMN_C2PMSG_82 if not debug else self.adev.mmMP1_SMN_C2PMSG_53).read() if read_back_arg else None

class AM_GFX(AM_IP):
  def init_sw(self):
    self.xccs = len(self.adev.regs_offset[am.GC_HWIP])
    self.mqd_paddr = [self.adev.mm.palloc(0x1000 * self.xccs, zero=False, boot=True) for i in range(2)]
    self.mqd_mc = [self.adev.paddr2mc(mqd_paddr) for mqd_paddr in self.mqd_paddr]
    # KIQ bootstrap (gfx10 only): allocate KIQ MQD here in init_sw via boot palloc. Ring and meta
    # regions are deferred to _setup_kiq() because they need GPUVMA-backed valloc (which requires
    # is_booting=False). The MQD itself is fine with palloc+paddr2mc because MEC's MQD load uses
    # a different code path that accepts MC addresses, but the ring is fetched via the queue's
    # vmid translation which only works with GPUVMA addresses in vmid 0's mapped range.
    if self.adev.ip_ver[am.GC_HWIP][0] >= 10:
      self.kiq_mqd_paddr = self.adev.mm.palloc(0x1000, zero=True, boot=True)
      self.kiq_setup_done = False
      self.kiq_host_wptr_dws = 0  # internal counter in dwords

  def init_hw(self):
    # Wait for RLC autoload to complete. Linux's gfx_v10_0_wait_for_rlc_autoload waits for
    # CP_STAT == 0 AND BOOTLOAD_COMPLETE == 1. Only run on autoload-supported gens (GFX11+);
    # RDNA2 doesn't kick autoload, so bootload_complete stays 0 forever.
    if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0):
      wait_cond(lambda: self.adev.regCP_STAT.read() == 0 and self.adev.regRLC_RLCS_BOOTLOAD_STATUS.read_bitfields()['bootload_complete'] == 1,
                value=True, msg="RLC autoload timeout")

    self.adev.gmc.init_hub("GC", inst_cnt=self.xccs)
    if self.adev.partial_boot: return self.reset_mec()

    self._config_mec()

    # NOTE: Golden reg for gfx11. No values for this reg provided. The kernel just ors 0x20000000 to this reg.
    if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0):
      for xcc in range(self.xccs): self.adev.regTCP_CNTL.write(self.adev.regTCP_CNTL.read() | 0x20000000, inst=xcc)

    for xcc in range(self.xccs): self.adev.regRLC_CNTL.write(0x1, inst=xcc)

    for xcc in range(self.xccs): self.adev.regRLC_SRM_CNTL.update(srm_enable=1, auto_incr_addr=1, inst=xcc)

    for xcc in range(self.xccs): self.adev.regRLC_SPM_MC_CNTL.write(0xf, inst=xcc)

    # S2A doorbell routing is an NBIO 4.x+ feature (GFX11+). NBIO 2.x (RDNA2) and NBIO 7.9 (MI300)
    # do not use this routing — they enable doorbells via the BIF aperture (handled in AM_NBIO.init_hw).
    if self.adev.ip_ver[am.NBIO_HWIP][:2] != (7,9) and self.adev.ip_ver[am.NBIO_HWIP][0] >= 4:
      self.adev.soc.doorbell_enable(port=0, awid=0x3, awaddr_31_28_value=0x3)
      self.adev.soc.doorbell_enable(port=3, awid=0x6, awaddr_31_28_value=0x3)

    for xcc in range(self.xccs):
      if self.adev.ip_ver[am.GC_HWIP] in {(9,4,3), (9,5,0)}:
        self.adev.regGB_ADDR_CONFIG.write(0x2a114042, inst=xcc) # Golden value for mi300/mi350
        self.adev.regTCP_UTCL1_CNTL2.update(spare=1, inst=xcc)

      self.adev.regGRBM_CNTL.update(read_timeout=0xff, inst=xcc)
      for i in range(0, 16):
        self._grbm_select(vmid=i, inst=xcc)
        self.adev.regSH_MEM_CONFIG.write(**({'initial_inst_prefetch':3} if self.adev.ip_ver[am.GC_HWIP][0]>=10 else {'retry_disable':1}),
          **({'f8_mode':1} if self.adev.ip_ver[am.GC_HWIP][:2]==(9,4) else {}),
          address_mode=self.adev.soc.module.SH_MEM_ADDRESS_MODE_64, alignment_mode=self.adev.soc.module.SH_MEM_ALIGNMENT_MODE_UNALIGNED, inst=xcc)

        # Configure apertures:
        # LDS:         0x10000000'00000000 - 0x10000001'00000000 (4GB)
        # Scratch:     0x20000000'00000000 - 0x20000001'00000000 (4GB)
        self.adev.regSH_MEM_BASES.write(shared_base=0x1, private_base=0x2, inst=xcc)
      self._grbm_select(inst=xcc)

      # Configure MEC doorbell range
      self.adev.regCP_MEC_DOORBELL_RANGE_LOWER.write(0x100 * xcc, inst=xcc)
      self.adev.regCP_MEC_DOORBELL_RANGE_UPPER.write(0x100 * xcc + 0xf8, inst=xcc)

    self._enable_mec()

    # Set 1 partition
    if self.xccs > 1: self.adev.psp._spatial_partition_cmd(1)

    # KIQ bootstrap (gfx10 only). Must run after _enable_mec so MEC microcode is unhalted.
    # AMD_KIQ_BOOTSTRAP=1 enables; default OFF while we bisect a regression where MEC1 firmware
    # appears unloaded (HEADER_DUMP=0xdef0def0) regardless of whether _setup_kiq runs.
    if self.adev.ip_ver[am.GC_HWIP][0] >= 10 and not self.adev.partial_boot and getenv("AMD_KIQ_BOOTSTRAP", 0):
      try: self._setup_kiq()
      except Exception as e: print(f"am {self.adev.devfmt}: KIQ setup failed: {e}")

  def _setup_kiq(self):
    """Set up a minimal KIQ on me=1/pipe=2/queue=0 to bootstrap MEC's compute scheduler.

    The KIQ is the boot-time control queue that MEC processes unconditionally once
    RLC_CP_SCHEDULERS.scheduler0 is set with its location and the enable bit. We use it
    to submit SET_RESOURCES + MAP_QUEUES PM4 packets that enable normal compute queues.

    Without this, MEC's main scheduling loop never starts on the AM driver path because
    nothing kicks it (KFD's HQD-direct path on Linux relies on amdgpu's KIQ activation
    having already done that bootstrap).

    KIQ placement: me=1, pipe=2, queue=0 — a separate MEC1 pipe slot from our compute
    queue at me=1/pipe=0/queue=0. The MEC2-on-me=2 standard placement requires loading
    mec2.bin via PSP, which broke MEC1 loading on first attempt — reverted for now to
    bisect the MAP_QUEUES KCQ activation in isolation.
    """
    self.kiq_me, self.kiq_pipe, self.kiq_queue = 1, 2, 0
    self.kiq_doorbell_idx = am.AMDGPU_NAVI10_DOORBELL_KIQ
    self.kiq_mqd_mc = self.adev.paddr2mc(self.kiq_mqd_paddr)
    # Allocate the KIQ ring buffer via valloc — gives a GPUVMA address (in mm.va_base range)
    # that vmid 0 can translate to VRAM physical. Then map_bar the same physical region for host
    # CPU access. This matches what PCIIfaceBase.alloc does for force_devmem=True buffers.
    self.kiq_ring_size = 0x1000  # 4 KB ring (1024 dwords)
    _ring_mapping = self.adev.mm.valloc(self.kiq_ring_size, uncached=True, contiguous=True)
    self.kiq_ring_va = _ring_mapping.va_addr
    _ring_paddr = _ring_mapping.paddrs[0][0]
    self.kiq_ring_view = self.adev.pci_dev.map_bar(bar=0, off=_ring_paddr, size=self.kiq_ring_size, fmt='I')
    # Allocate the KIQ meta region (rptr_report at offset 0, wptr_poll at offset 8 — both 64-bit)
    _meta_mapping = self.adev.mm.valloc(0x1000, uncached=True, contiguous=True)
    self.kiq_meta_va = _meta_mapping.va_addr
    _meta_paddr = _meta_mapping.paddrs[0][0]
    self.kiq_meta_view = self.adev.pci_dev.map_bar(bar=0, off=_meta_paddr, size=0x1000, fmt='Q')
    self.kiq_rptr_addr = self.kiq_meta_va + 0x00
    self.kiq_wptr_addr = self.kiq_meta_va + 0x08
    print(f"am {self.adev.devfmt}: KIQ ring va=0x{self.kiq_ring_va:x} paddr=0x{_ring_paddr:x}; meta va=0x{self.kiq_meta_va:x} paddr=0x{_meta_paddr:x}")

    # Build the KIQ MQD struct (compute MQD shape, KIQ-specific values)
    struct_t = getattr(am, f"struct_v{self.adev.ip_ver[am.GC_HWIP][0]}_compute_mqd")
    mqd = struct_t(header=0xC0310800,
      compute_pipelinestat_enable=1, compute_misc_reserved=3,
      cp_mqd_base_addr_lo=lo32(self.kiq_mqd_mc), cp_mqd_base_addr_hi=hi32(self.kiq_mqd_mc),
      cp_hqd_pipe_priority=2, cp_hqd_queue_priority=0xf, cp_hqd_quantum=0x111,
      cp_hqd_persistent_state=self.adev.regCP_HQD_PERSISTENT_STATE.encode(preload_size=0x55, preload_req=1),
      # GPUVMA addresses (not MC) — fix for first-attempt failure where MC addresses for the ring
      # didn't translate through vmid 0 and MEC silently couldn't fetch from the ring.
      cp_hqd_pq_base_lo=lo32(self.kiq_ring_va >> 8), cp_hqd_pq_base_hi=hi32(self.kiq_ring_va >> 8),
      cp_hqd_pq_rptr_report_addr_lo=lo32(self.kiq_rptr_addr), cp_hqd_pq_rptr_report_addr_hi=hi32(self.kiq_rptr_addr),
      cp_hqd_pq_wptr_poll_addr_lo=lo32(self.kiq_wptr_addr), cp_hqd_pq_wptr_poll_addr_hi=hi32(self.kiq_wptr_addr),
      cp_hqd_pq_doorbell_control=self.adev.regCP_HQD_PQ_DOORBELL_CONTROL.encode(doorbell_offset=self.kiq_doorbell_idx*2, doorbell_en=1),
      cp_hqd_pq_control=self.adev.regCP_HQD_PQ_CONTROL.encode(rptr_block_size=5, unord_dispatch=0,
        queue_size=(self.kiq_ring_size//4).bit_length()-2),
      cp_hqd_ib_control=self.adev.regCP_HQD_IB_CONTROL.encode(min_ib_avail_size=0x3),
      cp_hqd_hq_status0=0x20004000,
      cp_mqd_control=self.adev.regCP_MQD_CONTROL.encode(priv_state=1),
      cp_hqd_vmid=0, cp_hqd_aql_control=0)
    for se in range(8): setattr(mqd, f'compute_static_thread_mgmt_se{se}', 0xffffffff)
    self.adev.vram.view(self.kiq_mqd_paddr, ctypes.sizeof(mqd))[:] = memoryview(mqd).cast('B')

    # Set RLC_CP_SCHEDULERS.scheduler0 (bits 0:7) to point at the KIQ.
    # Linux's gfx_v10_0_kiq_setting writes (me<<5)|(pipe<<3)|queue|0x80 in scheduler0.
    sched0 = ((self.kiq_me & 0x7) << 5) | ((self.kiq_pipe & 0x3) << 3) | (self.kiq_queue & 0x7) | 0x80
    cur = self.adev.regRLC_CP_SCHEDULERS.read()
    new = (cur & 0xffffff00) | sched0
    self.adev.regRLC_CP_SCHEDULERS.write(new)
    print(f"am {self.adev.devfmt}: KIQ scheduler0 set: pre=0x{cur:08x} new=0x{new:08x} (me={self.kiq_me} pipe={self.kiq_pipe} queue={self.kiq_queue})")

    # Activate KIQ HQD via direct register writes (same path as compute setup_ring)
    self._grbm_select(me=self.kiq_me, pipe=self.kiq_pipe, queue=self.kiq_queue, inst=0)
    mqd_st_mv = to_mv(ctypes.addressof(mqd), ctypes.sizeof(mqd)).cast('I')
    for i, reg in enumerate(range(self.adev.regCP_MQD_BASE_ADDR.addr[0], self.adev.regCP_HQD_PQ_WPTR_HI.addr[0] + 1)):
      self.adev.wreg(reg, mqd_st_mv[0x80 + i])

    # KFD-parity activation tail
    self.adev.regCP_HQD_PQ_DOORBELL_CONTROL.update(doorbell_en=1, doorbell_offset=self.kiq_doorbell_idx*2)
    try: self.adev.regCP_PQ_WPTR_POLL_CNTL1.write(1 << (self.kiq_pipe * 8 + self.kiq_queue))
    except Exception: pass
    try: self.adev.regCP_HQD_EOP_RPTR.update(init_fetcher=1)
    except Exception:
      try: self.adev.regCP_HQD_EOP_RPTR.write(0x80000000)
      except Exception: pass
    self.adev.regCP_HQD_ACTIVE.write(0x1)
    try: self.adev.regCP_PQ_STATUS.update(doorbell_enable=1)
    except Exception: pass
    self.adev.gmc.flush_hdp()
    self._grbm_select(inst=0)
    self.kiq_setup_done = True
    print(f"am {self.adev.devfmt}: KIQ HQD activated (ring va=0x{self.kiq_ring_va:x}, mqd mc=0x{self.kiq_mqd_mc:x}, doorbell idx {self.kiq_doorbell_idx})")

    # Submit a SET_RESOURCES packet to the KIQ as a smoke test that MEC is consuming the KIQ ring.
    # The queue mask covers all possible compute queues (0xffffffff for the low half).
    self._kiq_set_resources(0xffffffff)
    time.sleep(0.1)
    # Read back KIQ state to see if it consumed the packet
    self._grbm_select(me=self.kiq_me, pipe=self.kiq_pipe, queue=self.kiq_queue, inst=0)
    kiq_rptr = self.adev.regCP_HQD_PQ_RPTR.read()
    kiq_wptr = self.adev.regCP_HQD_PQ_WPTR_LO.read()
    kiq_active = self.adev.regCP_HQD_ACTIVE.read()
    self._grbm_select(inst=0)
    print(f"am {self.adev.devfmt}: KIQ post-SET_RESOURCES: ACTIVE=0x{kiq_active:x} RPTR=0x{kiq_rptr:x} WPTR=0x{kiq_wptr:x} (host_wptr_bytes={self.kiq_host_wptr_dws*4})")

  def _kiq_submit_packets(self, dwords):
    """Write PM4 dwords into the KIQ ring buffer and ring the KIQ doorbell.

    Linux convention (per gfx_v10_0_ring_set_wptr_compute, kgd_hqd_load, etc.):
      - host wptr counter is in DWORDS internally
      - wptr poll memory and doorbell value are in BYTES (= dwords << 2)
    First-attempt sent dwords to both, which the GPU treated as bytes — meaning
    only 25% of the actual dword count was published. Fixed here.
    """
    n = len(dwords)
    ring_dws = self.kiq_ring_size // 4
    start = self.kiq_host_wptr_dws % ring_dws
    if start + n > ring_dws:
      raise RuntimeError(f"KIQ ring would wrap (start={start} n={n} ring_dws={ring_dws}); not supported in bootstrap")
    for i, dw in enumerate(dwords):
      self.kiq_ring_view[start + i] = dw
    self.kiq_host_wptr_dws += n
    wptr_bytes = self.kiq_host_wptr_dws * 4
    # Update wptr poll memory in BYTES (host writes via BAR-mapped VRAM at meta + 0x08)
    self.kiq_meta_view[1] = wptr_bytes  # element 1 of the Q-formatted view = offset 8 (wptr slot)
    self.adev.gmc.flush_hdp()
    # Ring the KIQ doorbell in BYTES (BAR2 doorbell aperture, 64-bit write at index*8)
    self.adev.doorbell64.view(self.kiq_doorbell_idx * 8, 8, fmt='Q')[0] = wptr_bytes

  def _kiq_set_resources(self, queue_mask):
    """SET_RESOURCES PM4 packet on the KIQ ring. Linux gfx10_kiq_set_resources, 8 dwords."""
    PACKET3_SET_RESOURCES = 0xA0
    header = (3 << 30) | ((PACKET3_SET_RESOURCES & 0xff) << 8) | ((6 & 0x3fff) << 16)
    dwords = [
      header,
      (0 << 0) | (0 << 29),  # vmid_mask=0, queue_type=0 (KIQ)
      queue_mask & 0xffffffff,
      (queue_mask >> 32) & 0xffffffff,
      0, 0,  # cleaner shader addr lo/hi (none)
      0,     # oac mask
      0,     # gds heap base/size
    ]
    self._kiq_submit_packets(dwords)

  def _kiq_map_queues(self, ring_me, ring_pipe, ring_queue, doorbell_idx, mqd_addr, wptr_addr):
    """MAP_QUEUES PM4 packet on the KIQ ring. Linux gfx10_kiq_map_queues, 7 dwords."""
    PACKET3_MAP_QUEUES = 0xA2
    header = (3 << 30) | ((PACKET3_MAP_QUEUES & 0xff) << 8) | ((5 & 0x3fff) << 16)
    me_field = 0 if ring_me == 1 else 1
    info = ((0 << 4) |     # QUEUE_SEL = 0 (PI mode)
            (0 << 8) |     # VMID = 0
            ((ring_queue & 0x7) << 13) |
            ((ring_pipe & 0x3) << 16) |
            ((me_field & 0x3) << 18) |
            (0 << 21) |    # QUEUE_TYPE = normal compute
            (0 << 24) |    # ALLOC_FORMAT = all_on_one_pipe
            (0 << 26) |    # ENGINE_SEL = compute
            (1 << 29))     # NUM_QUEUES = 1
    dwords = [
      header,
      info,
      doorbell_idx << 2,  # PACKET3_MAP_QUEUES_DOORBELL_OFFSET shift = 2
      mqd_addr & 0xffffffff,
      (mqd_addr >> 32) & 0xffffffff,
      wptr_addr & 0xffffffff,
      (wptr_addr >> 32) & 0xffffffff,
    ]
    self._kiq_submit_packets(dwords)

  def fini_hw(self): self._dequeue_hqds()

  def reset_mec(self):
    self._dequeue_hqds()

    for xcc in range(self.xccs): self.adev.regGRBM_SOFT_RESET.write(soft_reset_cp=1, soft_reset_cpc=1, inst=xcc)
    time.sleep(0.05)
    for xcc in range(self.xccs): self.adev.regGRBM_SOFT_RESET.write(0x0, inst=xcc)

    self._config_mec()
    self._enable_mec()

  def setup_ring(self, ring_addr:int, ring_size:int, rptr_addr:int, wptr_addr:int, eop_addr:int, eop_size:int, idx:int, aql:bool) -> int:
    pipe, queue, doorbell = idx // 4, idx % 4, am.AMDGPU_NAVI10_DOORBELL_MEC_RING0

    # ARCHITECTURAL SHIFT: Linux gfx_v10_0_kcq_init_queue does NOT directly write HQD registers
    # for KCQs. KCQs only get an MQD built in memory, then activated via MAP_QUEUES PM4 packet
    # submitted through a working KIQ ring. The direct-register-write path tinygrad has been
    # using is the KIQ activation pattern applied to a KCQ — wrong for KCQs on gfx10.
    #
    # AMD_KCQ_DIRECT_HQD=1 (debug env): keep the old direct-register-write activation as a
    # fallback for A/B testing while we get the KIQ MAP_QUEUES path working. Default is the
    # MAP_QUEUES path. Setting AMD_KCQ_DIRECT_HQD=1 reverts to the old behavior.
    _kcq_direct_hqd = bool(getenv("AMD_KCQ_DIRECT_HQD", 0))

    # RLC_CP_SCHEDULERS scheduler1 (HIQ slot): KFD's hqd_load_v10_3 has a special case for
    # cp_hqd_vmid==0 queues — it treats them as HIQ (Hardware Interface Queue) and writes
    # RLC_CP_SCHEDULERS.scheduler1 (bits 8:15) with (mec<<5)|(pipe<<3)|queue|0x80. The scheduler1
    # slot is the boot-time interface queue that MEC processes unconditionally, without needing
    # KIQ MAP_QUEUES. We were previously writing scheduler0 (bits 0:7), which is the wrong slot.
    # KFD source: drivers/gpu/drm/amd/amdgpu/amdgpu_amdkfd_gfx_v10_3.c::hqd_load_v10_3 line 195-205
    #   if (m->cp_hqd_vmid == 0) {
    #     value = REG_SET_FIELD(value, RLC_CP_SCHEDULERS, scheduler1,
    #             ((mec << 5) | (pipe << 3) | queue_id | 0x80));
    #     WREG32_SOC15(GC, 0, mmRLC_CP_SCHEDULERS, value);
    #   }
    # Our compute queue is at me=1, pipe=0, queue=0, vmid=0 → scheduler1 byte = 0xa0.
    if self.adev.ip_ver[am.GC_HWIP][0] >= 10 and not aql:
      try:
        # KFD's mec computation: mec = (pipe_id / num_pipe_per_mec) + 1.
        # For our compute queue (pipe_id=0, num_pipe_per_mec=4): mec = 1.
        # Note: tinygrad's `pipe` variable here is the local pipe (idx//4), not pipe_id.
        # me_field = mec from KFD = our self.me (=1 for compute).
        _mec_field = 1  # we always use me=1 for compute queues
        _sched = ((_mec_field & 0x7) << 5) | ((pipe & 0x3) << 3) | (queue & 0x7) | 0x80
        _cur = self.adev.regRLC_CP_SCHEDULERS.read()
        # Set bits 8:15 (scheduler1), preserving the rest. KFD uses REG_SET_FIELD.
        _new = (_cur & ~0x0000ff00) | (_sched << 8)
        self.adev.regRLC_CP_SCHEDULERS.write(_new)
        _rb = self.adev.regRLC_CP_SCHEDULERS.read()
        print(f"am {self.adev.devfmt}: RLC_CP_SCHEDULERS scheduler1 pre=0x{_cur:08x} write=0x{_new:08x} readback=0x{_rb:08x} (sched1 byte=0x{_sched:02x})")
      except Exception as e: print(f"am {self.adev.devfmt}: RLC_CP_SCHEDULERS write failed: {e}")

    for xcc in range(self.xccs if aql else 1):
      self._grbm_select(me=1, pipe=pipe, queue=queue, inst=xcc)

      struct_t = getattr(am, f"struct_v{self.adev.ip_ver[am.GC_HWIP][0]}{'_compute' if self.adev.ip_ver[am.GC_HWIP][0] >= 10 else ''}_mqd")
      # compute_pipelinestat_enable=1 and compute_misc_reserved=0x3 — Linux's gfx_v10_0_compute_mqd_init
      # and KFD's init_mqd both set these on every compute MQD. tinygrad was leaving them at 0,
      # which may be why MEC's pipeline-stat / scheduling logic ignores the queue. Adding to match
      # upstream parity.
      mqd_struct = struct_t(header=0xC0310800,
        compute_pipelinestat_enable=0x00000001, compute_misc_reserved=0x00000003,
        cp_mqd_base_addr_lo=lo32(self.mqd_mc[queue] + 0x1000*xcc),
        cp_mqd_base_addr_hi=hi32(self.mqd_mc[queue] + 0x1000*xcc), cp_hqd_pipe_priority=0x2, cp_hqd_queue_priority=0xf, cp_hqd_quantum=0x111,
        # preload_req=1 matches KFD's kfd_mqd_manager_v10.c::init_mqd line 108:
        # m->cp_hqd_persistent_state = PRELOAD_REQ_MASK | 0x53 << PRELOAD_SIZE_SHIFT
        # We had bisected this to 0 earlier suspecting CSA dereference faults; the actual fix is
        # CP_PQ_WPTR_POLL_CNTL1 + CP_HQD_EOP_RPTR.INIT_FETCHER (see end of this method).
        cp_hqd_persistent_state=self.adev.regCP_HQD_PERSISTENT_STATE.encode(preload_size=0x55, preload_req=1),
        cp_hqd_pq_base_lo=lo32(ring_addr>>8), cp_hqd_pq_base_hi=hi32(ring_addr>>8),
        cp_hqd_pq_rptr_report_addr_lo=lo32(rptr_addr), cp_hqd_pq_rptr_report_addr_hi=hi32(rptr_addr),
        cp_hqd_pq_wptr_poll_addr_lo=lo32(wptr_addr), cp_hqd_pq_wptr_poll_addr_hi=hi32(wptr_addr),
        cp_hqd_pq_doorbell_control=self.adev.regCP_HQD_PQ_DOORBELL_CONTROL.encode(doorbell_offset=doorbell*2, doorbell_en=1),
        cp_hqd_pq_control=self.adev.regCP_HQD_PQ_CONTROL.encode(rptr_block_size=5, unord_dispatch=0, queue_size=(ring_size//4).bit_length()-2,
          **({'queue_full_en':1, 'slot_based_wptr':2, 'no_update_rptr':xcc!=0 or self.xccs==1} if aql else {})),
        cp_hqd_ib_control=self.adev.regCP_HQD_IB_CONTROL.encode(min_ib_avail_size=0x3), cp_hqd_hq_status0=0x20004000,
        cp_mqd_control=self.adev.regCP_MQD_CONTROL.encode(priv_state=1), cp_hqd_vmid=0, cp_hqd_aql_control=int(aql),
        cp_hqd_eop_base_addr_lo=lo32(eop_addr>>8), cp_hqd_eop_base_addr_hi=hi32(eop_addr>>8),
        cp_hqd_eop_control=self.adev.regCP_HQD_EOP_CONTROL.encode(eop_size=(eop_size//4).bit_length()-2),
        **({'compute_tg_chunk_size':1, 'compute_current_logic_xcc_id':xcc, 'cp_mqd_stride_size':0x1000} if aql and self.xccs > 1 else {}))
      for se in range(8 if self.adev.ip_ver[am.GC_HWIP][0] >= 10 else 4): setattr(mqd_struct, f'compute_static_thread_mgmt_se{se}', 0xffffffff)

      self.adev.vram.view(self.mqd_paddr[queue] + 0x1000*xcc, ctypes.sizeof(mqd_struct))[:] = memoryview(mqd_struct).cast('B')

      if _kcq_direct_hqd:
        # OLD direct-HQD activation path (debug fallback). Linux's gfx_v10_0_kcq_init_queue does
        # NOT do this for KCQs — it only builds the MQD in memory and lets MEC firmware load it
        # via MAP_QUEUES from the KIQ. This block is the KIQ activation pattern (kiq_init_register)
        # applied to a KCQ, which is wrong but kept here for A/B comparison.
        mqd_st_mv = to_mv(ctypes.addressof(mqd_struct), ctypes.sizeof(mqd_struct)).cast('I')
        for i, reg in enumerate(range(self.adev.regCP_MQD_BASE_ADDR.addr[xcc], self.adev.regCP_HQD_PQ_WPTR_HI.addr[xcc] + 1)):
          self.adev.wreg(reg, mqd_st_mv[0x80 + i])

        # KFD HQD-direct activation tail — matching kgd_hqd_load() in amdgpu_amdkfd_gfx_v10.c.
        self.adev.regCP_HQD_PQ_DOORBELL_CONTROL.update(doorbell_en=1, doorbell_offset=doorbell*2, inst=xcc)
        _queue_mask = 1 << (pipe * 8 + queue)
        try:
          self.adev.regCP_PQ_WPTR_POLL_CNTL1.write(_queue_mask, inst=xcc)
        except Exception as e: print(f"am {self.adev.devfmt}: CP_PQ_WPTR_POLL_CNTL1 write failed: {e}")
        try: self.adev.regCP_HQD_EOP_RPTR.update(init_fetcher=1, inst=xcc)
        except Exception:
          try: self.adev.regCP_HQD_EOP_RPTR.write(0x80000000, inst=xcc)
          except Exception as e2: print(f"am {self.adev.devfmt}: CP_HQD_EOP_RPTR raw write failed: {e2}")
        self.adev.regCP_HQD_ACTIVE.write(0x1, inst=xcc)
        try: self.adev.regCP_PQ_STATUS.update(doorbell_enable=1, inst=xcc)
        except Exception:
          try:
            _cur = self.adev.regCP_PQ_STATUS.read(inst=xcc)
            self.adev.regCP_PQ_STATUS.write(_cur | 0x2, inst=xcc)
          except Exception as e2: print(f"am {self.adev.devfmt}: CP_PQ_STATUS write failed: {e2}")
        print(f"am {self.adev.devfmt}: KCQ activated via DIRECT-HQD path (debug fallback, AMD_KCQ_DIRECT_HQD=1)")
      else:
        # DEFAULT MAP_QUEUES path — matches Linux gfx_v10_0_kcq_init_queue: build MQD only,
        # let MEC firmware load it via MAP_QUEUES PM4 packet through the KIQ ring (issued
        # below, after this loop).
        print(f"am {self.adev.devfmt}: KCQ MQD built; activation deferred to KIQ MAP_QUEUES")

      self.adev.gmc.flush_hdp()
      self._grbm_select(inst=xcc)

    # KIQ MAP_QUEUES bootstrap: if the KIQ is alive, submit a MAP_QUEUES PM4 packet for our compute
    # queue. This tells MEC's scheduler to start dispatching this queue. Without this, MEC has the
    # HQD active but never picks it up because no SET_RESOURCES/MAP_QUEUES sequence ever ran.
    if getattr(self, 'kiq_setup_done', False) and not aql:
      try:
        self._kiq_map_queues(ring_me=1, ring_pipe=pipe, ring_queue=queue,
                             doorbell_idx=doorbell, mqd_addr=self.mqd_mc[queue],
                             wptr_addr=wptr_addr)
        time.sleep(0.05)
        # Re-read RPTR/ACTIVE for the compute queue under the per-pipe context
        self._grbm_select(me=1, pipe=pipe, queue=queue, inst=0)
        rptr_after = self.adev.regCP_HQD_PQ_RPTR.read()
        active_after = self.adev.regCP_HQD_ACTIVE.read()
        self._grbm_select(inst=0)
        print(f"am {self.adev.devfmt}: post-MAP_QUEUES compute queue: ACTIVE=0x{active_after:x} RPTR=0x{rptr_after:x} (host_kiq_wptr_bytes={self.kiq_host_wptr_dws*4})")
      except Exception as e: print(f"am {self.adev.devfmt}: KIQ map_queues failed: {e}")
    return doorbell

  def set_clockgating_state(self):
    if hasattr(self.adev, 'regMM_ATC_L2_MISC_CG'): self.adev.regMM_ATC_L2_MISC_CG.write(enable=1, mem_ls_enable=1)

    for xcc in range(self.xccs):
      self.adev.regRLC_SAFE_MODE.write(message=1, cmd=1, inst=xcc)
      wait_cond(lambda: self.adev.regRLC_SAFE_MODE.read(inst=xcc) & 0x1, value=0, msg="RLC safe mode timeout")

      self.adev.regRLC_CGCG_CGLS_CTRL.update(cgcg_gfx_idle_threshold=0x36, cgcg_en=1, cgls_rep_compansat_delay=0xf, cgls_en=1, inst=xcc)

      self.adev.regCP_RB_WPTR_POLL_CNTL.update(poll_frequency=0x100, idle_poll_count=0x90, inst=xcc)
      self.adev.regCP_INT_CNTL.update(cntx_busy_int_enable=1, cntx_empty_int_enable=1, cmp_busy_int_enable=1, inst=xcc)
      if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0):
        self.adev.regSDMA0_RLC_CGCG_CTRL.update(cgcg_int_enable=1, inst=xcc)
        self.adev.regSDMA1_RLC_CGCG_CTRL.update(cgcg_int_enable=1, inst=xcc)

      feats_gfx9 = {'gfxip_mgls_override':0, 'gfxip_rep_fgcg_override':0} if self.adev.ip_ver[am.GC_HWIP][0] == 9 else {}
      feats_gfx11 = {'perfmon_clock_state':1, 'gfxip_repeater_fgcg_override':0} if self.adev.ip_ver[am.GC_HWIP][0] >= 11 else {}
      self.adev.regRLC_CGTT_MGCG_OVERRIDE.update(**feats_gfx9, **feats_gfx11, gfxip_fgcg_override=0, grbm_cgtt_sclk_override=0,
        rlc_cgtt_sclk_override=0, gfxip_mgcg_override=0, gfxip_cgls_override=0, gfxip_cgcg_override=0, inst=xcc)

      self.adev.regRLC_SAFE_MODE.write(message=0, cmd=1, inst=xcc)

  def _grbm_select(self, me=0, pipe=0, queue=0, vmid=0, inst=0):
    self.adev.regGRBM_GFX_CNTL.write(meid=me, pipeid=pipe, vmid=vmid, queueid=queue, inst=inst)

  def _enable_mec(self):
    for xcc in range(self.xccs):
      # RS64 microcode + the MEC_RS64 register family was introduced in GFX11. RDNA2 / GFX10
      # uses the legacy MEC microcode and clears the halt bits via regCP_MEC_CNTL.
      if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0): self.adev.regCP_MEC_RS64_CNTL.update(mec_pipe0_reset=0, mec_pipe0_active=1, mec_halt=0, inst=xcc)
      else: self.adev.regCP_MEC_CNTL.write(0x0, inst=xcc)
    time.sleep(0.05)  # Wait for MEC to be ready

  def _config_mec(self):
    def _config_helper(eng_name, cntl_reg, eng_reg, pipe_cnt, me=0, xcc=0):
      for pipe in range(pipe_cnt):
        self._grbm_select(me=me, pipe=pipe, inst=xcc)
        self.adev.wreg_pair(f"regCP_{eng_reg}_PRGRM_CNTR_START", "", "_HI", self.adev.fw.ucode_start[eng_name] >> 2, inst=xcc)
      self._grbm_select(inst=xcc)
      self.adev.reg(f"regCP_{cntl_reg}_CNTL").update(**{f"{eng_name.lower()}_pipe{pipe}_reset": 1 for pipe in range(pipe_cnt)}, inst=xcc)
      self.adev.reg(f"regCP_{cntl_reg}_CNTL").update(**{f"{eng_name.lower()}_pipe{pipe}_reset": 0 for pipe in range(pipe_cnt)}, inst=xcc)

    for xcc in range(self.adev.gfx.xccs):
      # Legacy MEC halt path covers everything before RS64 (GFX11+): vega/arcturus AND RDNA2 (GFX10).
      if self.adev.ip_ver[am.GC_HWIP] < (11,0,0):
        self.adev.regCP_MEC_CNTL.update(mec_invalidate_icache=1, mec_me1_pipe0_reset=1, mec_me2_pipe0_reset=1, mec_me1_halt=1,mec_me2_halt=1,inst=xcc)
      if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0):
        _config_helper(eng_name="PFP", cntl_reg="ME", eng_reg="PFP", pipe_cnt=1, xcc=xcc)
        _config_helper(eng_name="ME", cntl_reg="ME", eng_reg="ME", pipe_cnt=1, xcc=xcc)
      # RS64 MEC config (regCP_MEC_RS64_*, ucode_start['MEC']) is GFX11+ only. RDNA2 doesn't program
      # PRGRM_CNTR_START — the MEC microcode loaded by PSP starts at a fixed address.
      if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0):
        _config_helper(eng_name="MEC", cntl_reg="MEC_RS64", eng_reg="MEC_RS64", pipe_cnt=1, me=1, xcc=xcc)

  def _dequeue_hqds(self):
    for q in range(2):
      for xcc in range(self.xccs):
        self._grbm_select(me=1, pipe=0, queue=q, inst=xcc)
        if self.adev.regCP_HQD_ACTIVE.read(inst=xcc) & 1:
          self.adev.regCP_HQD_DEQUEUE_REQUEST.write(0x2, inst=xcc) # 1 - DRAIN_PIPE; 2 - RESET_WAVES
          if not self.adev.is_err_state: wait_cond(lambda: self.adev.regCP_HQD_ACTIVE.read(inst=xcc) & 1, value=0, msg="HQD dequeue timeout")
    self._grbm_select()

class AM_IH(AM_IP):
  def init_sw(self):
    self.ring_size = 256 << 10
    def _alloc_ring(size): return (self.adev.mm.palloc(size, zero=False, boot=True), self.adev.mm.palloc(0x1000, zero=False, boot=True))
    self.rings = [(*_alloc_ring(self.ring_size), "", 0), (*_alloc_ring(self.ring_size), "_RING1", 1)]
    self.ring_view = self.adev.vram.view(offset=self.rings[0][0], size=self.ring_size, fmt='I')

  def init_hw(self):
    for ring_vm, rwptr_vm, suf, ring_id in self.rings:
      self.adev.wreg_pair("regIH_RB_BASE", suf, f"_HI{suf}", self.adev.paddr2mc(ring_vm) >> 8)

      self.adev.reg(f"regIH_RB_CNTL{suf}").write(mc_space=4, wptr_overflow_clear=1, rb_size=((self.ring_size//4)-1).bit_length(),
        mc_snoop=1, mc_ro=0, mc_vmid=0, **({'wptr_overflow_enable': 1, 'rptr_rearm': 1} if ring_id == 0 else {'rb_full_drain_enable': 1}))

      if ring_id == 0: self.adev.wreg_pair("regIH_RB_WPTR_ADDR", "_LO", "_HI", self.adev.paddr2mc(rwptr_vm))

      self.adev.reg(f"regIH_RB_WPTR{suf}").write(0)
      self.adev.reg(f"regIH_RB_RPTR{suf}").write(0)

      self.adev.reg(f"regIH_DOORBELL_RPTR{suf}").write(enable=0)

    if self.adev.ip_ver[am.OSSSYS_HWIP] != (4,4,2):
      self.adev.regIH_STORM_CLIENT_LIST_CNTL.update(client18_is_storm_client=1)
      self.adev.regIH_INT_FLOOD_CNTL.update(flood_cntl_enable=1)
      if 'regIH_MSI_STORM_CTRL' in self.adev.__dict__: self.adev.regIH_MSI_STORM_CTRL.update(delay=3)  # not present on RDNA2 OSSSYS 5.0.0

    # toggle interrupts
    for _, rwptr_vm, suf, ring_id in self.rings:
      self.adev.reg(f"regIH_RB_CNTL{suf}").update(rb_enable=1, **({'enable_intr': 1} if ring_id == 0 else {}))

  def drain(self):
    _, _, suf, _ = self.rings[0]
    wptr = self.adev.reg(f"regIH_RB_WPTR{suf}").read_bitfields()
    self.adev.regIH_RB_RPTR.write(wptr['offset'] % (self.ring_size // 4))

    if wptr['rb_overflow']:
      self.adev.reg(f"regIH_RB_WPTR{suf}").update(rb_overflow=0)
      self.adev.reg(f"regIH_RB_CNTL{suf}").update(wptr_overflow_clear=1)
      self.adev.reg(f"regIH_RB_CNTL{suf}").update(wptr_overflow_clear=0)

  def interrupt_handler(self):
    _, _, suf, _ = self.rings[0]
    wptr = self.adev.reg(f"regIH_RB_WPTR{suf}").read_bitfields()
    rptr = self.adev.regIH_RB_RPTR.read()

    while rptr != wptr['offset']:
      entry = [self.ring_view[(rptr + i) % (self.ring_size // 4)] for i in range(8)]
      rptr = (rptr + 8) % (self.ring_size // 4)

      client, src, ring_id, vmid, vmid_type, pasid, node = \
        [getattr(am, f'SOC15_{n}_FROM_IH_ENTRY')(entry) for n in ['CLIENT_ID', 'SOURCE_ID', 'RING_ID', 'VMID', 'VMID_TYPE', 'PASID', 'NODEID']]
      ctx = [getattr(am, f'SOC15_CONTEXT_ID{i}_FROM_IH_ENTRY')(entry) for i in range(4)]

      src_name = self.adev.soc.ih_srcs_names.get(client, {}).get(src, '')
      if src_name in {"SDMA_TRAP", "CP_EOP_INTR"}: continue

      print(f"am {self.adev.devfmt}: IH ({rptr:#x}/{wptr['offset']:#x}) client={self.adev.soc.ih_clients.get(client)} src={src_name}({src}) "
            f"ring={ring_id} vmid={vmid}({vmid_type}) pasid={pasid} node={node} ctx=[{ctx[0]:#x}, {ctx[1]:#x}, {ctx[2]:#x}, {ctx[3]:#x}]")

      if src_name == "SQ_INTERRUPT_ID":
        enc_type = getbits(ctx[1], 6, 7) if (is_soc21:=self.adev.ip_ver[am.GC_HWIP][0] >= 11) else getbits(ctx[0], 26, 27)
        err_type = getbits(ctx[0], 21, 24) if is_soc21 else getbits((ctx[0] & 0xfff) | ((ctx[0]>>16) & 0xf000) | ((ctx[1]<<16) & 0xff0000), 20, 23)
        err_info = f" ({['EDC_FUE', 'ILLEGAL_INST', 'MEMVIOL', 'EDC_FED'][err_type]})" if enc_type == 2 else ""
        print(f"am {self.adev.devfmt}: sq_intr: {['auto', 'wave', 'error'][enc_type]}{err_info}")
        self.adev.is_err_state |= enc_type == 2
      elif src_name == "UTCL2_FAULT" or (self.adev.ip_ver[am.GC_HWIP][0] == 9 and client == am.SOC15_IH_CLIENTID_UTCL2):
        bf = self.adev.reg(self.adev.gmc.pf_status_reg('GC')).read_bitfields()
        va = (self.adev.reg('regGCVM_L2_PROTECTION_FAULT_ADDR_HI32').read()<<32) | self.adev.reg('regGCVM_L2_PROTECTION_FAULT_ADDR_LO32').read()
        print(f"am {self.adev.devfmt}: GCVM_L2_PROTECTION_FAULT_STATUS: {bf} {va<<12:#x}")
        self.adev.reg('regGCVM_L2_PROTECTION_FAULT_CNTL').update(clear_protection_fault_status_addr=1)
        self.adev.is_err_state = True
      else: self.adev.is_err_state = True

    self.drain()

    bif_intr = self.adev.regBIF_BX0_BIF_DOORBELL_INT_CNTL.read_bitfields()
    athub_err, cntlr_err = bif_intr['ras_athub_err_event_interrupt_status'], bif_intr['ras_cntlr_interrupt_status']
    if athub_err or cntlr_err:
      print(f"am {self.adev.devfmt}: fatal hardware error detected: {'RAS_ATHUB_ERR_EVENT ' if athub_err else ''}{'RAS_CNTLR' if cntlr_err else ''}")

      acas = self.adev.smu._aca_read_banks(ue=True) + self.adev.smu._aca_read_banks(ue=False)
      for regs in acas:
        acatyp = 'Uncorrectable' if (regs[1] >> 61) & 1 and (regs[1] >> 57) & 1 else 'Correctable'
        hwname = f'{self.adev.hwid_names.get((regs[5] >> 32) & 0xFFF, "")} ({(regs[5] >> 32) & 0xFFF:#03x})'
        print(f"am {self.adev.devfmt}: {acatyp} ACA: {hwname} mcatype={(regs[5] >> 48) & 0xFFFF:#06x} regs=[{', '.join(f'{r:#x}' for r in regs)}]")

      self.adev.regBIF_BX0_BIF_DOORBELL_INT_CNTL.write(ras_cntlr_interrupt_clear=cntlr_err, ras_athub_err_event_interrupt_clear=athub_err)
      self.adev.is_err_state = True

class AM_SDMA(AM_IP):
  def init_sw(self): self.sdma_reginst, self.sdma_name = [], "F32" if self.adev.ip_ver[am.SDMA0_HWIP] < (7,0,0) else "MCU"
  def init_hw(self):
    # RDNA2 (sdma_v5_2) soft-reset pulse. Linux's sdma_v5_2_start() begins with
    # sdma_v5_2_soft_reset() before enable(HALT=0) / ctx_switch_enable / gfx_resume. It pulses
    # GRBM_SOFT_RESET.SOFT_RESET_SDMA0 (bit 23, mask 0x00800000) with a udelay(50) on each side,
    # to bring the engine out of whatever state the previous owner (boot ROM / prior driver / PSP)
    # left it in. Must run BEFORE the UTCL1/F32/SDMA0_CNTL init below — post-init pulses corrupt
    # the state we just programmed and regress to a walker fault. Only valid remaining test of
    # the Linux start-order branch for the stuck-RPTR / CTXSW_ABLE=0 condition on Navi 21.
    if self.adev.ip_ver[am.SDMA0_HWIP][0] == 5 and self.adev.ip_ver[am.SDMA0_HWIP] <= (5,2,0):
      _SOFT_RESET_SDMA0_BIT = 1 << 23
      _gsr_before = self.adev.regGRBM_SOFT_RESET.read()
      self.adev.regGRBM_SOFT_RESET.write(_gsr_before | _SOFT_RESET_SDMA0_BIT)
      self.adev.regGRBM_SOFT_RESET.read()  # post the write
      time.sleep(50e-6)
      self.adev.regGRBM_SOFT_RESET.write(_gsr_before & ~_SOFT_RESET_SDMA0_BIT)
      self.adev.regGRBM_SOFT_RESET.read()
      time.sleep(50e-6)

    # Linux-order parity: for RDNA2 sdma_v5_2, all of UTCL1_CNTL, UTCL1_PAGE, F32_CNTL, and
    # SDMA0_CNTL (UTC_L1_ENABLE, MIDCMD_PREEMPT_ENABLE, AUTO_CTXSW_ENABLE) are programmed
    # inside sdma_v5_2_gfx_resume_instance AFTER queue/ring register programming, not during
    # ip_block init. tinygrad previously landed them here in init_hw, which produces the
    # parser-to-dispatch stall (RB_RPTR_FETCH advances, PACKET_READY=1, but CMD_OP stays 0
    # and CTXSW_READY never asserts). This flag pushes them out of init_hw for v5.2 and into
    # setup_ring so they land in Linux order just before RB_ENABLE / IB_ENABLE.
    _is_sdma_v52 = self.adev.ip_ver[am.SDMA0_HWIP][0] == 5 and self.adev.ip_ver[am.SDMA0_HWIP] <= (5,2,0)

    for pipe_id in range(16 if self.adev.ip_ver[am.SDMA0_HWIP] < (5,0,0) else 1):
      pipe, inst = ("", pipe_id) if self.adev.ip_ver[am.SDMA0_HWIP] < (5,0,0) else (str(pipe_id), 0)

      if self.adev.ip_ver[am.SDMA0_HWIP] >= (6,0,0):
        self.adev.reg(f"regSDMA{pipe}_WATCHDOG_CNTL").update(queue_hang_count=100, inst=inst) # 10s, 100ms per unit
        self.adev.reg(f"regSDMA{pipe}_UTCL1_CNTL").update(resp_mode=3, redo_delay=9, inst=inst)

        # rd=noa, wr=bypass
        self.adev.reg(f"regSDMA{pipe}_UTCL1_PAGE").update(rd_l2_policy=2, wr_l2_policy=3, **({'llc_noalloc':1} if self.sdma_name == "F32" else {}),
                                                          inst=inst)
        self.adev.reg(f"regSDMA{pipe}_{self.sdma_name}_CNTL").update(halt=0, **{f"{'th1_' if self.sdma_name == 'F32' else ''}reset":0}, inst=inst)
      elif not _is_sdma_v52:
        # SDMA < 6.0.0 AND NOT v5.2 (e.g., v4.x) — keep the prior tinygrad init ordering.
        # v5.2 falls through to setup_ring for Linux-order parity.
        self.adev.reg(f"regSDMA{pipe}_UTCL1_CNTL").update(resp_mode=3, redo_delay=9, inst=inst)
        self.adev.reg(f"regSDMA{pipe}_UTCL1_PAGE").update(rd_l2_policy=2, wr_l2_policy=3, llc_noalloc=1, inst=inst)
        self.adev.reg(f"regSDMA{pipe}_F32_CNTL").update(halt=0, reset=0, inst=inst)

      if not _is_sdma_v52:
        # v5.2 SDMA0_CNTL programming also deferred to setup_ring below.
        self.adev.reg(f"regSDMA{pipe}_CNTL").update(trap_enable=1, inst=inst)

    if self.adev.ip_ver[am.NBIO_HWIP] in {(7,9,0), (7,9,1)}:
      for aid_id in range(4):
        for dev_inst, (port, awid, offset, awaddr) in enumerate([(1, 0xe, 0xe, 0x1), (2, 0x8, 0x8, 0x2), (5, 0x9, 0x9, 0x8), (6, 0xa, 0xa, 0x9)]):
          entry = dev_inst + 1 + 4 * aid_id
          self.adev.reg(f"regDOORBELL0_CTRL_ENTRY_{entry}").write(**{f"bif_doorbell{entry}_range_size_entry": 20,
            f"bif_doorbell{entry}_range_offset_entry": (am.AMDGPU_NAVI10_DOORBELL_sDMA_ENGINE0 + (entry - 1) * 0xA) * 2})
          self.adev.soc.doorbell_enable(port=port, awid=awid, awaddr_31_28_value=awaddr, offset=offset, size=4, aid=aid_id)
    elif self.adev.ip_ver[am.NBIO_HWIP][0] >= 4:
      # S2A doorbell routing requires NBIO 4.x+. RDNA2 NBIO 2.x uses the BIF aperture (set up in AM_NBIO.init_hw)
      # and per-engine SDMA*_DOORBELL registers programmed by setup_ring — no S2A routing needed here.
      self.adev.soc.doorbell_enable(port=2, awid=0xe, awaddr_31_28_value=0x3, offset=am.AMDGPU_NAVI10_DOORBELL_sDMA_ENGINE0*2, size=4)

  def fini_hw(self):
    for reg, inst in self.sdma_reginst:
      self.adev.reg(f"{reg}_RB_CNTL").update(rb_enable=0, inst=inst)
      self.adev.reg(f"{reg}_IB_CNTL").update(ib_enable=0, inst=inst)
      self.adev.reg(f"{reg}_DOORBELL").update(enable=0, inst=inst)
      self.adev.reg(f"{reg}_DOORBELL_OFFSET").update(offset=0, inst=inst)

    if self.adev.ip_ver[am.SDMA0_HWIP] >= (6,0,0):
      self.adev.regGRBM_SOFT_RESET.write(soft_reset_sdma0=1)
      time.sleep(0.01)
      self.adev.regGRBM_SOFT_RESET.write(0x0)

  def setup_ring(self, ring_addr:int, ring_size:int, rptr_addr:int, wptr_addr:int, idx:int) -> int:
    if self.adev.ip_ver[am.SDMA0_HWIP] >= (5,0,0) and idx > 0: raise RuntimeError(f"am {self.adev.devfmt}: sdma queue {idx} is not available")

    pipe, queue = idx // 4, idx % 4
    if self.adev.ip_ver[am.SDMA0_HWIP][:2] == (4,4):
      reg, inst = ("regSDMA_GFX", pipe+queue*4)
    elif self.adev.ip_ver[am.SDMA0_HWIP][0] == 5:
      # RDNA2 (sdma_v5_2 on Sienna Cichlid): per-queue-TYPE register naming. SDMA{pipe}_GFX_* is
      # the graphics queue, used for general SDMA work. RDNA3+ unified everything into
      # SDMA{pipe}_QUEUE{queue}_*. The idx>0 guard above already restricts us to a single queue
      # per engine on SDMA 5.x, so we only need the GFX form here.
      reg, inst = (f"regSDMA{pipe}_GFX", 0)
    else:
      reg, inst = (f"regSDMA{pipe}_QUEUE{queue}", 0)
    doorbell = am.AMDGPU_NAVI10_DOORBELL_sDMA_ENGINE0 + (pipe+queue*4) * 0xA
    self.sdma_reginst.append((reg, inst))

    # SEM_WAIT_FAIL_TIMER_CNTL: Linux's sdma_v5_2_gfx_resume_instance unconditionally clears this
    # before any other ring programming, to prevent semaphore-wait-fail timeouts from hanging the
    # engine on the first packet. Field-name update would require knowing the field layout; the
    # whole register is set to 0, so a raw write is fine.
    if self.adev.ip_ver[am.SDMA0_HWIP][0] == 5:
      # SEM_WAIT_FAIL_TIMER_CNTL is per-engine (not per-queue-type), so the register name has no
      # _GFX_ infix — use regSDMA{pipe}_SEM_WAIT_FAIL_TIMER_CNTL, not {reg}_SEM_WAIT_FAIL_TIMER_CNTL
      # which would expand to regSDMA0_GFX_SEM_WAIT_FAIL_TIMER_CNTL.
      try: self.adev.reg(f"regSDMA{pipe}_SEM_WAIT_FAIL_TIMER_CNTL").write(0x0, inst=inst)
      except Exception as e: print(f"am {self.adev.devfmt}: SEM_WAIT_FAIL_TIMER_CNTL clear failed: {e}")

    self.adev.reg(f"{reg}_MINOR_PTR_UPDATE").write(0x1, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_RPTR", "", "_HI", 0, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_WPTR", "", "_HI", 0, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_BASE", "", "_HI", ring_addr >> 8, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_RPTR_ADDR", "_LO", "_HI", rptr_addr, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_WPTR_POLL_ADDR", "_LO", "_HI", wptr_addr, inst=inst)
    # WPTR_POLL_CNTL has TWO enable bits and BOTH must be set on sdma_v5_2:
    #   - `enable` (bit 0): MASTER poll subsystem enable. Without it, the polling machinery
    #     is dormant — STATUS_REG.WPTR_POLLING stays 0 and wptr_gpu_addr writes do nothing.
    #   - `f32_poll_enable` (bit 2): the F32-side poll arming, separate from the master bit.
    # Tinygrad previously only set f32_poll_enable, leaving the master bit clear, which made
    # the poll path silently dead. Verified empirically: setting enable=1 transitions
    # STATUS_REG from IDLE/RB_EMPTY to actively fetching memory and the engine starts
    # ingesting wptr_gpu_addr writes for the first time. Drop the try/except — silent
    # exception swallowing is what hid this bug for two days. v6+ moved both bits into
    # RB_CNTL itself, which is why the previous gate skipped v5.x.
    if self.adev.ip_ver[am.SDMA0_HWIP][0] == 5:
      self.adev.reg(f"{reg}_RB_WPTR_POLL_CNTL").update(enable=1, f32_poll_enable=1, inst=inst)
    self.adev.reg(f"{reg}_DOORBELL_OFFSET").update(offset=doorbell * 2, inst=inst)
    self.adev.reg(f"{reg}_DOORBELL").update(enable=1, inst=inst)
    # Second RB_WPTR/HI write while MINOR_PTR_UPDATE=1: Linux's sdma_v5_2_gfx_resume_instance
    # explicitly stamps RB_WPTR (with ring->wptr<<2, typically 0) inside the MINOR_PTR_UPDATE=1
    # window before clearing it. This is the commit-arming sequence — without writing the wptr
    # while MINOR_PTR_UPDATE is set, the wptr commit machinery stays armed-but-unfired and the
    # engine never picks up subsequent wptr changes.
    self.adev.wreg_pair(f"{reg}_RB_WPTR", "", "_HI", 0, inst=inst)
    self.adev.reg(f"{reg}_MINOR_PTR_UPDATE").write(0x0, inst=inst)
    # RDNA2 sdma_v5_2 Linux-order parity block: Linux's sdma_v5_2_gfx_resume_instance runs the
    # following writes AFTER queue/ring programming and BEFORE RB_ENABLE / IB_ENABLE:
    #   SDMA0_CNTL (UTC_L1_ENABLE | MIDCMD_PREEMPT_ENABLE | AUTO_CTXSW_ENABLE | TRAP_ENABLE)
    #   UTCL1_CNTL (resp_mode=3, redo_delay=9)
    #   UTCL1_PAGE (rd_l2_policy=2, wr_l2_policy=3, llc_noalloc=1)
    #   F32_CNTL.HALT=0, RESET=0
    # tinygrad previously programmed these in AM_SDMA.init_hw, which made the queue reach
    # parser-to-dispatch but never retire. Moving them here so they land in Linux order.
    if self.adev.ip_ver[am.SDMA0_HWIP][0] == 5 and self.adev.ip_ver[am.SDMA0_HWIP] <= (5,2,0):
      self.adev.reg(f"regSDMA{pipe}_CNTL").update(trap_enable=1, utc_l1_enable=1, midcmd_preempt_enable=1, auto_ctxsw_enable=1, inst=inst)
      self.adev.reg(f"regSDMA{pipe}_UTCL1_CNTL").update(resp_mode=3, redo_delay=9, inst=inst)
      self.adev.reg(f"regSDMA{pipe}_UTCL1_PAGE").update(rd_l2_policy=2, wr_l2_policy=3, llc_noalloc=1, inst=inst)
      self.adev.reg(f"regSDMA{pipe}_F32_CNTL").update(halt=0, reset=0, inst=inst)

    # RB_CNTL pre-write log (diagnostic only, cheap): Linux preserves the existing RB_CNTL
    # value and only sets RB_SIZE, RPTR_WRITEBACK_ENABLE, RB_ENABLE. tinygrad force-writes
    # rb_priv=1, rb_vmid=0, rptr_writeback_timer=4 in addition. Capture the pre-write value
    # so diagnostic runs can see exactly what's being replaced.
    if self.adev.ip_ver[am.SDMA0_HWIP][0] == 5:
      try:
        _rb_cntl_pre = self.adev.reg(f"{reg}_RB_CNTL").read(inst=inst)
        print(f"am {self.adev.devfmt}: {reg}_RB_CNTL pre-write = 0x{_rb_cntl_pre:08x}", flush=True)
      except Exception: pass

    # WPTR_POLL_ENABLE was added in RDNA3+ (SDMA 6.x) inside RB_CNTL itself. RDNA2 SDMA v5.2's
    # regSDMA0_GFX_RB_CNTL has no such field — only RB_ENABLE, RB_SIZE, RB_VMID, RPTR_WRITEBACK_*,
    # RB_PRIV, RPTR_WB_IDLE. The v5.2 equivalent (F32_POLL_ENABLE) lives in WPTR_POLL_CNTL, set above.
    needs_wptr_poll = self.adev.ip_ver[am.SDMA0_HWIP][:2] != (4,4) and self.adev.ip_ver[am.SDMA0_HWIP][0] >= 6
    self.adev.reg(f"{reg}_RB_CNTL").write(**({f'{self.sdma_name.lower()}_wptr_poll_enable':1} if needs_wptr_poll else {}),
      rb_vmid=0, rptr_writeback_enable=1, rptr_writeback_timer=4, rb_enable=1, rb_priv=1, rb_size=(ring_size//4).bit_length()-1, inst=inst)
    self.adev.reg(f"{reg}_IB_CNTL").update(ib_enable=1, inst=inst)
    return doorbell

class AM_PSP(AM_IP):
  def init_sw(self):
    self.reg_pref = "regMP0_SMN_C2PMSG" if self.adev.ip_ver[am.MP0_HWIP] < (14,0,0) else "regMPASP_SMN_C2PMSG"

    if self.adev.devfmt.startswith("usb:"):
      self.msg1_view, paddrs = self.adev.pci_dev.alloc_sysmem(512 << 10)
      self.msg1_addr = self.adev.mm.alloc_vaddr(size=self.msg1_view.nbytes, align=am.PSP_1_MEG)
      self.adev.mm.map_range(self.msg1_addr, self.msg1_view.nbytes, [(paddrs[0], self.msg1_view.nbytes)], AddrSpace.SYS, uncached=True, boot=True)
    else:
      self.msg1_paddr = self.adev.mm.palloc(am.PSP_1_MEG, align=am.PSP_1_MEG, zero=False, boot=True)
      self.msg1_addr, self.msg1_view = self.adev.paddr2mc(self.msg1_paddr), self.adev.vram.view(self.msg1_paddr, am.PSP_1_MEG, 'B')

    self.cmd_paddr = self.adev.mm.palloc(am.PSP_CMD_BUFFER_SIZE, zero=False, boot=True)
    self.fence_paddr = self.adev.mm.palloc(am.PSP_FENCE_BUFFER_SIZE, zero=True, boot=True)

    self.ring_size = 0x10000
    self.ring_paddr = self.adev.mm.palloc(self.ring_size, zero=False, boot=True)

    self.max_tmr_size, self.tmr_size = 0x1300000, 0
    self.boot_time_tmr = self.adev.ip_ver[am.MP0_HWIP] in {(13,0,6), (13,0,14), (14,0,2), (14,0,3)}
    self.autoload_tmr = self.adev.ip_ver[am.MP0_HWIP] not in {(13,0,6), (13,0,14)}
    self.tmr_paddr = self.adev.mm.palloc(self.max_tmr_size, align=am.PSP_TMR_ALIGNMENT, zero=False, boot=True) if not self.boot_time_tmr else 0

  def init_hw(self):
    # Use the real SPL blob whenever the SOS firmware parser extracted one. On RDNA2 the v1_3 PSP header
    # parser populates sos_fw[PSP_FW_TYPE_PSP_SPL] from sos_hdr.spl. On RDNA3/4 the v2_0 parser populates
    # the same key from the explicit fw_type=PSP_SPL entry in psp_fw_bin. Falling back to KDB only matters
    # for ASICs whose SOS firmware genuinely has no SPL section, which preserves the previous behavior.
    spl_key = am.PSP_FW_TYPE_PSP_SPL if am.PSP_FW_TYPE_PSP_SPL in self.adev.fw.sos_fw else am.PSP_FW_TYPE_PSP_KDB
    sos_components = [(am.PSP_FW_TYPE_PSP_KDB, am.PSP_BL__LOAD_KEY_DATABASE), (spl_key, am.PSP_BL__LOAD_TOS_SPL_TABLE),
      (am.PSP_FW_TYPE_PSP_SYS_DRV, am.PSP_BL__LOAD_SYSDRV), (am.PSP_FW_TYPE_PSP_SOC_DRV, am.PSP_BL__LOAD_SOCDRV),
      (am.PSP_FW_TYPE_PSP_INTF_DRV, am.PSP_BL__LOAD_INTFDRV), (am.PSP_FW_TYPE_PSP_DBG_DRV, am.PSP_BL__LOAD_DBGDRV),
      (am.PSP_FW_TYPE_PSP_RAS_DRV, am.PSP_BL__LOAD_RASDRV), (am.PSP_FW_TYPE_PSP_SOS, am.PSP_BL__LOAD_SOSDRV)]

    if not self.is_sos_alive():
      for fw, compid in sos_components: self._bootloader_load_component(fw, compid)
      wait_cond(self.is_sos_alive, value=True, msg="sOS failed to start")

    self._ring_create()
    if am.PSP_FW_TYPE_PSP_TOC in self.adev.fw.sos_fw: self._tmr_init()

    # SMU fw should be loaded before TMR.
    if hasattr(self.adev.fw, 'smu_psp_desc'): self._load_ip_fw_cmd(*self.adev.fw.smu_psp_desc)
    if not self.boot_time_tmr or not self.autoload_tmr: self._tmr_load_cmd()

    # AMD_PSP_DESC_AUDIT=1: optional read-only audit log of the exact PSP descriptor load order
    # before the loop runs. Off by default. Kept for investigation traceability.
    if getenv("AMD_PSP_DESC_AUDIT", 0) == 1:
      print(f"am {self.adev.devfmt}: PSP descriptor load order ({len(self.adev.fw.descs)} descs):", flush=True)
      for _i, _d in enumerate(self.adev.fw.descs):
        _types = _d[0] if isinstance(_d, (tuple, list)) and len(_d) >= 1 else _d
        print(f"  [{_i:2d}] types={_types}", flush=True)

    for psp_desc in self.adev.fw.descs: self._load_ip_fw_cmd(*psp_desc)

    # Issue GFX_CMD_ID_AUTOLOAD_RLC on gfx10+ (including RDNA2 / Sienna Cichlid / Navi21). Linux's
    # psp_v11_0 autoload path does this for Navi21-class ASICs after the descriptor load loop; it
    # is the PSP-side trigger that finalizes the autoloaded firmware set (SMU, SDMA, MEC, RLC G/DRAM/IRAM)
    # into running microcode. Without it, tinygrad's fw descriptors landed in PSP staging but the
    # SDMA/MEC firmware was never actually promoted to the engines' F32/MEC cores.
    #
    # Historical note: this gate used to require GC_HWIP >= (11,0,0), which excluded RDNA2
    # entirely. The fallback elif for PSP_FW_TYPE_PSP_RL was also a no-op on Sienna Cichlid because
    # sienna_cichlid_sos.bin has rl.size_bytes == 0. Net effect: no PSP autoload trigger was ever
    # issued on RDNA2, SDMA0_UCODE_CHECKSUM stayed at 0x0, and the first SDMA ring test stalled at
    # parser-to-dispatch (RB_RPTR_FETCH advanced but RB_RPTR never retired).
    #
    # Widening the gate to >= (10,0,0) was the final missing piece after the full SDMA-local parity
    # stack (runtime soft reset, AUTO_CTXSW_ENABLE, BIF_SDMA0_DOORBELL_RANGE, CONTEXT0_CNTL reserved
    # bit clear, 16-dword packet padding, Linux-order setup_ring sequence). See tiny-egpu:
    # docs/rdna2-investigation-log.md "MILESTONE: RDNA2 SDMA ring test passes".
    if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0):
      self._rlc_autoload_cmd()
    elif am.PSP_FW_TYPE_PSP_RL in self.adev.fw.sos_fw:
      # gfx<10 (vega/mi etc.): load REG_LIST from the SOS firmware's RL section.
      self._load_ip_fw_cmd([am.GFX_FW_TYPE_REG_LIST], self.adev.fw.sos_fw[am.PSP_FW_TYPE_PSP_RL])

  def is_sos_alive(self): return self.adev.reg(f"{self.reg_pref}_81").read() != 0x0

  def _wait_for_bootloader(self):
    # PSP bootloader handshake: poll C2PMSG_35 bit 31 for ready. On timeout, also read C2PMSG_39 (the
    # bootloader post-code register) so the failure carries diagnostic context instead of just being
    # a bare timeout. Credit: @m0dm0d on X discovered C2PMSG_39 by reverse-engineering AMDRadeonX6000.kext
    # from an old iMac (queryBootLoaderPostCode); see tiny-egpu/docs/m0dm0d-rdna2-timeline.md for the
    # cross-reference and rationale for adopting it here.
    try:
      wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_35").read() & 0x80000000, value=0x80000000, msg="BL not ready")
    except TimeoutError as e:
      try: post_code = f"0x{self.adev.reg(f'{self.reg_pref}_39').read():08x}"
      except (KeyError, AttributeError): post_code = "<unavailable>"
      raise TimeoutError(f"{e} (C2PMSG_39 post-code={post_code})") from e

  def _prep_msg1(self, data:memoryview):
    assert len(data) <= self.msg1_view.nbytes, f"msg1 buffer is too small {len(data):#x} > {self.msg1_view.nbytes:#x}"
    padded_data = pad_bytes(bytes(data) + b'\x00' * 4, 16) # HACK: apple's memcpy requires 16-bytes alignment
    self.msg1_view[:len(padded_data)] = padded_data
    self.adev.gmc.flush_hdp()

  def _bootloader_load_component(self, fw:int, compid:int):
    if fw not in self.adev.fw.sos_fw: return 0

    self._wait_for_bootloader()

    if DEBUG >= 2: print(f"am {self.adev.devfmt}: loading sos component: {am.enum_psp_fw_type.get(fw)}")

    self._prep_msg1(self.adev.fw.sos_fw[fw])
    self.adev.reg(f"{self.reg_pref}_36").write(self.msg1_addr >> 20)
    self.adev.reg(f"{self.reg_pref}_35").write(compid)

    return self._wait_for_bootloader() if compid != am.PSP_BL__LOAD_SOSDRV else 0

  def _tmr_init(self):
    # Load TOC and calculate TMR size
    self._prep_msg1(fwm:=self.adev.fw.sos_fw[am.PSP_FW_TYPE_PSP_TOC])
    self.tmr_size = self._load_toc_cmd(len(fwm)).resp.tmr_size
    assert self.tmr_size <= self.max_tmr_size

  def _ring_create(self):
    # If the ring is already created, destroy it
    if self.adev.reg(f"{self.reg_pref}_71").read() != 0:
      self.adev.reg(f"{self.reg_pref}_64").write(am.GFX_CTRL_CMD_ID_DESTROY_RINGS)

      # There might be handshake issue with hardware which needs delay
      time.sleep(0.02)

    # Wait until the sOS is ready
    wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_64").read() & 0x80000000, value=0x80000000, msg="sOS not ready")

    self.adev.wreg_pair(self.reg_pref, "_69", "_70", self.adev.paddr2mc(self.ring_paddr))
    self.adev.reg(f"{self.reg_pref}_71").write(self.ring_size)
    self.adev.reg(f"{self.reg_pref}_64").write(am.PSP_RING_TYPE__KM << 16)

    # There might be handshake issue with hardware which needs delay
    time.sleep(0.02)

    wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_64").read() & 0x8000FFFF, value=0x80000000, msg="sOS ring not created")

  def _ring_submit(self, cmd:am.struct_psp_gfx_cmd_resp) -> am.struct_psp_gfx_cmd_resp:
    msg = am.struct_psp_gfx_rb_frame(fence_value=(prev_wptr:=self.adev.reg(f"{self.reg_pref}_67").read()) + 1,
      cmd_buf_addr_lo=lo32(self.adev.paddr2mc(self.cmd_paddr)), cmd_buf_addr_hi=hi32(self.adev.paddr2mc(self.cmd_paddr)),
      fence_addr_lo=lo32(self.adev.paddr2mc(self.fence_paddr)), fence_addr_hi=hi32(self.adev.paddr2mc(self.fence_paddr)))

    self.adev.vram.view(self.cmd_paddr, ctypes.sizeof(cmd))[:] = memoryview(cmd).cast('B')
    self.adev.vram.view(self.ring_paddr + prev_wptr * 4, ctypes.sizeof(msg))[:] = memoryview(msg).cast('B')

    # Move the wptr
    self.adev.reg(f"{self.reg_pref}_67").write(prev_wptr + ctypes.sizeof(am.struct_psp_gfx_rb_frame) // 4)

    wait_cond(lambda: self.adev.vram.view(self.fence_paddr, 4, 'I')[0], value=msg.fence_value, msg="sOS ring not responding")

    resp = type(cmd).from_buffer(bytearray(self.adev.vram.view(self.cmd_paddr, ctypes.sizeof(cmd))[:]))
    if resp.resp.status != 0: raise RuntimeError(f"PSP command failed {resp.cmd_id} {resp.resp.status}")

    return resp

  def _load_ip_fw_cmd(self, fw_types:list[int], fw_bytes:memoryview):
    self._prep_msg1(fw_bytes)
    for fw_type in fw_types:
      if DEBUG >= 2: print(f"am {self.adev.devfmt}: loading fw: {am.enum_psp_gfx_fw_type.get(fw_type)}")
      cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_LOAD_IP_FW)
      cmd.cmd.cmd_load_ip_fw.fw_phy_addr_hi, cmd.cmd.cmd_load_ip_fw.fw_phy_addr_lo = data64(self.msg1_addr)
      cmd.cmd.cmd_load_ip_fw.fw_size = len(fw_bytes)
      cmd.cmd.cmd_load_ip_fw.fw_type = cast(am.enum_psp_gfx_fw_type, fw_type)
      self._ring_submit(cmd)

  def _tmr_load_cmd(self) -> am.struct_psp_gfx_cmd_resp:
    tmr_paddr = self.adev.paddr2xgmi(self.tmr_paddr) if self.tmr_paddr else 0

    cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_SETUP_TMR)
    cmd.cmd.cmd_setup_tmr.buf_phy_addr_hi, cmd.cmd.cmd_setup_tmr.buf_phy_addr_lo = data64(self.adev.paddr2mc(self.tmr_paddr) if self.tmr_paddr else 0)
    cmd.cmd.cmd_setup_tmr.system_phy_addr_hi, cmd.cmd.cmd_setup_tmr.system_phy_addr_lo = data64(tmr_paddr)
    cmd.cmd.cmd_setup_tmr.bitfield.virt_phy_addr = 1
    cmd.cmd.cmd_setup_tmr.buf_size = self.tmr_size if self.tmr_paddr else 0
    return self._ring_submit(cmd)

  def _load_toc_cmd(self, toc_size:int) -> am.struct_psp_gfx_cmd_resp:
    cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_LOAD_TOC)
    cmd.cmd.cmd_load_toc.toc_phy_addr_hi, cmd.cmd.cmd_load_toc.toc_phy_addr_lo = data64(self.msg1_addr)
    cmd.cmd.cmd_load_toc.toc_size = toc_size
    return self._ring_submit(cmd)

  def _spatial_partition_cmd(self, mode):
    cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_SRIOV_SPATIAL_PART)
    cmd.cmd.cmd_spatial_part.mode = mode
    return self._ring_submit(cmd)

  def _rlc_autoload_cmd(self): return self._ring_submit(am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_AUTOLOAD_RLC))
