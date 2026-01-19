#coding=utf-8
"""
Config loader for MCP/agent server.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ToolchainConfig:
    compiler: Optional[str] = None
    elf_loader: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ServerConfig:
    agent_endpoint: str
    agent_transport: str = "shm"  # shm | unix | tcp
    headless: bool = True
    tty_buf_lines: int = 4096
    agent_module: Optional[str] = None
    toolchain: ToolchainConfig = field(default_factory=ToolchainConfig)

    @staticmethod
    def load(path: str) -> "ServerConfig":
        with open(path, "r") as f:
            data = json.load(f)
        tc_raw = data.get("toolchain", {})
        tc = ToolchainConfig(
            compiler=tc_raw.get("compiler"),
            elf_loader=tc_raw.get("elf_loader"),
            extras={k: v for k, v in tc_raw.items() if k not in ("compiler", "elf_loader")},
        )
        return ServerConfig(
            agent_endpoint=data.get("agent_endpoint", ".xspdb_ckpt/agent.shm"),
            agent_transport=data.get("agent_transport", os.environ.get("XSPDB_AGENT_TRANSPORT", "shm")),
            headless=bool(data.get("headless", True)),
            tty_buf_lines=int(data.get("tty_buf_lines", 4096)),
            agent_module=data.get("agent_module"),
            toolchain=tc,
        )

    @staticmethod
    def default_path() -> str:
        return os.environ.get("XSPDB_SERVER_CONFIG", "xspdb_server.json")
