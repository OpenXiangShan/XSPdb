## Introduction to XSPdb

[中文介绍](README.cn.md)

XSPdb is a Python-based extension of `pdb` designed for the XiangShan IP, providing GDB-like "interactive debugging" capabilities. It supports features such as a text-based user interface, waveform enable/disable controls, script playback, snapshots, register initialization, memory read/write operations, and disassembly, etc.

### Quick Start

TBD

### Common Commands：

- `xload` Load a binary file into memory
- `xflash` Load a binary file into Flash
- `xreset_flash` Reset Flash
- `xexport_bin` Export Flash + memory data to a file
- `xexport_flash` Export Flash data to a file
- `xexport_ram` Export memory data to a file
- `xload_script` Load an XSPdb script
- `xmem_write` Write memory data
- `xbytes_to_bin` Convert bytes data to a binary file
- `xnop_insert` Insert NOP instructions in a specified address range
- `xclear_dasm_cache` Clear disassembly cache
- `xprint` Print the value and width of an internal signal
- `xset` Set the value of an internal signal
- `xstep` Step through the circuit
- `xistep` Step through instructions
- `xwatch_commit_pc` Watch commit PC
- `xunwatch_commit_pc` Unwatch commit PC
- `xwatch` Add a watch variable
- `xunwatch` Remove a watch variable
- `xpc` Print the current Commit PCs
- `xexpdiffstate` Set a variable to difftest_stat
- `xexportself` Set a variable to XSPdb self
- `xreset` Reset DUT
- `xlist_xclock_cb` List all xclock callbacks
- `xui` Enter the Text UI interface
- `xdasm` Disassemble memory data
- `xdasmflash` Disassemble Flash data
- `xdasmbytes` Disassemble binary data
- `xdasmnumber` Disassemble a number
- `xbytes2number` Convert bytes to an integer
- `xnumber2bytes` Convert an integer to bytes
- `xparse_instr_file` Parse uint64 strings
- `xload_instr_file` Load uint64 strings into memory
- `xparse_reg_file` Parse a register file
- `xload_reg_file` Load a register file
- `xset_iregs` Set Flash internal registers (Integer)
- `xset_mpc` Set the jump address (by mpc) after Flash initialization, default is 0x80000000
- `xget_mpc` Get the jump address after Flash initialization, default is 0x80000000
- `xset_fregs` Set Flash floating-point registers (general)
- `xset_ireg` Set a single Flash internal register (Integer)
- `xset_freg` Set a Flash floating-point register
- `xlist_flash_iregs` List Flash internal registers
- `xlist_flash_fregs` List Flash floating-point registers
- `xlist_freg_map` List floating-point register mappings