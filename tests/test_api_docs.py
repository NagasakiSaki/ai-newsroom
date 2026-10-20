import json

from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from scripts.export_api_docs import export


def test_live_schema_matches_authentication_and_requests(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test'), start_worker=False)
    with TestClient(app) as client:
        schema = client.get('/openapi.json').json()
        assert schema['paths']['/api/state']['get']['security'] == [{'AdminBearer': []}]
        assert schema['paths']['/api/health']['get']['security'] == []
        assert client.get('/api/state').status_code == 401
        assert client.get('/api/state', headers={'Authorization': 'Bearer test'}).status_code == 200
        fields = schema['components']['schemas']['ComposeRequest']['properties']
        assert {'editor_id', 'custom_style', 'story_id', 'draft_id', 'revision'} <= fields.keys()
        assert 'text/html' in schema['paths']['/']['get']['responses']['200']['content']
        assert 'application/zip' in schema['paths']['/api/drafts/{draft_id}/export/{format}']['get']['responses']['200']['content']
        # 旧自动发表队列接口仍然不存在；正式发布走受保护的发布状态机接口。
        assert not any('/queue' in path for path in schema['paths'])
        assert schema['paths']['/api/drafts/{draft_id}/publish']['post']['security'] == [{'AdminBearer': []}]
        assert schema['paths']['/api/publication']['put']['security'] == [{'AdminBearer': []}]
        assert schema['paths']['/api/publication/capability']['post']['security'] == [{'AdminBearer': []}]
        assert client.post('/api/drafts/x/publish', json={'revision': 1, 'confirmed': True}).status_code == 401
        assert client.post('/api/publications/x/retry', json={'confirmed': True}).status_code == 401
        action = next(p for p in schema['paths']['/api/publications/{publication_id}/{action}']['post']['parameters']
                      if p['name'] == 'action')
        assert 'submit' in action['schema']['enum']


def test_export_is_deterministic_offline_and_detects_stale_docs(tmp_path, monkeypatch):
    # 导出不能加载用户配置或访问生产服务。
    monkeypatch.setattr(Settings, 'load', lambda: (_ for _ in ()).throw(AssertionError('must not load .env')))
    assert not export(tmp_path)
    assert not export(tmp_path, check=True)
    schema = json.loads((tmp_path / 'schemas/domain-models.json').read_text(encoding='utf-8'))
    assert 'WritingPrompt' in schema['x-roots'] and 'Crosscheck' in schema['x-roots']
    (tmp_path / 'openapi.json').write_text('{}', encoding='utf-8')
    assert export(tmp_path, check=True) == ['openapi.json']
