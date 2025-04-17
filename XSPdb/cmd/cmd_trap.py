#coding=utf-8

from XSPdb.cmd.util import message, warn, GREEN, RESET

class CmdTrap:
    """Trap command class
    """

    def __init__(self):
        assert hasattr(self, "dut"), "this class must be used in XSPdb, canot be used alone"
        self.condition_good_trap = {}
        self.api_init_good_trap()

    def api_init_good_trap(self):
        """Initialize the good trap"""
        checker = self.condition_good_trap.get("checker")
        if checker:
            return
        if hasattr(self.difftest_stat.trap, "get_code_address"):
            checker = self.xsp.ComUseCondCheck(self.dut.xclock)
            target_trap_vali = self.xsp.ComUseDataArray(1)
            target_trap_code = self.xsp.ComUseDataArray(8)
            target_trap_vali.FromBytes(int(0).to_bytes(1, byteorder='little', signed=False))
            target_trap_code.FromBytes(int(0).to_bytes(8, byteorder='little', signed=False))
            source_trap_code = self.xsp.ComUseDataArray(self.difftest_stat.trap.get_code_address(), 8)
            source_trap_vali = self.xsp.ComUseDataArray(self.difftest_stat.trap.get_hasTrap_address(), 1)
            checker.SetCondition("good_trap", source_trap_code.BaseAddr(), target_trap_code.BaseAddr(), self.xsp.ComUseCondCmp_EQ, 8,
                                 source_trap_vali.BaseAddr(), target_trap_vali.BaseAddr(), 1)
            checker.SetValidCmpMode("good_trap", self.xsp.ComUseCondCmp_NE)
        else:
            warn("trap.get_code_address not found, please build the latest difftest-python")
            return
        trap_key = "good_trap"
        self.dut.xclock.RemoveStepRisCbByDesc(trap_key)
        self.dut.xclock.StepRis(checker.GetCb(), checker.CSelf(), trap_key)
        self.condition_good_trap["checker"] = checker

    def api_is_hit_good_trap(self, show_log=False):
        """Check if the good trap is hit

        Returns:
            bool: Whether the good trap is hit
        """
        trap = self.difftest_stat.trap
        if trap.hasTrap != 0 and trap.code == 0:
            if show_log:
                message(f"{GREEN}HIT GOOD TRAP at pc = 0x{trap.pc:x} cycle = 0x{trap.cycleCnt:x} {RESET}")
            return True
        return False

    def api_is_hit_good_loop(self, show_log=False):
        """Check if the good trap is hit

        Args:
            show_log (bool): Whether to show the log
        Returns:
            bool: Whether the good trap is hit
        """
        for i in range(8):
            cmt = self.difftest_stat.get_commit(i)
            if cmt and cmt.valid:
                if cmt.instr == 0x6f:
                    if show_log:
                        message(f"{GREEN}HIT GOOD LOOP at pc = 0x{cmt.pc:x}{RESET}")
                    return True
        return False

    def do_xtrap_info(self, arg):
        """Print trap information

        Args:
            arg (None): No arguments
        """
        trap = self.difftest_stat.trap
        message(f"trap pc: 0x{trap.pc:x}  code: {trap.code}  hasTrap: {trap.hasTrap}  cycle: {trap.cycleCnt} hasWFI: {trap.hasWFI}")
