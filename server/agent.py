#coding=utf-8
"""
Lightweight in-process agent that exposes XSPdb controls over shared-memory
queues. Designed to be launched by the target process (emu.py) when running
under the socket-wrapper/supervisor in headless mode.

Protocol:
  - Discovery file (JSON) at XSPDB_AGENT_ENDPOINT or provided path:
      {"type": "shm", "req": "<req_name>", "resp": "<resp_name>", "size": N}
  - Requests/responses are JSON objects sent as single frames on req/resp.
  - Minimal commands:
      {"cmd": "ping"}
      {"cmd": "call_api", "api": "api_dut_bin_load", "args": [...], "kwargs": {...}}
      {"cmd": "tty_tail", "n": 200}
      {"cmd": "session_info"}
      {"cmd": "load_elf", "path": "...", "base_override": 0}
      {"cmd": "dump_region", "path": "...", "regions": [[addr,len], ...]}
      {"cmd": "disasm", "addr": 0x80000000, "length": 64, "flash": false}
      {"cmd": "patch_instr", "addr": 0x80000000, "bytes": "00100013"}
      {"cmd": "encode_instr", "fields": {...}, "width": 32}
      {"cmd": "decode_instr", "value": "0x00000013"}
      {"cmd": "run_script", "path": "...", "gap_ms": 0}
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from tools.shm_queue import SharedMsgQueue


def _load_module(mod: str):
    """Load module from dotted name or file path."""
    if os.path.isfile(mod):
        spec = importlib.util.spec_from_file_location("xspdb_agent_module", mod)
        if not spec or not spec.loader:
            raise ImportError(f"cannot load module from {mod}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(mod)


def _hash_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class AgentConfig:
    endpoint: str
    queue_size: int = 1 << 16
    transport: str = "shm"  # shm | unix
    tty_log: Optional[str] = None
    external_tools: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_env() -> "AgentConfig":
        ep = os.environ.get("XSPDB_AGENT_ENDPOINT", os.path.join(os.environ.get("XSPDB_CKPT_DIR", ".xspdb_ckpt"), "agent.shm"))
        size = int(os.environ.get("XSPDB_AGENT_QUEUE_SIZE", str(1 << 16)))
        transport = os.environ.get("XSPDB_AGENT_TRANSPORT", "shm")
        tty_log = os.environ.get("XSPDB_TTY_STDOUT_LOG")
        tools_json = os.environ.get("XSPDB_AGENT_TOOLS")
        tools_cfg: Dict[str, Any] = {}
        if tools_json and os.path.isfile(tools_json):
            try:
                with open(tools_json, "r") as f:
                    tools_cfg = json.load(f)
            except Exception:
                tools_cfg = {}
        return AgentConfig(endpoint=ep, queue_size=size, transport=transport, tty_log=tty_log, external_tools=tools_cfg)


class AgentServer:
    def __init__(self, xspdb: Any, cfg: AgentConfig):
        self.xspdb = xspdb
        self.cfg = cfg
        self.req_q: Optional[SharedMsgQueue] = None
        self.resp_q: Optional[SharedMsgQueue] = None
        self.thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._unix_server: Optional["UnixSocketServer"] = None

    # ---------- lifecycle ----------
    def start(self):
        if self.cfg.transport == "unix":
            disc, sock = self._resolve_unix_paths(self.cfg.endpoint)
            self._unix_server = UnixSocketServer(sock, self._dispatch, stop_event=self._stop)
            self._write_unix_discovery(disc, sock)
            self._unix_server.start()
            return self
        else:
            req_name, resp_name = self._queue_names(self.cfg.endpoint)
            self.req_q = SharedMsgQueue(req_name, size=self.cfg.queue_size, create=True)
            self.resp_q = SharedMsgQueue(resp_name, size=self.cfg.queue_size, create=True)
            self._write_discovery(self.cfg.endpoint, req_name, resp_name, self.cfg.queue_size)
            self.thread = threading.Thread(target=self._serve_loop, name="XSPdbAgent", daemon=True)
            self.thread.start()
            return self

    def stop(self, unlink: bool = False):
        self._stop.set()
        if self._unix_server:
            self._unix_server.stop()
            self._unix_server = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        for q in (self.req_q, self.resp_q):
            if q:
                try:
                    q.close()
                    if unlink:
                        q.unlink()
                except Exception:
                    pass
        self.req_q = None
        self.resp_q = None

    # ---------- core loop ----------
    def _serve_loop(self):
        assert self.req_q and self.resp_q
        while not self._stop.is_set():
            raw = self.req_q.pop()
            if not raw:
                time.sleep(0.005)
                continue
            try:
                req = json.loads(raw.decode("utf-8"))
            except Exception as ex:
                self._respond({"ok": False, "error": f"bad_json: {ex}"})
                continue
            resp = self._dispatch(req)
            self._respond(resp)

    def _respond(self, obj: Dict[str, Any]):
        if not self.resp_q:
            return
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if self.resp_q.push(data):
                return
            time.sleep(0.002)

    # ---------- dispatch ----------
    def _dispatch(self, req: Dict[str, Any]) -> Dict[str, Any]:
        cmd = req.get("cmd")
        try:
            if cmd == "ping":
                return {"ok": True, "data": "pong"}
            if cmd == "call_api":
                return self._handle_call_api(req)
            if cmd == "tty_tail":
                n = int(req.get("n", 200))
                return {"ok": True, "data": self._tty_tail(n)}
            if cmd == "session_info":
                return {"ok": True, "data": self._session_info()}
            if cmd == "load_elf":
                return self._handle_load_elf(req)
            if cmd == "dump_region":
                return self._handle_dump_region(req)
            if cmd == "disasm":
                return self._handle_disasm(req)
            if cmd == "patch_instr":
                return self._handle_patch_instr(req)
            if cmd == "encode_instr":
                return self._handle_encode(req)
            if cmd == "decode_instr":
                return self._handle_decode(req)
            if cmd == "run_script":
                return self._handle_run_script(req)
            if cmd == "step_cycles":
                return self._handle_step(req, mode="cycles")
            if cmd == "step_instructions":
                return self._handle_step(req, mode="inst")
            if cmd == "run_commits":
                return self._handle_step(req, mode="commits")
            if cmd == "reset_dut":
                return self._call_simple("api_dut_reset")
            if cmd == "flash_reset":
                return self._call_simple("api_dut_reset_flash")
            if cmd == "load_bin":
                return self._handle_load_bin(req)
            if cmd == "load_flash":
                return self._handle_load_flash(req)
            if cmd == "wave_enable":
                return self._handle_wave(req, enable=True)
            if cmd == "wave_disable":
                return self._handle_wave(req, enable=False)
            if cmd == "get_regs":
                return self._handle_get_regs(req)
            if cmd == "get_csr":
                return self._handle_get_csr(req)
            if cmd == "read_mem":
                return self._handle_read_mem(req)
            if cmd == "write_mem":
                return self._handle_write_mem(req)
            if cmd == "difftest_load_ref":
                return self._handle_difftest_ref(req)
            if cmd == "difftest_enable":
                return self._handle_difftest_enable(req)
            if cmd == "get_flash_regs":
                return self._handle_flash_regs(get_only=True, req=req)
            if cmd == "set_flash_regs":
                return self._handle_flash_regs(get_only=False, req=req)
            if cmd == "trap_break":
                return self._handle_trap_break(req)
            if cmd == "good_trap_enable":
                return self._handle_good_trap(req)
            if cmd == "watch_pc":
                return self._handle_watch_pc(req)
            if cmd == "clear_watch_pc":
                return self._handle_unwatch_pc(req)
            if cmd == "signal_break":
                return self._handle_signal_break(req)
            if cmd == "backtrace":
                return self._handle_backtrace(req)
            if cmd == "symbol_lookup":
                return self._handle_symbol_lookup(req)
            return {"ok": False, "error": f"unknown_cmd:{cmd}"}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    # ---------- handlers ----------
    def _handle_call_api(self, req: Dict[str, Any]) -> Dict[str, Any]:
        api = req.get("api")
        args = req.get("args", [])
        kwargs = req.get("kwargs", {})
        fn = getattr(self.xspdb, api, None)
        if not callable(fn):
            return {"ok": False, "error": f"api_not_found:{api}"}
        res = fn(*args, **kwargs)
        return {"ok": True, "data": res}

    def _tty_tail(self, n: int = 200) -> str:
        path = self.cfg.tty_log
        if not path:
            ckpt = os.environ.get("XSPDB_CKPT_DIR", ".")
            path = os.path.join(ckpt, "wrapper.tty.headless.log")
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-n:])

    def _session_info(self) -> Dict[str, Any]:
        return {
            "mem_base": getattr(self.xspdb, "mem_base", None),
            "flash_base": getattr(self.xspdb, "flash_base", None),
            "mem_inited": getattr(self.xspdb, "mem_inited", None),
            "flash_file": getattr(self.xspdb, "flash_bin_file", None),
            "exec_file": getattr(self.xspdb, "exec_bin_file", None),
        }

    def _handle_load_elf(self, req: Dict[str, Any]) -> Dict[str, Any]:
        path = req.get("path")
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "elf_not_found"}
        base_override = req.get("base_override")
        try:
            from elftools.elf.elffile import ELFFile  # type: ignore
        except Exception as ex:
            return {"ok": False, "error": f"pyelftools_missing:{ex}"}
        if not getattr(self.xspdb, "mem_inited", False):
            if hasattr(self.xspdb, "api_init_mem"):
                self.xspdb.api_init_mem()
        loaded = []
        with open(path, "rb") as f:
            elf = ELFFile(f)
            for seg in elf.iter_segments():
                if seg["p_type"] != "PT_LOAD":
                    continue
                data = seg.data()
                addr = seg["p_paddr"] or seg["p_vaddr"]
                if base_override is not None:
                    addr = int(base_override) + (addr or 0)
                if hasattr(self.xspdb, "api_write_bytes"):
                    self.xspdb.api_write_bytes(addr, data)
                loaded.append({"addr": addr, "size": len(data)})
        return {"ok": True, "data": loaded}

    def _handle_dump_region(self, req: Dict[str, Any]) -> Dict[str, Any]:
        path = req.get("path")
        regions = req.get("regions") or []
        if not path:
            return {"ok": False, "error": "path_missing"}
        if not hasattr(self.xspdb, "api_read_bytes_from"):
            return {"ok": False, "error": "api_read_bytes_from_missing"}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            for r in regions:
                addr, length = int(r[0]), int(r[1])
                data = self.xspdb.api_read_bytes_from(addr, length)
                f.write(bytes(data))
        return {"ok": True, "data": {"path": os.path.abspath(path)}}

    def _handle_disasm(self, req: Dict[str, Any]) -> Dict[str, Any]:
        addr = int(req.get("addr", 0))
        length = int(req.get("length", 0))
        use_flash = bool(req.get("flash", False))
        asm_list: List[Any] = []
        if use_flash and hasattr(self.xspdb, "api_flash_data_to_asm"):
            asm_list = self.xspdb.api_flash_data_to_asm(addr, length)
        elif hasattr(self.xspdb, "api_all_data_to_asm"):
            asm_list = self.xspdb.api_all_data_to_asm(addr, length)
        elif hasattr(self.xspdb, "api_read_bytes_from"):
            raw = self.xspdb.api_read_bytes_from(addr, length)
            asm_list = [(addr + i, f"{b:02x}", f"0x{b:02x}") for i, b in enumerate(raw)]
        return {"ok": True, "data": asm_list}

    def _handle_patch_instr(self, req: Dict[str, Any]) -> Dict[str, Any]:
        addr = int(req.get("addr", 0))
        data_hex = req.get("bytes")
        fields = req.get("fields")
        if data_hex:
            raw = bytes.fromhex(data_hex)
        elif fields:
            width = int(req.get("width", 32))
            raw = self._encode_from_fields(fields, width)
        else:
            return {"ok": False, "error": "no_bytes"}
        if hasattr(self.xspdb, "api_write_bytes"):
            self.xspdb.api_write_bytes(addr, raw)
        return {"ok": True, "data": {"addr": addr, "size": len(raw)}}

    def _handle_encode(self, req: Dict[str, Any]) -> Dict[str, Any]:
        fields = req.get("fields") or {}
        width = int(req.get("width", 32))
        val = self._encode_from_fields(fields, width)
        return {"ok": True, "data": val}

    def _handle_decode(self, req: Dict[str, Any]) -> Dict[str, Any]:
        val = req.get("value")
        if val is None:
            return {"ok": False, "error": "value_missing"}
        if isinstance(val, str):
            num = int(val, 0)
        else:
            num = int(val)
        width = 16 if num <= 0xFFFF else 32
        if width == 16 and hasattr(self.xspdb, "api_decode_instr16"):
            decoded = self.xspdb.api_decode_instr16(num)
        elif width == 32 and hasattr(self.xspdb, "api_decode_instr32"):
            decoded = self.xspdb.api_decode_instr32(num)
        else:
            decoded = None
        return {"ok": True, "data": decoded}

    def _handle_run_script(self, req: Dict[str, Any]) -> Dict[str, Any]:
        path = req.get("path")
        text = req.get("text")
        gap_ms = float(req.get("gap_ms", 0))
        if path and hasattr(self.xspdb, "api_exec_script"):
            self.xspdb.api_exec_script(path, gap_time=gap_ms / 1000.0)
            return {"ok": True}
        if text and hasattr(self.xspdb, "api_exec_batch_cmd"):
            cmds = text.splitlines()
            self.xspdb.api_exec_batch_cmd(cmds, gap_time=gap_ms / 1000.0)
            return {"ok": True}
        return {"ok": False, "error": "script_missing"}

    def _handle_step(self, req: Dict[str, Any], mode: str) -> Dict[str, Any]:
        count = int(req.get("count", 0))
        if mode == "cycles" and hasattr(self.xspdb, "api_step_dut"):
            ran = self.xspdb.api_step_dut(count)
            return {"ok": True, "data": {"ran_cycles": ran}}
        if mode == "inst" and hasattr(self.xspdb, "api_xistep"):
            ran = self.xspdb.api_xistep(count)
            return {"ok": True, "data": {"ran_insts": ran}}
        if mode == "commits" and hasattr(self.xspdb, "api_run_commits"):
            ran = self.xspdb.api_run_commits(count)
            return {"ok": True, "data": {"ran_commits": ran}}
        return {"ok": False, "error": "step_not_supported"}

    def _handle_flash_regs(self, get_only: bool, req: Dict[str, Any]) -> Dict[str, Any]:
        if get_only:
            data = {}
            if hasattr(self.xspdb, "api_get_flash_init_iregs"):
                data["iregs"] = self.xspdb.api_get_flash_init_iregs()
            if hasattr(self.xspdb, "api_get_flash_init_fregs"):
                data["fregs"] = self.xspdb.api_get_flash_init_fregs()
            return {"ok": True, "data": data}
        iregs = req.get("iregs") or {}
        fregs = req.get("fregs") or {}
        if iregs and hasattr(self.xspdb, "api_set_flash_int_regs"):
            self.xspdb.api_set_flash_int_regs(iregs)
        if fregs and hasattr(self.xspdb, "api_set_flash_float_regs"):
            self.xspdb.api_set_flash_float_regs(fregs)
        return {"ok": True}

    def _handle_trap_break(self, req: Dict[str, Any]) -> Dict[str, Any]:
        on = bool(req.get("on", True))
        if hasattr(self.xspdb, "api_break_on_trap"):
            self.xspdb.api_break_on_trap(on)
            return {"ok": True, "data": {"on": on}}
        return {"ok": False, "error": "api_break_on_trap_missing"}

    def _handle_good_trap(self, req: Dict[str, Any]) -> Dict[str, Any]:
        on = bool(req.get("on", True))
        if hasattr(self.xspdb, "api_disable_good_trap"):
            self.xspdb.api_disable_good_trap(not on)
            return {"ok": True, "data": {"on": on}}
        return {"ok": False, "error": "api_disable_good_trap_missing"}

    def _handle_watch_pc(self, req: Dict[str, Any]) -> Dict[str, Any]:
        addr = int(req.get("addr", 0))
        if hasattr(self.xspdb, "do_xwatch_commit_pc"):
            self.xspdb.do_xwatch_commit_pc(hex(addr))
            return {"ok": True}
        return {"ok": False, "error": "watch_pc_not_supported"}

    def _handle_unwatch_pc(self, req: Dict[str, Any]) -> Dict[str, Any]:
        addr = req.get("addr")
        if hasattr(self.xspdb, "do_xunwatch_commit_pc"):
            arg = hex(int(addr)) if addr is not None else ""
            self.xspdb.do_xunwatch_commit_pc(arg)
            return {"ok": True}
        return {"ok": False, "error": "watch_pc_not_supported"}

    def _handle_signal_break(self, req: Dict[str, Any]) -> Dict[str, Any]:
        sig = req.get("signal")
        cmp = req.get("cmp", "eq")
        val = req.get("value")
        once = bool(req.get("once", False))
        if not sig or val is None:
            return {"ok": False, "error": "signal/value required"}
        if hasattr(self.xspdb, "api_xbreak"):
            self.xspdb.api_xbreak(sig, cmp, val, callback_once=once)
            return {"ok": True}
        return {"ok": False, "error": "api_xbreak_missing"}

    def _handle_backtrace(self, req: Dict[str, Any]) -> Dict[str, Any]:
        depth = int(req.get("depth", 10))
        if hasattr(self.xspdb, "api_get_current_call_stack"):
            return {"ok": True, "data": self.xspdb.api_get_current_call_stack(depth)}
        return {"ok": False, "error": "api_get_current_call_stack_missing"}

    def _handle_symbol_lookup(self, req: Dict[str, Any]) -> Dict[str, Any]:
        addr = int(req.get("addr", 0))
        if hasattr(self.xspdb, "api_address_to_symbol"):
            return {"ok": True, "data": self.xspdb.api_address_to_symbol(addr)}
        return {"ok": False, "error": "api_address_to_symbol_missing"}

    def _handle_load_bin(self, req: Dict[str, Any]) -> Dict[str, Any]:
        path = req.get("path")
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "bin_not_found"}
        if hasattr(self.xspdb, "api_dut_bin_load"):
            self.xspdb.api_dut_bin_load(path)
            return {"ok": True, "data": {"path": os.path.abspath(path)}}
        return {"ok": False, "error": "api_dut_bin_load_missing"}

    def _handle_load_flash(self, req: Dict[str, Any]) -> Dict[str, Any]:
        path = req.get("path")
        base = req.get("base")
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "flash_not_found"}
        if hasattr(self.xspdb, "api_dut_flash_load"):
            if base is not None and hasattr(self.xspdb, "flash_base"):
                try:
                    self.xspdb.flash_base = int(base)
                except Exception:
                    pass
            self.xspdb.api_dut_flash_load(path)
            return {"ok": True, "data": {"path": os.path.abspath(path)}}
        return {"ok": False, "error": "api_dut_flash_load_missing"}

    def _handle_wave(self, req: Dict[str, Any], enable: bool) -> Dict[str, Any]:
        path = req.get("path") or ""
        if enable:
            if hasattr(self.xspdb, "api_waveform_on"):
                self.xspdb.api_waveform_on(path)
                return {"ok": True, "data": {"path": path}}
            return {"ok": False, "error": "api_waveform_on_missing"}
        else:
            if hasattr(self.xspdb, "api_waveform_off"):
                self.xspdb.api_waveform_off()
                return {"ok": True}
            return {"ok": False, "error": "api_waveform_off_missing"}

    def _handle_get_regs(self, req: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        try:
            if hasattr(self.xspdb, "difftest_stat"):
                ints = [self.xspdb.xsp.GetFromU64Array(self.xspdb.difftest_stat.regs_int.value, i) for i in range(32)]
                fprs = [self.xspdb.xsp.GetFromF64Array(self.xspdb.difftest_stat.regs_fp.value, i) for i in range(32)]
            else:
                ints = list(getattr(self.xspdb, "iregs", [0]*32))
                fprs = list(getattr(self.xspdb, "fregs", [0.0]*32))
            out["iregs"] = ints
            out["fregs"] = fprs
        except Exception as ex:
            return {"ok": False, "error": f"get_regs_failed:{ex}"}
        return {"ok": True, "data": out}

    def _handle_get_csr(self, req: Dict[str, Any]) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        try:
            if hasattr(self.xspdb, "difftest_stat"):
                csr = self.xspdb.difftest_stat.csr
                data = {k: getattr(csr, k) for k in ("mstatus", "mcause", "mepc", "mtval", "mtvec", "privilegeMode",
                                                     "mie", "mip", "satp", "sstatus", "scause", "sepc", "stval", "stvec") if hasattr(csr, k)}
            elif hasattr(self.xspdb, "csr_data"):
                data = dict(self.xspdb.csr_data)
        except Exception as ex:
            return {"ok": False, "error": f"get_csr_failed:{ex}"}
        return {"ok": True, "data": data}

    def _handle_read_mem(self, req: Dict[str, Any]) -> Dict[str, Any]:
        addr = int(req.get("addr", 0))
        length = int(req.get("length", 0))
        if hasattr(self.xspdb, "api_read_bytes_from"):
            data = self.xspdb.api_read_bytes_from(addr, length)
            return {"ok": True, "data": {"addr": addr, "bytes": bytes(data).hex()}}
        return {"ok": False, "error": "api_read_bytes_from_missing"}

    def _handle_write_mem(self, req: Dict[str, Any]) -> Dict[str, Any]:
        addr = int(req.get("addr", 0))
        data_hex = req.get("bytes", "")
        width = int(req.get("width", 8))
        if not data_hex:
            return {"ok": False, "error": "bytes_missing"}
        raw = bytes.fromhex(data_hex)
        if hasattr(self.xspdb, "api_write_bytes"):
            self.xspdb.api_write_bytes(addr, raw)
            return {"ok": True, "data": {"addr": addr, "size": len(raw)}}
        return {"ok": False, "error": "api_write_bytes_missing"}

    def _handle_difftest_ref(self, req: Dict[str, Any]) -> Dict[str, Any]:
        path = req.get("path")
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "ref_not_found"}
        if hasattr(self.xspdb, "api_load_ref_so"):
            ok = self.xspdb.api_load_ref_so(path)
            return {"ok": bool(ok), "data": {"path": os.path.abspath(path)}}
        return {"ok": False, "error": "api_load_ref_so_missing"}

    def _handle_difftest_enable(self, req: Dict[str, Any]) -> Dict[str, Any]:
        on = bool(req.get("on", True))
        if hasattr(self.xspdb, "api_set_difftest_diff"):
            ok = self.xspdb.api_set_difftest_diff(on)
            return {"ok": bool(ok), "data": {"on": on}}
        return {"ok": False, "error": "api_set_difftest_diff_missing"}

    def _call_simple(self, name: str) -> Dict[str, Any]:
        fn = getattr(self.xspdb, name, None)
        if not callable(fn):
            return {"ok": False, "error": f"{name}_missing"}
        fn()
        return {"ok": True}

    # ---------- helpers ----------
    def _encode_from_fields(self, fields: Dict[str, Any], width: int):
        if width == 16 and hasattr(self.xspdb, "api_encode_instr16"):
            return self.xspdb.api_encode_instr16(fields)
        if width == 32 and hasattr(self.xspdb, "api_encode_instr32"):
            return self.xspdb.api_encode_instr32(fields)
        raise ValueError("encode not supported")

    def _queue_names(self, endpoint: str) -> tuple[str, str]:
        sid = _hash_id(os.path.abspath(endpoint))
        return (f"xspdb_agent_req_{sid}", f"xspdb_agent_resp_{sid}")

    def _write_discovery(self, path: str, req: str, resp: str, size: int):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"type": "shm", "req": req, "resp": resp, "size": size}, f)

    def _resolve_unix_paths(self, endpoint: str) -> tuple[str, str]:
        ep = os.path.abspath(endpoint)
        if ep.endswith(".json"):
            disc = ep
            sock = ep[:-5] + ".sock"
        else:
            sock = ep
            disc = ep + ".json"
        return disc, sock

    def _write_unix_discovery(self, disc: str, sock: str):
        os.makedirs(os.path.dirname(disc) or ".", exist_ok=True)
        with open(disc, "w") as f:
            json.dump({"type": "unix", "path": sock}, f)


def start_agent(xspdb: Any, cfg: Optional[AgentConfig] = None):
    cfg = cfg or AgentConfig.from_env()
    srv = AgentServer(xspdb, cfg).start()
    return srv


def load_and_start_agent(xspdb: Any, module: Optional[str], cfg: Optional[AgentConfig] = None):
    """Helper to dynamically load a module and start its start_agent if present."""
    if not module:
        return start_agent(xspdb, cfg)
    mod = _load_module(module)
    if hasattr(mod, "start_agent"):
        return mod.start_agent(xspdb, cfg)
    raise ImportError(f"Module {module} missing start_agent")


class UnixSocketServer:
    """Minimal Unix socket JSON-line server."""
    def __init__(self, sock_path: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]], stop_event: threading.Event):
        self.sock_path = os.path.abspath(sock_path)
        self.handler = handler
        self.stop_event = stop_event
        self.thread: Optional[threading.Thread] = None
        self._srv = None

    def start(self):
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except Exception:
            pass
        import socket
        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(self.sock_path)
            srv.listen(1)
            srv.settimeout(0.2)
            self._srv = srv
        except Exception as ex:
            # bubble up so caller can skip
            raise ex
        self.thread = threading.Thread(target=self._serve, name="XSPdbAgentUnix", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            try:
                # poke accept with a dummy connect
                import socket
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.1)
                s.connect(self.sock_path)
                s.close()
            except Exception:
                pass
            self.thread.join(timeout=1.0)
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except Exception:
            pass

    def _serve(self):
        srv = self._srv
        if srv is None:
            return
        while not self.stop_event.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            try:
                data = self._recv_all(conn)
                if not data:
                    conn.close()
                    continue
                try:
                    req = json.loads(data.decode("utf-8"))
                except Exception:
                    resp = {"ok": False, "error": "bad_json"}
                else:
                    resp = self.handler(req)
                out = (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")
                conn.sendall(out)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            srv.close()
        except Exception:
            pass

    def _recv_all(self, conn):
        chunks = []
        while True:
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            except Exception:
                break
        if not chunks:
            return b""
        return b"".join(chunks)
