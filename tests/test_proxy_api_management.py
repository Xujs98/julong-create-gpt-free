import json
import platform
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from config import proxy as cfg
from config.proxy_api import build_request_url, detect_provider, load_api_entries, validate_api_entries
from core.live_check_proxy import fetch_available_proxy_api, fetch_proxy_api
from core.live_check_service import _live_check_routes
from webui import config_editor

B2 = r'https://b2.example/gen?zone=custom&ptype=1&count=1&proto=http&stype=txt&sessType=rotating&split=\r\n'
CLIP = 'https://clip.example/api?region=Rand&num=1&time=10&type=json&token=secret'


def entry(url=B2, enabled=True, identity='b2'):
    return dict(id=identity, name=identity, url=url, provider=detect_provider(url), enabled=enabled)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for key, value in dict(PROXY_API_SOURCES_JSON='', PROXY_API_URL=CLIP,
        PROXY_MODE='api', PROXY_API_REGION='Rand', PROXY_API_NUM=1, PROXY_API_TYPE='txt',
        PROXY_API_FORMAT='rn', PROXY_API_TIME=5, PROXY_API_SESSION_TYPE='rotating',
        PROXY_API_MAX_ATTEMPTS=3, PROXY_HEALTH_CHECK_BEFORE_REGISTRATION=False).items():
        monkeypatch.setattr(cfg, key, value)


def test_migrate_legacy_only_when_registry_absent():
    assert load_api_entries('', CLIP)[0]['url'] == CLIP
    assert load_api_entries('', CLIP)[0]['enabled'] is True
    assert load_api_entries('[]', CLIP) == []
    with pytest.raises(ValueError):
        load_api_entries('invalid', CLIP)


@pytest.mark.parametrize('bad', [None, {}, 'no-json', [None], [entry(enabled='true')],
    [entry(), entry()], [entry(identity='')], [entry(url='javascript:alert(1)')],
    [entry(url='file:///tmp/test')], [entry(url='https://example:0')],
    [entry(url='https://example:bad')], [entry(url='https://example/\nabc')],
    [dict(entry(), provider='unknown')], [dict(entry(), name='')]])
def test_reject_bad_registry(bad):
    with pytest.raises(ValueError):
        validate_api_entries(bad)


@pytest.mark.parametrize('region', ['Rand','US','JP'])
@pytest.mark.parametrize('session', ['sticky','rotating'])
def test_b2_parameters_without_cliproxy_parameters(monkeypatch, region, session):
    monkeypatch.setattr(cfg, 'PROXY_API_SESSION_TYPE', session)
    q = parse_qs(urlsplit(cfg.build_proxy_api_request_url(region, entry=entry())).query)
    assert q == dict(zone=['custom'], ptype=['1'], proto=['http'], count=['1'],
                    stype=['txt'], split=[r'\r\n'], sessType=[session],
                    **({} if region == 'Rand' else dict(region=[region])))
    assert cfg.PROXY_API_REGION == 'Rand'


def test_manual_provider_with_custom_domain_and_parameters():
    e = dict(entry('https://custom.example/gen?key=secret'), provider='b2proxy')
    url = cfg.build_proxy_api_request_url('Rand', entry=e)
    assert detect_provider(url) == 'b2proxy'
    assert parse_qs(urlsplit(url).query)['key'] == ['secret']


def test_random_selection_each_retry_with_one_budget(monkeypatch):
    selected = [entry(), entry(CLIP, identity='clip')]
    monkeypatch.setattr(cfg, 'PROXY_API_SOURCES_JSON', json.dumps(selected + [entry('https://off.example', False, 'off')]))
    with patch('config.proxy.random.choice', side_effect=[selected[0], selected[1], selected[0]]) as choose, patch(
        'core.live_check_proxy.fetch_proxy_api', side_effect=[TimeoutError('private'), [], ['http://ok.example:8080']]
    ) as fetch:
        assert cfg.pick_proxy() == 'http://ok.example:8080'
    assert fetch.call_count == choose.call_count == 3
    assert all(call.args[0] == selected for call in choose.call_args_list)
    assert [urlsplit(call.kwargs['api_url']).hostname for call in fetch.call_args_list] == ['b2.example', 'clip.example', 'b2.example']


@pytest.mark.parametrize('sources', [[], [entry(enabled=False)]])
def test_unselected_sources_fail_without_request_or_legacy_fallback(monkeypatch, sources):
    monkeypatch.setattr(cfg, 'PROXY_API_SOURCES_JSON', json.dumps(sources))
    with patch('core.live_check_proxy.fetch_proxy_api') as fetch, pytest.raises(ValueError, match='至少选中'):
        cfg.pick_proxy()
    fetch.assert_not_called()


def test_explicit_fetch_url_overrides_registry(monkeypatch):
    monkeypatch.setattr(cfg, 'PROXY_API_SOURCES_JSON', '[]')
    with patch('core.live_check_proxy.fetch_proxy_api', return_value=['http://ok:8080']) as fetch:
        assert fetch_available_proxy_api('US', api_url=CLIP) == ['http://ok:8080']
    assert fetch.call_args.kwargs['api_url'] == CLIP


@pytest.mark.parametrize('body,sep', [
    ({'code':200, 'data':[{'ip':'proxy.test', 'port':8080}]}, r'\r\n'),
    ('proxy.test:8080\r\nproxy2.test:8081', r'\r\n'),
    ('proxy.test:8080\tproxy2.test:8081', r'\t'),
    ('proxy.test:8080|proxy2.test:8081', '|'),
])
def test_b2_formats_and_separators_without_protocol_probe(body, sep):
    response = Mock()
    if isinstance(body, dict): response.json.return_value = body
    else:
        response.json.side_effect = ValueError
        response.text = body
    from urllib.parse import urlencode
    url = 'https://b2.example/gen?' + urlencode(dict(zone='custom', ptype=1, proto='http', split=sep))
    with patch('core.live_check_proxy.requests.get', return_value=response) as get, patch('core.proxy_utils.detect_proxy_scheme') as probe:
        proxies = fetch_proxy_api('Rand', api_url=url)
    assert proxies[0] == 'http://proxy.test:8080'
    assert len(proxies) == (1 if isinstance(body, dict) else 2)
    assert 'region' not in parse_qs(urlsplit(get.call_args.args[0]).query)
    probe.assert_not_called()


def test_b2_business_error_does_not_treat_error_data_as_proxy():
    response = Mock()
    response.json.return_value = {'code':403, 'msg':'secret-token', 'data':[{'ip':'example', 'port':80}]}
    with patch('core.live_check_proxy.requests.get', return_value=response), pytest.raises(ValueError, match='业务错误') as error:
        fetch_proxy_api('US', api_url=B2)
    assert 'secret-token' not in str(error.value)


def test_liveness_uses_selected_b2_and_only_overrides_region(monkeypatch):
    from config import live_check
    monkeypatch.setattr(live_check, 'LIVE_CHECK_USE_REGISTRATION_PROXY', False)
    monkeypatch.setattr(live_check, 'LIVE_CHECK_PROXY_API_REGION', 'account')
    monkeypatch.setattr(cfg, 'PROXY_API_SOURCES_JSON', json.dumps([entry()]))
    monkeypatch.setattr(cfg, 'PROXY_API_REGION', 'US')
    with patch('core.live_check_proxy.fetch_proxy_api', return_value=['http://ok:8080']) as fetch:
        assert _live_check_routes({'proxy_country_code':'JP'})[0]['proxy'] == 'http://ok:8080'
    assert parse_qs(urlsplit(fetch.call_args.kwargs['api_url']).query)['region'] == ['JP']
    assert cfg.PROXY_API_REGION == 'US'


@pytest.mark.parametrize('system,machine', [('Darwin','x86_64'),('Darwin','arm64'),('Linux','x86_64'),('Linux','aarch64'),('Windows','AMD64'),('Windows','ARM64')])
def test_registry_env_round_trip_across_platforms(monkeypatch, tmp_path, system, machine):
    from config import env_loader
    monkeypatch.setattr(platform, 'system', lambda: system)
    monkeypatch.setattr(platform, 'machine', lambda: machine)
    monkeypatch.setattr(env_loader, '_ENV_PATH', tmp_path / '.env')
    monkeypatch.setenv('PROXY_API_SOURCES_JSON', '')
    entries = [dict(entry(), name='b2 中文'), entry(CLIP, False, 'clip')]
    config_editor.update_config({'PROXY_API_SOURCES_JSON':json.dumps(entries)})
    raw = env_loader.read_env_file()['PROXY_API_SOURCES_JSON']
    assert validate_api_entries(raw) == entries
    namespace = {'PROXY_API_SOURCES_JSON':''}
    env_loader.apply_env_overrides(namespace, {'PROXY_API_SOURCES_JSON':'str'})
    assert validate_api_entries(namespace['PROXY_API_SOURCES_JSON']) == entries
    field = next(f for f in config_editor.get_config() if f['key'] == 'PROXY_API_SOURCES_JSON')
    assert validate_api_entries(field['value']) == entries


def test_invalid_registry_rejected_before_any_write():
    with patch('config.env_loader.write_env_values') as write, pytest.raises(ValueError):
        config_editor.update_config({'PROXY_API_SOURCES_JSON':'[bad]'})
    write.assert_not_called()
