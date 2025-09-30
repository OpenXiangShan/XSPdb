#!/usr/bin/env python3
#coding=utf-8

"""
Example client for the shared-memory CmdApiServer and TtyForwardServer.

Usage:
  python3 tools/shm_client_example.py --ckpt-dir .xspdb_ckpt --which cmd ping
  python3 tools/shm_client_example.py --ckpt-dir .xspdb_ckpt --which cmd inject --data "hello"
  python3 tools/shm_client_example.py --ckpt-dir .xspdb_ckpt --which cmd shutdown
  python3 tools/shm_client_example.py --ckpt-dir .xspdb_ckpt --which api status
  python3 tools/shm_client_example.py --ckpt-dir .xspdb_ckpt --which api save --data NAME
  python3 tools/shm_client_example.py --ckpt-dir .xspdb_ckpt --which api restore --data NAME|latest
  python3 tools/shm_client_example.py --ckpt-dir .xspdb_ckpt --which tty write --data "hello tty"
  python3 tools/shm_client_example.py --ckpt-dir .xspdb_ckpt --which tty read
"""

import argparse
import os
import sys
import json
import time
from multiprocessing import shared_memory
import struct

_HDR_SIZE = 16
_WRAP_MARK = 0xFFFFFFFF


class SharedMsgQueue:
    def __init__(self, name: str):
        self.shm = shared_memory.SharedMemory(name=name)
        self.buf = self.shm.buf
        self.size = len(self.buf)
        self.capacity = self.size - _HDR_SIZE

    def close(self):
        try:
            self.shm.close()
        except Exception:
            pass

    def _ru64(self, off):
        return struct.unpack_from('<Q', self.buf, off)[0]

    def _wu64(self, off, v):
        struct.pack_into('<Q', self.buf, off, int(v) & 0xFFFFFFFFFFFFFFFF)

    def push(self, data: bytes) -> bool:
        head = self._ru64(0); tail = self._ru64(8)
        cap = self.capacity
        used = head - tail if head >= tail else (head + cap - tail)
        free = cap - used
        need = 4 + len(data)
        if free < need:
            return False
        off = head % cap
        contig = cap - off
        if contig < need:
            if contig >= 4:
                struct.pack_into('>I', self.buf, _HDR_SIZE + off, _WRAP_MARK)
                head += 4; off = head % cap
            head += (cap - off); off = 0
        struct.pack_into('>I', self.buf, _HDR_SIZE + off, len(data))
        self.buf[_HDR_SIZE + off + 4 : _HDR_SIZE + off + 4 + len(data)] = data
        head += need
        self._wu64(0, head)
        return True

    def pop(self) -> bytes | None:
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


def load_discovery(path: str) -> dict:
    with open(path, 'r') as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    import os
    ap.add_argument('--ckpt-dir', default=os.environ.get('XSPDB_CKPT_DIR', '.xspdb_ckpt'))
    ap.add_argument('--which', choices=['cmd', 'tty', 'api'], required=True)
    ap.add_argument('op', nargs='?')
    ap.add_argument('--data')
    args = ap.parse_args()

    if args.which == 'cmd':
        info = load_discovery(os.path.join(args.ckpt_dir, 'cmdapi.shm'))
        q_req = SharedMsgQueue(info['req'])
        q_resp = SharedMsgQueue(info['resp'])
        def ask(obj: dict) -> dict:
            payload = (json.dumps(obj) + '\n').encode('utf-8')
            for _ in range(200):
                if q_req.push(payload):
                    break
                time.sleep(0.01)
            for _ in range(500):
                data = q_resp.pop()
                if data:
                    return json.loads(data.decode('utf-8'))
                time.sleep(0.01)
            return {"ok": False, "error": "timeout"}
        if args.op == 'ping' or not args.op:
            print(ask({"cmd":"ping"}))
        elif args.op == 'inject':
            print(ask({"cmd":"inject", "args": {"data": args.data or ""}}))
        elif args.op == 'shutdown':
            print(ask({"cmd":"shutdown"}))
        else:
            print(ask({"cmd": args.op, "args": {}}))
        q_req.close(); q_resp.close()
    elif args.which == 'tty':
        info = load_discovery(os.path.join(args.ckpt_dir, 'tty.shm'))
        q_in = SharedMsgQueue(info['in'])
        q_out = SharedMsgQueue(info['out'])
        if args.op == 'write':
            data = (args.data or '').encode('utf-8')
            ok = q_in.push(data)
            print({'ok': ok})
        else:
            data = q_out.pop()
            if data:
                print(data.decode('utf-8', errors='ignore'))
            else:
                print('')
        q_in.close(); q_out.close()
    else:
        info = load_discovery(os.path.join(args.ckpt_dir, 'api.shm'))
        q_req = SharedMsgQueue(info['req'])
        q_resp = SharedMsgQueue(info['resp'])
        def ask(obj: dict) -> dict:
            payload = (json.dumps(obj) + '\n').encode('utf-8')
            for _ in range(200):
                if q_req.push(payload):
                    break
                time.sleep(0.01)
            for _ in range(500):
                data = q_resp.pop()
                if data:
                    return json.loads(data.decode('utf-8'))
                time.sleep(0.01)
            return {"ok": False, "error": "timeout"}
        op = args.op or 'status'
        if op == 'status':
            print(ask({"cmd":"status"}))
        elif op == 'save':
            print(ask({"cmd":"save", "args": {"name": args.data}}))
        elif op == 'restore':
            print(ask({"cmd":"restore", "args": {"name": args.data}}))
        elif op == 'shutdown':
            print(ask({"cmd":"shutdown"}))
        else:
            print(ask({"cmd": op, "args": {"data": args.data}}))


if __name__ == '__main__':
    raise SystemExit(main())
