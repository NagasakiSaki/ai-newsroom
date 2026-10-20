import pytest

from newsroom.config import Settings
from newsroom.engine import Engine

BODY = '这段说明具体变化、测试条件与影响，避免空泛表述。'


def make(tmp_path, **over):
    values = dict(data_dir=tmp_path, admin_token='test', demo=False, drafts_only=True, fast_mode=True, review_mode='off')
    values.update(over)
    return Engine(Settings(**values))


def article(title='AI 智能体进入实验室', markdown=None):
    markdown = markdown or ('## 机制\n\n' + BODY * 6 + '[F1]\n\n## 影响\n\n' + BODY * 4 + '[F1]')
    return {'title': title, 'digest': '一句话摘要', 'markdown': markdown}


def context():
    return {'evidence': {'summary': '研究发布', 'facts': [
                {'id': 'F1', 'text': '研究发布', 'source_url': 'https://example.com/news', 'quote': 'quote'}],
                'uncertainties': []},
            'source_snapshot': {'tier': 'primary', 'organization': 'Example'}}


def test_polish_applies_valid_edit(tmp_path, monkeypatch):
    engine = make(tmp_path)
    run = engine.new_run('test')
    calls = []

    def complete(stage, ctx, run_, **kwargs):
        calls.append(stage)
        return {'title': '更具体的标题', 'digest': '更短的摘要',
                'markdown': '## 机制\n\n改写后的开头。' + BODY * 5 + '[F1]\n\n## 影响\n\n' + BODY * 4 + '[F1]'}

    monkeypatch.setattr(engine.model, 'complete', complete)
    polished = engine.polish_article(article(), context(), run)
    assert calls == ['polish'] and polished['title'] == '更具体的标题'
    assert any(e['stage'] == 'polish' for e in engine.store.events())


def test_polish_rejects_overlong_title_and_missing_citation(tmp_path, monkeypatch):
    engine = make(tmp_path)
    run = engine.new_run('test')
    original = article()
    long_body = '## 机制\n\n' + BODY * 6 + '[F1]'

    monkeypatch.setattr(engine.model, 'complete', lambda *a, **k: {
        'title': '标题过长' * 9, 'digest': '摘要', 'markdown': long_body})
    assert engine.polish_article(original, context(), run) == original

    monkeypatch.setattr(engine.model, 'complete', lambda *a, **k: {
        'title': '合法标题', 'digest': '摘要', 'markdown': '## 机制\n\n' + BODY * 6})
    assert engine.polish_article(original, context(), run) == original
    assert any(e['stage'] == 'polish_rejected' for e in engine.store.events())


def test_polish_rejects_heavy_shortening(tmp_path, monkeypatch):
    engine = make(tmp_path)
    run = engine.new_run('test')
    long_article = article(markdown='## 机制\n\n' + BODY * 40 + '[F1]')
    short = '## 机制\n\n' + BODY * 8 + '[F1]'
    monkeypatch.setattr(engine.model, 'complete', lambda *a, **k: {
        'title': '短标题', 'digest': '摘要', 'markdown': short})
    assert engine.polish_article(long_article, context(), run) == long_article


def test_polish_can_be_disabled_or_demo(tmp_path, monkeypatch):
    engine = make(tmp_path, polish_pass=False)
    called = []
    monkeypatch.setattr(engine.model, 'complete', lambda *a, **k: called.append(1))
    run = engine.new_run('test')
    assert engine.polish_article(article(), context(), run) == article()
    assert called == []

    demo_engine = Engine(Settings(data_dir=tmp_path / 'demo', admin_token='test', demo=True))
    assert demo_engine.polish_article(article(), context(), demo_engine.new_run('test')) == article()


def test_house_style_and_synthesis_rules_are_in_prompts():
    from newsroom.llm import TASKS
    assert '开头第一段不超过120字' in TASKS['write']
    assert 'crosscheck.sources' in TASKS['write']
    assert '套话' in TASKS['review']
    assert '终稿润色' in TASKS['polish']
