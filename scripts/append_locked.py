#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台文件追加锁：给 jsonl/txt 追加写加进程间互斥，防止并发行交错。

背景：注册多 worker / 双批次事故曾导致 keys/*.jsonl、accounts.txt 并发写。
open(path, "a") 在多个进程同时写时，小写入在 POSIX 上通常原子，
但跨进程+WSL/Windows 混合环境不保证；这里用 flock(POSIX)/msvcrt(Windows)
给“追加写”加文件锁，锁文件与目标文件同目录（*.lock），写完后释放。

用法:
    from scripts.append_locked import locked_append
    locked_append(path, line, durable=True)

    # 或作为上下文管理器
    with locked_open(path, "a") as f:
        f.write(line)
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if sys.platform == "win32":  # pragma: no cover - Windows-only
    import msvcrt


def _lock_fd(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows-only
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows-only
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def locked_open(path: Path | str, mode: str = "a", encoding: str = "utf-8") -> Iterator:  # noqa: ANN401 - file object type varies by mode
    """以追加模式打开文件并持有排它锁；退出时释放锁（不删除锁文件）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _lock_fd(fd)
        with open(path, mode, encoding=encoding) as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        _unlock_fd(fd)
        os.close(fd)


def locked_append(path: Path | str, line: str, *, durable: bool = False) -> None:
    """追加一行（自动补 \n）到文件，带进程间锁；durable=True 时 fsync。"""
    if not line.endswith("\n"):
        line += "\n"
    with locked_open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        if durable:
            fh.flush()
            os.fsync(fh.fileno())
