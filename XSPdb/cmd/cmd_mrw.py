#coding = utf-8


from XSPdb.cmd.util import error, info, message, warn

class CmdMRW:
    """Command class for MRW (Memory Read/Write) operations."""


    def api_write_bytes_with_rw(self, address, bytes, dword_read, dword_write):
        """Write memory data

        Args:
            address (int): Target memory address
            bytes (bytes): Data to write
            dword_read (function): Function to read uint64
            dword_write (function): Function to write uint64
        """
        if len(bytes) < 1:
            error("write data length < 1")
            return False
        if not self.mem_inited:
            error("mem not inited, please load a bin file")
            return False
        start_offset = address % 8
        head = dword_read(address - start_offset).to_bytes(8,
                              byteorder='little', signed=False)[:start_offset]
        end_address = address + len(bytes)
        end_offset = end_address % 8
        tail = dword_read(end_address - end_offset).to_bytes(8,
                              byteorder='little', signed=False)[end_offset:]
        data_to_write = head + bytes + tail
        assert len(data_to_write)%8 == 0
        base_address = address - start_offset
        for i in range(len(data_to_write)//8):
            dword_write(base_address + i*8,  int.from_bytes(data_to_write[i*8:i*8+8],
                                                            byteorder='little', signed=False))
        info(f"write {len(data_to_write)} bytes to address: 0x{base_address:x} ({len(bytes)} bytes)")
        return True

    def api_write_bytes(self, address, bytes):
        """Write memory data

        Args:
            address (int): Target memory address
            bytes (bytes): Data to write
        """
        if address < self.mem_base:
            real_address = address - self.flash_base
            if real_address < 0:
                warn(f"write address {hex(address)} is not in Flash range, less than {hex(self.flash_base)} ignored")
                return False
            if real_address > 0x7FFFFFFF:
                warn(f"write address {hex(address)} is not in Flash range, bigger than {hex(self.flash_base+ 0x7FFFFFFF)} (max uint32 0x7FFFFFFF) ignored")
                return False
            ret = self.api_write_bytes_with_rw(real_address,
                                                bytes, self.df.FlashRead, self.df.FlashWrite)
        else:
            ret = self.api_write_bytes_with_rw(address,
                                                bytes, self.df.pmem_read, self.df.pmem_write)
        if ret:
            # Delete asm data in cache
            pos_str = address - address % self.info_cache_bsz
            pos_end = address + len(bytes)
            pos_end = (pos_end - pos_end % self.info_cache_bsz) + self.info_cache_bsz
            for cache_index in range(pos_str, pos_end, self.info_cache_bsz):
                if cache_index in self.info_cache_asm:
                    del self.info_cache_asm[cache_index]
        return ret

    def do_xmem_write(self, arg):
        """Write memory data

        Args:
            arg (bytes/number): Memory address and data
        """
        if not arg:
            message("usage: xmem_write <address> <bytes/number>")
            return
        args = arg.strip().split()
        if len(args) < 2:
            message("usage: xmem_write <address> <bytes/number>")
            return
        try:
            address = int(args[0], 0)
            if arg[1].startswith("b"):
                data = eval(args[1])
            else:
                byte_count = max(1, len(args[1].replace("0x",""))//2)
                data = int(args[1], 0).to_bytes(byte_count, byteorder='little', signed=False)
            if not isinstance(data, bytes):
                error("data must be bytes, eg b'\\x00\\x01...' or hex number")
                return
            self.api_write_bytes(address, data)
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} to number/bytes fail: {str(e)}")
