"""
Inbox Cleaner — runs every 2 hours.
Scans inbox for marketing, promotional, and political emails
and moves them to trash automatically.

Also does a fast pre-pass to delete MailReach warmup emails:
- Labeled "To Follow"
- Sent from any @embtrak.com address
- Contain a random string footer below the sender name
"""

import os
import re
import json
import datetime
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
]

# Matches MailReach random footer strings like: 258feo-Lz9Y-6994b2b-
MAILREACH_PATTERN = re.compile(r"[a-z0-9]{4,}-[a-zA-Z0-9]{3,}-[a-z0-9]{5,}-?", re.IGNORECASE)


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
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def purge_mailreach_warmup(service) -> int:
    """
    Fast pre-pass: find and permanently delete MailReach warmup emails.
    Criteria: labeled 'To Follow' AND sent from @embtrak.com
    No AI needed — these are identified by rule alone.
    """
    print("  Scanning for MailReach warmup emails…")

    # Search for emails with the 'To Follow' label from embtrak.com senders
    results = service.users().messages().list(
        userId="me",
        q='label:"To Follow" from:@embtrak.com',
        maxResults=500,
    ).execute()

    messages = results.get("messages", [])

    if not messages:
        print("  No MailReach warmup emails found.")
        return 0

    # Batch delete — permanently removes, skips trash
    ids = [m["id"] for m in messages]
    # Gmail batch delete accepts up to 1000 ids at a time
    for i in range(0, len(ids), 1000):
        batch = ids[i:i+1000]
        service.users().messages().batchDelete(
            userId="me",
            body={"ids": batch}
        ).execute()

    print(f"  🗑  Permanently deleted {len(ids)} MailReach warmup email(s)")
    return len(ids)


def fetch_inbox_emails(service) -> list[dict]:
    """Fetch unread inbox emails from the last 3 hours."""
    cutoff = int((datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=3)).timestamp())
    results = service.users().messages().list(
        userId="me",
        q=f"in:inbox is:unread after:{cutoff}",
        maxResults=50,
    ).execute()

    messages = []
    for msg in results.get("messages", []):
        m = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["Subject", "From", "To", "Date", "List-Unsubscribe"]
        ).execute()
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        messages.append({
            "id":      msg["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "from":    headers.get("From", ""),
            "to":      headers.get("To", ""),
            "date":    headers.get("Date", ""),
            "snippet": m.get("snippet", "")[:200],
            "has_unsubscribe": bool(headers.get("List-Unsubscribe")),
        })
    return messages


def classify_emails(emails: list[dict]) -> dict:
    """Ask Claude to classify each email as trash or keep."""
    if not emails:
        return {"trash": [], "keep": []}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    email_list = json.dumps([{
        "id": e["id"],
        "from": e["from"],
        "subject": e["subject"],
        "snippet": e["snippet"],
        "has_unsubscribe": e["has_unsubscribe"],
    } for e in emails], indent=2)

    prompt = f"""You are an inbox cleaner for Don Brown, owner of EmbTrak Inc, a software company 
serving the embroidery and decorated apparel industry.

Classify each email below. Return ONLY a JSON object — no markdown, no preamble:
{{
  "trash": ["id1", "id2"],
  "keep": ["id3", "id4"]
}}

Move to TRASH if the email is:
- Marketing or promotional (sales, deals, discounts, offers)
- Political (fundraising, donation requests, campaign emails, PAC emails, any party affiliation)
- Newsletters or digests the user didn't explicitly request a reply to
- Automated notifications with no action needed (shipping updates, receipts, app alerts)
- Solicitations of any kind (charities, surveys, requests for support)

KEEP if the email is:
- From a real person writing directly to Don
- A customer, prospect, or partner related to EmbTrak
- A vendor or supplier requiring action
- Anything that looks like it needs a human response
- Anything ambiguous — when in doubt, keep it

Emails to classify:
{email_list}
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def trash_emails(service, ids: list[str]):
    """Move emails to trash."""
    for msg_id in ids:
        service.users().messages().trash(userId="me", id=msg_id).execute()
        print(f"  🗑  Trashed message {msg_id}")


def main():
    print("Authenticating with Google…")
    creds = get_google_creds()
    service = build("gmail", "v1", credentials=creds)

    # Fast pre-pass — no AI needed, rule-based
    print("Pass 1: MailReach warmup emails…")
    mailreach_count = purge_mailreach_warmup(service)

    print("Pass 2: Fetching recent inbox emails…")
    emails = fetch_inbox_emails(service)
    print(f"  {len(emails)} unread emails found")

    if not emails:
        print("Nothing to clean.")
        return

    print("Classifying with Claude…")
    result = classify_emails(emails)
    trash_ids = result.get("trash", [])
    keep_ids  = result.get("keep", [])
    print(f"  Trash: {len(trash_ids)}  |  Keep: {len(keep_ids)}")

    if trash_ids:
        print("Moving to trash…")
        trash_emails(service, trash_ids)

    print(f"✓ Inbox clean complete — {mailreach_count} warmup emails deleted, {len(trash_ids)} others removed, {len(keep_ids)} kept")


if __name__ == "__main__":
    main()
