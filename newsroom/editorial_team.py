"""一篇长文的责任编辑分工、分段接力与独立图文编辑。"""
import hashlib
import io
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

from .editors import choose_editor
from .models import Article, OutputCheckError, content_hash
from .render import validate_article
from .images import fetch_image, save_source_image, SourceImageError
from .article_style import public_text


def editorial_image_url(url):
    parts = urlsplit(url)
    if parts.hostname == 'opengraph.githubassets.com':
        return False  # Repository sharing cards are rendered text, not experimental figures.
    return not re.search(r'(?:^|[/_.-])(logo|avatar|favicon|tracking|spacer|portrait|author|metalogo|generic|miss-main-pic|og-docs)(?:[/_.-]|$)|\.svg$', parts.path, re.I)


def image_candidates(html, url, source=None):
    soup = BeautifulSoup(html, 'html.parser')
    found = []
    source = source or {}
    origin = 'official' if source.get('kind') == 'official' else 'benchmark' if source.get('kind') == 'benchmark' else 'article'
    tags = soup.select('article img, main img, #paragraph img, .post_content img, .entry-content img, '
                       '.article-content img, .lph-article-comView img, .markdown-body img')
    if not tags and soup.find(['article', 'main', 'body']) is None:
        tags = soup.select('img')  # Publisher RSS fragments have no article/body wrapper.
    tags += soup.select('meta[property="og:image"], meta[name="og:image"]')
    for tag in tags:
        if tag.name == 'meta' and found:
            continue  # Prefer actual body figures to duplicate social sharing title cards.
        if any(parent.name in {'nav', 'footer', 'header'} or re.search(r'(?:related|recommended|recommendations|sidebar|author|byline|avatar|social|preferred-source)',
                ' '.join(parent.get('class', [])), re.I) for parent in tag.parents):
            continue
        picture = tag.find_parent('picture')
        picture_source = picture.find('source') if picture else None
        srcset = tag.get('data-srcset') or tag.get('srcset') or (picture_source.get('srcset', '') if picture_source else '')
        versions = []
        for entry in srcset.split(','):
            fields = entry.strip().rsplit(' ', 1)
            if len(fields) == 2:
                try:
                    versions.append((float(fields[1].rstrip('wx')), fields[0]))
                except ValueError:
                    continue
        anchor = tag.find_parent('a')
        linked = anchor.get('href', '') if anchor else ''
        linked = linked if re.search(r'\.(?:png|jpe?g|webp|gif|avif)(?:\?|$)', linked, re.I) else ''
        value = tag.get('data-original') or linked or (max(versions)[1] if versions else None) or tag.get('content') or tag.get('data-src') or tag.get('data-lazy-src') or tag.get('src')
        if not value:
            continue
        absolute = urljoin(url, value)
        try:
            parts = urlsplit(absolute)
            if parts.scheme == 'http':
                absolute = parts._replace(scheme='https').geturl()
                parts = urlsplit(absolute)
            port = parts.port
        except ValueError:
            continue
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or port not in (None, 443):
            continue
        if not editorial_image_url(absolute):
            continue
        if any(row['url'] == absolute for row in found):
            continue
        figure = tag.find_parent('figure')
        caption = figure.find('figcaption') if figure else None
        heading = tag.find_previous(['h2', 'h3'])
        row = {'id': 'I' + str(len(found) + 1), 'url': absolute, 'source_url': url,
                      'caption': ((caption.get_text(' ', strip=True) if caption else '') or tag.get('alt') or '原文配图')[:400],
                      'source_heading': heading.get_text(' ', strip=True)[:100] if heading else '',
                      'origin': origin, 'source_id': source.get('id'), 'source_name': source.get('name', ''),
                      'usage': 'source_original' if origin in {'official', 'benchmark'} else 'reference_only'}
        alternatives = [urljoin(url, value) for value in [tag.get('data-src'), tag.get('data-lazy-src'), tag.get('src'),
                        *[entry[1] for entry in sorted(versions, reverse=True)]] if value]
        row['alternative_urls'] = list(dict.fromkeys(value for value in alternatives if value != absolute and value.startswith('https://')))[:4]
        # 页面同时列有缩略版与灯箱原文件时，只用真实存在的原文件链接，保留正文图注。
        stem = lambda address: re.sub(r'\.width-\d+(?:\.format-[a-z]+)?(?=\.)', '', urlsplit(address).path.rsplit('/', 1)[-1])
        previous = next((item for item in found if urlsplit(item['url']).hostname == parts.hostname and stem(item['url']) == stem(absolute)), None)
        if previous:
            if '.width-' in previous['url'] and '.width-' not in absolute:
                previous['url'] = absolute
            continue
        found.append(row)
    found = found[:24]
    for index, item in enumerate(found):
        item['id'] = 'I' + str(index + 1)
    return found


def validate_visual_plan(plan, evidence, candidates):
    facts = {f['id'] for f in evidence['facts']}
    images = {c['id'] for c in candidates}
    if any(set(card['fact_ids']) - facts for card in plan['cards']) or set(plan.get('reference_image_ids', [])) - images:
        raise OutputCheckError('图文方案使用了不存在的证据或图片编号', 'visual_unknown_reference')
    selected = [row['image_id'] for row in plan.get('images', [])]
    usable = {c['id'] for c in candidates if c.get('usage') == 'source_original'}
    if set(selected) - usable or len(selected) != len(set(selected)):
        raise OutputCheckError('官方原图选择了未知、重复或仅供参考的图片', 'visual_unknown_reference')
    if not plan['cards'] and not selected and not (set(plan.get('reference_image_ids', [])) & usable):
        raise OutputCheckError('图文方案没有可用插图', 'visual_unknown_reference')


def draw_card(directory, card, evidence, theme):
    facts = {f['id']: f for f in evidence['facts']}
    font_path = next((p for p in [Path('C:/Windows/Fonts/msyh.ttc'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.exists()), None)
    def font(size):
        return ImageFont.truetype(str(font_path), size) if font_path else ImageFont.load_default(size=size)
    title_font, body_font, small_font = font(43), font(30), font(22)
    accent = {'indigo': '#5a50ce', 'red': '#c03647', 'green': '#167b63'}[theme]
    # 按像素宽度换行，不截断事实；长卡片自动增高。
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    def wrap(value, face, width=920):
        lines, line = [], ''
        for char in value:
            if char == '\n' or measure.textlength(line + char, font=face) > width:
                lines.append(line)
                line = '' if char == '\n' else char
            else:
                line += char
        return lines + ([line] if line else [])
    title_lines = wrap(card['title'], title_font)
    rows = [(ident, wrap(facts[ident]['text'], body_font)) for ident in card['fact_ids']]
    height = 180 + len(title_lines) * 62 + sum(58 + len(lines) * 48 for _, lines in rows)
    image = Image.new('RGB', (1080, height), '#f6f7fc')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 20, height), fill=accent)
    y = 52
    for line in title_lines:
        draw.text((76, y), line, font=title_font, fill=accent); y += 62
    y += 30
    for ident, lines in rows:
        draw.text((76, y), ident, font=small_font, fill=accent); y += 36
        for line in lines:
            draw.text((76, y), line, font=body_font, fill='#232938'); y += 48
        y += 22
    draw.text((76, height - 55), 'AI 编辑部 · 根据文内证据整理', font=small_font, fill='#697286')
    directory.mkdir(parents=True, exist_ok=True)
    import io
    output = io.BytesIO(); image.save(output, format='PNG', optimize=True)
    raw = output.getvalue(); digest = hashlib.sha256(raw).hexdigest()
    path = directory / (digest + '.png')
    if not path.exists():
        path.write_bytes(raw)
    return {'id': digest, 'file': path.name, 'path': str(path.resolve()), 'sha256': digest,
            'kind': 'infographic',
            'title': card['title'], 'fact_ids': card['fact_ids'], 'after_heading': card['after_heading'], 'after_text': card.get('after_text', ''),
            'credit': 'AI 编辑部 · 信息图', 'mime': 'image/png'}


def asset_file(root, asset):
    ident = asset.get('id', '')
    if not re.fullmatch('[a-f0-9]{64}', ident):
        raise ValueError('插图标识不合法')
    file = asset.get('file') or (ident + '.png')
    if file not in {ident + '.png', ident + '.jpg'}:
        raise ValueError('插图文件名不合法')
    path = Path(root) / 'media' / file
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset.get('sha256'):
        raise ValueError('插图文件缺失或已变化，不能沿用原审批')
    return path


def original_asset_file(root, asset):
    ident = asset.get('original_sha256', '')
    file = asset.get('original_file', '')
    if not re.fullmatch(r'[a-f0-9]{64}', ident) or file not in {ident + ext for ext in ('.jpg', '.png', '.webp', '.gif', '.avif')}:
        raise ValueError('原图文件名或哈希不合法')
    path = Path(root) / 'original-media' / file
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ident:
        raise ValueError('新闻原图文件缺失或已变化')
    return path


class EditorialTeam:
    def collect_image_candidates(self, draft, discover=False, force_discovery=False):
        stories = [self.store.get('stories', draft['story_id'])]
        from .source_quality import policy
        for row in (draft.get('crosscheck') or {}).get('sources', []):
            if not row.get('same_event') or row.get('confidence', 0) < policy()['crosscheck_min_confidence'] or not row.get('story_id'):
                continue
            try:
                stories.append(self.store.get('stories', row['story_id']))
            except KeyError:
                continue
        candidates = {}
        from .source_quality import identity
        for index, story in enumerate(stories):
            try:
                source = self.source_for(story)
            except ValueError:
                source = {}
            if discover and index < 3 and source and not draft.get('demo'):
                from .visual_discovery import discover_story_images
                story = discover_story_images(self, story, force=force_discovery)
            for raw in story.get('image_candidates', []):
                candidate = dict(raw)
                if not editorial_image_url(candidate['url']):
                    continue
                origin = 'article'
                try:
                    identity(source, candidate['source_url'])
                    if source.get('kind') in {'official', 'benchmark'}:
                        origin = 'official' if source['kind'] == 'official' else 'benchmark'
                except (ValueError, KeyError):
                    pass
                candidate.update(origin=origin, source_id=source.get('id'), source_name=source.get('name') or story['source'],
                    usage='source_original' if origin != 'article' or (self.settings.drafts_only and source.get('tier') in {'primary', 'secondary', 'research'}) else 'reference_only', network=source.get('network'),
                    source_body_hash=(story.get('source_snapshot') or {}).get('body_hash', ''))
                previous = candidates.get(candidate['url'])
                if not previous or previous['usage'] == 'reference_only':
                    candidates[candidate['url']] = candidate
            from .visual_discovery import official_link
            for candidate in story.get('linked_image_candidates', []):
                name = official_link(candidate.get('source_url', ''))
                if name and candidate.get('linked_from') == story['url']:
                    candidates[candidate['url']] = {**candidate, 'origin': 'official', 'source_name': name,
                                                   'usage': 'source_original', 'network': None}
        result = sorted(candidates.values(), key=lambda c: {'official': 0, 'benchmark': 1, 'article': 2}[c['origin']])[:48]
        for index, candidate in enumerate(result):
            candidate['id'] = 'I' + str(index + 1)
        return result

    def prefetch_images(self, candidates):
        """只做网络下载的有界并发预取；不写盘、不写数据库、不调用模型。"""
        if not candidates:
            return {}

        def one(candidate):
            last_error = None
            # Only try URLs actually present on this image element; never guess signed CDN transforms.
            for address in [candidate['url'], *candidate.get('alternative_urls', [])][:3]:
                try:
                    raw, final = fetch_image(address, candidate['source_url'], self.settings)
                    # 预取阶段先确认字节可解码，坏文件继续尝试同一元素的备用地址。
                    try:
                        with Image.open(io.BytesIO(raw)) as probe:
                            probe.verify()
                    except Exception:
                        raise SourceImageError('图片文件无法解码') from None
                    return ('ok', raw, final, None)
                except (SourceImageError, ValueError, OSError) as exc:
                    last_error = exc
            return ('error', None, None, last_error)

        if len(candidates) == 1:
            return {candidates[0]['_ident']: one(candidates[0])}
        from concurrent.futures import ThreadPoolExecutor
        workers = min(4, len(candidates))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, candidates))
        return {candidate['_ident']: result for candidate, result in zip(candidates, results)}

    def download_source_images(self, candidates, plan, draft):
        eligible = {row['id']: row for row in candidates if row['usage'] == 'source_original'}
        placements = {row['image_id']: row.get('after_heading', '') for row in plan.get('images', [])}
        text_placements = {row['image_id']: row.get('after_text', '') for row in plan.get('images', [])}
        ordered = list(placements) + [ident for ident in plan.get('reference_image_ids', []) if ident in eligible]
        ordered += [ident for ident in eligible if ident not in ordered]
        ordered = list(dict.fromkeys(ordered))
        from .article_style import visual_target
        target = min(10, max(len(placements), visual_target(draft)))
        headings = re.findall(r'^#{2,3}\s+(.+)', draft['article']['markdown'], re.M)
        assets, errors, seen = [], [], set()
        cached, pending = {}, []
        for ident in ordered[:18]:
            candidate = eligible.get(ident)
            if not candidate:
                continue
            key = hashlib.sha256((candidate['url'] + '\0' + candidate['source_url']).encode()).hexdigest()
            try:
                row = dict(self.store.get('source_image_cache', key)['asset'])
                asset_file(self.settings.data_dir, row)
                original_asset_file(self.settings.data_dir, row)
                cached[ident] = row
            except (KeyError, ValueError):
                pending.append((ident, candidate, key))
        # 只需补足缺口；多预取两张用于失败替换，避免把整列表都读进内存。
        need = max(0, target - len(cached))
        prefetched = {}
        if need:
            queue = []
            for ident, candidate, key in pending[:min(need + 2, 8)]:
                queue.append({**candidate, '_ident': ident})
            self.controls.checkpoint()
            prefetched = self.prefetch_images(queue)
        for ident in ordered[:18]:
            if len(assets) >= target:
                break
            candidate = eligible.get(ident)
            if not candidate:
                continue
            if ident in cached:
                asset = dict(cached[ident])
            elif ident in prefetched:
                status, raw, final, error = prefetched[ident]
                if status != 'ok':
                    reason = str(error) if isinstance(error, SourceImageError) else '原图下载或文件核验失败'
                    errors.append({'image_id': ident, 'image_host': urlsplit(candidate['url']).hostname,
                                   'source_url': candidate['source_url'], 'error': reason,
                                   'code': getattr(error, 'code', 'file_validation'), 'http_status': getattr(error, 'status', None)})
                    continue
                key = hashlib.sha256((candidate['url'] + '\0' + candidate['source_url']).encode()).hexdigest()
                try:
                    asset = save_source_image(self.settings.data_dir, candidate, raw, final)
                except (SourceImageError, ValueError, OSError) as exc:
                    reason = str(exc) if isinstance(exc, SourceImageError) else '原图下载或文件核验失败'
                    errors.append({'image_id': ident, 'image_host': urlsplit(candidate['url']).hostname,
                                   'source_url': candidate['source_url'], 'error': reason,
                                   'code': getattr(exc, 'code', 'file_validation'), 'http_status': getattr(exc, 'status', None)})
                    continue
                self.store.put('source_image_cache', {'id': key, 'asset': asset})
            else:
                continue
            if asset['sha256'] in seen:
                continue
            seen.add(asset['sha256'])
            asset['image_id'] = ident
            asset['after_heading'] = placements.get(ident, headings[len(assets)] if len(assets) < len(headings) else '')
            asset['after_text'] = text_placements.get(ident, '')
            assets.append(asset)
        return assets, {'available_count': len(eligible), 'selected_count': len(assets), 'errors': errors}

    def write_with_team(self, context, angle, editor, run):
        if editor['id'] == 'auto':
            return self.write_autonomously(context, angle, editor, run, plan=context.get('prepared_plan'))
        if editor['id'] != 'relay':
            prompt = self.model.complete('prompt', {**context, 'angle': angle}, run)
            article = self.model.complete('write', {**context, 'angle': angle, 'writing_prompt': prompt}, run)
            article = self.polish_article(article, {**context, 'angle': angle, 'writing_prompt': prompt}, run)
            return article, prompt, None, []
        team = [choose_editor(self.store, {'editor_id': ident, 'format': 'commentary',
                'custom_style': editor.get('custom_style', '')}) for ident in ('flash', 'rigor', 'common')]
        def validate(plan):
            ids = [row['editor_id'] for row in plan['sections']]
            if len(set(ids)) != len(ids) or ids != sorted(ids, key=['flash', 'rigor', 'common'].index):
                raise OutputCheckError('接力顺序或小编重复', 'editorial_plan_order')
        plan = self.model.complete('plan', {**context, 'angle': angle, 'team': team}, run, validator=validate)
        parts = []
        for section in plan['sections']:
            member = next(e for e in team if e['id'] == section['editor_id'])
            data = {**context, 'editor': member, 'angle': angle, 'section': section, 'editorial_plan': plan,
                    'previous_sections': [{'heading': p['heading'], 'markdown': p['article']['markdown']} for p in parts]}
            prompt = self.model.complete('prompt', data, run)
            article = self.model.complete('write', {**data, 'writing_prompt': prompt}, run)
            validate_article(article, context['evidence'])
            parts.append({'heading': section['heading'], 'editor': member, 'prompt': prompt, 'article': article})
        article = {**parts[0]['article'], 'markdown': '\n\n'.join('## ' + p['heading'] + '\n\n' + p['article']['markdown'] for p in parts)}
        Article.model_validate(article)
        return article, parts[0]['prompt'], plan, parts

    def write_autonomously(self, context, angle, editor, run, plan=None):
        if plan is None:
            plan = self.model.complete('plan_article', context, run)
        else:
            from .models import ArticlePlan
            plan = ArticlePlan.model_validate(plan).model_dump()
        modern_feature = self.settings.drafts_only and not self.settings.demo
        if modern_feature:
            from .article_style import feature_plan
            plan = feature_plan(plan, context['evidence'])
        angle.clear(); angle.update(plan['angle'])
        write_context = {k: v for k, v in context.items() if k != 'prepared_plan'}
        article = self.model.complete('write', {**write_context, 'angle': angle, 'editorial_plan': plan,
            'autonomous_article': True, 'writing_prompt': plan['writing_prompt'],
            'length_instruction': '一次写成完整文章，目标约' + str(plan['target_chars']) + '字；按信息量自然展开，不分小编，不固定新闻评论比例，不截断或凑字数。'}, run)
        # Remove only stand-alone production directions; never remove factual qualifications.
        article['markdown'] = re.sub(r'(?m)^[ \t]*(?:【(?:配图|插图|图片)(?:建议)?[：:][^\n】]{1,240}】|\[(?:配图|插图|图片)(?:建议)?[：:][^\n\]]{1,240}\])[ \t]*$', '', article['markdown'])
        article['markdown'] = re.sub(r'\n{3,}', '\n\n', article['markdown']).strip()
        validate_article(article, context['evidence'])
        from .article_style import article_chars
        actual = article_chars(article['markdown'])
        if (modern_feature and plan['story_mode'] != 'brief' and len(context['evidence']['facts']) >= 8
                and actual < plan['target_chars'] * .8):
            # At most one additive writing call, never a review/rewrite loop. Keep the first draft on failure.
            self.controls.checkpoint()
            plan['expansion_status'] = 'attempted_once'
            try:
                extra = self.model.complete('expand_article', {**write_context, 'existing_article': article,
                    'requested_additional_chars': max(1000, plan['target_chars'] - actual),
                    'editorial_plan': plan}, run)
                validate_article(extra, context['evidence'])
                existing_paragraphs = {re.sub(r'\s+', '', p) for p in public_text(article['markdown']).split('\n\n') if len(p) > 30}
                additions = [p for p in extra['markdown'].split('\n\n') if re.sub(r'\s+', '', public_text(p)) not in existing_paragraphs]
                supplement = '\n\n'.join(additions).strip()
                if article_chars(supplement) >= 300:
                    # Keep the original closing section at the end rather than tack another ending onto it.
                    endings = list(re.finditer(r'(?m)^##\s+', article['markdown']))
                    position = endings[-1].start() if len(endings) >= 3 else len(article['markdown'])
                    article['markdown'] = article['markdown'][:position].rstrip() + '\n\n' + supplement + '\n\n' + article['markdown'][position:]
                    validate_article(article, context['evidence'])
                    plan['expansion_status'] = 'added_once'
                else:
                    plan['expansion_status'] = 'no_useful_addition'
            except Exception as exc:
                plan['expansion_status'] = 'failed_keep_original'
                self.store.event(run['id'], 'expansion_failed', self.safe_error(exc))
            actual = article_chars(article['markdown'])
        article = self.polish_article(article, write_context, run)
        validate_article(article, context['evidence'])
        actual = article_chars(article['markdown'])
        plan.update(mode='single_article', actual_chars=actual,
                    length_status='short' if actual < plan['target_chars'] * .85 else 'long' if actual > plan['target_chars'] * 1.35 else 'on_target')
        return article, plan['writing_prompt'], plan, []

    def polish_article(self, article, context, run):
        """终稿润色：只改表达。任何校验失败都保留原稿，绝不因润色放行新事实。"""
        if not self.settings.polish_pass or self.settings.demo:
            return article
        try:
            polished = self.model.complete('polish', {**context, 'article': article}, run)
        except Exception as exc:
            self.store.event(run['id'], 'polish_failed', self.safe_error(exc))
            return article
        try:
            from .article_style import article_chars
            from .source_quality import validate_claims
            candidate = Article.model_validate(polished).model_dump()
            if len(candidate['title']) > 32 or len(candidate['digest']) > 120:
                raise ValueError('润色后的标题或摘要超过微信字段上限')
            if article_chars(candidate['markdown']) < article_chars(article['markdown']) * 0.7:
                raise ValueError('润色后篇幅明显缩短')
            validate_article(candidate, context['evidence'])
            validate_claims(candidate, context['evidence'], context.get('source_snapshot'))
            self.store.event(run['id'], 'polish', '已完成终稿润色（仅表达层，事实与引用未变）')
            return candidate
        except Exception as exc:
            self.store.event(run['id'], 'polish_rejected', self.safe_error(exc))
            return article

    def prepare_visuals(self, draft, run, force_refresh=False, allow_generation=True):
        from .article_style import visual_target
        from .source_quality import as_date
        from .db import now
        from datetime import datetime, timedelta, timezone
        originals_only = self.settings.drafts_only and not draft.get('demo')
        previous = draft.get('visuals') or {}
        existing = (draft.get('visuals') or {}).get('assets', [])
        reusable = []
        if (draft.get('visuals') or {}).get('article_hash') == content_hash(draft['article']):
            for asset in existing:
                if originals_only and (asset.get('kind') == 'infographic' or not editorial_image_url(asset.get('original_url', ''))):
                    continue
                try:
                    asset_file(self.settings.data_dir, asset)
                    reusable.append(asset)
                except ValueError:
                    pass
        checked = as_date(previous.get('recovery_checked_at'))
        recently_tried = checked and datetime.now(timezone.utc) - checked < timedelta(minutes=30)
        if not force_refresh and (reusable or recently_tried) and (not originals_only or len(reusable) >= visual_target(draft) or recently_tried):
            draft['visuals']['assets'] = reusable
            if self.settings.drafts_only:
                self.complete_visuals(draft, allow_generation=allow_generation)
            return draft
        candidates = self.collect_image_candidates(draft, discover=originals_only, force_discovery=force_refresh)
        context = {'article': draft['article'], 'evidence': draft['evidence'], 'image_candidates': candidates}
        try:
            if self.settings.fast_mode or run is None:
                usable = [c for c in candidates if c['usage'] == 'source_original'][:visual_target(draft)]
                plan = {'theme': 'red', 'reason': '快速模式：程序选配真实原图并按段落分散排版，未调用图文模型',
                        'images': [{'image_id': c['id'], 'after_heading': ''} for c in usable],
                        'cards': [] if usable or originals_only else [{'after_heading': '', 'title': '这次变化，重点在这里', 'fact_ids': [f['id'] for f in draft['evidence']['facts'][:3]]}],
                        'reference_image_ids': []}
                state, error = 'automatic_layout', ''
            else:
                plan = self.model.complete('visual', context, run,
                    validator=lambda p: validate_visual_plan(p, draft['evidence'], candidates))
                state, error = 'model_planned', ''
        except Exception as exc:
            # 图片服务失败不丢失已写正文；使用已校验事实做原创配图，审核台保留错误。
            state, error = 'fallback', self.safe_error(exc)
            self.store.event(draft['id'], 'visual_fallback', error)
            plan = {'theme': 'indigo', 'reason': '图文服务未完成，采用原始事实卡片', 'cards': [
                {'after_heading': '', 'title': '一图看懂关键进展', 'fact_ids': [f['id'] for f in draft['evidence']['facts'][:3]]}],
                'reference_image_ids': []}
        originals, image_report = self.download_source_images(candidates, plan, draft) if not draft.get('demo') else ([], {'available_count': 0, 'selected_count': 0, 'errors': []})
        if originals_only:
            ids = [draft['story_id']] + [row['story_id'] for row in (draft.get('crosscheck') or {}).get('sources', [])
                if row.get('same_event') and row.get('story_id')]
            image_report['discovery'] = []
            for story_id in list(dict.fromkeys(ids))[:3]:
                source_story = self.store.get('stories', story_id)
                image_report['discovery'].append({'source': source_story['source'], **(source_story.get('image_discovery') or {})})
        cards = [] if originals_only else [draw_card(self.settings.data_dir / 'media', card, draft['evidence'], plan['theme']) for card in plan['cards']]
        assets = list({a['sha256']: a for a in reusable + originals + cards}.values())
        if not assets and not originals_only:
            state, error = 'fallback', '官方原图未能取到，使用证据信息图'
            assets = [draw_card(self.settings.data_dir / 'media', {'title': '关键事实', 'fact_ids': [f['id'] for f in draft['evidence']['facts'][:3]], 'after_heading': ''}, draft['evidence'], plan['theme'])]
        draft['visuals'] = {'state': state, 'error': error, 'plan': plan, 'assets': assets, 'recovery_checked_at': now(),
                            'reference_candidates': candidates, 'source_images': image_report, 'article_hash': content_hash(draft['article'])}
        if self.settings.drafts_only:
            self.complete_visuals(draft, allow_generation=allow_generation)
        return draft

    def complete_visuals(self, draft, allow_generation=True):
        from .article_style import visual_target
        visual = draft['visuals']
        originals_only = self.settings.drafts_only and not draft.get('demo')
        assets = visual['assets'] = [a for a in visual['assets'] if not originals_only or a.get('kind') != 'infographic']
        mode = self.publication_rules().get('cover_mode', 'auto')
        illustration_report = {'generated': 0, 'gap': 0}
        if allow_generation and self.settings.image_enabled:
            # 按“目标张数－已有实际图”补齐：优先使用整篇策划里的插画 brief，旧稿退回一张模板主题图。
            from .image_service import fill_illustrations
            illustration_report = fill_illustrations(self, draft)
            visual['illustration_report'] = illustration_report
            assets = visual['assets']
        real = [a for a in assets if a.get('kind') in {'official_image', 'benchmark_image', 'media_image', 'illustration'}]
        # Three distinct figures for a feature; short news can use two. Never duplicate one logo to fill space.
        minimum = 3 if len(draft['article']['markdown']) >= 1800 else 2
        facts = draft['evidence']['facts']
        for offset in range(0 if originals_only else len(facts)):
            if len(assets) >= minimum:
                break
            group = facts[offset:offset + 2]
            card = draw_card(self.settings.data_dir / 'media', {'title': group[0]['text'][:24],
                'fact_ids': [f['id'] for f in group], 'after_heading': ''}, draft['evidence'], 'red')
            if not any(a['id'] == card['id'] for a in assets):
                assets.append(card)
        if originals_only:
            original = len([a for a in assets if a.get('kind') in {'official_image', 'benchmark_image', 'media_image'}])
            generated = len([a for a in assets if a.get('kind') == 'illustration' and a.get('generated')])
            count = original + generated
            target = visual_target(draft)
            visual['quality'] = {'original_count': original, 'generated_count': generated, 'text_card_count': 0,
                                 'target_images': target, 'needs_more_images': count < target,
                                 'generation_requests': (illustration_report or {}).get('generated', 0),
                                 'missing_after': max(0, target - count)}
            visual['state'] = 'illustrated' if count >= target else 'images_partial' if assets else 'needs_images'
            visual['error'] = '' if count >= target else f'实际图片 {original} 张、概念插画 {generated} 张，共 {count}/{target}；没有用文字卡片补数'
        if not assets:
            visual.update(cover_asset_id=None, cover_reason='没有可用新闻图片或主题图，请先补图；未用文字卡片冒充封面', article_hash=content_hash(draft['article']))
            return draft
        import math
        def relevance(asset):
            title_words = set(re.findall(r'[a-z0-9]{3,}|[\u4e00-\u9fff]{2,4}', draft['article']['title'].lower()))
            caption = asset.get('title', '').lower()
            area = max(1, asset.get('width', 800) * asset.get('height', 450))
            ratio = max(.01, asset.get('width', 800) / max(1, asset.get('height', 450)))
            landscape = max(-6, 3 - abs(math.log(ratio / (16 / 9))) * 3)
            logo_penalty = 20 if re.search(r'\blogo\b|头像|图标', caption) else 0
            return sum(word in caption for word in title_words) * 3 + math.log(area) + landscape - logo_penalty + (3 if asset.get('kind') == 'official_image' else 0)
        cover = max(real or assets, key=relevance)
        if mode == 'generated':
            cover = next((a for a in assets if a.get('generated')), cover)
        visual['cover_asset_id'] = cover['id']
        visual['cover_reason'] = '优先本次新闻的实际图片，结合标题相关性、图片尺寸与横图比例选择；没有可用原图则采用主题配图或信息图'
        visual['article_hash'] = content_hash(draft['article'])
        return draft

    def refresh_draft_images(self, job):
        import copy
        from .db import now
        run = self.new_run('images', job)
        try:
            with self.lock:
                draft = copy.deepcopy(self.store.get('drafts', job['subject']))
                if draft['revision'] != job['revision']:
                    raise ValueError('稿件版本已更新，请刷新后再补图')
                self.assert_editable(draft['id'])
                if draft.get('review_hash') != content_hash(draft['article']):
                    raise ValueError('请先完成正文的程序检查，再单独补图')
            self.prepare_visuals(draft, run, force_refresh=True, allow_generation=False)
            self.controls.checkpoint()
            with self.lock:
                current = self.store.get('drafts', draft['id'])
                if current['revision'] != job['revision']:
                    raise ValueError('正文已被编辑，保留下载缓存，没有覆盖新版本')
                self.assert_editable(draft['id'])
                draft.update(revision=draft['revision'] + 1, status='pending_human', approved_hash=None, updated_at=now())
                self.seal_checks(draft)
                self.store.put('drafts', draft)
                self.refresh_local_handoff(draft)
            quality = draft['visuals'].get('quality') or {}
            self.finish_run(run, 'done' if not quality.get('needs_more_images') else 'partial',
                message=f"原图搜集完成：{quality.get('original_count', 0)} 张实际图片；未改写正文、未调用文字或生图模型", progress=100)
        except Exception:
            self.finish_run(run, 'failed')
            raise

    def generate_draft_illustrations(self, job):
        """按缺口补概念插画：只补缺的，不重生成已成功的；部分失败保留已有图片。"""
        import copy
        from .db import now
        from .image_service import fill_illustrations
        run = self.new_run('illustrations', job)
        try:
            with self.lock:
                draft = copy.deepcopy(self.store.get('drafts', job['subject']))
                if draft['revision'] != job['revision']:
                    raise ValueError('稿件版本已更新，请刷新后再补插画')
                self.assert_editable(draft['id'])
                if draft.get('review_hash') != content_hash(draft['article']):
                    raise ValueError('请先完成正文的程序检查，再补齐插画')
            visual = draft.setdefault('visuals', {})
            visual.setdefault('assets', [])
            report = fill_illustrations(self, draft)
            self.complete_visuals(draft, allow_generation=False)
            quality = visual.get('quality') or {}
            target = quality.get('target_images', 0)
            count = quality.get('original_count', 0) + quality.get('generated_count', 0)
            if not report.get('generated'):
                reason = '；'.join(report.get('failures') or []) or '生图未启用、目标已满足或没有可用的插画策划'
                self.finish_run(run, 'partial' if report.get('gap') else 'done',
                                message='本次没有新增插画：' + reason + f'（当前 {count}/{target}）', progress=100)
                return draft
            with self.lock:
                current = self.store.get('drafts', draft['id'])
                if current['revision'] != job['revision']:
                    raise ValueError('正文已被编辑，保留已生成图片，没有覆盖新版本')
                self.assert_editable(draft['id'])
                draft.update(revision=draft['revision'] + 1, status='pending_human', approved_hash=None, updated_at=now())
                self.seal_checks(draft)
                self.store.put('drafts', draft)
                self.refresh_local_handoff(draft)
            message = (f"补齐概念插画 {report.get('generated', 0)} 张；实际图片 {quality.get('original_count', 0)} 张 + 插画 "
                       f"{quality.get('generated_count', 0)} 张，共 {count}/{target} 张；未改写正文、未提交微信")
            if report.get('failures'):
                message += f"；{len(report['failures'])} 次生成失败"
            self.finish_run(run, 'done' if not quality.get('needs_more_images') else 'partial', message=message, progress=100)
            return draft
        except Exception:
            self.finish_run(run, 'failed')
            raise
