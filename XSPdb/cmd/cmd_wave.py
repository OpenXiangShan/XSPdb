#coding=utf-8

import os
from XSPdb.cmd.util import message, info, error

class CmdTrap:
    """Trap command class
    """

    def __init__(self):
        assert hasattr(self, "dut"), "this class must be used in XSPdb, canot be used alone"

    def api_init_waveform(self):
        """Initialize the waveform (close waveform at beginning)
        """
        self.dut.RefreshComb()
        self.dut.CloseWaveform()
        self.waveform_on = False

    def api_is_waveform_on(self):
        """Check if waveform recording is on

        Returns:
            bool: Whether the waveform is on
        """
        return self.waveform_on

    def do_xwave_on(self, arg):
        """Start waveform recording

        Args:
           name (str): Waveform file path [optional]
        """
        wave_file = arg.strip()
        if wave_file:
            if not os.path.isabs(wave_file):
                error(f"waveform file[{arg}] name must be a ligal path")
                message("usage: xwave_on [waveform file path]")
                return
            self.dut.CloseWaveform(wave_file)
        self.dut.OpenWaveform()
        self.waveform_on = True
        info("waveform on")

    def do_xwave_off(self, arg):
        """Close waveform recording

        Args:
            arg (None): No arguments
        """
        self.dut.CloseWaveform()
        self.waveform_on = False
        info("waveform off")

    def do_xwave_flush(self, arg):
        """Flush waveform recording

        Args:
            arg (None): No arguments
        """
        if not self.waveform_on:
            error("waveform is not on")
            return
        self.dut.FlushWaveform()
        info("waveform flush complete")
