from unittest.mock import Mock, patch
import pytest
from core.proxy_http_compat import compatible_get, close_diagnostic_session, safe_transport_error, _TunnelAdapter, _TunnelPool
from core.proxy_test import choose_healthy_proxy
from config import proxy as cfg

class TransportFailure(Exception):
    code = 56


def test_http_tunnel_fallback_preserves_proxy_tls_and_reuses_session():
    primary=Mock();primary.proxies={'http':'http://user:secret@proxy.test:80','https':'http://user:secret@proxy.test:80'}
    primary.get.side_effect=TransportFailure('CONNECT aborted')
    fallback=Mock();fallback.get.return_value=Mock(status_code=200)
    with patch('core.proxy_http_compat.requests.Session',return_value=fallback):
        assert compatible_get(primary,'https://example.test',timeout=2).status_code==200
        compatible_get(primary,'https://other.test',timeout=3)
    assert fallback.trust_env is False
    assert fallback.proxies==primary.proxies
    assert fallback.get.call_args.args == ('https://other.test',)
    assert 0 < fallback.get.call_args.kwargs['timeout'] <= 3
    primary.get.assert_called_once()
    close_diagnostic_session(primary)
    fallback.close.assert_called_once();primary.close.assert_called_once()


@pytest.mark.parametrize('proxy,code',[('socks5h://proxy.test:80',56),('https://proxy.test:80',56),('http://proxy.test:80',67),('http://proxy.test:80',60)])
def test_no_auth_certificate_or_other_protocol_fallback(proxy,code):
    primary=Mock();primary.proxies={'https':proxy}
    err=TransportFailure();err.code=code;primary.get.side_effect=err
    with patch('core.proxy_http_compat.requests.Session') as fallback,pytest.raises(TransportFailure):
        compatible_get(primary,'https://example.test',timeout=2)
    fallback.assert_not_called()


def test_http_responses_are_not_retried_or_accepted_as_success():
    primary=Mock();primary.proxies={'https':'http://proxy:80'};primary.get.return_value=Mock(status_code=403)
    with patch('core.proxy_http_compat.requests.Session') as fallback:
        assert compatible_get(primary,'https://example.test').status_code==403
    fallback.assert_not_called()


def test_proxy_manager_override_is_local_to_adapter():
    from urllib3.poolmanager import pool_classes_by_scheme
    before=dict(pool_classes_by_scheme)
    adapter=_TunnelAdapter()
    manager=adapter.proxy_manager_for('http://example.test:80')
    assert manager.pool_classes_by_scheme['https'] is _TunnelPool
    assert pool_classes_by_scheme==before
    adapter.close()


def test_transport_logs_never_include_credentials_or_urls():
    message=safe_transport_error(TransportFailure('CONNECT aborted http://user:secret@proxy.test:80?token=private'))
    assert 'CONNECT' in message
    assert 'secret' not in message and 'private' not in message and 'proxy.test' not in message
    with patch('core.proxy_test.test_proxy_health',side_effect=TransportFailure('CONNECT aborted secret')):
        selected=choose_healthy_proxy(['http://proxy.test:80'])
    assert selected['ok'] is False and 'CONNECT' in selected['checked'][0]['reason']
    assert 'secret' not in selected['checked'][0]['reason']


def test_pool_selection_excludes_previous_account_proxy(monkeypatch):
    monkeypatch.setattr(cfg,'PROXY_MODE','pool');monkeypatch.setattr(cfg,'PROXY_POOL',['http://old.test:80','http://new.test:80'])
    assert cfg.pick_proxy(excluded_proxies={'http://old.test:80'})=='http://new.test:80'
    with pytest.raises(RuntimeError):cfg.pick_proxy(excluded_proxies=set(cfg.PROXY_POOL))


def test_source_test_reports_extraction_and_exit_separately():
    from webui.app import create_app
    app=create_app(auth_code='test');client=app.test_client();client.environ_base['HTTP_X_AUTH_CODE']='test'
    data={'entry':dict(id='b2',name='b2',provider='b2proxy',enabled=True,url='https://api.test?zone=custom&ptype=1'), 'region':'Rand'}
    with patch('core.live_check_proxy.fetch_proxy_api',return_value=['http://user:secret@proxy.test:80']),patch('core.proxy_test.test_proxy',side_effect=TransportFailure('CONNECT aborted secret')):
        response=client.post('/api/proxy/source-test',json=data)
    result=response.get_json()
    assert response.status_code==200 and result['extracted'] is True and result['exit_ok'] is False
    assert 'secret' not in response.text and 'CONNECT' in result['message']
    with patch('core.live_check_proxy.fetch_proxy_api',return_value=['http://proxy.test:80']),patch('core.proxy_test.test_proxy',return_value={'country_code':'US'}):
        assert client.post('/api/proxy/source-test',json=data).get_json()['exit_ok'] is True
