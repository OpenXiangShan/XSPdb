#coding=utf-8

import pdb
from .ui import enter_simple_tui
from collections import OrderedDict
import os
import pkgutil

from XSPdb.cmd.util import message, info, error, build_prefix_tree, register_commands

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
        self.register_map = OrderedDict()
        self.load_cmds()

    def load_cmds(self):
        import XSPdb.cmd
        cmd_count = 0
        for _, modname, _ in pkgutil.iter_modules(XSPdb.cmd.__path__):
            if not modname.startswith("cmd_"):
                continue
            # load Cmd* Class from cmd.cmd_*
            mod = __import__(f"XSPdb.cmd.{modname}", fromlist=[modname])
            cmd_count += register_commands(mod, self.__class__, self)
        info(f"Loaded {cmd_count} commands from XSPdb.cmd")

    def do_xexportself(self, var):
        """Set a variable to XSPdb self

        Args:
            var (string): Variable name
        """
        self.curframe.f_locals[var] = self

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

    def do_xcmds(self, arg):
        """Print all xcmds

        Args:
            arg (None): No arguments
        """
        cmd_count = 0
        max_cmd_len = 0
        cmds = []
        for cmd in dir(self):
            if not cmd.startswith("do_x"):
                continue
            cmd_name = cmd[4:]
            max_cmd_len = max(max_cmd_len, len(cmd_name))
            cmd_desc = getattr(self, cmd).__doc__.split("\n")[0]
            cmds.append((cmd, cmd_name, cmd_desc))
            cmd_count += 1
        cmds.sort(key=lambda x: x[0])
        for c in cmds:
            message(("%-"+str(max_cmd_len+2)+"s: %s (from %s)") % (c[1], c[2], self.register_map.get(c[0], self.__class__.__name__)))
        info(f"Total {cmd_count} xcommands")

    def do_xapis(self, arg):
        """Print all APIs

        Args:
            arg (None): No arguments
        """
        api_count = 0
        max_api_len = 0
        apis = []
        for api in dir(self):
            if not api.startswith("api_"):
                continue
            api_name = api
            max_api_len = max(max_api_len, len(api_name))
            api_desc = getattr(self, api).__doc__.split("\n")[0]
            apis.append((api, api_name, api_desc))
            api_count += 1
        apis.sort(key=lambda x: x[0])
        for c in apis:
            message(("%-"+str(max_api_len+2)+"s: %s (from %s)") % (c[1], c[2], self.register_map.get(c[0], self.__class__.__name__)))
        info(f"Total {api_count} APIs")
