"""Reading the verification e-mails portals send (a code to type, or a link to open), from your Gmail, read-only.

Set up once (README "Gmail for verification codes"): a Google Cloud project with the Gmail API, an OAuth client of
type "Desktop app" saved as data/google_client.json, then `jev-apply gmail-connect`: Google asks you, in your
browser, to let jev-apply *read* your mail (gmail.readonly: it can't send, change or delete anything). The token
is kept in data/google_token.json (git-ignored, like all of data/).

A run only ever looks at mail that arrived after it started waiting, that reads like a verification message, and
prefers the portal's own sender. It takes the code or the link and nothing else; nothing is stored.

Plain HTTP (httpx) and Google's documented OAuth endpoints: no Google libraries to install."""

import base64
import hashlib
import html
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from .files import write_json

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
WORDS = r"(?i:code|verification|verify|one[- ]?time|otp|confirm|activate|security|passcode|pin)"
# A code is digits (4 to 8), or 6 to 8 capitals and digits with at least one digit: never an ordinary word.
TOKEN_CODE = r"(?:[0-9]{4,8}|(?=[A-Z0-9]*[0-9])[A-Z0-9]{6,8})"
CODE = re.compile(rf"{WORDS}[^0-9]{{0,80}}?\b({TOKEN_CODE})\b|\b([0-9]{{4,8}})\b[^0-9]{{0,40}}{WORDS}")
# What a verification message's subject says (newsletters that merely mention "code" or "security" don't count).
SUBJECT = re.compile(
    r"verif|\bcode\b|one[- ]?time|\botp\b|passcode|confirm (your )?(e-?mail|account|registration)|activat|"
    r"sign[- ]?in|log[- ]?in|reset your password|account (created|setup|registration)",
    re.I,
)
# Gmail search: the last day's mail with any of these words (newer mail is then filtered by time).
QUERY = "newer_than:1d {code verification verify otp confirm activate passcode security}"
LINK = re.compile(r"https?://[^\s\"'<>]+", re.I)
LINK_WORDS = re.compile(r"verif|confirm|activat|validate|token|registration|account", re.I)
# Senders portals use for their mail, when it isn't the company's own domain.
PORTAL_SENDERS = {
    "myworkdayjobs.com": ["myworkday.com", "workday.com"],
    "oraclecloud.com": ["oracle.com", "oraclecloud.com"],
    "icims.com": ["icims.com"],
    "successfactors": ["successfactors.com", "sap.com"],
    "taleo.net": ["taleo.net", "oracle.com"],
}


def folder(data_dir):
    return Path(data_dir)


def client_path(data_dir):
    return folder(data_dir) / "google_client.json"


def token_path(data_dir):
    return folder(data_dir) / "google_token.json"


def configured(data_dir):
    return token_path(data_dir).is_file() and client_path(data_dir).is_file()


def client(data_dir):
    raw = json.loads(client_path(data_dir).read_text(encoding="utf-8"))
    info = raw.get("installed") or raw.get("web") or raw
    return info["client_id"], info.get("client_secret", "")


def connect(data_dir, open_browser=True):
    """Ask Google, in your browser, to let jev-apply read your mail; keep the token. Returns your address."""
    client_id, secret = client(data_dir)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    redirect = f"http://127.0.0.1:{port}/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Back(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the http.server API
            got.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<h2>jev-apply can now read your verification e-mails. You can close this tab.</h2>".encode()
            )

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Back)
    threading.Thread(target=server.handle_request, daemon=True).start()
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent",
    })  # fmt: skip
    print(f"Opening Google to allow read-only access to your Gmail. If no browser opens, visit:\n{url}")
    if open_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + 300
    while not got and time.monotonic() < deadline:
        time.sleep(0.3)
    server.server_close()
    if got.get("state", [""])[0] != state or "code" not in got:
        raise RuntimeError(f"Google didn't return an approval: {got.get('error', ['no answer within 5 minutes'])[0]}")
    token = httpx.post(TOKEN, data={
        "client_id": client_id, "client_secret": secret, "code": got["code"][0], "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect,
    }, timeout=30).json()  # fmt: skip
    if "refresh_token" not in token:
        raise RuntimeError(f"Google gave no lasting token: {token.get('error_description') or token}")
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
    write_json(token_path(data_dir), token)
    return profile_address(data_dir)


def access_token(data_dir):
    token = json.loads(token_path(data_dir).read_text(encoding="utf-8"))
    if token.get("expires_at", 0) > time.time():
        return token["access_token"]
    client_id, secret = client(data_dir)
    fresh = httpx.post(TOKEN, data={"client_id": client_id, "client_secret": secret,
                                    "refresh_token": token["refresh_token"], "grant_type": "refresh_token"},
                       timeout=30).json()  # fmt: skip
    if "access_token" not in fresh:
        raise RuntimeError(f"Gmail access was refused ({fresh.get('error')}): run `jev-apply gmail-connect` again")
    token.update(access_token=fresh["access_token"], expires_at=time.time() + int(fresh.get("expires_in", 3600)) - 60)
    write_json(token_path(data_dir), token)
    return token["access_token"]


def get(data_dir, path, **params):
    response = httpx.get(f"{API}/{path}", params=params, timeout=30,
                         headers={"Authorization": f"Bearer {access_token(data_dir)}"})  # fmt: skip
    response.raise_for_status()
    return response.json()


def profile_address(data_dir):
    return get(data_dir, "profile").get("emailAddress")


def senders_for(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    parts = host.split(".")
    own = ".".join(parts[-2:]) if len(parts) >= 2 else host
    extra = [s for key, found in PORTAL_SENDERS.items() if key in host for s in found]
    tenant = parts[0] if "myworkdayjobs" in host else ""  # Workday mails as <tenant>@myworkday.com
    return [own, *extra], tenant


def body_text(payload):
    """A message's text, from its plain or HTML parts."""
    out = []

    def walk(part):
        data = (part.get("body") or {}).get("data")
        if data and part.get("mimeType", "").startswith("text/"):
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            if part["mimeType"] == "text/html":
                text = re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>", r" \1 ", text)
                text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            out.append(text)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return re.sub(r"[ \t]+", " ", "\n".join(out))


def recent(data_dir, since, url):
    """Verification-looking messages since `since` (epoch seconds), the portal's own first: [(sender, subject,
    text)]."""
    senders, tenant = senders_for(url)
    # Gmail itself picks the portal's mail, or mail whose subject is about verification: newsletters that merely
    # mention a "code" never crowd it out of the results.
    froms = " OR ".join(f"from:{s}" for s in senders)
    query = f"{QUERY.split(' ', 1)[0]} ({froms} OR subject:{{verify verification code otp passcode activate}})"
    found = get(data_dir, "messages", q=query, maxResults=15).get("messages") or []
    out = []
    for item in found:
        message = get(data_dir, f"messages/{item['id']}", format="full")
        if int(message.get("internalDate", 0)) / 1000 < since - 5:
            continue
        headers = {h["name"].lower(): h["value"] for h in (message.get("payload") or {}).get("headers") or []}
        sender, subject = headers.get("from", ""), headers.get("subject", "")
        mine = any(s in sender.lower() for s in senders) or bool(tenant and tenant in sender.lower())
        if not mine and not SUBJECT.search(subject):
            continue  # a newsletter or a statement, not this portal's verification message
        # This company's own Workday sender (abb@...) first, then the portal's, then mail with a verification subject.
        rank = 0 if tenant and tenant in sender.lower() else (1 if mine else 2)
        out.append((rank, -int(message.get("internalDate", 0)), sender, subject, body_text(message.get("payload"))))
    return [(s, subj, text) for _, _, s, subj, text in sorted(out)]


def applied_before(data_dir, url):
    """Has this company's own Workday (<tenant>@myworkday.com) written to you about an application before ("We've
    received your application")? None when Gmail isn't connected or the portal isn't a Workday one; read-only."""
    _, tenant = senders_for(url)
    if not tenant or not configured(data_dir):
        return None
    query = (f"from:{tenant}@myworkday.com subject:(application OR applying OR applied OR received OR "
             f"\"thank you\") -subject:(verify OR verification OR password OR reset OR activate)")  # fmt: skip
    try:
        return bool(get(data_dir, "messages", q=query, maxResults=1).get("messages"))
    except Exception:  # noqa: BLE001 - a Gmail hiccup leaves the record as the only answer
        return None


def code_in(subject, text):
    found = CODE.search(f"{subject}\n{text}")
    return next((g for g in (found.group(1), found.group(2)) if g), None) if found else None


def domain(host):
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def link_in(text, url=""):
    """A verification link that points to the portal itself (its site, or Workday's / Oracle's own), never
    anywhere else."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    allowed = {domain(host)} | {d for key, found in PORTAL_SENDERS.items() if key in host for d in found}
    workday = "myworkdayjobs" in host
    for link in (u.rstrip(").,;") for u in LINK.findall(text)):
        target_host = (urllib.parse.urlparse(link).hostname or "").lower()
        if workday:
            # Every company's Workday is "myworkdayjobs.com": only this company's own address (acme.wd5...)
            # counts, or Motorola's activation link would be opened for Siemens.
            ok = target_host == host
        else:
            ok = not url or domain(target_host) in allowed
        if ok and LINK_WORDS.search(link) and "unsubscribe" not in link.lower():
            return link
    return None


def wait_for(data_dir, since, url, want="code", timeout=180, poll=6):
    """The code (or verification link) in the newest matching message after `since`, waiting up to `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for _, subject, text in recent(data_dir, since, url):
                found = code_in(subject, text) if want == "code" else link_in(text, url)
                if found:
                    return found
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError):
            pass  # Gmail briefly unreachable: try again on the next poll
        time.sleep(poll)
    return None


def now():
    return datetime.now(timezone.utc).timestamp()
