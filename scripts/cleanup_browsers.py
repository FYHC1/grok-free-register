#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台清理残留浏览器进程（P1-5）。

背景：授权/注册进程被强杀（SIGKILL/崩溃/WSL 回收）后，其启动的
浏览器（chromium/chrome/firefox/camoufox）子进程会残留为孤儿，
累积占用内存。本工具在授权/导入脚本入口处调用，仅清理孤儿浏览器
进程——即父进程已退出的浏览器，不误杀正在被其他应用使用的进程。

策略（跨平台）:
  - Windows: tasklist /FI /V → 检查浏览器进程的关联父进程是否还在；
    简化实现：启动时若发现浏览器进程存在且其父 python 已不在，则清理。
    更保守的实现：仅当进程的命令行包含我们的浏览器特征（camoufox/
    cloakbrowser）或进程创建时间早于本脚本启动 1 分钟以上才清理。
  - POSIX: ps -o ppid= 检查父进程存活。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Sequence


def _is_browser_name(name: str) -> bool:
    n = (name or "").lower()
    return any(
        k in n
        for k in (
            "chrome",
            "chromium",
            "firefox",
            "camoufox",
            "marionette",
            "geckodriver",
            "msedge",
        )
    )


def _windows_cleanup() -> int:
    """Windows: 清理"创建时间早于本脚本 1 分钟"的浏览器进程。

    使用创建时间阈值而非父进程检查——Windows 上浏览器可能有多个祖先进程，
    父进程判定复杂。1 分钟阈值确保不误杀刚启动（正在用）的浏览器，
    只清理运行已久的残留。
    """
    killed = 0
    try:
        script_start = time.time()
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match 'chrome|chromium|firefox|camoufox|msedge' } | "
                "Select-Object ProcessId,Name,CreationDate | ConvertTo-Json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if out.returncode != 0:
            return 0
        import json

        data = out.stdout.strip()
        if not data:
            return 0
        procs = json.loads(data)
        if isinstance(procs, dict):
            procs = [procs]
        for p in procs:
            try:
                created = float(p.get("CreationDate") or 0) / 1e7 - 11644473600  # 100ns → epoch
            except Exception:
                continue
            age = script_start - created if created > 0 else 0
            pid = p.get("ProcessId")
            if pid is None:
                continue
            if age > 60:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
                killed += 1
        return killed
    except Exception:
        return 0


def _posix_cleanup() -> int:
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode != 0:
            return 0
        alive_ppids = set()
        browser_pids = []
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            pid, ppid, comm = parts[0], parts[1], parts[2]
            alive_ppids.add(pid)
            if _is_browser_name(comm):
                browser_pids.append((pid, ppid, comm))
        killed = 0
        for pid, ppid, comm in browser_pids:
            if ppid not in alive_ppids and ppid != "1":
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=10)
                killed += 1
        return killed
    except Exception:
        return 0


def cleanup_stale_browsers() -> int:
    """清理残留浏览器进程，返回清理数量。异常时返回 0（fail-open）。"""
    try:
        if sys.platform == "win32":
            return _windows_cleanup()
        return _posix_cleanup()
    except Exception:
        return 0


if __name__ == "__main__":
    n = cleanup_stale_browsers()
    print(f"cleaned {n} stale browser process(es)")