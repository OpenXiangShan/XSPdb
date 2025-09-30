#!/usr/bin/env python3
#coding=utf-8

"""
XSPdb Socket Wrapper (skeleton)

All previous socket-related implementations have been removed, leaving only
minimal class scaffolding for cmdapi and tty. Implement your own logic here.
"""

import argparse
import asyncio
import os
import sys
import json
import hashlib
import struct
from multiprocessing import shared_memory
import fcntl
import termios
import signal
import traceback
import time


_HDR_SIZE = 16  # 8 bytes head, 8 bytes tail
_WRAP_MARK = 0xFFFFFFFF  # special marker to indicate wrap to 0


class SharedMsgQueue:
    """A simple single-producer/single-consumer ring buffer using POSIX shared memory.

    Layout:
      [0:8]   head (uint64, bytes written)
      [8:16]  tail (uint64, bytes read)
      [16:]   data region of capacity = size - 16

    Frames are stored as [len:u32][payload...]. If a frame cannot fit contiguously,
    a wrap marker (len=0xFFFFFFFF) is placed, and the frame is written at offset 0.
    """

    def __init__(self, name: str, size: int = 1 << 20, create: bool = False):
        if size < (_HDR_SIZE + 8):
            raise ValueError("size too small for SharedMsgQueue")
        self.name = name
        self.size = size
        self.capacity = size - _HDR_SIZE
        self.shm = None
        # Robust create: if already exists (leftover from crash), unlink and recreate
        if create:
            try:
                self.shm = shared_memory.SharedMemory(name=name, create=True, size=size)
            except FileExistsError:
                # Attempt to cleanup leftover and recreate
                try:
                    tmp = shared_memory.SharedMemory(name=name, create=False)
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                    try:
                        tmp.close()
                    except Exception:
                        pass
                except FileNotFoundError:
                    pass
                # Retry create; if still exists, just open existing
                try:
                    self.shm = shared_memory.SharedMemory(name=name, create=True, size=size)
                except FileExistsError:
                    self.shm = shared_memory.SharedMemory(name=name, create=False)
            except Exception:
                # Fallback: open existing if any
                try:
                    self.shm = shared_memory.SharedMemory(name=name, create=False)
                except Exception as ex:
                    raise ex
        else:
            self.shm = shared_memory.SharedMemory(name=name, create=False)
        self.buf = self.shm.buf
        if create:
            # Initialize head/tail to 0
            self._write_u64(0, 0)
            self._write_u64(8, 0)

    def close(self):
        try:
            self.shm.close()
        except Exception:
            pass

    def unlink(self):
        try:
            self.shm.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    # --- head/tail helpers ---
    def _read_u64(self, off: int) -> int:
        return struct.unpack_from('<Q', self.buf, off)[0]

    def _write_u64(self, off: int, val: int) -> None:
        struct.pack_into('<Q', self.buf, off, int(val) & 0xFFFFFFFFFFFFFFFF)

    def _read_head(self) -> int:
        return self._read_u64(0)

    def _read_tail(self) -> int:
        return self._read_u64(8)

    def _write_head(self, v: int) -> None:
        self._write_u64(0, v)

    def _write_tail(self, v: int) -> None:
        self._write_u64(8, v)

    # --- queue ops ---
    def _space_used(self, head: int, tail: int) -> int:
        return head - tail if head >= tail else (head + self.capacity - tail)

    def _space_free(self, head: int, tail: int) -> int:
        return self.capacity - self._space_used(head, tail)

    def push(self, payload: bytes) -> bool:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")
        n = len(payload)
        if n > (1 << 31) - 8:
            return False
        frame_len = 4 + n
        head = self._read_head()
        tail = self._read_tail()
        if self._space_free(head, tail) < frame_len:
            return False
        head_off = head % self.capacity
        # Check if fits contiguously
        contiguous = self.capacity - head_off
        if contiguous < frame_len:
            # Need to insert wrap marker and wrap to 0
            if contiguous >= 4:
                struct.pack_into('>I', self.buf, _HDR_SIZE + head_off, _WRAP_MARK)
                head = head + 4
                head_off = (head % self.capacity)
            # wrap
            head = head + (self.capacity - head_off)
            head_off = 0
        # Write length and payload
        struct.pack_into('>I', self.buf, _HDR_SIZE + head_off, n)
        self.buf[_HDR_SIZE + head_off + 4 : _HDR_SIZE + head_off + 4 + n] = payload
        head += frame_len
        self._write_head(head)
        return True

    def pop(self) -> bytes | None:
        head = self._read_head()
        tail = self._read_tail()
        if tail == head:
            return None
        tail_off = tail % self.capacity
        # Check wrap marker
        if self.capacity - tail_off >= 4:
            (length,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + tail_off)
            if length == _WRAP_MARK:
                tail += 4
                tail_off = (tail % self.capacity)
                # wrapped, read new length after wrap
                if tail == head:
                    self._write_tail(tail)
                    return None
                (length,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + tail_off)
        else:
            # Not enough space to read length, implicit wrap
            tail += (self.capacity - tail_off)
            tail_off = 0
            if tail == head:
                self._write_tail(tail)
                return None
            (length,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + tail_off)

        if length > self.capacity - 4:
            # Corruption guard: drop buffer
            self._write_tail(head)
            return None
        need = 4 + length
        contiguous = self.capacity - tail_off
        if contiguous < need:
            # Shouldn't happen due to wrap marker on push, but guard
            # Treat as implicit wrap
            tail += (self.capacity - tail_off)
            tail_off = 0
            if tail == head:
                self._write_tail(tail)
                return None
            (length,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + tail_off)
            need = 4 + length
        data = bytes(self.buf[_HDR_SIZE + tail_off + 4 : _HDR_SIZE + tail_off + 4 + length])
        tail += need
        self._write_tail(tail)
        return data

    async def apop(self, interval_sec: float = 0.01) -> bytes | None:
        msg = self.pop()
        if msg is not None:
            return msg
        await asyncio.sleep(interval_sec)
        return self.pop()

    async def iter_messages(self, interval_sec: float = 0.01):
        while True:
            msg = self.pop()
            if msg is None:
                await asyncio.sleep(interval_sec)
                continue
            yield msg


class CmdApiServer:
    """Command API server implemented via shared memory message queues.

    Two queues are used:
      - req: client -> server (JSON lines)
      - resp: server -> client (JSON lines)

    A small sidecar JSON file is written at `sock_path` for discovery:
      {"type":"shm","req":"<name>","resp":"<name>","size":<bytes>}
    """
    def __init__(self, sock_path, state, size: int = 1 << 16):
        self.sock_path = os.path.abspath(sock_path)
        self.state = state
        self.size = size
        self._id = self._derive_id(self.sock_path)
        self.req_q: SharedMsgQueue | None = None
        self.resp_q: SharedMsgQueue | None = None
        self._task: asyncio.Task | None = None
        self._sig_handlers_installed: bool = False
        # path for SIGCONT recovery log
        try:
            base = getattr(state, 'ckpt_dir', os.path.dirname(self.sock_path))
        except Exception:
            base = os.path.dirname(self.sock_path)
        self._sig_log = os.path.join(base, 'wrapper.sigcont.log')

    def _elog_sigcont(self, msg: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._sig_log), exist_ok=True)
            ts = time.strftime('%Y-%m-%d %H:%M:%S')
            with open(self._sig_log, 'a') as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    @staticmethod
    def _derive_id(key: str) -> str:
        return hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]

    def _req_name(self) -> str:
        return f"xspdb_cmd_req_{self._id}"

    def _resp_name(self) -> str:
        return f"xspdb_cmd_resp_{self._id}"

    async def start(self):
        os.makedirs(os.path.dirname(self.sock_path), exist_ok=True)
        # Create queues
        self.req_q = SharedMsgQueue(self._req_name(), size=self.size, create=True)
        self.resp_q = SharedMsgQueue(self._resp_name(), size=self.size, create=True)
        # Write discovery file
        try:
            with open(self.sock_path, 'w') as f:
                json.dump({
                    'type': 'shm',
                    'req': self._req_name(),
                    'resp': self._resp_name(),
                    'size': self.size
                }, f)
        except Exception:
            pass
        # Install SIGCONT callback to reconnect queues after CRIU restore
        if not self._sig_handlers_installed:
            try:
                loop = asyncio.get_running_loop()
                self._elog_sigcont('installing signal handlers for SIGCONT/SIGUSR1')
                loop.add_signal_handler(signal.SIGCONT, lambda: asyncio.create_task(self._on_sigcont()))
                self._sig_handlers_installed = True
                # Optional: also allow SIGUSR1 to trigger reconnect in dev
                try:
                    loop.add_signal_handler(signal.SIGUSR1, lambda: asyncio.create_task(self._on_sigcont()))
                except Exception:
                    pass
            except (NotImplementedError, RuntimeError):
                # Fallback using signal.signal
                def _h(signum, frame):
                    try:
                        asyncio.get_event_loop().create_task(self._on_sigcont())
                    except Exception:
                        pass
                try:
                    self._elog_sigcont('fallback install SIGCONT via signal.signal')
                    signal.signal(signal.SIGCONT, _h)
                except Exception:
                    pass
        # Start serving loop
        self._task = asyncio.create_task(self._serve_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Cleanup shm and discovery
        if self.req_q:
            self.req_q.close(); self.req_q.unlink()
        if self.resp_q:
            self.resp_q.close(); self.resp_q.unlink()
        try:
            if os.path.exists(self.sock_path):
                os.remove(self.sock_path)
        except Exception:
            pass

    async def _serve_loop(self):
        assert self.req_q and self.resp_q
        while True:
            raw = await self.req_q.apop()
            if not raw:
                continue
            try:
                req = json.loads(raw.decode('utf-8', errors='ignore'))
            except Exception:
                await self._respond({"ok": False, "error": "bad_json"})
                continue
            cmd = req.get('cmd') if isinstance(req, dict) else None
            args = req.get('args') if isinstance(req, dict) else {}
            res = await self._dispatch(cmd, args)
            await self._respond(res)

    async def _on_sigcont(self):
        # Reconnect SharedMsgQueue channels when receiving SIGCONT
        try:
            self._elog_sigcont('SIGCONT received: begin reconnect cmdapi shm')
            if getattr(self.state, '_elog', None):
                try:
                    self.state._elog(os.path.join(getattr(self.state, 'ckpt_dir', '.'), 'wrapper.cmd.log'), 'sigcont: reconnect cmdapi shm')
                except Exception:
                    pass
            await self._reconnect_shm()
        except Exception as ex:
            self._elog_sigcont('SIGCONT reconnect error: ' + str(ex))
            try:
                if getattr(self.state, '_elog', None):
                    self.state._elog(os.path.join(getattr(self.state, 'ckpt_dir', '.'), 'wrapper.cmd.log'), 'sigcont: reconnect failed: ' + str(ex) + '\n' + traceback.format_exc())
            except Exception:
                pass

    async def _reconnect_shm(self):
        # Stop current serve loop to safely swap queue instances
        t = self._task
        self._task = None
        if t is not None:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        # Close old queues without unlinking
        old_req, old_resp = self.req_q, self.resp_q
        self.req_q = None
        self.resp_q = None
        try:
            self._elog_sigcont('serve loop cancelled; closing old queues')
        except Exception:
            pass
        try:
            if old_req:
                old_req.close()
        except Exception:
            pass
        try:
            if old_resp:
                old_resp.close()
        except Exception:
            pass
        # Re-open existing segments first; fall back to create if missing
        req_name = self._req_name()
        resp_name = self._resp_name()
        try:
            self._elog_sigcont(f'open shm names: req={req_name} resp={resp_name} size={self.size}')
        except Exception:
            pass
        try:
            self.req_q = SharedMsgQueue(req_name, size=self.size, create=False)
            try: self._elog_sigcont('opened existing req queue');
            except Exception: pass
        except Exception as ex:
            try: self._elog_sigcont('open existing req failed; creating: ' + str(ex))
            except Exception: pass
            self.req_q = SharedMsgQueue(req_name, size=self.size, create=True)
        try:
            self.resp_q = SharedMsgQueue(resp_name, size=self.size, create=False)
            try: self._elog_sigcont('opened existing resp queue');
            except Exception: pass
        except Exception as ex:
            try: self._elog_sigcont('open existing resp failed; creating: ' + str(ex))
            except Exception: pass
            self.resp_q = SharedMsgQueue(resp_name, size=self.size, create=True)
        # Refresh discovery file
        try:
            with open(self.sock_path, 'w') as f:
                json.dump({'type': 'shm', 'req': req_name, 'resp': resp_name, 'size': self.size}, f)
            try: self._elog_sigcont('discovery file refreshed');
            except Exception: pass
        except Exception as ex:
            try: self._elog_sigcont('refresh discovery failed: ' + str(ex))
            except Exception: pass
        # Restart serve loop
        try: self._elog_sigcont('restarting serve loop')
        except Exception: pass
        self._task = asyncio.create_task(self._serve_loop())

    async def _respond(self, obj: dict):
        assert self.resp_q
        data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        # Retry a few times if full
        for _ in range(50):
            if self.resp_q.push(data):
                return
            await asyncio.sleep(0.01)

    async def _dispatch(self, cmd: str | None, args: dict | None) -> dict:
        S = self.state
        try:
            if cmd == 'ping':
                return {"ok": True, "pong": True}
            if cmd == 'set_winsize':
                ok = await S.set_winsize_async(int(args.get('rows', 0)), int(args.get('cols', 0)))
                return {"ok": bool(ok)}
            if cmd == 'inject':
                data = args.get('data', '')
                ok = await S.inject_async(data)
                return {"ok": bool(ok)}
            if cmd == 'pause':
                ok = await S.pause(); return {"ok": bool(ok)}
            if cmd == 'resume':
                ok = await S.resume(); return {"ok": bool(ok)}
            if cmd == 'detach':
                ok = await S.io_detach(); return {"ok": bool(ok)}
            if cmd == 'get_target_pid':
                pid = S.get_target_pid()
                return {"ok": pid is not None, "pid": pid}
            if cmd == 'adopt':
                pid = int(args.get('pid', 0))
                ok = S.adopt_target_pid(pid)
                return {"ok": bool(ok)}
            if cmd == 'stop_target':
                ok = S.stop_target()
                return {"ok": bool(ok)}
            if cmd == 'reopen_tty':
                ok = await S.reopen_tty(); return {"ok": bool(ok)}
            if cmd == 'shutdown':
                # Signal shutdown and ack
                await S.shutdown()
                return {"ok": True}
            return {"ok": False, "error": "unknown_cmd"}
        except SystemExit as e:
            # propagate shutdown
            return {"ok": True, "exit": getattr(e, 'code', 0)}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}


class TtyForwardServer:
    """TTY forwarder implemented via shared memory.

    Two queues are used:
      - in: client -> server (keystrokes/input as bytes)
      - out: server -> client (pty output as bytes)

    Discovery JSON is written to `sock_path`:
      {"type":"shm","in":"<name>","out":"<name>","size":<bytes>}
    """
    def __init__(self, sock_path, master_fd=None, size: int = 1 << 20, state=None):
        self.sock_path = os.path.abspath(sock_path)
        self.master_fd = master_fd
        self.size = size
        self.state = state
        self._id = self._derive_id(self.sock_path)
        self.in_q: SharedMsgQueue | None = None
        self.out_q: SharedMsgQueue | None = None
        self._task_in: asyncio.Task | None = None
        self._task_out: asyncio.Task | None = None
        self._loop_reader_installed: bool = False
        self._paused: bool = False
        self._sig_handlers_installed: bool = False
        # debug logs
        ckpt = getattr(state, 'ckpt_dir', os.path.dirname(self.sock_path)) if state is not None else os.path.dirname(self.sock_path)
        self._log_in = os.path.join(ckpt, 'wrapper.tty.stdin.log')
        self._log_out = os.path.join(ckpt, 'wrapper.tty.stdout.log')

    @staticmethod
    def _derive_id(key: str) -> str:
        return hashlib.sha1((key + ":tty").encode('utf-8')).hexdigest()[:16]

    def _in_name(self) -> str:
        return f"xspdb_tty_in_{self._id}"

    def _out_name(self) -> str:
        return f"xspdb_tty_out_{self._id}"

    async def start(self):
        os.makedirs(os.path.dirname(self.sock_path), exist_ok=True)
        self.in_q = SharedMsgQueue(self._in_name(), size=self.size, create=True)
        self.out_q = SharedMsgQueue(self._out_name(), size=self.size, create=True)
        try:
            with open(self.sock_path, 'w') as f:
                json.dump({
                    'type': 'shm',
                    'in': self._in_name(),
                    'out': self._out_name(),
                    'size': self.size
                }, f)
        except Exception:
            pass
        # Install SIGCONT callback to reconnect TTY queues after CRIU restore
        if not self._sig_handlers_installed:
            try:
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(signal.SIGCONT, lambda: asyncio.create_task(self._on_sigcont()))
                self._sig_handlers_installed = True
                try:
                    loop.add_signal_handler(signal.SIGUSR1, lambda: asyncio.create_task(self._on_sigcont()))
                except Exception:
                    pass
            except (NotImplementedError, RuntimeError):
                def _h(signum, frame):
                    try:
                        asyncio.get_event_loop().create_task(self._on_sigcont())
                    except Exception:
                        pass
                try:
                    signal.signal(signal.SIGCONT, _h)
                except Exception:
                    pass
        # Consume input from client in background
        self._task_in = asyncio.create_task(self._consume_input_loop())
        # Produce output from PTY master to client
        if self.master_fd is not None:
            try:
                fcntl.fcntl(self.master_fd, fcntl.F_SETFL, os.O_NONBLOCK)
            except Exception:
                pass
            # Prefer event-driven read for low latency
            loop = asyncio.get_running_loop()
            try:
                loop.add_reader(self.master_fd, self._on_master_readable)
                self._loop_reader_installed = True
                try:
                    if self.state and getattr(self.state, '_elog', None):
                        self.state._elog(os.path.join(self.state.ckpt_dir, 'wrapper.tty.log'), 'start: add_reader installed')
                except Exception:
                    pass
            except Exception as ex:
                # Any failure: fall back to polling loop
                try:
                    if self.state and getattr(self.state, '_elog', None):
                        self.state._elog(os.path.join(self.state.ckpt_dir, 'wrapper.tty.log'), 'start: add_reader failed, fallback loop: ' + str(ex))
                except Exception:
                    pass
                self._task_out = asyncio.create_task(self._produce_output_loop())

    async def _on_sigcont(self):
        # Recreate TTY shm queues and reinstall hooks
        try:
            if self.state and getattr(self.state, '_elog', None):
                self.state._elog(os.path.join(self.state.ckpt_dir, 'wrapper.tty.log'), 'sigcont: reconnect tty shm')
        except Exception:
            pass
        try:
            await self._reconnect_tty_shm()
        except Exception:
            pass

    async def _reconnect_tty_shm(self):
        # Cancel tasks
        if self._task_in:
            self._task_in.cancel()
            try:
                await self._task_in
            except asyncio.CancelledError:
                pass
            self._task_in = None
        if self._task_out:
            self._task_out.cancel()
            try:
                await self._task_out
            except asyncio.CancelledError:
                pass
            self._task_out = None
        # Remove reader hook
        if self._loop_reader_installed and self.master_fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.master_fd)
            except Exception:
                pass
            self._loop_reader_installed = False
        # Close queues without unlink
        try:
            if self.in_q: self.in_q.close()
        except Exception:
            pass
        try:
            if self.out_q: self.out_q.close()
        except Exception:
            pass
        self.in_q = None
        self.out_q = None
        # Recreate shm queues and rewrite discovery
        in_name = self._in_name(); out_name = self._out_name()
        try:
            if self.state and getattr(self.state, '_elog', None):
                self.state._elog(os.path.join(self.state.ckpt_dir, 'wrapper.tty.log'), f'reconnect tty shm create in={in_name} out={out_name}')
        except Exception:
            pass
        self.in_q = SharedMsgQueue(in_name, size=self.size, create=True)
        self.out_q = SharedMsgQueue(out_name, size=self.size, create=True)
        try:
            with open(self.sock_path, 'w') as f:
                json.dump({'type': 'shm', 'in': in_name, 'out': out_name, 'size': self.size}, f)
        except Exception:
            pass
        # Emit a small marker to out queue to verify end-to-end path
        try:
            marker = b"\r\n[XSPDB] TTY reconnected\r\n"
            for _ in range(10):
                if self.out_q.push(marker):
                    break
                await asyncio.sleep(0.01)
        except Exception:
            pass
        # Reinstall reader task/hook
        self._task_in = asyncio.create_task(self._consume_input_loop())
        if self.master_fd is not None:
            try:
                fcntl.fcntl(self.master_fd, fcntl.F_SETFL, os.O_NONBLOCK)
            except Exception:
                pass
            loop = asyncio.get_running_loop()
            try:
                loop.add_reader(self.master_fd, self._on_master_readable)
                self._loop_reader_installed = True
                try:
                    if self.state and getattr(self.state, '_elog', None):
                        self.state._elog(os.path.join(self.state.ckpt_dir, 'wrapper.tty.log'), 'reconnect: add_reader reinstalled')
                except Exception:
                    pass
            except Exception as ex:
                try:
                    if self.state and getattr(self.state, '_elog', None):
                        self.state._elog(os.path.join(self.state.ckpt_dir, 'wrapper.tty.log'), 'reconnect: add_reader failed, fallback loop: ' + str(ex))
                except Exception:
                    pass
                self._task_out = asyncio.create_task(self._produce_output_loop())

    async def stop(self):
        if self._task_in:
            self._task_in.cancel()
            try:
                await self._task_in
            except asyncio.CancelledError:
                pass
        if self._task_out:
            self._task_out.cancel()
            try:
                await self._task_out
            except asyncio.CancelledError:
                pass
        if self.in_q:
            self.in_q.close(); self.in_q.unlink()
        if self._loop_reader_installed and self.master_fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.master_fd)
            except Exception:
                pass
        if self.out_q:
            self.out_q.close(); self.out_q.unlink()
        try:
            if os.path.exists(self.sock_path):
                os.remove(self.sock_path)
        except Exception:
            pass

    async def _consume_input_loop(self):
        assert self.in_q
        while True:
            data = await self.in_q.apop()
            if not data:
                continue
            # Write to PTY master (keyboard to target)
            if self.master_fd is not None and not self._paused:
                try:
                    self._blog(self._log_in, b"in "+self._snip(data))
                    view = memoryview(data)
                    while view:
                        n = os.write(self.master_fd, view[:4096])
                        view = view[n:]
                except BlockingIOError:
                    await asyncio.sleep(0.005)
                except Exception:
                    await asyncio.sleep(0.005)
            else:
                self._blog(self._log_in, b"in.dropped, masterfd={}, paused={}".format(self.master_fd, self._paused).encode('utf-8'))

    async def write_output(self, data: bytes) -> bool:
        assert self.out_q
        if not isinstance(data, (bytes, bytearray, memoryview)):
            data = str(data).encode('utf-8', errors='ignore')
        for _ in range(50):
            if self.out_q.push(bytes(data)):
                return True
            await asyncio.sleep(0.01)
        return False

    async def _produce_output_loop(self):
        assert self.out_q and self.master_fd is not None
        while True:
            try:
                chunk = os.read(self.master_fd, 8192)
                if not chunk:
                    await asyncio.sleep(0.01)
                    continue
                # push out; if full, retry a bit
                if not self._paused:
                    self._blog(self._log_out, b"out "+self._snip(chunk))
                    for _ in range(50):
                        if self.out_q.push(chunk):
                            break
                        await asyncio.sleep(0.005)
            except BlockingIOError:
                await asyncio.sleep(0.005)
            except Exception:
                await asyncio.sleep(0.01)

    def _on_master_readable(self):
        try:
            if not self.out_q or self.master_fd is None:
                return
            while True:
                try:
                    chunk = os.read(self.master_fd, 8192)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                # Non-blocking push; if full, drop oldest by advancing tail?
                # For simplicity, retry a few times then give up to avoid stalling
                if not self._paused:
                    self._blog(self._log_out, b"out "+self._snip(chunk))
                    for _ in range(5):
                        if self.out_q.push(chunk):
                            break
        except Exception:
            pass

    def _blog(self, path: str, data: bytes):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'ab') as f:
                f.write(data + b"\n")
        except Exception:
            pass

    def _snip(self, data: bytes, limit: int = 256) -> bytes:
        try:
            b = bytes(data[:limit])
            # include length prefix for clarity
            return (f"[{len(data)}] ").encode('ascii') + b
        except Exception:
            return data[:limit]

    async def pause(self):
        self._paused = True
        return True

    async def resume(self):
        self._paused = False
        return True


class WrapperState:
    def __init__(self, ckpt_dir, target_argv, cmd_sock, tty_sock):
        self.ckpt_dir = os.path.abspath(ckpt_dir)
        self.target = list(target_argv)
        self.cmd_sock = os.path.abspath(cmd_sock)
        self.tty_sock = os.path.abspath(tty_sock)
        self.cmd_server = None
        self.tty_server = None
        self._shutdown = asyncio.Event()
        # simple logs for demo
        self._log_cmd = os.path.join(self.ckpt_dir, 'wrapper.cmd.log')
        self._log_tty = os.path.join(self.ckpt_dir, 'wrapper.tty.log')
        self._child_pid: int | None = None
        self._pty_master: int | None = None
        self._pty_slave: int | None = None
        self._sig_handlers_installed = False

    async def set_winsize_async(self, rows, cols):
        self._elog(self._log_cmd, f"set_winsize rows={rows} cols={cols}")
        try:
            if self._pty_master is not None:
                winsz = struct.pack('HHHH', int(rows), int(cols), 0, 0)
                fcntl.ioctl(self._pty_master, termios.TIOCSWINSZ, winsz)
                if self._child_pid:
                    os.kill(self._child_pid, signal.SIGWINCH)
                return True
        except Exception:
            pass
        return False

    async def inject_async(self, lines):
        self._elog(self._log_cmd, f"inject {repr(lines)[:120]}")
        # Inject into PTY master (simulate user input)
        try:
            data = lines if isinstance(lines, (bytes, bytearray)) else str(lines).encode('utf-8', errors='ignore')
            if self._pty_master is not None:
                view = memoryview(data)
                while view:
                    n = os.write(self._pty_master, view[:4096])
                    view = view[n:]
                return True
        except Exception:
            pass
        return False

    async def io_detach(self):
        self._elog(self._log_cmd, "io_detach")
        return True

    async def pause(self):
        self._elog(self._log_cmd, "pause")
        # Pause PTY forwarding and stop child
        try:
            if self.tty_server:
                await self.tty_server.pause()
        except Exception:
            pass
        try:
            if self._child_pid:
                os.kill(self._child_pid, signal.SIGSTOP)
        except Exception:
            pass
        return True

    async def resume(self):
        self._elog(self._log_cmd, "resume")
        # Continue child and resume PTY forwarding
        try:
            if self._child_pid:
                os.kill(self._child_pid, signal.SIGCONT)
        except Exception:
            pass
        try:
            if self.tty_server:
                await self.tty_server.resume()
        except Exception:
            pass
        return True

    async def shutdown(self):
        self._elog(self._log_cmd, "shutdown")
        # terminate child and cleanup
        try:
            if self._child_pid:
                # Try killing the whole process group if child created a new session
                try:
                    os.killpg(self._child_pid, signal.SIGTERM)
                except Exception:
                    os.kill(self._child_pid, signal.SIGTERM)
        except Exception:
            pass
        self._shutdown.set()
        raise SystemExit(0)

    def _install_signal_handlers(self):
        if self._sig_handlers_installed:
            return
        try:
            loop = asyncio.get_running_loop()
            def _on_term():
                try:
                    self._elog(self._log_cmd, 'sigterm: received, begin graceful shutdown')
                except Exception:
                    pass
                try:
                    asyncio.create_task(self.shutdown())
                except Exception:
                    # As a fallback, set the event to allow run() to proceed cleanup
                    try:
                        self._shutdown.set()
                    except Exception:
                        pass
            try:
                loop.add_signal_handler(signal.SIGTERM, _on_term)
            except NotImplementedError:
                signal.signal(signal.SIGTERM, lambda s, f: _on_term())
            # Also handle SIGINT for quick stop in dev
            try:
                loop.add_signal_handler(signal.SIGINT, _on_term)
            except Exception:
                try:
                    signal.signal(signal.SIGINT, lambda s, f: _on_term())
                except Exception:
                    pass
            self._sig_handlers_installed = True
            try:
                self._elog(self._log_cmd, 'signals: SIGTERM/SIGINT handlers installed')
            except Exception:
                pass
        except Exception:
            pass

    # ---- target pid helpers for CRIU coordination ----
    def get_target_pid(self) -> int | None:
        return self._child_pid

    def adopt_target_pid(self, pid: int) -> bool:
        try:
            self._child_pid = int(pid)
            return True
        except Exception:
            return False

    def stop_target(self) -> bool:
        try:
            if self._child_pid:
                os.kill(self._child_pid, signal.SIGTERM)
            return True
        except Exception:
            return False

    def _elog(self, path, msg):
        os.makedirs(self.ckpt_dir, exist_ok=True)
        try:
            with open(path, 'a') as f:
                f.write(msg + "\n")
        except Exception:
            pass

    async def run(self):
        # Create PTY and spawn target, then start shared memory servers
        os.makedirs(self.ckpt_dir, exist_ok=True)
        await self._spawn_target_with_pty()
        self.cmd_server = CmdApiServer(self.cmd_sock, self)
        self.tty_server = TtyForwardServer(self.tty_sock, self._pty_master, state=self)
        self._elog(self._log_cmd, f"cmdapi.start {self.cmd_sock}")
        self._elog(self._log_tty, f"tty.start {self.tty_sock}")
        # Install termination signal handlers for clean shutdown
        self._install_signal_handlers()
        await self.cmd_server.start()
        await self.tty_server.start()
        # Wait for shutdown
        await self._shutdown.wait()
        # Cleanup
        try:
            await self.tty_server.stop()
        except Exception:
            pass
        try:
            await self.cmd_server.stop()
        except Exception:
            pass
        # Ensure child is gone
        try:
            if self._child_pid:
                try:
                    os.kill(self._child_pid, 0)
                    # Still alive -> force kill PGID then PID
                    try:
                        os.killpg(self._child_pid, signal.SIGKILL)
                    except Exception:
                        os.kill(self._child_pid, signal.SIGKILL)
                except Exception:
                    pass
        except Exception:
            pass

    async def reopen_tty(self):
        # Stop current TTY server and recreate shm queues and reader hooks
        try:
            if self.tty_server:
                await self.tty_server.stop()
        except Exception:
            pass
        try:
            self.tty_server = TtyForwardServer(self.tty_sock, self._pty_master, state=self)
            self._elog(self._log_tty, f"tty.reopen {self.tty_sock}")
            await self.tty_server.start()
            return True
        except Exception:
            return False

    async def _spawn_target_with_pty(self):
        if not self.target:
            return
        master_fd, slave_fd = os.openpty()
        self._pty_master, self._pty_slave = master_fd, slave_fd
        # Set raw-ish mode on master (optional); target config its own tty
        # Do not force raw/cooked here; let target configure its TTY
        pid = os.fork()
        if pid == 0:
            # Child: create new session, make slave controlling tty, dup to stdio, exec target
            try:
                os.setsid()
                os.close(master_fd)
                # Re-open slave to get it as controlling tty if needed
                slave_name = os.ttyname(slave_fd)
                # Ensure O_RDWR
                sfd = os.open(slave_name, os.O_RDWR)
                try:
                    # Make controlling tty if supported
                    try:
                        fcntl.ioctl(sfd, termios.TIOCSCTTY, 0)
                    except Exception:
                        pass
                    os.dup2(sfd, 0)
                    os.dup2(sfd, 1)
                    os.dup2(sfd, 2)
                finally:
                    try:
                        if sfd not in (0,1,2):
                            os.close(sfd)
                    except Exception:
                        pass
                if slave_fd not in (0,1,2):
                    try: os.close(slave_fd)
                    except Exception: pass
                os.execvp(self.target[0], self.target)
            except Exception:
                os._exit(127)
        else:
            # Parent
            self._child_pid = pid
            try:
                os.close(slave_fd)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="XSPdb Socket Wrapper (skeleton)")
    ap.add_argument("--cmd-sock", default=".xspdb_ckpt/cmdapi.shm", help="command API discovery path (.shm JSON)")
    ap.add_argument("--ckpt-dir", default=".xspdb_ckpt", help="checkpoint directory")
    ap.add_argument("--data-sock", default=".xspdb_ckpt/tty.shm", help="TTY discovery path (.shm JSON)")
    ap.add_argument("target", nargs=argparse.REMAINDER, help="target command after --")
    args = ap.parse_args()

    # argparse.REMAINDER may include a leading '--' depending on caller; strip it.
    if args.target and args.target[0] == "--":
        args.target = args.target[1:]

    state = WrapperState(args.ckpt_dir, args.target or [], args.cmd_sock, args.data_sock)
    try:
        return asyncio.run(state.run())
    except SystemExit as e:
        return int(e.code) if hasattr(e, 'code') else 0


if __name__ == "__main__":
    raise SystemExit(main())
