#!/usr/bin/env python3
#coding=utf-8

import json
import os
import time
from typing import Optional

from tools.shm_queue import SharedMsgQueue


class ShmEndpoint:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.req: Optional[SharedMsgQueue] = None
        self.resp: Optional[SharedMsgQueue] = None

    def open(self, timeout_sec: float = 5.0):
        info = {}
        deadline = time.time() + max(0.0, float(timeout_sec))
        last_err = None
        while time.time() < deadline:
            try:
                with open(self.path, 'r') as f:
                    info = json.load(f)
                if info.get('type') == 'shm':
                    break
                last_err = f"bad_type={info.get('type')}"
            except Exception as ex:
                last_err = str(ex)
            time.sleep(0.01)
        if info.get('type') != 'shm':
            raise RuntimeError('discovery file is not shm type: ' + self.path + (f' ({last_err})' if last_err else ''))
        last_err = None
        while time.time() < deadline:
            try:
                self.req = SharedMsgQueue(info['req'])
                self.resp = SharedMsgQueue(info['resp'])
                break
            except FileNotFoundError as ex:
                last_err = str(ex)
                time.sleep(0.01)
            except Exception as ex:
                last_err = str(ex)
                time.sleep(0.01)
        if not self.req or not self.resp:
            raise FileNotFoundError('shared memory queues not ready for ' + self.path + (f' ({last_err})' if last_err else ''))
        return self

    def close(self):
        if self.req:
            self.req.close(); self.req = None
        if self.resp:
            self.resp.close(); self.resp = None

    def request(self, obj: dict, timeout_sec: float = 5.0) -> dict:
        data = (json.dumps(obj) + '\n').encode('utf-8')
        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            if self.req and self.req.push(data):
                break
            time.sleep(0.01)
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

