import copy
import json
import socket

import pytest
from bs4 import BeautifulSoup

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.engine import Engine
from newsroom.model_contracts import compact_context
from newsroom.models import VisualPlan
from newsroom.render import render_article
from tests.test_crosscheck import fixtures
from tests.test_run_diagnostics import fake_completion


def draft():
    return {'demo': False, 'source': '原始来源', 'news_url': 'https://example.org/release',
            'article': {'title': '具体变化', 'digest': '摘要', 'markdown':
                '导语交代这个具体问题，==关键变化在这里==。[F1]\n\n## 为什么会这样\n\n' +
                '这是第二段原理解释，**用黑体突出术语**。\n\n这是第三段结果说明，带来一个值得讨论的新变化。\n\n末尾提出一个具体问题。'}}


def test_orange_emphasis_keeps_black_bold_and_never_evaluates_html():
    d = draft(); d['article']['markdown'] += '\n\n==<script>alert(1)</script>== <img src=x onerror=alert(2)>'
    soup = BeautifulSoup(render_article(d), 'html.parser')
    assert soup.find('span', string='关键变化在这里')['style'] == 'color:#f15a24;font-weight:700;'
    assert soup.find('strong', string='用黑体突出术语')['style'].endswith('color:#111;')
    assert not soup.find('script') and not soup.find(attrs={'onerror': True})
    assert '[F1]' not in soup.get_text()
    assert 'text-align:center' in soup.find('h2')['style']


def test_precise_image_anchor_takes_precedence_and_unanchored_images_spread():
    d = draft()
    d['visuals'] = {'assets': [{'id': str(i), 'file': str(i)+'.png', 'title': '真实图片', 'credit': '出处',
                             'after_text': '这是第三段结果说明' if i == 0 else '', 'after_heading': ''} for i in range(3)]}
    soup = BeautifulSoup(render_article(d), 'html.parser')
    figures = soup.find_all('figure')
    assert figures[0].find('img')['src'].endswith('1.png')  # fallback image goes earlier in the article
    original = next(f for f in figures if f.find('img')['src'].endswith('0.png'))
    assert original.find_previous_sibling('p').get_text().startswith('这是第三段结果说明')
    assert len({id(f.find_previous_sibling('p')) for f in figures}) >= 2


def test_writer_context_omits_internal_audit_but_keeps_real_conditions():
    _, _, evidence, _ = fixtures()
    original = {'evidence': copy.deepcopy(evidence), 'source_snapshot': {'warnings': ['internal doubt'], 'tier': 'primary'},
                'crosscheck': {'state': 'primary_announcement', 'summary': 'internal audit', 'warnings': ['no independent test'], 'sources': []}}
    slim = compact_context('plan_article', original)
    assert 'uncertainties' not in slim['evidence']
    assert slim['evidence']['facts'] == evidence['facts'] and original['evidence']['uncertainties']
    assert 'warnings' not in slim['source_snapshot'] and 'summary' not in slim['crosscheck']
    assert compact_context('review', original)['evidence']['uncertainties']


def test_fast_mode_bounds_crosscheck_attempts_without_passing_bad_quotes(tmp_path, monkeypatch):
    e = Engine(Settings(data_dir=tmp_path, demo=False, fast_mode=True, llm_base_url='https://model.example', llm_api_key='fake', llm_model='fake'))
    a, b, evidence, output = fixtures()
    output['sources'][0]['comparisons'][0]['quote'] = 'Not actually in original source'
    calls = fake_completion(monkeypatch, [output])
    with pytest.raises(ValueError):
        e.model.complete('crosscheck', {}, e.new_run('test'), validator=lambda p: e.validate_crosscheck(a, evidence, [b], p))
    assert len(calls) == 1 and calls[0]['_deadline'] == 25


def test_fast_mode_batches_candidates_in_one_model_stage(tmp_path, monkeypatch):
    from newsroom.collect import demo_items
    e = Engine(Settings(data_dir=tmp_path, fast_mode=True, editorial_mode='auto', review_mode='off'))
    items = [dict(demo_items()[0], id='demo-'+str(i), title='虚构新闻'+str(i), url='https://example.com/demo-'+str(i)) for i in range(2)]
    monkeypatch.setattr('newsroom.collect.demo_items', lambda: items)
    e.enqueue('pipeline'); e.work_once()
    calls = e.store.all('calls')
    assert sum(c['stage'] == 'score_batch' for c in calls) == 1
    assert not any(c['stage'] == 'score' for c in calls)
    assert e.store.all('drafts')


def test_fast_mode_is_configurable_and_image_placement_accepts_ten_images(tmp_path):
    from newsroom.runtime_config import RuntimeConfigPatch, candidate, persist, load_saved, public_config
    e = Engine(Settings(data_dir=tmp_path, admin_token='fake'))
    e.settings = candidate(e.settings, RuntimeConfigPatch(fast_mode=True))
    persist(e.settings)
    assert load_saved(tmp_path)['fast_mode'] is True
    assert public_config(e.settings, e.store)['fast_mode'] is True
    plan = VisualPlan.model_validate({'images': [{'image_id': 'I'+str(i), 'after_text': '原文段落连续内容'} for i in range(10)]})
    assert len(plan.images) == 10
    with pytest.raises(ValueError):
        VisualPlan.model_validate({'images': [{'image_id': 'I'+str(i)} for i in range(11)]})


def test_image_ipv6_fake_ip_is_checked_via_https_dns(monkeypatch):
    from newsroom.images import image_url
    checks = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('198.18.0.1', 443)), (23, 1, 6, '', ('2001:2::75', 443, 0, 0))])
    monkeypatch.setattr('newsroom.images.verify_public_dns', lambda host, **kw: checks.append(host))
    assert image_url('https://cdn.example.com/figure.png') and checks == ['cdn.example.com']


@pytest.mark.parametrize('base,should_adapt', [('https://api.xiaomimimo.com/v1', True), ('https://gateway.example/v1', False)])
def test_only_official_mimo_fast_requests_disable_reasoning_and_force_json(tmp_path, monkeypatch, base, should_adapt):
    from newsroom.llm import ModelClient
    e = Engine(Settings(data_dir=tmp_path, demo=False, fast_mode=True, json_mode=False,
                        llm_base_url=base, llm_api_key='fake', llm_model='mimo-v2.6-pro'))
    calls = fake_completion(monkeypatch, [ModelClient.demo('score', {})])
    e.model.complete('score', {}, e.new_run('test'))
    payload = calls[0]['json']
    assert ('thinking' in payload) == should_adapt
    assert ('response_format' in payload) == should_adapt
    if should_adapt:
        assert payload['thinking'] == {'type': 'disabled'}


def test_whole_article_length_budget_is_last_writer_instruction(tmp_path, monkeypatch):
    from newsroom.llm import ModelClient
    e = Engine(Settings(data_dir=tmp_path, demo=False, fast_mode=True, llm_base_url='https://model.example', llm_api_key='fake', llm_model='fake'))
    plan = ModelClient.demo('plan_article', {'editor': {}})
    plan['target_chars'] = 4200
    output = ModelClient.demo('write', {})
    calls = fake_completion(monkeypatch, [output])
    e.model.complete('write', {'autonomous_article': True, 'editorial_plan': plan, 'writing_prompt': plan['writing_prompt']}, e.new_run('test'))
    instruction = calls[0]['json']['messages'][-1]['content']
    assert '4200' in instruction and '47个有信息的短段落' in instruction
    assert '单一信源' in instruction and '正文不得写' in instruction
    assert '七成' not in instruction and '三人' not in instruction
    assert calls[0]['_deadline'] == 300


def test_hand_selected_fast_story_skips_importance_scoring(tmp_path):
    from newsroom.collect import demo_items
    e = Engine(Settings(data_dir=tmp_path, fast_mode=True, editorial_mode='auto', review_mode='off'))
    item = demo_items()[0]; e.store.put('stories', item)
    e.enqueue('pipeline', payload={'story_id': item['id']}); e.work_once()
    assert len(e.store.all('drafts')) == 1
    assert not any(c['stage'] in {'score', 'score_batch'} for c in e.store.all('calls'))
    assert e.store.all('drafts')[0]['score'] is None


def test_optional_planner_checklists_use_house_rules_without_model_retry():
    from newsroom.llm import ModelClient
    from newsroom.models import ArticlePlan
    plan = ModelClient.demo('plan_article', {'editor': {}})
    plan['writing_prompt'].pop('style_checklist')
    plan['writing_prompt'].pop('evidence_rules')
    result = ArticlePlan.model_validate(plan)
    assert len(result.writing_prompt.style_checklist) == 2
    assert len(result.writing_prompt.evidence_rules) == 2
