import json
import platform
from unittest.mock import Mock, patch

import pytest
from config import roxybrowser as cfg
from core import roxy_profile_pool as pool
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, 'ROXY_PERSIST_PROFILE_PER_ACCOUNT', True)
    monkeypatch.setattr(cfg, 'ROXY_WORKSPACE_ID', 'w')
    monkeypatch.setattr(cfg, 'ROXY_PROJECT_ID', '')
    monkeypatch.setattr(cfg, 'ROXY_IDLE_PROFILE_WAIT_TIMEOUT', 0)
    monkeypatch.setattr(pool, '_runtime_root', lambda: tmp_path)


def client(rows=None):
    c=Mock()
    c.list_profiles.return_value=rows if rows is not None else [dict(dirId='a',openStatus=0)]
    c.profile_detail.return_value=dict(dirId='a',os='Windows',userAgent='original',fingerInfo={'canvas':True})
    return c


def prepare(c, **kwargs):
    return pool.prepare_idle_profile(c,proxy='http://fresh.test:8080',proxy_is_fresh=True,
        account=kwargs.get('account'),account_key=kwargs.get('account_key',''),task_kind=kwargs.get('task_kind','registration'))


@pytest.mark.parametrize('status,closed',[(False,True),(0,True),('0',True),('false',True),('closed',True),(True,False),(1,False),('true',False),('opening',False),(None,False),('',False)])
def test_only_known_closed_states(status,closed):
    assert pool.profile_closed({'openStatus':status}) is closed


def test_generic_tasks_never_read_accounts_or_create():
    c=client()
    with patch('core.db.get_account_by_email') as read,patch('core.db.get_roxy_profile_binding') as binding:
        pid,lease,snapshot=prepare(c,account_key='user@test')
    try:
        assert pid=='a' and snapshot['userAgent']=='original'
        read.assert_not_called();binding.assert_not_called();c.create_profile.assert_not_called()
        c.clear_profile_state.assert_called_once_with('a',cloud=True)
        c.randomize_profile.assert_called_once_with('a')
        c.update_profile_proxy.assert_called_once_with('a','http://fresh.test:8080')
    finally:lease.release()


def test_no_closed_window_never_creates_or_closes_running():
    c=client([dict(dirId='a',openStatus=True)])
    with pytest.raises(RuntimeError,match='暂无空闲'):
        prepare(c)
    c.create_profile.assert_not_called();c.close_profile.assert_not_called()


def test_leases_exclude_concurrent_tasks_and_release():
    c=client([dict(dirId='a',openStatus=0),dict(dirId='b',openStatus=False)])
    pid,a,_=prepare(c)
    try:
        second,b,_=prepare(c)
        try:
            assert pid!=second
            with pytest.raises(RuntimeError,match='暂无空闲'):prepare(c)
        finally:b.release()
    finally:a.release()
    pid,clease,_=prepare(c);clease.release()


def test_preparation_failure_releases_and_never_opens():
    c=client();c.update_profile_proxy.side_effect=RuntimeError('update failed')
    with pytest.raises(RuntimeError,match='update failed'):prepare(c)
    lease=pool.ProfileLease.try_acquire('w','a');assert lease is not None;lease.release()
    c.open_profile.assert_not_called()


def test_rechecks_closed_state_after_reserving():
    c=client();c.list_profiles.side_effect=[[dict(dirId='a',openStatus=0)],[dict(dirId='a',openStatus=1)]]
    with pytest.raises(RuntimeError,match='暂无空闲'):prepare(c)
    c.clear_profile_state.assert_not_called()


@pytest.mark.parametrize('kind',sorted(pool.ACCOUNT_TASKS))
def test_only_allowlisted_account_tasks_restore_filtered_fingerprint(kind):
    c=client()
    account={'email':'user@test','proxy_used':'http://old.test:80', 'extra_json':json.dumps({'roxybrowser':{'profile_id':'missing','fingerprint':{'userAgent':'saved','proxyInfo':{'host':'old'},'cookie':['secret'],'fingerInfo':{'canvas':False,'startupParam':'bad'}}}})}
    with patch('core.db.get_roxy_profile_binding',return_value=None):
        pid,lease,_=prepare(c,account=account,task_kind=kind)
    try:
        c.restore_profile_fingerprint.assert_called_once_with('a',{'userAgent':'saved','fingerInfo':{'canvas':False}})
        c.randomize_profile.assert_not_called()
        c.update_profile_proxy.assert_called_once_with('a','http://fresh.test:8080')
    finally:lease.release()


def test_old_account_profile_initial_snapshot_survives_other_tasks():
    c=client();_,lease,_=prepare(c);lease.release()
    c.profile_detail.return_value={'dirId':'a','userAgent':'someone-else'}
    account={'email':'user@test','extra_json':json.dumps({'roxybrowser':{'profile_id':'a'}})}
    with patch('core.db.get_roxy_profile_binding',return_value=None),patch('core.fingerprint_profile.load_browser_profile',return_value=None):
        _,lease,_=prepare(c,account=account,task_kind='live_check')
    try:assert c.restore_profile_fingerprint.call_args.args[1]['userAgent']=='original'
    finally:lease.release()


def test_old_proxy_is_replaced_even_if_passed_in():
    c=client();account={'email':'u@test','proxy_used':'http://fresh.test:8080'}
    with patch('core.db.get_roxy_profile_binding',return_value=None),patch('core.fingerprint_profile.load_browser_profile',return_value=None),patch('config.proxy.pick_proxy',return_value='http://new.test:8080') as pick:
        _,lease,_=prepare(c,account=account,task_kind='live_check')
    try:
        pick.assert_called_once_with(excluded_proxies={'http://fresh.test:8080'})
        c.update_profile_proxy.assert_called_once_with('a','http://new.test:8080')
    finally:lease.release()


def test_persistent_open_cannot_fall_back_to_create():
    c=RoxyBrowserClient()
    with patch.object(c,'request') as request,pytest.raises(RuntimeError,match='不创建'):
        c.create_profile()
    request.assert_not_called()


def test_client_list_pagination_filters_project_and_detail_rows(monkeypatch):
    monkeypatch.setattr(cfg,'ROXY_PROJECT_ID','p')
    c=RoxyBrowserClient()
    with patch.object(c,'request',side_effect=[{'data':{'total':2,'rows':[{'dirId':'a','projectId':'other','openStatus':0}]}},{'data':{'total':2,'rows':[{'dirId':'b','projectId':'p','openStatus':0}]}}]) as req:
        assert [x['dirId'] for x in c.list_profiles()]==['b']
        assert req.call_args.kwargs['params']['page_index']==2
    with patch.object(c,'request',return_value={'data':{'rows':[{'dirId':'b','userAgent':'ua'}],'total':1}}):
        assert c.profile_detail('b')['userAgent']=='ua'


def test_open_uses_non_force_and_cleanup_releases_even_when_mode_changes(monkeypatch):
    c=RoxyBrowserClient();lease=Mock()
    with patch('core.roxy_profile_pool.prepare_idle_profile',return_value=('a',lease,{'userAgent':'saved'})),patch.object(c,'request',return_value={'data':{'debuggerAddress':'127.0.0.1:9000'}}) as req:
        opened=c.open_profile(account_key='user@test',task_kind='codex_retry')
        assert req.call_args.kwargs['json_body']['forceOpen'] is False
        assert opened.created_by_run is False and opened.lease is lease
    monkeypatch.setattr(cfg,'ROXY_PERSIST_PROFILE_PER_ACCOUNT',False)
    with patch.object(c,'close_profile') as close,patch.object(c,'delete_profile') as delete:
        c.cleanup_profile(opened,force=True)
    close.assert_called_once_with('a');delete.assert_not_called();lease.release.assert_called_once()


@pytest.mark.parametrize('system,arch',[('Darwin','x86_64'),('Darwin','arm64'),('Linux','x86_64'),('Linux','aarch64'),('Windows','AMD64'),('Windows','ARM64')])
def test_lease_lock_platform_independent(monkeypatch,system,arch):
    monkeypatch.setattr(platform,'system',lambda:system);monkeypatch.setattr(platform,'machine',lambda:arch)
    first=pool.ProfileLease.try_acquire('w','a')
    assert pool.ProfileLease.try_acquire('w','a') is None
    first.release()
    second=pool.ProfileLease.try_acquire('w','a');assert second;second.release()


def test_lease_excludes_other_processes_and_recovers_after_process_exit(tmp_path):
    import subprocess,sys
    first=pool.ProfileLease.try_acquire('w','a')
    script="import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute('BEGIN IMMEDIATE')"
    lock_path=str(first.path.with_suffix('.sqlite3'))
    result=subprocess.run([sys.executable,'-c',script,lock_path],capture_output=True)
    assert result.returncode!=0 and b'locked' in result.stderr
    first.release()
    assert subprocess.run([sys.executable,'-c',script,lock_path],capture_output=True).returncode==0
    next_lease=pool.ProfileLease.try_acquire('w','a');assert next_lease;next_lease.release()


def test_liveness_persistent_mode_skips_saved_registration_proxy(monkeypatch):
    from core.live_check_service import _live_check_routes
    from config import proxy,live_check
    monkeypatch.setattr(proxy,'PROXY_MODE','api')
    monkeypatch.setattr(live_check,'LIVE_CHECK_USE_REGISTRATION_PROXY',True)
    monkeypatch.setattr(live_check,'LIVE_CHECK_PROXY_API_REGION','account')
    with patch('core.live_check_service.fetch_available_proxy_api',return_value=['http://new:80']) as fetch:
        routes=_live_check_routes({'email':'u@test','proxy_used':'http://old:80','proxy_country_code':'JP'},explicit_proxy='http://old:80')
    assert [r['proxy'] for r in routes]==['http://new:80']
    assert fetch.call_args.args==('JP',)


def test_plan_persistent_mode_reallocates_proxy_instead_of_restoring_old():
    from core.plan_check_service import _check_plan_with_account_context
    with patch('config.proxy.pick_proxy',return_value='http://new:80') as pick,patch('core.plan_check_service.check_account_plan',return_value={'ok':True}) as check:
        _check_plan_with_account_context({'email':'u@test','proxy_used':'http://old:80'},'token',proxy=None,timezone_offset_min='0',check_oaics=True)
    pick.assert_called_once();assert check.call_args.kwargs['proxy']=='http://new:80'
    assert check.call_args.kwargs['preserve_proxy_session'] is False


def test_live_browser_passes_account_only_for_liveness(monkeypatch):
    from core.browser_liveness import _open_roxy
    opened=RoxyOpenResult('a',{},lease=Mock())
    c=Mock();c.open_profile.return_value=opened
    with patch('core.roxybrowser_client.RoxyBrowserClient',return_value=c),patch('core.roxy_registration._build_driver',return_value=Mock()):
        _,_,close=_open_roxy('http://fresh:80',True,account={'email':'u@test'},task_kind='live_check')
        close()
    assert c.open_profile.call_args.kwargs['account']=={'email':'u@test'}
    assert c.open_profile.call_args.kwargs['task_kind']=='live_check'
    c.cleanup_profile.assert_called_once()
