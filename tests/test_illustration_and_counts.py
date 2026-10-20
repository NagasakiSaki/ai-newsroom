import copy
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.editors import choose_editor
from newsroom.engine import Engine
from newsroom.image_service import fill_illustrations
from newsroom.models import ArticlePlan, IllustrationBrief


def make(tmp_path, **overrides):
    values = dict(data_dir=tmp_path, admin_token='test', demo=False, drafts_only=True, fast_mode=True,
                  review_mode='off', image_enabled=True, image_base_url='https://api.minimax.cn/v1/image_generation',
                  image_api_key='FAKE-IMAGE-KEY', image_model='image-01', image_size='1024x1024')
    values.update(overrides)
    return Engine(Settings(**values))


def article_draft(briefs, markdown=None):
    return {'id': 'd1', 'story_id': 's1', 'news_url': 'https://example.com/news', 'demo': False,
            'article': {'title': 'AI 智能体进入实验室', 'digest': '摘要',
                        'markdown': markdown or ('## 机制\n\n正文段落说明。' * 260)},
            'evidence': {'summary': '机构发布研究', 'facts': [
                {'id': 'F1', 'text': '研究发布', 'source_url': 'https://example.com/news', 'quote': 'quote'}]},
            'editorial_plan': {'illustrations': briefs},
            'visuals': {'assets': []}}


def briefs(count):
    return [{'purpose': f'说明第{i}个机制', 'section_hint': '机制', 'prompt': f'concept art number {i}',
             'fact_ids': ['F1'], 'category': 'concept'} for i in range(count)]


def fake_generator(monkeypatch, fail_on=None):
    calls = []

    def generate(engine, draft, brief=None):
        calls.append(brief)
        if fail_on and len(calls) == fail_on:
            return None
        index = len(calls)
        return {'id': 'G%d' % index, 'kind': 'illustration', 'generated': True, 'sha256': 'hash%d' % index,
                'brief_prompt': (brief or {}).get('prompt', ''), 'caption': (brief or {}).get('purpose', '主题插画')}

    monkeypatch.setattr('newsroom.image_service.generate_image', generate)
    return calls


def test_article_plan_accepts_illustration_briefs_and_defaults_empty():
    plan = ArticlePlan.model_validate({
        'story_mode': 'explainer', 'target_chars': 3000,
        'angle': {'selected': 'x', 'reader_value': 'y', 'alternatives': ['a', 'b'], 'outline': ['o1', 'o2'], 'reason': 'r'},
        'headline_candidates': ['标题'], 'hook': '钩子', 'narrative_beats': ['b1', 'b2'], 'reason': 'r',
        'writing_prompt': {'system_prompt': 's' * 60, 'user_prompt': 'u' * 60},
        'illustrations': [{'purpose': '解释模拟先行', 'section_hint': '机制', 'prompt': 'robot arm in a glass lab',
                           'fact_ids': ['F1'], 'category': 'concept'}]})
    assert plan.illustrations[0].purpose == '解释模拟先行'
    empty = dict(plan.model_dump())
    empty.pop('illustrations')
    assert ArticlePlan.model_validate(empty).illustrations == []


def test_fill_illustrations_uses_briefs_in_order_and_stops_at_target(tmp_path, monkeypatch):
    engine = make(tmp_path)
    draft = article_draft(briefs(5))
    calls = fake_generator(monkeypatch)
    report = fill_illustrations(engine, draft)
    # 正文约 2600 字符 -> 目标 4 张；只补缺口，按策划顺序取前 4 条。
    assert report['gap'] == 4 and report['generated'] == 4 and report['target'] == 4
    assert [b['purpose'] for b in calls] == [f'说明第{i}个机制' for i in range(4)]
    assert len({a['brief_prompt'] for a in draft['visuals']['assets']}) == 4
    again = fill_illustrations(engine, draft)
    assert again['gap'] == 0 and again['generated'] == 0 and len(calls) == 4


def test_fill_illustrations_partial_failure_keeps_successes(tmp_path, monkeypatch):
    engine = make(tmp_path)
    draft = article_draft(briefs(5))
    calls = fake_generator(monkeypatch, fail_on=2)
    report = fill_illustrations(engine, draft)
    assert report['generated'] == 3 and len(report['failures']) == 1
    assert len(calls) == 4  # 失败的一次继续尝试下一条，不重复已成功的
    assert len(draft['visuals']['assets']) == 3


def test_fill_illustrations_legacy_draft_uses_single_template(tmp_path, monkeypatch):
    engine = make(tmp_path)
    draft = article_draft([])
    calls = fake_generator(monkeypatch)
    report = fill_illustrations(engine, draft)
    assert report['legacy_single'] is True and report['generated'] == 1
    assert calls == [None]


def test_fill_illustrations_skips_when_disabled_or_target_met(tmp_path, monkeypatch):
    engine = make(tmp_path, image_enabled=False)
    draft = article_draft(briefs(3))
    calls = fake_generator(monkeypatch)
    assert fill_illustrations(engine, draft)['generated'] == 0 and calls == []
    engine = make(tmp_path / 'second')
    full = article_draft(briefs(3))
    full['visuals']['assets'] = [{'id': 'A%d' % i, 'kind': 'official_image', 'sha256': 's%d' % i} for i in range(4)]
    assert fill_illustrations(engine, full)['generated'] == 0 and calls == []


def test_run_endpoint_accepts_article_count_and_rejects_out_of_range(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test'), start_worker=False)
    headers = {'Authorization': 'Bearer test'}
    with TestClient(app) as client:
        queued = client.post('/api/run', headers=headers, json={'article_count': 3})
        assert queued.status_code == 200 and queued.json()['payload']['article_count'] == 3
        assert client.post('/api/run', headers=headers, json={'article_count': 0}).status_code == 422
        assert client.post('/api/run', headers=headers, json={'article_count': 21}).status_code == 422
        plain = client.post('/api/run', headers=headers)
        assert plain.status_code == 200 and 'article_count' not in plain.json()['payload']


def test_pipeline_honours_requested_count_and_records_timings(tmp_path, monkeypatch):
    from newsroom.collect import demo_items
    first = demo_items()[0]
    items = [{**copy.deepcopy(first), 'id': 'topic-%d' % index, 'title': 'Topic %d 新发布' % index,
              'url': 'https://example.com/topic-%d' % index} for index in range(3)]
    monkeypatch.setattr('newsroom.collect.demo_items', lambda: items)
    engine = Engine(Settings(data_dir=tmp_path, admin_token='test'))
    editor = choose_editor(engine.store, None)
    engine.enqueue('pipeline', payload={'editor': editor, 'article_count': 2})
    engine.work_once()
    assert len(engine.store.all('drafts')) == 2
    run = engine.store.all('runs')[0]
    assert run['requested_count'] == 2 and run['status'] in {'done', 'partial'}
    assert '本次目标 2 篇' in run['message']
    assert {'collect', 'write'} <= set(run['timings'])


def test_image_prefetch_preserves_plan_order_and_reuses_cache(tmp_path, monkeypatch):
    engine = Engine(Settings(data_dir=tmp_path, admin_token='test', demo=False, drafts_only=True))
    draft = {'id': 'd', 'story_id': 's', 'article': {'markdown': '## A\n\n正文'}, 'news_url': 'https://example.com/n',
             'evidence': {'facts': []}, 'visuals': {'assets': []}}
    candidates = [{'id': 'I%d' % i, 'usage': 'source_original', 'url': 'https://cdn.example/%d.png' % i,
                   'source_url': 'https://example.com/n', 'source_name': '来源', 'origin': 'official',
                   'caption': '图片 %d' % i} for i in (1, 2, 3)]
    plan = {'images': [{'image_id': 'I3'}, {'image_id': 'I1'}, {'image_id': 'I2'}]}
    calls = []

    def fetch(url, *args):
        calls.append(url)
        stream = io.BytesIO()
        Image.new('RGB', (900, 500), '#2%d5a91' % (len(calls) % 8)).save(stream, format='PNG')
        return stream.getvalue(), url

    monkeypatch.setattr('newsroom.editorial_team.fetch_image', fetch)
    assets, report = engine.download_source_images(candidates, plan, draft)
    assert [a['image_id'] for a in assets] == ['I3', 'I1', 'I2'] and report['selected_count'] == 3
    downloaded = len(calls)
    cached, _ = engine.download_source_images(candidates, plan, draft)
    assert [a['image_id'] for a in cached] == ['I3', 'I1', 'I2'] and len(calls) == downloaded


def test_illustration_job_fills_gap_and_bumps_revision(tmp_path, monkeypatch):
    from test_draft_workflow import configured
    engine, draft = configured(tmp_path)
    draft['editorial_plan'] = {'illustrations': briefs(3)}
    engine.store.put('drafts', draft)

    def fake_fill(engine_, draft_):
        from newsroom.images import save_source_image
        for index in range(2):
            raw = io.BytesIO()
            Image.new('RGB', (900, 500), ['#123456', '#654321'][index]).save(raw, format='PNG')
            asset = save_source_image(tmp_path, {'id': 'G%d' % index, 'origin': 'article',
                'source_url': draft_['news_url'], 'source_name': '主题插画',
                'url': 'https://cdn.example/g%d.png' % index, 'caption': '概念插画'},
                raw.getvalue(), 'https://cdn.example/g%d.png' % index)
            asset.update(kind='illustration', generated=True, caption='概念插画 %d' % index)
            draft_['visuals']['assets'].append(asset)
        return {'generated': 2, 'gap': 2, 'target': 4, 'failures': []}

    monkeypatch.setattr('newsroom.image_service.fill_illustrations', fake_fill)
    engine.enqueue('illustrations', draft['id'], draft['revision'])
    engine.work_once()
    current = engine.store.get('drafts', draft['id'])
    assert current['revision'] == draft['revision'] + 1
    assert len([a for a in current['visuals']['assets'] if a.get('generated')]) == 2
    run = engine.store.all('runs')[0]
    assert run['kind'] == 'illustrations' and '补齐概念插画 2 张' in (run.get('message') or '')


def test_illustration_route_requires_confirmation(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test'), start_worker=False)
    headers = {'Authorization': 'Bearer test'}
    with TestClient(app) as client:
        unconfirmed = client.post('/api/drafts/missing/illustrations', headers=headers,
                                  json={'revision': 1, 'confirmed': False})
        assert unconfirmed.status_code == 409
        assert client.post('/api/drafts/missing/illustrations', headers=headers,
                           json={'revision': 1, 'confirmed': True}).status_code == 404
