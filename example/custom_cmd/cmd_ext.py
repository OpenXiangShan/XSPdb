#coding=utf-8
from XSPdb.cmd.util import info, override

class CmdExample:
    """Example command class
    """

    def do_xexample_cmd(self, arg):
        """Example new command
        """
        info("Example new command executed with argument:" + arg)

    @override
    def do_xcmds(self, arg):
        """Example command with override
        """
        old_fc = getattr(self.do_xcmds, "__old_func__")
        if old_fc:
            old_fc(self, arg)
        info(f"This is en example command with override")
