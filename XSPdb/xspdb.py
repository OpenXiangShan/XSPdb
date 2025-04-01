#coding=utf-8

import pdb
import bisect
from .ui import enter_simple_tui
from collections import OrderedDict
import os

def message(*a, **k):
    """Print a message"""
    print(*a, **k)

def info(msg):
    """Print information"""
    RESET = "\033[0m"
    GREEN = "\033[32m"
    print(f"{GREEN}[Info] %s{RESET}" % msg)

def debug(msg):
    """Print debug information"""
    print("[Debug] %s" % msg)

def error(msg):
    """Print error information"""
    RESET = "\033[0m"
    RED = "\033[31m"
    print(f"{RED}[Error] %s{RESET}" % msg)

def warn(msg):
    """Print warning information"""
    RESET = "\033[0m"
    YELLOW = "\033[33m"
    print(f"{YELLOW}[Warn] %s{RESET}" % msg)

def build_prefix_tree(signals):
    tree = {}
    for signal in signals:
        current = tree
        parts = signal.split('.')
        for part in parts:
            if part not in current:
                current[part] = {}
            current = current[part]
    return tree


def get_completions(tree, prefix):
    parts = prefix.split('.')
    current_node = tree
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            break
        if part not in current_node:
            return []
        current_node = current_node[part]

    if prefix.endswith('.'):
        return [prefix + v for v in current_node.keys()]
    elif part in current_node:
        return [prefix + "." + v for v in current_node[part].keys()]
    else:
        if "." in prefix:
            prefix = prefix.rsplit(".", 1)[0] + "."
        else:
            prefix = ""
        last_part = parts[-1] if parts else ''
        candidates = list(current_node.keys())
        completions = [prefix + c for c in candidates if c.startswith(last_part)]
        return completions


def find_executable_in_dirs(executable_name, search_dirs="./ready-to-run"):
    """
    Search for an executable file in the specified directories. If not found, search in the system path.

    Args:
        executable_name (str): Name of the executable file
        search_dirs (list): List of specified directories

    Returns:
        str: Path to the executable file, or None if not found
    """
    import shutil
    for directory in search_dirs:
        potential_path = os.path.join(directory, executable_name)
        if os.path.isfile(potential_path) and os.access(potential_path, os.X_OK):
            return os.path.abspath(potential_path)
    return shutil.which(executable_name)


spike_dasm_path = find_executable_in_dirs("spike-dasm", search_dirs=["./ready-to-run"])
if not spike_dasm_path:
    info(f"spike-dasm found, use captone to disassemble, this may cannot work for some instructions")
def dasm_bytes(bytes_data, address):
    """Disassemble binary data

    Args:
        bytes_data (bytes): Binary data
        address (int): Starting address

    Returns:
        list: List of disassembled results
    """
    if spike_dasm_path is not None:
        # iterate over bytes_data in chunks of 2 bytes (c.instr. 16 bits)
        instrs_todecode = []
        full_instr = None
        for i in range(0, len(bytes_data), 2):
            c_instr = bytes_data[i:i+2]
            if full_instr is not None:
                full_instr += c_instr
                instrs_todecode.append((int.from_bytes(full_instr, byteorder='little', signed=False),
                                        full_instr.hex(), i-2 + address))
                full_instr = None
                continue
            # Is full 32 bit instr
            if c_instr[0] & 0x3 == 0x3: # full instr
                full_instr = c_instr
                continue
            # Is compressed 16 instr
            instrs_todecode.append((int.from_bytes(c_instr, byteorder='little', signed=False),
                                    c_instr.hex(), i + address))
        import subprocess
        result_asm = []
        # For every 1000 instrs, call spike-dasm
        for i in range(0, len(instrs_todecode), 1000):
            instrs = instrs_todecode[i:i+1000]
            ins_dm = "\\n".join(["DASM(%016lx)" % i[0] for i in instrs])
            # Call spike-dasm
            bash_cmd = 'echo "%s"|%s' % (ins_dm, spike_dasm_path)
            result = subprocess.run(bash_cmd,
                                    shell=True,
                                    text=True,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            assert result.returncode == 0, f"Error({bash_cmd}): " + str(result.stderr)
            ins_dm = result.stdout.strip().split("\n")
            assert len(ins_dm) == len(instrs), "Error(%s): %d != %d\n%s vs %s" % (bash_cmd, len(ins_dm), len(instrs), ins_dm, instrs)
            for i, v in enumerate(instrs):
                result_asm.append((v[2], v[1], ins_dm[i] if "unknown" not in ins_dm[i] else "unknown.bytes %s" % v[1], ""))
        return result_asm
    try:
        import capstone
    except ImportError:
        raise ImportError("Please install capstone library: pip install capstone")
    md = capstone.Cs(capstone.CS_ARCH_RISCV, capstone.CS_MODE_RISCV32|capstone.CS_MODE_RISCV64|capstone.CS_MODE_RISCVC)
    md.detail = True
    md.skipdata = True
    md.skipdata_setup = (".byte", None, None)
    asm_data = []
    for instr in md.disasm(bytes_data, address):
        asm_data.append((instr.address, instr.bytes.hex(), instr.mnemonic, instr.op_str))
    return asm_data


class XSPdb(pdb.Pdb):
    def __init__(self, dut, df, xsp, default_file=None,
                 mem_base=0x80000000, flash_base=0x10000000, defautl_mem_size=1024*1024*1024):
        """Create a PDB debugger for XiangShan

        Args:
            dut (DUT): DUT exported by picker
            df (difftest): Difftest exported from DUT Python library
            xsp (xspcomm): xspcomm exported from DUT Python library
            default_file (string): Default bin file to load
            mem_base (int): Memory base address
            flash_base (int): Flash base address
        """
        super().__init__()
        self.dut = dut
        self.df = df
        self.xsp = xsp
        self.mem_base = mem_base
        self.flash_base = flash_base
        self.dut_tree = build_prefix_tree(dut.GetInternalSignalList())
        self.prompt = "(XiangShan) "
        self.in_tui = False
        self.interrupt = False
        self.info_cache_asm = {}
        self.info_cache_bsz = 256
        self.info_cached_cmpclist = None
        self.info_watch_list = OrderedDict()
        # Init dut uart echo
        self.dut.InitClock("clock")
        self.c_stderr_echo = xsp.ComUseEcho(dut.difftest_uart_out_valid.CSelf(), dut.difftest_uart_out_ch.CSelf())
        self.dut.StepRis(self.c_stderr_echo.GetCb(), self.c_stderr_echo.CSelf(), "uart_echo")
        # Init difftest
        self.exec_bin_file = default_file
        self.mem_size = defautl_mem_size
        self.mem_inited = False
        if self.exec_bin_file:
            assert os.path.exists(self.exec_bin_file), "file %s not found" % self.exec_bin_file
            info("load: %s" % self.exec_bin_file)
            self.df.InitRam(self.exec_bin_file, self.mem_size)
            self.mem_inited = True
        self.df.InitFlash("")
        self.xspdb_init_bin = "xspdb_flash_init.bin"
        self.flash_bin_file = None
        self.df.difftest_init()
        self.difftest_stat =  df.GetDifftest(0).dut
        self.difftest_flash = df.GetFlash()
        self.condition_instrunct_istep = {}
        self.condition_watch_commit_pc = {}
        self.api_dut_reset()
        self.fregs = ["ft0", "ft1", "ft2",  "ft3", "ft4", "ft5", "ft6",  "ft7",  
                      "fs0", "fs1", "fa0",  "fa1", "fa2", "fa3", "fa4",  "fa5",
                      "fa6", "fa7", "fs2",  "fs3", "fs4", "fs5", "fs6",  "fs7",
                      "fs8", "fs9", "fs10", "fs11","ft8", "ft9", "ft10", "ft11"]
        self.iregs = ["zero", "ra", "sp", "gp",  "tp", "t0", "t1", "t2",
                      "s0",   "s1", "a0", "a1",  "a2", "a3", "a4", "a5",
                      "a6",   "a7", "s2", "s3",  "s4", "s5", "s6", "s7",
                      "s8",   "s9", "s10","s11", "t3", "t4", "t5", "t6"]
        self.mpc_iregs = self.iregs.copy()
        self.mpc_iregs[0] = "mpc"

    # Custom PDB commands and corresponding auto-completion methods
    # do_XXX is the implementation of the command, complete_XXX is the implementation of auto-completion
    def do_xload(self, arg):
        """Load a binary file into memory

        Args:
            arg (string): Path to the binary file
        """
        if not arg:
            message("usage: xload <bin_file>")
            return
        if not os.path.exists(arg):
            error(f"{arg} not found")
            return
        self.api_dut_bin_load(arg)

    def complete_xload(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xflash(self, arg):
        """Load a binary file into Flash

        Args:
            arg (string): Path to the binary file
        """
        if not arg:
            message("usage: xload <bin_file>")
            return
        if not os.path.exists(arg):
            error(f"{arg} not found")
            return
        self.api_dut_flash_load(arg)

    def do_xreset_flash(self, arg):
        """Reset Flash

        Args:
            arg (None): No arguments
        """
        self.api_reset_flash()

    def do_xexport_bin(self, arg):
        """Export Flash + memory data to a file

        Args:
            end_address (int): End address of memory
            file_path (string): Path to the export file
            start_address (int): Start address of memory
        """
        mem_base = self.mem_base
        start_address = mem_base
        params = arg.strip().split()
        if len(params) < 2:
            message("usage: xexport_bin <end_address> <file> [start_address]")
            return
        file_path = params[1]
        if os.path.isdir(file_path):
            file_path = os.path.join(file_path, "XSPdb")
        file_dir = os.path.dirname(file_path)
        if not os.path.exists(file_dir):
            os.makedirs(file_dir)
        try:
            if len(params) > 2:
                start_address = int(params[2], 0)
            end_address = int(params[0], 0)
            if start_address != mem_base:
               if self.api_export_unified_bin(start_address, end_address, file_path+"_all.bin") is not None:
                   return
               warn(f"export unified bin to {file_path}_all.bin fail, try to export flash and ram individually")
            self.api_export_flash(file_path + "_flash.bin")
            self.api_export_ram(end_address, file_path + "_ram.bin")
        except Exception as e:
            error(f"convert {arg} to number fail: {str(e)}")

    def complete_xexport_bin(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xexport_flash(self, arg):
        """Export Flash data to a file

        Args:
            arg (string): Path to the export file
        """
        if not arg:
            message("usage: xexport_flash <file>")
            return
        self.api_export_flash(arg)

    def complete_xexport_flash(self, text, line, begidx, endidx):
        return  self.api_complite_localfile(text)

    def do_xexport_ram(self, arg):
        """Export memory data to a file

        Args:
            addr (int): Export address
            arg (string): Path to the export file
        """
        args = arg.strip().split()
        if len(args) < 2:
            message("usage: xexport_mem <address> <file>")
            return
        try:
            self.api_export_ram(int(args[0], 0), args[1])
        except Exception as e:
            error(f"convert {args[0]} to number fail: {str(e)}")

    def complete_xexport_ram(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def complete_xflash(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xload_script(self, arg):
        """Load an XSPdb script

        Args:
            arg (string): Path to the script file
        """
        if not arg:
            message("usage: xload_script <script_file>")
            return
        if not os.path.exists(arg):
            error(f"{arg} not found")
            return
        error("Please call this function in TUI")

    def complete_xload_script(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xmem_write(self, arg):
        """Write memory data

        Args:
            arg (bytes): Memory address and data
        """
        if not arg:
            message("usage: xmem_write <address> <bytes>")
            return
        args = arg.strip().split()
        if len(args) < 2:
            message("usage: xmem_write <address> <bytes>")
            return
        try:
            address = int(args[0], 0)
            data = eval(args[1])
            if not isinstance(data, bytes):
                error("data must be bytes, eg b'\\x00\\x01...'")
                return
            self.api_write_bytes(address, data)
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} to number/bytes fail: {str(e)}")

    def do_xbytes_to_bin(self, arg):
        """Convert bytes data to a binary file

        Args:
            arg (string): Bytes data
        """
        if not arg:
            message("usage xbytes_to_bin <bytes> <file>")
            return
        args = arg.strip().split()
        if len(args) < 2:
            message("usage xbytes_to_bin <bytes> <file>")
            return
        try:
            data = eval(args[0])
            if not isinstance(data, bytes):
                error("data must be bytes, eg b'\\x00\\x01...'")
                return
            with open(args[1], "wb") as f:
                f.write(data)
        except Exception as e:
            error(f"convert {args[0]} to bytes fail: {str(e)}")

    def complete_xbytes_to_bin(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xnop_insert(self, arg):
        """Insert NOP instructions in a specified address range

        Args:
            start (int): Start address
            end (int): End address
        """
        if not arg:
            message("usage: xnop_at <start> <end>")
            return
        args = arg.strip().split()
        if len(args) < 2:
            message("usage: xnop_at <start> <end>")
            return
        try:
            start = int(args[0], 0)
            end = int(args[1], 0)
            assert start < end, "start address must less than end address"
            assert start % 2 == 0, "start address must be aligned to 2"
            assert end % 2 == 0, "end address must be aligned to 2"
            noop_data = bytearray()
            for i in range((end - start) // 2):
                noop_data += b'\x01\x00' # nop
            self.api_write_bytes(start, noop_data)
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} to number fail: {str(e)}")

    def do_xclear_dasm_cache(self, arg):
        """Clear disassembly cache

        Args:
            arg (None): No arguments
        """
        self.info_cache_asm.clear()

    def do_xprint(self, arg):
        """Print the value and width of an internal signal

        Args:
            arg (string): Name of the internal signal
        """
        sig = self.dut.GetInternalSignal(arg)
        if sig:
            message(f"value: {hex(sig.value)}  width: {sig.W()}")

    def complete_xprint(self, text, line, begidx, endidx):
        cmp = get_completions(self.dut_tree, text)
        return cmp

    def do_xset(self, arg):
        """Set the value of an internal signal

        Args:
            name (string): Name of the internal signal
            value (int): Value of the internal signal
        """
        args = arg.strip().split()
        if len(args) < 2:
            error("need args format: name value")
            return
        pin_name, pin_value = args[0], args[1]
        try:
            pin_value = int(pin_value)
        except Exception as e:
            error(f"convert {args[1]} to number fail: {str(e)}")
            return
        pin = self.dut.GetInternalSignal(pin_name)
        if pin:
            pin.AsImmWrite()
            pin.value = pin_value

    def do_xstep(self, arg):
        """Step through the circuit

        Args:
            cycle (int): Number of cycles
            steps (int): Number of cycles per run; after each run, check for interrupt signals
        """
        try:
            steps = 200
            cycle = arg.strip().split()
            if len(cycle) > 1:
                steps = int(cycle[1])
            cycle = int(cycle[0])
            self.api_step_dut(cycle, steps)
        except Exception as e:
            error(e)

    def do_xistep(self, arg):
        """Step through instructions

        Args:
            instr_count (int): Number of instructions
        """
        arg = arg.strip()
        instr_count = 1
        try:
            instr_count = 1 if not arg else int(arg)
        except Exception as e:
            error(f"convert {arg} to number fail: {str(e)}")
            return

        if not self.condition_instrunct_istep:
            checker = self.xsp.ComUseCondCheck(self.dut.xclock)
            self.condition_instrunct_istep["checker"] = checker
            pc_old_list = [self.xsp.ComUseDataArray(8) for i in range(8)]
            pc_lst_list = [self.xsp.ComUseDataArray(self.difftest_stat.get_commit(i).get_pc_address(), 8) for i in range(8)]
            # sync pc and add checker
            for i, opc in enumerate(pc_old_list):
                lpc = pc_lst_list[i]
                opc.SyncFrom(lpc.BaseAddr(), 8)
                checker.SetCondition("stepi_check_pc_%d" % i, lpc.BaseAddr(), opc.BaseAddr(), self.xsp.ComUseCondCmp_NE, 8)
            self.condition_instrunct_istep["pc_old_list"] = pc_old_list
            self.condition_instrunct_istep["pc_lst_list"] = pc_lst_list
            def _update_old_pc():
                for i, opc in enumerate(pc_old_list):
                    lpc = pc_lst_list[i]
                    opc.SyncFrom(lpc.BaseAddr(), 8)
            self.condition_instrunct_istep["pc_sync_list"] = _update_old_pc
        cb_key = "stepi_check"
        checker = self.condition_instrunct_istep["checker"]
        self.dut.xclock.StepRis(checker.GetCb(), checker.CSelf(), cb_key)
        update_pc_func = self.condition_instrunct_istep["pc_sync_list"]
        update_pc_func()
        for i in range(instr_count):
            self.api_step_dut(10000)
            update_pc_func()
            if self.api_is_hit_good_trap():
                break
        # remove stepi_check
        self.dut.xclock.RemoveStepRisCbByDesc(cb_key)
        assert cb_key not in self.dut.xclock.ListSteRisCbDesc()

    def do_xwatch_commit_pc(self, arg):
        """Watch commit PC

        Args:
            arg (address): PC address
        """
        if arg.strip() == "update":
            checker = self.condition_watch_commit_pc.get("checker")
            if checker:
                checker.Reset()
            return
        try:
            address = int(arg, 0)
        except Exception as e:
            error(f"convert {arg} to number fail: {str(e)}")
            return

        if not self.condition_watch_commit_pc.get("checker"):
            checker = self.xsp.ComUseCondCheck(self.dut.xclock)
            cmtpccmp = self.xsp.ComUseRangeCheck(6, 8);
            self.condition_watch_commit_pc["checker"] = checker
            self.condition_watch_commit_pc["cmtpcmp"] = cmtpccmp

        checker = self.condition_watch_commit_pc["checker"]
        if "watch_pc_0x%x_0"%address not in checker.ListCondition():
            cmtpccmp = self.condition_watch_commit_pc["cmtpcmp"]
            target_pc = self.xsp.ComUseDataArray(8)
            target_pc.FromBytes(address.to_bytes(8, byteorder='little', signed=False))
            pc_lst_list = [self.xsp.ComUseDataArray(self.difftest_stat.get_commit(i).get_pc_address(), 8) for i in range(8)]
            for i, lpc in enumerate(pc_lst_list):
                checker.SetCondition("watch_pc_0x%x_%d" % (address, i), lpc.BaseAddr(), target_pc.BaseAddr(), self.xsp.ComUseCondCmp_GE, 8,
                                     0, 0, 1, cmtpccmp.GetArrayCmp(), cmtpccmp.CSelf())
            checker.SetMaxCbs(1)
            self.condition_watch_commit_pc["0x%x"%address] = {"pc_lst_list": pc_lst_list, "target_pc": target_pc}
        else:
            error(f"watch_commit_pc 0x{address:x} already exists")
            return
        cb_key = "watch_commit_pc"
        self.dut.xclock.RemoveStepRisCbByDesc(cb_key)
        self.dut.xclock.StepRis(checker.GetCb(), checker.CSelf(), cb_key)
        message(f"watch commit pc: 0x{address:x}")

    def do_xunwatch_commit_pc(self, arg):
        """Unwatch commit PC

        Args:
            arg (address): PC address
        """
        try:
            address = int(arg, 0)
        except Exception as e:
            error(f"convert {arg} to number fail: {str(e)}")
            return
        checker = self.condition_watch_commit_pc.get("checker")
        if not checker:
            error("watch_commit_pc.checker not found")
            return
        if "watch_pc_0x%x_0"%address not in checker.ListCondition():
            error(f"watch_commit_pc 0x{address:x} not found")
            return
        key = "0x%x"%address
        if key in self.condition_watch_commit_pc:
            self.condition_watch_commit_pc[key]
        for i in range(8):
            checker.RemoveCondition("watch_pc_0x%x_%d" % (address, i))
        if len(checker.ListCondition()) < 1:
            self.dut.xclock.RemoveStepRisCbByDesc("watch_commit_pc")
            assert "watch_commit_pc" not in self.dut.xclock.ListSteRisCbDesc()
            self.condition_watch_commit_pc.clear()
            message("No commit pc to wathc, remove checker")

    def complete_xset(self, text, line, begidx, endidx):
        cmp = get_completions(self.dut_tree, text)
        return cmp

    def do_xwatch(self, arg):
        """Add a watch variable

        Args:
            arg (string): Variable name
        """
        key = arg.strip().split()
        if not key:
            for k, v in self.info_watch_list.items():
                message(f"{k}({v.W()}): 0x{v.value}")
            return
        arb = key[-1]
        sig = self.dut.GetInternalSignal(key[0])
        if sig:
            self.info_watch_list[arb] = sig

    def do_xunwatch(self, arg):
        """Remove a watch variable

        Args:
            arg (string): Variable name
        """
        key = arg.strip()
        if key in self.info_watch_list:
            del self.info_watch_list[key]
        else:
            error(f"watch {key} not found")

    def complete_xwatch(self, text, line, begidx, endidx):
        cmp = get_completions(self.dut_tree, text)
        return cmp

    def complete_xunwatch(self, text, line, begidx, endidx):
        return [k for k in self.info_watch_list.keys() if k.startswith(text)]

    def do_xpc(self, a):
        """Print the current Commit PCs

        Args:
            a (None): No arguments
        """
        for p in self.get_commit_pc_list():
            error("PC: 0x%x%s" % (p[0], "" if p[1] else "*"))

    def do_xexpdiffstate(self, var):
        """Set a variable to difftest_stat

        Args:
            var (string): Variable name
        """
        self.curframe.f_locals[var] = self.difftest_stat

    def do_xexportself(self, var):
        """Set a variable to XSPdb self

        Args:
            var (string): Variable name
        """
        self.curframe.f_locals[var] = self

    def do_xreset(self, arg):
        """Reset DUT

        Args:
            arg (None): No arguments
        """
        self.api_dut_reset()

    def do_xlist_xclock_cb(self, arg):
        """List all xclock callbacks

        Args:
            arg (None): No arguments
        """
        message("Ris Cbs:")
        for cb in self.dut.xclock.ListSteRisCbDesc():
            message("\t", cb)
        message("Fal Cbs:")
        for cb in self.dut.xclock.ListSteFalCbDesc():
            message("\t", cb)

    def do_xui(self, arg):
        """Enter the Text UI interface

        Args:
            arg (None): No arguments
        """
        if self.in_tui:
            error("Already in TUI")
            return
        self.in_tui = True
        enter_simple_tui(self)
        self.in_tui = False
        self.on_update_tstep = None
        self.interrupt = False
        info("XUI Exited.")

    def do_xdasm(self, arg):
        """Disassemble memory data

        Args:
            arg (string): Memory address and length
        """
        if not arg:
            error("dasm <address> [length]")
            return
        args = arg.strip().split()
        length = 10
        if len(args) < 2:
            args.append(str(length))
        try:
            address = int(args[0], 0)
            length = int(args[1])
            for l in self.api_mem_data_to_asm(address, length):
                message("0x%x: %s\t%s\t%s" % (l[0], l[1], l[2], l[3]))
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} to number fail: {str(e)}")

    def do_xdasmflash(self, arg):
        """Disassemble Flash data

        Args:
            arg (string): Flash address and length
        """
        if not arg:
            error("dasmflash <address> [length]")
            return
        args = arg.strip().split()
        length = 10
        if len(args) < 2:
            args.append(str(length))
        try:
            address = int(args[0], 0)
            length = int(args[1])
            for l in self.api_flash_data_to_asm(address, length):
                message("0x%x: %s\t%s\t%s" % (l[0], l[1], l[2], l[3]))
        except Exception as e:
            error(f"convert {args[0]} or {args[1]} to number fail: {str(e)}")

    def do_xdasmbytes(self, arg):
        """Disassemble binary data

        Args:
            arg (string): Binary data
        """
        if not arg:
            error("dasmbytes <bytes> [address]")
            return
        try:
            params = arg.strip().split()
            address = 0
            if len(params) > 1:
                address = int(params[1], 0)
            data_bytes = params[0].strip()
            if not data_bytes.startswith("b'"):
                new_data_bytes = "b'"
                for i in range(0, len(data_bytes), 2):
                    new_data_bytes += "\\x%s" % params[0][i:i+2]
                data_bytes = new_data_bytes + "'"
            for i in self.api_dasm_from_bytes(eval(data_bytes), address):
                message("0x%x: %s\t%s\t%s" % (i[0], i[1], i[2], i[3]))
        except Exception as e:
            error(f"convert {arg} to bytes fail: {str(e)}")

    def do_xdasmnumber(self, arg):
        """Disassemble a number

        Args:
            arg (string): Number data
        """
        if not arg:
            error("dasmbytes <number> [address]")
            return
        try:
            params = arg.strip().split()
            address = 0
            if len(params) > 1:
                address = int(params[1], 0)
            for i in self.api_dasm_from_bytes(int(params[0], 0).to_bytes(4, byteorder="little", signed=False), address):
                message("0x%x: %s\t%s\t%s" % (i[0], i[1], i[2], i[3]))
        except Exception as e:
            error(f"convert {arg} to bytes fail: {str(e)}")

    def do_xbytes2number(self, arg):
        """Convert bytes to an integer

        Args:
            arg (string): Bytes data
        """
        if not arg:
            error("bytes2number <bytes>")
            return
        try:
            data_bytes = arg.strip()
            if not data_bytes.startswith("b'"):
                new_data_bytes = "b'"
                for i in range(0, len(data_bytes), 2):
                    new_data_bytes += "\\x%s" % data_bytes[i:i+2]
                data_bytes = new_data_bytes + "'"
            message(f'{int.from_bytes(eval(data_bytes), byteorder="little", signed=False):x}')
        except Exception as e:
            error(f"convert {arg} to bytes fail: {str(e)}")

    def do_xnumber2bytes(self, arg):
        """Convert an integer to bytes

        Args:
            arg (string): Integer data
        """
        if not arg:
            error("number2bytes <number>")
            return
        try:
            data = int(arg, 0)
            message(f'b"{data.to_bytes(4, byteorder="little", signed=False).hex()}"')
        except Exception as e:
            error(f"convert {arg} to bytes fail: {str(e)}")

    def do_xparse_instr_file(self, arg):
        """Parse uint64 strings

        Args:
            arg (file): File to parse
        """
        if not arg:
            message("usage: xparse_instr_file <instr_file>")
            return
        if not os.path.exists(arg):
            error("file %s not found" % arg)
            return
        hex_str = ''.join([f'\\x{byte:02x}' for byte in self.api_convert_uint64_bytes(arg)])
        message(hex_str)

    def complete_xparse_instr_file(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xload_instr_file(self, arg):
        """Load uint64 strings into memory

        Args:
            arg (file): File to load
        """
        params = arg.strip().split()
        if not len(params) == 2:
            error("load_instr_file <address> <instr_file>")
            return
        if not os.path.exists(params[1]):
            error("file %s not found" % params[1])
            return
        try:
            address = int(params[0], 0)
            self.api_write_bytes(address, self.api_convert_uint64_bytes(params[1]))
            self.mem_inited = True
        except Exception as e:
            error(f"convert {params[0]} to number fail: {str(e)}")

    def complete_xload_instr_file(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xparse_reg_file(self, arg):
        """Parse a register file

        Args:
            arg (file): Register file
        """
        if not arg:
            error("parse_reg_file <reg_file>")
            return
        if not os.path.exists(arg):
            error("file %s not found" % arg)
            return
        iregs, fregs = self.api_convert_reg_file(arg)
        message("iregs:\n", str(iregs))        
        message("fregs:\n", str(fregs))

    def complete_xparse_reg_file(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xload_reg_file(self, arg):
        """Load a register file

        Args:
            arg (file): Register file
        """
        if not arg:
            error("load_reg_file <reg_file>")
            return
        if not os.path.exists(arg):
            error("file %s not found" % arg)
            return
        iregs, fregs = self.api_convert_reg_file(arg)
        self.api_set_flash_int_regs(iregs)
        self.api_set_flash_float_regs(fregs)

    def complete_xload_reg_file(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xset_iregs(self, arg):
        """Set Flash internal registers (Integer)

        Args:
            arg (string): Register values
        """
        if not arg:
            message("usage: xset_iregs <regs>, format: {\"reg_name\": value} or [value1, value2, ...]")
            return
        try:
            self.api_set_flash_int_regs(eval(arg))
        except Exception as e:
            error(f"set_iregs fail: {str(e)}")

    def do_xset_mpc(self, arg):
        """Set the jump address (by mpc) after Flash initialization, default is 0x80000000

        Args:
            arg (string): Register name and value
        """
        args = arg.strip().split()
        if len(args) < 2:
            message("usage: xset_mpc <value>")
            return
        try:
            self.api_set_flash_int_regs({"mpc": int(args[1], 0)})
        except Exception as e:
            error(f"set_mpc fail: {str(e)}")

    def do_xget_mpc(self, arg):
        """Get the jump address after Flash initialization, default is 0x80000000

        Args:
            arg (None): No arguments
        """
        mpc = self.api_get_flash_init_iregs()
        for r in mpc:
            if r[0] == "mpc":
                message(f"mpc: {hex(r[1])}", end=" ")
        message("")


    def do_xset_fregs(self, arg):
        """Set Flash floating-point registers (general)

        Args:
            arg (string): Register values
        """
        if not arg:
            message("usage: xset_fregs <regs>, format: {\"reg_name\": value} or [value1, value2, ...]")
            return
        try:
            self.api_set_flash_float_regs(eval(arg))
        except Exception as e:
            error(f"set_fregs fail: {str(e)}")

    def do_xset_ireg(self, arg):
        """Set a single Flash internal register (Integer)

        Args:
            arg (string): Register name and value
        """
        if not arg:
            message("usage: xset_ireg <reg_name> <value>")
            return
        args = arg.strip().split()
        if len(args) < 2:
            message("usage: xset_ireg <reg_name> <value>")
            return
        try:
            self.api_set_flash_int_regs({args[0]: int(args[1], 0)})
        except Exception as e:
            error(f"set_ireg fail: {str(e)}")

    def do_xset_freg(self, arg):
        """Set a Flash floating-point register

        Args:
            arg (string): Register name and value
        """
        if not arg:
            message("usage: xset_freg <reg_name> <value>")
            return
        args = arg.strip().split()
        if len(args) < 2:
            message("usage: xset_freg <reg_name> <value>")
            return
        try:
            self.api_set_flash_float_regs({args[0]: int(args[1], 0)})
        except Exception as e:
            error(f"set_freg fail: {str(e)}")

    def complete_xset_ireg(self, text, line, begidx, endidx):
        return [k for k in ["mpc", "ra", "sp", "gp",  "tp", "t0", "t1", "t2",
                            "s0",   "s1", "a0", "a1",  "a2", "a3", "a4", "a5",
                            "a6",   "a7", "s2", "s3",  "s4", "s5", "s6", "s7",
                            "s8",   "s9", "s10","s11", "t3", "t4", "t5", "t6"] if k.startswith(text)]

    def complete_xset_freg(self, text, line, begidx, endidx):
        return [k for k in self.fregs if k.startswith(text)]

    def do_xlist_flash_iregs(self, arg):
        """List Flash internal registers

        Args:
            arg (None): No arguments
        """
        for r in self.api_get_flash_init_iregs():
            message(f"{r[0]}: {hex(r[1])}", end=" ")
        message("")

    def do_xlist_flash_fregs(self, arg):
        """List Flash floating-point registers

        Args:
            arg (None): No arguments
        """
        for r in self.api_get_flash_init_fregs():
            message(f"{r[0]}: {hex(r[1])}", end=" ")
        message("")
    
    def do_xlist_freg_map(self, arg):
        """List floating-point register mappings

        Args:
            arg (None): No arguments
        """
        for i, r in enumerate(self.fregs):
            message(f"x{i}: {r}", end=" ")
        message("")

    # Custom APIs for PDB commands and TUI interface
    # All APIs start with get_ or api_
    def get_commit_pc_list(self):
        """Get the list of all commit PCs

        Returns:
        list((pc, valid)): List of PCs
        """
        index = 0
        pclist=[]
        while True:
            cmt = self.difftest_stat.get_commit(index)
            if cmt:
                pclist.append((cmt.pc, cmt.valid))
                index += 1
            else:
                break
        return pclist

    def api_complite_localfile(self, text):
        """Auto-complete local files

        Args:
            text (string): File name

        Returns:
            list(string): Completion list
        """
        text = text.strip()
        if not text:
            return [f for f in os.listdir('.') if f != '.' or f != '..']
        path = ""
        fname = text
        if "/" in text:
            path, fname = text.rsplit("/", 1)
        return [os.path.join(path, f) for f in os.listdir(path if path else ".") if f.startswith(fname)]

    def api_step_dut(self, cycle, batch_cycle=200):
        """Step through the circuit

        Args:
            cycle (int): Number of cycles
            batch_cycle (int): Number of cycles per run; after each run, check for interrupt signals
        """
        assert not self.dut.xclock.IsDisable(), "clock is disable"
        self.interrupt = False
        batch, offset = cycle//batch_cycle, cycle % batch_cycle
        c_count = self.dut.xclock.clk
        for i in range(batch):
            if self.interrupt:
                break
            self.dut.Step(batch_cycle)
            if self.dut.xclock.IsDisable():
                info("Find break point, break (step %d cycles)" % (self.dut.xclock.clk - c_count))
                break
            fc = getattr(self, "on_update_tstep", None)
            if fc:
                fc()
            if self.api_is_hit_good_trap(show_log=True):
                break
        if not self.interrupt and not self.dut.xclock.IsDisable():
            self.dut.Step(offset)
        self.dut.xclock.Enable()
        self.interrupt = False

    def api_dut_reset(self):
        """Reset the DUT"""
        for i in range(8):
            self.difftest_stat.get_commit(i).pc = 0x0
        self.dut.reset.AsImmWrite()
        self.dut.reset.value = 1
        self.dut.reset.AsRiseWrite()
        self.dut.reset.value = 1
        self.dut.Step(100)
        self.dut.reset.value = 0
        info("reset complete")

    def api_get_flash_init_iregs(self):
        """Get Flash internal registers

        Returns:
            list(int): Register values
        """
        if not self.api_check_if_xspdb_init_bin_loaded():
            return []
        base_offset = 8
        reg_index = self.mpc_iregs
        regs = []
        for i in range(len(reg_index)):
            regs.append((reg_index[i], self.df.FlashRead(base_offset + i*8)))
        return regs

    def api_get_flash_init_fregs(self):
        """Get Flash floating-point registers

        Returns:
            list(int): Register values
        """
        if not self.api_check_if_xspdb_init_bin_loaded():
            return []
        base_offset = 8 + 32*8
        regs = []
        for i in range(len(self.fregs)):
            regs.append((self.fregs[i], self.df.FlashRead(base_offset + i*8)))
        return regs

    def api_set_flash_float_regs(self, regs):
        """Set Flash floating-point registers

        Args:
            regs (list(float), dict): Register values
        """
        if not self.api_check_if_xspdb_init_bin_loaded():
            return
        base_offset = 8 + 32*8
        reg_map = {k: v for v, k in enumerate(self.fregs)}
        return self.api_set_flash_data_values(base_offset, self.fregs, reg_map, regs, "fregs")

    def api_set_flash_int_regs(self, regs):
        """Set Flash internal registers

        Args:
            regs (list(int), dict): Register values
        """
        if not self.api_check_if_xspdb_init_bin_loaded():
            return
        base_offset = 8
        reg_index = self.mpc_iregs
        reg_map = {k: v for v, k in enumerate(reg_index)}
        return self.api_set_flash_data_values(base_offset, reg_index, reg_map, regs, "iregs")

    def api_check_if_xspdb_init_bin_loaded(self):
        """Check if xspdb_flash_init.bin is loaded

        Returns:
            bool: Whether it is loaded
        """
        if not self.flash_bin_file or self.xspdb_init_bin not in self.flash_bin_file:
            error(f"{self.xspdb_init_bin} not loaded")
            return False
        return True

    def api_set_flash_data_values(self, base_offset, reg_index, reg_map, kdata, kname):
        """Set Flash register values

        Args:
            base_offset (int): Base address of the registers
            reg_index (list(string)): List of register names
            reg_map (dict): Mapping of register names
            kdata (list(int), dict): Register values
            kname (string): Register name
        """
        if isinstance(kdata, list):
            for i, r in enumerate(kdata):
                if isinstance(r, str):
                    r = r.strip()
                    if r == "-":
                        continue
                    r = int(r, 0)
                assert isinstance(r, int), f"{kname}[{i}] not number"
                self.df.FlashWrite(base_offset + i*8, r)
        elif isinstance(kdata, dict):
            if "*" in kdata:
                v = kdata["*"]
                for key in reg_index:
                    if key in kdata:
                        v = kdata[key]
                    self.df.FlashWrite(base_offset + reg_map[key]*8, v)
            else:
                for key, v in kdata.items():
                    if key in reg_map:
                        self.df.FlashWrite(base_offset + reg_map[key]*8, v)
                    else:
                        warn(f"{kname}[{key}] not found")
        else:
            assert False, "regs type error"

        # delete asm data in cache
        cache_index = self.flash_base - self.flash_base % self.info_cache_bsz
        if cache_index in self.info_cache_asm:
            del self.info_cache_asm[cache_index]

    def api_convert_uint64_bytes(self, file_name):
        """Parse uint64 strings

        Args:
            file_name (file): File to parse
        """
        ret = bytearray()
        with open(file_name, "r") as f:
            for l in f:
                l = l.strip()
                if not l:
                    continue
                for v in l.split():
                    if not v.startswith("0x"):
                        v = "0x" + v
                    ccount = len(v) - 2
                    assert ccount % 2 == 0, f"invalid hex string: {v}"
                    ret += int(v, 0).to_bytes(ccount//2, byteorder='little', signed=False)
        return ret

    def api_convert_reg_file(self, file_name):
        """Parse a register file

        Args:
            file_name (file): Register file
        """
        assert os.path.exists(file_name), "file %s not found" % file_name
        ret_iregs = {}
        ret_fregs = {}
        raw_iregs = {"x%d"%i : self.iregs[i] for i in range(32)}
        raw_fregs = {"f%d"%i : self.fregs[i] for i in range(32)}
        with open(file_name, "r") as f:
            for i, l in enumerate(f.readlines()):
                try:
                    l = l.strip()
                    if not l:
                        continue
                    key, value = l.split(":")
                    key = key.strip().lower()
                    value = int(value.strip(), 0)
                    if key in raw_iregs:
                        key = raw_iregs[key]
                    if key in self.iregs:
                        assert key not in ret_iregs, f"{key} already exists"
                        ret_iregs[key] = value
                    if key in raw_fregs:
                        key = raw_fregs[key]
                    if key in self.fregs:
                        assert key not in ret_fregs, f"{key} already exists"
                        ret_fregs[key] = value
                except Exception as e:
                    assert False, f"line {i+1} parse fail: {str(e)}"
        return ret_iregs, ret_fregs

    def api_dut_bin_load(self, bin_file):
        """Load a bin file into memory

        Args:
            bin_file (string): Path to the bin file
        """
        assert os.path.exists(bin_file), "file %s not found" % bin_file
        if self.mem_inited:
            self.df.overwrite_ram(bin_file, self.mem_size)
        else:
            self.df.InitRam(bin_file, self.mem_size)
            self.mem_inited = True
        self.exec_bin_file = bin_file

    def api_export_flash(self, bin_file):
        """Export Flash data

        Args:
            bin_file (string): Path to the export file
        """
        if not self.api_check_if_xspdb_init_bin_loaded():
            return
        # search mret
        mret = 0x30200073
        last_data = 0
        bin_data = bytearray()
        for i in range(1024*10):
            data = self.df.FlashRead(i*8)
            bin_data += data.to_bytes(8, byteorder='little', signed=False)
            if last_data >> 32 == mret or last_data & 0xffffffff == mret:
                break
            last_data = data
        with open(bin_file, "wb") as f:
            f.write(bin_data)
        info(f"export {len(bin_data)} bytes to flash file: {bin_file}")

    def api_export_ram(self, end_address, bin_file):
        """Export memory data

        Args:
            end_address (int): End address of memory
            bin_file (string): Path to the export file
        """
        if not self.mem_inited:
            error("mem not loaded")
            return
        end_index = 8 + end_address - end_address % 8
        with open(bin_file, "wb") as f:
            for index in range(self.mem_base, end_index, 8):
                f.write(self.df.pmem_read(index).to_bytes(8, byteorder='little', signed=False))
        info(f"export {end_index - self.mem_base} bytes to ram file: {bin_file}")

    def api_is_hit_good_trap(self, show_log=False):
        """Check if the good trap is hit

        Args:
            show_log (bool): Whether to show the log
        Returns:
            bool: Whether the good trap is hit
        """
        RESET = "\033[0m"
        GREEN = "\033[32m"
        for i in range(8):
            cmt = self.difftest_stat.get_commit(i)
            if cmt and cmt.valid:
                if cmt.instr == 0x6f:
                    if show_log:
                        message(f"{GREEN}HIT GOOD TRAP at pc = 0x{cmt.pc:x}{RESET}")
                    return True
        return False

    def api_export_unified_bin(self, ram_start, ram_end, bin_file):
        """Export a unified bin file

        Args:
            ram_start (int): Start address of memory
            ram_end (int): End address of memory
            bin_file (string): Path to the export file
        """
        if not self.mem_inited:
            error("mem not loaded")
            return False
        if not self.api_check_if_xspdb_init_bin_loaded():
            return False
        # read flash data
        mret = 0x30200073
        last_data = 0
        last_indx = 0
        bin_data = bytearray()
        for i in range(1024*10):
            data = self.df.FlashRead(i*8)
            bin_data += data.to_bytes(8, byteorder='little', signed=False)
            last_indx = i + 1
            if last_data >> 32 == mret or last_data & 0xffffffff == mret:
                break
            last_data = data
        # check conflict
        # mem base
        mem_base = self.mem_base
        ram_start = ram_start - ram_start % 8
        sta_index = (ram_start - mem_base)//8
        if sta_index < last_indx:
            error(f"conflict with flash data, ram_start: 0x{ram_start:x}, flash_data_end: 0x{last_indx*8+ mem_base:x}, please check")
            return None
        ram_end = ram_end - ram_end % 8
        end_index = (ram_end - mem_base)//8 + 1
        # read ram data
        with open(bin_file, "wb") as f:
            f.write(bin_data)
            for index in range(last_indx, end_index):
                f.write(self.df.pmem_read(index*8 + mem_base).to_bytes(8, byteorder='little', signed=False))
        info(f"export {8*(end_index - last_indx) + len(bin_data)} bytes to unified bin file: {bin_file}")
        return True

    def api_dut_flash_load(self, flash_file):
        """Load a bin file into Flash

        Args:
            flash_file (string): Path to the bin file
        """
        assert os.path.exists(flash_file)
        self.df.flash_finish()
        self.df.InitFlash(flash_file)
        self.flash_bin_file = flash_file

    def api_dut_reset_flash(self):
        self.df.flash_finish()
        self.df.InitFlash("")
        self.flash_bin_file = None

    def api_all_data_to_asm(self, address, length):
        """Convert memory data to assembly instructions

        Args:
            address (int): Target memory address
            length (int): Target memory length

        Returns:
            list((address, hex, mnemonic, str)): Disassembly results
        """
        if address < self.mem_base:
            return self.api_flash_data_to_asm(address, length)
        else:
            return self.api_mem_data_to_asm(address, length)

    def api_flash_data_to_asm(self, address, length):
        """Convert Flash data to assembly instructions

        Args:
            address (int): Target Flash address
            length (int): Target Flash length

        Returns:
            list((address, hex, mnemonic, str)): Disassembly results
        """
        def _flash_read(addr):
            return self.df.FlashRead(max(0, addr - self.flash_base))
        return self.api_read_data_as_asm(address, length, _flash_read)

    def api_mem_data_to_asm(self, address, length):
        """Convert memory data to assembly instructions

        Args:
            address (int): Target memory address
            length (int): Target memory length

        Returns:
            list((address, hex, mnemonic, str)): Disassembly results
        """
        return self.api_read_data_as_asm(address, length, self.df.pmem_read)

    def api_dasm_from_bytes(self, bytes, start_address=0):
        """Convert binary data to assembly instructions

        Args:
            bytes (bytes): Binary data
            start_address (int): Starting address

        Returns:
            list((address, hex, mnemonic, str)): Disassembly results
        """
        return dasm_bytes(bytes, start_address)

    def api_write_bytes_with_rw(self, address, bytes, dword_read, dword_write):
        """Write memory data

        Args:
            address (int): Target memory address
            bytes (bytes): Data to write
            dword_read (function): Function to read uint64
            dword_write (function): Function to write uint64
        """
        if len(bytes) < 1:
            return
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

    def api_write_bytes(self, address, bytes):
        """Write memory data

        Args:
            address (int): Target memory address
            bytes (bytes): Data to write
        """
        if address < self.mem_base:
            self.api_write_bytes_with_rw(address - self.flash_base,
                                                bytes, self.df.FlashRead, self.df.FlashWrite)
        else:
            self.api_write_bytes_with_rw(address,
                                                bytes, self.df.pmem_read, self.df.pmem_write)
        # Delete asm data in cache
        pos_str = address - address % self.info_cache_bsz
        pos_end = address + len(bytes)
        pos_end = (pos_end - pos_end % self.info_cache_bsz) + self.info_cache_bsz
        for cache_index in range(pos_str, pos_end, self.info_cache_bsz):
            if cache_index in self.info_cache_asm:
                del self.info_cache_asm[cache_index]

    def api_read_data_as_asm(self, address, length, read_func):
        """Convert memory data to assembly instructions

        Args:
            address (int): Target memory address
            length (int): Target memory length
            read_func (function): Function to read uint64

        Returns:
            list((address, hex, mnemonic, str)): Disassembly results
        """
        dasm_list = []
        try:
            sta_address = address - address % 2                      # Starting memory address must be 2-byte aligned
            end_address = sta_address + (2 + length//2 + length % 2) # Ending memory address must be 2-byte aligned; read at least 2 bytes
            assert sta_address >=0 , "address need >=0 and not miss align"
            assert length >=0, "length need >=0 "

            pmem_sta_address = sta_address - sta_address % 8         # Physical memory reads 8 bytes at a time; must be 8-byte aligned
            pmem_end_address = end_address - end_address % 8         # Physical memory reads 8 bytes at a time; must be 8-byte aligned
            count = 1 + pmem_end_address - pmem_sta_address
            buffer = bytearray()
            for index in range(count):
                padd = pmem_sta_address + 8*index
                buffer += read_func(padd).to_bytes(8, byteorder='little', signed=False)
            # Calculate offset
            offset = sta_address - pmem_sta_address
            for instr in dasm_bytes(buffer[offset:], sta_address):
                    dasm_list.append(instr)
        except Exception as e:
            import traceback
            error(f"disasm fail: {str(e)} {traceback.print_exc()}")
        return dasm_list

    def get_asm_info(self, size):
        """Get the current memory disassembly

        Args:
            size (int, int): Width, height = size

        Returns:
            list[string]: Disassembly list
        """
        # size: w, h
        _, h = size
        base_addr = self.mem_base
        pc_list = self.get_commit_pc_list()
        valid_pc_list = [x[0] for x in pc_list if x[1]]
        pc_last = base_addr

        if self.info_cached_cmpclist:
            new_pc = [x[0] for x, y in zip(pc_list, self.info_cached_cmpclist) if x[0] != y[0] and x[1] != 0]
            if new_pc:
                pc_last = max(new_pc)

        if pc_last == base_addr and valid_pc_list:
            pc_last = max(valid_pc_list)

        self.info_cached_cmpclist = pc_list.copy()
        # Check the cache first; if not found, generate it
        cache_index = pc_last - pc_last % self.info_cache_bsz
        asm_data = self.info_cache_asm.get(cache_index,
                                           self.api_all_data_to_asm(cache_index, self.info_cache_bsz))
        self.info_cache_asm[cache_index] = asm_data

        # Need to check boundaries; if near a boundary, fetch adjacent cache blocks
        cache_index_ext = base_addr
        if pc_last % self.info_cache_bsz < h:
            cache_index_ext = cache_index - self.info_cache_bsz
        elif self.info_cache_bsz - pc_last % self.info_cache_bsz < h:
            cache_index_ext = cache_index + self.info_cache_bsz

        # Boundary is valid
        if cache_index_ext > base_addr:
            asm_data_ext = self.info_cache_asm.get(cache_index_ext,
                                                   self.api_all_data_to_asm(cache_index_ext, self.info_cache_bsz))
            self.info_cache_asm[cache_index_ext] = asm_data_ext
            if cache_index_ext < cache_index:
                asm_data = asm_data_ext + asm_data
            else:
                asm_data = asm_data + asm_data_ext

        # Quickly locate the position of pc_last
        address_list = [x[0] for x in asm_data]
        pc_last_index = bisect.bisect_left(address_list, pc_last)
        start_line = max(0, pc_last_index - h//2)
        asm_lines = []
        for l in  asm_data[start_line:start_line + h]:
            find_pc = l[0] in valid_pc_list
            line = "%s|0x%x: %s  %s  %s" % (">" if find_pc else " ", l[0], l[1], l[2], l[3])
            if find_pc and l[0] == pc_last:
                line = ("norm_red", line)
            asm_lines.append(line)
        return asm_lines

    def get_abs_info(self, size):
        """Get the current status summary information, such as general-purpose registers

        Args:
            size (int, int): Width, height = size

        Returns:
            list[string]: Status list
        """
        # size: w, h
        # FIXME
        abs_list = []
        # Int regs
        abs_list += ["IntReg:"]
        def ireg_map():
            if not hasattr(self.xsp, "GetFromU64Array"):
                return [('error_red',"<Error! xspcomm.GetFromU64Array not find, please update your xspcomm lib>>")]
            return " ".join(["%3s: 0x%x" % (self.iregs[i],
                                            self.xsp.GetFromU64Array(self.difftest_stat.regs_int.value, i))
                             for i in range(32)])
        abs_list += [ireg_map()]
        # Float regs
        abs_list += ["\nFloatReg:"]
        def freg_map():
            return " ".join(["%3s: 0x%x" % (self.fregs[i],
                                            self.xsp.GetFromU64Array(self.difftest_stat.regs_fp.value, i))
                             for i in range(32)])
        abs_list += [freg_map()]
        # Commit PCs
        abs_list += ["\nCommit PC:"]
        abs_list += [" ".join(["0x%x%s" % (x[0], "" if x[1] else "*") for x in self.get_commit_pc_list()])]
        abs_list += ["max commit: 0x%x" % max([x[0] for x in self.get_commit_pc_list()])]
        # Add other content to display here

        # csr
        abs_list += ["\nCSR:"]
        abs_list += ["mstatus: 0x%x  " % self.difftest_stat.csr.mstatus + 
                     "mcause: 0x%x  " % self.difftest_stat.csr.mcause +
                     "mepc: 0x%x  " % self.difftest_stat.csr.mepc +
                     "mtval: 0x%x  " % self.difftest_stat.csr.mtval +
                     "mtvec: 0x%x  " % self.difftest_stat.csr.mtvec +
                     "privilegeMode: %d  " % self.difftest_stat.csr.privilegeMode +
                     "mie: 0x%x  " % self.difftest_stat.csr.mie +
                     "mip: 0x%x  " % self.difftest_stat.csr.mip + 
                     "satp: 0x%x  " % self.difftest_stat.csr.satp +
                     "sstatus: 0x%x  " % self.difftest_stat.csr.sstatus +
                     "scause: 0x%x  " % self.difftest_stat.csr.scause +
                     "sepc: 0x%x  " % self.difftest_stat.csr.sepc +
                     "stval: 0x%x  " % self.difftest_stat.csr.stval +
                     "stvec: 0x%x  " % self.difftest_stat.csr.stvec
                     ]
        # fcsr
        abs_list += ["\nFCSR: 0x%x" % self.difftest_stat.fcsr.fcsr]

        # Bin file
        abs_list += ["\nLoaded Bin:"]
        abs_list += [f"file: {self.exec_bin_file}"]

        # Watch List
        if self.info_watch_list:
            abs_list += ["\nWatch List:"]
            for k , v in self.info_watch_list.items():
                abs_list += [f"{k}({v.W()}): 0x{v.value}"]

        if self.flash_bin_file:
            abs_list += ["\nFlash Bin:"]
            abs_list += [f"file: {self.flash_bin_file}"]

        # Watched commit pc
        commit_pc_cheker = self.condition_watch_commit_pc.get("checker")
        if commit_pc_cheker:
            stat_txt = "(Disabled)" if commit_pc_cheker.IsDisable() else ""
            abs_list += [f"\nWatched Commit PC{stat_txt}:"]
            watch_pc = OrderedDict()
            for k, v in commit_pc_cheker.ListCondition().items():
                pc = k.split("_")[2]
                if pc in watch_pc:
                    watch_pc[pc].append(v)
                else:
                    watch_pc[pc] = [v]
            for pc, v in watch_pc.items():
                checked = sum(v) > 0
                if checked:
                    abs_list += [("error_red", f"{pc}: {checked}")]
                else:
                    abs_list += [f"{pc}: {checked}"]

        if self.api_is_hit_good_trap():
            abs_list += ["\nProgram:"]
            abs_list += [("success_green", "HIT GOOD TRAP")]

        # TBD
        # abs_list += [("error_red", "\nFIXME:\nMore Data to be done\n")]
        return abs_list
