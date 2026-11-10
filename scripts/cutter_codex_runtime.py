#!/usr/bin/env python3
"""Cutter-only Codex compatibility: suppress unrelated legacy MCP transports.

The terminal bridge starts both an app-server and a TUI. Applying this one
runtime override to both preserves CODEX_HOME auth, role, model and base options
without rewriting the user's config or disabling connectors in other sessions.
"""
import os
from pathlib import Path
import sys


def runtime_argv(args, environ=None):
    env=os.environ if environ is None else environ
    binary=env.get('CUTTER_CODEX_REAL_BIN')
    if not binary or not Path(binary).is_absolute() or Path(binary).resolve()==Path(__file__).resolve():
        raise ValueError('Valid CUTTER_CODEX_REAL_BIN required')
    return [binary,'-c','mcp_servers={}',*args]


def main():
    args=runtime_argv(sys.argv[1:])
    os.execv(args[0],args)


if __name__=='__main__':main()
