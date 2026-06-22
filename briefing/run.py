"""
Evening Briefing — runs nightly, scans Gmail + Google Calendar,
sends a summary email via Gmail.
"""

import os
import json
import base64
import datetime
import anthropic
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

RECIPIENT = "d.brown@embtrak.com"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
]


# ── Credentials ───────────────────────────────────────────────────────────────

def get_google_creds() -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_calendar_events(creds: Credentials) -> list[dict]:
    """Return events for the next 48 hours."""
    service = build("calendar", "v3", credentials=creds)
    now = datetime.datetime.utcnow()
    end = now + datetime.timedelta(hours=48)
    result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat() + "Z",
        timeMax=end.isoformat() + "Z",
        singleEvents=True,
        orderBy="startTime",
        maxResults=30,
    ).execute()
    events = []
    for e in result.get("items", []):
        start = e.get("start", {})
        events.append({
            "summary":   e.get("summary", "(no title)"),
            "start":     start.get("dateTime") or start.get("date"),
            "end":       e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
            "attendees": [a.get("email") for a in e.get("attendees", [])],
            "description": (e.get("description") or "")[:300],
            "location":  e.get("location", ""),
        })
    return events


def fetch_recent_emails(creds: Credentials) -> list[dict]:
    """Return last 72 hours of inbox threads (subject + snippet)."""
    service = build("gmail", "v1", credentials=creds)
    cutoff = int((datetime.datetime.utcnow() - datetime.timedelta(hours=72)).timestamp())
    results = service.users().messages().list(
        userId="me",
        q=f"in:inbox after:{cutoff}",
        maxResults=40,
    ).execute()
    messages = []
    for msg in results.get("messages", []):
        m = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        messages.append({
            "subject": headers.get("Subject", "(no subject)"),
            "from":    headers.get("From", ""),
            "date":    headers.get("Date", ""),
            "snippet": m.get("snippet", ""),
        })
    return messages


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyse(events: list[dict], emails: list[dict]) -> dict:
    """Send calendar + inbox data to Claude, get structured briefing back."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    now_iso = datetime.datetime.now().isoformat()

    prompt = f"""Today is {now_iso}.

CALENDAR EVENTS (next 48 hours):
{json.dumps(events, indent=2)}

INBOX EMAILS (last 72 hours):
{json.dumps(emails, indent=2)}

Analyse this data and return ONLY a JSON object — no markdown, no preamble — with this shape:
{{
  "unusual": [{{"text": "...", "meta": "..."}}],
  "prep":    [{{"text": "...", "meta": "..."}}],
  "commitments": [{{"text": "...", "meta": "..."}}]
}}

Rules:
- "unusual": meetings outside normal hours (before 8am / after 7pm), back-to-back with no buffer, \
unknown attendees, no agenda on important calls, double-bookings, last-minute additions, anything \
that looks like a surprise.
- "prep": events in the next 48h that need materials, decisions, or review beforehand (demos, \
client calls, reviews, pitches). Note specifically what prep is likely needed.
- "commitments": emails or calendar notes where the sender said "I'll send…", "I'll follow up…", \
"Let me get back to you…", "I'll check on…" — but there is no evidence of a follow-up yet.
- Each list: 0–5 items max. Empty array [] if nothing applies.
- "meta" field: short context under 8 words (e.g. "Tomorrow 9am" or "Email from Sarah, 2d ago").
- Be direct and specific. No filler phrases.
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude hit max_tokens limit — response truncated. Increase max_tokens.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"JSON parse error at char {e.pos}: {e.msg}")
        print(f"Raw Claude response:\n{text}")
        raise


# ── Email formatting ──────────────────────────────────────────────────────────

def build_email_html(briefing: dict) -> str:
    now_str = datetime.datetime.now().strftime("%A, %B %-d · %-I:%M %p")

    def section(title: str, icon: str, items: list, color: str) -> str:
        if not items:
            return ""
        rows = "".join(
            f'<tr><td style="padding:8px 0;border-bottom:1px solid #f0f0f0;">'
            f'<span style="color:{color};font-size:16px;margin-right:8px;">●</span>'
            f'<strong style="font-size:14px;color:#1a1a1a;">{i["text"]}</strong>'
            f'<span style="font-size:12px;color:#888;margin-left:8px;">{i.get("meta","")}</span>'
            f'</td></tr>'
            for i in items
        )
        return f"""
        <tr><td style="padding:20px 0 6px;">
          <p style="margin:0;font-size:11px;font-weight:600;letter-spacing:.08em;
                     text-transform:uppercase;color:#888;">{icon} {title}</p>
        </td></tr>
        {rows}
        <tr><td style="padding:8px 0;"></td></tr>
        """

    unusual_html     = section("Unusual / needs attention", "⚠️", briefing.get("unusual", []), "#E24B4A")
    prep_html        = section("Prep needed",               "📋", briefing.get("prep", []),    "#EF9F27")
    commitments_html = section("Open commitments",          "✅", briefing.get("commitments", []), "#378ADD")

    all_clear = not any([
        briefing.get("unusual"), briefing.get("prep"), briefing.get("commitments")
    ])
    body_html = (
        '<tr><td style="padding:24px 0;text-align:center;color:#888;font-size:14px;">'
        '✅ All clear — nothing unusual, no prep needed, no open commitments.'
        '</td></tr>'
        if all_clear else
        unusual_html + prep_html + commitments_html
    )

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:32px 16px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:10px;overflow:hidden;
                    border:1px solid #e8e8e8;">
        <!-- header -->
        <tr>
          <td style="background:#1a1a1a;padding:20px 28px;">
            <p style="margin:0;font-size:18px;font-weight:600;color:#ffffff;">🌙 Evening Briefing</p>
            <p style="margin:4px 0 0;font-size:13px;color:#888;">{now_str} · Next 48 hours</p>
          </td>
        </tr>
        <!-- body -->
        <tr><td style="padding:8px 28px 20px;">
          <table width="100%" cellpadding="0" cellspacing="0">
            {body_html}
          </table>
        </td></tr>
        <!-- footer -->
        <tr>
          <td style="border-top:1px solid #f0f0f0;padding:14px 28px;">
            <p style="margin:0;font-size:11px;color:#bbb;">
              Sent by Evening Briefing · EmbTrak Automation
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""


def send_email(creds: Credentials, html: str, briefing: dict):
    service = build("gmail", "v1", credentials=creds)
    date_str = datetime.datetime.now().strftime("%a %-d %b")
    total = sum(len(briefing.get(k, [])) for k in ["unusual", "prep", "commitments"])
    subject = f"Evening Briefing · {date_str} · {total} item{'s' if total != 1 else ''}"

    msg = MIMEMultipart("alternative")
    msg["To"]      = RECIPIENT
    msg["From"]    = RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✓ Briefing sent to {RECIPIENT} ({total} items)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print("Authenticating with Google…")
    creds = get_google_creds()

    print("Fetching calendar events…")
    events = fetch_calendar_events(creds)
    print(f"  {len(events)} events found")

    print("Fetching inbox emails…")
    emails = fetch_recent_emails(creds)
    print(f"  {len(emails)} emails found")

    print("Analysing with Claude…")
    briefing = analyse(events, emails)
    print(f"  unusual={len(briefing.get('unusual',[]))}, "
          f"prep={len(briefing.get('prep',[]))}, "
          f"commitments={len(briefing.get('commitments',[]))}")

    print("Building and sending email…")
    html = build_email_html(briefing)
    send_email(creds, html, briefing)


if __name__ == "__main__":
    main()
