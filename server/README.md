# XSPdb MCP/Agent Server

组件概览
- `server/agent.py`: 运行在目标进程内的代理，使用共享内存队列提供结构化控制。通过环境变量配置：`XSPDB_AGENT_ENDPOINT`（发现 JSON 路径），可选 `XSPDB_AGENT_MODULE`（自定义 agent），`XSPDB_AGENT_STANDALONE=1`（仅跑 agent，不进入 emu 主循环）。
- `server/mcp_server.py`: MCP server，将工具请求转发给 agent（需额外安装 `mcp` Python 包）。
- `server/config.py`: 读取 JSON 配置（headless/supervisor/agent/toolchain 等）。

Headless 入口
- `tools/xspdb_supervisor.py --headless --agent-endpoint <path> [--agent-module ...]`：启动 socket-wrapper/target，不绑定本地 TTY，PTX 输出写入 `wrapper.tty.headless.log`，并写出 agent 发现文件。
- 环境变量：`XSPDB_HEADLESS=1`, `XSPDB_TTY_BUF_LINES`, `XSPDB_AGENT_MODULE`, `XSPDB_AGENT_ENDPOINT`, `XSPDB_AGENT_STANDALONE`，可选 `XSPDB_CKPT_ENABLE=0` 关闭 CRIU/ckpt。

Agent IPC（共享内存）
- 发现文件：`{"type": "shm", "req": "<name>", "resp": "<name>", "size": N}`。
- 命令（JSON）：`ping`, `session_info`, `load_bin`, `load_flash`, `load_elf`, `dump_region`, `step_cycles`, `step_instructions`, `run_commits`, `reset_dut`, `flash_reset`, `wave_enable/wave_disable`, `get_regs`, `get_csr`, `read_mem`, `write_mem`, `signal_break`, `watch_pc/clear_watch_pc`, `trap_break`, `good_trap_enable`, `difftest_load_ref`, `difftest_enable`, `get_flash_regs`, `set_flash_regs`, `disasm`, `decode_instr`, `encode_instr`, `patch_instr`, `run_script`, `tty_tail`.

MCP 工具（`server/mcp_server.py`）
- 1:1 封装上述命令并带参数说明（不公开泛用 `call_api`）。安装 `mcp` 后运行 `python -m server.mcp_server`（默认 stdio，可自行改 TCP/Unix）。支持 shm/Unix 两种 agent 传输（`XSPDB_AGENT_TRANSPORT=shm|unix`）。若需要屏蔽 CKPT，可设置环境 `XSPDB_CKPT_ENABLE=0`。

测试
- `python -m unittest server.tests.test_agent_basic`（当前环境无共享内存权限会跳过）。
