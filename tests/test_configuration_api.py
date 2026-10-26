import stat

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from app.sources import load_sources
from app.policies import load_default_policy


@pytest.fixture
def configured(tmp_path):
    sources = tmp_path / 'sources.yaml'
    sources.write_text('sources: []\n')
    policy = tmp_path / 'policy.yaml'
    policy.write_text('profiles: {}\n')
    settings = Settings(database_url='postgresql://unused', api_key='secret',
                        sources_path=str(sources), policy_path=str(policy))
    return TestClient(create_app(settings)), sources, policy


@pytest.mark.parametrize('url', ['/api/v1/sources', '/api/v1/sources?raz=true', '/api/v1/policy'])
def test_authentication(configured, url):
    client, sources, policy = configured
    before = sources.read_bytes(), policy.read_bytes()
    for headers in ({}, {'X-API-Key': 'wrong'}):
        assert client.post(url, headers=headers).status_code == 401
    assert before == (sources.read_bytes(), policy.read_bytes())


def test_sources_upload_and_reset(configured):
    client, sources, _ = configured
    headers = {'X-API-Key': 'secret', 'Content-Type': 'application/yaml'}
    body = 'sources:\n  - id: prod\n    db_uri: postgresql://user:password@host/db\n'
    response = client.post('/api/v1/sources', content=body, headers=headers)
    assert response.json() == {'status': 'updated'}
    assert 'password' not in response.text
    assert load_sources(str(sources))['prod']['db_uri'].endswith('@host/db')
    assert stat.S_IMODE(sources.stat().st_mode) == 0o600
    for invalid in ('', 'sources: [', 'sources: [null]', 'sources: [{id: a}, {id: a}]', 'defaults: []'):
        response = client.post('/api/v1/sources', content=invalid, headers=headers)
        assert response.status_code == 422
        assert sources.read_text() == body
    assert client.post('/api/v1/sources?raz=true', content=body, headers=headers).status_code == 422
    assert client.post('/api/v1/sources?raz=true', headers=headers).json() == {'status': 'reset'}
    assert load_sources(str(sources)) == {}


def test_policy_upload(configured):
    client, _, policy = configured
    headers = {'X-API-Key': 'secret'}
    response = client.post('/api/v1/policy', json={'mode': 'execute', 'profiles': {'index_advisor': {'enabled': True}}}, headers=headers)
    assert response.status_code == 200
    loaded = load_default_policy(str(policy))
    assert loaded.mode == 'execute'
    assert loaded.policy.rules
    before = policy.read_bytes()
    for invalid in ('{"mode": "secret-invalid"}', '{', '[]', '{"unknown": true}'):
        response = client.post('/api/v1/policy', content=invalid, headers=headers)
        assert response.status_code == 422
        assert 'secret-invalid' not in response.text
        assert policy.read_bytes() == before


def test_unwritable_destination(configured):
    client, sources, _ = configured
    sources.unlink()
    sources.mkdir()
    assert client.post('/api/v1/sources?raz=true', headers={'X-API-Key': 'secret'}).status_code == 503
    assert list(sources.parent.glob('tmp*')) == []
