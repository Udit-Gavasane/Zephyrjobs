"""
gmail_handler.py — Gmail Verification Handler
==============================================
Handles all email-based verification flows during automated job applications.

Covers:
  - OTP / numeric codes in email body
  - Magic links (click-to-verify)
  - Google account 2FA codes

Setup (one-time):
  py gmail_handler.py --setup
  (opens browser once, saves token to sessions/gmail_token.pickle)
"""

import os
import re
import time
import base64
import pickle
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent / '.env')

# ── GMAIL SCOPES ──────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

SESSIONS_DIR = Path(__file__).parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

CREDENTIALS_PATH = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "./agent/gmail_credentials.json"))
TOKEN_PATH       = SESSIONS_DIR / "gmail_token.pickle"

POLL_TIMEOUT_SECONDS  = int(os.getenv("GMAIL_POLL_TIMEOUT", "90"))
POLL_INTERVAL_SECONDS = 3


# ── PORTAL SENDER PATTERNS ────────────────────────────────────────────────────
PORTAL_EMAIL_PATTERNS = {
    "workday": {
        "senders":          ["workday.com", "myworkday.com"],
        "subject_keywords": ["verify", "confirm", "code", "activate"],
    },
    "google": {
        "senders":          ["accounts.google.com", "google.com", "no-reply@accounts.google.com"],
        "subject_keywords": ["sign", "verify", "code", "security", "new device"],
    },
    "greenhouse": {
        "senders":          ["greenhouse.io", "notifications.greenhouse.io"],
        "subject_keywords": ["verify", "confirm", "application"],
    },
    "lever": {
        "senders":          ["lever.co", "hire.lever.co"],
        "subject_keywords": ["verify", "confirm", "application"],
    },
    "linkedin": {
        "senders":          ["linkedin.com", "e.linkedin.com"],
        "subject_keywords": ["verify", "confirm", "code", "security"],
    },
    "indeed": {
        "senders":          ["indeed.com", "em.indeed.com"],
        "subject_keywords": ["verify", "sign in", "code"],
    },
    "generic": {
        "senders":          [],
        "subject_keywords": ["verify", "confirm", "code", "activate", "sign in", "magic link"],
    },
}


# ── OAUTH SETUP ───────────────────────────────────────────────────────────────

def get_gmail_service():
    """
    Returns authenticated Gmail API service.
    Uses saved token if available, otherwise runs OAuth flow once.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Google API packages not installed.\n"
            "Run: py -m pip install google-auth google-auth-oauthlib google-api-python-client"
        )

    creds = None

    if TOKEN_PATH.exists():
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at {CREDENTIALS_PATH}.\n"
                    "Run: py gmail_handler.py --setup"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=9877, prompt="consent")

        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)


# ── EMAIL READER ──────────────────────────────────────────────────────────────

class GmailVerificationHandler:
    def __init__(self):
        self._service = None
        self._seen_message_ids: set = set()

    @property
    def service(self):
        if self._service is None:
            self._service = get_gmail_service()
        return self._service

    def _get_message_body(self, msg: dict) -> str:
        """Extract plain text or HTML body from Gmail message."""
        payload = msg.get("payload", {})
        parts   = payload.get("parts", [])

        def decode(data: str) -> str:
            try:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
            except Exception:
                return ""

        if not parts and payload.get("body", {}).get("data"):
            return decode(payload["body"]["data"])

        for mime_type in ("text/plain", "text/html"):
            for part in parts:
                if part.get("mimeType") == mime_type:
                    data = part.get("body", {}).get("data", "")
                    if data:
                        return decode(data)
                for subpart in part.get("parts", []):
                    if subpart.get("mimeType") == mime_type:
                        data = subpart.get("body", {}).get("data", "")
                        if data:
                            return decode(data)
        return ""

    def _search_emails(self, since: datetime, portal: str = "generic",
                       extra_query: str = "") -> list:
        """Search Gmail for verification emails from a portal."""
        try:
            from googleapiclient.errors import HttpError
        except ImportError:
            return []

        patterns       = PORTAL_EMAIL_PATTERNS.get(portal.lower(), PORTAL_EMAIL_PATTERNS["generic"])
        senders        = patterns["senders"]
        keywords       = patterns["subject_keywords"]
        date_str       = since.strftime("%Y/%m/%d")
        query_parts    = [f"after:{date_str}"]

        if senders:
            query_parts.append("(" + " OR ".join(f"from:{s}" for s in senders) + ")")
        if keywords:
            query_parts.append("(" + " OR ".join(f'subject:"{k}"' for k in keywords) + ")")
        if extra_query:
            query_parts.append(extra_query)

        query = " ".join(query_parts)

        try:
            result = self.service.users().messages().list(
                userId="me", q=query, maxResults=10
            ).execute()
            return result.get("messages", [])
        except Exception as e:
            print(f"[GMAIL] Search error: {e}")
            return []

    def _get_full_message(self, msg_id: str) -> Optional[dict]:
        try:
            return self.service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
        except Exception:
            return None

    def _mark_as_read(self, msg_id: str):
        try:
            self.service.users().messages().modify(
                userId="me", id=msg_id,
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except Exception:
            pass

    # ── EXTRACTION ────────────────────────────────────────────────────────────

    def extract_otp(self, body: str) -> Optional[str]:
        """Extract numeric OTP / verification code from email body."""
        patterns = [
            r"(?:code|pin|otp|verification code|security code)[:\s]+([A-Z0-9]{4,8})",
            r"(?<!\d)(\d{6})(?!\d)",
            r"(?<!\d)(\d{4})(?!\d)",
            r"\b([A-Z0-9]{3}-[A-Z0-9]{3,4})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def extract_magic_link(self, body: str, portal: str = "") -> Optional[str]:
        """Extract click-to-verify magic link from email body."""
        all_urls = re.findall(r'https?://[^\s<>"\']+', body)
        if not all_urls:
            return None

        patterns       = PORTAL_EMAIL_PATTERNS.get(portal.lower(), {})
        portal_domains = patterns.get("senders", [])
        verify_kws     = ["verify", "confirm", "activate", "token", "magic", "signin", "login"]

        for url in all_urls:
            url_lower    = url.lower()
            domain_match = any(d in url_lower for d in portal_domains) if portal_domains else True
            kw_match     = any(kw in url_lower for kw in verify_kws)
            if domain_match and kw_match:
                return url.rstrip(".,;)")

        for url in all_urls:
            if any(kw in url.lower() for kw in verify_kws):
                return url.rstrip(".,;)")

        for url in all_urls:
            if "unsubscribe" not in url.lower() and "privacy" not in url.lower():
                return url.rstrip(".,;)")

        return None

    # ── MAIN POLLING INTERFACE ────────────────────────────────────────────────

    def wait_for_verification(self, portal: str, timeout: int = POLL_TIMEOUT_SECONDS,
                               extra_query: str = "") -> dict:
        """
        Poll Gmail until verification email arrives or timeout.

        Returns:
            {
                "type":     "otp" | "link" | "none",
                "value":    "123456" | "https://...",
                "raw_body": "...",
                "sender":   "noreply@workday.com",
                "subject":  "Verify your email",
            }
        """
        start = datetime.now(timezone.utc)
        print(f"[GMAIL] Waiting for verification — portal={portal}, timeout={timeout}s")

        while (datetime.now(timezone.utc) - start).seconds < timeout:
            messages = self._search_emails(
                since=start - timedelta(seconds=30),
                portal=portal,
                extra_query=extra_query,
            )

            for stub in messages:
                msg_id = stub["id"]
                if msg_id in self._seen_message_ids:
                    continue

                self._seen_message_ids.add(msg_id)
                full_msg = self._get_full_message(msg_id)
                if not full_msg:
                    continue

                msg_ts = int(full_msg.get("internalDate", "0")) / 1000
                msg_dt = datetime.fromtimestamp(msg_ts, tz=timezone.utc)
                if msg_dt < start - timedelta(seconds=30):
                    continue

                body    = self._get_message_body(full_msg)
                headers = {
                    h["name"].lower(): h["value"]
                    for h in full_msg.get("payload", {}).get("headers", [])
                }
                sender  = headers.get("from", "")
                subject = headers.get("subject", "")

                print(f"[GMAIL] Found: From={sender} | Subject={subject}")

                otp = self.extract_otp(body)
                if otp:
                    print(f"[GMAIL] OTP extracted: {otp}")
                    self._mark_as_read(msg_id)
                    return {"type": "otp", "value": otp,
                            "raw_body": body, "sender": sender, "subject": subject}

                link = self.extract_magic_link(body, portal)
                if link:
                    print(f"[GMAIL] Magic link extracted: {link[:60]}...")
                    self._mark_as_read(msg_id)
                    return {"type": "link", "value": link,
                            "raw_body": body, "sender": sender, "subject": subject}

                print(f"[GMAIL] Email found but no OTP/link — preview: {body[:200]}")

            time.sleep(POLL_INTERVAL_SECONDS)

        print(f"[GMAIL] Timeout after {timeout}s — no verification email received")
        return {"type": "none", "value": None, "raw_body": "", "sender": "", "subject": ""}


# ── PLAYWRIGHT INTEGRATION ────────────────────────────────────────────────────

async def handle_email_verification_in_page(
    page,
    gmail: GmailVerificationHandler,
    portal: str,
    timeout: int = 90,
) -> bool:
    """
    Call after triggering an action that sends a verification email.
    Handles OTP input and magic link navigation.
    Returns True if verification succeeded.
    """
    result = gmail.wait_for_verification(portal=portal, timeout=timeout)

    if result["type"] == "none":
        print(f"[VERIFY] No verification email received for portal={portal}")
        return False

    if result["type"] == "otp":
        code = result["value"]
        otp_selectors = [
            "input[type='text'][maxlength='6']",
            "input[type='number'][maxlength='6']",
            "input[placeholder*='code' i]",
            "input[name*='code' i]",
            "input[name*='otp' i]",
            "input[id*='code' i]",
            "input[id*='otp' i]",
            "input[autocomplete='one-time-code']",
        ]
        for sel in otp_selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=3000)
                if el:
                    await el.fill(code)
                    for sb in [
                        "button[type='submit']",
                        "button:has-text('Verify')",
                        "button:has-text('Confirm')",
                        "button:has-text('Next')",
                        "button:has-text('Continue')",
                    ]:
                        try:
                            btn = await page.query_selector(sb)
                            if btn and await btn.is_visible():
                                await btn.click()
                                await page.wait_for_timeout(1500)
                                print(f"[VERIFY] OTP {code} submitted")
                                return True
                        except Exception:
                            continue
                    await el.press("Enter")
                    await page.wait_for_timeout(1500)
                    print(f"[VERIFY] OTP {code} submitted via Enter")
                    return True
            except Exception:
                continue

        print(f"[VERIFY] Got OTP {code} but could not find input field")
        return False

    if result["type"] == "link":
        link = result["value"]
        print(f"[VERIFY] Navigating to magic link...")
        await page.goto(link, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        print(f"[VERIFY] Magic link navigated")
        return True

    return False


# ── SETUP + TEST ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--setup" in sys.argv:
        print("=== ZephyrJobs Gmail OAuth Setup ===")
        print(f"Looking for credentials at: {CREDENTIALS_PATH}")
        if not CREDENTIALS_PATH.exists():
            print("\nERROR: gmail_credentials.json not found.")
            print("\nSteps:")
            print("  1. Go to https://console.cloud.google.com")
            print("  2. Create project ZephyrJobs")
            print("  3. Enable Gmail API")
            print("  4. OAuth consent screen → External → add your Gmail as test user")
            print("  5. Credentials → OAuth client ID → Desktop app → Download JSON")
            print(f"  6. Save as: {CREDENTIALS_PATH}")
            sys.exit(1)

        print("\nOpening browser for one-time authorization...")
        svc     = get_gmail_service()
        profile = svc.users().getProfile(userId="me").execute()
        print(f"\n✓ Authenticated as: {profile['emailAddress']}")
        print(f"✓ Token saved to: {TOKEN_PATH}")
        print("\nSetup complete.")

    elif "--test" in sys.argv:
        print("=== Testing Gmail Handler ===")
        handler = GmailVerificationHandler()
        print("Waiting 30s — send yourself an email with 'Your code is 123456'")
        result  = handler.wait_for_verification(portal="generic", timeout=30)
        print(f"Result: {result}")

    else:
        print("Usage:")
        print("  py gmail_handler.py --setup   # one-time OAuth setup")
        print("  py gmail_handler.py --test    # test email detection")
