## XSPdb 介绍

[英文介绍](/README.en.md)

XSPdb 是基于Python pdb调试器构建的专用RSIC-V IP调试工具，针对香山difftest接口核进行了定制，提供类GDB的交互式调试能力。该工具集成终端命令行界面、RTL级波形调试开关、自动化脚本回放、系统快照保存/恢复等基础功能模块，同时实现寄存器初始化配置、指令集反汇编解析等核心调试能力，并扩展支持断点条件触发、观察点实时追踪、寄存器/内存可视化监控等高级调试场景，通过硬件信号级调试接口与软件执行状态联动机制，为RISC-V IP的软硬件协同验证提供解决方案。

<div align="center">
<img src="/.github/screenshot.png" alt="screenshot" width="800" />
<br>
XSPdb 截图
</div>

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

### 命令行模式（非交互）：

除了提供交互模式外，XSPdb也提供了批处理模式，可以把它当成一个全系统仿真器使用，其入口为 `emu.py`，例如

```bash
./emu.py -i ready-to-run/microbench.bin
```

`emu.py`在执行过程中，可以通过 ctrl+c 中断进入XSPdb交互模式。具体命令行参数如下：

```bash
usage: emu.py [-h] [-v] [-C MAX_CYCLES] [-i IMAGE] [-b WAVE_BEGIN] [-e WAVE_END] [-t INTERACT_AT] [-l] [--log-file LOG_FILE]
              [-bi BATCH_INTERVAL] [-s SCRIPT] [-r REPLAY] [--debug-level {debug,info,warn,erro}]
              [--log-level {debug,info,warn,erro}] [-pc PC_COMMITS] [--sim-args SIM_ARGS] [-F FLASH] [--no-interact]
              [--wave-path WAVE_PATH] [--ram-size RAM_SIZE] [--diff DIFF] [--cmds CMDS] [--cmds-post CMDS_POST]
              [--mem-base-address MEM_BASE_ADDRESS] [--flash-base-address FLASH_BASE_ADDRESS]
              [--diff-first-inst_address DIFF_FIRST_INST_ADDRESS] [--trace-pc-symbol-block-change] [--max-run-time MAX_RUN_TIME]

XSPdb Emulation Tool

options:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  -C MAX_CYCLES, --max-cycles MAX_CYCLES
                        maximum simulation cycles to execute
  -i IMAGE, --image IMAGE
                        image file to load and run
  -b WAVE_BEGIN, --wave-begin WAVE_BEGIN
                        start waveform dump at the specified cycle
  -e WAVE_END, --wave-end WAVE_END
                        stop waveform dump at the specified cycle
  -t INTERACT_AT, --interact-at INTERACT_AT
                        enter interactive mode at the specified cycle
  -l, --log             enable logging output
  --log-file LOG_FILE   log file name (default: ./XSPdb.log)
  -bi BATCH_INTERVAL, --batch-interval BATCH_INTERVAL
                        interval time (seconds) between batch commands
  -s SCRIPT, --script SCRIPT
                        script file to execute
  -r REPLAY, --replay REPLAY
                        replay log file
  --debug-level {debug,info,warn,erro}
                        set debug level
  --log-level {debug,info,warn,erro}
                        set log level
  -pc PC_COMMITS, --pc-commits PC_COMMITS
                        run until the specified number of commits; -1 means no limit
  --sim-args SIM_ARGS   additional simulator arguments (comma-separated)
  -F FLASH, --flash FLASH
                        flash binary file for simulation
  --no-interact         disable interactive mode (do not handle the ctrl-c signal)
  --wave-path WAVE_PATH
                        output path for waveform file
  --ram-size RAM_SIZE   simulation RAM size (e.g., 8GB or 128MB)
  --diff DIFF           path to REF shared object for difftest testing
  --cmds CMDS           XSPdb commands to execute before run (\n for newline)
  --cmds-post CMDS_POST
                        XSPdb commands to execute after script/replay (\n for newline)
  --mem-base-address MEM_BASE_ADDRESS
                        base address of memory
  --flash-base-address FLASH_BASE_ADDRESS
                        base address of flash
  --diff-first-inst_address DIFF_FIRST_INST_ADDRESS
                        first instruction address for difftest
  --trace-pc-symbol-block-change
                        enable tracing of PC symbol block changes
  --max-run-time MAX_RUN_TIME
                        maximum run time (eg 10s, 1m, 1h)
```

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

可通过`xcmds`列出所有命令，说明和所在module（通过`xapis`列出所有API，说明和所在module）。

### 基于 Socket-Wrapper 的无感回滚（TUI）

为在 TUI 下实现更稳健的“原地回滚”，本仓库提供“supervisor ⇄ socket-wrapper ⇄ target”的结构：

- Supervisor（`tools/xspdb_supervisor.py`）：
  - 读取你的终端输入、回显输出。
  - 与 wrapper 建立数据通道，转发 I/O 到 PTY。
  - 通过 CRIU 对“wrapper+target”整棵子树做保存/恢复（TTY 全在子树内，避免跨环境问题）。
  - 暴露 `.xspdb_ckpt/api.shm`，供 XSPdb 内的 `xckpt_*` 命令调用。
- Socket-Wrapper（`tools/xspdb_socket_wrapper.py`）：
  - 创建 PTY 并在 slave 上 exec target（XSPdb 后端）。
  - 提供数据发现文件（`.xspdb_ckpt/tty.shm`）与控制发现文件（`.xspdb_ckpt/cmdapi.shm`），描述共享内存通道信息。

使用步骤（入口 + 配置文件）：

1) 新建 `xspdb.json`：

```
{
  "ckpt_dir": ".xspdb_ckpt",
  "sock": ".xspdb_ckpt/api.shm",
  "prefix_key": "^A",
  "cmd_key": "c",
  "target": ["python3", "emu.py"]
}
```

2) 启动：

```
python3 tools/xspdb_entry.py -c xspdb.json
```

3) 命令模式与热键：在真实终端按 `Ctrl-A c`（可通过 `prefix_key` / `cmd_key` 配置）。看到 `[ckpt] >` 后可输入：

- `list`：列出检查点（最新项会标注 latest）
- `save [name]`：保存检查点（保存前可自动注入命令，如 xwave_flush）
- `restore [name|latest]`：从检查点恢复（先停旧、再启新，避免双写）
- `help` 或 `?`：显示帮助、当前热键、目录与 socket
- `back`：仅退出命令模式，恢复后端 I/O
- `exit`：退出并清理所有后台进程（终止 wrapper+target 与 supervisor）

说明与注意：
- TUI 内 `xckpt_restore/xckpt_rollback` 已禁用；请使用命令模式或 XSPdb 的 `xckpt_save` 等走 supervisor。
- 需要系统安装并正确配置 CRIU（以及必要的权限/内核特性）。

#### 保存/恢复 Hook（可选）

通过入口 `tools/xspdb_entry.py` 的 JSON 配置，您可以为保存/恢复配置 Hook：

- `manifest_include`：需要随检查点快照的外部文件（支持 glob），例如波形与覆盖率产物。
- `pre_save_backend_cmds`：保存前在后端注入的命令（例如 `xwave_flush`）。命令模式下会临时恢复后端注入命令，随后立即暂停（无额外等待逻辑，完成判定由用户的 Python Hook 自行处理）。

支持 Python 函数 Hook：在 `python_hooks` 中以 `module:function` 或 `/path/file.py:function` 指定：

```
"python_hooks": {
  "pre_save": "myhooks:pre_save",        
  "post_save": "myhooks:post_save",
  "pre_restore": "myhooks:pre_restore",
  "post_restore": "myhooks:post_restore"
}
```

Hook 函数签名：`def pre_save(frontend): ...`

可用辅助方法（在 `frontend` 上）：
- `hook_inject_backend_cmds(["xwave_flush"])`：向后端注入命令（无内置等待策略，请在 Hook 内自行判断完成或超时）
- `hook_child_pid()`：获取当前后端 PID

示例配置见 `example/xspdb.json`。

#### CRIU 加速选项（可选）

可在配置中启用以下 CRIU 选项以加速 dump/restore（需要内核/权限支持，具体兼容性以系统为准）：

- `criu.lazy_pages`：在恢复时加入 `--lazy-pages`，按需调页。
- `criu.track_mem`：在保存时加入 `--track-mem`，跟踪内存变化以优化镜像。
- `criu.prev_images_dir`：为 dump/restore 传入 `--prev-images-dir <PATH>` 以复用之前的镜像数据。

也可直接通过监督器脚本 CLI 开启：`--lazy-pages`、`--track-mem`、`--prev-images-dir PATH`。
