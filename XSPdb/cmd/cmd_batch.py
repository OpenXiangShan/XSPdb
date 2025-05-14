#coding=utf-8

import os
import time
from XSPdb.cmd.util import info, error, message, warn, find_executable_in_dirs, YELLOW, RESET


class CmdBatch:
    """Excute batch cmds"""

    def api_exec_batch_cmd(self, cmd_list, callback=None, gap_time=0, target_prefix="", cmd_handler=None):
        cmd_exced = 0
        for i, line in enumerate(cmd_list):
            line = str(line).strip()
            if target_prefix:
                if not line.startswith(target_prefix):
                    continue
                line = line.replace(target_prefix, "").strip()
            if line.startswith("#"):
                continue
            tag = "__sharp_tag_%s__" % str(time.time())
            line = line.replace("\#", tag).split("#")[0].replace(tag, "#").strip()
            if not line:
                continue
            info(f"batch execmd[{i}]: {line}")
            if callable(cmd_handler):
                cmd_handler(line)
            else:
                self.onecmd(line)
            if callable(callback):
                callback(line)
            if gap_time > 0:
                time.sleep(gap_time)
            cmd_exced += 1
        return cmd_exced

    def api_exec_script(self, script_file, callback=None, gap_time=0, target_prefix="", cmd_handler=None):
        if not os.path.exists(script_file):
            error(f"script: {script_file} not find!")
            return -1
        with open(script_file, "r") as f:
            return self.api_exec_batch_cmd(f.readlines(),
                                           callback,
                                           gap_time,
                                           target_prefix,
                                           cmd_handler
                                           )
