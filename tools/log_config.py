#!/usr/bin/env python3
#coding=utf-8

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class LogConfig:
    supervisor: str = 'supervisor.log'
    wrapper_cmd: Optional[str] = None
    wrapper_tty: Optional[str] = None
    sigcont: Optional[str] = None
    tty_stdin: Optional[str] = None
    tty_stdout: Optional[str] = None

    def abs_or_ckpt(self, ckpt_dir: str, p: Optional[str], default: Optional[str] = None) -> Optional[str]:
        if p is None:
            p = default
        if p is None:
            return None
        return p if os.path.isabs(p) else os.path.join(ckpt_dir, p)

    def to_env(self, ckpt_dir: str) -> dict:
        env = {}
        if self.wrapper_cmd is not None:
            env['XSPDB_WRAPPER_CMD_LOG'] = self.abs_or_ckpt(ckpt_dir, self.wrapper_cmd, 'wrapper.cmd.log')
        if self.wrapper_tty is not None:
            env['XSPDB_WRAPPER_TTY_LOG'] = self.abs_or_ckpt(ckpt_dir, self.wrapper_tty, 'wrapper.tty.log')
        if self.sigcont is not None:
            env['XSPDB_SIGCONT_LOG'] = self.abs_or_ckpt(ckpt_dir, self.sigcont, 'wrapper.sigcont.log')
        if self.tty_stdin is not None:
            env['XSPDB_TTY_STDIN_LOG'] = self.abs_or_ckpt(ckpt_dir, self.tty_stdin, 'wrapper.tty.stdin.log')
        if self.tty_stdout is not None:
            env['XSPDB_TTY_STDOUT_LOG'] = self.abs_or_ckpt(ckpt_dir, self.tty_stdout, 'wrapper.tty.stdout.log')
        return env

