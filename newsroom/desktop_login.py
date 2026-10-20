"""桌面启动的一次性登录票据；管理员口令不进入浏览器 URL。"""
import hashlib
import secrets
import threading
import time

from pydantic import BaseModel, ConfigDict, Field


class DesktopLoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ticket: str = Field(min_length=32, max_length=256)


class DesktopLoginResponse(BaseModel):
    token: str


class DesktopTicketResponse(BaseModel):
    ticket: str
    expires_in: int


class DesktopTickets:
    TTL = 60

    def __init__(self):
        self.pending = {}
        self.lock = threading.Lock()

    def issue(self):
        current = time.monotonic()
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.lock:
            self.pending = {key: deadline for key, deadline in self.pending.items() if deadline > current}
            if len(self.pending) >= 8:
                self.pending.pop(next(iter(self.pending)))
            self.pending[digest] = current + self.TTL
        return {'ticket': raw, 'expires_in': self.TTL}

    def consume(self, raw):
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.lock:
            deadline = self.pending.pop(digest, None)
            return deadline is not None and deadline > time.monotonic()
