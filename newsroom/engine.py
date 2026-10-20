import json
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import collect, trending
from .config import editorial, sources
from .db import Store, now
from .llm import ModelClient
from .models import content_hash
from .render import validate_article, validate_evidence
from .schedule import SchedulePlan, next_occurrence
from .wechat import Wechat, WechatError
from .actions import ManualPublication
from .sourcing import SourceControl
from .editors import initialize_editors, choose_editor, resolve_profile
from .source_quality import assessment, validate_claims, policy
from .crosscheck import CrossSource, CorroborationPending
from .connection import WechatConnection
from .publication import STATUS_TEXT, Publication
from .editorial_team import EditorialTeam
from .workflow_control import WorkflowControl, WorkflowCancelled
from .draft_workflow import DraftWorkflow

BEIJING = timezone(timedelta(hours=8))
IMMUTABLE = {"manual_published", "published", "submitted", "publish_unknown"}


class Engine(DraftWorkflow, CrossSource, SourceControl, ManualPublication, Publication, EditorialTeam):
    def __init__(self, settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "newsroom.sqlite3")
        initialize_editors(self.store)
        self.model = ModelClient(settings, self.store)
        self.wechat = Wechat(settings)
        self.wechat_connection = WechatConnection(settings, self.store)
        self.lock = threading.RLock()
        # 串行化所有发布侧微信请求：发布步骤、查询与接口权限检测互斥。
        self.publish_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.controls = WorkflowControl(self.store, self.stop_event)
        self.model.checkpoint = self.controls.checkpoint
        self.thread = None
        self.process_lock = None
        try:
            self.store.get("schedule", "main")
        except KeyError:
            plan = SchedulePlan(enabled=settings.scheduler, times=list(settings.schedule_times), interval_minutes=settings.interval_minutes).model_dump()
            self.store.put("schedule", {"id": "main", **plan, "next_run_at": next_occurrence(plan), "updated_at": now(), "timezone": "Asia/Shanghai"})

    def start(self):
        self.process_lock = (self.settings.data_dir / "worker.lock").open("a+b")
        if self.process_lock.tell() == 0:
            self.process_lock.write(b"0")
            self.process_lock.flush()
        self.process_lock.seek(0)
        try:
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.process_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.process_lock.close()
            self.process_lock = None
            raise ValueError("该数据目录已有编辑台进程运行，请只启动一个 worker") from None
        self.controls.recover()
        # 运行中断后，不盲目重放任何有外部发布副作用的动作；先对账再决定。
        for job in self.store.all("jobs"):
            if job["kind"] in {"publish", "publish_check"}:
                if job["status"] in {"queued", "running"}:
                    job.update(status="interrupted", error="程序中断；发布记录先对账，不自动重新提交")
                    self.store.put("jobs", job)
                continue
            if job["status"] in {"running", "paused"}:
                job.update(status="interrupted", error="程序中断，采编或交稿将按当前状态恢复")
                self.store.put("jobs", job)
        for batch in self.store.all('draft_batches'):
            if batch['status'] in {'running', 'paused'}:
                batch.update(status='interrupted', finished_at=now())
                self.store.put('draft_batches', batch)
        for publication in self.store.all("publications"):
            if publication.get('batch_id') and publication['status'] == 'prepared' and not publication.get('intent'):
                batch = self.store.get('draft_batches', publication['batch_id'])
                if batch['status'] in {'interrupted', 'cancelled'}:
                    self.finish_publication(publication, 'cancelled', error='批次中断；未发起建草稿请求，等待用户再次批量提交')
                    continue
            if publication["status"] in {"prepared", "draft_created"}:
                if publication.get("intent"):
                    publication.update(status="unknown", attention="interrupted", error="程序在外部请求期间中断，结果未确认",
                                       next_check_at=now(), updated_at=now())
                    self.store.put("publications", publication)
                    self.enqueue("publish_check", publication["id"], trigger="restart")
                else:
                    self.store.put("publications", publication)
                    self.enqueue("publish", publication["id"], trigger="restart")
            elif publication["status"] in {"submitting", "accepted", "publishing"}:
                publication.update(status="unknown", attention="interrupted", resumable=False,
                                   error="程序中断，发布结果未确认；先查询对账，不自动重新提交",
                                   next_check_at=now(), updated_at=now())
                self.store.put("publications", publication)
                self.enqueue("publish_check", publication["id"], trigger="restart")
        for draft in self.store.all("drafts"):
            if draft["status"] == "publishing":
                draft["status"] = "publish_unknown"
                self.store.put("drafts", draft)
            elif draft["status"] in {"approved", "queued", "exported", "demo_published", "wechat_draft"}:
                draft.update(legacy_status=draft["status"], status="pending_human", approved_hash=None, manual_publication=True)
                for key in ("approved_by", "approved_at", "approved_revision"):
                    draft.pop(key, None)
                self.store.put("drafts", draft)
        for run in self.store.all("runs"):
            if run["status"] in {"running", "paused"}:
                run.update(status="interrupted", finished_at=now())
                self.store.put("runs", run)
        for item in self.store.all("stories"):
            if item["status"] == "processing":
                has_draft = any(d["story_id"] == item["id"] for d in self.store.all("drafts"))
                item.update(status="drafted" if has_draft else "new")
                self.store.put("stories", item)
        for draft in self.store.all("drafts"):
            if draft["status"] in {"pending_human", "ready_to_publish"}:
                if self.settings.drafts_only:
                    try:
                        self.refresh_local_handoff(draft)
                    except (ValueError, OSError) as exc:
                        self.store.event(draft['id'], 'handoff_refresh', '本地排版暂未刷新：' + self.safe_error(exc))
                    continue
                expected = self.final_hash(draft) + (draft.get('approved_hash') or '') + self.settings.delivery_mode
                package = self.settings.data_dir / 'exports' / draft['id'] / f"v{draft['revision']}" / 'handoff.zip'
                if (draft.get('delivery') or {}).get('fingerprint') != expected or not package.exists():
                    self.enqueue("deliver", draft["id"], draft["revision"])
            elif draft["status"] == "needs_ai_review":
                self.enqueue("review", draft["id"], draft["revision"])
        self.thread = threading.Thread(target=self.loop, name="newsroom-worker", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.process_lock and not (self.thread and self.thread.is_alive()):
            self.process_lock.close()
            self.process_lock = None

    def enqueue(self, kind, subject="", revision=None, due_at=None, trigger="manual", schedule_slot=None, payload=None):
        with self.lock:
            for job in self.store.all("jobs"):
                if job["kind"] == kind and job["subject"] == subject and job.get("revision") == revision and job.get('payload') == payload and job["status"] in {"queued", "running", "paused"}:
                    return job
            return self.store.put("jobs", {"id": uuid.uuid4().hex, "kind": kind, "subject": subject, "revision": revision,
                                          "status": "queued", "created_at": now(), "due_at": due_at or now(),
                                          "trigger": trigger, "schedule_slot": schedule_slot, "payload": payload})

    def get_schedule(self):
        return self.store.get("schedule", "main")

    def set_schedule(self, plan):
        plan = SchedulePlan.model_validate(plan).model_dump()
        with self.lock:
            result = self.store.put("schedule", {"id": "main", **plan, "next_run_at": next_occurrence(plan), "updated_at": now(), "timezone": "Asia/Shanghai"})
            self.store.event("schedule", "schedule", "已启用采编计划" if plan["enabled"] else "已暂停采编计划")
            return result

    def schedule_tick(self, current=None):
        if self.controls.state().get('dispatch_paused'):
            return
        current = current or datetime.now(timezone.utc)
        with self.lock:
            plan = self.get_schedule()
            if not plan["enabled"] or not plan.get("next_run_at") or datetime.fromisoformat(plan["next_run_at"]) > current:
                return None
            slot = plan["next_run_at"]
            # 计划槽先落盘到任务，再推进下次时间；重启时同一槽不会重复运行。
            existing = next((j for j in self.store.all("jobs") if j.get("schedule_slot") == slot), None)
            job = existing or self.enqueue("pipeline", trigger="schedule", schedule_slot=slot)
            plan.update(last_trigger_at=current.isoformat(), next_run_at=next_occurrence(plan, current))
            self.store.put("schedule", plan)
            self.store.event(job["id"], "schedule", "定时采编已入队；离线遗漏多个时段时只补一次")
            return job

    def loop(self):
        # 后台线程不能因为某个周期任务异常而整体退出；错误写事件日志后继续下一轮。
        while not self.stop_event.is_set():
            for tick in (self.collection_tick, self.schedule_tick, self.publish_tick):
                try:
                    tick()
                except Exception as exc:
                    self.store.event('worker', 'tick_error', tick.__name__ + '：' + self.safe_error(exc))
            try:
                self.work_once()
            except Exception as exc:
                self.store.event('worker', 'tick_error', 'work_once：' + self.safe_error(exc))
            self.stop_event.wait(1)

    def work_once(self):
        with self.lock:
            paused = self.controls.state().get('dispatch_paused')
            due = [j for j in self.store.all("jobs") if j["status"] == "queued" and datetime.fromisoformat(j["due_at"]) <= datetime.now(timezone.utc)
                   and not (paused and j['kind'] in self.controls.kinds)]
            if not due:
                return False
            job = min(due, key=lambda j: j["created_at"])
            job["status"] = "running"
            self.store.put("jobs", job)
            controlled = job['kind'] in self.controls.kinds
            if controlled:
                self.controls.begin(job)
        try:
            self.controls.checkpoint()
            if job["kind"] == "pipeline":
                self.pipeline(job)
            elif job['kind'] == 'collect':
                self.collect_sources(job=job)
            elif job["kind"] == "review":
                self.review_draft(job["subject"], job["revision"], job=job)
            elif job["kind"] == "deliver":
                self.deliver(job["subject"], job["revision"], allow_cached_source=self.settings.review_mode != 'strict')
            elif job["kind"] == "publish":
                # 任务只是传输层；业务结果写在 publications 记录上，并同步到 job.result。
                job["result"] = self.run_publication(job["subject"])["status"]
            elif job["kind"] == "publish_check":
                job["result"] = self.check_publication(job["subject"])["status"]
            elif job['kind'] == 'draft_batch':
                self.run_draft_batch(job)
            elif job['kind'] == 'images':
                self.refresh_draft_images(job)
            elif job['kind'] == 'illustrations':
                self.generate_draft_illustrations(job)
            else:
                raise ValueError("不支持的任务；发布只走 /api/drafts/{id}/publish 与发布记录")
            job["status"] = "done"
        except WorkflowCancelled:
            status = 'interrupted' if self.stop_event.is_set() else 'cancelled'
            job.update(status=status, error='后续采编已停止；已发请求可能计费，已完成稿件和模型用量记录保留')
            if job.get('run_id'):
                generated = sum(d.get('run_id') == job['run_id'] for d in self.store.all('drafts'))
                if job['kind'] == 'draft_batch':
                    generated = self.store.get('draft_batches', job['subject']).get('saved', 0)
                self.finish_run({'id': job['run_id']}, status, generated=generated, message=job['error'])
            for story in self.store.all('stories'):
                if story.get('status') == 'processing':
                    story['status'] = 'drafted' if any(d['story_id'] == story['id'] for d in self.store.all('drafts')) else 'new'
                    self.store.put('stories', story)
            self.store.event(job['id'], 'workflow_control', job['error'])
        except Exception as exc:
            job.update(status="failed", error=self.safe_error(exc))
            self.store.event(job["subject"] or job["id"], "error", job["error"])
        finally:
            if controlled:
                self.controls.end()
        job["finished_at"] = now()
        if job.get('run_id'):
            job['run_status'] = self.store.get('runs', job['run_id'])['status']
        self.store.put("jobs", job)
        return True

    @staticmethod
    def safe_error(exc):
        # HTTP 库异常可能含 access_token URL，绝不原样保存。
        if isinstance(exc, (ValueError, WechatError)):
            return str(exc)[:500]
        if isinstance(exc, (AttributeError, TypeError, KeyError, NameError)):
            return f"{type(exc).__name__}：程序内部处理异常，已保留诊断位置；无需修改网络或 API 密钥"
        return f"{type(exc).__name__}：任务失败，请检查配置与网络；系统不会盲目重复提交发布"

    def new_run(self, kind, job=None):
        run = {"id": uuid.uuid4().hex, "kind": kind, "status": "running", "started_at": now(), "calls": 0, "demo": self.settings.demo}
        if job:
            run.update(job_id=job['id'], trigger=job['trigger'])
            job['run_id'] = run['id']
        self.store.put('runs', run)
        if job:
            self.store.put('jobs', job)
        return run

    def finish_run(self, run, status, **fields):
        run = self.store.get("runs", run["id"])
        run.pop('active_model_call', None)
        run.update(status=status, finished_at=now(), **fields)
        self.store.put("runs", run)

    def refresh_source_reports(self):
        """重新评估全部线索的信源资格；规则或日期精度变化后，只作线索的条目可以回到候选。"""
        for story in self.store.all('stories'):
            if story.get('demo'):
                continue
            try:
                story['source_report'] = assessment(self.source_for(story), story)
            except ValueError as exc:
                story['source_report'] = {'auto_eligible': False, 'note': str(exc)}
            if story.get('status') == 'discovery_only' and story['source_report'].get('auto_eligible'):
                story['status'] = 'new'
            self.store.put('stories', story)

    TIER_WEIGHT = {'primary': 3, 'secondary': 2, 'research': 1}

    def tier_of(self, item):
        if item.get('demo'):
            return 'primary'
        try:
            return self.source_for(item).get('tier') or 'secondary'
        except ValueError:
            return 'secondary'

    def attempt_order(self, selected, allowance, support=None):
        """本轮先试哪些候选：只影响尝试顺序与名额，不改变旁证门禁。

        排序键依次是：新闻先于论文 → 同事件其他机构数量（热度信号）→ 官方优先 → 评分。
        每轮最多试 allowance 篇论文，避免论文占满名额、新闻永远轮不到。
        """
        support = support if support is not None else self.event_support()

        def rank(item):
            return (self.tier_of(item) != 'research', min(int(support.get(item['id'], 0)), 2), self.TIER_WEIGHT.get(self.tier_of(item), 0),
                    float(item.get('score') or 0) + float(item.get('heat_boost') or 0))

        limit = max(1, allowance) * 3
        research_quota = max(1, allowance)
        ordered, research = [], 0
        for item in sorted(selected, key=rank, reverse=True):
            if len(ordered) >= limit:
                break
            if self.tier_of(item) == 'research':
                if research >= research_quota:
                    continue
                research += 1
            ordered.append(item)
        return ordered

    def pipeline(self, job):
        run = self.new_run("pipeline", job=job)
        payload = job.get('payload') or {}
        requested = payload.get('article_count')
        started_at = time.monotonic()
        run.update(trigger=job.get("trigger", "manual"), schedule_slot=job.get("schedule_slot"), stage="collect", progress=5,
                   requested_count=requested, timings={})
        self.store.put("runs", run)
        source_errors = []
        def log(stage, message):
            self.store.event(run["id"], stage, message)
            current = self.store.get("runs", run["id"])
            timings = current.setdefault('timings', {})
            timings[stage] = round(time.monotonic() - started_at, 1)
            current.update(stage=stage, progress={"collect": 10, "score": 20, "fetch": 35, "extract": 45, 'crosscheck': 50, "angle": 55, 'prompt': 60, "write": 65, "review": 80, "deliver": 95, "finished": 100}.get(stage, current.get("progress", 0)), message=message)
            self.store.put("runs", current)
            if stage == "source_error":
                source_errors.append(message)
        try:
            self.controls.checkpoint()
            rules = self.writing_rules()
            editor = (job.get('payload') or {}).get('editor') or choose_editor(self.store)
            if self.settings.editorial_mode == 'auto':
                editor = {**editor, 'id': 'auto', 'name': '自动策划编辑部', 'style_name': '全领域信息·整篇写作',
                          'instructions': '按新闻自主选择标题、叙事结构和篇幅，整篇连贯写作，不设固定角色与新闻评论比例。',
                          'prompt_profile': 'prompt', 'writer_profile': 'writer', 'prompt_model': '', 'writer_model': ''}
            fmt = editor.get('format') or rules.get('default_format', 'news')
            if not self.settings.demo:
                for role in ('prompt', 'writer'):
                    route = resolve_profile(self.settings, editor[role + '_profile'], editor[role + '_model'])
                    if not route['api_key'] or not route['base_url'].startswith('https://') or not route['model']:
                        raise ValueError(('提示词策划' if role == 'prompt' else '正文写作') + '服务尚未配置 HTTPS 地址、密钥和模型 ID；已在付费调用前停止')
            target = (job.get('payload') or {}).get('story_id')
            replacing = (job.get('payload') or {}).get('draft_id')
            original = self.store.get('drafts', replacing) if replacing else None
            if original and (original['revision'] != job.get('revision') or original['status'] in IMMUTABLE):
                raise ValueError('稿件版本已改变，重写任务已停止')
            existing = self.store.all("stories")
            recent_poll = self.recent_collection() if not target and not self.settings.demo else None
            if recent_poll:
                log('collect_cache', '复用五分钟内刚完成的信源采集；不重复等待全部新闻源')
            incoming = ([] if target or recent_poll else collect.demo_items() if self.settings.demo else
                        self.collect_catalog(rules["max_age_hours"], log, health=self.health_update))
            self.ingest(incoming)
            if incoming:
                # 新线索入库后立刻刷新同事件候选组，排序与对照都依赖它。
                self.index_events()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=rules["max_age_hours"])
            already_drafted = {d["story_id"] for d in self.store.all("drafts")}
            if not target:
                for draft in self.store.all('drafts'):
                    already_drafted.update(row['story_id'] for row in (draft.get('crosscheck') or {}).get('sources', []) if row.get('same_event') and row.get('confidence', 0) >= 0.85)
            self.refresh_source_reports()
            def retry_ready(item):
                if item['status'] in {'new', 'shortlisted', 'awaiting_corroboration'}:
                    return True
                return (item['status'] == 'failed' and item.get('pipeline_failures', 0) < 3 and
                        (not item.get('retry_after') or datetime.fromisoformat(item['retry_after']) <= datetime.now(timezone.utc)))
            candidates = sorted([item for item in self.store.all("stories") if retry_ready(item) and item["demo"] == self.settings.demo
                                 and item["id"] not in already_drafted and (item.get('demo') or item.get('source_report', {}).get('auto_eligible'))
                                 and item.get('published_at') and datetime.fromisoformat(item["published_at"]) >= cutoff], key=lambda item: item["published_at"], reverse=True)
            if not target:
                tier_order = {'primary': 3, 'secondary': 2, 'research': 1}
                candidates.sort(key=lambda item: (tier_order.get(item.get('source_report', {}).get('tier'), 0), item['published_at']), reverse=True)
                balanced, per_source = [], {}
                research_cap = policy().get('research_candidates_per_source', 1)
                for candidate in candidates:
                    ident = candidate.get('source_id') or candidate['source']
                    cap = research_cap if candidate.get('source_report', {}).get('tier') == 'research' else policy()['max_candidates_per_source']
                    if per_source.get(ident, 0) >= cap:
                        continue
                    balanced.append(candidate)
                    per_source[ident] = per_source.get(ident, 0) + 1
                    if len(balanced) >= self.settings.max_candidates:
                        break
                candidates = balanced
            if target:
                item = self.store.get('stories', target)
                if not item.get('demo') and not item.get('source_report', {}).get('auto_eligible'):
                    raise ValueError('此线索未通过信源与时效检查，不能自动撰稿')
                if item.get('demo') != self.settings.demo:
                    raise ValueError('演示模式只能撰写虚构演示线索；真实新闻需要配置模型并关闭演示模式')
                candidates = [item]
            if not target and not self.settings.demo:
                try:
                    entries, note = trending.items(self.store, self.settings)
                    changed = trending.annotate(candidates, entries)
                    for candidate in changed:
                        self.store.put('stories', candidate)
                    if changed:
                        log('trending', f"热榜命中 {len(changed)} 条候选（{note}）；命中只影响排序与入选加权，不写进正文")
                    else:
                        log('trending', '热榜未命中本轮候选；' + note)
                except Exception as exc:
                    log('trending', '热榜信号不可用，不影响采编：' + self.safe_error(exc))
            log("collect", f"本轮处理 {len(candidates)} 条去重后的候选新闻")
            funnel = {'candidates': len(candidates), 'selected': 0, 'filtered': 0, 'source_ready': 0,
                      'extracted': 0, 'crosschecked': 0, 'written': 0, 'review_passed': 0}
            selected = []
            failures = 0
            unscored = [item for item in candidates if item.get('score') is None]
            if self.settings.fast_mode and not target and len(unscored) > 1:
                news_items = [{k: n.get(k) for k in ('id', 'title', 'source', 'published_at')} |
                              {'excerpt': n.get('excerpt', '')[:500]} for n in unscored]
                def check_scores(output):
                    ids = [s['story_id'] for s in output['scores']]
                    if len(ids) != len(set(ids)) or set(ids) != {n['id'] for n in unscored}:
                        raise ValueError('批量评分的新闻编号缺失、重复或多余')
                try:
                    log('score_batch', '快速模式：一次评估全部未评分候选')
                    batch = self.model.complete('score_batch', {'editorial': rules, 'news_items': news_items}, run, validator=check_scores)
                    scored = {s['story_id']: s['score'] for s in batch['scores']}
                    for item in unscored:
                        score = scored[item['id']]
                        item.update(score_detail=score, score=round(sum(score[k] * v for k, v in rules['weights'].items()), 1))
                except ValueError as exc:
                    log('score_batch_error', '批量评分未完成，回退逐条：' + self.safe_error(exc))
            for item in candidates:
                self.controls.checkpoint()
                try:
                    if item.get("score") is None and not (target and self.settings.fast_mode):
                        log("score", "评估：" + item["title"])
                        score = self.model.complete("score", {"editorial": rules, "news": item}, run)
                        item["score_detail"] = score
                        item["score"] = round(sum(score[k] * v for k, v in rules["weights"].items()), 1)
                    # 人工明确指定选题时，重要性评分仅供参考，事实与时效门禁仍保留。
                    effective = (item.get('score') or 0) + (item.get('heat_boost') or 0)
                    item["status"] = "shortlisted" if target or effective >= rules["min_score"] else "filtered"
                    if target and item.get('score') is not None and item['score'] < rules['min_score']:
                        log('manual_assignment', '按人工指定选题继续写稿；低重要性分不替代事实核查')
                    self.store.put("stories", item)
                    if item["status"] == "shortlisted":
                        selected.append(item)
                        funnel['selected'] += 1
                    else:
                        funnel['filtered'] += 1
                except Exception as exc:
                    failures += 1
                    item.update(status="failed", error=self.safe_error(exc), pipeline_failures=item.get('pipeline_failures', 0) + 1,
                                retry_after=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())
                    self.store.put("stories", item)
                    log("score_error", item["error"])
            day = datetime.now(BEIJING).date()
            produced_today = sum(datetime.fromisoformat(d["created_at"]).astimezone(BEIJING).date() == day and d["demo"] == self.settings.demo for d in self.store.all("drafts"))
            # 单次手动目标优先于全局每轮上限；0 仍表示不设业务额度。
            allowance = requested or self.settings.max_drafts_run or len(selected)
            if self.settings.max_drafts_day:
                allowance = min(allowance, max(0, self.settings.max_drafts_day - produced_today))
            if original:
                allowance = 1
            if requested and requested > allowance:
                log('limit', f'本次目标 {requested} 篇，受当日剩余额度限制，本轮最多 {allowance} 篇')
            made = 0
            held = 0
            drafted_events = set()
            # 对照不足的一条不占成稿额度，尝试后续候选；仍受每轮模型总预算约束。
            for item in self.attempt_order(selected, allowance):
                self.controls.checkpoint()
                if made >= allowance:
                    break
                if item['id'] in drafted_events:
                    continue
                item["status"] = "processing"
                self.store.put("stories", item)
                try:
                    self.prepare_source(item)
                    if not item.get("text"):
                        log("fetch", "读取原文：" + item["source"])
                        item["text"] = collect.plain(collect.fetch_text(item["url"], settings=self.settings))[:24000]
                    if len(item["text"]) < 120:
                        raise ValueError("原文过短或无法读取，不依赖标题编写新闻")
                    funnel['source_ready'] += 1
                    self.store.put("stories", item)
                    log('extract', '正在请求模型提炼原文证据；引文校验不合格会在预算内重试')
                    prepared_plan = None
                    if self.settings.merged_preparation and self.settings.fast_mode and editor['id'] == 'auto':
                        log('prepare_article', '合并提炼证据与整篇策划：一次读取原文，少一次文字模型请求')
                        prepared = self.model.complete('prepare_article', {'editorial': rules, 'news': item, 'editor': editor}, run,
                            validator=lambda output: validate_evidence(output['evidence'], item))
                        evidence, prepared_plan = prepared['evidence'], prepared['plan']
                    else:
                        evidence = self.model.complete("extract", {"editorial": rules, "news": item}, run,
                            validator=lambda output: validate_evidence(output, item))
                    validate_evidence(evidence, item)
                    funnel['extracted'] += 1
                    log("extract", f"已校验 {len(evidence['facts'])} 条事实的原文摘录")
                    log('crosscheck', '对照其他信源的事件、数字、版本与报道出处')
                    crosscheck = self.crosscheck_story(item, evidence, run)
                    funnel['crosschecked'] += 1
                    context = {"editorial": rules, "evidence": evidence, "published_at": item["published_at"], 'source': item['source'], 'editor': editor, 'source_snapshot': item.get('source_snapshot'), 'crosscheck': crosscheck,
                               'format': fmt, 'format_instruction': rules.get('formats', {}).get(fmt, '')}
                    if self.settings.drafts_only:
                        context['source_material'] = {'text': item['text'][:18000], 'url': item['url'],
                            'usage': '供展开已提炼事实的过程与条件，不得引入证据编号以外的新闻事实'}
                    if prepared_plan:
                        context['prepared_plan'] = prepared_plan
                    context['cache_body'] = self.settings.merged_preparation and not bool(original)
                    angle = (self.model.complete("angle", context, run) if self.settings.editorial_mode != 'auto' else
                             {'selected': '由策划模型自主选择传播方向', 'reader_value': '', 'alternatives': [], 'outline': [], 'reason': ''})
                    log("angle", "选定方向：" + angle["selected"])
                    log('prompt', '提示词模型正在为小编「' + editor['name'] + '」策划写作指令')
                    log("write", "写作模型正在根据小编分工与专用提示词撰稿")
                    article, writing_prompt, team_plan, contributions = self.write_with_team(context, angle, editor, run)
                    validate_article(article, evidence)
                    validate_claims(article, evidence, item.get('source_snapshot'))
                    draft = {"id": original['id'] if original else uuid.uuid4().hex, "story_id": item["id"], "news_url": item["url"], "source": item["source"],
                             "run_id": run["id"],
                             "news_published_at": item["published_at"], "created_at": original['created_at'] if original else now(), "updated_at": now(), "revision": original['revision'] + 1 if original else 1,
                             "demo": self.settings.demo, "score": item.get("score"), "angle": angle, "evidence": evidence,
                             "article": article, "status": "needs_ai_review", "approved_hash": None, "review": None,
                             'editor': editor, 'writing_prompt': writing_prompt, 'source_snapshot': item.get('source_snapshot'), 'crosscheck': crosscheck, 'format': fmt,
                             'editorial_plan': team_plan, 'team_contributions': contributions}
                    draft['models'] = {role: {'profile': editor[role + '_profile'], 'model': 'demo' if self.settings.demo else resolve_profile(self.settings, editor[role + '_profile'], editor[role + '_model'])['model']} for role in ('prompt', 'writer')}
                    with self.lock:
                        if original and (self.store.get('drafts', original['id'])['revision'] != original['revision'] or self.store.get('drafts', original['id'])['status'] != original['status']):
                            raise ValueError('人工已更新稿件，旧版本重写结果已丢弃')
                        self.store.put("drafts", draft)
                    item["status"] = "drafted"
                    item.pop("error", None)
                    self.store.put("stories", item)
                    drafted_events.update(row['story_id'] for row in crosscheck['sources'] if row.get('same_event') and row.get('confidence', 0) >= 0.85)
                    made += 1
                    funnel['written'] += 1
                    log('visual', '正文已保存；图文编辑正在下载官方原图、规划信息图与正文排版')
                    self.controls.checkpoint()
                    self.prepare_visuals(draft, run)
                    with self.lock:
                        current = self.store.get('drafts', draft['id'])
                        if current['revision'] != draft['revision'] or current['status'] != 'needs_ai_review':
                            log('review_stale', '人工已更新稿件，停止旧版本的图文与审核处理')
                            continue
                        self.store.put('drafts', draft)
                    log("review", "程序检查后交稿（独立AI复审已关闭）" if self.settings.review_mode == 'off' else "正在审核稿件与证据，必要时自动修订")
                    self.perform_review(draft, run, rules, allow_rewrite=True)
                    log("deliver", "已完成当前稿件的审核与交付处理")
                    self.controls.checkpoint()
                except CorroborationPending as exc:
                    held += 1
                    item.update(status='awaiting_corroboration', error=str(exc))
                    self.store.put('stories', item)
                    log('crosscheck_hold', str(exc))
                except Exception as exc:
                    failures += 1
                    if item["status"] != "drafted":
                        item.update(status="failed", error=self.safe_error(exc), pipeline_failures=item.get('pipeline_failures', 0) + 1,
                                    retry_after=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())
                        self.store.put("stories", item)
                    log("article_error", self.safe_error(exc))
            if not allowance and selected:
                log("limit", "已达到北京时间当日写稿上限，其余候选保留到下一轮")
            target_met = bool(requested) and made >= requested
            # 达成单次目标即视为本轮完成；其他候选的失败仍保存在 failures／events 中。
            status = ('done' if target_met and not source_errors else 'partial' if failures or source_errors else 'done') if made else 'failed' if failures else 'awaiting_corroboration' if held else 'partial' if source_errors else 'no_news'
            if source_errors and not incoming and not candidates:
                status = "failed"
            if made:
                written = [d for d in self.store.all('drafts') if d.get('run_id') == run['id']]
                funnel['review_passed'] = sum(d['status'] in {'pending_human', 'ready_to_publish', 'published'} for d in written)
                message = (f"本次目标 {requested} 篇，实际生成 {made} 篇；审核通过 {funnel['review_passed']} 篇，其余请查看稿件的待审核或修改状态"
                           if requested else f"本轮生成 {made} 篇；审核通过 {funnel['review_passed']} 篇，其余请查看稿件的待审核或修改状态")
                if target_met:
                    message += '；本次目标已达成，本轮结束'
            elif failures:
                message = f'本轮未生成稿件：{failures} 条采编失败；请查看错误与模型校验记录'
            elif held:
                message = f'本轮未生成稿件：{held} 条等待同事件旁证；没有进入写稿和人工审核'
            elif not allowance and selected:
                message = '本轮未生成稿件：已达到当日写稿上限'
            elif not candidates:
                message = '本轮未生成稿件：没有符合时效、去重与信源要求的候选'
            else:
                message = '本轮未生成稿件：候选未达到选题评分要求'
            if requested and made < requested:
                message += f'；本次目标 {requested} 篇、完成 {made} 篇'
            if source_errors:
                message += f'；{len(source_errors)} 个来源读取失败，采集覆盖不完整'
            self.finish_run(run, status, generated=made, failures=failures, source_failures=len(source_errors), awaiting_corroboration=held,
                            funnel=funnel, collection_reused=bool(recent_poll), progress=100)
            log('finished', message)
        except Exception:
            self.finish_run(run, "failed")
            raise

    def perform_review(self, draft, run, rules, allow_rewrite=False):
        # 审核以快照运行；若人工同时修改，则丢弃过期审核结果。
        starting_revision = draft["revision"]
        try:
            if self.settings.review_mode == 'off':
                validate_article(draft['article'], draft['evidence'])
                validate_claims(draft['article'], draft['evidence'], draft.get('source_snapshot'))
                draft.update(review={'passed': True, 'score': None, 'issues': [], 'mode': 'off',
                                     'summary': '按设置未调用独立AI复审；已完成程序引用与字段检查。'},
                             review_hash=content_hash(draft['article']), status='pending_human', updated_at=now())
                draft.pop('error', None)
                self.seal_checks(draft)
                with self.lock:
                    current = self.store.get('drafts', draft['id'])
                    if current['revision'] != starting_revision or current['status'] != 'needs_ai_review':
                        return
                    self.store.put('drafts', draft)
                self.store.event(draft['id'], 'review_skipped', '独立AI复审已关闭；程序检查通过，进入交稿')
                self.controls.checkpoint()
                delivered = self.deliver(draft['id'], draft['revision'], allow_cached_source=True)
                if (delivered.get('source_snapshot') or {}).get('verification_method') != 'cached_original':
                    self.auto_approve(draft['id'], draft['revision'])
                return
            for attempt in range((rules["max_rewrites"] if allow_rewrite else 0) + 1):
                if draft.get('visuals') and draft['visuals'].get('article_hash') != content_hash(draft['article']):
                    self.prepare_visuals(draft, run)
                validate_article(draft["article"], draft["evidence"])
                validate_claims(draft['article'], draft['evidence'], draft.get('source_snapshot'))
                fmt = draft.get('format') or rules.get('default_format', 'news')
                context = {"editorial": rules, "evidence": draft["evidence"], "article": draft["article"], "angle": draft["angle"], "published_at": draft["news_published_at"], 'editor': draft.get('editor') or choose_editor(self.store), 'source_snapshot': draft.get('source_snapshot'), 'crosscheck': draft.get('crosscheck'),
                           'format': fmt, 'format_instruction': rules.get('formats', {}).get(fmt, ''),
                           'editorial_plan': draft.get('editorial_plan'), 'visuals': draft.get('visuals')}
                try:
                    review = self.model.complete("review", context, run)
                except Exception as exc:
                    if self.settings.review_mode != 'advisory':
                        raise
                    review = {'passed': False, 'score': None, 'issues': [], 'mode': 'advisory',
                              'summary': '建议复审服务未完成；保留程序检查结果交稿，未取得AI评分。', 'error': self.safe_error(exc)}
                passed = review["passed"] and isinstance(review.get('score'), (int, float)) and review["score"] >= rules["review_min_score"] and not any(i["severity"] in {"blocker", "major"} for i in review["issues"])
                if self.settings.review_mode == 'advisory':
                    review['mode'] = 'advisory'
                    passed = True
                draft.update(review=review, review_hash=content_hash(draft["article"]), status="pending_human" if passed else "needs_revision", updated_at=now())
                draft.pop("error", None)
                if passed:
                    draft.pop("rejection", None)
                    self.seal_checks(draft)
                if passed or attempt >= (rules["max_rewrites"] if allow_rewrite else 0):
                    break
                draft['writing_prompt'] = self.model.complete('prompt', {**context, 'review': review}, run)
                draft["article"] = self.model.complete("rewrite", {**context, "review": review, 'writing_prompt': draft['writing_prompt']}, run)
                draft["revision"] += 1
            with self.lock:
                current = self.store.get("drafts", draft["id"])
                if current["revision"] != starting_revision or current["status"] != "needs_ai_review":
                    self.store.event(draft["id"], "review_stale", "稿件已修改，旧版本审核结果已丢弃")
                    return
                self.store.put("drafts", draft)
            self.store.event(draft["id"], "review", f"AI 审核 {review['score']} 分；状态 {draft['status']}")
            if draft["status"] == "pending_human":
                self.controls.checkpoint()
                delivered = self.deliver(draft["id"], draft["revision"], allow_cached_source=self.settings.review_mode == 'advisory')
                if (delivered.get('source_snapshot') or {}).get('verification_method') != 'cached_original':
                    self.auto_approve(draft["id"], draft["revision"])
        except Exception as exc:
            with self.lock:
                current = self.store.get("drafts", draft["id"])
                if current["revision"] == starting_revision and current["status"] == "needs_ai_review":
                    current.update(status="needs_ai_review", error=self.safe_error(exc))
                    self.store.put("drafts", current)
            raise

    def review_draft(self, draft_id, revision, job=None):
        with self.lock:
            draft = self.store.get("drafts", draft_id)
            if draft["revision"] != revision or draft["status"] != "needs_ai_review":
                return
        run = self.new_run("review", job=job)
        try:
            self.perform_review(draft, run, editorial())
            self.finish_run(run, "done")
        except Exception:
            self.finish_run(run, "failed")
            raise

    def delete_draft(self, draft_id, confirmed=False):
        """删除本地稿件与交稿包；不动公众号草稿／已发表文章，也不删除公共图片文件。"""
        if not confirmed:
            raise ValueError('删除稿件需要确认')
        with self.lock:
            draft = self.store.get('drafts', draft_id)
            active = self.active_publication(draft_id)
            if active:
                raise ValueError('稿件有进行中的发布记录（' + STATUS_TEXT.get(active['status'], active['status'])
                                 + '），请先完成对账或放弃后再删除')
            busy = [j for j in self.store.all('jobs') if j.get('subject') == draft_id
                    and j['status'] in {'queued', 'running', 'paused'}]
            if busy:
                raise ValueError('稿件还有排队或运行中的任务，请等任务结束或中断后再删除')
            self.store.delete('drafts', draft_id)
        exports = (self.settings.data_dir / 'exports').resolve()
        target = (exports / draft_id).resolve()
        if target.parent == exports and target.exists():
            shutil.rmtree(target)
        title = (draft.get('article') or {}).get('title', '')
        self.store.event(draft_id, 'deleted', '已删除本地稿件：' + title[:60])
        return {'deleted': True, 'id': draft_id, 'title': title, 'published_url': draft.get('published_url')}

    def edit(self, draft_id, revision, article):
        with self.lock:
            draft = self.store.get("drafts", draft_id)
            if draft["revision"] != revision:
                raise ValueError("版本已更新，请刷新后编辑")
            if draft["status"] in IMMUTABLE:
                raise ValueError("该版本已进入外部发布流程，不能修改")
            self.assert_editable(draft_id)
            validate_article(article, draft["evidence"])
            draft.update(article=article, revision=revision + 1, status="needs_ai_review", approved_hash=None,
                         review=None, review_hash=None, delivery=None, updated_at=now())
            for key in ("approved_by", "approved_at", "approved_revision"):
                draft.pop(key, None)
            self.store.put("drafts", draft)
            self.enqueue("review", draft_id, draft["revision"])
            self.store.event(draft_id, "edit", "人工修改；旧版本审批失效，已重新排队 AI 审核")
            return draft
