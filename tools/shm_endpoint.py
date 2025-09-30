#!/usr/bin/env python3
#coding=utf-8
"""shm_endpoint

Open a pair of shared-memory message queues described by a JSON discovery
file to implement a simple request/response IPC channel.

Discovery file format:
  {"type": "shm", "req": "<req_name>", "resp": "<resp_name>", "size": N}

- `req`: queue for sending requests (one JSON line, UTF-8 encoded).
- `resp`: queue for receiving responses (one JSON line, UTF-8 encoded).

Works with tools.shm_queue.SharedMsgQueue; single-producer/single-consumer.
"""

import json
import os
import time
from typing import Optional

from tools.shm_queue import SharedMsgQueue


class ShmEndpoint:
    """Shared-memory endpoint that connects req/resp queues via a discovery file."""

    def __init__(self, path: str):
        """Initialize the endpoint.

        - path: path to the JSON discovery file written by the peer.
        """
        self.path = os.path.abspath(path)
        self.req: Optional[SharedMsgQueue] = None
        self.resp: Optional[SharedMsgQueue] = None

    def open(self, timeout_sec: float = 5.0):
        """Open the endpoint: poll the discovery file and attach SHM queues.

        - timeout_sec: timeout in seconds.
        Returns self for chaining.
        """
        info = {}
        deadline = time.time() + max(0.0, float(timeout_sec))
        last_err = None
        # Read the discovery file until we see type=="shm"
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

        # Open req/resp queues by name (may need to wait for creator)
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
        """Close opened shared-memory handles (does not unlink)."""
        if self.req:
            self.req.close(); self.req = None
        if self.resp:
            self.resp.close(); self.resp = None

    def request(self, obj: dict, timeout_sec: float = 5.0) -> dict:
        """Send a JSON request and wait for a JSON response.

        - obj: serialized to a single line (with trailing newline) and sent as UTF-8 to the req queue.
        - timeout_sec: max wait for push and for receive (each phase uses this bound independently).

        Returns the parsed dict; on parse error returns {"ok": False, "error": "bad_json"};
        on timeout returns {"ok": False, "error": "timeout"}.
        """
        data = (json.dumps(obj) + '\n').encode('utf-8')
        # Send phase: until push succeeds or times out
        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            if self.req and self.req.push(data):
                break
            time.sleep(0.01)
        # Receive phase: wait for one response frame
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
