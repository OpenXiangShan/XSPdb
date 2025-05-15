#coding=utf-8

import os
import time
from XSPdb.cmd.util import info, error, message, warn, find_executable_in_dirs, YELLOW, RESET


class CmdBatch:
    """Excute batch cmds"""

    def __init__(self):
        self.ignore_cmds_in_batch = [
            "xload_script",
            "xreplay_log",
            "xui",
        ]

    def cmd_in_ignore_list(self, cmd):
        """Check if the command is in the ignore list"""
        for ignore_cmd in self.ignore_cmds_in_batch:
            if cmd.startswith(ignore_cmd):
                return True
        return False

    def api_clear_batch_ignore_list(self):
        """Clear the ignore list"""
        self.ignore_cmds_in_batch = []
        info("ignore cmd list cleared")
        return True

    def api_add_batch_ignore_list(self, cmd):
        """Add a command to the ignore list"""
        if cmd in self.ignore_cmds_in_batch:
            info(f"cmd: {cmd} already in ignore list")
            return False
        self.ignore_cmds_in_batch.append(cmd)
        info(f"add cmd: {cmd} to ignore list")
        return True

    def api_del_batch_ignore_list(self, cmd):
        """Delete a command from the ignore list"""
        if cmd not in self.ignore_cmds_in_batch:
            info(f"cmd: {cmd} not in ignore list")
            return False
        self.ignore_cmds_in_batch.remove(cmd)
        info(f"delete cmd: {cmd} from ignore list")
        return True

    def api_exec_batch_cmd(self, cmd_list, callback=None, gap_time=0, target_prefix="", target_subfix="", cmd_handler=None):
        cmd_exced = 0
        for i, line in enumerate(cmd_list):
            line = str(line).strip()
            if target_prefix:
                if not line.startswith(target_prefix):
                    continue
                line = line[len(target_prefix):].strip()
            if target_subfix:
                if not line.endswith(target_subfix):
                    continue
                line = line[:-len(target_subfix)].strip()
            if line.startswith("#"):
                continue
            tag = "__sharp_tag_%s__" % str(time.time())
            line = line.replace("\#", tag).split("#")[0].replace(tag, "#").strip()
            if not line:
                continue
            if self.cmd_in_ignore_list(line):
                warn(f"ignore cmd: {line}")
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

    def api_exec_script(self, script_file, callback=None, gap_time=0, target_prefix="", target_subfix="", cmd_handler=None):
        if not os.path.exists(script_file):
            error(f"script: {script_file} not find!")
            return -1
        with open(script_file, "r") as f:
            return self.api_exec_batch_cmd(f.readlines(),
                                           callback,
                                           gap_time,
                                           target_prefix,
                                           target_subfix,
                                           cmd_handler
                                           )

    def do_xload_script(self, arg):
        """Load an XSPdb script

        Args:
            script (string): Path to the script file
            delay_time (float): time delay between each cmd
        """
        usage = "usage: xload_script <script_file> [delay_time]"
        if not arg:
            message(usage)
            return
        args = arg.split()
        path = args[0]
        delay = 0.2
        if len(args) > 1:
            try:
                delay = float(args[1])
            except Exception as e:
                error("convert dalay fail: %s, from args: %s\n%s" % (e, arg, usage))
        self.api_exec_script(path, gap_time=delay)

    def complete_xload_script(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xreplay_log(self, arg):
        """Replay a log file

        Args:
            log_file (string): Path to the log file
            delay_time (float): time delay between each cmd
        """
        usage = "usage: xreplay_log <log_file> [delay_time]"
        if not arg:
            message(usage)
            return
        args = arg.split()
        path = args[0]
        delay = 0.2
        if len(args) > 1:
            try:
                delay = float(args[1])
            except Exception as e:
                error("convert dalay fail: %s, from args: %s\n%s" % (e, arg, usage))
        self.api_exec_script(path, gap_time=delay,
                             target_prefix=self.log_cmd_prefix,
                             target_subfix=self.log_cmd_suffix,
                             )

    def complete_xreplay_log(self, text, line, begidx, endidx):
        return self.api_complite_localfile(text)

    def do_xbatch_ignore_cmd(self, arg):
        """Add a command to the ignore list

        Args:
            cmd (string): Command to ignore
        """
        if not arg.strip():
            message("usage: xbatch_ignore_cmd <cmd>")
            return
        self.api_add_batch_ignore_list(arg.strip())

    def complete_xbatch_ignore_cmd(self, text, line, begidx, endidx):
        """Complete the command for xbatch_ignore_cmd"""
        cmd_list = []
        for cmd in dir(self):
            if not cmd.startswith("do_x"):
                continue
            cmd = cmd[3:]
            if cmd in self.ignore_cmds_in_batch:
                continue
            cmd_list.append(cmd)
        if not text:
            return cmd_list
        else:
            return [cmd for cmd in cmd_list if cmd.startswith(text)]

    def do_xbatch_clear_ignore_cmd(self, arg):
        """Clear the ignore list"""
        self.api_clear_batch_ignore_list()

    def do_xbatch_unignore_cmd(self, arg):
        """Delete a command from the ignore list

        Args:
            cmd (string): Command to unignore
        """
        if not arg.strip():
            message("usage: xbatch_unignore_cmd <cmd>")
            return
        self.api_del_batch_ignore_list(arg.strip())

    def complete_xbatch_unignore_cmd(self, text, line, begidx, endidx):
        """Complete the command for xbatch_unignore_cmd"""
        if not text:
            return self.ignore_cmds_in_batch
        else:
            return [cmd for cmd in self.ignore_cmds_in_batch if cmd.startswith(text)]

    def do_xbatch_list_ignore_cmd(self, arg):
        """List the ignore list"""
        if not self.ignore_cmds_in_batch:
            message("ignore cmd list is empty")
            return
        message(f"{YELLOW}{' '.join(self.ignore_cmds_in_batch)}{RESET}")
