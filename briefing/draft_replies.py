"""
Draft Reply Generator — runs 3x daily (9am, 1pm, 5pm).
Scans inbox for customer inquiries about EmbTrak products
and saves draft replies for Don to review and send.
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

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
]

EMBTRAK_CONTEXT = """
EmbTrak is a production management software company for the embroidery and decorated apparel 
industry. Product tiers: Elite (SMB shops) and Enterprise (larger operations). A newer scheduling 
product called Cadence by EmbTrak is in active development with a waitlist at cadence.embtrak.com.
Don Brown is the owner. The software helps embroidery and screen print shops manage orders, 
scheduling, production workflows, and machine management (Tajima, Barudan equipment).
"""


def get_google_creds() -> Credentials:
    token_data = {
        "token":         os.environ["GOOGLE_TOKEN"],
        "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
        "token_uri":     "https://oauth2.googleapis.com/token",
        "client_id":     os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "scopes":        SCOPES,
    }
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return creds


def fetch_candidate_emails(service) -> list[dict]:
    """Fetch unread emails from the last 4 hours that might be customer inquiries."""
    cutoff = int((datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=4)).timestamp())
    results = service.users().messages().list(
        userId="me",
        q=f"in:inbox is:unread after:{cutoff} -from:noreply -from:no-reply",
        maxResults=30,
    ).execute()

    messages = []
    for msg in results.get("messages", []):
        m = service.users().messages().get(
            userId="me", id=msg["id"], format="full",
            metadataHeaders=["Subject", "From", "To", "Date", "Message-ID", "References"]
        ).execute()
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}

        # Extract body text
        body = ""
        payload = m.get("payload", {})
        if payload.get("body", {}).get("data"):
            body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
        elif payload.get("parts"):
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                    break

        messages.append({
            "id":         msg["id"],
            "subject":    headers.get("Subject", "(no subject)"),
            "from":       headers.get("From", ""),
            "to":         headers.get("To", ""),
            "date":       headers.get("Date", ""),
            "message_id": headers.get("Message-ID", ""),
            "body":       body[:1500],
            "snippet":    m.get("snippet", "")[:300],
        })
    return messages


def identify_and_draft(emails: list[dict]) -> list[dict]:
    """Ask Claude to identify EmbTrak inquiries and draft replies."""
    if not emails:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    email_list = json.dumps([{
        "id":      e["id"],
        "from":    e["from"],
        "subject": e["subject"],
        "body":    e["body"] or e["snippet"],
        "date":    e["date"],
    } for e in emails], indent=2)

    prompt = f"""You are an email assistant for Don Brown, owner of EmbTrak Inc.

EmbTrak context:
{EMBTRAK_CONTEXT}

Review the emails below. For any that are genuine customer or prospect inquiries about EmbTrak 
products (Elite, Enterprise, or Cadence), draft a reply from Don.

Return ONLY a JSON array — no markdown, no preamble:
[
  {{
    "id": "message_id",
    "should_draft": true,
    "reason": "one line explaining why this needs a reply",
    "draft_subject": "Re: original subject",
    "draft_body": "full reply text here"
  }}
]

Rules for drafts:
- Write in Don's voice: direct, knowledgeable, friendly but professional
- Don owns the company so he speaks with authority about the product
- If they're asking about pricing, say you'll follow up with details and ask about their shop size
- If they're asking about features, answer based on EmbTrak context above
- If they're asking about Cadence, mention the waitlist at cadence.embtrak.com
- If they're asking for a demo, offer to schedule a call
- Keep replies concise — 3 to 6 sentences max
- Sign off as: Don Brown | EmbTrak

Only include emails where should_draft is true.
If no emails qualify, return an empty array [].

Emails to review:
{email_list}
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    drafts = json.loads(text)
    return [d for d in drafts if d.get("should_draft")]


def save_draft(service, email: dict, draft: dict):
    """Save a draft reply in Gmail."""
    msg = MIMEMultipart("alternative")
    msg["To"]      = email["from"]
    msg["From"]    = "d.brown@embtrak.com"
    msg["Subject"] = draft["draft_subject"]
    msg["In-Reply-To"] = email.get("message_id", "")
    msg["References"]  = email.get("message_id", "")

    body = draft["draft_body"]
    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "threadId": email["id"]}}
    ).execute()
    print(f"  ✉  Draft saved: {draft['draft_subject']} → {email['from']}")
    print(f"     Reason: {draft['reason']}")


def main():
    print("Authenticating with Google…")
    creds = get_google_creds()
    service = build("gmail", "v1", credentials=creds)

    print("Fetching candidate emails…")
    emails = fetch_candidate_emails(service)
    print(f"  {len(emails)} emails to review")

    if not emails:
        print("No emails to process.")
        return

    print("Identifying inquiries and drafting replies…")
    drafts = identify_and_draft(emails)
    print(f"  {len(drafts)} draft(s) to save")

    if not drafts:
        print("No EmbTrak inquiries found this pass.")
        return

    # Match drafts back to original emails for threading
    email_map = {e["id"]: e for e in emails}
    for draft in drafts:
        email = email_map.get(draft["id"])
        if email:
            save_draft(service, email, draft)

    print(f"✓ Done — {len(drafts)} draft(s) saved to Gmail Drafts folder")


if __name__ == "__main__":
    main()
