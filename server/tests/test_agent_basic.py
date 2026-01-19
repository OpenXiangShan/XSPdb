#coding=utf-8

import json
import os
import tempfile
import time
import unittest

from server.agent import AgentConfig, AgentServer
from tools.shm_endpoint import ShmEndpoint


class DummyXspdb:
    def __init__(self):
        self.calls = []
        self.mem = bytearray(4096)
        self.mem_inited = True
        self.mem_base = 0x80000000
        self.flash_base = 0x10000000
        self.exec_bin_file = None
        self.flash_bin_file = None
        self.iregs = [0] * 32
        self.fregs = [0.0] * 32
        self.csr_data = {"mstatus": 0, "mepc": 0}
        # difftest_stat stub for agent fallback path
        class _Arr:
            def __init__(self, arr):
                self.value = arr
        class _Csr:
            def __init__(self, data):
                for k, v in data.items():
                    setattr(self, k, v)
        class _Stat:
            def __init__(self, iregs, fregs, csr):
                self.regs_int = _Arr(iregs)
                self.regs_fp = _Arr(fregs)
                self.csr = _Csr(csr)
        class _Xsp:
            @staticmethod
            def GetFromU64Array(arr, idx): return arr[idx]
            @staticmethod
            def GetFromF64Array(arr, idx): return arr[idx]
        self.difftest_stat = _Stat(self.iregs, self.fregs, self.csr_data)
        self.xsp = _Xsp()
        self.cycles = 0
        self.insts = 0
        self.commits = 0

    def api_echo(self, *a, **k):
        self.calls.append(("echo", a, k))
        return {"args": list(a), "kwargs": k}

    def api_write_bytes(self, addr, data):
        base = int(addr) - self.mem_base
        data = bytes(data)
        end = base + len(data)
        if end > len(self.mem):
            self.mem += b"\x00" * (end - len(self.mem))
        self.mem[base:end] = data
        return len(data)

    def api_read_bytes_from(self, addr, size):
        base = int(addr) - self.mem_base
        end = base + int(size)
        return bytes(self.mem[base:end])

    def api_dut_bin_load(self, path):
        with open(path, "rb") as f:
            data = f.read()
        self.api_write_bytes(self.mem_base, data)
        self.exec_bin_file = path
        self.mem_inited = True
        return True

    def api_dut_flash_load(self, path):
        self.flash_bin_file = path
        return True

    def api_waveform_on(self, path):
        self.wave_path = path

    def api_waveform_off(self):
        self.wave_path = None

    def api_set_flash_int_regs(self, regs):
        self.flash_iregs = dict(regs)

    def api_set_flash_float_regs(self, regs):
        self.flash_fregs = dict(regs)

    def api_get_flash_init_iregs(self):
        return list(getattr(self, "flash_iregs", {}).items())

    def api_get_flash_init_fregs(self):
        return list(getattr(self, "flash_fregs", {}).items())

    def api_all_data_to_asm(self, addr, length):
        base = addr - self.mem_base
        data = self.mem[base:base+length]
        return [(addr + i, f"{b:02x}", f"0x{b:02x}") for i, b in enumerate(data)]

    def api_step_dut(self, count, batch_cycle=200):
        self.cycles += count
        return count

    def api_xistep(self, count):
        self.insts += count
        return count

    def api_run_commits(self, count):
        self.commits += count
        return count

    def api_address_to_symbol(self, addr):
        return f"sym_{hex(addr)}"

    def api_decode_instr32(self, val):
        return {"raw": int(val), "asm": f"0x{int(val):08x}"}

    def api_encode_instr32(self, fields):
        return int(fields.get("raw", 0))


class AgentBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        os.environ["XSPDB_CKPT_DIR"] = self.tmpdir.name

    def _start_agent(self):
        ep = os.path.join(self.tmpdir.name, "agent.shm")
        cfg = AgentConfig(endpoint=ep)
        try:
            srv = AgentServer(DummyXspdb(), cfg).start()
        except Exception as ex:
            self.skipTest(f"shared memory not available: {ex}")
        self.addCleanup(lambda: srv.stop(unlink=True))
        return ep, srv

    def _start_agent_unix(self):
        ep = os.path.join(self.tmpdir.name, "agent_unix.json")
        cfg = AgentConfig(endpoint=ep, transport="unix")
        try:
            srv = AgentServer(DummyXspdb(), cfg).start()
        except Exception as ex:
            self.skipTest(f"unix transport not available: {ex}")
        self.addCleanup(lambda: srv.stop(unlink=True))
        return ep, srv

    def _connect(self, ep_path: str):
        ep = ShmEndpoint(ep_path).open(timeout_sec=2)
        self.addCleanup(ep.close)
        return ep

    def test_ping_and_call_api(self):
        ep_path, _srv = self._start_agent()
        ep = self._connect(ep_path)
        r = ep.request({"cmd": "ping"})
        self.assertTrue(r.get("ok"))
        r = ep.request({"cmd": "call_api", "api": "api_echo", "args": [1, 2], "kwargs": {"k": "v"}})
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["data"]["args"], [1, 2])
        self.assertEqual(r["data"]["kwargs"]["k"], "v")

    def test_unix_transport_ping(self):
        ep_path, _srv = self._start_agent_unix()
        # Discovery json should contain socket path
        try:
            with open(ep_path, "r") as f:
                info = json.load(f)
            sock = info.get("path")
        except Exception as ex:
            self.skipTest(f"unix discovery not ready: {ex}")
        if not sock:
            self.skipTest("unix discovery missing path")
        from server.mcp_server import UnixEndpoint
        ue = UnixEndpoint(sock)
        r = ue.request({"cmd": "ping"})
        self.assertTrue(r.get("ok"))

    def test_patch_and_dump(self):
        ep_path, _srv = self._start_agent()
        ep = self._connect(ep_path)
        payload_hex = "01020304"
        base = 0x80000000
        r = ep.request({"cmd": "patch_instr", "addr": base, "bytes": payload_hex})
        self.assertTrue(r.get("ok"))
        out_path = os.path.join(self.tmpdir.name, "dump.bin")
        r = ep.request({"cmd": "dump_region", "path": out_path, "regions": [[base, 4]]})
        self.assertTrue(r.get("ok"))
        with open(out_path, "rb") as f:
            data = f.read()
        self.assertEqual(data, bytes.fromhex(payload_hex))

    def test_load_bin_ready_to_run(self):
        bin_path = os.path.join(os.getcwd(), "ready-to-run", "microbench.bin")
        if not os.path.exists(bin_path):
            self.skipTest("ready-to-run/microbench.bin not found")
        ep_path, _srv = self._start_agent()
        ep = self._connect(ep_path)
        r = ep.request({"cmd": "load_bin", "path": bin_path})
        self.assertTrue(r.get("ok"))
        prefix = open(bin_path, "rb").read(16)
        r = ep.request({"cmd": "read_mem", "addr": 0x80000000, "length": 16})
        self.assertTrue(r.get("ok"))
        self.assertEqual(bytes.fromhex(r["data"]["bytes"]), prefix)
        r = ep.request({"cmd": "disasm", "addr": 0x80000000, "length": 8})
        self.assertTrue(r.get("ok"))
        self.assertGreater(len(r["data"]), 0)

    def test_flash_regs(self):
        ep_path, _srv = self._start_agent()
        ep = self._connect(ep_path)
        r = ep.request({"cmd": "set_flash_regs", "iregs": {"mpc": 0x80000000}, "fregs": {"ft0": 1}})
        self.assertTrue(r.get("ok"))
        r = ep.request({"cmd": "get_flash_regs"})
        self.assertTrue(r.get("ok"))


if __name__ == "__main__":
    unittest.main()
