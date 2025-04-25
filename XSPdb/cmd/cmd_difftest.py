#coding=utf-8

import os
from XSPdb.cmd.util import message, error, warn, info, GREEN, RESET

class CmdDiffTest:

    def __init__(self):
        assert hasattr(self, "difftest_stat"), "difftest_stat not found"
        self.condition_watch_commit_pc = {}    
        self.condition_instrunct_istep = {}
        self.difftest_ref_so = self.xsp.CString()
        self.difftest_ref_is_inited = False
        self.difftest_diff_checker = {}
        self.difftest_diff_is_run = False

    def api_load_ref_so(self, so_path):
        """Load the difftest reference shared object

        Args:
            so_path (string): Path to the shared object
        """
        if not os.path.exists(so_path):
            error(f"file {so_path} not found")
            return False
        self.difftest_ref_so.Set(so_path)
        self.df.SetProxyRefSo(self.difftest_ref_so.CharAddress())
        info(f"load difftest ref so: {so_path} complete")
        return True

    def api_get_ref_so_path(self):
        """Get the path of the difftest reference shared object

        Returns:
            string: Path to the shared object
        """
        return self.difftest_ref_so.Get()

    def api_init_ref(self):
        """Initialize the difftest reference"""
        if self.difftest_ref_is_inited:
            error("difftest reference already inited")
            return False
        if self.difftest_ref_so.Get() == "":
            error("difftest reference so not loaded")
            return False
        if not self.mem_inited:
            error("mem not loaded, please load bin file to mem first")
            return False
        self.df.init_device()
        self.df.GoldenMemInit()
        self.df.init_nemuproxy(0)
        self.difftest_ref_is_inited = True
        return True

    def api_set_difftest_diff(self, turn_on):
        """Initialize the difftest diff"""
        if not self.api_init_ref():
            return False
        checker = self.difftest_diff_checker.get("checker")
        if not checker:
            checker = self.xsp.ComUseCondCheck(self.dut.xclock)
            tmp_dat = self.xsp.ComUseDataArray(4)
            checker.SetCondition("diff_test_do_diff_check", tmp_dat.BaseAddr(), tmp_dat.BaseAddr(),
                                 self.xsp.ComUseCondCmp_NE, 4, 0, 0, 0,
                                 checker.AsPtrXFunc(self.df.GetFuncAddressOfDifftestStepAndCheck()),
                                 0)
            self.difftest_diff_checker["checker"] = checker
        key = "diff_test_do_diff_check"
        self.dut.xclock.RemoveStepRisCbByDesc(key)
        self.difftest_diff_is_run = False
        if turn_on:
            self.dut.xclock.StepRis(checker.GetCb(), checker.CSelf(), key)
            self.difftest_diff_is_run = True
            info("turn on difftest diff")
        else:
            info("turn off difftest diff")
        return True

    def api_is_difftest_diff_exit(self, show_log=False):
        """Check if the difftest diff has exited

        Returns:
            bool: True if exited, False otherwise
        """
        if not self.difftest_diff_is_run:
            return False
        stat = self.df.GetDifftestStat()
        if stat == -1:
            return False
        if show_log:
            message(f"{GREEN}Difftest run exit with code: {stat} {RESET}")
        return True

    def api_is_difftest_diff_run(self):
        """Check if the difftest diff is running"""
        return self.difftest_diff_is_run

    def do_xload_difftest_ref_so(self, arg):
        """Load the difftest reference shared object

        Args:
            arg (string): Path to the shared object
        """
        if not arg.strip():
            error("difftest ref so path not found\n usage: xload_difftest_ref_so <path>")
            return
        if not self.api_load_ref_so(arg):
            error(f"load difftest ref so {arg} failed")
            return

    def complete_xload_difftest_ref_so(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xdifftest_turn_on(self, arg):
        """Turn on the difftest diff

        Args:
            arg (string): Turn on or off
        """
        if arg.strip() == "on":
            self.api_set_difftest_diff(True)
        elif arg.strip() == "off":
            self.api_set_difftest_diff(False)
        else:
            error("usage: xdifftest_turn_on <on|off>")

    def complete_xdifftest_turn_on(self, text, line, begidx, endidx):
        return [x for x in ["on", "off"] if x.startswith(text)] if text else ["on", "off"]

    def api_commit_pc_list(self):
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

    def do_xpc(self, a):
        """Print the current Commit PCs and instructions

        Args:
            a (None): No arguments
        """
        for i in range(8):
            cmt = self.difftest_stat.get_commit(i)
            message(f"PC[{i}]: 0x{cmt.pc:x}    Instr: 0x{cmt.instr:x}")

    def do_xexpdiffstate(self, var):
        """Set a variable to difftest_stat

        Args:
            var (string): Variable name
        """
        self.curframe.f_locals[var] = self.difftest_stat

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
            v = self.api_step_dut(10000)
            update_pc_func()
            if self.api_is_hit_good_trap():
                break
            elif self.api_is_hit_good_loop():
                break
            if v == 10000:
                warn("step %d cycles complete, but no instruction commit find" % v)
            if self.interrupt:
                break
            if self.dut.xclock.IsDisable():
                break
        # remove stepi_check
        self.dut.xclock.RemoveStepRisCbByDesc(cb_key)
        assert cb_key not in self.dut.xclock.ListSteRisCbDesc()
