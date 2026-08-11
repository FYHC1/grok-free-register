#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单实例锁：防止 注册.py / 授权.py 被重复启动导致双批次并发写同一台账/SSO 文件。

用法（在 wrapper 脚本中）:
  sys.path.insert(0, str(ROOT / "scripts"))
  from single_instance import acquire_single_instance
  if not acquire_single_instance(ROOT / "keys" / ".lock-register", "注册"):
      return 3

锁文件内含启动进程 PID；退出时自动删除（atexit）。
残留锁（进程已死）会在下次启动时被清理，不阻塞后续运行。
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _release(lock_file: Path) -> None:
    try:
        lock_file.unlink()
    except OSError:
        pass


def acquire_single_instance(lock_file: Path, name: str) -> bool:
    """原子获取单实例锁；已有存活实例时返回 False。

    失败返回 False 时调用方应直接退出（不执行注册/授权逻辑）。
    锁文件无法创建等异常按“放行”处理（fail-open），避免误伤正常流程。
    """
    lock_file = Path(lock_file)
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    for _ in range(3):
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int((lock_file.read_text(encoding="utf-8") or "0").strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if _pid_alive(pid):
                print(f"[!] 已有 {name} 进程在运行 (PID={pid})，本次启动取消。", flush=True)
                return False
            # 陈旧锁：进程已死，删除后重试
            try:
                lock_file.unlink()
            except OSError:
                pass
            continue
        except OSError as exc:
            print(f"[!] 单实例锁异常（{exc}），继续运行。", flush=True)
            return True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        atexit.register(_release, lock_file)
        return True
    print(f"[!] 无法获取 {name} 单实例锁，本次启动取消。", flush=True)
    return False
