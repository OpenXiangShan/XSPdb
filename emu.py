#!/usr/bin/env python3

import os
import sys
import argparse
import logging
import time

logging_level_map = {"debug": logging.DEBUG,
                      "info": logging.INFO,
                      "warn": logging.WARN,
                      "erro": logging.ERROR,
                     }

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
    def timesec(s):
        s = s.strip().lower()
        if s.endswith("s"):
            return int(s[:-1])
        elif s.endswith("m"):
            return int(s[:-1]) * 60
        elif s.endswith("h"):
            return int(s[:-1]) * 3600
        else:
            raise ValueError(f"Invalid time format: {s}")
    parser = argparse.ArgumentParser(description="XSPdb Emulation Tool")
    parser.add_argument("-v", "--version", action="version", version=f"XSPdb {XSPdb.__version__}")
    parser.add_argument("-C", "--max-cycles", type=int, default=0xFFFFFFFFFFFFFFFF, help="maximum simulation cycles to execute")
    parser.add_argument("-i", "--image", type=str, default="", help="image file to load and run")
    parser.add_argument("-b", "--wave-begin", type=int, default=-2, help="start waveform dump at the specified cycle")
    parser.add_argument("-e", "--wave-end", type=int, default=-2, help="stop waveform dump at the specified cycle")
    parser.add_argument("-t", "--interact-at", type=int, default=-1, help="enter interactive mode at the specified cycle")
    parser.add_argument("-l", "--log", action="store_true", default=False, help="enable logging output")
    parser.add_argument("--log-file", type=str, default="", help="log file name (default: ./XSPdb.log)")
    parser.add_argument("-bi", "--batch-interval", type=float, default=0.1, help="interval time (seconds) between batch commands")
    parser.add_argument("-s", "--script", type=str, default="", help="script file to execute")
    parser.add_argument("-r", "--replay", type=str, default="", help="replay log file")
    parser.add_argument("--debug-level", type=str, default="", choices=["debug", "info", "warn", "erro"], help="set debug level")
    parser.add_argument("--log-level", type=str, default="", choices=["debug", "info", "warn", "erro"], help="set log level")
    parser.add_argument("-pc", "--pc-commits", type=int, default=0, help="run until the specified number of commits; -1 means no limit")
    parser.add_argument("--sim-args", type=lambda s: s.split(','), default=[], help="additional simulator arguments (comma-separated)")
    parser.add_argument("-F", "--flash", type=str, default="", help="flash binary file for simulation")
    parser.add_argument("--no-interact", action="store_true", default=False, help="disable interactive mode (do not handle the ctrl-c signal)")
    parser.add_argument("--wave-path", type=str, default="", help="output path for waveform file")
    parser.add_argument("--ram-size", type=str, default="", help="simulation RAM size (e.g., 8GB or 128MB)")
    parser.add_argument("--diff", type=str, default="", help="path to REF shared object for difftest testing")
    parser.add_argument("--cmds", type=str, default="", help="XSPdb commands to execute before run (\\n for newline)")
    parser.add_argument("--cmds-post", type=str, default="", help="XSPdb commands to execute after script/replay (\\n for newline)")
    parser.add_argument("--mem-base-address", type=address, default=0x80000000, help="base address of memory")
    parser.add_argument("--flash-base-address", type=address, default=0x10000000, help="base address of flash")
    parser.add_argument("--diff-first-inst_address", type=address, default=-1, help="first instruction address for difftest")
    parser.add_argument("--trace-pc-symbol-block-change", action="store_true", default=False, help="enable tracing of PC symbol block changes")
    parser.add_argument("--max-run-time", type=timesec, default=0, help="maximum run time (eg 10s, 1m, 1h)")
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

def timesec_to_str(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    return f"{h:02}:{m:02}:{s:02}"

def run_script(xspdb, script_path, it_time):
    xspdb.api_exec_script(script_path, gap_time=it_time)
    xspdb.api_append_init_cmd("xnop")
    xspdb.set_trace()
    return False


def run_replay(xspdb, replay_path, it_time):
    xspdb.api_exec_script(replay_path, gap_time=it_time,
                          target_prefix=xspdb.log_cmd_prefix,
                          target_subfix=xspdb.log_cmd_suffix,
                          )
    xspdb.api_append_init_cmd("xnop")
    xspdb.set_trace()
    return False


def run_commits(xspdb, commits, max_run_time):
    time_start = time.time()
    if commits < 0:
        commits = 0xFFFFFFFFFFFFFF
    batch_size = 100
    batch_count = commits // batch_size
    batch_remain = commits % batch_size
    reach_max_time = False
    def run_delta(delta):
        nonlocal reach_max_time
        runc = 0
        while delta > 0 and not xspdb.api_dut_is_step_exit():
            c = xspdb.api_xistep(delta)
            runc += c
            delta = delta - c
            check_is_need_trace(xspdb)
            delta_time = time.time() - time_start
            if delta_time > max_run_time and max_run_time > 0:
                XSPdb.info(f"Max run time {timesec_to_str(max_run_time)} reached (runed {timesec_to_str(delta_time)}), exit instruction execution")
                reach_max_time = True
                break
        return runc
    run_ins = 0
    for _ in range(batch_count):
        if not reach_max_time and not xspdb.api_dut_is_step_exit():
           run_ins += run_delta(batch_size)
        else:
            break
    if not reach_max_time:
        run_ins += run_delta(batch_remain)
    xspdb.message(f"Execute {run_ins} commits completed ({commits - run_ins} ignored)")


def create_xspdb():
    args = args_parser()
    if args.log:
        XSPdb.XSPdb.api_log_enable_log(True)
    if args.log_file:
        XSPdb.XSPdb.api_log_set_log_file(args.log_file)
    XSPdb.message(f"Exec: {' '.join(sys.argv)}")
    sim_kwargs = {}
    sim_args = args.sim_args
    if args.wave_path:
        args.wave_path = os.path.abspath(args.wave_path)
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
    if args.no_interact:
        xpd_kwagrs["no_interact"] = True
    if args.debug_level:
        XSPdb.set_xspdb_debug_level(logging_level_map[args.debug_level])
    if args.log_level:
        XSPdb.set_xspdb_log_level(logging_level_map[args.log_level])
    # New XSPdb
    from XSPython import difftest as df, xsp
    xspdb = XSPdb.XSPdb(dut, df, xsp, **xpd_kwagrs)
    assert args.wave_begin <= args.wave_end, "arg --log-begin must be less than log_end"
    assert args.wave_end <= args.max_cycles, "arg --log-end must be less than max_cycles"
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
    if args.wave_begin != args.wave_end:
        wave_file_path = args.wave_path if args.wave_path else ""
        if args.wave_begin <= 0:
            XSPdb.info(f"Waweform on at HW cycle = Zero")
            xspdb.api_waveform_on(wave_file_path)
        else:
            def cb_on_log_begin(s, checker, k, clk, sig, target):
                XSPdb.info(f"Waveform on at HW cycle = {target}")
                xspdb.api_waveform_on(wave_file_path)
                s.interrupt = False
            XSPdb.info(f"Set waveform on callback at HW cycle = {args.wave_begin}")
            xspdb.api_xbreak("SimTop_top.SimTop.timer", "eq", args.wave_begin, callback=cb_on_log_begin, callback_once=True)
        def cb_on_log_end(s, checker, k, clk, sig, target):
            XSPdb.info(f"Waveform off at HW cycle = {target}")
            xspdb.api_waveform_off()
            s.interrupt = False
        if args.wave_end > 0:
            XSPdb.info(f"Set waveform off callback at HW cycle = {args.wave_end}")
            xspdb.api_xbreak("SimTop_top.SimTop.timer", "eq", args.wave_end, callback=cb_on_log_end, callback_once=True)
    if args.interact_at > 0:
        def cb_on_interact(s, checker, k, clk, sig, target):
            XSPdb.info(f"Interact at HW cycle = {target}")
            setattr(xspdb, "__xspdb_need_fast_trace__", True)
        XSPdb.info(f"Set interact callback at HW cycle = {args.interact_at}")
        xspdb.api_xbreak("SimTop_top.SimTop.timer", "eq", args.interact_at, callback=cb_on_interact, callback_once=True)
    if args.flash:
        xspdb.api_dut_flash_load(args.flash)
    if args.trace_pc_symbol_block_change:
        xspdb.api_turn_on_pc_symbol_block_change(True)
    if args.cmds:
        for c in args.cmds.replace("\\n", "\n").split("\n"):
            xspdb.api_append_init_cmd(c.strip())
    if args.cmds_post:
        for c in args.cmds_post.replace("\\n", "\n").split("\n"):
            xspdb.api_batch_append_tail_one_cmd(c.strip())
    if args.script:
        if run_script(xspdb, args.script, args.batch_interval):
            return
    if args.replay:
        if run_replay(xspdb, args.replay, args.batch_interval):
            return
    if args.diff:
        assert os.path.isfile(args.diff)
        if not xspdb.api_load_ref_so(args.diff):
            XSPdb.error(f"Load difftest ref so {args.diff} failed")
            return
        xspdb.api_set_difftest_diff(True)
    if args.cmds or args.cmds_post:
        if not args.script and not args.replay:
            # Not run script or replay, so set trace
            xspdb.set_trace()
    wave_at_last = (args.wave_begin != args.wave_end) and (args.wave_end <= 0)
    if args.pc_commits != 0:
        cycle_index = xspdb.dut.xclock.clk
        run_commits(xspdb, args.pc_commits, args.max_run_time)
        run_cycles = xspdb.dut.xclock.clk - cycle_index
        if run_cycles >= args.wave_end or wave_at_last:
            XSPdb.info("Waveform off at HW cycle = %d (simulated %d cycles)" % (xspdb.dut.xclock.clk, run_cycles))
            xspdb.api_waveform_off()
        return
    if args.interact_at == 0:
        xspdb.set_trace()
    if not args.image:
        XSPdb.warn("No image to execute, Entering the interactive debug mode")
        xspdb.set_trace()
    cycle_batch_size = 10000
    cycle_batch_count = args.max_cycles // cycle_batch_size
    cycle_batch_remain = args.max_cycles % cycle_batch_size
    cycle_reach_max_time = False
    time_start = time.time()
    def run_cycle_deta(delta):
        nonlocal cycle_reach_max_time
        runc = 0
        while delta > 0 and not xspdb.api_dut_is_step_exit():
            c = emu_step(delta)
            runc += c
            delta = delta - c
            check_is_need_trace(xspdb)
            delta_time = time.time() - time_start
            if delta_time > args.max_run_time and args.max_run_time > 0:
                cycle_reach_max_time = True
                XSPdb.info(f"Max run time {timesec_to_str(args.max_run_time)} reached (runed {timesec_to_str(delta_time)}), exit cycle execution")
                break
        return runc
    run_cycles = 0
    for _ in range(cycle_batch_count):
        if not cycle_reach_max_time and not xspdb.api_dut_is_step_exit():
            run_cycles += run_cycle_deta(cycle_batch_size)
        else:
            break
    if not cycle_reach_max_time:
        run_cycles += run_cycle_deta(cycle_batch_remain)
    delta = args.max_cycles - run_cycles
    # Check if the waveform is on
    if wave_at_last or args.wave_end >= run_cycles:
        XSPdb.info("Waveform off at HW cycle = %d" % (xspdb.dut.xclock.clk))
        xspdb.api_waveform_off()
    XSPdb.info("Finished cycles: %d (%d ignored)" % (run_cycles, delta))


if __name__ == "__main__":
    args, xspdb = create_xspdb()
    import bdb
    try:
        main(args, xspdb)
        XSPdb.info("Exit.")
    except bdb.BdbQuit:
        pass
