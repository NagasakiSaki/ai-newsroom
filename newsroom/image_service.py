"""Optional native MiniMax/compatible image adapters; cached, one attempt, no paid retries."""
import base64
import hashlib
import json
import time
import uuid
from dataclasses import replace

import httpx

from .db import now
from .images import fetch_image, save_source_image
from .network import options
from .image_protocol import image_request, image_result


SAFETY = ('Conceptual editorial illustration for a modern Chinese technology feature. '
          'No text, logos, charts, numbers, watermarks, identifiable real people, or depiction of a real reported event as a photograph; '
          'no fake product screenshots, benchmark charts or test scenes. '
          'Strong central subject, cinematic lighting, clean composition, usable as a wide cover crop.')


def illustration_prompt(draft, brief=None):
    """策划给出的绘图提示词优先；没有策划时退回标题＋摘要模板。"""
    title = draft['article']['title']
    if brief and brief.get('prompt'):
        return (SAFETY + '\nSubject: ' + brief['prompt']
                + '\nTheme (for atmosphere only, do not render as text): ' + title)
    return (SAFETY + '\nTopic: ' + title + '\nBackground: ' + draft['evidence']['summary'])


def generate_image(engine, draft, brief=None):
    settings = engine.settings
    if not settings.image_enabled or settings.demo or draft.get('demo'):
        return None
    prompt = illustration_prompt(draft, brief)
    purpose = (brief or {}).get('purpose') or '主题配图'
    try:
        provider, endpoint, payload = image_request(settings, prompt)
    except ValueError as exc:
        engine.store.event(draft['id'], 'image_configuration', str(exc))
        return None
    # Protocol/endpoint changes bypass old 404 cooldowns without deleting historical failure evidence.
    key = hashlib.sha256(json.dumps(['image-adapter-v2', provider, endpoint, payload], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    credential = hashlib.sha256(settings.image_api_key.encode()).hexdigest()
    from .source_quality import as_date
    from datetime import datetime, timezone, timedelta
    try:
        cached = engine.store.get('generated_images', key)
        failed_at = as_date(cached.get('failed_at'))
        if failed_at and cached.get('credential') == credential and datetime.now(timezone.utc) - failed_at < timedelta(hours=6):
            return None
        asset = cached['asset']
        from .editorial_team import asset_file
        asset_file(settings.data_dir, asset)
        return asset
    except (KeyError, ValueError):
        pass
    engine.controls.checkpoint()
    row = {'id': uuid.uuid4().hex, 'run_id': draft.get('run_id'), 'draft_id': draft['id'],
           'at': now(), 'model': settings.image_model, 'provider': provider, 'endpoint': endpoint,
           'outcome': 'requesting', 'usage': {}}
    engine.store.put('image_calls', row)
    started = time.monotonic()
    try:
        route = replace(settings, visual_proxy_url=settings.image_proxy_url or settings.visual_proxy_url)
        with httpx.Client(timeout=httpx.Timeout(180, connect=15), **options(route, 'visual')) as client:
            with client.stream('POST', endpoint,
                    headers={'Authorization': 'Bearer ' + settings.image_api_key},
                    json=payload) as response:
                row['http_status'] = response.status_code
                if response.status_code >= 300:
                    hint = {404: '接口路径不存在，请核对生图协议与实际请求地址',
                            401: '鉴权失败，请核对图片密钥和账号区域', 403: '接口无权限，请检查图片服务权限',
                            429: '请求限流，请稍后再试'}.get(response.status_code, '供应商未接受请求，不自动重试')
                    raise ValueError(f'生图接口 HTTP {response.status_code}（{provider}）：{hint}')
                chunks, length = [], 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > 30_000_000 or time.monotonic() - started > 240:
                        raise ValueError('生图响应超过大小或耗时限制')
                    chunks.append(chunk)
        data = json.loads(b''.join(chunks))
        result_type, value = image_result(provider, data)
        if result_type == 'base64':
            if value.startswith('data:image/') and ';base64,' in value:
                value = value.split(';base64,', 1)[1]
            raw = base64.b64decode(value, validate=True)
        else:
            download_route = replace(settings, news_proxy_url=settings.image_proxy_url or settings.visual_proxy_url or settings.news_proxy_url)
            raw, _ = fetch_image(value, draft['news_url'], download_route)
        candidate = {'id': 'G' + key[:16], 'origin': 'generated', 'source_name': '主题插画', 'caption': purpose,
                     'source_url': draft['news_url'], 'url': draft['news_url']}
        asset = save_source_image(settings.data_dir, candidate, raw, draft['news_url'])
        asset.update(kind='illustration', credit='主题插画（概念示意）', generated=True, image_provider=provider,
                     caption=purpose, brief_prompt=(brief or {}).get('prompt', ''),
                     brief_purpose=(brief or {}).get('purpose', ''), brief_category=(brief or {}).get('category', ''),
                     fact_ids=(brief or {}).get('fact_ids', []))
        engine.store.put('generated_images', {'id': key, 'asset': asset, 'created_at': now()})
        row.update(outcome='ok')
        return asset
    except Exception as exc:
        row.update(outcome='failed', error=engine.safe_error(exc))
        if getattr(exc, 'provider_code', None) is not None:
            row['provider_code'] = exc.provider_code
        engine.store.put('generated_images', {'id': key, 'failed_at': now(), 'credential': credential, 'error': row['error']})
        engine.store.event(draft['id'], 'image_fallback', '生图未完成，不自动重试付费请求；保留正文和已有图片，缺图继续显示待补齐')
        return None
    finally:
        row['duration_ms'] = round((time.monotonic() - started) * 1000)
        engine.store.put('image_calls', row)


MAX_PER_PASS = 4


def fill_illustrations(engine, draft, limit=MAX_PER_PASS):
    """按“目标张数－已有实际图”补概念插画；只补缺的，不重生成已成功的。"""
    settings = engine.settings
    visual = draft.setdefault('visuals', {})
    assets = visual.setdefault('assets', [])
    if not settings.image_enabled or settings.demo or draft.get('demo'):
        return {'generated': 0, 'gap': 0, 'reason': 'disabled'}
    from .article_style import visual_target
    kinds = {'official_image', 'benchmark_image', 'media_image', 'illustration'}

    def real_count():
        return len([a for a in assets if a.get('kind') in kinds])

    target = visual_target(draft)
    gap = max(0, target - real_count())
    if not gap:
        return {'generated': 0, 'gap': 0, 'target': target}
    plan = (draft.get('editorial_plan') or {})
    briefs = [b for b in (plan.get('illustrations') or []) if isinstance(b, dict) and b.get('prompt')]
    used = {a.get('brief_prompt') for a in assets if a.get('generated') and a.get('brief_prompt')}
    pending = [b for b in briefs if b['prompt'] not in used][:min(gap, limit)]
    legacy = False
    if not pending and not any(a.get('generated') for a in assets):
        # 旧稿没有插画策划时，沿用一张标题＋摘要主题图，不额外消耗文字模型。
        pending, legacy = [None], True
    made, failures = 0, []
    for brief in pending:
        engine.controls.checkpoint()
        try:
            asset = generate_image(engine, draft, brief)
        except Exception as exc:
            failures.append(engine.safe_error(exc))
            continue
        if not asset:
            failures.append('生图未返回图片')
            continue
        if any(a.get('sha256') == asset.get('sha256') for a in assets):
            continue
        assets.append(asset)
        made += 1
        if real_count() >= target:
            break
    return {'generated': made, 'gap': gap, 'target': target, 'planned': len(briefs),
            'legacy_single': legacy, 'failures': failures[:4]}
