## XSPdb 介绍

[英文介绍](README.en.md)

XSPdb 是基于Python pdb调试器构建的专用RSIC-V IP调试工具，针对香山difftest接口核进行了定制，提供类GDB的交互式调试能力。该工具集成终端命令行界面、RTL级波形调试开关、自动化脚本回放、系统快照保存/恢复等基础功能模块，同时实现寄存器初始化配置、指令集反汇编解析等核心调试能力，并扩展支持断点条件触发、观察点实时追踪、寄存器/内存可视化监控等高级调试场景，通过硬件信号级调试接口与软件执行状态联动机制，为RISC-V IP的软硬件协同验证提供解决方案。

### 安装依赖

下载仓库，安装依赖：

```
git clone https://github.com/OpenXiangShan/XSPdb.git # 下载仓库
pip install -r XSPdb/requirements.txt                # 安装依赖
pip install .                                        # 根据需要确定是否需要安装
```

如果不需要源码集成，也可通过 pip 直接安装：
```bash
pip3 install XSPdb@git+https://github.com/OpenXiangShan/XSPd@master
```

### 快速开始

下载仓库安装依赖后，在仓库中执行`make test`
```bash
cd XSPdb
make test
```

上述命令依次执行以下内容：

- 下载XiangShan的二进制Python版本
- 下载测试bin文件
- 运行`example/test.py`进入XSPdb交互模式

示例输出与交互如下：

```bash
LD_PRELOAD=XSPython/xspcomm/libxspcomm.so.0.0.1 PYTHONPATH=. python3 example/test.py
Using simulated 32768B flash
[Info] reset complete
> XSPdb/example/test.py(13)test_sim_top()
-> while True:
(XiangShan) # 进入交互模式，可通过tab查询所有可用命令
(XiangShan)xui                               # 进入ui模式
(XiangShan)xload ready-to-run/microbench.bin # 加载需要运行的bin文件，命令可通过tab补全
(XiangShan)xistep                            # 执行到下一次指令提交
(XiangShan)xstep 10000                       # 执行10000个cycles
```

默认情况下，XSPdb会寻找系统中的`spike-dasm`进行反汇编，如果没找到，则采用`capstone`进行反汇编（部分指令`capstone`无法识别）。

### 手动测试

XSPdb 交互的对象是 XiangShan的Python版本，因此需要提前构建，具体构建方法可参考：[TBD](TBD)。

为了方便测试，也可以在[Release中](https://github.com/OpenXiangShan/XSPdb/releases)下载编译好的版本:

```bash
cd XSPdb
wget https://github.com/OpenXiangShan/XSPdb/releases/download/v0.1.0-test/XSPython.tar.gz
wget https://github.com/OpenXiangShan/XSPdb/releases/download/v0.1.0-test/ready-to-run.tar.gz
tar xf XSPython.tar.gz
tar xf ready-to-run.tar.gz
```

然后，通过以下代码进行测试：

```bash
LD_PRELOAD=XSPython/xspcomm/libxspcomm.so.0.0.1 PYTHONPATH=. python3 example/test.py
```

使用LD_PRELOAD提前加载xspcomm的原因是防止本地系统xspcomm库与XSPython中的，有版本冲突.

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