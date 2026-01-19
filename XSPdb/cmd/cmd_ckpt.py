#coding=utf-8

"""
CRIU-based checkpoint/restore commands for XSPdb.

This module exposes simple commands to:
  - xckpt_save [name]           Save a checkpoint using CRIU
  - xckpt_list                  List available checkpoints
  - xckpt_restore [name] [--kill]  Restore from a checkpoint (spawns restored process); optionally exit current
  - xckpt_rollback              Restore the latest checkpoint (alias for restore latest)

Notes/Requirements:
  - Requires CRIU installed and usable on the host (criu >= 3.15 recommended).
  - Restoring typically needs CAP_SYS_ADMIN or proper kernel/user configuration (user namespaces, etc.).
  - Dump/restore a running Python + native modules process may fail depending on HW resources (sockets, maps).
  - Prefer invoking from non-TUI mode. If in TUI, the command warns and tries to proceed.
  - Checkpoints are stored under a configurable directory (default ./.xspdb_ckpt/<name>).
"""

import os
import time
import json
import shutil
import socket
import glob
import subprocess

from XSPdb.cmd.util import info, warn, error, message
from XSPdb.cmd.ckpt_utils import load_manifest, save_manifest, rel_target, copy_in, write_files_index, restore_files


class CmdCkpt:
    def __init__(self):
        # Default checkpoint root directory
        self._ckpt_root = os.path.abspath(os.environ.get("XSPDB_CKPT_DIR", ".xspdb_ckpt"))
        os.makedirs(self._ckpt_root, exist_ok=True)
        # Optional supervisor socket (if running under supervisor wrapper)
        self._ckpt_sock = os.environ.get("XSPDB_CKPT_SOCK", "")
        # File manifest path (persist patterns across runs)
        self._ckpt_manifest = os.environ.get("XSPDB_CKPT_MANIFEST", os.path.join(self._ckpt_root, "manifest.json"))
        # Base directory for relative paths
        self._ckpt_base = os.path.abspath(os.environ.get("XSPDB_CKPT_BASE", os.getcwd()))
        # Optional toggle to disable CKPT (for environments without CRIU/root)
        self._ckpt_enabled = str(os.environ.get("XSPDB_CKPT_ENABLE", "1")).lower() in ("1", "true", "yes")

    # ---------- helpers ----------
    def api_criu_available(self):
        return shutil.which("criu") is not None

    def api_ckpt_root(self):
        return self._ckpt_root

    def api_ckpt_path(self, name):
        return os.path.join(self._ckpt_root, name)

    def api_ckpt_latest_file(self):
        return os.path.join(self._ckpt_root, "latest")

    def api_ckpt_metadata_path(self, dir_path):
        return os.path.join(dir_path, "xspdb.meta.json")

    def api_ckpt_list_dirs(self):
        if not os.path.isdir(self._ckpt_root):
            return []
        return sorted([d for d in os.listdir(self._ckpt_root)
                       if os.path.isdir(os.path.join(self._ckpt_root, d))])

    def api_ckpt_resolve_latest(self):
        latest = self.api_ckpt_latest_file()
        if os.path.islink(latest):
            try:
                t = os.readlink(latest)
                # if relative link, resolve
                path = t if os.path.isabs(t) else os.path.join(self._ckpt_root, t)
                if os.path.isdir(path):
                    return os.path.basename(path)
            except OSError:
                pass
        # fallback: find most recent by mtime
        entries = self.api_ckpt_list_dirs()
        if not entries:
            return None
        entries.sort(key=lambda n: os.path.getmtime(self.api_ckpt_path(n)), reverse=True)
        return entries[0]

    def api_ckpt_make_name(self, name=None):
        if name:
            return name
        return time.strftime("ckpt-%Y%m%d-%H%M%S")

    def api_ckpt_write_latest(self, name):
        latest = self.api_ckpt_latest_file()
        dst = self.api_ckpt_path(name)
        try:
            if os.path.islink(latest) or os.path.exists(latest):
                os.remove(latest)
            os.symlink(dst, latest)
        except Exception:
            # best-effort
            pass

    # ---------- core ops ----------
    def api_ckpt_save(self, name=None, leave_running=True, extra_args=None):
        if not self._ckpt_enabled:
            warn("Checkpointing disabled by XSPDB_CKPT_ENABLE")
            return False
        # First, compute and stage external files snapshot
        ckpt_name = self.api_ckpt_make_name(name)
        ckpt_dir = self.api_ckpt_path(ckpt_name)
        os.makedirs(ckpt_dir, exist_ok=True)
        files_dir = os.path.join(ckpt_dir, "files")
        os.makedirs(files_dir, exist_ok=True)
        files = self.api_ckpt_collect_files()
        self.api_ckpt_copy_in(files, files_dir)
        self.api_ckpt_write_files_index(files, files_dir)

        # Prefer supervisor mode if available
        if self._ckpt_sock and os.path.exists(self._ckpt_sock):
            req = {"cmd": "save", "name": name}
            ok, msg = self.api_ckpt_send_supervisor(req)
            if ok:
                info(msg or "Checkpoint saved via supervisor")
                return True
            warn(f"Supervisor save failed: {msg}, fallback to self-criu")
        if not self.api_criu_available():
            error("CRIU not found in PATH. Please install criu.")
            return False
        pid = os.getpid()
        if getattr(self, "in_tui", False):
            warn("Running in TUI. Checkpoint from TTY-bound jobs may be unstable. Consider exiting TUI (xui -> exit).")
        # Best-effort quiesce
        try:
            self.dut.xclock.Disable()
        except Exception:
            pass
        args = [
            "criu", "dump",
            "-t", str(pid),
            "-D", ckpt_dir,
            "-j",                 # --shell-job (job with controlling terminal)
            "--tcp-established",  # allow open TCP (best-effort)
            "--file-locks",       # file locks support
        ]
        if leave_running:
            args.append("--leave-running")
        if extra_args:
            args += list(extra_args)
        info(f"Saving checkpoint to {ckpt_dir} (pid={pid}) [self-criu]")
        res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            error(f"CRIU dump failed: {res.stderr.strip()}")
            return False
        # write metadata
        meta = {
            "name": ckpt_name,
            "time": int(time.time()),
            "cwd": os.getcwd(),
            "argv": getattr(self, "__xspdb_exec_argv__", []),
            "pid": pid,
            "base": self._ckpt_base,
        }
        try:
            with open(self.api_ckpt_metadata_path(ckpt_dir), "w") as f:
                json.dump(meta, f, indent=2)
        except Exception:
            pass
        self.api_ckpt_write_latest(ckpt_name)
        info("Checkpoint saved successfully")
        return True

    def api_ckpt_restore(self, name=None, kill_current=False, extra_args=None):
        if not self._ckpt_enabled:
            warn("Checkpointing disabled by XSPDB_CKPT_ENABLE")
            return False
        # Prefer supervisor mode if available (recommended)
        if self._ckpt_sock and os.path.exists(self._ckpt_sock):
            req = {"cmd": "restore", "name": name}
            ok, msg = self.api_ckpt_send_supervisor(req)
            if ok:
                info(msg or "Restore requested via supervisor")
                if kill_current:
                    warn("Exiting current process as requested (--kill)")
                    raise SystemExit(0)
                return True
            warn(f"Supervisor restore failed: {msg}, fallback to self-criu")
        if not self.api_criu_available():
            error("CRIU not found in PATH. Please install criu.")
            return False
        if name in (None, "", "latest"):
            name = self.api_ckpt_resolve_latest()
        if not name:
            error("No checkpoint found. Use xckpt_save first.")
            return False
        ckpt_dir = self.api_ckpt_path(name)
        if not os.path.isdir(ckpt_dir):
            error(f"Checkpoint '{name}' not found at {ckpt_dir}")
            return False
        # Restore external files first (best effort)
        files_dir = os.path.join(ckpt_dir, "files")
        if os.path.isdir(files_dir):
            self.api_ckpt_restore_files(files_dir)
        args = [
            "criu", "restore",
            "-D", ckpt_dir,
            "-j",           # --shell-job, restore with controlling terminal
            "-d",           # daemonize restored process
        ]
        if extra_args:
            args += list(extra_args)
        info(f"Restoring from checkpoint {ckpt_dir} [self-criu]")
        res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            error(f"CRIU restore failed: {res.stderr.strip()}")
            return False
        info("Restore requested. A new restored process should come up.")
        if kill_current:
            warn("Exiting current process as requested (--kill)")
            try:
                # Try to gracefully exit interactive loop
                raise SystemExit(0)
            except SystemExit:
                raise
        return True

    # ---------- supervisor IPC ----------
    def api_ckpt_send_supervisor(self, payload):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(self._ckpt_sock)
                data = (json.dumps(payload) + "\n").encode()
                s.sendall(data)
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    return False, "empty supervisor response"
                try:
                    resp = json.loads(buf.decode())
                except Exception as e:
                    return False, f"invalid supervisor response: {e}"
                return bool(resp.get("ok", False)), resp.get("msg", "")
        except Exception as e:
            return False, str(e)

    # ---------- file versioning helpers ----------
    def api_ckpt_load_manifest(self):
        return load_manifest(self._ckpt_manifest)

    def api_ckpt_save_manifest(self, data):
        save_manifest(self._ckpt_manifest, data)

    def api_ckpt_collect_files(self):
        mf = self.api_ckpt_load_manifest()
        patterns = mf.get("include", [])
        files = set()
        # auto-detected files
        for p in [getattr(self, "exec_bin_file", None), getattr(self, "flash_bin_file", None)]:
            if p and os.path.isfile(p):
                files.add(os.path.abspath(p))
        # user patterns
        for pat in patterns:
            for m in glob.glob(pat, recursive=True):
                if os.path.isfile(m):
                    files.add(os.path.abspath(m))
        return sorted(list(files))

    def api_ckpt_rel_target(self, path):
        return rel_target(path, self._ckpt_base)

    def api_ckpt_copy_in(self, files, files_dir):
        copy_in(files, files_dir, self._ckpt_base)

    def api_ckpt_write_files_index(self, files, files_dir):
        write_files_index(files, files_dir, self._ckpt_base)

    def api_ckpt_restore_files(self, files_dir):
        restored = restore_files(files_dir)
        info(f"Restored {restored} files from checkpoint")

    # ---------- manifest CLI ----------
    def do_xckpt_manifest_add(self, arg):
        """Add include pattern(s) to external file manifest

        Args:
            pattern(s): space separated glob patterns (supports **)
        Example:
            xckpt_manifest_add ready-to-run/*.bin scripts/**/*.py
        """
        parts = [p for p in arg.strip().split() if p]
        if not parts:
            self.do_xckpt_manifest_list("")
            return
        mf = self.api_ckpt_load_manifest()
        inc = mf.get("include", [])
        for p in parts:
            if p not in inc:
                inc.append(p)
        mf["include"] = inc
        self.api_ckpt_save_manifest(mf)
        info("manifest updated")

    def do_xckpt_manifest_clear(self, arg):
        """Clear external file manifest patterns"""
        self.api_ckpt_save_manifest({"include": []})
        info("manifest cleared")

    def do_xckpt_manifest_list(self, arg):
        """Show manifest patterns and expanded files"""
        mf = self.api_ckpt_load_manifest()
        inc = mf.get("include", [])
        message("Patterns: " + (" ".join(inc) if inc else "<empty>"))
        files = self.api_ckpt_collect_files()
        for f in files:
            message(f)

    # ---------- CLI commands ----------
    def do_xckpt_save(self, arg):
        """Save a CRIU checkpoint

        Args:
            name (optional): checkpoint name
        Usage:
            xckpt_save [name]
        """
        name = arg.strip() if arg.strip() else None
        ok = self.api_ckpt_save(name=name, leave_running=True)
        if not ok:
            error("xckpt_save failed")

    def do_xckpt_list(self, arg):
        """List checkpoints in the checkpoint directory"""
        latest = self.api_ckpt_resolve_latest()
        for d in self.api_ckpt_list_dirs():
            tag = " (latest)" if d == latest else ""
            message(f"{d}{tag}")
        if not self.api_ckpt_list_dirs():
            message("No checkpoints found")

    def do_xckpt_restore(self, arg):
        """Restore is disabled in TUI; use supervisor command mode.

        This command is intentionally disabled to avoid double-write and to
        enforce restores only from the PTY supervisor's command mode.
        """
        error("xckpt_restore is disabled inside TUI. Use PTY supervisor command mode: Ctrl-A c -> restore [name|latest]")

    def do_xckpt_rollback(self, arg):
        """Alias to restore latest; disabled inside TUI.

        Use PTY supervisor command mode instead (Ctrl-A c -> restore latest).
        """
        error("xckpt_rollback is disabled inside TUI. Use PTY supervisor command mode: Ctrl-A c -> restore latest")
