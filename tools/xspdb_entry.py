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
  "sock": ".xspdb_ckpt/sock",
  "prefix_key": "^A",
  "cmd_key": "c",
  "target": ["python3", "emu.py"]
}
"""

import argparse
import json
import os
import sys


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

    ckpt_dir = args.ckpt_dir or cfg.get('ckpt_dir', '.xspdb_ckpt')
    sock = args.sock or cfg.get('sock', '.xspdb_ckpt/sock')
    prefix = decode_ctrl(args.prefix_key or cfg.get('prefix_key', '^A'))
    cmd_key = (args.cmd_key or cfg.get('cmd_key', 'c'))
    target = args.target or cfg.get('target', [])

    if not target:
        print('No target specified. Provide in config or after --', file=sys.stderr)
        return 2

    # Lazy import to avoid circulars
    from tools.xspdb_pty_supervisor import PTYFrontend

    fe = PTYFrontend(ckpt_dir, sock, target, prefix_key=prefix, cmd_key=cmd_key.encode()[:1] or b'c')
    os.makedirs(ckpt_dir, exist_ok=True)
    return fe.run()


if __name__ == '__main__':
    raise SystemExit(main())

