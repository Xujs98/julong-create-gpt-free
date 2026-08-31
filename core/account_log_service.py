# -*- coding: utf-8 -*-
"""账号级日志清理服务。

账号页的查活、提链、2FA 重设和 Codex 补跑日志都按邮箱写在项目根的
``注册日志``目录中。本模块只匹配这四类固定前缀，注册任务 UUID 日志和
其他运行日志保持不受影响。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_ACCOUNT_LOG_PATTERNS = (
    "live-check-*.log",
    "extract-link-*.log",
    "twofa-reset-*.log",
    "codex-retry-*.log",
)
_AUTO_CLEANUP_INTERVAL = 6 * 60 * 60
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STARTED = False


def account_log_paths(log_dir: Path | None = None) -> list[Path]:
    """返回账号级日志文件，按路径去重并限制在目标目录内。"""
    root = Path(log_dir or _LOG_DIR).expanduser()
    try:
        root = root.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return []
    if not root.exists() or not root.is_dir():
        return []
    found: dict[Path, None] = {}
    for pattern in _ACCOUNT_LOG_PATTERNS:
        for path in root.glob(pattern):
            try:
                resolved = path.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved.parent != root or not path.is_file():
                continue
            found[resolved] = None
    return sorted(found, key=lambda item: item.name.lower())


def _safe_days(value: object, default: int = 30) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = default
    return max(1, min(3650, days))


def cleanup_account_logs(
    *,
    older_than_days: int | None = None,
    all_logs: bool = False,
    log_dir: Path | None = None,
) -> dict:
    """清理账号级日志。

    ``all_logs=True`` 清空全部账号日志；否则按文件修改时间删除超过
    ``older_than_days`` 的日志。正在写入的日志只要尚未达到年龄阈值就会
    保留，避免影响当前任务。
    """
    paths = account_log_paths(log_dir)
    cutoff = None
    days = None
    if not all_logs:
        days = _safe_days(older_than_days)
        cutoff = time.time() - days * 24 * 60 * 60
    deleted = 0
    deleted_bytes = 0
    skipped = 0
    failed: list[dict[str, str]] = []
    for path in paths:
        try:
            stat = path.stat()
            if cutoff is not None and stat.st_mtime > cutoff:
                skipped += 1
                continue
            deleted_bytes += int(stat.st_size or 0)
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            failed.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "ok": not failed,
        "mode": "all" if all_logs else "older_than_days",
        "days": days,
        "scanned": len(paths),
        "deleted": deleted,
        "deleted_bytes": deleted_bytes,
        "skipped": skipped,
        "failed": failed,
        "failed_count": len(failed),
        "log_dir": str(Path(log_dir or _LOG_DIR)),
    }


def cleanup_account_logs_from_config() -> dict:
    """按当前配置执行一次自动清理；关闭开关时仅返回 skipped。"""
    try:
        from config import webui as webui_config

        enabled = bool(getattr(webui_config, "ACCOUNT_LOG_AUTO_CLEANUP", False))
        days = getattr(webui_config, "ACCOUNT_LOG_RETENTION_DAYS", 30)
    except Exception:
        enabled, days = False, 30
    if not enabled:
        return {"ok": True, "enabled": False, "mode": "disabled", "deleted": 0, "skipped": 0}
    result = cleanup_account_logs(older_than_days=days)
    result["enabled"] = True
    return result


def start_auto_cleanup_scheduler() -> bool:
    """启动单例后台清理线程；首次启动立即按配置清理一次。"""
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    def _run() -> None:
        while True:
            try:
                result = cleanup_account_logs_from_config()
                if result.get("enabled") and result.get("deleted"):
                    logger.info("[AccountLog] 自动清理完成：删除 %s 个账号日志", result.get("deleted"))
                if result.get("failed_count"):
                    logger.warning("[AccountLog] 有 %s 个账号日志删除失败", result.get("failed_count"))
            except Exception:
                logger.exception("[AccountLog] 自动清理失败")
            time.sleep(_AUTO_CLEANUP_INTERVAL)

    threading.Thread(target=_run, name="account-log-cleanup", daemon=True).start()
    return True


__all__ = [
    "account_log_paths",
    "cleanup_account_logs",
    "cleanup_account_logs_from_config",
    "start_auto_cleanup_scheduler",
]
