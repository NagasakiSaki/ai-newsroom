import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .config import ROOT
from .db import now


def policy():
    return json.loads((ROOT / "config/source_policy.json").read_text(encoding="utf-8"))


def as_date(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def identity(source, url):
    parts = urlsplit(url)
    allowed = source.get("allowed_hosts") or [urlsplit(source["url"]).hostname]
    if parts.scheme != "https" or parts.hostname not in allowed or parts.username or parts.password:
        raise ValueError("文章域名与已登记信源不一致")
    prefixes = source.get("article_prefixes", ["/"])
    if not any(parts.path.startswith(prefix) for prefix in prefixes):
        raise ValueError("文章路径不在信源允许的内容范围")
    return True


def assessment(source, item, corroboration=False, rules=None):
    if item.get("demo"):
        return {"state": "demo", "tier": "demo", "auto_eligible": True, "note": "虚构演示，不是权威新闻认证"}
    if not source.get('enabled', True):
        return {'state': 'blocked', 'tier': source.get('tier', 'discovery'), 'auto_eligible': False, 'note': '信源已停用，不能用于自动采编或旁证'}
    rules = rules or policy()
    try:
        identity(source, item["url"])
    except ValueError as exc:
        return {"state": "blocked", "tier": source.get("tier", "discovery"), "auto_eligible": False, "note": str(exc)}
    date = as_date(item.get("published_at"))
    tier = source.get("tier", "discovery")
    # 官方一手发布频率低，给更长窗口；媒体二手维持 24 小时，保证"新闻要新"。
    if corroboration:
        limit = rules.get('crosscheck_max_age_hours', 72)
    elif source.get("kind") == "paper":
        limit = rules["paper_max_age_hours"]
    elif tier == "primary":
        limit = rules.get("official_max_age_hours", rules["news_max_age_hours"])
    else:
        limit = rules["news_max_age_hours"]
    current = datetime.now(timezone.utc)
    valid_date = bool(date and current - timedelta(hours=limit) <= date <= current + timedelta(minutes=10))
    # 官方列表只有日期时按天精度采信，但必须带标注；媒体二手仍要求精确发布时间。
    bases = {'published', 'published_date_only'} if (corroboration or tier == "primary" or source.get('kind') == 'benchmark') else {'published'}
    dated = item.get('date_basis') in bases
    eligible = valid_date and dated and tier in {"primary", "research", 'secondary'}
    note = source.get("evidence_note", "未分级来源，不能自动当作权威来源")
    if not valid_date:
        note = "原始发布时间过期、缺失或异常，不能作为当前新闻自动成稿"
    elif not dated:
        note = "仅有更新时间，无法证明是新事件；保留线索，不自动成稿"
    elif item.get('date_basis') == 'published_date_only':
        note = note.rstrip('。') + "；列表只提供日期，按天精度处理，正文须写明发布日期、不虚构具体时刻"
    return {"state": "eligible" if eligible else "discovery_only", "tier": tier, "auto_eligible": eligible,
            "organization": source.get("organization"), "note": note, "checked_at": now(), "max_age_hours": limit}


def page_dates(html):
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for label, names in {"published_at": {"article:published_time", "datepublished", "date", "dc.date.issued"},
                         "updated_at": {"article:modified_time", "datemodified", "last-modified"}}.items():
        for tag in soup.find_all("meta"):
            name = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
            value = as_date(tag.get("content"))
            if name in names and value:
                result[label] = value.isoformat()
                break
    def inspect(value):
        if isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, dict):
            types = value.get('@type', [])
            types = [types] if isinstance(types, str) else types
            for key, target in (("datePublished", "published_at"), ("dateModified", "updated_at")) if any(t in {'Article', 'NewsArticle', 'BlogPosting', 'ScholarlyArticle'} for t in types) else []:
                date = as_date(value.get(key))
                if date:
                    result.setdefault(target, date.isoformat())
            if "@graph" in value:
                inspect(value["@graph"])
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            inspect(json.loads(tag.string or ""))
        except (ValueError, TypeError):
            continue
    return result


def snapshot(source, item, html, text, final_url=None, corroboration=False):
    identity(source, final_url or item["url"])
    report = assessment(source, item, corroboration=corroboration)
    if not report["auto_eligible"]:
        raise ValueError(report["note"])
    dates = page_dates(html)
    original = as_date(dates.get("published_at"))
    feed_date = as_date(item.get("published_at"))
    rules = policy()
    if original:
        if original > datetime.now(timezone.utc) + timedelta(minutes=10):
            raise ValueError('原文发布时间在未来，需人工复核')
        if abs((original - feed_date).total_seconds()) > rules["page_feed_date_tolerance_hours"] * 3600:
            raise ValueError("原文与 RSS 发布时间冲突，阻止旧闻冒充新消息")
        if datetime.now(timezone.utc) - original > timedelta(hours=report["max_age_hours"]):
            raise ValueError("原文初次发布时间已过期，更新时间不能当作新事件时间")
    return {**report, "url": final_url or item["url"], "published_at": item["published_at"], **dates,
            "body_hash": hashlib.sha256(text.encode()).hexdigest(), "body_length": len(text), "checked_at": now(),
            "page_date_verified": bool(original), "independent_verification": False,
            'date_precision': item.get('date_precision', 'timestamp'),
            "warnings": ([] if original else ["原文未提供可解析的带时区日期，依据订阅或列表日期，需人工复核"])
                        + (["官方列表只提供日期，精度为天，不当作准确发布时间"] if item.get('date_precision') == 'day' else [])
                        + (["同行评审状态未核实，不能当作已验证结论"] if source.get("kind") == "paper" else ["机构公告或媒体报道不能独立证明性能宣传"])}


def validate_claims(article, evidence, report):
    if not report or report.get("state") == "demo":
        return
    if any(f.get("claim_type") == "independently_verified" for f in evidence["facts"]):
        raise ValueError("当前只有单源证据，不能标记为独立验证")
    if re.search(r"全球第一|全面碾压|彻底取代|已被证实|科学家证实", article["title"]):
        raise ValueError("标题含证据不足的确定性或排名断言，需要编辑核实")
    if report.get("tier") == "research" and not re.search(r"同行评审|预印本|论文作者|研究团队", article["markdown"]):
        raise ValueError("论文稿必须交代作者归属或同行评审状态")
