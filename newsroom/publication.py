"""公众号文章自动发布：显式开关、持久幂等、异步结果查询与未知结果对账。

约束来自 docs/ARCHITECTURE.md：
- 只有人工确认后的当前版本才会发布；未显式启用时任何入口都不调用发布链。
- 唯一键 = 账号 + 稿件 ID + revision + approved_hash，同一键只保留一条发布记录。
- 外部请求前先把意图写库；拿到 media_id／publish_id 立即落盘。
- 超时、5xx 或响应不可解析进入“结果待核对”，只查询与对账，不自动重新提交。
- 只有平台返回成功状态才把草稿标为已发布，并保存平台返回的 article_id 与链接。
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .db import now
from .models import StrictModel, content_hash
from .render import render_article
from .source_quality import policy
from .wechat import COVER_LIMIT, COVER_TYPES, WechatAPIError, WechatError, WechatUncertain

# 官方 draft/add 与 freepublish 文档给出的上限，超限必须在提交前拦下。
TITLE_LIMIT = 32
AUTHOR_LIMIT = 16
DIGEST_LIMIT = 120
CONTENT_LIMIT = 20000
CONTENT_BYTES = 1_000_000
SOURCE_URL_LIMIT = 1024

ACTIVE = frozenset({'prepared', 'draft_created', 'submitting', 'accepted', 'publishing', 'unknown'})
RELEASED = frozenset({'published', 'failed', 'blocked', 'cancelled', 'draft_only'})
BACKOFF = (15, 30, 60, 120, 300, 600, 900, 1800)
MAX_CHECKS = 30

STATUS_TEXT = {
    'prepared': '准备中', 'draft_created': '草稿已建立', 'submitting': '正在提交发布', 'accepted': '发布已受理',
    'publishing': '平台处理中', 'published': '已发布', 'failed': '明确失败', 'blocked': '发布前检查未通过',
    'unknown': '结果待核对', 'cancelled': '已放弃', 'draft_only': '仅建草稿',
}
ATTENTION_TEXT = {
    'draft_unknown': '创建草稿的请求结果未知，尚未确认草稿是否已建立',
    'submit_unknown': '提交发布的请求结果未知，不能判定文章是否已进入发布流程',
    'interrupted': '程序在发布过程中中断，结果未知',
    'query_exhausted': '自动查询次数已达到上限，平台仍在处理或状态异常',
    'cover_upload': '封面上传结果未确认',
    'publish_disabled': '发布已关闭，排队写操作已暂停；同账号的状态查询仍可继续',
    'account_changed': '当前公众号与任务绑定账号不一致，已停止该任务的微信请求',
}
# freepublish 文档的 publish_status 枚举。
PLATFORM_TEXT = {
    0: '发布成功', 1: '发布中', 2: '原创审核未通过', 3: '常规失败', 4: '平台审核不通过',
    5: '发布成功后被用户删除', 6: '发布成功后被系统封禁',
}


def utc_after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clean(value):
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('字段不得包含控制字符')
    return value.strip()


class PublishConfig(StrictModel):
    enabled: bool = False
    mode: Literal['manual', 'draft', 'auto'] = 'manual'
    # 全自动：AI 审核通过即视为审定，不再等人工点击。默认关闭。
    auto_approve: bool = False
    author: str = Field(default='AI 编辑部', min_length=1, max_length=AUTHOR_LIMIT)
    cover_mode: Literal['auto', 'manual', 'generated'] = 'manual'
    cover_media_id: str = Field(default='', max_length=200)
    cover_path: str = Field(default='', max_length=1000)
    need_open_comment: int = Field(default=0, ge=0, le=1)
    only_fans_can_comment: int = Field(default=0, ge=0, le=1)

    @field_validator('author', 'cover_media_id', 'cover_path')
    @classmethod
    def text(cls, value):
        return clean(value)


class PublishConfigPatch(StrictModel):
    enabled: bool | None = None
    mode: Literal['manual', 'draft', 'auto'] | None = None
    auto_approve: bool | None = None
    author: str | None = Field(default=None, max_length=AUTHOR_LIMIT)
    cover_mode: Literal['auto', 'manual', 'generated'] | None = None
    cover_media_id: str | None = Field(default=None, max_length=200)
    cover_path: str | None = Field(default=None, max_length=1000)
    need_open_comment: int | None = Field(default=None, ge=0, le=1)
    only_fans_can_comment: int | None = Field(default=None, ge=0, le=1)

    @field_validator('author', 'cover_media_id', 'cover_path')
    @classmethod
    def text(cls, value):
        return clean(value) if value is not None else value


class PublicationAction(StrictModel):
    confirmed: bool = False
    note: str = Field(default='', max_length=200)


class CapabilityResult(StrictModel):
    checked_at: str
    appid_masked: str
    checks: dict
    publish_available: bool
    summary: str


class PublishStale(ValueError):
    """稿件版本或审批在发布过程中改变，必须停止。"""


class PublishPaused(ValueError):
    """开关、账号或任务状态改变；保留现有记录，不执行外部写操作。"""


def cover_problems(rules):
    if rules.get('cover_mode') in {'auto', 'generated'}:
        return []
    if rules.get('cover_media_id'):
        return []
    path = (rules.get('cover_path') or '').strip()
    if not path:
        return ['请配置封面永久素材 media_id，或本地 PNG／JPEG 封面路径']
    candidate = Path(path)
    if not candidate.is_absolute():
        return ['封面路径需要绝对路径，例如 D:/assets/cover.png']
    if not candidate.is_file():
        return ['封面文件不存在：' + path]
    if candidate.suffix.lower() not in COVER_TYPES:
        return ['封面需要 PNG／JPEG／GIF／BMP 文件']
    if candidate.stat().st_size > COVER_LIMIT:
        return ['封面文件需要小于 10MB']
    return []


def validate_wechat_article(title, author, digest, html, source_url):
    if len(title) > TITLE_LIMIT:
        raise ValueError(f'微信草稿标题上限 {TITLE_LIMIT} 字，当前 {len(title)} 字；请缩短标题后重新审核')
    if len(author) > AUTHOR_LIMIT:
        raise ValueError(f'微信作者字段上限 {AUTHOR_LIMIT} 字，当前 {len(author)} 字')
    if len(digest) > DIGEST_LIMIT:
        raise ValueError(f'微信摘要上限 {DIGEST_LIMIT} 字，当前 {len(digest)} 字')
    if len(html) >= CONTENT_LIMIT:
        raise ValueError(f'微信正文上限 {CONTENT_LIMIT} 字符，当前排版后 {len(html)} 字符；请删减正文后重新审核')
    if len(html.encode('utf-8')) >= CONTENT_BYTES:
        raise ValueError('微信正文超过 1MB，无法提交草稿')
    if source_url and len(source_url.encode('utf-8')) >= SOURCE_URL_LIMIT:
        raise ValueError('原文地址超过 1KB，不能作为 content_source_url 提交')


class Publication:
    """发布状态机混入 Engine；共享 store/settings/lock/wechat。"""

    # ---------- 配置 ----------

    def publication_settings(self):
        defaults = {**PublishConfig().model_dump(), 'target_app_id': '', 'auto_since': '',
                    'updated_at': '', 'updated_by': ''}
        if self.settings.drafts_only:
            defaults.update(mode='draft', cover_mode='auto', author='AI 编辑部')
        try:
            record = self.store.get('publish_settings', 'main')
        except KeyError:
            record = {'id': 'main', **defaults}
            self.store.put('publish_settings', record)
            return record
        # 旧版本保存的记录可能缺少新增字段；补齐后写回，避免定时任务因此崩溃。
        missing = {key: value for key, value in defaults.items() if record.get(key) is None}
        if missing:
            record.update(missing)
            self.store.put('publish_settings', record)
        return record

    def publication_rules(self):
        record = self.publication_settings()
        try:
            rules = PublishConfig.model_validate({key: record.get(key) for key in PublishConfig.model_fields}).model_dump()
            if self.settings.drafts_only:
                rules['mode'] = 'draft'
            return rules
        except ValueError:
            self.store.event('publish_settings', 'publish_config', '发布设置字段损坏，已回退默认值（发布关闭）')
            return PublishConfig().model_dump()

    def appid_masked(self):
        appid = self.settings.wechat_app_id
        if len(appid) >= 8:
            return appid[:2] + '…' + appid[-4:]
        return '已填写' if appid else '未填写'

    def publishing_problems(self, rules=None):
        rules = rules or self.publication_rules()
        problems = []
        if not (self.settings.wechat_app_id and self.settings.wechat_app_secret):
            problems.append('未保存公众号 AppID / AppSecret')
        problems.extend(cover_problems(rules))
        if not rules.get('author'):
            problems.append('请填写发布署名')
        return problems

    def publish_summary(self):
        record = self.publication_settings()
        rules = self.publication_rules()
        problems = self.publishing_problems(rules)
        if rules['enabled'] and record.get('target_app_id') != self.settings.wechat_app_id:
            problems.append('公众号 AppID 与启用发布时的账号不一致，请重新确认后开启')
        capability = None
        try:
            stored = self.store.get('integrations', 'wechat_capability')
            if stored.get('credential_fingerprint') == self.wechat_connection.fingerprint():
                capability = stored.get('result')
        except KeyError:
            pass
        return {'config': {**rules, 'updated_at': record.get('updated_at', ''), 'updated_by': record.get('updated_by', '')},
                'account': {'appid_masked': self.appid_masked(),
                            'bound': bool(record.get('target_app_id')),
                            'matched': record.get('target_app_id') == self.settings.wechat_app_id,
                            'credentials_configured': bool(self.settings.wechat_app_id and self.settings.wechat_app_secret)},
                'auto_since': record.get('auto_since', ''), 'capability': capability,
                'ready': rules['enabled'] and not problems, 'problems': problems,
                'limits': {'title': TITLE_LIMIT, 'author': AUTHOR_LIMIT, 'digest': DIGEST_LIMIT, 'content': CONTENT_LIMIT}}

    def save_publish_config(self, patch, actor='页面'):
        if not self.lock.acquire(blocking=False):
            raise ValueError('当前有操作正在进行，请稍后保存发布设置')
        try:
            current = self.publication_settings()
            updates = {key: getattr(patch, key) for key in PublishConfig.model_fields if getattr(patch, key) is not None}
            candidate = {**current, **updates}
            try:
                rules = PublishConfig.model_validate({key: candidate.get(key) for key in PublishConfig.model_fields})
            except ValueError:
                raise ValueError('发布设置字段不合法：署名需要 1–16 字，封面 media_id 与路径不能超长') from None
            candidate.update(rules.model_dump())
            if rules.enabled:
                problems = self.publishing_problems(rules.model_dump())
                if problems:
                    raise ValueError('无法启用公众号发布：' + '；'.join(problems))
                candidate['target_app_id'] = self.settings.wechat_app_id
                if rules.mode == 'auto' and current.get('mode') != 'auto':
                    candidate['auto_since'] = now()
                elif rules.mode != 'auto':
                    candidate['auto_since'] = ''
            else:
                candidate['target_app_id'] = ''
                candidate['auto_since'] = ''
            candidate['updated_at'] = now()
            candidate['updated_by'] = actor
            self.store.put('publish_settings', candidate)
            self.store.event('publish_settings', 'publish_config',
                             '发布设置已保存：' + ('启用' if rules.enabled else '关闭') + '；模式：'
                             + {'auto': '自动发布', 'draft': '仅建草稿', 'manual': '仅手动发布'}[rules.mode]
                             + '；账号：' + self.appid_masked())
            return self.publish_summary()
        finally:
            self.lock.release()

    def on_credentials_changed(self):
        """公众号 AppID 变更后关闭发布；同一账号轮换 Secret 不改变发布方式。"""
        with self.lock:
            record = self.publication_settings()
            if record.get('enabled'):
                record.update(enabled=False, target_app_id='', auto_since='', updated_at=now())
                self.store.put('publish_settings', record)
                self.store.event('publish_settings', 'publish_config', '公众号凭证已更换，自动发布已关闭，需要重新确认后开启')

    # ---------- 记录 ----------

    def publication_key(self, draft, app_id):
        material = f'{app_id}\0{draft["id"]}\0{draft["revision"]}\0{draft.get("approved_hash") or ""}'
        return hashlib.sha256(material.encode()).hexdigest()[:40]

    def get_publication(self, publication_id):
        return self.store.get('publications', publication_id)

    def publication_for_draft(self, draft_id, revision=None):
        rows = [row for row in self.store.all('publications') if row.get('draft_id') == draft_id]
        if revision is not None:
            rows = [row for row in rows if row.get('revision') == revision]
        return sorted(rows, key=lambda row: row.get('created_at', ''), reverse=True)

    def active_publication(self, draft_id):
        return next((row for row in self.publication_for_draft(draft_id) if row.get('status') in ACTIVE), None)

    def assert_editable(self, draft_id):
        active = self.active_publication(draft_id)
        if active:
            raise ValueError('当前版本正在发布或发布结果待核对（' + STATUS_TEXT.get(active['status'], active['status'])
                             + '），暂不能修改；请先在“发布到公众号”页面完成对账或放弃该记录')

    def release_problems(self, draft, revision):
        problems = []
        if draft.get('demo'):
            problems.append('虚构演示稿禁止发布到真实公众号')
        if draft.get('status') != 'ready_to_publish':
            problems.append('只有人工确认后的当前版本可以发布；当前状态：' + STATUS_TEXT.get(draft.get('status'), draft.get('status')))
        if draft.get('revision') != revision:
            problems.append('稿件版本已更新，请刷新后重试')
        approved = draft.get('approved_hash')
        if not approved:
            problems.append('缺少人工确认审批，请先完成最终审核')
        else:
            try:
                if approved != self.final_hash(draft):
                    problems.append('稿件、证据或来源信息已改变，审批已失效，请重新人工确认')
            except (KeyError, TypeError):
                problems.append('稿件快照不完整，无法校验审批')
        if draft.get('approved_revision') not in (None, draft.get('revision')):
            problems.append('审批版本与当前版本不一致')
        if draft.get('review_hash') != content_hash(draft['article']):
            problems.append('正文与 AI 审核版本不一致，请重新审核')
        crosscheck = draft.get('crosscheck') or {}
        if not draft.get('demo'):
            acceptable = {'crosschecked'}
            if policy().get('allow_primary_announcement_only', False):
                acceptable.add('primary_announcement')
            if self.settings.review_mode != 'strict' and policy().get('allow_trusted_media_only', False):
                acceptable.add('media_report')
            passed = bool(crosscheck.get('passed')) and (
                not policy()['require_corroboration'] or crosscheck.get('state') in acceptable)
            if not passed:
                problems.append('缺少通过的多源对照报告，不能公开发布')
            if not draft.get('source_snapshot'):
                problems.append('缺少来源快照，不能公开发布')
        return problems

    def create_publication(self, draft_id, revision, trigger='manual', draft_only=False, enqueue_job=True):
        with self.lock:
            draft_only = draft_only or self.settings.drafts_only
            rules = self.publication_rules()
            if not rules['enabled']:
                raise ValueError('公众号自动发布未启用；请先在“发布到公众号”页面开启并确认账号、封面与署名')
            record = self.publication_settings()
            if record.get('target_app_id') != self.settings.wechat_app_id:
                raise ValueError('公众号 AppID 与启用发布时的账号不一致，请重新确认发布设置')
            draft = self.store.get('drafts', draft_id)
            key = self.publication_key(draft, record['target_app_id'])
            try:
                existing = self.store.get('publications', key)
            except KeyError:
                existing = None
            if existing:
                if draft_only:
                    if (existing.get('publish_id') or existing.get('intent') == 'submit'
                            or existing.get('attention') == 'submit_unknown'
                            or existing['status'] in {'submitting', 'accepted', 'publishing', 'published'}):
                        raise ValueError('该版本已进入正式发布，无法改为仅存草稿；请查看发布记录')
                    if existing['status'] == 'draft_only':
                        return existing
                    if existing['status'] == 'unknown':
                        # Reconciliation may find the draft later; it must still stop before formal publish.
                        existing.update(target_mode='draft', manual_submit=False)
                        self.store.put('publications', existing)
                        return existing
                    if existing['status'] in {'failed', 'blocked', 'cancelled'} and not existing.get('media_id') and not existing.get('intent'):
                        problems = self.release_problems(draft, revision)
                        if problems:
                            raise ValueError('；'.join(problems))
                        existing.update(status='prepared', target_mode='draft', manual_submit=False, error='', attention='',
                                        errcode=None, updated_at=now())
                        self.store.put('publications', existing)
                        if enqueue_job:
                            self.enqueue('publish', key, trigger=trigger)
                        return existing
                    if existing['status'] in {'prepared', 'draft_created'} and not existing.get('intent') and not existing.get('publish_id'):
                        existing.update(target_mode='draft', manual_submit=False)
                        self.store.put('publications', existing)
                        return existing
                if existing['status'] in ACTIVE:
                    return existing
                raise ValueError('当前版本已有发布记录（' + STATUS_TEXT.get(existing['status'], existing['status'])
                                 + '）；请在发布记录中查看原因后选择重试或放弃，系统不会自动重复提交')
            problems = self.release_problems(draft, revision)
            if problems:
                blocked = {'id': key, 'draft_id': draft_id, 'revision': draft.get('revision'), 'approved_hash': draft.get('approved_hash') or '',
                           'account': record['target_app_id'], 'status': 'blocked', 'error': '发布前检查未通过：' + '；'.join(problems),
                           'created_at': now(), 'updated_at': now(), 'attempt': 0, 'trigger': trigger}
                self.store.put('publications', blocked)
                self.store.event(draft_id, 'publish_blocked', blocked['error'])
                raise ValueError(blocked['error'])
            other = self.active_publication(draft_id)
            if other and other['id'] != key:
                raise ValueError('该稿件已有另一条进行中的发布记录，请先完成或放弃它')
            publication = {'id': key, 'draft_id': draft_id, 'revision': draft['revision'],
                           'approved_hash': draft['approved_hash'], 'account': record['target_app_id'],
                           'status': 'prepared', 'created_at': now(), 'updated_at': now(), 'trigger': trigger,
                           'attempt': 0, 'check_count': 0, 'intent': '', 'error': ''}
            if draft_only:
                publication['target_mode'] = 'draft'
            self.store.put('publications', publication)
            self.store.event(draft_id, 'publish_queued', f'已排队发布 v{draft["revision"]}；外部请求前会再次校验审批与来源')
            if enqueue_job:
                self.enqueue('publish', key, trigger=trigger)
            return publication

    # ---------- 状态机 ----------

    def run_publication(self, publication_id):
        publication = self.store.get('publications', publication_id)
        if publication['status'] in RELEASED:
            return publication
        if not self.publish_lock.acquire(blocking=False):
            # 另一个微信操作正在进行；由 publish_tick 重新排队，不会重复提交。
            return publication
        try:
            publication = self.store.get('publications', publication_id)
            if not self.execution_ready(publication, write=True):
                return publication
            if publication['status'] == 'prepared':
                publication = self.prepare_publication(publication)
                if (self.settings.drafts_only and publication['status'] == 'failed' and not publication.get('media_id')
                        and publication.get('errcode') in {45002, 45003, 45004, 45005, 40007} and not publication.get('format_retry')):
                    publication.update(status='prepared', intent='', target_mode='draft', format_retry=1, error='')
                    self.store.put('publications', publication)
                    self.store.event(publication['draft_id'], 'draft_format_retry', '平台明确拒绝格式：本地调整后重试一次，不调用文字模型')
                    publication = self.prepare_publication(publication)
            if publication['status'] == 'draft_created':
                if publication.get('target_mode') == 'draft' or (self.publication_rules()['mode'] == 'draft' and not publication.get('manual_submit')):
                    # 仅建草稿：账号没有发布权限或先人工核对时使用。
                    publication.update(status='draft_only', finished_at=now(), next_check_at='', intent='', updated_at=now())
                    self.store.put('publications', publication)
                    self.store.event(publication['draft_id'], 'publish_draft_only',
                                     '已创建公众号草稿；当前为仅建草稿模式，请在后台核对后发布或改用自动发布')
                    return publication
                publication = self.submit_publication(publication)
            return publication
        finally:
            self.publish_lock.release()

    def _guard(self, draft, publication):
        if draft.get('revision') != publication.get('revision') or (draft.get('approved_hash') or '') != (publication.get('approved_hash') or ''):
            raise PublishStale('稿件版本或审批在排队期间改变，发布已停止')
        if draft.get('status') != 'ready_to_publish':
            raise PublishStale('稿件当前状态已不是“待发表”，发布已停止')

    def execution_ready(self, publication, write=False):
        """查询只核对账号；写操作还核对当前开关与启用时绑定。"""
        with self.lock:
            current = self.store.get('publications', publication['id'])
            if current['status'] != publication['status']:
                publication.clear()
                publication.update(current)
                return False
            reason = ''
            account = publication.get('account')
            if not account or account != self.settings.wechat_app_id:
                reason = 'account_changed'
            elif write:
                rules = self.publication_rules()
                binding = self.publication_settings().get('target_app_id')
                if not rules['enabled']:
                    reason = 'publish_disabled'
                elif binding != account:
                    reason = 'account_changed'
            if reason:
                if publication.get('attention') != reason:
                    changes = dict(attention=reason, error=ATTENTION_TEXT[reason], updated_at=now())
                    publication.update(changes)
                    current.update(changes)
                    self.store.put('publications', current)
                    self.store.event(publication['draft_id'], 'publish_paused', ATTENTION_TEXT[reason])
                return False
            if publication.get('attention') in {'publish_disabled', 'account_changed'}:
                # 未知结果的对账分支依赖 intent / media_id；暂停时不清除这些证据。
                changes = dict(attention='', error='', updated_at=now())
                publication.update(changes)
                current.update(changes)
                self.store.put('publications', current)
            return True

    def external_write(self, publication, request, *args, intent='', status=None):
        self.controls.checkpoint()
        # 与开关／凭证保存共用锁：复核和发起请求之间不能切换账号或关闭开关。
        # 已经发出的请求无法撤回；开关关闭后，后续请求会停在这里。
        with self.lock:
            current = self.store.get('publications', publication['id'])
            if current['status'] != publication['status'] or not self.execution_ready(publication, write=True):
                raise PublishPaused()
            self._guard(self.store.get('drafts', publication['draft_id']), publication)
            if intent:
                self.intent(publication, intent, status=status)
            return request(*args)

    def prepare_publication(self, publication):
        draft = self.store.get('drafts', publication['draft_id'])
        try:
            self._guard(draft, publication)
        except PublishStale as error:
            return self.finish_publication(publication, 'failed', error=str(error))
        rules = self.publication_rules()
        if publication.get('format_retry'):
            rules = {**rules, '_format_retry': True, '_cover_refresh': True}
        try:
            # 公开发布前强制复读原文与对照来源；失败会撤销审批并停止。
            self.verify_source(draft, force=not (self.settings.fast_mode and self.settings.review_mode != 'strict'
                                                and (publication.get('target_mode') == 'draft' or rules['mode'] == 'draft')))
        except ValueError as error:
            return self.finish_publication(publication, 'blocked', error=str(error))
        draft = self.store.get('drafts', publication['draft_id'])
        try:
            self._guard(draft, publication)
            cover = (self.external_write(publication, self.cover_media_id, rules, draft)
                     if rules.get('cover_mode') in {'auto', 'generated'} else self.external_write(publication, self.cover_media_id, rules))
            # 先检查微信字段限制，再上传正文插图，避免超长稿件留下无用素材。
            self.article_payload(draft, rules, cover)
            image_urls = {}
            from .editorial_team import asset_file, original_asset_file
            for asset in (draft.get('visuals') or {}).get('assets', []):
                path = asset_file(self.settings.data_dir, asset)
                if asset.get('original_file'):
                    original_asset_file(self.settings.data_dir, asset)
                key = hashlib.sha256((publication['account'] + '\0' + asset['sha256']).encode()).hexdigest()
                try:
                    image_urls[asset['id']] = self.store.get('inline_images', key)['url']
                except KeyError:
                    url = self.external_write(publication, self.wechat.upload_inline_image, path)
                    if not isinstance(url, str) or not url.startswith('http'):
                        raise WechatError('微信正文插图上传未返回有效 URL')
                    image_urls[asset['id']] = url
                    self.store.put('inline_images', {'id': key, 'account': publication['account'], 'url': url, 'sha256': asset['sha256']})
            payload = self.article_payload(draft, rules, cover, image_urls=image_urls)
        except PublishPaused:
            return self.store.get('publications', publication['id'])
        except PublishStale as error:
            return self.finish_publication(publication, 'failed', error=str(error))
        except WechatAPIError as error:
            return self.finish_publication(publication, 'blocked', errcode=error.errcode, error=str(error))
        except (ValueError, WechatError) as error:
            return self.finish_publication(publication, 'blocked', error=self.safe_error(error))
        publication['snapshot'] = {'title': payload['title'], 'digest': payload['digest'], 'author': payload['author'],
                                   'news_url': payload['content_source_url'], 'revision': draft['revision'],
                                   'approved_hash': draft.get('approved_hash'), 'cover': cover,
                                   'html_sha256': hashlib.sha256(payload['content'].encode()).hexdigest(),
                                   'content_chars': len(payload['content'])}
        try:
            media_id = self.external_write(publication, self.wechat.add_draft, payload, intent='draft_add')
        except PublishPaused:
            return self.store.get('publications', publication['id'])
        except PublishStale as error:
            return self.finish_publication(publication, 'failed', error=str(error))
        except WechatAPIError as error:
            return self.finish_publication(publication, 'failed', errcode=error.errcode, error=str(error))
        except WechatUncertain as error:
            return self.unknown_publication(publication, 'draft_unknown', str(error))
        except WechatError as error:
            return self.finish_publication(publication, 'failed', error=str(error))
        publication.update(status='draft_created', media_id=media_id, draft_created_at=now(), intent='', error='', updated_at=now())
        self.store.put('publications', publication)
        self.store.event(publication['draft_id'], 'publish_draft', '已创建公众号草稿并保存 media_id，准备提交发布')
        return publication

    def submit_publication(self, publication):
        if self.settings.drafts_only:
            return self.finish_publication(publication, 'blocked', error='当前工作流止于建立草稿，不提供正式发表')
        draft = self.store.get('drafts', publication['draft_id'])
        try:
            self._guard(draft, publication)
        except PublishStale as error:
            return self.finish_publication(publication, 'failed', error=str(error))
        if not publication.get('media_id'):
            return self.finish_publication(publication, 'failed', error='缺少草稿 media_id，无法提交发布')
        try:
            data = self.external_write(publication, self.wechat.submit_publish, publication['media_id'],
                                       intent='submit', status='submitting')
        except PublishPaused:
            return self.store.get('publications', publication['id'])
        except PublishStale as error:
            return self.finish_publication(publication, 'failed', error=str(error))
        except WechatAPIError as error:
            if error.errcode == 40001:
                self.wechat.token, self.wechat.expires = '', 0
            return self.finish_publication(publication, 'failed', errcode=error.errcode, error=str(error))
        except WechatUncertain as error:
            return self.unknown_publication(publication, 'submit_unknown', str(error))
        except WechatError as error:
            return self.finish_publication(publication, 'failed', error=str(error))
        publish_id = data.get('publish_id')
        if not isinstance(publish_id, (str, int)) or not str(publish_id).strip():
            return self.unknown_publication(publication, 'submit_unknown', '发布提交未返回 publish_id，结果未确认')
        publication.update(status='accepted', publish_id=str(publish_id), msg_data_id=str(data.get('msg_data_id') or ''),
                           submitted_at=now(), intent='', error='', check_count=0, attention='', manual_submit=False,
                           next_check_at=utc_after(BACKOFF[0]), updated_at=now())
        self.store.put('publications', publication)
        self.store.event(publication['draft_id'], 'publish_submitted', '平台已受理发布任务 publish_id=' + str(publish_id) + '；提交成功不等于已经上线')
        return publication

    def check_publication(self, publication_id):
        publication = self.store.get('publications', publication_id)
        if publication['status'] not in {'submitting', 'accepted', 'publishing', 'unknown'}:
            return publication
        if not self.publish_lock.acquire(blocking=False):
            return publication
        try:
            with self.lock:
                publication = self.store.get('publications', publication_id)
                if not self.execution_ready(publication):
                    return publication
                if publication['status'] in {'unknown', 'submitting'}:
                    publication = self.reconcile_publication(publication)
                if publication.get('publish_id') and publication['status'] in {'accepted', 'publishing', 'unknown'}:
                    publication = self.poll_publication(publication)
                return publication
        finally:
            self.publish_lock.release()

    def poll_publication(self, publication):
        try:
            data = self.wechat.query_publish(publication['publish_id'])
        except WechatAPIError as error:
            if error.errcode == 40001:
                self.wechat.token, self.wechat.expires = '', 0
            return self.schedule_check(publication, '查询发布状态失败：' + str(error))
        except WechatUncertain as error:
            return self.schedule_check(publication, str(error))
        status = data.get('publish_status')
        if type(status) is not int:
            return self.schedule_check(publication, '平台返回的发布状态无法识别')
        if status == 0:
            return self.mark_published(publication, data)
        if status == 1:
            return self.schedule_check(publication, '平台仍在处理发布任务', status='publishing')
        if status in {2, 3, 4}:
            return self.finish_publication(publication, 'failed', platform_status=status,
                                           fail_idx=data.get('fail_idx') or [], error='平台发布未通过：' + PLATFORM_TEXT[status])
        if status in {5, 6}:
            return self.mark_published(publication, data, warning=PLATFORM_TEXT[status])
        return self.schedule_check(publication, '平台返回未知发布状态：' + str(status))

    def mark_published(self, publication, data, warning=''):
        article_id = str(data.get('article_id') or '')
        url = ''
        for item in ((data.get('article_detail') or {}).get('item') or []):
            if item.get('article_url'):
                url = str(item['article_url'])
                break
        if not url and article_id:
            try:
                article = self.wechat.published_article(article_id)
                items = article.get('news_item') or []
                if items and items[0].get('url'):
                    url = str(items[0]['url'])
            except WechatError:
                pass
        with self.lock:
            publication = self.store.get('publications', publication['id'])
            publication.update(status='published', article_id=article_id, article_url=url, finished_at=now(),
                               next_check_at='', attention='', error='', platform_warning=warning, updated_at=now())
            self.store.put('publications', publication)
            draft = self.store.get('drafts', publication['draft_id'])
            if draft.get('revision') == publication.get('revision'):
                draft.update(status='published', published_url=url, published_at=now(),
                             publication_id=publication['id'], published_by='公众号接口')
                self.store.put('drafts', draft)
        detail = '平台已发布'
        if warning:
            detail += '；' + warning
        if not url:
            detail += '；平台未返回文章链接，请在公众号后台核对'
        self.store.event(publication['draft_id'], 'publish_success', detail)
        return publication

    def finish_publication(self, publication, status, **fields):
        publication.update({**fields, 'status': status, 'finished_at': now(), 'next_check_at': '', 'intent': '',
                           'updated_at': now()})
        self.store.put('publications', publication)
        self.store.event(publication['draft_id'], 'publish_' + status, publication.get('error') or STATUS_TEXT.get(status, status))
        return publication

    def unknown_publication(self, publication, attention, message):
        publication.update(status='unknown', attention=attention, error=message[:500], updated_at=now(),
                           check_count=0, next_check_at=utc_after(30), resumable=False)
        self.store.put('publications', publication)
        self.store.event(publication['draft_id'], 'publish_unknown', ATTENTION_TEXT.get(attention, message))
        return publication

    def schedule_check(self, publication, message, status=None):
        count = int(publication.get('check_count') or 0) + 1
        if count > MAX_CHECKS:
            publication.update(status='unknown', attention='query_exhausted', error=message[:500],
                               finite_checks=count, next_check_at='', updated_at=now())
            self.store.put('publications', publication)
            self.store.event(publication['draft_id'], 'publish_unknown', '自动查询次数已用尽：' + message[:200] + '；请到公众号后台核对')
            return publication
        delay = BACKOFF[min(count - 1, len(BACKOFF) - 1)]
        publication.update(status=status or publication.get('status') or 'accepted', check_count=count,
                           last_checked_at=now(), next_check_at=utc_after(delay), error=message[:500], updated_at=now())
        self.store.put('publications', publication)
        return publication

    # ---------- 未知结果对账 ----------

    def reconcile_publication(self, publication):
        intent = publication.get('intent') or ''
        if intent == 'draft_add' and not publication.get('media_id'):
            return self.reconcile_draft(publication)
        if publication.get('media_id') and not publication.get('publish_id'):
            return self.reconcile_submit(publication)
        if publication.get('publish_id'):
            return publication
        return self.reconcile_draft(publication)

    def reconcile_draft(self, publication):
        snapshot = publication.get('snapshot') or {}
        title, author = snapshot.get('title') or '', snapshot.get('author') or ''
        intended_at = parse_time(publication.get('intent_at'))
        try:
            # no_content=0：对账要用标题与署名，平台默认也会返回 content。
            listing = self.wechat.draft_list(0, 20, 0)
        except WechatError as error:
            return self.schedule_check(publication, '对账时读取草稿箱失败：' + str(error))
        matches = []
        for item in listing.get('item') or []:
            items = ((item.get('content') or {}).get('news_item') or [])
            if len(items) != 1:
                continue
            first = items[0]
            html = first.get('content')
            updated = item.get('update_time')
            recent = (intended_at is not None and type(updated) in {int, float}
                      and intended_at.timestamp() - 5 <= updated <= datetime.now(timezone.utc).timestamp() + 5)
            if (first.get('title') == title and first.get('author', '') == author
                    and first.get('digest', '') == snapshot.get('digest', '')
                    and first.get('content_source_url', '') == snapshot.get('news_url', '')
                    and isinstance(html, str) and snapshot.get('html_sha256')
                    and hashlib.sha256(html.encode()).hexdigest() == snapshot['html_sha256'] and recent):
                matches.append(item.get('media_id'))
        matches = [value for value in matches if value]
        if len(matches) == 1:
            publication.update(status='draft_created', media_id=matches[0], intent='', error='', attention='',
                               reconciled='draft_list_match', updated_at=now())
            self.store.put('publications', publication)
            self.store.event(publication['draft_id'], 'publish_reconciled', '按正文哈希、元数据和请求时间在草稿箱对账到唯一草稿，继续提交发布')
            return publication
        if len(matches) > 1:
            publication.update(status='unknown', attention='draft_unknown', resumable=False,
                               error='草稿箱中有多条同名草稿，无法自动对账；请到公众号后台确认后处理', updated_at=now())
            self.store.put('publications', publication)
            return publication
        publication.update(status='unknown', attention='draft_unknown', resumable=True,
                           error='草稿箱中未找到能唯一核对正文、元数据与请求时间的草稿；请先到公众号后台核对，确认后才能重新创建', updated_at=now(),
                           next_check_at=utc_after(300))
        self.store.put('publications', publication)
        return publication

    def reconcile_submit(self, publication):
        media_id = publication.get('media_id')
        try:
            exists = self.wechat.draft_exists(media_id)
        except WechatError as error:
            return self.schedule_check(publication, '对账时检查草稿失败：' + str(error))
        # 没有 publish_id 时不能把同标题历史文章当成本次提交的成功证据。
        # 草稿存在与否也不能单独证明未发布；只允许人工后台核对后明确恢复。
        if exists:
            publication.update(status='unknown', attention='submit_unknown', resumable=True,
                               error='提交结果未知；草稿仍在草稿箱，但缺少本次 publish_id，无法自动确认是否已发布。请到公众号后台确认未发布后再重新提交',
                               updated_at=now(), next_check_at=utc_after(300))
            self.store.put('publications', publication)
            return publication
        publication.update(status='unknown', attention='submit_unknown', resumable=False,
                           error='草稿已不在草稿箱，且缺少本次 publish_id；无法自动确认发布结果，请在公众号后台核对，不要直接重新提交',
                           updated_at=now(), next_check_at=utc_after(600))
        self.store.put('publications', publication)
        return publication

    # ---------- 用户动作 ----------

    def publication_action(self, publication_id, action, body):
        with self.lock:
            if action == 'check':
                return self._publication_action(publication_id, action, body)
            if not self.publish_lock.acquire(blocking=False):
                raise ValueError('微信操作正在进行，请等待结果后再更改发布记录')
            try:
                return self._publication_action(publication_id, action, body)
            finally:
                self.publish_lock.release()

    def _publication_action(self, publication_id, action, body):
        publication = self.store.get('publications', publication_id)
        if action in {'retry', 'resume', 'submit'} and not self.execution_ready(publication, write=True):
            raise ValueError(publication['error'])
        if action == 'check':
            return self.enqueue('publish_check', publication_id, trigger='manual_check')
        if action == 'retry':
            if publication['status'] not in {'failed', 'blocked'}:
                raise ValueError('只有明确失败或未通过发布前检查的记录可以重试')
            with self.lock:
                publication = self.store.get('publications', publication_id)
                # 已建草稿的重试复用同一 media_id，避免在草稿箱里留下重复草稿。
                reuse = bool(publication.get('media_id'))
                publication.update(status='draft_created' if reuse else 'prepared', error='', attention='',
                                   finished_at='', intent='', publish_id='', check_count=0, next_check_at='',
                                   errcode=None, platform_status=None, fail_idx=[], resumable=False, updated_at=now())
                if not reuse:
                    publication['media_id'] = ''
                self.store.put('publications', publication)
                self.store.event(publication['draft_id'], 'publish_retry',
                                 '用户要求重试发布：' + ('复用已建草稿' if reuse else '重新创建草稿') + '；会重新校验版本、审批与来源')
                self.enqueue('publish', publication_id, trigger='manual_retry')
                return publication
        if action == 'resume':
            if not body.confirmed:
                raise ValueError('请先到公众号后台确认该草稿未发表，再勾选确认后重新提交')
            if publication['status'] != 'unknown' or not publication.get('resumable'):
                raise ValueError('当前记录不满足重新提交的条件：需要先对账确认草稿仍在且未发布')
            with self.lock:
                publication = self.store.get('publications', publication_id)
                reuse = bool(publication.get('media_id'))
                publication.update(status='draft_created' if reuse else 'prepared', error='', attention='', intent='',
                                   next_check_at='', updated_at=now())
                self.store.put('publications', publication)
                self.store.event(publication['draft_id'], 'publish_resume',
                                 '用户确认后台已核对：' + ('重新提交同一草稿，不重复创建' if reuse else '重新创建未确认建立的草稿'))
                self.enqueue('publish', publication_id, trigger='manual_resume')
                return publication
        if action == 'submit':
            if self.settings.drafts_only:
                raise ValueError('当前工作流只建立微信草稿，请到公众号后台人工发表')
            if not body.confirmed:
                raise ValueError('请确认将已建草稿提交发布')
            if publication['status'] != 'draft_only' or not publication.get('media_id'):
                raise ValueError('只有“仅建草稿”的记录可以手动提交发布')
            with self.lock:
                publication = self.store.get('publications', publication_id)
                publication.update(status='draft_created', manual_submit=True, target_mode='publish', updated_at=now())
                self.store.put('publications', publication)
                self.store.event(publication['draft_id'], 'publish_submit_now', '用户要求提交已建草稿发布')
                self.enqueue('publish', publication_id, trigger='manual_submit')
                return publication
        if action == 'abandon':
            if not body.confirmed:
                raise ValueError('放弃发布需要确认；结果未知时请先到公众号后台核对是否已经发表')
            if publication['status'] == 'published':
                raise ValueError('已经发布的记录不能放弃')
            with self.lock:
                publication = self.store.get('publications', publication_id)
                publication.update(status='cancelled', finished_at=now(), next_check_at='', intent='', attention='',
                                   note=body.note, error=publication.get('error') or '', updated_at=now())
                self.store.put('publications', publication)
                self.store.event(publication['draft_id'], 'publish_cancelled', '用户放弃此发布记录；稿件可重新审核后再发布')
                return publication
        raise ValueError('不支持的发布操作')

    # ---------- 周期任务 ----------

    def publish_tick(self, current=None):
        current = current or datetime.now(timezone.utc)
        rules = self.publication_rules()
        record = self.publication_settings()
        jobs = self.store.all('jobs')
        busy = {(row['kind'], row['subject']) for row in jobs if row['status'] in {'queued', 'running'}}
        for publication in self.store.all('publications'):
            status = publication.get('status')
            if status in {'prepared', 'draft_created'}:
                if not self.execution_ready(publication, write=True):
                    continue
                if ('publish', publication['id']) not in busy:
                    self.enqueue('publish', publication['id'], trigger='resume')
                continue
            if status in {'accepted', 'publishing', 'submitting', 'unknown'}:
                if not self.execution_ready(publication):
                    continue
                due = parse_time(publication.get('next_check_at'))
                if due and due <= current and ('publish_check', publication['id']) not in busy:
                    self.enqueue('publish_check', publication['id'], trigger='poll')
        if not rules['enabled'] or rules['mode'] != 'auto':
            return
        if record.get('target_app_id') != self.settings.wechat_app_id:
            return
        since = record.get('auto_since') or ''
        for draft in self.store.all('drafts'):
            if draft.get('status') != 'ready_to_publish' or not draft.get('approved_hash'):
                continue
            if since and (draft.get('approved_at') or '') < since:
                continue
            key = self.publication_key(draft, self.settings.wechat_app_id)
            try:
                self.store.get('publications', key)
                continue
            except KeyError:
                pass
            if self.active_publication(draft['id']):
                continue
            try:
                self.create_publication(draft['id'], draft['revision'], trigger='auto')
            except (ValueError, KeyError):
                continue

    # ---------- 素材与请求体 ----------

    def cover_media_id(self, rules, draft=None):
        if draft and rules.get('cover_mode') in {'auto', 'generated'}:
            from .editorial_team import asset_file
            assets = (draft.get('visuals') or {}).get('assets', [])
            preferred = (draft.get('visuals') or {}).get('cover_asset_id')
            asset = next((a for a in assets if a['id'] == preferred), None) or next(iter(assets), None)
            if not asset:
                raise WechatError('稿件尚无可用封面，请先补齐图文')
            rules = {**rules, 'cover_mode': 'manual', 'cover_media_id': '', 'cover_path': str(asset_file(self.settings.data_dir, asset))}
        if rules.get('cover_media_id'):
            return rules['cover_media_id']
        path = Path(rules.get('cover_path') or '')
        problems = cover_problems(rules)
        if problems:
            raise WechatError('；'.join(problems))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cache_key = hashlib.sha256((self.settings.wechat_app_id + '\0' + digest).encode()).hexdigest()
        if not rules.get('_cover_refresh'):
            try:
                return self.store.get('covers', cache_key)['media_id']
            except KeyError:
                pass
        media_id = self.wechat.upload_cover(path)
        self.store.put('covers', {'id': cache_key, 'account': self.settings.wechat_app_id,
                                  'sha256': digest, 'media_id': media_id, 'file': path.name,
                                  'size': path.stat().st_size, 'uploaded_at': now()})
        return media_id

    def article_payload(self, draft, rules, cover, image_urls=None):
        article = draft['article']
        html = render_article(draft, image_urls=image_urls)
        title, digest = article['title'], article['digest']
        if self.settings.drafts_only:
            from .draft_workflow import short_title, compact_html
            title = short_title(draft, 24 if rules.get('_format_retry') else 32)
            digest = re.sub(r'\s+', ' ', digest).strip()[:60 if rules.get('_format_retry') else 120]
            if len(html) >= CONTENT_LIMIT or rules.get('_format_retry'):
                html = compact_html(html)
        source_url = draft.get('news_url') or ''
        validate_wechat_article(title, rules['author'], digest, html, source_url)
        return {'article_type': 'news', 'title': title, 'author': rules['author'],
                'digest': digest, 'content': html, 'content_source_url': source_url,
                'thumb_media_id': cover, 'need_open_comment': int(rules['need_open_comment']),
                'only_fans_can_comment': int(rules['only_fans_can_comment'])}

    def intent(self, publication, intent, status=None):
        publication.update(intent=intent, intent_at=now(), attempt=int(publication.get('attempt') or 0) + 1, updated_at=now())
        if status:
            publication['status'] = status
        self.store.put('publications', publication)
        return publication

    # ---------- 接口权限检测（只读） ----------

    def capability_check(self):
        if not self.publish_lock.acquire(blocking=False):
            raise ValueError('微信操作正在进行，请稍后再检测接口权限')
        try:
            checks = {}
            token_ok = True
            try:
                self.wechat.access_token()
                checks['token'] = {'ok': True}
            except WechatAPIError as error:
                token_ok = False
                checks['token'] = {'ok': False, 'errcode': error.errcode, 'message': str(error)}
            except WechatUncertain as error:
                token_ok = False
                checks['token'] = {'ok': None, 'message': str(error)}
            except WechatError as error:
                token_ok = False
                checks['token'] = {'ok': False, 'message': str(error)}
            if token_ok:
                checks['draft'] = self.probe(lambda: {'count': self.wechat.draft_count()})
                checks['publish'] = ({'ok': None, 'message': '当前工作流仅建立草稿，不检测正式发布接口'} if self.settings.drafts_only
                                     else self.probe(lambda: {'total': int(self.wechat.published_list(0, 1).get('total_count') or 0)}))
                checks['material'] = self.probe(lambda: self.wechat.material_count())
            else:
                checks['draft'] = {'ok': None, 'message': '未取得调用凭据，未检测'}
                checks['publish'] = {'ok': None, 'message': '未取得调用凭据，未检测'}
                checks['material'] = {'ok': None, 'message': '未取得调用凭据，未检测'}
            publish = checks.get('publish') or {}
            if publish.get('ok'):
                summary = '当前账号可以读取已发布列表，说明发布接口已授权。这不代表标题、封面或内容能通过平台审核。'
            elif publish.get('errcode') == 48001:
                summary = ('当前账号没有 freepublish 发布接口权限（错误码 48001）。官方文档标注该接口仅认证公众号（企业主体）可调用；'
                           '未认证或个人主体账号需要先完成微信认证，否则只能在公众号后台人工发表。')
            elif publish.get('ok') is False:
                summary = '发布接口检测未通过：' + str(publish.get('message'))
            else:
                summary = str(publish.get('message') or '未能检测发布接口权限')
            result = {'checked_at': now(), 'appid_masked': self.appid_masked(), 'checks': checks,
                      'publish_available': bool(publish.get('ok')), 'summary': summary}
            if self.settings.drafts_only:
                result['summary'] = '微信草稿权限' + ('可用' if checks['draft'].get('ok') else '未确认，请查看草稿与素材检测结果') + '；流程只建立草稿，正式发表请在公众号后台完成。'
            self.store.put('integrations', {'id': 'wechat_capability',
                                            'credential_fingerprint': self.wechat_connection.fingerprint(),
                                            'result': result})
            return result
        finally:
            self.publish_lock.release()

    @staticmethod
    def probe(call):
        try:
            return {'ok': True, **call()}
        except WechatAPIError as error:
            return {'ok': False, 'errcode': error.errcode, 'message': str(error)}
        except WechatUncertain as error:
            return {'ok': None, 'message': str(error)}
        except WechatError as error:
            return {'ok': False, 'message': str(error)}
        except (KeyError, TypeError, ValueError):
            return {'ok': False, 'message': '接口返回结构异常'}
