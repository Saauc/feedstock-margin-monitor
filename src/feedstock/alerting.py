"""
alerting.py — turn a margin-pressure reading into an alert when it crosses
a configurable threshold.

The whole reason the daily run exists is to *notify*, not just display. So this
module has two outputs that escalate:

  1. A prominent written flag — always rendered into the briefing when the
     index is at/above `alert_threshold`. Costs nothing, can't fail, idempotent.
  2. An email — sent only if SMTP creds are present in the environment, and
     only ONCE per day (deduped via the alert_emails table) so re-running the
     pipeline doesn't spam the recipient.

Everything is best-effort and degrades quietly: no SMTP config → flag only;
SMTP send fails → log it, keep going. A monitoring job must never crash on the
notify step.

SMTP environment variables (set in .env or as CI secrets):
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
    ALERT_EMAIL_FROM, ALERT_EMAIL_TO
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

# Reuse the project's .env loader rather than duplicating it. It no-ops if
# python-dotenv is absent and never overwrites already-exported vars (CI).
from feedstock.narrative import _load_dotenv


def evaluate_alert(snapshot: dict, config: dict) -> dict:
    """Decide whether the snapshot warrants an alert.

    Returns a small dict the briefing and email can both render. We only ever
    alert on a *meaningful* reading: if there isn't enough trailing history,
    the index defaults to a neutral 50 and we suppress alerts (a 50 from no
    data must not look like a deliberate 'all clear' or trip the threshold).
    """
    threshold = float(config.get("alert_threshold", 70))
    index = float(snapshot.get("index", 50.0))
    sufficient = bool(snapshot.get("sufficient", False))
    triggered = sufficient and index >= threshold
    return {
        "triggered": triggered,
        "index": index,
        "threshold": threshold,
        "date": snapshot.get("date"),
        "exceedance": round(index - threshold, 1),
        "suppressed_no_history": (not sufficient) and index >= threshold,
    }


def render_alert_flag(alert: dict) -> str:
    """The prominent banner shown in the briefing.

    Returns a clearly visible block when triggered, or a one-line all-clear
    otherwise so the briefing always states the alert status explicitly.
    """
    if alert["triggered"]:
        return (
            "!" * 60 + "\n"
            "  *** MARGIN-PRESSURE ALERT ***\n"
            f"  Index {alert['index']:.0f}/100 has crossed the alert threshold "
            f"of {alert['threshold']:.0f}\n"
            f"  ({alert['exceedance']:+.1f} above threshold) as of {alert['date']}.\n"
            "  Feedstock costs are running materially hotter than their\n"
            "  trailing baseline — coatings/paint input margins under pressure.\n"
            + "!" * 60
        )
    return (
        f"[ok] No alert: margin-pressure index {alert['index']:.0f}/100 is below "
        f"the {alert['threshold']:.0f} threshold."
    )


def _smtp_config() -> dict | None:
    """Read SMTP settings from the environment; None if not fully configured."""
    _load_dotenv()
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("ALERT_EMAIL_TO")
    if not (host and user and password and to_addr):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "from_addr": os.environ.get("ALERT_EMAIL_FROM", user),
        "to_addr": to_addr,
    }


def _build_email(alert: dict, body_extra: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = (
        f"[Feedstock Alert] Margin-pressure {alert['index']:.0f}/100 "
        f"crossed {alert['threshold']:.0f} ({alert['date']})"
    )
    body = render_alert_flag(alert)
    if body_extra:
        body += "\n\n" + body_extra
    msg.set_content(body)
    return msg


def maybe_send_email(
    conn,
    alert: dict,
    body_extra: str | None = None,
) -> dict:
    """Send an alert email if configured, not already sent today, and triggered.

    Returns a status dict describing exactly what happened, so the caller can
    report it honestly ('flag only', 'emailed', 'skipped — already sent', ...).
    """
    if not alert["triggered"]:
        return {"attempted": False, "sent": False, "reason": "not triggered"}

    cfg = _smtp_config()
    if cfg is None:
        return {"attempted": False, "sent": False, "reason": "SMTP not configured"}

    from feedstock.store import alert_email_already_sent, record_alert_email

    date = alert["date"]
    if date and alert_email_already_sent(conn, date):
        return {"attempted": False, "sent": False, "reason": "already emailed today"}

    msg = _build_email(alert, body_extra)
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as server:
            server.starttls(context=context)
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        if date:
            record_alert_email(conn, date, alert["index"], alert["threshold"])
        return {"attempted": True, "sent": True, "reason": "email sent",
                "to": cfg["to_addr"]}
    except Exception as exc:  # noqa: BLE001 — notify must not crash the run
        return {"attempted": True, "sent": False,
                "reason": f"send failed: {type(exc).__name__}: {exc}"}


def run_alert_check() -> dict:
    """Top-level: compute today's snapshot, evaluate, flag, and maybe email.

    Pulls the latest stored narrative into the email body for context.
    """
    from feedstock import analysis, transform
    from feedstock.store import get_connection, init_db, latest_narrative

    config = analysis.load_config()
    metrics = list(config["components"].keys())

    init_db()
    conn = get_connection()
    try:
        series_map = analysis.build_series_map(conn, metrics)
        snapshot = transform.compute_latest_pressure(series_map, config)
        alert = evaluate_alert(snapshot, config)

        print(render_alert_flag(alert))
        if alert["suppressed_no_history"]:
            print("  (index is at/above threshold but flagged neutral — "
                  "not enough history yet, so no alert raised.)")

        narrative_row = latest_narrative(conn)
        body_extra = narrative_row["text"] if narrative_row else None
        status = maybe_send_email(conn, alert, body_extra=body_extra)

        print(f"  email: {status['reason']}")
        return {"alert": alert, "email": status}
    finally:
        conn.close()


if __name__ == "__main__":
    run_alert_check()
