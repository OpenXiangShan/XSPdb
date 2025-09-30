#!/usr/bin/env python3
#coding=utf-8

import struct
from multiprocessing import shared_memory

_HDR_SIZE = 16  # 8 bytes head, 8 bytes tail
_WRAP_MARK = 0xFFFFFFFF  # special marker to indicate wrap to 0


class SharedMsgQueue:
    """Single-producer/single-consumer ring buffer over POSIX shared memory.

    Header layout:
      [0:8]   head (uint64, bytes written)
      [8:16]  tail (uint64, bytes read)
      [16:]   data region of capacity = size - 16

    Frames: [len:u32][payload...] using big-endian length. If a frame cannot fit
    contiguously, a wrap marker (len=0xFFFFFFFF) is placed, and the frame is written at 0.
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
                try:
                    self.shm = shared_memory.SharedMemory(name=name, create=True, size=size)
                except FileExistsError:
                    self.shm = shared_memory.SharedMemory(name=name, create=False)
            except Exception:
                try:
                    self.shm = shared_memory.SharedMemory(name=name, create=False)
                except Exception as ex:
                    raise ex
        else:
            self.shm = shared_memory.SharedMemory(name=name, create=False)
        self.buf = self.shm.buf
        if create:
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
        contiguous = self.capacity - head_off
        if contiguous < frame_len:
            if contiguous >= 4:
                struct.pack_into('>I', self.buf, _HDR_SIZE + head_off, _WRAP_MARK)
                head = head + 4
                head_off = (head % self.capacity)
            head = head + (self.capacity - head_off)
            head_off = 0
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
        if self.capacity - tail_off >= 4:
            (length,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + tail_off)
            if length == _WRAP_MARK:
                tail += 4
                tail_off = (tail % self.capacity)
                if tail == head:
                    self._write_tail(tail)
                    return None
                (length,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + tail_off)
        else:
            tail += (self.capacity - tail_off)
            tail_off = 0
            if tail == head:
                self._write_tail(tail)
                return None
            (length,) = struct.unpack_from('>I', self.buf, _HDR_SIZE + tail_off)

        if length > self.capacity - 4:
            self._write_tail(head)
            return None
        need = 4 + length
        contiguous = self.capacity - tail_off
        if contiguous < need:
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
        import asyncio
        await asyncio.sleep(interval_sec)
        return self.pop()

