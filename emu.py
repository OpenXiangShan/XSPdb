#!/usr/bin/env python3

import os
import sys
import argparse
import signal

# Search XSPdb and XSPython
def import_or_search(*module_names):
    import importlib
    from importlib.util import spec_from_file_location, module_from_spec
    imported_modules = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
            imported_modules.append(module)
            continue
        except ImportError:
            pass
        # If the module is not found, search in the current directory and the directory of this script
        fpath = os.path.dirname(os.path.abspath(__file__))
        search_dirs = [os.getcwd(),
                       fpath,
                       os.path.join(fpath, ".."),
                       ]
        module_file = None
        for dir_path in search_dirs:
            possible_paths = [os.path.join(dir_path, f"{module_name}.py"),
                              os.path.join(dir_path, module_name, "__init__.py")]
            for path in possible_paths:
                if os.path.exists(path):
                    module_file = path
                    break
            if module_file:
                break
        if module_file:
            spec = spec_from_file_location(module_name, module_file)
            module = module_from_spec(spec)
            spec.loader.exec_module(module)
            sys.modules[module_name] = module
            imported_modules.append(module)
        else:
            raise ImportError(f"Cannot find {module_name} (SerchPath: {search_dirs})")
    return imported_modules if len(imported_modules) > 1 else imported_modules[0]


try:
    XSPdb, XSPython = import_or_search("XSPdb", "XSPython")
except ImportError as e:
    assert False, e


def args_parser():
    address = lambda s: int(s, 0)
    parser = argparse.ArgumentParser(description="XSPdb Emulation Tool")
    parser.add_argument("-v", "--version", action="version", version=f"XSPdb {XSPdb.__version__}")
    parser.add_argument("-C", "--max-cycles", type=int, default=0xFFFFFFFFFFFFFFFF, help="execute at most NUM cycles")
    parser.add_argument("-i", "--image", type=str, default="", help="run with this image file")
    parser.add_argument("-b", "--log-begin", type=int, default=0, help="display log from NUM th cycle")
    parser.add_argument("-e", "--log-end", type=int, default=0xFFFFFFFFFFFFFFFF, help="stop display log at NUM th cycle")
    parser.add_argument("-t", "--interact-at", type=int, default=-1, help="interact at NUM th cycle")
    parser.add_argument("-l", "--log-level", type=int, default=0, help="set the log level")
    parser.add_argument("-s", "--script", type=str, default="", help="script file to run")
    parser.add_argument("-r", "--replay", type=str, default="", help="logs to replay")
    parser.add_argument("--debug-level", type=str, default="", choices=["debug", "info", "warn", "erro"], help="set debug level")
    parser.add_argument("--pc-commits", type=int, default=0, help="run until NUM th commit, -1 means no limit")
    parser.add_argument("--sim-args", type=lambda s: s.split(','), default=[], help="additional args for simulation")
    parser.add_argument("--dump-wave", action="store_true", default=False, help="dump waveform when log is enabled")
    parser.add_argument("-F", "--flash", type=str, default="", help="the flash bin file for simulation")
    parser.add_argument("--wave-path", type=str, default="", help="dump waveform to a specified PATH")
    parser.add_argument("--ram-size", type=str, default="", help="simulation memory size, for example 8GB / 128MB")
    parser.add_argument("--diff", type=str, default="", help="set the path of REF for differential testing")
    parser.add_argument("--cmds", type=str, default="", help="xspdb cmds to exceute")
    parser.add_argument("--mem-base-address", type=address, default=0x80000000, help="set the base address of memory")
    parser.add_argument("--flash-base-address", type=address, default=0x10000000, help="set the base address of flash")
    parser.add_argument("--diff-first-inst_address", type=address, default=-1, help="set the first instruction address for difftest testing")
    parser.add_argument("--trace-pc-symbol-block-change", action="store_true", default=False, help="trace pc symbol block change on")
    return parser.parse_args()


def parse_mem_size(size_str):
    size_str = size_str.strip().upper()
    if size_str.endswith("GB"):
        return int(size_str[:-2]) * 1024 * 1024 * 1024
    elif size_str.endswith("MB"):
        return int(size_str[:-2]) * 1024 * 1024
    elif size_str.endswith("KB"):
        return int(size_str[:-2]) * 1024
    else:
        raise ValueError(f"Invalid memory size format: {size_str}")


def run_script(xspdb, script_path):
    xspdb.api_exec_script(script_path, gap_time=0.1)
    xspdb.api_append_init_cmd("xnop")
    xspdb.set_trace()
    return False


def run_replay(xspdb, replay_path):
    xspdb.api_exec_script(replay_path, gap_time=0.1,
                          target_prefix=xspdb.log_cmd_prefix,
                          target_subfix=xspdb.log_cmd_suffix,
                          )
    xspdb.api_append_init_cmd("xnop")
    xspdb.set_trace()
    return True


def run_commits(xspdb, commits):
    if commits < 0:
        commits = 0xFFFFFFFFFFFFFF
    delta = commits
    while delta > 0 and not xspdb.api_dut_is_step_exit():
        delta = delta - xspdb.api_xistep(delta)
        check_is_need_trace(xspdb)
    xspdb.message(f"Execute {commits - delta} commits completed")


def create_xspdb():
    args = args_parser()
    sim_kwargs = {}
    sim_args = args.sim_args
    if args.wave_path:
        sim_kwargs["waveform_filename"] = args.wave_path
    dut = XSPython.DUTSimTop(*sim_args, **sim_kwargs)
    xpd_kwagrs = {
        "mem_base":         args.mem_base_address,
        "flash_base":       args.flash_base_address,
    }
    if args.image:
        xpd_kwagrs["default_file"] = args.image
    if args.diff_first_inst_address != -1:
        xpd_kwagrs["finstr_addr"] = args.diff_first_inst_address
    if args.ram_size:
        xpd_kwagrs["default_mem_size"] = parse_mem_size(args.ram_size)
    if args.debug_level:
        import logging
        XSPdb.set_xspdb_log_level({"debug":logging.DEBUG,
                                   "info": logging.INFO,
                                   "warn": logging.WARN,
                                   "erro": logging.ERROR}[args.debug_level])
    # New XSPdb
    from XSPython import difftest as df, xsp
    xspdb = XSPdb.XSPdb(dut, df, xsp, **xpd_kwagrs)
    assert args.log_begin <= args.log_end, "arg --log-begin must be less than log_end"
    assert args.log_begin >= 0, "arg --log-begin must be greater than 0"
    assert args.log_end <= args.max_cycles, "arg --log-end must be less than max_cycles"
    return args, xspdb


def check_is_need_trace(xspdb):
    if getattr(xspdb, "__xspdb_need_fast_trace__", False) is True:
        setattr(xspdb, "__xspdb_need_fast_trace__", False)
        XSPdb.info("Force set trace")
        xspdb.set_trace()
    if xspdb.interrupt is True:
        if getattr(xspdb, "__xspdb_set_traced__", None) is None:
            setattr(xspdb, "__xspdb_set_traced__", True)
            XSPdb.info("Find interrupt, set trace")
            xspdb.set_trace()
    return False


def main(args, xspdb):
    def emu_step(delta):
        c = xspdb.api_step_dut(delta)
        check_is_need_trace(xspdb)
        return c
    if args.log_begin != args.log_end:
        if args.dump_wave:
            if args.log_begin == 0:
                XSPdb.info(f"Waweform on at HW cycle = Zero")
                xspdb.api_waveform_on()
            else:
                def cb_on_log_begin(s, checker, k, clk, sig, target):
                    XSPdb.info(f"Waveform on at HW cycle = {target}")
                    xspdb.api_waveform_on()
                    s.interrupt = False
                XSPdb.info(f"Set waveform on callback at HW cycle = {args.log_begin}")
                xspdb.api_xbreak("SimTop_top.SimTop.timer", "eq", args.log_begin, callback=cb_on_log_begin, callback_once=True)
            def cb_on_log_end(s, checker, k, clk, sig, target):
                XSPdb.info(f"Waveform off at HW cycle = {target}")
                xspdb.api_waveform_off()
                s.interrupt = False
            XSPdb.info(f"Set waveform off callback at HW cycle = {args.log_end}")
            xspdb.api_xbreak("SimTop_top.SimTop.timer", "eq", args.log_end, callback=cb_on_log_end, callback_once=True)
    if args.interact_at > 0:
        def cb_on_interact(s, checker, k, clk, sig, target):
            XSPdb.info(f"interact at HW cycle = {target}")
            setattr(xspdb, "__xspdb_need_fast_trace__", True)
        XSPdb.info(f"Set interact callback at HW cycle = {args.interact_at}")
        xspdb.api_xbreak("SimTop_top.SimTop.timer", "eq", args.interact_at, callback=cb_on_interact, callback_once=True)
    if args.flash:
        xspdb.api_dut_flash_load(args.flash)
    if args.trace_pc_symbol_block_change:
        xspdb.api_turn_on_pc_symbol_block_change(True)
    if args.cmds:
        for c in args.cmds.replace("\\n", "\n").split("\n"):
            xspdb.api_append_init_cmd(c)
    if args.script:
        if run_script(xspdb, args.script):
            return
    if args.replay:
        if run_replay(xspdb, args.replay):
            return
    if args.diff:
        assert os.path.isfile(args.diff)
        if not xspdb.api_load_ref_so(args.diff):
            XSPdb.error(f"load difftest ref so {args.diff} failed")
            return
        xspdb.api_set_difftest_diff(True)
    if args.pc_commits != 0:
        return run_commits(xspdb, args.pc_commits)
    if args.interact_at == 0:
        xspdb.set_trace()
    if not args.image:
        XSPdb.warn("No image to execute, Entering the interactive debug mode")
        xspdb.set_trace()
    delta = args.max_cycles
    while delta > 0 and not xspdb.api_dut_is_step_exit():
        delta = delta - emu_step(delta)
    XSPdb.info("Finished cycles: %d (%d ignored)" % (args.max_cycles - delta, delta))


if __name__ == "__main__":
    args, xspdb = create_xspdb()
    import bdb
    try:
        main(args, xspdb)
        XSPdb.info("Exit.")
    except bdb.BdbQuit:
        pass
