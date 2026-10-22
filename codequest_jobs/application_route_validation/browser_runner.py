"""Bounded Playwright navigation with per-request URL enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from application_route_validation.browser_safety import is_url_allowed, resolve_public_host

NavigateHook = Callable[[str], None]


@dataclass
class BrowserRunLimits:
    total_timeout_ms: int = 45_000
    per_navigation_timeout_ms: int = 25_000
    max_navigations: int = 4


@dataclass
class SafeBrowserSession:
    """One isolated browser context with redirect/navigation guards."""

    page: Any
    limits: BrowserRunLimits = field(default_factory=BrowserRunLimits)
    navigations: int = 0
    _blocked: str | None = None

    def _guard(self, url: str) -> tuple[bool, str]:
        ok, reason = is_url_allowed(url)
        if not ok:
            return False, reason
        return resolve_public_host(url)

    def _on_frame_navigate(self, frame: Any) -> None:
        if frame != self.page.main_frame:
            return
        target = frame.url
        if not target or target.startswith("about:"):
            return
        ok, reason = self._guard(target)
        if not ok:
            self._blocked = reason
            try:
                self.page.close()
            except Exception:
                pass

    def attach(self) -> None:
        self.page.on("framenavigated", self._on_frame_navigate)

    def goto(self, url: str) -> Any | None:
        if self._blocked:
            return None
        if self.navigations >= self.limits.max_navigations:
            self._blocked = "navigation_limit"
            return None
        ok, reason = self._guard(url)
        if not ok:
            self._blocked = reason
            return None
        self.navigations += 1
        return self.page.goto(url, wait_until="domcontentloaded", timeout=self.limits.per_navigation_timeout_ms)

    def blocked_reason(self) -> str | None:
        return self._blocked

    def final_host_allowed(self) -> bool:
        try:
            host = (urlsplit(self.page.url).hostname or "").lower()
        except ValueError:
            return False
        return bool(host) and is_url_allowed(self.page.url)[0]
