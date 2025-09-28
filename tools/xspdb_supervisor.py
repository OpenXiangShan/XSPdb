#!/usr/bin/env python3
#coding=utf-8

"""
XSPdb CRIU Supervisor

Responsibilities:
- Launch the socket-wrapper (which runs PTY/target and exposes shm discovery for cmd/tty).
- Expose a supervisor API over shared memory (discovery at api_shm path) that supports:
  save, restore, status, pause/resume, inject, shutdown.
- Execute CRIU dump/restore on the wrapper subtree using options from config.
"""

import argparse
import asyncio
import json
import os
import signal
import struct
import subprocess
import sys
import time
from typing import Optional

from multiprocessing import shared_memory

try:
    # Reuse SharedMsgQueue implementation from wrapper
    from tools.xspdb_socket_wrapper import SharedMsgQueue  # type: ignore
except Exception:
    # Minimal inline fallback (same format: 16B hdr, >I length, 0xFFFFFFFF wrap)
    _HDR_SIZE = 16
    _WRAP_MARK = 0xFFFFFFFF

    class SharedMsgQueue:
        def __init__(self, name: str, size: int = 1 << 16, create: bool = False):
            self.name = name
            self.size = size
            self.capacity = size - _HDR_SIZE
            self.shm = shared_memory.SharedMemory(name=name, create=create, size=size)
            self.buf = self.shm.buf
            if create:
                struct.pack_into('<Q', self.buf, 0, 0)
                struct.pack_into('<Q', self.buf, 8, 0)

        def close(self):
            try: self.shm.close()
            except Exception: pass

        def unlink(self):
            try: self.shm.unlink()
            except Exception: pass

        def _ru64(self, off):
            return struct.unpack_from('<Q', self.buf, off)[0]

        def _wu64(self, off, v):
            struct.pack_into('<Q', self.buf, off, int(v) & 0xFFFFFFFFFFFFFFFF)

        def push(self, payload: bytes) -> bool:
            head = self._ru64(0); tail = self._ru64(8)
            used = head - tail if head >= tail else (head + self.capacity - tail)
            free = self.capacity - used
            need = 4 + len(payload)
            if free < need: return False
            off = head % self.capacity
            contig = self.capacity - off
            if contig < need:
                if contig >= 4:
                    struct.pack_into('>I', self.buf, _HDR_SIZE + off, _WRAP_MARK)
                    head += 4; off = head % self.capacity
                head += (self.capacity - off); off = 0
            struct.pack_into('>I', self.buf, _HDR_SIZE + off, len(payload))
            self.buf[_HDR_SIZE + off + 4 : _HDR_SIZE + off + 4 + len(payload)] = payload
            head += need
            self._wu64(0, head)
            return True

        def pop(self) -> Optional[bytes]:
            head = self._ru64(0); tail = self._ru64(8)
            if tail == head: return None
            cap = self.capacity
            off = tail % cap
            if cap - off >= 4:
                (ln,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + off)
                if ln == _WRAP_MARK:
                    tail += 4; off = tail % cap
                    if tail == head:
                        self._wu64(8, tail); return None
                    (ln,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + off)
            else:
                tail += (cap - off); off = 0
                if tail == head:
                    self._wu64(8, tail); return None
                (ln,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + off)
            data = bytes(self.buf[_HDR_SIZE + off + 4 : _HDR_SIZE + off + 4 + ln])
            tail += 4 + ln
            self._wu64(8, tail)
            return data

        async def apop(self, interval_sec: float = 0.01) -> Optional[bytes]:
            d = self.pop()
            if d is not None: return d
            await asyncio.sleep(interval_sec)
            return self.pop()


class ShmEndpoint:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.req: Optional[SharedMsgQueue] = None
        self.resp: Optional[SharedMsgQueue] = None

    def open(self):
        info = {}
        # Retry reading discovery file briefly
        for _ in range(200):
            try:
                with open(self.path, 'r') as f:
                    info = json.load(f)
                break
            except Exception:
                time.sleep(0.01)
        t = info.get('type')
        if t != 'shm':
            raise RuntimeError('discovery file is not shm type: ' + self.path)
        # Retry opening queues; wrapper may have just created them
        for _ in range(200):
            try:
                self.req = SharedMsgQueue(info['req'])
                self.resp = SharedMsgQueue(info['resp'])
                break
            except FileNotFoundError:
                time.sleep(0.01)
        if not self.req or not self.resp:
            raise FileNotFoundError('shared memory queues not ready for ' + self.path)
        return self

    def close(self):
        if self.req:
            self.req.close(); self.req = None
        if self.resp:
            self.resp.close(); self.resp = None

    def request(self, obj: dict, timeout_sec: float = 5.0) -> dict:
        data = (json.dumps(obj) + '\n').encode('utf-8')
        # Push with retry
        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            if self.req and self.req.push(data):
                break
            time.sleep(0.01)
        # Pop response
        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            if self.resp:
                d = self.resp.pop()
                if d:
                    try:
                        return json.loads(d.decode('utf-8'))
                    except Exception:
                        return {"ok": False, "error": "bad_json"}
            time.sleep(0.01)
        return {"ok": False, "error": "timeout"}


class SupervisorApiServer:
    def __init__(self, api_path: str, sup: 'WrapperSupervisor', size: int = 1 << 16):
        self.api_path = os.path.abspath(api_path)
        self.size = size
        self.sup = sup
        self.req_q: Optional[SharedMsgQueue] = None
        self.resp_q: Optional[SharedMsgQueue] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        os.makedirs(os.path.dirname(self.api_path), exist_ok=True)
        # Use deterministic names based on file path
        key = self.api_path
        import hashlib
        sid = hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]
        req_name = f"xspdb_api_req_{sid}"
        resp_name = f"xspdb_api_resp_{sid}"
        self.req_q = SharedMsgQueue(req_name, size=self.size, create=True)
        self.resp_q = SharedMsgQueue(resp_name, size=self.size, create=True)
        try:
            with open(self.api_path, 'w') as f:
                json.dump({
                    'type': 'shm',
                    'req': req_name,
                    'resp': resp_name,
                    'size': self.size
                }, f)
        except Exception:
            pass
        self._task = asyncio.create_task(self._serve())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
        for q in (self.req_q, self.resp_q):
            if q:
                q.close(); q.unlink()
        try:
            if os.path.exists(self.api_path):
                os.remove(self.api_path)
        except Exception:
            pass

    async def _serve(self):
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
            try:
                if cmd == 'status':
                    r = self.sup.status()
                elif cmd == 'save':
                    name = args.get('name')
                    self.sup._log(f"save start name={name}")
                    r = await self.sup.save_checkpoint(name)
                    self.sup._log(f"save done rc={r.get('rc')} dir={r.get('images_dir')}")
                elif cmd == 'restore':
                    name = args.get('name')
                    self.sup._log(f"restore start name={name}")
                    r = await self.sup.restore_checkpoint(name)
                    self.sup._log(f"restore done rc={r.get('rc')} dir={r.get('images_dir')} pid={r.get('wrapper_pid')}")
                elif cmd == 'pause':
                    r = await self.sup.cmd_pause()
                elif cmd == 'resume':
                    r = await self.sup.cmd_resume()
                elif cmd == 'inject':
                    data = args.get('data', '')
                    r = await self.sup.cmd_inject(str(data))
                elif cmd == 'list':
                    r = self.sup.list_checkpoints()
                elif cmd == 'shutdown':
                    await self.sup.shutdown()
                    r = {"ok": True}
                else:
                    r = {"ok": False, "error": "unknown_cmd"}
            except Exception as ex:
                r = {"ok": False, "error": str(ex)}
            await self._respond(r)

    async def _respond(self, obj: dict):
        assert self.resp_q
        data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        for _ in range(50):
            if self.resp_q.push(data):
                return
            await asyncio.sleep(0.01)


class TtyProxy:
    """Bridge user terminal <-> wrapper TTY shared memory.

    - Reads from wrapper out-queue and writes to stdout.
    - Reads from stdin (raw mode), handles prefix/cmd-key, and writes to wrapper in-queue.
    - Propagates window size changes to wrapper cmd channel.
    """
    def __init__(self, sup: 'WrapperSupervisor'):
        self.sup = sup
        self.in_q: Optional[SharedMsgQueue] = None
        self.out_q: Optional[SharedMsgQueue] = None
        self._task_out: Optional[asyncio.Task] = None
        self._task_in: Optional[asyncio.Task] = None
        self._old_term = None
        self._prefix = sup.prefix or '\x01'
        self._cmd_key = (sup.cmd_key or 'c')[:1]
        self._prefix_pending = False
        self._cmd_mode = False
        self._stdin_reader_installed = False

    async def start(self):
        # Wait for discovery file from wrapper
        for _ in range(500):
            if os.path.exists(self.sup.data_sock):
                break
            await asyncio.sleep(0.01)
        info = {}
        # Retry reading and opening SHM queues
        for _ in range(200):
            try:
                with open(self.sup.data_sock, 'r') as f:
                    info = json.load(f)
                self.in_q = SharedMsgQueue(info['in'])
                self.out_q = SharedMsgQueue(info['out'])
                break
            except Exception:
                await asyncio.sleep(0.01)
        if not self.in_q or not self.out_q:
            raise RuntimeError('TTY shm queues not ready')
        # Set raw mode on stdin
        self._enter_raw()
        # Send initial winsize
        try:
            rows, cols = _get_tty_winsize()
            await self.sup.cmd_set_winsize(rows, cols)
        except Exception:
            pass
        # Setup signal for resize
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGWINCH, lambda: asyncio.create_task(self._on_winch()))
        except NotImplementedError:
            pass
        # Start IO tasks
        self._task_out = asyncio.create_task(self._pump_out_to_stdout(interval=0.001))
        # Prefer event-driven stdin
        loop = asyncio.get_running_loop()
        try:
            loop.add_reader(sys.stdin.fileno(), self._on_stdin_readable)
            self._stdin_reader_installed = True
        except (NotImplementedError, PermissionError):
            self._task_in = asyncio.create_task(self._pump_stdin_to_in())

    async def stop(self):
        for t in (self._task_out, self._task_in):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        if self._stdin_reader_installed:
            try:
                asyncio.get_running_loop().remove_reader(sys.stdin.fileno())
            except Exception:
                pass
        self._leave_raw()
        for q in (self.in_q, self.out_q):
            try:
                if q: q.close()
            except Exception:
                pass

    async def reconnect(self):
        # Stop output pump
        if self._task_out:
            self._task_out.cancel()
            try:
                await self._task_out
            except asyncio.CancelledError:
                pass
            self._task_out = None
        # Close queues
        try:
            if self.in_q:
                self.in_q.close()
        except Exception:
            pass
        try:
            if self.out_q:
                self.out_q.close()
        except Exception:
            pass
        self.in_q = None
        self.out_q = None
        # Re-open discovery and queues
        info = {}
        for _ in range(500):
            try:
                with open(self.sup.data_sock, 'r') as f:
                    info = json.load(f)
                self.in_q = SharedMsgQueue(info['in'])
                self.out_q = SharedMsgQueue(info['out'])
                break
            except Exception:
                await asyncio.sleep(0.02)
        # Restart output pump
        if self.out_q:
            self._task_out = asyncio.create_task(self._pump_out_to_stdout(interval=0.001))

    async def _pump_out_to_stdout(self, interval: float = 0.001):
        assert self.out_q
        w = sys.stdout.buffer
        while True:
            data = await self.out_q.apop(interval_sec=interval)
            if not data:
                continue
            try:
                w.write(data)
                w.flush()
            except Exception:
                await asyncio.sleep(0.005)

    async def _pump_stdin_to_in(self):
        assert self.in_q
        fd = sys.stdin.fileno()
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
        except Exception:
            pass
        while True:
            try:
                chunk = os.read(fd, 4096)
                if not chunk:
                    await asyncio.sleep(0.01)
                    continue
                await self._handle_input(chunk)
            except BlockingIOError:
                await asyncio.sleep(0.005)
            except Exception:
                await asyncio.sleep(0.01)

    def _on_stdin_readable(self):
        try:
            chunk = os.read(sys.stdin.fileno(), 4096)
            if chunk:
                asyncio.create_task(self._handle_input(chunk))
        except BlockingIOError:
            pass
        except Exception:
            pass

    async def _handle_input(self, data: bytes):
        for b in data:
            ch = chr(b)
            if not self._cmd_mode:
                if self._prefix_pending:
                    self._prefix_pending = False
                    if ch == self._cmd_key:
                        # enter command mode (switch to REPL)
                        await self._enter_cmd_mode()
                        continue
                    else:
                        # forward prefix + char
                        await self._push_bytes(self._prefix.encode('utf-8') + bytes([b]))
                        continue
                if ch == self._prefix:
                    self._prefix_pending = True
                    continue
                await self._push_bytes(bytes([b]))
            else:
                # In REPL mode, _pump_stdin_to_in is paused; this path shouldn't be hit
                pass

    async def _enter_cmd_mode(self):
        # Pause wrapper, stop stdin pump, leave raw, start REPL
        self._cmd_mode = True
        try:
            await self.sup.cmd_pause()
        except Exception:
            pass
        _print_line("\n===== ENTER COMMAND MODE =====")
        if self._stdin_reader_installed:
            try:
                asyncio.get_running_loop().remove_reader(sys.stdin.fileno())
            except Exception:
                pass
            self._stdin_reader_installed = False
        if self._task_in:
            self._task_in.cancel()
            try:
                await self._task_in
            except asyncio.CancelledError:
                pass
        self._leave_raw()
        await self._cmd_repl()

    async def _exit_cmd_mode(self):
        # Resume wrapper, re-enter raw, restart stdin pump
        try:
            await self.sup.cmd_resume()
        except Exception:
            pass
        self._cmd_mode = False
        _print_line("===== LEAVE COMMAND MODE; RESUME TARGET =====")
        self._enter_raw()
        loop = asyncio.get_running_loop()
        try:
            loop.add_reader(sys.stdin.fileno(), self._on_stdin_readable)
            self._stdin_reader_installed = True
        except (NotImplementedError, PermissionError):
            self._task_in = asyncio.create_task(self._pump_stdin_to_in())
        # Refresh target display after returning to TTY mode
        await self._refresh_screen(clear=False)

    async def _cmd_repl(self):
        _print_line("\n[Command Mode] Type 'help' for commands.")
        while True:
            try:
                line = await asyncio.to_thread(input, "xspdb> ")
            except (EOFError, KeyboardInterrupt):
                line = 'continue'
            if not line:
                continue
            parts = line.strip().split()
            cmd = parts[0].lower()
            args = parts[1:]
            if cmd in ('c', 'cont', 'continue', 'back', 'resume'):
                await self._exit_cmd_mode()
                break
            elif cmd in ('s', 'save'):
                name = args[0] if args else None
                r = await self.sup.save_checkpoint(name)
                _print_line(f"saved ok={r.get('ok')} rc={r.get('rc')} dir={r.get('images_dir')} log={r.get('dump_log')}")
                tail = r.get('dump_log_tail')
                if tail:
                    _print_line("--- dump.log (tail) ---\n" + tail + "\n--- end ---")
                # Auto-continue and refresh screen to match target PTY
                await self._exit_cmd_mode()
                await self._refresh_screen(clear=False)
                break
            elif cmd in ('r', 'restore'):
                name = args[0] if args else 'latest'
                # Clear local terminal before restore
                _clear_local_terminal()
                r = await self.sup.restore_checkpoint(name)
                _print_line(f"restored ok={r.get('ok')} rc={r.get('rc')} pid={r.get('wrapper_pid')} log={r.get('restore_log')}")
                tail = r.get('restore_log_tail')
                if tail:
                    _print_line("--- restore.log (tail) ---\n" + tail + "\n--- end ---")
                # After restore, reconnect TTY and continue
                try:
                    if self.sup._tty:
                        await self.sup._tty.reconnect()
                except Exception:
                    pass
                await self._exit_cmd_mode()
                await self._refresh_screen(clear=False)
                break
            elif cmd in ('ls', 'list'):
                r = self.sup.list_checkpoints()
                if r.get('ok'):
                    items = r.get('images') or []
                    for it in items:
                        _print_line(f" - {it}")
                else:
                    _print_line(str(r))
            elif cmd in ('p', 'pause'):
                r = await self.sup.cmd_pause(); _print_line(str(r))
            elif cmd in ('res', 'resume'):
                r = await self.sup.cmd_resume(); _print_line(str(r))
            elif cmd in ('inj', 'inject'):
                data = ' '.join(args) + ('\n' if args else '')
                r = await self.sup.cmd_inject(data); _print_line(str(r))
            elif cmd in ('q', 'quit', 'exit', 'shutdown'):
                await self.sup.shutdown(); break
            elif cmd in ('h', 'help', '?'):
                _print_line("commands: save [name], restore [name|latest], pause, resume, inject <text>, continue, shutdown")
            else:
                _print_line("unknown command; type 'help'")

    async def _on_winch(self):
        try:
            rows, cols = _get_tty_winsize()
            await self.sup.cmd_set_winsize(rows, cols)
        except Exception:
            pass

    async def _push_bytes(self, data: bytes):
        assert self.in_q
        # push with small retries
        for _ in range(50):
            if self.in_q.push(data):
                return
            await asyncio.sleep(0.005)

    def _enter_raw(self):
        try:
            import tty
            fd = sys.stdin.fileno()
            self._old_term = termios.tcgetattr(fd)
            tty.setraw(fd)
        except Exception:
            self._old_term = None

    def _leave_raw(self):
        if self._old_term is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_term)
            except Exception:
                pass

    async def _refresh_screen(self, clear: bool = False):
        # Optionally clear local terminal, then request target to redraw
        try:
            if clear:
                _clear_local_terminal()
            rows, cols = _get_tty_winsize()
            await self.sup.cmd_set_winsize(rows, cols)
        except Exception:
            pass
        # Ctrl-L is a common redraw request for shells and TUIs
        try:
            await self.sup.cmd_inject('\x0c')
        except Exception:
            pass


def _get_tty_winsize():
    try:
        import array
        fd = sys.stdout.fileno()
        s = struct.pack('HHHH', 0, 0, 0, 0)
        out = fcntl.ioctl(fd, termios.TIOCGWINSZ, s)
        rows, cols, _, _ = struct.unpack('HHHH', out)
        rows = rows or 24
        cols = cols or 80
        return int(rows), int(cols)
    except Exception:
        return 24, 80


def _print_line(s: str):
    try:
        sys.stdout.write(s + ('' if s.endswith('\n') else '\n'))
        sys.stdout.flush()
    except Exception:
        pass


def _clear_local_terminal():
    try:
        sys.stdout.write("\x1b[2J\x1b[H")  # clear screen and move cursor home
        sys.stdout.flush()
    except Exception:
        pass


class WrapperSupervisor:
    def __init__(self, ckpt_dir, api_sock, target_argv,
                 wrap_sock=None, data_sock=None,
                 enable_lazy_pages=False, enable_track_mem=False,
                 prev_images_dir=None, pre_save_backend_cmds=None,
                 pre_restore_backend_cmds=None, env_overrides=None,
                 prefix=None, cmd_key='c'):
        self.ckpt_dir = os.path.abspath(ckpt_dir)
        self.api_sock = os.path.abspath(api_sock)
        self.wrap_sock = os.path.abspath(wrap_sock or os.path.join(self.ckpt_dir, "cmdapi.shm"))
        self.data_sock = os.path.abspath(data_sock or os.path.join(self.ckpt_dir, "tty.shm"))
        self.target = list(target_argv or [])
        self.enable_lazy_pages = bool(enable_lazy_pages)
        self.enable_track_mem = bool(enable_track_mem)
        self.prev_images_dir = prev_images_dir or None
        self.pre_save_backend_cmds = list(pre_save_backend_cmds or [])
        self.pre_restore_backend_cmds = list(pre_restore_backend_cmds or [])
        self.env_overrides = dict(env_overrides or {})
        self.prefix = prefix
        self.cmd_key = cmd_key
        self.wrapper_proc: Optional[subprocess.Popen] = None
        self.api_server: Optional[SupervisorApiServer] = None
        self._stop = asyncio.Event()
        self._tty: Optional[TtyProxy] = None
        self._log_path = os.path.join(self.ckpt_dir, 'supervisor.log')

    def status(self) -> dict:
        return {
            'ok': True,
            'wrapper_pid': (self.wrapper_proc.pid if self.wrapper_proc and self.wrapper_proc.poll() is None else None),
            'ckpt_dir': self.ckpt_dir,
            'lazy_pages': self.enable_lazy_pages,
            'track_mem': self.enable_track_mem,
            'prev_images_dir': self.prev_images_dir,
        }

    async def cmd_pause(self) -> dict:
        ep = ShmEndpoint(self.wrap_sock).open()
        try:
            return ep.request({'cmd': 'pause'})
        finally:
            ep.close()

    async def cmd_resume(self) -> dict:
        ep = ShmEndpoint(self.wrap_sock).open()
        try:
            return ep.request({'cmd': 'resume'})
        finally:
            ep.close()

    async def cmd_inject(self, data: str) -> dict:
        ep = ShmEndpoint(self.wrap_sock).open()
        try:
            return ep.request({'cmd': 'inject', 'args': {'data': data}})
        finally:
            ep.close()

    async def cmd_set_winsize(self, rows: int, cols: int) -> dict:
        ep = ShmEndpoint(self.wrap_sock).open()
        try:
            return ep.request({'cmd': 'set_winsize', 'args': {'rows': int(rows), 'cols': int(cols)}})
        finally:
            ep.close()

    def _images_root(self) -> str:
        d = os.path.join(self.ckpt_dir, 'images')
        os.makedirs(d, exist_ok=True)
        return d

    def _make_ckpt_dir(self, name: Optional[str]) -> str:
        ts = time.strftime('ckpt-%Y%m%d-%H%M%S')
        if name:
            ts = f"{ts}-{name}"
        path = os.path.join(self._images_root(), ts)
        os.makedirs(path, exist_ok=True)
        return path

    async def save_checkpoint(self, name: Optional[str] = None) -> dict:
        if not (self.wrapper_proc and self.wrapper_proc.poll() is None):
            return {"ok": False, "error": "wrapper_not_running"}
        # Pause I/O via wrapper cmd (best-effort)
        try:
            await self.cmd_pause()
        except Exception:
            pass
        # Pre-save backend commands (inject to target)
        for cmd in self.pre_save_backend_cmds:
            try:
                await self.cmd_inject(cmd + "\n")
            except Exception:
                pass
        # Build CRIU dump
        images_dir = self._make_ckpt_dir(name)
        dump_log = os.path.join(images_dir, 'dump.log')
        # Dump entire wrapper subtree to preserve PTY/ctty
        root_pid = self.wrapper_proc.pid
        criu = ['criu', 'dump', '-t', str(root_pid), '-D', images_dir, '-o', dump_log, '--shell-job', '--leave-running']
        if self.enable_track_mem:
            criu.append('--track-mem')
        if self.prev_images_dir:
            criu += ['--prev-images-dir', str(self.prev_images_dir)]
        if self.enable_lazy_pages:
            # Note: full lazy-pages flow requires running `criu lazy-pages` separately.
            criu.append('--lazy-pages')
        env = os.environ.copy()
        env.update({k: v for k, v in self.env_overrides.items() if v is not None})
        for k, v in list(self.env_overrides.items()):
            if v is None:
                env.pop(k, None)
        # Execute
        rc = await _run_cmd(criu, env=env, cwd=self.ckpt_dir)
        # Resume regardless
        try:
            await self.cmd_resume()
        except Exception:
            pass
        tail = _read_tail(dump_log, 40)
        return {"ok": rc == 0, "rc": rc, "images_dir": images_dir, "dump_log": dump_log, "dump_log_tail": tail}

    async def restore_checkpoint(self, name_or_latest: Optional[str] = None) -> dict:
        # Resolve images dir
        root = self._images_root()
        cand = None
        if name_or_latest and name_or_latest != 'latest':
            cand = name_or_latest if os.path.isabs(name_or_latest) else os.path.join(root, name_or_latest)
        else:
            # pick latest
            try:
                subs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
                subs.sort()
                if subs:
                    cand = os.path.join(root, subs[-1])
            except Exception:
                cand = None
        if not cand or not os.path.isdir(cand):
            return {"ok": False, "error": "images_dir_not_found"}
        images_dir = cand
        restore_log = os.path.join(images_dir, 'restore.log')
        pidfile = os.path.join(images_dir, 'restore.pid')

        # Stop current wrapper tree gracefully first, then force if needed
        if self.wrapper_proc and self.wrapper_proc.poll() is None:
            # Ask wrapper to shutdown (so it unlinks shm)
            try:
                ep = ShmEndpoint(self.wrap_sock).open()
                _ = ep.request({'cmd': 'shutdown'})
                ep.close()
            except Exception:
                pass
            # Wait a bit
            for _ in range(20):
                if self.wrapper_proc.poll() is not None:
                    break
                time.sleep(0.05)
            # Terminate if still alive
            if self.wrapper_proc.poll() is None:
                try:
                    os.kill(self.wrapper_proc.pid, signal.SIGTERM)
                except Exception:
                    pass
                for _ in range(40):
                    if self.wrapper_proc.poll() is not None:
                        break
                    time.sleep(0.05)
                if self.wrapper_proc.poll() is None:
                    try:
                        os.kill(self.wrapper_proc.pid, signal.SIGKILL)
                    except Exception:
                        pass

        # Build CRIU restore
        criu = ['criu', 'restore', '-D', images_dir, '-o', restore_log, '--shell-job', '--pidfile', pidfile]
        env = os.environ.copy()
        env.update({k: v for k, v in self.env_overrides.items() if v is not None})
        for k, v in list(self.env_overrides.items()):
            if v is None:
                env.pop(k, None)
        rc = await _run_cmd(criu, env=env, cwd=self.ckpt_dir)
        new_pid = None
        try:
            with open(pidfile, 'r') as f:
                new_pid = int(f.read().strip() or '0')
        except Exception:
            new_pid = None
        ok = (rc == 0 and bool(new_pid))
        # Reconnect TTY to restored wrapper
        try:
            if ok and self._tty:
                await self._tty.reconnect()
        except Exception:
            pass
        # Post-restore backend commands (optional), e.g., re-init waves
        if ok and self.pre_restore_backend_cmds:
            for cmd in self.pre_restore_backend_cmds:
                try:
                    await self.cmd_inject(cmd + "\n")
                except Exception:
                    pass
        tail = _read_tail(restore_log, 60)
        return {"ok": ok, "rc": rc, "wrapper_pid": new_pid, "images_dir": images_dir, "restore_log": restore_log, "restore_log_tail": tail}

    def list_checkpoints(self) -> dict:
        root = self._images_root()
        try:
            items = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
            items.sort()
            return {"ok": True, "images": items}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    async def shutdown(self):
        # Stop API server and wrapper
        if self.api_server:
            try:
                await self.api_server.stop()
            except Exception:
                pass
        if self.wrapper_proc and self.wrapper_proc.poll() is None:
            try:
                os.kill(self.wrapper_proc.pid, signal.SIGTERM)
            except Exception:
                pass
        self._stop.set()

    async def run(self) -> int:
        os.makedirs(self.ckpt_dir, exist_ok=True)
        # Spawn wrapper
        if not self.target:
            print('No target provided to supervisor', file=sys.stderr)
            return 2
        args = [sys.executable, os.path.join(os.path.dirname(__file__), 'xspdb_socket_wrapper.py'),
                '--ckpt-dir', self.ckpt_dir,
                '--cmd-sock', self.wrap_sock,
                '--data-sock', self.data_sock,
                '--', *self.target]
        # Build environment for wrapper from overrides
        env = os.environ.copy()
        env.update({k: v for k, v in self.env_overrides.items() if v is not None})
        for k, v in list(self.env_overrides.items()):
            if v is None:
                env.pop(k, None)
        self.wrapper_proc = subprocess.Popen(args, env=env)
        # Start API server
        self.api_server = SupervisorApiServer(self.api_sock, self)
        await self.api_server.start()
        # Start TTY proxy (wait for wrapper to create discovery file)
        self._tty = TtyProxy(self)
        await self._tty.start()
        try:
            await self._stop.wait()
        finally:
            try:
                await self.api_server.stop()  # type: ignore
            except Exception:
                pass
            if self._tty:
                try:
                    await self._tty.stop()
                except Exception:
                    pass
        return 0


async def _run_cmd(cmd, env=None, cwd=None) -> int:
    try:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env,
                                                 stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        try:
            if out:
                sys.stdout.write(out.decode('utf-8', errors='ignore'))
            if err:
                sys.stderr.write(err.decode('utf-8', errors='ignore'))
        except Exception:
            pass
        return int(p.returncode or 0)
    except Exception as ex:
        sys.stderr.write(f"exec failed: {ex}\n")
        return 127


def _read_tail(path: str, max_lines: int = 40) -> str:
    try:
        with open(path, 'r', errors='ignore') as f:
            lines = f.readlines()[-max_lines:]
            return ''.join(lines)
    except Exception:
        return ''


def main():
    ap = argparse.ArgumentParser(description="XSPdb CRIU Supervisor")
    ap.add_argument("--ckpt-dir", default=".xspdb_ckpt", help="checkpoint directory")
    ap.add_argument("--sock", default=".xspdb_ckpt/api.shm", help="XSPdb API discovery path (.shm JSON)")
    ap.add_argument("--wrap-sock", default=".xspdb_ckpt/cmdapi.shm", help="wrapper control discovery path (.shm JSON)")
    ap.add_argument("--data-sock", default=".xspdb_ckpt/tty.shm", help="wrapper data discovery path (.shm JSON)")
    ap.add_argument("--", dest="dashdash", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("target", nargs=argparse.REMAINDER, help="target command to run after --")
    args = ap.parse_args()

    sup = WrapperSupervisor(
        args.ckpt_dir, args.sock, args.target,
        wrap_sock=args.wrap_sock,
        data_sock=args.data_sock,
    )
    try:
        return asyncio.run(sup.run())
    except SystemExit as e:
        return int(e.code) if hasattr(e, 'code') else 0


def _eprint(*a):
    try:
        print(*a, file=sys.stderr, flush=True)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
