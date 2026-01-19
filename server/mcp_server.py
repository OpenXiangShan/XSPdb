#coding=utf-8
"""
MCP server that forwards structured tools to the in-target agent.

Notes:
- Requires the optional `mcp` Python package at runtime to serve MCP.
- Tools are explicitly enumerated (no generic call_api in the public list).
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from tools.shm_endpoint import ShmEndpoint
from server.config import ServerConfig


class AgentClient:
    """Thin wrapper that adapts to shm/unix transports."""
    def __init__(self, endpoint_path: str, transport: str = "shm"):
        self.endpoint_path = os.path.abspath(endpoint_path)
        self.transport = transport
        self.ep: Optional[Any] = None  # ShmEndpoint or UnixEndpoint
        self.sock_path: Optional[str] = None

    def connect(self):
        info = self._load_discovery()
        if info.get("type") == "unix":
            self.transport = "unix"
            self.sock_path = info.get("path") or self._maybe_socket_path()
            self.ep = UnixEndpoint(self.sock_path)
        else:
            self.transport = "shm"
            self.ep = ShmEndpoint(self.endpoint_path).open()
        return self

    def request(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        assert self.ep, "AgentClient not connected"
        if self.transport == "unix":
            return self.ep.request(obj)  # type: ignore
        return self.ep.request(obj)  # type: ignore

    def close(self):
        if self.ep:
            try:
                self.ep.close()
            except Exception:
                pass
            self.ep = None

    def _load_discovery(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.endpoint_path):
                with open(self.endpoint_path, "r") as f:
                    info = json.load(f)
                if isinstance(info, dict):
                    return info
        except Exception:
            pass
        return {}

    def _maybe_socket_path(self) -> str:
        if self.endpoint_path.endswith(".json"):
            return self.endpoint_path[:-5] + ".sock"
        return self.endpoint_path


class UnixEndpoint:
    """Simple Unix socket client for JSON-line RPC."""
    def __init__(self, sock_path: str):
        self.sock_path = sock_path

    def request(self, obj: Dict[str, Any], timeout_sec: float = 5.0) -> Dict[str, Any]:
        import socket
        data = (json.dumps(obj) + "\n").encode("utf-8")
        resp = b""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_sec)
            s.connect(self.sock_path)
            s.sendall(data)
            while True:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                resp += chunk
                if b"\n" in chunk:
                    break
        try:
            return json.loads(resp.decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "bad_json"}

    def close(self):
        pass


# Optional MCP integration ----------------------------------------------------

async def _build_mcp_server(cfg: ServerConfig):
    try:
        from mcp.server import Server
    except Exception:
        raise RuntimeError("mcp package not available; install mcp to use the MCP server")

    server = Server("xspdb")
    agent = AgentClient(cfg.agent_endpoint, transport=getattr(cfg, "agent_transport", "shm")).connect()

    def _wrap(cmd: Dict[str, Any]) -> str:
        return json.dumps(agent.request(cmd))

    # Core / session-lite tools (single session model)
    @server.tool(name="ping", description="Ping the in-target agent; returns pong.")
    async def tool_ping() -> str:
        return _wrap({"cmd": "ping"})

    @server.tool(name="load_bin", description="Load a binary into RAM. args: path (str), addr (int).")
    async def tool_load_bin(path: str, addr: Optional[int] = None) -> str:
        cmd = {"cmd": "load_bin", "path": path}
        if addr is not None:
            cmd["addr"] = addr
        return _wrap(cmd)

    @server.tool(name="load_flash", description="Load a binary into flash. args: path (str), base (int, optional).")
    async def tool_load_flash(path: str, base: Optional[int] = None) -> str:
        cmd = {"cmd": "load_flash", "path": path}
        if base is not None:
            cmd["base"] = base
        return _wrap(cmd)

    @server.tool(name="load_elf", description="Parse and load ELF PT_LOAD segments. args: path (str), base_override (int, optional).")
    async def tool_load_elf(path: str, base_override: Optional[int] = None) -> str:
        cmd = {"cmd": "load_elf", "path": path}
        if base_override is not None:
            cmd["base_override"] = base_override
        return _wrap(cmd)

    @server.tool(name="step_cycles", description="Step DUT by cycles. args: count (int).")
    async def tool_step_cycles(count: int) -> str:
        return _wrap({"cmd": "step_cycles", "count": count})

    @server.tool(name="step_instructions", description="Step by instructions. args: count (int).")
    async def tool_step_instructions(count: int) -> str:
        return _wrap({"cmd": "step_instructions", "count": count})

    @server.tool(name="run_until_commit", description="Run until N commits. args: count (int).")
    async def tool_run_until_commit(count: int) -> str:
        return _wrap({"cmd": "run_commits", "count": count})

    @server.tool(name="reset_dut", description="Reset DUT (assert reset and release).")
    async def tool_reset_dut() -> str:
        return _wrap({"cmd": "reset_dut"})

    @server.tool(name="flash_reset", description="Reset flash state and clear cached info.")
    async def tool_flash_reset() -> str:
        return _wrap({"cmd": "flash_reset"})

    @server.tool(name="wave_enable", description="Enable waveform dump. args: path (str optional).")
    async def tool_wave_enable(path: Optional[str] = None) -> str:
        return _wrap({"cmd": "wave_enable", "path": path or ""})

    @server.tool(name="wave_disable", description="Disable waveform dump.")
    async def tool_wave_disable() -> str:
        return _wrap({"cmd": "wave_disable"})

    @server.tool(name="get_regs", description="Get integer/fp regs snapshot.")
    async def tool_get_regs() -> str:
        return _wrap({"cmd": "get_regs"})

    @server.tool(name="get_csr", description="Get CSR snapshot.")
    async def tool_get_csr() -> str:
        return _wrap({"cmd": "get_csr"})

    @server.tool(name="read_mem", description="Read memory. args: addr (int), length (int).")
    async def tool_read_mem(addr: int, length: int) -> str:
        return _wrap({"cmd": "read_mem", "addr": addr, "length": length})

    @server.tool(name="write_mem", description="Write memory. args: addr (int), bytes (hex string), width (optional int).")
    async def tool_write_mem(addr: int, bytes_hex: str, width: Optional[int] = None) -> str:
        cmd = {"cmd": "write_mem", "addr": addr, "bytes": bytes_hex}
        if width is not None:
            cmd["width"] = width
        return _wrap(cmd)

    @server.tool(name="signal_break", description="Set internal signal breakpoint. args: signal (str), cmp (eq/ne/gt/lt/ge/le), value (int or signal name), once (bool).")
    async def tool_signal_break(signal: str, cmp: str, value: str, once: Optional[bool] = None) -> str:
        cmd = {"cmd": "signal_break", "signal": signal, "cmp": cmp, "value": value}
        if once is not None:
            cmd["once"] = once
        return _wrap(cmd)

    @server.tool(name="watch_pc", description="Watch commit PC; break when hit. args: pc (int).")
    async def tool_watch_pc(pc: int) -> str:
        return _wrap({"cmd": "watch_pc", "addr": pc})

    @server.tool(name="clear_watch_pc", description="Clear commit PC watch.")
    async def tool_clear_watch_pc() -> str:
        return _wrap({"cmd": "clear_watch_pc"})

    @server.tool(name="trap_break", description="Break on trap. args: on (bool).")
    async def tool_trap_break(on: bool = True) -> str:
        return _wrap({"cmd": "trap_break", "on": on})

    @server.tool(name="good_trap_enable", description="Enable/disable good_trap checker. args: on (bool).")
    async def tool_good_trap_enable(on: bool = True) -> str:
        return _wrap({"cmd": "good_trap_enable", "on": on})

    @server.tool(name="difftest_load_ref", description="Load difftest reference SO. args: path (str).")
    async def tool_difftest_load_ref(path: str) -> str:
        return _wrap({"cmd": "difftest_load_ref", "path": path})

    @server.tool(name="difftest_enable", description="Enable/disable difftest diff. args: on (bool).")
    async def tool_difftest_enable(on: bool = True) -> str:
        return _wrap({"cmd": "difftest_enable", "on": on})

    @server.tool(name="get_flash_regs", description="Get flash init integer/fp regs.")
    async def tool_get_flash_regs() -> str:
        return _wrap({"cmd": "get_flash_regs"})

    @server.tool(name="set_flash_regs", description="Set flash init regs. args: iregs (dict), fregs (dict).")
    async def tool_set_flash_regs(iregs: Optional[dict] = None, fregs: Optional[dict] = None) -> str:
        return _wrap({"cmd": "set_flash_regs", "iregs": iregs or {}, "fregs": fregs or {}})

    @server.tool(name="set_mpc", description="Set flash mpc jump address. args: addr (int).")
    async def tool_set_mpc(addr: int) -> str:
        return _wrap({"cmd": "set_flash_regs", "iregs": {"mpc": addr}})

    @server.tool(name="disasm", description="Disassemble memory/flash. args: addr (int), length (int), flash (bool optional).")
    async def tool_disasm(addr: int, length: int, flash: Optional[bool] = None) -> str:
        return _wrap({"cmd": "disasm", "addr": addr, "length": length, "flash": bool(flash)})

    @server.tool(name="decode_instr", description="Decode instruction word. args: value (int or hex string).")
    async def tool_decode_instr(value: str) -> str:
        return _wrap({"cmd": "decode_instr", "value": value})

    @server.tool(name="encode_instr", description="Encode instruction from fields. args: fields (dict), width (16 or 32).")
    async def tool_encode_instr(fields: dict, width: int = 32) -> str:
        return _wrap({"cmd": "encode_instr", "fields": fields, "width": width})

    @server.tool(name="patch_instr", description="Patch instruction bytes at addr. args: addr (int), bytes (hex string) or fields+width.")
    async def tool_patch_instr(addr: int, bytes_hex: Optional[str] = None, fields: Optional[dict] = None, width: Optional[int] = None) -> str:
        cmd = {"cmd": "patch_instr", "addr": addr}
        if bytes_hex:
            cmd["bytes"] = bytes_hex
        if fields:
            cmd["fields"] = fields
        if width:
            cmd["width"] = width
        return _wrap(cmd)

    @server.tool(name="insert_nop", description="Insert NOPs starting at addr. args: addr (int), count (int).")
    async def tool_insert_nop(addr: int, count: int) -> str:
        # reuse patch handler with encoded nops via fields not yet exposed; use bytes of rv32 e.g., 0x13 for addi x0,x0,0
        nop_bytes = ("13000000" * count)  # simplistic hex repeat
        return _wrap({"cmd": "patch_instr", "addr": addr, "bytes": nop_bytes})

    @server.tool(name="run_script", description="Run XSPdb script/commands. args: path (str) or text (str), gap_ms (int optional).")
    async def tool_run_script(path: Optional[str] = None, text: Optional[str] = None, gap_ms: Optional[int] = None) -> str:
        cmd = {"cmd": "run_script"}
        if path:
            cmd["path"] = path
        if text:
            cmd["text"] = text
        if gap_ms is not None:
            cmd["gap_ms"] = gap_ms
        return _wrap(cmd)

    @server.tool(name="dump_region", description="Dump memory regions to file. args: path (str), regions (list of [addr,len]).")
    async def tool_dump_region(path: str, regions: list) -> str:
        return _wrap({"cmd": "dump_region", "path": path, "regions": regions})

    @server.tool(name="tty_tail", description="Tail headless TTY output (noop in TUI). args: n (int optional).")
    async def tool_tty_tail(n: Optional[int] = None) -> str:
        return _wrap({"cmd": "tty_tail", "n": n or 200})

    @server.tool(name="session_info", description="Get basic session info (mem/flash files, init state).")
    async def tool_session_info() -> str:
        return _wrap({"cmd": "session_info"})

    return server


async def main():
    cfg_path = ServerConfig.default_path()
    cfg = ServerConfig.load(cfg_path) if os.path.exists(cfg_path) else ServerConfig(
        agent_endpoint=os.environ.get("XSPDB_AGENT_ENDPOINT", ".xspdb_ckpt/agent.shm"),
        agent_transport=os.environ.get("XSPDB_AGENT_TRANSPORT", "shm"),
    )
    server = await _build_mcp_server(cfg)
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
