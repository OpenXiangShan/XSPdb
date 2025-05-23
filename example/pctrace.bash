#!/bin/bash

# This script is used to run the emu.py program with a set of ELF files
# and generate logs for each ELF file.
# It takes a target directory containing ELF files, an optional log directory,
# and optional custom arguments to be passed to emu.py.
# The script will find the emu.py and jump_zero.script files in the current
# directory or its parent directory, and use them to run the emu.py program.
# The script will create a log directory if it does not exist, and will
# generate log files for each ELF file in the target directory.
# The log files will be named after the ELF files, with a .all.log and
# .exec.log extension.
# The script will also generate a .exec.fst file for each ELF file, which
# contains the execution trace of the ELF file.
# The script will print the progress of the processing, including the
# number of ELF files processed, the total number of ELF files, the
# percentage of completion, the elapsed time, and the estimated finish
# time.

find_file() {
    for dir in "$PWD" "$(dirname "$PWD")" "$OLDPWD" "example" "ready-to-run"; do
        if [ -f "$dir/$1" ]; then
            realpath "$dir/$1"
            return 0
        fi
    done
    echo "$1 not found in current, parent, or previous directory." >&2
    return 1
}

EMU_PY_PATH=$(find_file "emu.py") || exit 1
JPZ_SC_PATH=$(find_file "jump_zero.script") || exit 1
echo "emu.py found at: $EMU_PY_PATH"
echo "jump_zero.script found at: $JPZ_SC_PATH"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <target_directory> [log_directory] [custom_arguments]"
    echo "    target_directory: the directory containing the .elf files to be processed"
    echo "       log_directory: pc log dir, defaults to ./log"
    echo "    custom_arguments: additional arguments to be passed to emu.py"
    exit 1
fi

TARGET_DIR=$1
LOG_DIR=${2:-./log}
CARGS=${@:3}

echo "Target directory: $TARGET_DIR" "Log directory: $LOG_DIR"
mkdir -p $LOG_DIR

elf_files=($(find "$TARGET_DIR" -name "*.elf"))
total_files=${#elf_files[@]}
used_files=0
start_time=$(date +%s)
process_log=$LOG_DIR"/process-${start_time}.log"

debug() {
    echo -e $@ | tee -a $process_log
}

seconds_to_hms() {
    local total_seconds=$1
    local hours=$((total_seconds / 3600))
    local minutes=$(( (total_seconds % 3600) / 60 ))
    local seconds=$((total_seconds % 60))
    printf "%02d:%02d:%02d" $hours $minutes $seconds
}

for elf in `find $TARGET_DIR -name *.elf`; do
    log_prefix=$LOG_DIR/"${elf//\//_}"
    save_alg=${log_prefix}".all.log"
    save_log=${log_prefix}".exec.log"
    save_fst=${log_prefix}".exec.fst"
    if [ -f "$save_log" ]; then
        debug "skip $elf, log file already exists"
        used_files=$((used_files + 1))
        continue
    fi
    job_start_time=$(date +%s)
    debug "Processing ELF: $elf at $(date +%Y-%m-%d\ %H:%M:%S)"
    # construct the arguments for emu.py
    ARGS="--no-interact -s $JPZ_SC_PATH -i $elf -pc -1 --trace-pc-symbol-block-change"
    ARGS="$ARGS --log-file $save_log --log-level warn --wave-path $save_fst -e -1 -C 1000000000 $CARGS"
    # run the emu.py
    stdbuf -oL -eL $EMU_PY_PATH $ARGS 2>&1|tee $save_alg
    ret_code=${PIPESTATUS[0]}
    if [[ $ret_code -ne 0 ]]; then
        debug "exit witch code $ret_code May be interrupted, check the log file for details."
        exit 1
    fi
    used_files=$((used_files + 1))
    percent=$((used_files * 100 / total_files))
    now_time=$(date +%s)
    elapsed_time=$((now_time - start_time))
    remain_time=$((elapsed_time * (total_files - used_files) / used_files))
    finish_time=$(date -d "@$((start_time + remain_time))" "+%Y-%m-%d %H:%M:%S")
    ctime=$(seconds_to_hms $((now_time - job_start_time)))
    debug "run: $elf  complete at $(date +%Y-%m-%d\ %H:%M:%S) time consumed: ${ctime}"
    debug "Progress: $used_files / $total_files ($percent%)"
    etime=$(seconds_to_hms $elapsed_time)
    rtime=$(seconds_to_hms $remain_time)
    debug "Elapsed: ${etime}, Estimated finish: $finish_time (remian: ${rtime})\n"
done
