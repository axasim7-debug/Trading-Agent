"""Phone notifications through a Telegram bot (optional).

Setup: talk to @BotFather in Telegram -> /newbot -> copy the token; send any message to your bot,
then open https://api.telegram.org/bot<TOKEN>/getUpdates to read your chat id.
Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as environment variables. Without them nothing is sent."""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, enabled: bool = True):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.enabled = enabled and bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000],
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:  # notifications must never break trading
            log.warning("telegram send failed: %s", e)
            return False


class NullNotifier(Notifier):
    def __init__(self):
        super().__init__(enabled=False)


class RecordingNotifier(Notifier):
    """For tests: keeps messages in memory."""
    def __init__(self):
        super().__init__(enabled=False)
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True
