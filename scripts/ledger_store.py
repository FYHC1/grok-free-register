#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSONL 台账的原子读-判-写封装（LedgerStore）。

背景：授权/注册脚本用 keys/cpa-enroll-ledger.jsonl 等 JSONL 记录每个
email 的处理结果。旧实现是"启动时读全量快照 → 循环中追加"，多进程
并发时双方都基于同一旧快照判断，导致同一 email 被重复处理（双批次
事故的台账侧根因）。append_locked.py 只保证"追加写"不交错，无法消除
"读-判-写"三步之间的竞态。

本模块把"读取最新状态"与"追加"都放进同一个文件锁临界区内，
提供原子化的 status()/append()；调用方用同一把锁保护"检查-处理-记录"
即可保证并发下每个 email 只被处理一次。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

try:
    from scripts.append_locked import _lock_fd, _unlock_fd
except Exception:  # pragma: no cover - defensive
    _lock_fd = _unlock_fd = None


class LedgerStore:
    """线程/进程安全的 JSONL 台账。

    用法:
        store = LedgerStore(Path("keys/cpa-enroll-ledger.jsonl"))
        if store.status(email) not in {"ok", "fail"}:
            # 处理 email ...
            store.append(email=email, status="ok", stage="done")
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.parent / f".{self.path.name}.lock"

    def _read_latest(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            return latest
        for raw in self.path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            email = str(obj.get("email") or "").strip().lower()
            status = str(obj.get("status") or "").strip().lower()
            if email and status in {"ok", "fail", "processing"}:
                latest[email] = obj
        return latest

    def _acquire_lock(self):
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if _lock_fd is not None:
            _lock_fd(fd)
        return fd

    def _release_lock(self, fd) -> None:
        try:
            if _unlock_fd is not None:
                _unlock_fd(fd)
        finally:
            os.close(fd)

    def status(self, email: str) -> str:
        """返回 email 的当前台账状态（""=未处理；ok/fail=已处理）。锁内实时读。"""
        email = str(email or "").strip().lower()
        if not email:
            return ""
        fd = self._acquire_lock()
        try:
            return self._read_latest().get(email, {}).get("status", "")
        finally:
            self._release_lock(fd)

    def claim(self, email: str, *, skip_existing: bool = True, skip_failed: bool = True) -> str:
        """原子认领：锁内检查 email 是否已处理；未处理则写入 processing 占位。

        返回 "ok"/"fail" 表示已处理（应跳过）；"processing" 表示其他进程
        正在处理（应跳过）；"" 表示本次认领成功（可执行任务，任务完成后
        调用 append 覆盖为最终 ok/fail 状态）。整个判断+占位在锁内完成，
        多进程并发时每个 email 只会被一个调用方认领。
        """
        email = str(email or "").strip().lower()
        if not email:
            return "ok"
        fd = self._acquire_lock()
        try:
            latest = self._read_latest()
            row = latest.get(email, {})
            status = row.get("status", "")
            if status == "ok" and skip_existing:
                return "ok"
            if status == "fail" and skip_failed:
                return "fail"
            if status == "processing":
                return "processing"
            self._write_row_locked(fd, {
                "email": email,
                "status": "processing",
                "stage": "",
                "error": "",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
            return ""
        finally:
            self._release_lock(fd)

    def _write_row_locked(self, fd, row: dict[str, Any]) -> None:
        """在已持有的锁内追加一行。"""
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def last(self, email: str) -> dict[str, Any]:
        """返回 email 的完整台账记录（无记录返回 {}）。"""
        email = str(email or "").strip().lower()
        if not email:
            return {}
        fd = self._acquire_lock()
        try:
            return self._read_latest().get(email, {})
        finally:
            self._release_lock(fd)

    def append(
        self,
        *,
        email: str,
        status: str,
        stage: str = "",
        error: str = "",
        index: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """锁内追加一条记录（追加本身在锁内，保证与 status() 原子一致）。"""
        email = str(email or "").strip().lower()
        status_l = str(status or "").strip().lower()
        if not email or status_l not in {"ok", "fail"}:
            return
        row: dict[str, Any] = {
            "email": email,
            "status": status_l,
            "stage": str(stage or ""),
            "error": str(error or "")[:500],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if index is not None:
            row["index"] = int(index)
        if extra:
            for k, v in extra.items():
                if k not in row:
                    row[k] = v
        fd = self._acquire_lock()
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            self._release_lock(fd)
