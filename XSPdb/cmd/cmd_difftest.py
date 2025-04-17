#coding=utf-8

from XSPdb.cmd.util import message, error, warn

class CmdDiffTest:

    def __init__(self):
        assert hasattr(self, "difftest_stat"), "difftest_stat not found"
        self.condition_watch_commit_pc = {}    
        self.condition_instrunct_istep = {}

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
        # remove stepi_check
        self.dut.xclock.RemoveStepRisCbByDesc(cb_key)
        assert cb_key not in self.dut.xclock.ListSteRisCbDesc()
