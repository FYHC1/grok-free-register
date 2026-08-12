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
import time
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否存活。

    Windows 用 OpenProcess（同步 API，无 tasklist 的登记延迟）；
    失败码 87(ERROR_INVALID_PARAMETER)/128 视为进程不存在。
    POSIX 用 os.kill(pid, 0)。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        open_proc = ctypes.WinDLL("kernel32", use_last_error=True).OpenProcess
        handle = open_proc(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() not in (87, 128)  # 87/128 = 进程不存在
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

    并发安全说明（2026-08-12 实战修复）：
    原实现存在竞态窗口——进程 A 用 O_CREAT|O_EXCL 创建锁文件后、
    写入 PID 之前，进程 B 读到空文件 → pid=0 → 误判为陈旧锁 → 删除并
    抢到锁，导致双实例并发（曾引发注册双批次事故）。修复：
      1. 创建后立即写入 PID（缩小窗口）；写入失败则回滚删除锁文件
      2. 读到空/非数字 PID 时视为“可能创建中”，绝不删除，等待后重试
      3. 只有 PID 有效且确认进程已死亡时才清理陈旧锁
    """
    lock_file = Path(lock_file)
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    for attempt in range(5):
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                raw = lock_file.read_text(encoding="utf-8", errors="ignore")
                pid = int((raw or "0").strip() or "0")
            except (OSError, ValueError):
                pid = -1
            if pid > 0:
                if _pid_alive(pid):
                    print(f"[!] 已有 {name} 进程在运行 (PID={pid})，本次启动取消。", flush=True)
                    return False
                # 陈旧锁：进程已死，删除后重试
                try:
                    lock_file.unlink()
                except OSError:
                    pass
            else:
                # pid 为空/非法：可能对方正在创建中，退避重试，绝不删除
                time.sleep(0.3 * (attempt + 1))
            continue
        except OSError as exc:
            print(f"[!] 单实例锁异常（{exc}），继续运行。", flush=True)
            return True
        # 创建成功：立即写入 PID（缩短竞态窗口）
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            # 写入失败：回滚删除锁文件，避免留下不可用锁
            try:
                lock_file.unlink()
            except OSError:
                pass
            return True
        atexit.register(_release, lock_file)
        return True
    print(f"[!] 无法获取 {name} 单实例锁，本次启动取消。", flush=True)
    return False
