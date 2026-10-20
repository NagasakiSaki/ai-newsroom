import hashlib
import json
from urllib.parse import urlsplit

from .db import now
from .handoff import build_handoff
from .models import content_hash
from .render import validate_article


class ManualPublication:
    def refresh_local_handoff(self, draft):
        """Re-render saved material without any network/model/WeChat request."""
        validate_article(draft['article'], draft['evidence'])
        if draft.get('review_hash') != content_hash(draft['article']):
            raise ValueError('当前正文尚未完成程序检查')
        fingerprint = self.final_hash(draft) + (draft.get('approved_hash') or '') + self.settings.delivery_mode
        previous = draft.get('delivery') or {}
        package = self.settings.data_dir / 'exports' / draft['id'] / f"v{draft['revision']}" / 'handoff.zip'
        if previous.get('fingerprint') != fingerprint or not package.exists():
            draft['delivery'] = {**previous, **build_handoff(self.settings.data_dir, draft), 'fingerprint': fingerprint, 'target': self.settings.delivery_mode}
            self.store.put('drafts', draft)
        return draft

    def seal_checks(self, draft):
        """Bind completed local checks to this exact article/evidence/layout/source snapshot."""
        draft['generation_check'] = {'fingerprint': self.final_hash(draft), 'checked_at': now()}

    def final_hash(self, draft):
        payload = {"article": draft["article"], "evidence": draft["evidence"], "news_url": draft["news_url"], "render_version": 4,
                   'author': draft.get('author') or 'AI 编辑部'}
        report = draft.get('source_snapshot') or {}
        payload['source'] = {k: report.get(k) for k in ('body_hash', 'published_at', 'tier', 'url', 'warnings', 'date_precision', 'page_date_verified', 'organization')}
        crosscheck = draft.get('crosscheck') or {}
        payload['crosscheck'] = {k: crosscheck.get(k) for k in ('state', 'summary', 'warnings', 'has_primary', 'supporting_organizations')}
        payload['crosscheck']['sources'] = [{k: row.get(k) for k in ('story_id', 'url', 'source', 'organization', 'tier', 'same_event', 'confidence', 'relationship', 'comparisons', 'reason')} | {'body_hash': row.get('snapshot', {}).get('body_hash')} for row in crosscheck.get('sources', [])]
        if draft.get('visuals'):
            payload['visuals'] = {key: draft['visuals'].get(key) for key in ('plan', 'assets', 'article_hash')}
        if draft.get('editorial_plan'):
            payload['editorial_plan'] = draft['editorial_plan']
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def deliver(self, draft_id, revision, allow_cached_source=False):
        with self.lock:
            draft = self.store.get("drafts", draft_id)
            if draft["revision"] != revision or draft["status"] not in {"pending_human", "ready_to_publish"}:
                return draft
            if self.settings.fast_mode and self.settings.review_mode != 'strict':
                self.verify_source(draft, allow_cached=allow_cached_source)
            elif allow_cached_source and self.settings.delivery_mode == 'local':
                self.verify_source(draft, force=not self.settings.fast_mode, allow_cached=True)
            else:
                self.verify_source(draft, force=True)
            validate_article(draft["article"], draft["evidence"])
            if draft.get("review_hash") != content_hash(draft["article"]):
                raise ValueError("稿件与 AI 审核版本不一致，请重新审核")
            self.seal_checks(draft)
            snapshot = self.final_hash(draft)
            previous = draft.get("delivery") or {}
            fingerprint = snapshot + (draft.get("approved_hash") or "") + self.settings.delivery_mode
            package = self.settings.data_dir / "exports" / draft_id / f"v{revision}" / "handoff.zip"
            if previous.get("fingerprint") == fingerprint and package.exists():
                self.store.put('drafts', draft)
                return draft
            draft["delivery"] = {**build_handoff(self.settings.data_dir, draft), "fingerprint": fingerprint, "target": self.settings.delivery_mode}
            self.store.put("drafts", draft)
            # 启用自动发布时，草稿由发布状态机创建，交稿只生成本地包，避免同一文章出现两份草稿。
            if self.settings.delivery_mode == "wechat_draft" and not previous.get("wechat_attempted") and not self.publication_rules()["enabled"]:
                draft["delivery"].update(wechat_attempted=True, wechat_state="needs_check")
                self.store.put("drafts", draft)
                try:
                    if self.settings.demo or draft["demo"]:
                        raise ValueError("虚构演示稿只交付本地文件，禁止同步到真实微信草稿箱")
                    # 只创建草稿，不存在正式发表调用。网络结果不明时保留本地交稿包，不自动重试。
                    draft["delivery"]["media_id"] = self.wechat.create_draft(draft)
                    draft["delivery"]["wechat_state"] = "ready"
                except Exception as exc:
                    draft["delivery"].update(wechat_state="needs_check", error=self.safe_error(exc))
            elif previous.get("wechat_attempted"):
                for key in ("wechat_attempted", "media_id", "wechat_state", "error"):
                    if key in previous:
                        draft["delivery"][key] = previous[key]
            elif self.publication_rules()["enabled"]:
                draft["delivery"].update(wechat_state="delegated",
                                         wechat_note="自动发布已启用：交稿只生成本地包，微信草稿与发表由发布状态机处理")
            self.store.put("drafts", draft)
            self.store.event(draft_id, "handoff", f"v{revision} 交稿包已生成，等待人工审核与发表")
            return draft

    def approve(self, draft_id, revision, reviewer, confirmed, manual=True, enqueue_publication=True):
        with self.lock:
            draft = self.store.get("drafts", draft_id)
            if draft["revision"] != revision or draft["status"] != "pending_human":
                raise ValueError("只有当前版本通过 AI 审核后才能人工确认")
            if not confirmed or not reviewer.strip():
                raise ValueError("请填写审核人并确认最终审核内容")
            if draft.get("review_hash") != content_hash(draft["article"]):
                raise ValueError("正文与 AI 审核版本不一致")
            self.verify_source(draft)
            validate_article(draft['article'], draft['evidence'])
            self.seal_checks(draft)
            draft.update(status="ready_to_publish", approved_hash=self.final_hash(draft), approved_revision=revision,
                         approved_by=reviewer.strip(), approved_at=now(), manual_publication=manual)
            self.store.put("drafts", draft)
            self.store.event(draft_id, "approved", f"{reviewer.strip()} 确认版本 v{revision}；是否自动发布由发布设置决定")
            result = self.deliver(draft_id, revision)
            rules = self.publication_rules()
            if enqueue_publication and rules["enabled"] and rules["mode"] in {"auto", "draft"}:
                try:
                    self.create_publication(draft_id, revision, trigger="auto_after_approval")
                except (ValueError, KeyError) as error:
                    self.store.event(draft_id, "publish_blocked", "自动发布未启动：" + str(error))
            return result

    def reject(self, draft_id, revision, note):
        with self.lock:
            draft = self.store.get("drafts", draft_id)
            if draft["revision"] != revision or draft["status"] in {"manual_published", "published", "submitted", "publish_unknown"}:
                raise ValueError("版本已改变或已经发表，无法退回")
            self.assert_editable(draft_id)
            draft.update(status="rejected", approved_hash=None, rejection=note)
            self.store.put("drafts", draft)
            self.store.event(draft_id, "rejected", note)
            return draft

    def auto_approve_problems(self, draft):
        """自动终审必须真的过复审线：advisory 不拦人工交稿，但不能拿来自动放行。"""
        from .config import editorial
        review = draft.get('review') or {}
        mode = review.get('mode') or 'off'
        if mode == 'off':
            return []
        problems = []
        if review.get('error'):
            problems.append('复审服务未完成：' + str(review.get('error'))[:60])
        issues = [row for row in review.get('issues') or [] if row.get('severity') in {'blocker', 'major'}]
        if issues:
            problems.append('复审有 ' + str(len(issues)) + ' 个 blocker/major：'
                            + '、'.join(str(row.get('category', '')) for row in issues[:3]))
        score = review.get('score')
        minimum = editorial().get('review_min_score', 80)
        if not isinstance(score, (int, float)):
            problems.append('缺少复审分数')
        elif score < minimum:
            problems.append('复审分数 ' + str(score) + ' 低于 ' + str(minimum))
        return problems

    def auto_approve(self, draft_id, revision):
        """全自动模式：复审通过才视为审定。默认关闭，演示稿永不自动审定。"""
        rules = self.publication_rules()
        if not rules.get('auto_approve'):
            return None
        self.controls.checkpoint()
        draft = self.store.get('drafts', draft_id)
        if draft.get('demo') or draft.get('revision') != revision or draft.get('status') != 'pending_human':
            return None
        problems = self.auto_approve_problems(draft)
        if problems:
            self.store.event(draft_id, 'auto_approve_blocked', '自动终审未通过：' + '；'.join(problems) + '；需人工确认')
            return None
        try:
            mode = (draft.get('review') or {}).get('mode')
            actor = ('程序检查后自动审定（未AI复审，未人工复核）' if mode == 'off' else
                     '自动审定（建议复审不拦稿，未人工复核）' if mode == 'advisory' else 'AI 自动终审（未人工复核）')
            return self.approve(draft_id, revision, actor, True, manual=False)
        except (ValueError, KeyError) as error:
            self.store.event(draft_id, 'auto_approve_failed', '自动终审未通过：' + str(error)[:300])
            return None

    def record_publication(self, draft_id, revision, url, reviewer):
        with self.lock:
            draft = self.store.get("drafts", draft_id)
            if draft["revision"] != revision or draft["status"] != "ready_to_publish":
                raise ValueError("请先确认当前版本已完成最终审核")
            if draft["demo"]:
                raise ValueError("演示内容不能登记为真实发表文章")
            self.assert_editable(draft_id)
            if self.final_hash(draft) != draft.get("approved_hash"):
                raise ValueError("稿件已改变，请重新人工审核")
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname != "mp.weixin.qq.com" or parts.username or parts.password or parts.port not in {None, 443} or not parts.path.strip("/"):
                raise ValueError("请填写 HTTPS 微信公众号文章链接")
            if not reviewer.strip():
                raise ValueError("请填写记录人")
            draft.update(status="manual_published", published_url=url, published_at=now(), publication_recorded_by=reviewer.strip())
            self.store.put("drafts", draft)
            self.store.event(draft_id, "manual_publication", "人工登记已发表文章：" + url)
            return draft

    def publish(self, draft_id, revision):
        """兼容旧调用名：走同一条受保护的发布状态机，未启用发布时直接拒绝。"""
        return self.create_publication(draft_id, revision, trigger="manual")

    def queue_publish(self, draft_id, revision):
        return self.create_publication(draft_id, revision, trigger="manual")

    def submit_wechat_draft(self, draft_id, revision, enqueue_job=True):
        """The click confirms this version for draft storage only, never formal publication."""
        if not self.publish_lock.acquire(blocking=False):
            raise ValueError('微信任务正在执行，请等待发布记录更新后再保存草稿')
        try:
            return self._submit_wechat_draft(draft_id, revision, enqueue_job)
        finally:
            self.publish_lock.release()

    def _submit_wechat_draft(self, draft_id, revision, enqueue_job=True):
        with self.lock:
            rules = self.publication_rules()
            binding = self.publication_settings().get('target_app_id')
            if not rules['enabled'] or binding != self.settings.wechat_app_id:
                raise ValueError('请先在“发布到公众号”保存并启用当前账号、封面与署名')
            draft = self.store.get('drafts', draft_id)
            if draft.get('demo') or draft['revision'] != revision:
                raise ValueError('演示稿不可提交，或稿件版本已改变，请刷新')
            if draft['status'] == 'pending_human':
                self.approve(draft_id, revision, '一键提交草稿（用户点击确认）', True, enqueue_publication=False)
            elif draft['status'] != 'ready_to_publish':
                raise ValueError('当前稿件尚未完成生成检查，请先完成当前版本')
            return self.create_publication(draft_id, revision, trigger='one_click_draft' if enqueue_job else 'batch_draft',
                                           draft_only=True, enqueue_job=enqueue_job)
