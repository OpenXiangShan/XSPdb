#!/usr/bin/env python3
#coding=utf-8

"""
XSPdb Launcher: read a JSON config and start the PTY supervisor with
configured options to simplify invocation.

Usage:
  python3 tools/xspdb_entry.py [-c xspdb.json] [--ckpt-dir DIR]
                               [--sock PATH] [--prefix-key ^A]
                               [--cmd-key c] [--] [target ...]

If target is omitted on CLI, it is read from config (field: target array).
CLI flags override config values.

Config (JSON) example (xspdb.json):
{
  "ckpt_dir": ".xspdb_ckpt",
  "sock": ".xspdb_ckpt/api.shm",
  "prefix_key": "^A",
  "cmd_key": "c",
  "target": ["python3", "emu.py"]
  ,"manifest_include": ["waves/*.fst", "waves/*.vcd", ".coverage", "coverage.xml", "cov/**/*.info", "build/**/*.gcda", "build/**/*.gcno"],
  "pre_save_backend_cmds": ["xwave_flush"],
  "criu": { "lazy_pages": false, "track_mem": false, "prev_images_dir": "" },
  "env": { "MY_FLAG": "1", "UNSET_THIS": null }
}
"""

import argparse
import json
import os
import sys
import importlib
import asyncio
import importlib.util


def decode_ctrl(s):
    if not s:
        return '\x01'
    if s.startswith('^') and len(s) == 2:
        ch = s[1].upper()
        return chr(ord(ch) - ord('@'))
    if len(s) == 1:
        return s
    return '\x01'


def load_config(path):
    cfg = {}
    try:
        with open(path, 'r') as f:
            cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
    except Exception:
        cfg = {}
    return cfg


def main():
    ap = argparse.ArgumentParser(description="XSPdb PTY Launcher")
    ap.add_argument('-c', '--config', default='xspdb.json', help='config JSON path')
    ap.add_argument('--ckpt-dir', help='checkpoint directory')
    ap.add_argument('--sock', help='unix control socket path')
    ap.add_argument('--prefix-key', help='prefix key, e.g., ^A for Ctrl-A')
    ap.add_argument('--cmd-key', help='command-mode key, default c')
    ap.add_argument('--', dest='dashdash', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('target', nargs=argparse.REMAINDER, help='target command to run after --')
    args = ap.parse_args()

    cfg = load_config(args.config)

    ckpt_dir = args.ckpt_dir or cfg.get('ckpt_dir', os.environ.get('XSPDB_CKPT_DIR', '.xspdb_ckpt'))
    sock = args.sock or cfg.get('sock', os.environ.get('XSPDB_API_SOCK', os.path.join(ckpt_dir, 'api.shm')))
    prefix = decode_ctrl(args.prefix_key or cfg.get('prefix_key', '^A'))
    cmd_key = (args.cmd_key or cfg.get('cmd_key', 'c'))
    target = args.target or cfg.get('target', [])
    images_subdir = cfg.get('images_subdir', os.environ.get('XSPDB_IMAGES_SUBDIR', 'images'))
    # LogConfig via nested object or env fallbacks
    logs_cfg = cfg.get('logs', {}) if isinstance(cfg.get('logs', {}), dict) else {}
    from tools.xspdb_supervisor import LogConfig
    log_cfg = LogConfig(
        supervisor=logs_cfg.get('supervisor', os.environ.get('XSPDB_SUPERVISOR_LOG', 'supervisor.log')),
        wrapper_cmd=logs_cfg.get('wrapper_cmd', os.environ.get('XSPDB_WRAPPER_CMD_LOG')),
        wrapper_tty=logs_cfg.get('wrapper_tty', os.environ.get('XSPDB_WRAPPER_TTY_LOG')),
        sigcont=logs_cfg.get('sigcont', os.environ.get('XSPDB_SIGCONT_LOG')),
        tty_stdin=logs_cfg.get('tty_stdin', os.environ.get('XSPDB_TTY_STDIN_LOG')),
        tty_stdout=logs_cfg.get('tty_stdout', os.environ.get('XSPDB_TTY_STDOUT_LOG')),
    )
    manifest_include = cfg.get('manifest_include', [])
    pre_save_backend_cmds = cfg.get('pre_save_backend_cmds', [])
    pre_restore_backend_cmds = cfg.get('pre_restore_backend_cmds', [])
    criu_cfg = cfg.get('criu', {}) if isinstance(cfg.get('criu', {}), dict) else {}
    enable_lazy_pages = bool(cfg.get('lazy_pages', criu_cfg.get('lazy_pages', False)))
    enable_track_mem = bool(cfg.get('track_mem', criu_cfg.get('track_mem', False)))
    prev_images_dir = cfg.get('prev_images_dir', criu_cfg.get('prev_images_dir', '')) or None
    pyhooks = cfg.get('python_hooks', {}) if isinstance(cfg.get('python_hooks', {}), dict) else {}
    pre_save_py = pyhooks.get('pre_save')
    post_save_py = pyhooks.get('post_save')
    pre_restore_py = pyhooks.get('pre_restore')
    post_restore_py = pyhooks.get('post_restore')
    env_cfg = cfg.get('env', {}) if isinstance(cfg.get('env', {}), dict) else {}

    def _load_callable(spec):
        if not spec:
            return None
        # spec could be 'module:func' or '/path/to/file.py:func'
        if ':' not in spec:
            return None
        mod, fn = spec.split(':', 1)
        try:
            if mod.endswith('.py') and os.path.isfile(mod):
                name = os.path.splitext(os.path.basename(mod))[0]
                specm = importlib.util.spec_from_file_location(name, mod)
                module = importlib.util.module_from_spec(specm)
                assert specm and specm.loader
                specm.loader.exec_module(module)  # type: ignore
            else:
                module = importlib.import_module(mod)
            cb = getattr(module, fn, None)
            return cb if callable(cb) else None
        except Exception:
            return None

    pre_save_cb = _load_callable(pre_save_py)
    post_save_cb = _load_callable(post_save_py)
    pre_restore_cb = _load_callable(pre_restore_py)
    post_restore_cb = _load_callable(post_restore_py)

    if not target:
        print('No target specified. Provide in config or after --', file=sys.stderr)
        return 2

    # Ensure ckpt dir and manifest
    os.makedirs(ckpt_dir, exist_ok=True)
    manifest_path = os.path.join(ckpt_dir, 'manifest.json')
    if isinstance(manifest_include, list) and manifest_include:
        try:
            with open(manifest_path, 'w') as f:
                json.dump({"include": manifest_include}, f, indent=2)
        except Exception:
            pass


    # 新架构：使用 Socket-Wrapper supervisor
    from tools.xspdb_supervisor import WrapperSupervisor

    # Discovery paths for wrapper (allow override)
    wrap_sock = cfg.get('wrap_sock') or os.path.join(ckpt_dir, 'cmdapi.shm')
    data_sock = cfg.get('data_sock') or os.path.join(ckpt_dir, 'tty.shm')

    # No need to push log envs here; supervisor will derive and export for wrapper

    sup = WrapperSupervisor(
        ckpt_dir, sock, target,
        wrap_sock=wrap_sock,
        enable_lazy_pages=enable_lazy_pages,
        enable_track_mem=enable_track_mem,
        prev_images_dir=prev_images_dir,
        pre_save_backend_cmds=pre_save_backend_cmds,
        pre_restore_backend_cmds=pre_restore_backend_cmds,
        data_sock=data_sock,
        prefix=prefix,
        cmd_key=(cmd_key[:1] if isinstance(cmd_key, str) else 'c'),
        env_overrides=env_cfg,
        images_subdir=images_subdir,
        log_cfg=log_cfg,
    )
    return asyncio.run(sup.run())


if __name__ == '__main__':
    raise SystemExit(main())
