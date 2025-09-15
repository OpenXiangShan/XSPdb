#!/usr/bin/env python3
#coding=utf-8

"""
XSPdb PTY Supervisor: run a TUI backend under a PTY, proxy I/O to the user
terminal, and expose CRIU checkpoint/restore controls so rollback feels
"in-place" to the user.

Usage:
  python3 tools/xspdb_pty_supervisor.py [--ckpt-dir DIR] [--sock PATH]
                                        [--prefix-key CTRL] [--cmd-key KEY] -- <target ...>

Hotkeys (default prefix: Ctrl-A, cmd-key: c):
  <prefix> <cmd-key>  Enter checkpoint command mode (no direct actions)
              Commands in mode: list | save [name] | restore [name|latest] | exit

Notes:
  - Requires CRIU installed and usable; restoring may need capabilities.
  - The backend sees env: XSPDB_CKPT_DIR, XSPDB_CKPT_SOCK so XSPdb's xckpt_*
    commands will route to this supervisor for stable checkpointing.
  - The restored backend attaches to the same PTY slave; the frontend keeps
    the master end so I/O continues seamlessly. Command mode pauses backend to
    avoid any double-writes during save/restore.
"""

import argparse
import errno
import fcntl
import glob
import hashlib
import json
import os
import pty
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import tty
import time


def criu_available():
    return shutil.which("criu") is not None


def ckpt_path(root, name):
    return os.path.join(root, name)


def ckpt_latest(root):
    latest = os.path.join(root, "latest")
    if os.path.islink(latest):
        try:
            t = os.readlink(latest)
            p = t if os.path.isabs(t) else os.path.join(root, t)
            if os.path.isdir(p):
                return os.path.basename(p)
        except OSError:
            pass
    entries = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    if not entries:
        return None
    entries.sort(key=lambda n: os.path.getmtime(os.path.join(root, n)), reverse=True)
    return entries[0]


def write_latest(root, name):
    latest = os.path.join(root, "latest")
    dst = os.path.join(root, name)
    try:
        if os.path.islink(latest) or os.path.exists(latest):
            os.remove(latest)
        os.symlink(dst, latest)
    except Exception:
        pass


def set_winsize(fd, rows, cols, xpix=0, ypix=0):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, xpix, ypix))


def get_winsize(fd):
    try:
        s = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\x00'*8)
        rows, cols, xpix, ypix = struct.unpack('HHHH', s)
        return rows or 24, cols or 80
    except Exception:
        return 24, 80


class FileSnap:
    def __init__(self, base, manifest_path):
        self.base = base
        self.manifest = manifest_path

    def _load_manifest(self):
        data = {"include": []}
        try:
            if os.path.exists(self.manifest):
                with open(self.manifest, "r") as f:
                    j = json.load(f)
                    if isinstance(j, dict):
                        data.update(j)
        except Exception:
            pass
        return data

    def _collect_files(self):
        mf = self._load_manifest()
        patterns = mf.get("include", [])
        files = set()
        for pat in patterns:
            for m in glob.glob(pat, recursive=True):
                if os.path.isfile(m):
                    files.add(os.path.abspath(m))
        return sorted(list(files))

    def _rel_target(self, path):
        ap = os.path.abspath(path)
        try:
            rel = os.path.relpath(ap, self.base)
            if rel.startswith(".."):  # outside base
                return os.path.join("abs", ap.lstrip(os.sep))
            return rel
        except Exception:
            return os.path.join("abs", ap.lstrip(os.sep))

    def _sha256(self, path):
        try:
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def snapshot(self, dst_dir):
        os.makedirs(dst_dir, exist_ok=True)
        files = self._collect_files()
        for src in files:
            rel = self._rel_target(src)
            dd = os.path.join(dst_dir, rel)
            os.makedirs(os.path.dirname(dd), exist_ok=True)
            try:
                shutil.copy2(src, dd)
            except Exception:
                pass
        index = []
        for src in files:
            rel = self._rel_target(src)
            dd = os.path.join(dst_dir, rel)
            index.append({"src": os.path.abspath(src), "rel": rel, "sha256": self._sha256(dd)})
        try:
            with open(os.path.join(dst_dir, "files_index.json"), "w") as f:
                json.dump(index, f, indent=2)
        except Exception:
            pass

    def restore(self, src_dir):
        idx = os.path.join(src_dir, "files_index.json")
        try:
            with open(idx, "r") as f:
                index = json.load(f)
        except Exception:
            index = []
        for ent in index:
            rel = ent.get("rel")
            ss = os.path.join(src_dir, rel)
            dd = ent.get("src")
            if not (rel and dd and os.path.isfile(ss)):
                continue
            os.makedirs(os.path.dirname(dd), exist_ok=True)
            try:
                shutil.copy2(ss, dd)
            except Exception:
                pass


class CkptServer:
    def __init__(self, sock_path, ckpt_dir, child_pid, base, manifest):
        self.sock_path = sock_path
        self.ckpt_dir = ckpt_dir
        self.child_pid = child_pid
        self.base = base
        self.snap = FileSnap(base, manifest)
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(self.sock_path) or ".", exist_ok=True)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock_path)
        self.srv.listen(1)
        self._stop = False

    def update_child(self, pid):
        self.child_pid = pid

    def stop(self):
        self._stop = True
        try:
            self.srv.close()
        except Exception:
            pass
        try:
            os.unlink(self.sock_path)
        except Exception:
            pass

    def serve_once(self, timeout=0.2):
        self.srv.settimeout(timeout)
        try:
            conn, _ = self.srv.accept()
        except socket.timeout:
            return
        except OSError:
            return
        with conn:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            try:
                req = json.loads(data.decode())
            except Exception as e:
                conn.sendall(json.dumps({"ok": False, "msg": f"bad request: {e}"}).encode())
                return
            cmd = req.get("cmd")
            if cmd == "save":
                name = req.get("name") or time.strftime("ckpt-%Y%m%d-%H%M%S")
                ok, msg = self._do_save(name)
                conn.sendall(json.dumps({"ok": ok, "msg": msg}).encode())
            elif cmd == "restore":
                name = req.get("name")
                ok, msg, new_pid = self._do_restore(name)
                if ok and new_pid:
                    self.update_child(new_pid)
                conn.sendall(json.dumps({"ok": ok, "msg": msg}).encode())
            elif cmd == "list":
                lst = [d for d in os.listdir(self.ckpt_dir) if os.path.isdir(os.path.join(self.ckpt_dir, d))]
                conn.sendall(json.dumps({"ok": True, "list": sorted(lst)}).encode())
            else:
                conn.sendall(json.dumps({"ok": False, "msg": "unknown cmd"}).encode())

    def _do_save(self, name):
        if not criu_available():
            return False, "criu not found"
        dst = ckpt_path(self.ckpt_dir, name)
        os.makedirs(dst, exist_ok=True)
        # files snapshot
        self.snap.snapshot(os.path.join(dst, "files"))
        args = [
            "criu", "dump",
            "-t", str(self.child_pid),
            "-D", dst,
            "-j",
            "--tcp-established",
            "--file-locks",
            "--leave-running",
        ]
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            return False, (r.stderr.strip() or r.stdout.strip() or "criu dump failed")
        write_latest(self.ckpt_dir, name)
        return True, f"checkpoint saved at {dst}"

    def _do_restore(self, name):
        if not criu_available():
            return False, "criu not found", None
        if not name or name == "latest":
            name = ckpt_latest(self.ckpt_dir)
        if not name:
            return False, "no checkpoint", None
        src = ckpt_path(self.ckpt_dir, name)
        if not os.path.isdir(src):
            return False, f"checkpoint not found: {name}", None
        # restore files first
        files_dir = os.path.join(src, "files")
        if os.path.isdir(files_dir):
            self.snap.restore(files_dir)
        pidfile = os.path.join(src, "restored.pid")
        args = [
            "criu", "restore",
            "-D", src,
            "-j",
            "-d",
            "--pidfile", pidfile,
        ]
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            return False, (r.stderr.strip() or r.stdout.strip() or "criu restore failed"), None
        new_pid = None
        try:
            with open(pidfile, "r") as f:
                new_pid = int(f.read().strip())
        except Exception:
            new_pid = None
        return True, f"restore requested from {src}", new_pid


class PTYFrontend:
    def __init__(self, ckpt_dir, sock_path, target_argv, prefix_key='\x01', cmd_key=b'c'):
        self.ckpt_dir = os.path.abspath(ckpt_dir)
        self.sock_path = os.path.abspath(sock_path)
        self.target = target_argv
        self.prefix = prefix_key  # default Ctrl-A
        self.cmd_key = cmd_key if isinstance(cmd_key, (bytes, bytearray)) else str(cmd_key).encode()
        self._prefix_mode = False
        self._cmd_mode = False
        self._cmd_buf = bytearray()
        self._orig_term = None
        self.child_pid = None
        self.master_fd = None
        self.slave_fd = None
        self.selector = selectors.DefaultSelector()
        self.server = None
        self.base = os.path.abspath(os.getcwd())
        self.manifest = os.environ.get("XSPDB_CKPT_MANIFEST", os.path.join(self.ckpt_dir, "manifest.json"))

    def _enter_raw(self):
        self._orig_term = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def _exit_raw(self):
        try:
            if self._orig_term:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._orig_term)
        except Exception:
            pass

    def _spawn_child(self):
        self.master_fd, self.slave_fd = pty.openpty()
        # inherit current window size
        rows, cols = get_winsize(sys.stdin.fileno())
        try:
            set_winsize(self.slave_fd, rows, cols)
        except Exception:
            pass

        pid = os.fork()
        if pid == 0:
            # Child
            try:
                os.setsid()
                os.close(self.master_fd)
                os.dup2(self.slave_fd, 0)
                os.dup2(self.slave_fd, 1)
                os.dup2(self.slave_fd, 2)
                # Set controlling TTY
                fcntl.ioctl(self.slave_fd, termios.TIOCSCTTY, 0)
                # Prepare env
                env = os.environ.copy()
                env["XSPDB_CKPT_DIR"] = self.ckpt_dir
                env["XSPDB_CKPT_SOCK"] = self.sock_path
                os.execvpe(self.target[0], self.target, env)
            except Exception as e:
                os.write(2, f"exec failed: {e}\n".encode())
                os._exit(127)
        # Parent
        self.child_pid = pid
        os.close(self.slave_fd)
        # non-blocking
        for fd in (self.master_fd, sys.stdin.fileno()):
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        # control server for XSPdb's xckpt_* commands
        self.server = CkptServer(self.sock_path, self.ckpt_dir, self.child_pid, self.base, self.manifest)

        # handle window size change
        signal.signal(signal.SIGWINCH, self._on_winch)

    def _on_winch(self, signum, frame):
        try:
            rows, cols = get_winsize(sys.stdin.fileno())
            set_winsize(self.master_fd, rows, cols)
        except Exception:
            pass

    def _kill_child(self):
        try:
            os.kill(self.child_pid, signal.SIGTERM)
        except Exception:
            pass
        # wait for exit
        try:
            os.waitpid(self.child_pid, 0)
        except Exception:
            pass

    def _enter_cmd_mode(self):
        if self._cmd_mode:
            return
        self._cmd_mode = True
        self._cmd_buf.clear()
        # Pause backend to avoid double-write
        try:
            os.kill(self.child_pid, signal.SIGSTOP)
        except Exception:
            pass
        msg = (
            "\r\n[ckpt] Enter command mode. Commands: list | save [name] | "
            "restore [name|latest] | exit\r\n[ckpt] > "
        )
        os.write(sys.stdout.fileno(), msg.encode())

    def _exit_cmd_mode(self):
        if not self._cmd_mode:
            return
        self._cmd_mode = False
        self._cmd_buf.clear()
        # Resume backend if still alive
        try:
            os.kill(self.child_pid, signal.SIGCONT)
        except Exception:
            pass
        os.write(sys.stdout.fileno(), b"\r\n[ckpt] Exit command mode.\r\n")

    def _handle_cmd_line(self, line):
        # line is a str without trailing newline
        parts = [p for p in line.strip().split() if p]
        if not parts:
            os.write(sys.stdout.fileno(), b"[ckpt] > ")
            return
        cmd = parts[0]
        args = parts[1:]
        if cmd == 'help' or cmd == '?':
            os.write(sys.stdout.fileno(), (
                "[ckpt] Commands: list | save [name] | restore [name|latest] | exit\r\n"
            ).encode())
        elif cmd == 'list':
            lst = [d for d in os.listdir(self.ckpt_dir) if os.path.isdir(os.path.join(self.ckpt_dir, d))]
            if not lst:
                os.write(sys.stdout.fileno(), b"[ckpt] <empty>\r\n")
            else:
                latest = ckpt_latest(self.ckpt_dir)
                for d in sorted(lst):
                    tag = " (latest)" if d == latest else ""
                    os.write(sys.stdout.fileno(), f"{d}{tag}\r\n".encode())
        elif cmd == 'save':
            name = args[0] if args else time.strftime("ckpt-%Y%m%d-%H%M%S")
            ok, msg = self._save(name)
            out = msg or ("save ok" if ok else "save failed")
            os.write(sys.stdout.fileno(), ("[ckpt] " + out + "\r\n").encode())
        elif cmd == 'restore':
            name = args[0] if args else 'latest'
            # Ensure no double-write: stop/kill old before restore
            try:
                os.kill(self.child_pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                os.waitpid(self.child_pid, 0)
            except Exception:
                pass
            ok, msg, new_pid = self._restore(name)
            if ok and new_pid:
                self.child_pid = new_pid
                if self.server:
                    self.server.update_child(new_pid)
            out = msg or ("restore ok" if ok else "restore failed")
            os.write(sys.stdout.fileno(), ("[ckpt] " + out + "\r\n").encode())
        elif cmd == 'exit':
            self._exit_cmd_mode()
            return
        else:
            os.write(sys.stdout.fileno(), b"[ckpt] unknown command\r\n")
        os.write(sys.stdout.fileno(), b"[ckpt] > ")

    def _handle_prefix_cmd(self, ch):
        # ch is a one-byte string
        if ch == self.cmd_key:
            self._enter_cmd_mode()

    def _save(self, name):
        if not criu_available():
            return False, "criu not found"
        dst = ckpt_path(self.ckpt_dir, name)
        os.makedirs(dst, exist_ok=True)
        # files snapshot
        FileSnap(self.base, self.manifest).snapshot(os.path.join(dst, "files"))
        args = [
            "criu", "dump",
            "-t", str(self.child_pid),
            "-D", dst,
            "-j",
            "--tcp-established",
            "--file-locks",
            "--leave-running",
        ]
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            return False, (r.stderr.strip() or r.stdout.strip() or "criu dump failed")
        write_latest(self.ckpt_dir, name)
        return True, f"checkpoint saved at {dst}"

    def _restore(self, name):
        if not criu_available():
            return False, "criu not found", None
        if not name or name == "latest":
            name = ckpt_latest(self.ckpt_dir)
        if not name:
            return False, "no checkpoint", None
        src = ckpt_path(self.ckpt_dir, name)
        if not os.path.isdir(src):
            return False, f"checkpoint not found: {name}", None
        FileSnap(self.base, self.manifest).restore(os.path.join(src, "files"))
        pidfile = os.path.join(src, "restored.pid")
        args = [
            "criu", "restore",
            "-D", src,
            "-j",
            "-d",
            "--pidfile", pidfile,
        ]
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            return False, (r.stderr.strip() or r.stdout.strip() or "criu restore failed"), None
        new_pid = None
        try:
            with open(pidfile, "r") as f:
                new_pid = int(f.read().strip())
        except Exception:
            new_pid = None
        return True, f"restore requested from {src}", new_pid

    def run(self):
        os.makedirs(self.ckpt_dir, exist_ok=True)
        self._enter_raw()
        try:
            self._spawn_child()
            self.selector.register(self.master_fd, selectors.EVENT_READ)
            self.selector.register(sys.stdin.fileno(), selectors.EVENT_READ)

            # reaper for child
            def _sigchld(signum, frame):
                try:
                    while True:
                        pid, _ = os.waitpid(-1, os.WNOHANG)
                        if pid == 0:
                            break
                except ChildProcessError:
                    pass
            signal.signal(signal.SIGCHLD, _sigchld)

            while True:
                # Serve control socket briefly to handle xckpt_* calls
                if self.server and not self.server._stop:
                    self.server.serve_once(timeout=0.0)
                events = self.selector.select(timeout=0.05)
                for key, _ in events:
                    if key.fd == self.master_fd:
                        if self._cmd_mode:
                            # pause backend output during command mode
                            try:
                                _ = os.read(self.master_fd, 4096)
                            except Exception:
                                pass
                            continue
                        try:
                            data = os.read(self.master_fd, 4096)
                        except OSError as e:
                            if e.errno in (errno.EIO, errno.EBADF):
                                return 0
                            continue
                        if not data:
                            return 0
                        os.write(sys.stdout.fileno(), data)
                    else:  # stdin
                        try:
                            data = os.read(sys.stdin.fileno(), 1024)
                        except OSError:
                            data = b""
                        if not data:
                            continue
                        # hotkey interception
                        for b in data:
                            ch = bytes([b])
                            if self._prefix_mode:
                                self._prefix_mode = False
                                self._handle_prefix_cmd(ch)
                                continue
                            if ch == self.prefix.encode(errors='ignore'):
                                self._prefix_mode = True
                                continue
                            if self._cmd_mode:
                                # simple line editor: handle CR, BS, printable
                                if ch in (b'\r', b'\n'):
                                    line = self._cmd_buf.decode(errors='ignore')
                                    self._cmd_buf.clear()
                                    os.write(sys.stdout.fileno(), b"\r\n")
                                    self._handle_cmd_line(line)
                                elif ch in (b'\x7f', b'\b'):
                                    if self._cmd_buf:
                                        self._cmd_buf = self._cmd_buf[:-1]
                                        os.write(sys.stdout.fileno(), b"\b \b")
                                else:
                                    # rudimentary filter of control chars
                                    if 32 <= b <= 126:
                                        self._cmd_buf.append(b)
                                        os.write(sys.stdout.fileno(), ch)
                                continue
                            os.write(self.master_fd, ch)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                if self.server:
                    self.server.stop()
            except Exception:
                pass
            self._exit_raw()
        return 0


def main():
    ap = argparse.ArgumentParser(description="XSPdb PTY CRIU Supervisor")
    ap.add_argument("--ckpt-dir", default=".xspdb_ckpt", help="checkpoint directory")
    ap.add_argument("--sock", default=".xspdb_ckpt/sock", help="unix socket path for control")
    ap.add_argument("--prefix-key", default="^A", help="control prefix key (e.g., ^A for Ctrl-A)")
    ap.add_argument("--cmd-key", default="c", help="command-mode key after prefix (single char)")
    ap.add_argument("--", dest="dashdash", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("target", nargs=argparse.REMAINDER, help="target command to run after --")
    args = ap.parse_args()

    if not args.target:
        print("Usage: tools/xspdb_pty_supervisor.py -- <target command...>", file=sys.stderr)
        return 2

    # decode prefix key
    prefix = '\x01'  # Ctrl-A default
    s = args.prefix_key
    if s.startswith('^') and len(s) == 2:
        ch = s[1].upper()
        prefix = chr(ord(ch) - ord('@'))
    elif len(s) == 1:
        prefix = s

    cmd_key = args.cmd_key.encode() if len(args.cmd_key) == 1 else b'c'
    fe = PTYFrontend(args.ckpt_dir, args.sock, args.target, prefix_key=prefix, cmd_key=cmd_key)
    rc = fe.run()
    return rc


if __name__ == "__main__":
    sys.exit(main())
