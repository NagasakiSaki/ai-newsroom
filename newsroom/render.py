import re
from html import escape

import bleach
import markdown
from bs4 import BeautifulSoup, NavigableString

from .models import Article, OutputCheckError
from .article_style import public_text


def emphasize(soup):
    """Render a narrow text-only emphasis syntax; model output never supplies HTML/style."""
    remaining = 12
    for node in list(soup.find_all(string=True)):
        if any(p.name in {'a', 'code', 'pre', 'h1', 'h2', 'h3', 'h4'} for p in node.parents):
            continue
        if not remaining:
            node.replace_with(NavigableString(re.sub(r'==([^=\n]{2,100})==', r'\1', str(node))))
            continue
        matches = list(re.finditer(r'==([^=\n]{2,100})==', str(node)))[:remaining]
        if not matches:
            continue
        fragments, end = [], 0
        for match in matches:
            fragments.append(NavigableString(str(node)[end:match.start()]))
            tag = soup.new_tag('span')
            tag['style'] = 'color:#f15a24;font-weight:700;'
            tag.string = match.group(1)
            fragments.append(tag)
            end = match.end()
        tail = str(node)[end:]
        if remaining == len(matches):
            tail = re.sub(r'==([^=\n]{2,100})==', r'\1', tail)
        fragments.append(NavigableString(tail))
        node.replace_with(*fragments)
        remaining -= len(matches)


def validate_evidence(evidence, news):
    ids = [fact["id"] for fact in evidence["facts"]]
    if len(ids) != len(set(ids)):
        raise OutputCheckError('证据编号重复', 'duplicate_fact_id', 'facts')
    for fact in evidence["facts"]:
        if fact["source_url"] != news["url"] or fact["quote"] not in news["text"]:
            raise OutputCheckError('提取证据与原文不匹配，停止生成', 'evidence_quote_or_url_mismatch', 'facts.' + fact['id'])


def validate_article(article, evidence):
    Article.model_validate(article)
    ids = {fact["id"] for fact in evidence["facts"]}
    cited = set(re.findall(r"\[(F\d+)\]", article["markdown"]))
    if not cited or cited - ids:
        raise ValueError("稿件缺少有效事实引用或引用不存在的证据")
    if re.search(r"<[^>]+>|!\[|\]\(\s*(?:https?:|javascript:|data:)", article["markdown"], re.I):
        raise ValueError("正文请使用纯 Markdown；配图和外链应在来源与权利核实后另行配置")


def render_article(draft, image_urls=None, image_prefix='/media/', original_prefix=None):
    public_markdown = re.sub(r'\[F\d+\]', '', draft['article']['markdown'])
    # Remove only a trailing production disclosure, never news about AI itself.
    public_markdown = re.sub(r'(?im)\n\s*[*_]*(?:本文|本稿)(?:由|使用|采用|经)\s*AI\s*(?:辅助|生成|整理|撰写)[^\n]{0,120}[*_]*\s*$', '', public_markdown)
    body = markdown.markdown(public_markdown)
    body = bleach.clean(body, tags={"p", "h1", "h2", "h3", "h4", "strong", "em", "ul", "ol", "li", "blockquote", "code", "pre", "br", "hr"}, attributes={}, strip=True)
    sources = '<h3>参考来源</h3>'
    rows = [(draft.get('source') or '原始来源', draft.get('news_url') or '')]
    rows.extend((r.get('source') or '参考来源', r.get('url') or '') for r in (draft.get('crosscheck') or {}).get('sources', []) if r.get('same_event') and r.get('counted_as_corroboration'))
    from urllib.parse import urlsplit
    seen = set()
    for name, url in rows:
        parts = urlsplit(url)
        if url in seen or parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            continue
        seen.add(url)
        sources += '<p><a href="' + escape(url, quote=True) + '">' + escape(name) + '</a></p>'
    disclosure = ''
    warning = "<p><strong>虚构演示内容，不是真实新闻，禁止用于真实发布。</strong></p>" if draft["demo"] else ""
    byline = '<p>文 / ' + escape(draft.get('author') or 'AI 编辑部') + '</p>'
    soup = BeautifulSoup(f'<section>{byline}{warning}{body}{sources}{disclosure}</section>', "html.parser")
    emphasize(soup)
    paragraphs = [p for p in soup.section.find_all('p') if not p.find('a') and
                  not p.find_parent('blockquote') and p.get_text(strip=True) != '本文由 AI 辅助采编与排版。']
    placement_tails = {}
    assets = (draft.get('visuals') or {}).get('assets', [])
    for index, asset in enumerate(assets):
        figure = soup.new_tag('figure')
        image = soup.new_tag('img')
        image['src'] = (image_urls or {}).get(asset['id']) or image_prefix + asset['file']
        image['alt'] = asset['title']
        figure.append(image)
        caption = soup.new_tag('figcaption')
        if asset.get('kind') in {'official_image', 'benchmark_image', 'media_image'}:
            if asset['title'] != '原文配图':
                text = soup.new_tag('span'); text.string = asset['title']; caption.append(text); caption.append(soup.new_tag('br'))
            credit = soup.new_tag('a', href=asset['source_url']); credit.string = asset['credit']; caption.append(credit)
            original_url = (original_prefix + asset['original_file']) if original_prefix else asset.get('original_url')
            if original_url:
                caption.append(' · ')
                original_link = soup.new_tag('a', href=original_url); original_link.string = '查看原图'; caption.append(original_link)
        else:
            caption.string = asset['credit']
        figure.append(caption)
        normalize = lambda value: re.sub(r'\s+', '', public_text(value))
        snippet = normalize(asset.get('after_text') or '')
        anchor = next((p for p in paragraphs if snippet in normalize(p.get_text())), None) if len(snippet) >= 8 else None
        heading = asset.get('after_heading')
        if anchor is None and heading:
            chapter = next((t for t in soup.find_all(['h2', 'h3']) if t.get_text(strip=True) == heading), None)
            if chapter is not None:
                anchor = chapter.find_next_sibling()
                while anchor is not None and anchor.name not in {'p', 'h2', 'h3'}:
                    anchor = anchor.find_next_sibling()
                if anchor is None or anchor.name != 'p':
                    anchor = chapter
        if anchor is None and paragraphs:
            anchor = paragraphs[min(len(paragraphs) - 1, max(0, round((index + 1) * len(paragraphs) / (len(assets) + 1)) - 1))]
        position = id(anchor) if anchor is not None else '__lead__'
        if position in placement_tails:
            placement_tails[position].insert_after(figure)
        elif anchor is not None:
            anchor.insert_after(figure)
        else:
            first = soup.section.find('p', recursive=False)
            if first: first.insert_after(figure)
            else: soup.section.append(figure)
        placement_tails[position] = figure
    styles = {"section": "font-size:16px;line-height:2;color:#222;letter-spacing:0.6px;word-break:break-word;",
              "p": "margin:0 0 26px;line-height:2;font-size:16px;text-align:justify;",
              "h1": "font-size:24px;margin:28px 0 18px;color:#32305c;",
              "h2": "font-size:21px;line-height:1.6;text-align:center;margin:42px 0 28px;color:#111;font-weight:800;",
              "h3": "font-size:18px;line-height:1.7;margin:30px 0 20px;color:#111;font-weight:700;",
              "blockquote": "margin:26px 0;padding:18px 20px;border-left:3px solid #ddd;background:#f4f4f4;color:#666;",
              "strong": "font-weight:700;color:#111;", "em": "font-size:13px;color:#888;",
              "li": "margin:8px 0;line-height:1.9;",
              "figure": "margin:28px 0 32px;", "img": "display:block;width:100%;max-width:100%;height:auto;",
              "figcaption": "text-align:center;font-size:12px;color:#858b99;margin:8px 0 18px;"}
    for tag in soup.find_all(True):
        if tag.name in styles:
            tag["style"] = styles[tag.name]
    return str(soup)


def export_document(draft, image_prefix='/media/', original_prefix=None):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + escape(draft["article"]["title"]) + '</title><body style="max-width:720px;margin:40px auto;padding:20px"><h1>' + escape(draft["article"]["title"]) + '</h1>' + render_article(draft, image_prefix=image_prefix, original_prefix=original_prefix) + '</body></html>'
