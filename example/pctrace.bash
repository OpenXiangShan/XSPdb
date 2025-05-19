#!/bin/bash

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
    echo "Usage: $0 <target_directory> [log_directory]"
    echo "    target_directory: the directory containing the .elf files to be processed"
    echo "       log_directory: pc log dir, defaults to ./log"
    exit 1
fi

TARGET_DIR=$1
LOG_DIR=${2:-./log}

echo "Target directory: $TARGET_DIR" "Log directory: $LOG_DIR"
mkdir -p $LOG_DIR

elf_files=($(find "$TARGET_DIR" -name "*.elf"))
total_files=${#elf_files[@]}
used_files=0
start_time=$(date +%s)

for elf in `find $TARGET_DIR -name *.elf`; do
    save_alg=$LOG_DIR/"${elf//\//_}".all.log
    save_log=$LOG_DIR/"${elf//\//_}".exec.log
    save_fst=$LOG_DIR/"${elf//\//_}".exec.fst
    echo "Processing ELF: $elf"
    # construct the arguments for emu.py
    ARGS="--no-interact -s $JPZ_SC_PATH -i $elf -pc -1 --trace-pc-symbol-block-change"
    ARGS="$ARGS --log-file $save_log --log-level warn --wave-path $save_fst -e -1"
    # run the emu.py
    stdbuf -oL -eL $EMU_PY_PATH $ARGS 2>&1|tee $save_alg
    ret_code=${PIPESTATUS[0]}
    if [[ $ret_code -ne 0 ]]; then
        echo "exit witch code $ret_code May be interrupted, check the log file for details."
        exit 1
    fi
    used_files=$((used_files + 1))
    percent=$((used_files * 100 / total_files))
    now_time=$(date +%s)
    elapsed_time=$((now_time - start_time))
    remain_time=$((elapsed_time * (total_files - used_files) / used_files))
    finish_time=$(date -d "@$((start_time + remain_time))" "+%Y-%m-%d %H:%M:%S")
    echo "run: $elf  complete"
    echo "Progress: $used_files / $total_files ($percent%)"
    echo -e "Elapsed: ${elapsed_time}s, Estimated finish: $finish_time (remian: ${remain_time}s)\n"
done
