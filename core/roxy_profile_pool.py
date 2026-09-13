"""Closed Roxy profile leasing and account fingerprint snapshots.

A separate SQLite lock file per profile serializes threads/processes sharing
runtime storage without locking the application database. Process exit releases
locks automatically. Roxy's reported open state remains authoritative.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from copy import deepcopy
from pathlib import Path

from config import roxybrowser as cfg

logger = logging.getLogger(__name__)
ACCOUNT_TASKS = frozenset({'codex_retry', 'live_check', 'plan_check', 'qualification'})
# A profile-shaped lock key used to serialize the workspace capacity check and
# the single replenishment create across threads and processes.
_CAPACITY_LOCK_PROFILE_ID = '__registration_capacity__'
FINGER_FIELDS = frozenset('''isLanguageBaseIp language isDisplayLanguageBaseIp displayLanguage
isTimeZone timeZone position isPositionBaseIp longitude latitude precisionPos webRTC
resolutionType resolutionX resolutionY fontType font canvas webGL webGLInfo webGLManufacturer
webGLRender webGpu audioContext speechVoices doNotTrack clientRects deviceInfo
hardwareConcurrent deviceMemory deviceNameSwitch deviceName macInfo macAddress
portScanProtect portScanList disableSsl disableSslList'''.split())
# Never restore cookies, proxies, startup arguments, saved tabs or platform logins.
CLEAN_START = dict(syncTab=False, syncCookie=False, syncLocalStorage=False,
                   syncIndexedDb=False, syncPassword=False, syncHistory=False,
                   clearCacheFile=True, clearCookie=True, clearLocalStorage=True,
                   clearHistory=True, randomFingerprint=False)


def fingerprint_snapshot(profile: dict) -> dict:
    if not isinstance(profile, dict):
        return {}
    result = {key: deepcopy(profile[key]) for key in ('coreVersion', 'os', 'osVersion', 'userAgent')
              if isinstance(profile.get(key), str) and profile[key]}
    finger = profile.get('fingerInfo')
    if isinstance(finger, dict):
        result['fingerInfo'] = {k: deepcopy(v) for k, v in finger.items() if k in FINGER_FIELDS}
    return result


def profile_closed(profile: dict) -> bool:
    status = profile.get('openStatus')
    return status is False or (type(status) is int and status == 0) or (
        isinstance(status, str) and status.strip().lower() in {'0', 'false', 'closed'})


def _capacity_target(value: int | str | None, task_kind: str) -> int:
    """Return a positive registration-only environment capacity target."""
    if str(task_kind or '').strip().lower() != 'registration':
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _runtime_root() -> Path:
    from core import db
    return db._SQLITE_PATH.parent / 'roxy-profile-leases'


class ProfileLease:
    def __init__(self, profile_id: str, connection: sqlite3.Connection, path: Path):
        self.profile_id = profile_id
        self.connection = connection
        self.path = path.with_suffix('.json')

    @classmethod
    def try_acquire(cls, workspace: str, profile_id: str):
        root = _runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        # Workspace IDs identify the same Roxy service through host/container aliases.
        key = hashlib.sha256(f'{workspace}:{profile_id}'.encode()).hexdigest()
        path = root / f'{key}.sqlite3'
        conn = sqlite3.connect(str(path), timeout=0, isolation_level=None, check_same_thread=False)
        try:
            conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            conn.close()
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                return None
            raise
        return cls(profile_id, conn, path)

    def remember_initial(self, profile: dict) -> dict:
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(value, dict):
                    return fingerprint_snapshot(value)
            except (OSError, ValueError):
                raise RuntimeError('Roxy 历史指纹快照读取失败，停止初始化以保护已有资料') from None
        snapshot = fingerprint_snapshot(profile)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        return snapshot

    def release(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def _account_context(account: dict | None, account_key: str, task_kind: str) -> tuple[str, dict, str]:
    """Only the four named account tasks may read existing account metadata."""
    if task_kind not in ACCOUNT_TASKS:
        return '', {}, ''
    from core import db
    account = account if isinstance(account, dict) else db.get_account_by_email(account_key) or {}
    key = str(account.get('email') or account_key or '').strip()
    try:
        extra = json.loads(account.get('extra_json') or '{}')
    except (TypeError, ValueError):
        extra = {}
    roxy = extra.get('roxybrowser', {}) if isinstance(extra, dict) else {}
    roxy = roxy if isinstance(roxy, dict) else {}
    binding = db.get_roxy_profile_binding(key) or {} if key else {}
    if str(binding.get('workspace_id') or '') not in {'', str(cfg.ROXY_WORKSPACE_ID)}:
        binding = {}
    saved = fingerprint_snapshot(roxy.get('fingerprint') or {}) or fingerprint_snapshot(binding.get('fingerprint') or {})
    if not saved:
        # Protocol profiles expose fewer compatible fields than native Roxy snapshots.
        from core.fingerprint_profile import load_browser_profile
        browser = (extra.get('browser_profile') if isinstance(extra, dict) else None) or (load_browser_profile(key) if key else None)
        if isinstance(browser, dict) and browser.get('user_agent'):
            saved = {'userAgent': str(browser['user_agent'])}
    preferred = str(roxy.get('profile_id') or binding.get('profile_id') or '').strip()
    return preferred, saved, str(account.get('proxy_used') or roxy.get('proxy_used') or binding.get('last_proxy') or '').strip()


def prepare_idle_profile(client, *, proxy: str | None, proxy_is_fresh: bool,
                         account: dict | None, account_key: str, task_kind: str,
                         capacity_target: int | None = None):
    preferred, saved, old_proxy = _account_context(account, account_key, task_kind)
    if preferred and not saved:
        # Reading a historical profile is allowed even when it is busy; never
        # close it or assume it is free. Saved initial fields win over mutations.
        key = hashlib.sha256(f'{cfg.ROXY_WORKSPACE_ID}:{preferred}'.encode()).hexdigest()
        path = _runtime_root() / f'{key}.json'
        try:
            saved = fingerprint_snapshot(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else fingerprint_snapshot(client.profile_detail(preferred))
        except Exception:
            logger.info('[Roxy] 历史环境资料不可用，本次使用可用快照或随机指纹')
    workspace = str(cfg.ROXY_WORKSPACE_ID or '').strip()
    if not workspace:
        raise RuntimeError('持久环境复用需要配置 ROXY_WORKSPACE_ID')
    timeout = max(0, min(3600, float(getattr(cfg, 'ROXY_IDLE_PROFILE_WAIT_TIMEOUT', 300))))
    deadline = time.monotonic() + timeout
    next_log = 0
    target = _capacity_target(capacity_target, task_kind)
    capacity_creation_attempted = False
    while True:
        profiles = client.list_profiles()

        # Registration workers define the reusable Roxy capacity.  Only one
        # profile is replenished per task start; other Roxy task kinds retain
        # the strict reuse-only behavior.  The workspace lock is separate
        # from per-profile leases so concurrent workers cannot all create for
        # the same capacity gap.
        if target and len(profiles) < target and not capacity_creation_attempted:
            capacity_lease = ProfileLease.try_acquire(workspace, _CAPACITY_LOCK_PROFILE_ID)
            if capacity_lease is not None:
                try:
                    current_profiles = client.list_profiles()
                    if len(current_profiles) < target:
                        create_proxy = proxy if proxy_is_fresh else None
                        client.create_profile(proxy=create_proxy, allow_persistent=True)
                        capacity_creation_attempted = True
                        logger.info(
                            '[Roxy] 注册环境容量不足，已补建 1 个环境：当前=%s 目标线程数=%s',
                            len(current_profiles), target,
                        )
                        # Creation is asynchronous in some Roxy versions;
                        # refresh immediately and let the normal closed-state
                        # loop wait for the new row if it is not visible yet.
                        profiles = client.list_profiles()
                    else:
                        profiles = current_profiles
                finally:
                    capacity_lease.release()

        profiles.sort(key=lambda row: str(row['dirId']) != preferred)
        for row in profiles:
            if not profile_closed(row):
                continue
            pid = str(row['dirId'])
            lease = ProfileLease.try_acquire(workspace, pid)
            if lease is None:
                continue
            try:
                # Recheck after obtaining the lease; another task may have opened it.
                current = next((p for p in client.list_profiles() if str(p['dirId']) == pid), None)
                if current is None or not profile_closed(current):
                    lease.release()
                    continue
                detail = client.profile_detail(pid)
                initial = lease.remember_initial(detail)
                restore = saved or (initial if pid == preferred else {})
                from config import proxy as proxy_cfg
                from core.proxy_utils import normalize_proxy_url
                old_normalized = normalize_proxy_url(old_proxy) if old_proxy else ''
                selected = normalize_proxy_url(proxy) if proxy else ''
                if not proxy_is_fresh or not selected or selected == old_normalized:
                    selected = proxy_cfg.pick_proxy(excluded_proxies={old_normalized} if old_normalized else set())
                if old_normalized and selected == old_normalized:
                    raise RuntimeError('代理池未分配不同于账号历史记录的出口，请补充可用代理')
                if not selected:
                    raise RuntimeError('持久环境初始化需要从代理池分配有效的新代理')
                # all only clears files; cloud also invalidates synchronized login state.
                client.clear_profile_state(pid, cloud=True)
                if restore:
                    client.restore_profile_fingerprint(pid, restore)
                    logger.info('[Roxy] 已恢复账号可用指纹字段（不恢复历史代理）：profile=%s', pid)
                else:
                    client.randomize_profile(pid)
                client.prepare_profile_start(pid)
                client.update_profile_proxy(pid, selected)
                snapshot = fingerprint_snapshot(client.profile_detail(pid))
                logger.info('[Roxy] 已占用关闭环境并完成初始化：profile=%s task=%s', pid, task_kind)
                return pid, lease, snapshot
            except Exception:
                lease.release()
                raise
        if time.monotonic() >= deadline:
            if target:
                raise RuntimeError(
                    f'Roxy 暂无空闲的关闭环境（等待 {timeout:g} 秒）；'
                    f'注册容量目标={target}，本任务已补建={capacity_creation_attempted}，请关闭空闲窗口或降低并发'
                )
            raise RuntimeError(f'Roxy 暂无空闲的关闭环境（等待 {timeout:g} 秒）；请关闭空闲窗口或降低并发，持久模式不新建环境')
        if time.monotonic() >= next_log:
            if target:
                logger.info(
                    '[Roxy] 等待关闭环境可用，当前环境总数=%s，注册容量目标=%s；本任务已补建=%s',
                    len(profiles), target, capacity_creation_attempted,
                )
            else:
                logger.info('[Roxy] 等待关闭环境可用，当前环境总数=%s；不创建新窗口', len(profiles))
            next_log = time.monotonic() + 15
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def save_account_snapshot(account_key: str, opened) -> None:
    if not account_key or not isinstance(getattr(opened, 'fingerprint', None), dict):
        return
    from core import db
    db.set_roxy_profile_binding(account_key, {
        'profile_id': opened.profile_id, 'workspace_id': str(cfg.ROXY_WORKSPACE_ID or ''),
        'fingerprint': opened.fingerprint, 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'status': 'reusable',
    })
