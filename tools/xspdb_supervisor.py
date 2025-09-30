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
import fcntl
import termios
import struct
import subprocess
import sys
import time
from typing import Optional
import traceback
import hashlib

from multiprocessing import shared_memory

from tools.shm_queue import SharedMsgQueue
from tools.shm_endpoint import ShmEndpoint
from tools.log_config import LogConfig


    


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
        # Brief on-screen hint before entering raw mode
        try:
            def _fmt_key(ch: str) -> str:
                if not ch:
                    return '^A'
                c = ch[0]
                o = ord(c)
                if 0 < o < 32:
                    return '^' + chr(o + 64)
                return c
            _print_line(f"[XSPdb] Press {_fmt_key(self._prefix)} then '{self._cmd_key}' to enter command mode (type 'help' there).")
        except Exception:
            pass
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
            except Exception as ex:
                self.sup._log("reconnect: cancel _task_out error: " + str(ex) + "\n" + traceback.format_exc())
            self._task_out = None
        # Close queues
        try:
            if self.in_q:
                self.in_q.close()
        except Exception as ex:
            self.sup._log("reconnect: close in_q error: " + str(ex) + "\n" + traceback.format_exc())
        try:
            if self.out_q:
                self.out_q.close()
        except Exception as ex:
            self.sup._log("reconnect: close out_q error: " + str(ex) + "\n" + traceback.format_exc())
        self.in_q = None
        self.out_q = None
        # Re-open discovery and queues
        info = {}
        last_err: str | None = None
        for _ in range(500):
            try:
                with open(self.sup.data_sock, 'r') as f:
                    info = json.load(f)
                self.in_q = SharedMsgQueue(info['in'])
                self.out_q = SharedMsgQueue(info['out'])
                try:
                    self.sup._log(f"tty.reconnect: discovery in={info.get('in')} out={info.get('out')} size={info.get('size')}")
                except Exception:
                    pass
                break
            except Exception as ex:
                last_err = str(ex)
                await asyncio.sleep(0.02)
        if not (self.in_q and self.out_q):
            raise RuntimeError("TTY reconnect failed to open shm queues: " + (last_err or "unknown"))
        # Restart output pump
        try:
            self._task_out = asyncio.create_task(self._pump_out_to_stdout(interval=0.001))
            try:
                self.sup._log("tty.reconnect: output pump restarted")
            except Exception:
                pass
        except Exception as ex:
            self.sup._log("reconnect: start _task_out error: " + str(ex) + "\n" + traceback.format_exc())
        # Ensure stdin reader/task is active when not in cmd mode
        if not self._cmd_mode:
            loop = asyncio.get_running_loop()
            if not self._stdin_reader_installed:
                try:
                    loop.add_reader(sys.stdin.fileno(), self._on_stdin_readable)
                    self._stdin_reader_installed = True
                    self._enter_raw()
                    try:
                        self.sup._log("tty.reconnect: stdin reader reinstalled and raw mode ensured")
                    except Exception:
                        pass
                except (NotImplementedError, PermissionError):
                    self._task_in = asyncio.create_task(self._pump_stdin_to_in())
                    try:
                        self.sup._log("tty.reconnect: stdin pump task restarted (no add_reader)")
                    except Exception:
                        pass

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
        _print_line("\n[Command Mode] Type 'help' for commands. 'continue' to return to target.")
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
                errs = r.get('reconnect_errors') or []
                if errs:
                    _print_line("[reconnect errors]")
                    for e in errs:
                        _print_line(" - " + str(e))
                # After restore, reconnect TTY and continue
                try:
                    if self.sup._tty:
                        await self.sup._tty.reconnect()
                except Exception:
                    pass
                await self._exit_cmd_mode()
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
                _print_line(
                    "commands: save [name], restore [name|latest], list, pause, resume, inject <text>, continue, shutdown\n"
                    "aliases: c/cont/back/continue, q/quit/exit/shutdown\n"
                    "hint: Press the configured prefix then cmd-key to enter this mode."
                )
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
                 prefix=None, cmd_key='c',
                 images_subdir: str = 'images',
                 log_cfg: Optional[LogConfig] = None):
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
        self.images_subdir = images_subdir or 'images'
        self.log_cfg = log_cfg or LogConfig()
        self._log_path = os.path.join(self.ckpt_dir, (self.log_cfg.supervisor or 'supervisor.log'))
        self._precreated_shms: list[shared_memory.SharedMemory] = []
        self._sigchld_installed: bool = False

    # ---- discovery helpers ----
    def _cmd_names(self) -> tuple[str, str, int]:
        sid = hashlib.sha1(self.wrap_sock.encode('utf-8')).hexdigest()[:16]
        return (f"xspdb_cmd_req_{sid}", f"xspdb_cmd_resp_{sid}", 1 << 16)

    def _tty_names(self) -> tuple[str, str, int]:
        sid = hashlib.sha1((self.data_sock + ":tty").encode('utf-8')).hexdigest()[:16]
        return (f"xspdb_tty_in_{sid}", f"xspdb_tty_out_{sid}", 1 << 20)

    def _write_discovery(self, path: str, info: dict) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump(info, f)
        except Exception as ex:
            self._log(f"restore: write discovery failed {path}: {ex}")

    # ---- child reaper for restored wrapper (avoid zombies) ----
    def _install_sigchld_reaper(self) -> None:
        if self._sigchld_installed:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGCHLD, lambda: asyncio.create_task(self._reap_children()))
            self._sigchld_installed = True
            self._log('reaper: SIGCHLD handler installed')
        except (NotImplementedError, RuntimeError):
            # Fallback to signal.signal
            def _h(signum, frame):
                try:
                    # schedule via call_soon_threadsafe if loop available
                    try:
                        asyncio.get_event_loop().call_soon_threadsafe(lambda: asyncio.create_task(self._reap_children()))
                    except Exception:
                        # last resort: do a non-blocking drain in signal context (quick and safe in CPython)
                        try:
                            while True:
                                pid, st = os.waitpid(-1, os.WNOHANG)
                                if pid <= 0:
                                    break
                        except ChildProcessError:
                            pass
                except Exception:
                    pass
            try:
                signal.signal(signal.SIGCHLD, _h)
                self._sigchld_installed = True
                self._log('reaper: SIGCHLD handler installed (fallback)')
            except Exception:
                pass

    async def _reap_children(self):
        try:
            reaped = []
            while True:
                try:
                    pid, st = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid <= 0:
                    break
                reaped.append((pid, st))
            if reaped:
                self._log('reaper: reaped ' + ', '.join(f"{pid}:{st}" for pid, st in reaped))
        except Exception as ex:
            self._log('reaper: error ' + str(ex))

    # ---- process cleanup helpers ----
    def _scan_wrapper_pids(self) -> list[int]:
        pids: list[int] = []
        proc = '/proc'
        for name in os.listdir(proc):
            if not name.isdigit():
                continue
            pid = int(name)
            cmdline_path = os.path.join(proc, name, 'cmdline')
            try:
                with open(cmdline_path, 'rb') as f:
                    raw = f.read()
                # cmdline is nul-separated
                parts = raw.split(b'\x00')
                s = b' '.join(parts).decode('utf-8', errors='ignore')
            except Exception:
                continue
            if 'xspdb_socket_wrapper.py' in s and self.ckpt_dir in s:
                # Avoid killing our own supervisor process if it somehow matches (it shouldn't)
                pids.append(pid)
        return pids

    def _terminate_pids(self, pids: list[int], desc: str) -> None:
        if not pids:
            return
        self._log(f'restore: cleanup {desc} pids={pids} (SIGTERM)')
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        # wait a bit
        for _ in range(40):
            alive = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except Exception:
                    pass
            if not alive:
                break
            time.sleep(0.05)
        # escalate
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except Exception:
                pass
        if alive:
            self._log(f'restore: cleanup {desc} still alive={alive} (SIGKILL)')
            for pid in alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
            # brief wait
            time.sleep(0.05)

    def _read_pidfile(self, path: str) -> Optional[int]:
        try:
            with open(path, 'r') as f:
                v = int((f.read().strip() or '0'))
                return v if v > 0 else None
        except Exception:
            return None

    def _list_restore_pids_from_images(self) -> list[int]:
        root = self._images_root()
        pids: list[int] = []
        try:
            for d in os.listdir(root):
                ip = os.path.join(root, d, 'restore.pid')
                if os.path.isfile(ip):
                    pid = self._read_pidfile(ip)
                    if pid:
                        pids.append(pid)
        except Exception:
            pass
        return pids

    def _collect_descendants(self, roots: list[int]) -> list[int]:
        # Traverse /proc/*/task/*/children to gather subtree PIDs
        seen = set()
        stack = list(roots)
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                task_dir = f'/proc/{pid}/task'
                tids = []
                for t in os.listdir(task_dir):
                    if t.isdigit():
                        tids.append(t)
                for tid in tids:
                    cpath = f'/proc/{pid}/task/{tid}/children'
                    try:
                        with open(cpath, 'r') as f:
                            data = f.read().strip()
                        if data:
                            for c in data.split():
                                try:
                                    cp = int(c)
                                    if cp not in seen:
                                        stack.append(cp)
                                except Exception:
                                    pass
                    except Exception:
                        pass
            except Exception:
                pass
        return list(seen)

    def _terminate_trees(self, roots: list[int], desc: str) -> None:
        if not roots:
            return
        # Kill children first, then roots
        tree = self._collect_descendants(roots)
        # Ensure roots are included
        for r in roots:
            if r not in tree:
                tree.append(r)
        # Exclude current supervisor pid from accidental kill
        safe_tree = [p for p in tree if p != os.getpid()]
        if not safe_tree:
            return
        # Split children and roots
        roots_set = set(roots)
        children = [p for p in safe_tree if p not in roots_set]
        if children:
            self._terminate_pids(children, desc + '.children')
        self._terminate_pids([p for p in safe_tree if p in roots_set], desc + '.roots')
        # Wait for full disappearance of the tree to avoid PID reuse conflicts
        self._wait_tree_gone(tree, desc)

    def _proc_status(self, pid: int) -> tuple[str|None, str|None, int|None]:
        # Returns (state, name, ppid) where state like 'R','S','Z', etc.
        try:
            with open(f'/proc/{pid}/status', 'r') as f:
                state = None; name = None; ppid = None
                for line in f:
                    if line.startswith('Name:'):
                        name = line.split(':',1)[1].strip()
                    elif line.startswith('State:'):
                        # e.g., State: Z (zombie)
                        parts = line.split()
                        if len(parts) >= 2:
                            state = parts[1]
                    elif line.startswith('PPid:'):
                        try:
                            ppid = int(line.split(':',1)[1].strip())
                        except Exception:
                            ppid = None
                return state, name, ppid
        except Exception:
            return None, None, None

    def _read_cmdline(self, pid: int) -> str:
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                raw = f.read()
            return b' '.join(raw.split(b'\x00')).decode('utf-8', errors='ignore')
        except Exception:
            return ''

    def _kill_parent_if_needed(self, zombies: list[tuple[int,int]], desc: str) -> bool:
        """zombies: list of (pid, ppid). If parent is not self and looks like our prior wrapper/supervisor,
        terminate that parent tree to allow init to reap the zombies. Returns True if any action taken."""
        acted = False
        for _, ppid in zombies:
            try:
                if ppid <= 1 or ppid == os.getpid():
                    continue
                cmd = self._read_cmdline(ppid)
                if (self.ckpt_dir in cmd) or ('xspdb_socket_wrapper.py' in cmd) or ('xspdb_supervisor.py' in cmd):
                    self._log(f'restore: killing zombie parent ppid={ppid} cmd={cmd!r}')
                    self._terminate_trees([ppid], desc + '.ppid')
                    acted = True
            except Exception:
                pass
        return acted

    def _wait_tree_gone(self, pids: list[int], desc: str, timeout_sec: float = 1.0) -> None:
        deadline = time.time() + max(0.0, float(timeout_sec))
        remaining = set(pids)
        attempts = 0
        while time.time() < deadline and remaining:
            alive = []
            for pid in list(remaining):
                if os.path.exists(f'/proc/{pid}'):
                    alive.append(pid)
                else:
                    remaining.discard(pid)
            if not alive:
                break
            # Log a snapshot of states for debugging zombies
            snap = []
            zombies: list[tuple[int,int]] = []
            for pid in alive[:8]:  # cap to avoid huge logs
                st, nm, ppid = self._proc_status(pid)
                snap.append(f"{pid}:{st or '?'}:{ppid or '?'}:{nm or '?'}")
                if (st == 'Z') and (ppid is not None):
                    zombies.append((pid, ppid))
            self._log(f'restore: waiting tree gone ({desc}), alive={alive} states={" ".join(snap)}')
            time.sleep(0.05)
            attempts += 1
            # If zombies persist and parent seems to be our previous processes, try killing parent to trigger reaping
            if zombies and (attempts % 5 == 0):  # don't spam; try occasionally
                if self._kill_parent_if_needed(zombies, desc):
                    # extend deadline slightly after action
                    deadline = max(deadline, time.time() + 0.5)

    async def _wait_cmdapi_ready(self, timeout_sec: float = 2.0) -> bool:
        deadline = time.time() + max(0.0, float(timeout_sec))
        last_err = None
        while time.time() < deadline:
            try:
                ep = ShmEndpoint(self.wrap_sock).open(timeout_sec=0.5)
                try:
                    resp = ep.request({'cmd': 'ping'}, timeout_sec=0.5)
                    if isinstance(resp, dict) and resp.get('ok'):
                        return True
                finally:
                    ep.close()
            except Exception as ex:
                last_err = str(ex)
            await asyncio.sleep(0.05)
        self._log('restore: cmdapi not ready: ' + (last_err or 'timeout'))
        return False

    def status(self) -> dict:
        return {
            'ok': True,
            'wrapper_pid': (self.wrapper_proc.pid if self.wrapper_proc and self.wrapper_proc.poll() is None else None),
            'ckpt_dir': self.ckpt_dir,
            'lazy_pages': self.enable_lazy_pages,
            'track_mem': self.enable_track_mem,
            'prev_images_dir': self.prev_images_dir,
        }

    def _log(self, msg: str) -> None:
        try:
            os.makedirs(self.ckpt_dir, exist_ok=True)
            with open(self._log_path, 'a') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + "\n")
        except Exception:
            pass

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
        d = os.path.join(self.ckpt_dir, self.images_subdir or 'images')
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
        self._log('restore: resolve images_dir start')
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
            self._log('restore: images_dir not found')
            return {"ok": False, "error": "images_dir_not_found"}
        images_dir = cand
        restore_log = os.path.join(images_dir, 'restore.log')
        pidfile = os.path.join(images_dir, 'restore.pid')
        self._log(f'restore: images_dir={images_dir}')

        # Capture discovery shm names before shutdown
        self._log('restore: collect shm discovery entries')
        entries = self._collect_shm_entries_from_discovery()
        self._log(f'restore: entries collected: {entries}')

        # Stop current wrapper tree gracefully first, then force if needed
        if self.wrapper_proc and self.wrapper_proc.poll() is None:
            self._log('restore: stopping current wrapper')
            # Ask wrapper to shutdown (so it unlinks shm)
            try:
                self._log('restore: sending wrapper shutdown via cmdapi')
                ep = ShmEndpoint(self.wrap_sock).open()
                _ = ep.request({'cmd': 'shutdown'})
                ep.close()
            except Exception:
                self._log('restore: wrapper shutdown via cmdapi failed')
                pass
            # Wait a bit
            self._log('restore: waiting for wrapper to exit')
            for _ in range(20):
                if self.wrapper_proc.poll() is not None:
                    break
                time.sleep(0.05)
            # Terminate if still alive
            if self.wrapper_proc.poll() is None:
                self._log('restore: wrapper still alive, send SIGTERM')
                try:
                    os.kill(self.wrapper_proc.pid, signal.SIGTERM)
                except Exception:
                    pass
                for _ in range(40):
                    if self.wrapper_proc.poll() is not None:
                        break
                    time.sleep(0.05)
                if self.wrapper_proc.poll() is None:
                    self._log('restore: wrapper still alive, send SIGKILL')
                    try:
                        os.kill(self.wrapper_proc.pid, signal.SIGKILL)
                    except Exception:
                        pass

        # Additionally, clean up any residual detached wrapper(s) from previous restore runs
        try:
            # Candidates from cmdline scan
            residual = self._scan_wrapper_pids()
            # Candidates from previous images' restore.pid files
            residual += self._list_restore_pids_from_images()
            # De-dup and exclude current managed wrapper pid if present
            cur = (self.wrapper_proc.pid if self.wrapper_proc and self.wrapper_proc.poll() is None else None)
            cand = sorted({p for p in residual if p and p != cur})
            if cand:
                self._log(f'restore: residual candidate roots={cand}')
                self._terminate_trees(cand, 'residual_wrappers')
        except Exception as ex:
            self._log(f'restore: residual scan failed: {ex}')

        # Pre-create shm segments expected during restore, based on captured discovery
        self._log('restore: precreate SHMs start')
        self._precreate_shms(entries)
        self._log('restore: precreate SHMs done')

        # Pre-write discovery files so supervisor/clients see stable paths during reconnect window
        try:
            req, resp, csz = self._cmd_names()
            self._write_discovery(self.wrap_sock, {'type': 'shm', 'req': req, 'resp': resp, 'size': csz})
            self._log(f'restore: prewrote cmd discovery {self.wrap_sock} -> req={req} resp={resp}')
        except Exception:
            pass
        try:
            tin, tout, tsz = self._tty_names()
            self._write_discovery(self.data_sock, {'type': 'shm', 'in': tin, 'out': tout, 'size': tsz})
            self._log(f'restore: prewrote tty discovery {self.data_sock} -> in={tin} out={tout}')
        except Exception:
            pass

        # Build CRIU restore (detached + sibling so CRIU exits promptly and wrapper becomes sibling)
        criu = [
            'criu', 'restore',
            '-D', images_dir,
            '-o', restore_log,
            '--shell-job',
            '--pidfile', pidfile,
            '--restore-detached',
            '--restore-sibling'
        ]
        # Remove stale pidfile before restore to avoid reading old PID on failure
        try:
            if os.path.exists(pidfile):
                os.remove(pidfile)
                self._log('restore: removed stale pidfile before restore')
        except Exception:
            pass
        self._log(f'restore: run CRIU restore: {criu}')
        env = os.environ.copy()
        env.update({k: v for k, v in self.env_overrides.items() if v is not None})
        for k, v in list(self.env_overrides.items()):
            if v is None:
                env.pop(k, None)
        t0 = time.time()
        rc = await _run_cmd(criu, env=env, cwd=self.ckpt_dir)
        self._log(f'restore: CRIU returned rc={rc} duration_sec={time.time()-t0:.3f}')
        # With --restore-detached, pidfile may be written shortly after CRIU returns; poll briefly
        new_pid = None
        for _ in range(200):  # ~2s
            try:
                with open(pidfile, 'r') as f:
                    new_pid = int(f.read().strip() or '0')
                if new_pid:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.01)
        if not new_pid:
            try:
                with open(pidfile, 'r') as f:
                    self._log('restore: pidfile content=' + (f.read().strip() or ''))
            except Exception:
                self._log('restore: pidfile not readable yet')
        self._log(f'restore: read new_pid={new_pid}')
        if rc != 0:
            tail = _read_tail(restore_log, 80)
            self._log('restore: restore.log tail (rc!=0):\n' + tail)
        self._log(f'restore: read new_pid={new_pid}')
        ok = (rc == 0 and bool(new_pid))
        self._log(f"restore_checkpoint ok={ok} rc={rc} pid={new_pid} dir={images_dir}")
        # Cleanup precreated shms (do not unlink, only close; restored wrapper owns lifecycle)
        for shm in self._precreated_shms:
            try:
                shm.close()
            except Exception:
                pass
        self._precreated_shms.clear()
        # If CRIU did not deliver SIGCONT or delivered too early, send it now
        if ok and new_pid:
            try:
                os.kill(new_pid, signal.SIGCONT)
                self._log(f"restore: sent SIGCONT to restored wrapper pid={new_pid}")
                # give wrapper time to reconnect cmd api
                await asyncio.sleep(0.05)
            except Exception as ex:
                self._log(f"restore: failed to send SIGCONT to {new_pid}: {ex}")
        # Wait for cmdapi discovery to become valid and responsive
        if ok:
            ready = await self._wait_cmdapi_ready(timeout_sec=8.0)
            self._log(f'restore: cmdapi ready={ready}')
        # Reconnect TTY to restored wrapper, log all exceptions
        reconnect_errors: list[str] = []
        if ok and self._tty:
            try:
                self._log('restore: tty initial reconnect start')
                await self._tty.reconnect()
                self._log('restore: tty initial reconnect done')
            except Exception as ex:
                msg = "tty.reconnect failed: " + str(ex)
                self._log(msg + "\n" + traceback.format_exc())
                reconnect_errors.append(msg)
        # Force wrapper to reopen TTY queues and reinstall hooks, then reconnect again
        if ok:
            try:
                self._log('restore: request wrapper reopen_tty')
                ep = ShmEndpoint(self.wrap_sock).open(timeout_sec=5.0)
                resp = ep.request({'cmd': 'reopen_tty'})
                ep.close()
                self._log(f'restore: reopen_tty resp={resp}')
                if not (isinstance(resp, dict) and resp.get('ok')):
                    reconnect_errors.append('wrapper reopen_tty returned not ok')
                else:
                    # Give wrapper a moment to recreate discovery
                    self._log('restore: sleep 50ms for tty discovery refresh')
                    await asyncio.sleep(0.05)
                    if self._tty:
                        self._log('restore: tty second reconnect start')
                        await self._tty.reconnect()
                        self._log('restore: tty second reconnect done')
            except Exception as ex:
                msg = "reopen_tty flow failed: " + str(ex)
                self._log(msg + "\n" + traceback.format_exc())
                reconnect_errors.append(msg)
        # Ensure restored wrapper/target are resumed (image may have been taken while paused)
        if ok:
            try:
                self._log('restore: send cmd_resume')
                r = await self.cmd_resume()
                self._log(f'restore: cmd_resume resp={r}')
            except Exception as ex:
                msg = "cmd_resume after restore failed: " + str(ex)
                self._log(msg + "\n" + traceback.format_exc())
                reconnect_errors.append(msg)
        # Post-restore backend commands (optional), e.g., re-init waves
        if ok and self.pre_restore_backend_cmds:
            for cmd in self.pre_restore_backend_cmds:
                try:
                    self._log(f'restore: post-restore inject: {cmd}')
                    await self.cmd_inject(cmd + "\n")
                except Exception:
                    pass
        tail = _read_tail(restore_log, 60)
        self._log(f'restore: done ok={ok} rc={rc} pid={new_pid} errors={reconnect_errors}')
        return {"ok": ok, "rc": rc, "wrapper_pid": new_pid, "images_dir": images_dir, "restore_log": restore_log, "restore_log_tail": tail, "reconnect_errors": reconnect_errors}

    def _collect_shm_entries_from_discovery(self) -> list[tuple[str,int]]:
        def _safe_load(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except Exception:
                return None
        entries: list[tuple[str,int]] = []
        info_cmd = _safe_load(self.wrap_sock)
        if isinstance(info_cmd, dict) and info_cmd.get('type') == 'shm':
            sz = int(info_cmd.get('size') or (1<<16))
            for key in ('req','resp'):
                n = info_cmd.get(key)
                if isinstance(n, str):
                    entries.append((n, sz))
        info_tty = _safe_load(self.data_sock)
        if isinstance(info_tty, dict) and info_tty.get('type') == 'shm':
            sz = int(info_tty.get('size') or (1<<20))
            for key in ('in','out'):
                n = info_tty.get(key)
                if isinstance(n, str):
                    entries.append((n, sz))
        return entries

    def _precreate_shms(self, entries: list[tuple[str,int]]):
        self._precreated_shms = []
        for name, size in entries:
            try:
                shm = shared_memory.SharedMemory(name=name, create=True, size=size)
                self._log(f"precreate shm {name} size={size}")
                self._precreated_shms.append(shm)
            except FileExistsError:
                try:
                    shm = shared_memory.SharedMemory(name=name, create=False)
                    self._log(f"open existing shm {name}")
                    self._precreated_shms.append(shm)
                except Exception as ex:
                    self._log(f"open existing shm failed {name}: {ex}")
            except Exception as ex:
                self._log(f"precreate shm failed {name}: {ex}")

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
        # Install child reaper to avoid zombies from restored wrapper
        self._install_sigchld_reaper()
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
        # Apply log config as env for wrapper
        env.update({k: v for k, v in self.log_cfg.to_env(self.ckpt_dir).items() if v})
        env.setdefault('XSPDB_CKPT_DIR', self.ckpt_dir)
        env.setdefault('XSPDB_CMD_SOCK', self.wrap_sock)
        env.setdefault('XSPDB_TTY_SOCK', self.data_sock)
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
    ap.add_argument("--ckpt-dir", default=None, help="checkpoint directory")
    ap.add_argument("--sock", default=None, help="XSPdb API discovery path (.shm JSON)")
    ap.add_argument("--wrap-sock", default=None, help="wrapper control discovery path (.shm JSON)")
    ap.add_argument("--data-sock", default=None, help="wrapper data discovery path (.shm JSON)")
    ap.add_argument("--", dest="dashdash", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("target", nargs=argparse.REMAINDER, help="target command to run after --")
    args = ap.parse_args()
    ckpt_dir = args.ckpt_dir or os.environ.get('XSPDB_CKPT_DIR', '.xspdb_ckpt')
    api_sock = args.sock or os.environ.get('XSPDB_API_SOCK', os.path.join(ckpt_dir, 'api.shm'))
    wrap_sock = args.wrap_sock or os.environ.get('XSPDB_WRAP_SOCK', os.path.join(ckpt_dir, 'cmdapi.shm'))
    data_sock = args.data_sock or os.environ.get('XSPDB_TTY_SOCK', os.path.join(ckpt_dir, 'tty.shm'))
    # Build log config from env if present
    log_cfg = LogConfig(
        supervisor=os.environ.get('XSPDB_SUPERVISOR_LOG', 'supervisor.log'),
        wrapper_cmd=os.environ.get('XSPDB_WRAPPER_CMD_LOG'),
        wrapper_tty=os.environ.get('XSPDB_WRAPPER_TTY_LOG'),
        sigcont=os.environ.get('XSPDB_SIGCONT_LOG'),
        tty_stdin=os.environ.get('XSPDB_TTY_STDIN_LOG'),
        tty_stdout=os.environ.get('XSPDB_TTY_STDOUT_LOG'),
    )

    sup = WrapperSupervisor(
        ckpt_dir, api_sock, args.target,
        wrap_sock=wrap_sock,
        data_sock=data_sock,
        images_subdir=os.environ.get('XSPDB_IMAGES_SUBDIR', 'images'),
        log_cfg=log_cfg,
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
