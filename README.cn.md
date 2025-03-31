## XSPdb 介绍

[英文介绍](README.en.md)

XSPdb 是基于python pdb 对香山IP进行类似GDB的“交互式debug”功能封装，支持字符界面、波形开启/关闭、脚本回放、快照、寄存器初始化、内存读写、反汇编等功能。

### 快速开始

TBD

### 常用命令：

- `xload` Load a binary file into memory （加载指定bin文件到内存）
- `xflash` Load a binary file into Flash （加载指定bin文件到Flash）
- `xreset_flash` Reset Flash （重置Flash）
- `xexport_bin` Export Flash + memory data to a file （导出Flash和内存数据到文件）
- `xexport_flash` Export Flash data to a file （导出Flash数据到文件）
- `xexport_ram` Export memory data to a file （导出内存数据到文件）
- `xload_script` Load an XSPdb script （加载XSPdb脚本）
- `xmem_write` Write memory data （写入内存数据）
- `xbytes_to_bin` Convert bytes data to a binary file （将字节数据转换为bin文件）
- `xnop_insert` Insert NOP instructions in a specified address range （在指定地址范围插入NOP指令）
- `xclear_dasm_cache` Clear disassembly cache （清除反汇编缓存）
- `xprint` Print the value and width of an internal signal （打印内部信号的值和宽度）
- `xset` Set the value of an internal signal （设置内部信号的值）
- `xstep` Step through the circuit （逐步执行电路）
- `xistep` Step through instructions （逐步执行指令）
- `xwatch_commit_pc` Watch commit PC （监视提交的PC）
- `xunwatch_commit_pc` Unwatch commit PC （取消监视提交的PC）
- `xwatch` Add a watch variable （添加监视变量）
- `xunwatch` Remove a watch variable （移除监视变量）
- `xpc` Print the current Commit PCs （打印当前提交的PC）
- `xexpdiffstate` Set a variable to difftest_stat （将变量设置为difftest_stat）
- `xexportself` Set a variable to XSPdb self （将变量设置为XSPdb自身）
- `xreset` Reset DUT （重置DUT）
- `xlist_xclock_cb` List all xclock callbacks （列出所有xclock回调）
- `xui` Enter the Text UI interface （进入文本用户界面）
- `xdasm` Disassemble memory data （反汇编内存数据）
- `xdasmflash` Disassemble Flash data （反汇编Flash数据）
- `xdasmbytes` Disassemble binary data （反汇编二进制数据）
- `xdasmnumber` Disassemble a number （反汇编一个数字）
- `xbytes2number` Convert bytes to an integer （将字节转换为整数）
- `xnumber2bytes` Convert an integer to bytes （将整数转换为字节）
- `xparse_instr_file` Parse uint64 strings （解析uint64字符串）
- `xload_instr_file` Load uint64 strings into memory （加载uint64字符串到内存）
- `xparse_reg_file` Parse a register file （解析寄存器文件）
- `xload_reg_file` Load a register file （加载寄存器文件）
- `xset_iregs` Set Flash internal registers (Integer) （设置Flash内部寄存器（整数））
- `xset_mpc` Set the jump address (by mpc) after Flash initialization, default is 0x80000000 （设置Flash初始化后的跳转地址（通过mpc），默认值为0x80000000）
- `xget_mpc` Get the jump address after Flash initialization, default is 0x80000000 （获取Flash初始化后的跳转地址，默认值为0x80000000）
- `xset_fregs` Set Flash floating-point registers (general) （设置Flash浮点寄存器（通用））
- `xset_ireg` Set a single Flash internal register (Integer) （设置单个Flash内部寄存器（整数））
- `xset_freg` Set a Flash floating-point register （设置Flash浮点寄存器）
- `xlist_flash_iregs` List Flash internal registers （列出Flash内部寄存器）
- `xlist_flash_fregs` List Flash floating-point registers （列出Flash浮点寄存器）
- `xlist_freg_map` List floating-point register mappings （列出浮点寄存器映射）