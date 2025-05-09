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
        if self.api_is_flash_address(address):
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

    def xapi_read_bytes_with_func(self, address, size, read_func):
        """Read memory data

        Args:
            address (int): Memory address
            size (int): Size of data to read
            read_func (callable): raw read funciton
        Return:
            bytes
        """
        read_data = bytearray()
        read_count = size//8 + 1
        start_address = address - address % 8
        start_offset  = address - start_address
        for index in range(read_count):
            padd = start_address + 8*index
            read_data += read_func(padd).to_bytes(8, byteorder='little', signed=False)
        return read_data[start_offset: start_offset + size]

    def xapi_read_bytes_from(self, address, size):
        """Read memory data

        Args:
            address (int): Memory address
            size (int): Size of data to read
        Return:
            bytes
        """
        if not self.mem_inited:
            error(f"memory is not inited")
            return None
        end_address = address + size
        if ((self.api_is_flash_address(address) and not self.api_is_flash_address(end_address))) or \
           (not self.api_is_flash_address(address) and self.api_is_flash_address(end_address)):
            error(f"read address {hex(address)} and {hex(end_address)} not in same range (overlaped with flash and mem)")
            return None
        if self.api_is_flash_address(address):
            def _flash_read(addr):
                return self.df.FlashRead(max(0, addr - self.flash_base))
            return self.xapi_read_bytes_with_func(address, size, _flash_read)
        else:
            return self.xapi_read_bytes_with_func(address, size, self.df.pmem_read)

    def do_xmem_copy(self, arg):
        """copy memory data from one address to another

        Args:
            source (int): Source address
            target (int): Target address
            size (int): Size of data to copy
        """
        if not arg:
            message("usage: xmem_copy <source> <target> <size>")
            return
        args = arg.strip().split()
        if len(args) < 3:
            message("usage: xmem_copy <source> <target> <size>")
            return
        try:
            source = int(args[0], 0)
            target = int(args[1], 0)
            size = int(args[2], 0)
            if size <= 0:
                error("size must be > 0")
                return
            data = self.xapi_read_bytes_from(source, size)
            if data is None:
                error(f"read {size} bytes from address {hex(source)} fail")
                return
            self.api_write_bytes(target, data)
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} or {args[2]} to number fail: {str(e)}")

    def do_xmem_copy_range_to(self, arg):
        """copy memory data from one address to another
        Args:
            source_start (int): Source address start
            source_emd (int): Source address end
            target (int): Target address
        """
        if not arg:
            message("usage: xmem_copy_range_to <source_start> <source_end> <target>")
            return
        args = arg.strip().split()
        if len(args) < 3:
            message("usage: xmem_copy_range_to <source_start> <source_end> <target>")
            return
        try:
            source_start = int(args[0], 0)
            source_end = int(args[1], 0)
            target = int(args[2], 0)
            size = source_end - source_start
            if size <= 0:
                error("size must be > 0")
                return
            data = self.xapi_read_bytes_from(source_start, size)
            if data is None:
                error(f"read {size} bytes from address {hex(source_start)} fail")
                return
            self.api_write_bytes(target, data)
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} or {args[2]} to number fail: {str(e)}")

    def do_xmem_read(self, arg):
        """copy memory data from one address to another

        Args:
            source (int): Source address
            target (int): Target address
            size (int): Size of data to copy
        """
        if not arg:
            error("usage: xmem_read <source> <size>")
            return
        args = arg.strip().split()
        if len(args) < 2:
            error("usage: xmem_read <source> <size>")
            return
        try:
            addr = int(args[0], 0)
            size = int(args[1], 0)
            if size <= 0:
                error("read size need > 0")
                return
            data = self.xapi_read_bytes_from(addr, size)
            if data is None:
                error(f"read None from {hex(addr), hex(addr + size)}")
                return
            message("data bytes(%d): %s"%(len(data), data))
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} to number fail: {str(e)}")

    def do_xmem_read_range(self, arg):
        """Read memory data from one address to another

        Args:
            source_start (int): Source address start
            source_end (int): Source address end
        """
        if not arg:
            message("usage: xmem_read_range <source_start> <source_end>")
            return
        args = arg.strip().split()
        if len(args) < 2:
            message("usage: xmem_read_range <source_start> <source_end>")
            return
        try:
            source_start = int(args[0], 0)
            source_end = int(args[1], 0)
            size = source_end - source_start
            if size <= 0:
                error("size must be > 0")
                return
            data = self.xapi_read_bytes_from(source_start, size)
            if data is None:
                error(f"read {size} bytes from address {hex(source_start)} fail")
                return
            message("data bytes(%d): %s"%(len(data), data))
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} to number fail: {str(e)}")
