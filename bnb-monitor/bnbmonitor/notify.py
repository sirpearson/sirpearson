"""Messaggi Telegram (bot personale)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

MAX_LEN = 4000


def send(cfg, text: str, kind: str = "info", ref: str | None = None, conn=None) -> bool:
    """Invia un messaggio; se il bot non è configurato lo scrive solo nel log."""
    ok = False
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        log.warning("Telegram non configurato, messaggio non inviato:\n%s", text)
    else:
        try:
            for start in range(0, len(text), MAX_LEN):
                r = requests.post(
                    f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                    json={"chat_id": cfg.telegram_chat_id, "text": text[start:start + MAX_LEN],
                          "disable_web_page_preview": True},
                    timeout=20,
                )
                r.raise_for_status()
            ok = True
        except requests.RequestException as exc:
            log.error("Invio Telegram fallito: %s", exc)
    if conn is not None:
        conn.execute("INSERT INTO notifications (kind, ref, text, ok) VALUES (%s, %s, %s, %s)",
                     (kind, ref, text, ok))
        conn.commit()
    return ok


def alarm(cfg, text: str, ref: str | None = None, conn=None) -> bool:
    return send(cfg, f"⚠️ Monitor BnB Trieste\n{text}", kind="alarm", ref=ref, conn=conn)
