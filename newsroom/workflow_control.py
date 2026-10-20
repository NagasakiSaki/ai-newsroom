"""Cooperative controls: retain the worker stack while paused; never replay paid stages."""
import threading
from datetime import datetime, timezone, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .db import now


class WorkflowCancelled(BaseException):
    # Deliberately bypass per-story/model/network retry handlers.
    pass


class ControlRequest(BaseModel):
    action: Literal['pause', 'resume', 'cancel']
    job_id: str | None = None


class UsagePreferences(BaseModel):
    run_alert_tokens: int = Field(default=50000, ge=0, le=1000000000)


class WorkflowControl:
    kinds = {'pipeline', 'collect', 'review', 'draft_batch', 'images'}

    def __init__(self, store, stop_event):
        self.store, self.stop_event = store, stop_event
        self.condition = threading.Condition(threading.RLock())
        self.local = threading.local()
        self.active_job = None

    def recover(self):
        # A paused Python stack cannot survive a service restart.
        # Only the process holding worker.lock may clear the former worker's request.
        state = self.state()
        state.update(job_id=None, requested=None)
        self.store.put('preferences', state)

    def state(self):
        try:
            return self.store.get('preferences', 'workflow_control')
        except KeyError:
            return {'id': 'workflow_control', 'dispatch_paused': False, 'requested': None, 'job_id': None}

    def begin(self, job):
        with self.condition:
            self.active_job = job['id']
            self.local.job_id = job['id']

    def end(self):
        with self.condition:
            state = self.state()
            state.update(job_id=None, requested=None)
            self.store.put('preferences', state)
            self.active_job = None
            self.local.job_id = None

    def request(self, action, job_id=None):
        with self.condition:
            state = self.state()
            target = job_id or self.active_job
            if job_id:
                job = self.store.get('jobs', job_id)
                if job['kind'] not in self.kinds:
                    raise ValueError('采编控制不处理微信写入任务；请使用发布记录的暂停或核对功能')
                if job['status'] not in {'queued', 'running', 'paused'}:
                    raise ValueError('此任务已经结束，不能继续或中断')
                if job_id != self.active_job:
                    if action == 'cancel' and job['status'] == 'queued':
                        job.update(status='cancelled', finished_at=now())
                        self.store.put('jobs', job)
                        return state
                    raise ValueError('排队任务可中断；暂停或继续请使用工作台的全局控制')
            state.update(dispatch_paused=action != 'resume', requested=action if target else None,
                         job_id=target, updated_at=now())
            self.store.put('preferences', state)
            self.store.event(target or 'worker', 'workflow_control', {
                'pause': '已请求暂停：当前请求收完并保存后停在下一控制点；排队和定时采编暂停',
                'resume': '继续原进度并恢复采编调度，不重放已完成模型请求',
                'cancel': '已请求中断后续采编：保留已完成结果；采编调度保持暂停',
            }[action])
            self.condition.notify_all()
            return state

    def checkpoint(self):
        job_id = getattr(self.local, 'job_id', None)
        if not job_id:
            return
        with self.condition:
            while True:
                state = self.state()
                if self.stop_event.is_set() or (state.get('job_id') == job_id and state.get('requested') == 'cancel'):
                    raise WorkflowCancelled()
                if state.get('job_id') != job_id or state.get('requested') != 'pause':
                    return
                job = self.store.get('jobs', job_id)
                if job['status'] != 'paused':
                    job['status'] = 'paused'
                    self.store.put('jobs', job)
                    if job.get('run_id'):
                        run = self.store.get('runs', job['run_id'])
                        run.update(status='paused', paused_at=now())
                        self.store.put('runs', run)
                self.condition.wait(timeout=0.25)
                state = self.state()
                if state.get('requested') == 'resume':
                    job['status'] = 'running'
                    self.store.put('jobs', job)
                    if job.get('run_id'):
                        run = self.store.get('runs', job['run_id'])
                        run.update(status='running', resumed_at=now())
                        self.store.put('runs', run)


def usage_preferences(store):
    try:
        return store.get('preferences', 'usage_alert')
    except KeyError:
        return {'id': 'usage_alert', 'run_alert_tokens': 50000}


def usage_summary(store):
    today = datetime.now(timezone(timedelta(hours=8))).date()
    calls = [c for c in store.all('calls') if c.get('model') != 'demo' and c.get('outcome') != 'cache_hit'
             and datetime.fromisoformat(c['at']).astimezone(timezone(timedelta(hours=8))).date() == today]
    image_calls = [c for c in store.all('image_calls') if datetime.fromisoformat(c['at']).astimezone(timezone(timedelta(hours=8))).date() == today]
    return {'date': str(today), 'requests': len(calls), 'image_requests': len(image_calls),
            'reported_tokens': sum(c.get('usage', {}).get('total_tokens', 0) for c in calls),
            'unknown_usage_requests': sum('total_tokens' not in c.get('usage', {}) for c in calls),
            'preferences': usage_preferences(store)}
