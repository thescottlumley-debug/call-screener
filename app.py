import os
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
import re
from datetime import datetime
import pytz
from flask import Flask, request, jsonify
from openai import OpenAI
import telnyx

app = Flask(__name__)
telnyx.api_key = os.environ.get("TELNYX_API_KEY")
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
SCOTT_REAL_NUMBER = os.environ.get("SCOTT_REAL_NUMBER")
TELNYX_NUMBER = os.environ.get("TELNYX_NUMBER", "+16159495810")
SCOTT_TIMEZONE = os.environ.get("SCOTT_TIMEZONE", "America/Chicago")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "thescottlumley-debug/call-screener")

QUIET_START = int(os.environ.get("QUIET_START_HOUR", "21"))  # 9pm
QUIET_END   = int(os.environ.get("QUIET_END_HOUR",   "7"))   # 7am

call_sessions = {}
MAX_TURNS = 8

# Pending SMS relay decisions: caller_id -> ccid
pending_relay = {}

# Appointment schedule store
appointments = []

# ✨ Number lookup cache so we don't re-lookup same number
lookup_cache = {}

# ✨ Feature 10: Do Not Disturb mode (toggled via SMS)
dnd_mode = False

# ✨ Feature 12: VIP numbers — always get through, even during quiet hours / DND
vip_numbers = []

# ✨ Feature 11: Daily summary — track calls per day
daily_call_log = []  # list of {time, caller_id, name, action, purpose}


# ─────────────────────────────────────────────
# ✨ Feature 10: Do Not Disturb
# ─────────────────────────────────────────────

def is_dnd():
    return dnd_mode

def set_dnd(on: bool):
    global dnd_mode
    dnd_mode = on
    print(f"[DND] Mode {'ON' if on else 'OFF'}")


# ─────────────────────────────────────────────
# ✨ Feature 12: VIP Fast-Track
# ─────────────────────────────────────────────

def is_vip(caller_id):
    return caller_id in vip_numbers

def add_vip(number):
    if number not in vip_numbers:
        vip_numbers.append(number)
        print(f"[VIP] Added {number}")
        return True
    return False

def remove_vip(number):
    if number in vip_numbers:
        vip_numbers.remove(number)
        print(f"[VIP] Removed {number}")
        return True
    return False


# ─────────────────────────────────────────────
# ✨ Feature 11: Daily Call Summary
# ─────────────────────────────────────────────

def log_daily_call(caller_id, name, action, purpose):
    """Log each call for the daily summary."""
    tz  = pytz.timezone(SCOTT_TIMEZONE)
    now = datetime.now(tz)
    daily_call_log.append({
        "time":      now.strftime("%I:%M %p"),
        "caller_id": caller_id,
        "name":      name or "Unknown",
        "action":    action or "unknown",
        "purpose":   purpose or "unknown",
        "date":      now.strftime("%Y-%m-%d"),
    })
    # Keep only last 100 entries
    if len(daily_call_log) > 100:
        daily_call_log.pop(0)

def build_daily_summary():
    """Build a daily SMS summary of all calls today."""
    tz    = pytz.timezone(SCOTT_TIMEZONE)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    today_calls = [c for c in daily_call_log if c.get("date") == today]

    if not today_calls:
        return f"📊 ARIA Daily Summary — {datetime.now(tz).strftime('%B %d')}\nNo calls today."

    total     = len(today_calls)
    forwarded = sum(1 for c in today_calls if "forward" in c.get("action", ""))
    voicemails= sum(1 for c in today_calls if "voicemail" in c.get("action", ""))
    blocked   = sum(1 for c in today_calls if "block" in c.get("action", ""))
    relayed   = sum(1 for c in today_calls if "relay" in c.get("action", ""))

    lines = [
        f"📊 ARIA Daily Summary — {datetime.now(tz).strftime('%A, %B %d')}",
        f"Total calls: {total}  |  Forwarded: {forwarded}  |  Voicemails: {voicemails}  |  Blocked: {blocked}  |  Relayed: {relayed}",
        "",
    ]
    for c in today_calls[-10:]:  # last 10 calls
        emoji = {"forward": "✅", "voicemail": "📬", "block": "🚫", "relay": "📲"}.get(
            c.get("action", "").split("_")[0], "📞")
        lines.append(f"{emoji} {c['time']} — {c['name']} — {c['purpose']}")

    return "\n".join(lines)

def send_daily_summary():
    """Send the daily summary SMS to Scott."""
    summary = build_daily_summary()
    print(f"[Daily Summary] Sending: {summary[:100]}")
    send_sms(SCOTT_REAL_NUMBER, summary)


# ─────────────────────────────────────────────
# ✨ NEW: Web Lookup — number intelligence
# ─────────────────────────────────────────────

def lookup_number(caller_id):
    """
    Look up a phone number using a free web search to determine:
    - Is it a known spam/scam number?
    - Is it a business? What business?
    - Is it a personal cell? Any public info?
    Returns a dict: {"is_spam": bool, "is_business": bool, "business_name": str,
                     "spam_score": int (0-10), "summary": str}
    """
    # Return cached result if available
    if caller_id in lookup_cache:
        print(f"[Lookup] Cache hit for {caller_id}")
        return lookup_cache[caller_id]

    # Format number for search (strip +1)
    digits = re.sub(r'\D', '', caller_id)
    if digits.startswith('1') and len(digits) == 11:
        digits = digits[1:]
    formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else caller_id

    result = {
        "is_spam": False,
        "is_business": False,
        "business_name": None,
        "spam_score": 0,
        "summary": "No public information found.",
    }

    try:
        # Search for the number via DuckDuckGo instant answer API (no key needed)
        query = urllib.parse.quote(f'phone number {formatted} who called spam scam business')
        search_url = f"https://api.duckduckgo.com/?q={query}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ARIA-CallScreener/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read().decode())

        # Pull abstract text and related topics
        abstract  = data.get("AbstractText", "") or ""
        answer    = data.get("Answer", "") or ""
        related   = " ".join(t.get("Text", "") for t in data.get("RelatedTopics", [])[:3])
        raw_text  = f"{abstract} {answer} {related}".strip()

        if not raw_text:
            raw_text = "No public information found for this number."

        # Ask GPT to interpret what was found
        interp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content":
                f"Phone number: {caller_id}\n"
                f"Web search results: \"{raw_text[:600]}\"\n\n"
                f"Based on this information, classify the phone number.\n"
                f"Reply ONLY with JSON:\n"
                f'{{"is_spam": false, "is_business": true, "business_name": "AT&T", '
                f'"spam_score": 2, "summary": "This is an AT&T customer service line."}}\n\n'
                f"spam_score: 0=clean, 5=suspicious, 8+=likely spam, 10=confirmed spam\n"
                f"If no info found, set is_spam=false, spam_score=0, summary='No public info found — treat as unknown caller.'\n"
                f"NEVER mark a number as spam unless there is strong evidence."
            }],
            response_format={"type": "json_object"},
            max_tokens=80,
            temperature=0.0,
        )
        result = json.loads(interp.choices[0].message.content)
        print(f"[Lookup] {caller_id} → spam={result.get('is_spam')} "
              f"score={result.get('spam_score')} biz={result.get('business_name')}")

    except Exception as e:
        print(f"[Lookup] Error for {caller_id}: {e}")
        result = {
            "is_spam": False, "is_business": False,
            "business_name": None, "spam_score": 0,
            "summary": "Lookup unavailable — treat as unknown caller.",
        }

    # Cache result for this session
    lookup_cache[caller_id] = result
    return result


# ─────────────────────────────────────────────
# ✨ NEW: Deep Memory & Relationship Tracking
# ─────────────────────────────────────────────

def get_relationship_summary(caller_id):
    """
    Build a rich relationship context string for ARIA to use
    based on everything known about this caller.
    """
    rec = caller_history["callers"].get(caller_id)
    if not rec:
        return None

    name        = rec.get("name") or "Unknown"
    call_count  = rec.get("call_count", 0)
    caller_type = rec.get("caller_type") or "unknown"
    last_purpose= rec.get("last_purpose") or "unknown reason"
    last_urgency= rec.get("last_urgency")
    first_call  = rec.get("first_call", "")[:10]
    last_call   = rec.get("last_call", "")[:10]
    notes       = rec.get("notes", [])
    voicemails  = rec.get("voicemails", [])

    # Relationship strength
    if call_count >= 10:
        relationship = "frequent caller"
    elif call_count >= 3:
        relationship = "returning caller"
    else:
        relationship = "occasional caller"

    summary = (
        f"{name} is a {relationship} ({call_count} total calls, first on {first_call}, "
        f"last on {last_call}). Type: {caller_type}. "
        f"Last called about: {last_purpose}."
    )
    if last_urgency:
        summary += f" Last urgency: {last_urgency}/10."
    if notes:
        summary += f" Notes: {'; '.join(notes[-3:])}."
    if voicemails:
        summary += f" Last voicemail: \"{voicemails[-1]['summary']}\"."

    return summary


def add_caller_note(caller_id, note):
    """Add a manual note to a caller's record."""
    now = datetime.utcnow().isoformat() + "Z"
    rec = caller_history["callers"].setdefault(caller_id, {
        "name": None, "first_call": now, "last_call": now,
        "call_count": 0, "last_action": None,
        "last_purpose": None, "last_urgency": None,
        "caller_type": None, "notes": [], "voicemails": [],
    })
    rec.setdefault("notes", []).append(f"[{now[:10]}] {note}")
    rec["notes"] = rec["notes"][-10:]  # keep last 10 notes
    save_caller_history_to_github(caller_history)
    print(f"[Note] Added note for {caller_id}: {note}")


def get_caller_stats():
    """Return overall stats about all callers for STATUS command."""
    callers = caller_history.get("callers", {})
    total   = len(callers)
    spam_blocked  = sum(1 for r in callers.values() if r.get("last_action") == "block")
    forwarded     = sum(1 for r in callers.values() if "forward" in (r.get("last_action") or ""))
    voicemails_left = sum(1 for r in callers.values() if r.get("voicemails"))
    frequent      = [r.get("name") or num for num, r in callers.items()
                     if r.get("call_count", 0) >= 3]
    return {
        "total": total,
        "spam_blocked": spam_blocked,
        "forwarded": forwarded,
        "voicemails_left": voicemails_left,
        "frequent_callers": frequent[:5],
    }


# ─────────────────────────────────────────────
# Contacts helpers
# ─────────────────────────────────────────────

def load_contacts():
    with open("contacts_whitelist.json", "r") as f:
        return json.load(f)

def save_contacts(contacts):
    with open("contacts_whitelist.json", "w") as f:
        json.dump(contacts, f, indent=2)

def name_in_whitelist(text):
    contacts = load_contacts()
    text_lower = text.lower().strip()
    for contact in contacts["approved_names"]:
        if contact.lower() in text_lower or text_lower in contact.lower():
            return True
    return False

def number_in_whitelist(caller_id):
    contacts = load_contacts()
    return caller_id in contacts.get("approved_numbers", [])

def add_number_to_whitelist(number):
    contacts = load_contacts()
    if number not in contacts.get("approved_numbers", []):
        contacts.setdefault("approved_numbers", []).append(number)
        save_contacts(contacts)
        print(f"[Whitelist] Added {number}")
        return True
    return False


# ─────────────────────────────────────────────
# Caller history
# ─────────────────────────────────────────────

def load_caller_history():
    try:
        with open("caller_history.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"callers": {}}

caller_history = load_caller_history()


def save_caller_history_to_github(data):
    if not GITHUB_TOKEN:
        try:
            with open("caller_history.json", "w") as f:
                json.dump(data, f, indent=2)
            print("[Caller History] Saved locally (no GITHUB_TOKEN set)")
        except Exception as e:
            print(f"[Caller History] Local save failed: {e}")
        return
    try:
        get_req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/caller_history.json",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json"},
        )
        try:
            with urllib.request.urlopen(get_req) as r:
                sha = json.loads(r.read())["sha"]
        except urllib.error.HTTPError:
            sha = None
        content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        body_dict = {"message": "Update caller history [auto]", "content": content}
        if sha:
            body_dict["sha"] = sha
        put_req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/caller_history.json",
            data=json.dumps(body_dict).encode(),
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(put_req) as r:
            print(f"[Caller History] Saved to GitHub → {r.status}")
    except urllib.error.HTTPError as e:
        print(f"[Caller History] GitHub save failed: {e.code} {e.read().decode()}")
    except Exception as e:
        print(f"[Caller History] Unexpected error: {e}")


def get_caller_record(caller_id):
    return caller_history["callers"].get(caller_id)


def update_caller_record(caller_id, name=None, action=None, voicemail_summary=None,
                          purpose=None, urgency=None, caller_type=None,
                          lookup_summary=None, increment_count=True):
    now = datetime.utcnow().isoformat() + "Z"
    rec = caller_history["callers"].setdefault(caller_id, {
        "name": None, "first_call": now, "last_call": now,
        "call_count": 0, "last_action": None,
        "last_purpose": None, "last_urgency": None,
        "caller_type": None, "lookup_summary": None,
        "notes": [], "voicemails": [],
    })
    rec["last_call"] = now
    if increment_count:
        rec["call_count"] = rec.get("call_count", 0) + 1
    if name:   rec["name"] = name
    if action: rec["last_action"] = action
    if purpose: rec["last_purpose"] = purpose
    if urgency is not None: rec["last_urgency"] = urgency
    if caller_type: rec["caller_type"] = caller_type
    if lookup_summary: rec["lookup_summary"] = lookup_summary
    if voicemail_summary:
        rec.setdefault("voicemails", []).append({"date": now, "summary": voicemail_summary})
        rec["voicemails"] = rec["voicemails"][-5:]
    save_caller_history_to_github(caller_history)


# ─────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────

def is_quiet_hours():
    tz = pytz.timezone(SCOTT_TIMEZONE)
    hour = datetime.now(tz).hour
    if QUIET_START > QUIET_END:
        return hour >= QUIET_START or hour < QUIET_END
    return QUIET_START <= hour < QUIET_END

def current_time_str():
    tz = pytz.timezone(SCOTT_TIMEZONE)
    return datetime.now(tz).strftime("%A, %B %d at %I:%M %p %Z")


# ─────────────────────────────────────────────
# Telnyx helpers
# ─────────────────────────────────────────────

def telnyx_action(ccid, action, **kwargs):
    body = json.dumps(kwargs).encode()
    url = f"https://api.telnyx.com/v2/calls/{ccid}/actions/{action}"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {telnyx.api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            print(f"[Telnyx] {action} → {r.status}")
            return r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if e.code == 422 and action == "transcription_start":
            print(f"[Telnyx] {action} already running (ok)")
        else:
            print(f"[Telnyx] {action} ERROR {e.code}: {body}")
        return e.code

def encode_state(s):
    return base64.b64encode(s.encode()).decode()

def decode_state(s):
    if not s:
        return ""
    try:
        return base64.b64decode(s).decode()
    except Exception:
        return s

def speak(ccid, message, client_state=None):
    kwargs = {
        "payload": message,
        "payload_type": "text",
        "voice": "Polly.Joanna-Neural",
        "language": "en-US",
    }
    if client_state:
        kwargs["client_state"] = encode_state(client_state)
    telnyx_action(ccid, "speak", **kwargs)

def start_listening(ccid):
    telnyx_action(ccid, "transcription_start",
        transcription_engine="Deepgram",
        transcription_model="flux",
        language="en",
    )

def send_sms(to, message):
    body = json.dumps({"from": TELNYX_NUMBER, "to": to, "text": message}).encode()
    req = urllib.request.Request(
        "https://api.telnyx.com/v2/messages", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {telnyx.api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            print(f"[SMS] Sent to {to} → {r.status}")
            return r.status
    except urllib.error.HTTPError as e:
        print(f"[SMS] ERROR {e.code}: {e.read().decode()}")
        return e.code


# ─────────────────────────────────────────────
# Live SMS Relay
# ─────────────────────────────────────────────

def send_relay_sms(ccid, caller_id, name, purpose, urgency, caller_type=None):
    caller_rec = get_caller_record(caller_id)
    call_count = caller_rec.get("call_count", 1) if caller_rec else 1
    history_note = f" (call #{call_count})" if call_count > 1 else " (first call)"
    display_name = name if name else "Unknown caller"
    urgency_bar = "🔴 HIGH" if urgency >= 8 else ("🟡 MED" if urgency >= 5 else "🟢 LOW")
    type_note = f"\nType: {caller_type}" if caller_type else ""
    msg = (
        f"📲 ARIA — Incoming Call{history_note}\n"
        f"From: {display_name} ({caller_id}){type_note}\n"
        f"Re: {purpose}\n"
        f"Urgency: {urgency_bar} ({urgency}/10)\n\n"
        f"Reply FORWARD, VM, or SCHEDULE to book a callback."
    )
    print(f"[SMS Relay] Texting Scott: {msg}")
    send_sms(SCOTT_REAL_NUMBER, msg)
    pending_relay[caller_id] = ccid
    print(f"[SMS Relay] Waiting for Scott's decision on {caller_id}")


# ─────────────────────────────────────────────
# Caller briefing before connecting
# ─────────────────────────────────────────────

def build_briefing(caller_id, name, purpose, urgency, caller_type=None):
    caller_rec = get_caller_record(caller_id)
    call_count = caller_rec.get("call_count", 1) if caller_rec else 1
    display = name if name else "an unknown caller"
    count_note = f"This is their call number {call_count}. " if call_count > 1 else ""
    urgency_note = "They say it is urgent. " if urgency and urgency >= 8 else ""
    type_note = f"They appear to be a {caller_type}. " if caller_type else ""
    briefing = (
        f"Heads up Scott. Connecting you now with {display}. "
        f"{count_note}{type_note}"
        f"They are calling about: {purpose}. "
        f"{urgency_note}Go ahead."
    )
    return briefing


# ─────────────────────────────────────────────
# ✨ NEW: Appointment Scheduling
# ─────────────────────────────────────────────

def book_appointment(name, number, time_str, purpose):
    """Store the appointment and notify Scott via SMS."""
    appt = {
        "name": name,
        "number": number,
        "time_str": time_str,
        "purpose": purpose,
        "booked_at": datetime.utcnow().isoformat() + "Z",
    }
    appointments.append(appt)
    print(f"[Appointment] Booked: {appt}")

    msg = (
        f"📅 ARIA — Callback Scheduled\n"
        f"Name: {name} ({number})\n"
        f"Time: {time_str}\n"
        f"Re: {purpose}\n\n"
        f"Reply APPTS to see all scheduled callbacks."
    )
    send_sms(SCOTT_REAL_NUMBER, msg)
    return appt

def list_appointments():
    if not appointments:
        return "No callbacks currently scheduled."
    lines = ["📅 Scheduled Callbacks:"]
    for i, a in enumerate(appointments, 1):
        lines.append(f"{i}. {a['name']} ({a['number']}) — {a['time_str']} — Re: {a['purpose']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# ✨ NEW: Caller Type Detection
# ─────────────────────────────────────────────

def detect_caller_type(transcript, purpose):
    """
    Classify the caller type so ARIA can ask the right follow-up questions.
    Returns one of: contractor, recruiter, doctor, sales, legal, personal,
                    business, media, government, unknown
    """
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content":
                f"Based on what the caller said and their stated purpose, classify the caller type.\n"
                f"Caller said: \"{transcript}\"\n"
                f"Purpose: \"{purpose}\"\n"
                f"Choose exactly one type: contractor, recruiter, doctor, sales, legal, personal, "
                f"business, media, government, unknown\n"
                f"Reply ONLY with JSON: {{\"type\": \"sales\"}}"
            }],
            response_format={"type": "json_object"},
            max_tokens=20,
            temperature=0.0,
        )
        return json.loads(resp.choices[0].message.content).get("type", "unknown")
    except Exception:
        return "unknown"


# ─────────────────────────────────────────────
# ✨ NEW: Dynamic Follow-up Questions by Caller Type
# ─────────────────────────────────────────────

FOLLOWUP_QUESTIONS = {
    "contractor": "What type of work are you quoting, and do you have a license and insurance?",
    "recruiter":  "What is the role and the compensation range you're looking to fill?",
    "doctor":     "Is this regarding a medical matter or appointment for Scott personally?",
    "sales":      "What product or service are you offering, and did Scott request this call?",
    "legal":      "Is this regarding an existing matter, and is it time sensitive?",
    "personal":   "How do you know Scott personally, and is everything okay?",
    "business":   "What company are you calling from, and what is the nature of the business?",
    "media":      "What publication or outlet are you with, and what is the story about?",
    "government": "What agency or department are you calling from, and is this time sensitive?",
    "unknown":    "Could you tell me a bit more about the nature of your call?",
}

def get_followup_for_type(caller_type):
    return FOLLOWUP_QUESTIONS.get(caller_type, FOLLOWUP_QUESTIONS["unknown"])


# ─────────────────────────────────────────────
# ✨ NEW: Intelligent Voicemail Follow-up
# ─────────────────────────────────────────────

def ai_voicemail_followup(transcript, session):
    """
    After the caller leaves their initial voicemail message, ARIA asks
    smart follow-up questions based on caller type to get complete info
    for Scott. Returns {"done": True/False, "question": "..."}
    """
    caller_type = session.get("caller_type", "unknown")
    vm_turns = session.get("vm_turns", 0)
    vm_history = session.get("vm_history", [])

    # After 2 follow-up turns, wrap up
    if vm_turns >= 2:
        return {"done": True, "question": None}

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content":
                f"You are ARIA, a professional AI assistant taking a message for Scott Lumley.\n"
                f"Caller type: {caller_type}\n"
                f"The caller just said: \"{transcript}\"\n"
                f"Previous voicemail exchange: {json.dumps(vm_history)}\n"
                f"Voicemail follow-up turn: {vm_turns + 1} of 2\n\n"
                f"Based on what they said and their caller type, decide:\n"
                f"1. Do you have enough info (name, purpose, contact details, key details)?\n"
                f"2. If not, what is the ONE most important follow-up question to ask?\n\n"
                f"For a {caller_type}, important details include:\n"
                f"- contractor: scope of work, timeline, license status\n"
                f"- recruiter: company, role, salary, remote/onsite\n"
                f"- doctor: which practice, appointment or results, patient name\n"
                f"- sales: company, product, whether Scott requested the call\n"
                f"- legal: case/matter reference, attorney name, deadline\n"
                f"- personal: relationship to Scott, callback number, urgency\n"
                f"- business: company name, deal/topic, decision maker\n\n"
                f"Reply ONLY with JSON:\n"
                f"{{\"done\": false, \"question\": \"What is your best callback number and time?\"}}\n"
                f"{{\"done\": true, \"question\": null}}"
            }],
            response_format={"type": "json_object"},
            max_tokens=80,
            temperature=0.2,
        )
        result = json.loads(resp.choices[0].message.content)
        print(f"[VM Follow-up] turn={vm_turns} done={result.get('done')} q={result.get('question')}")
        return result
    except Exception as e:
        print(f"[VM Follow-up Error] {e}")
        return {"done": True, "question": None}


# ─────────────────────────────────────────────
# Voicemail
# ─────────────────────────────────────────────

def start_voicemail(ccid, caller_id, reason=""):
    session = call_sessions.get(ccid, {})
    session["voicemail"] = True
    session["voicemail_caller"] = caller_id
    session["voicemail_reason"] = reason
    session["vm_turns"] = 0
    session["vm_history"] = []
    session["vm_transcript_parts"] = []
    call_sessions[ccid] = session
    speak(ccid,
        "Scott is not available right now. Please leave your name, "
        "a brief message, and the best way to reach you.",
        client_state="voicemail_prompt"
    )

def finalize_voicemail(ccid, caller_id, session):
    """Combine all VM transcript parts, clean up, and notify Scott."""
    full_transcript = " ".join(session.get("vm_transcript_parts", []))
    name = session.get("caller_name")
    purpose = session.get("caller_purpose")
    urgency = session.get("caller_urgency")
    caller_type = session.get("caller_type")

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content":
                f"Clean up this voicemail transcript into a clear, professional message summary. "
                f"Include all key details: who called, why, and any important info they provided. "
                f"Keep it concise but complete.\nTranscript: \"{full_transcript}\""
            }],
            max_tokens=150,
            temperature=0.2,
        )
        clean = resp.choices[0].message.content.strip()
    except Exception:
        clean = full_transcript

    update_caller_record(caller_id, action="voicemail", voicemail_summary=clean,
                          purpose=purpose, urgency=urgency, caller_type=caller_type,
                          increment_count=False)

    caller_rec = get_caller_record(caller_id)
    stored_name = name or (caller_rec.get("name") if caller_rec else None)
    display = f"{stored_name} ({caller_id})" if stored_name else caller_id

    urgency_str = f"\nUrgency: {urgency}/10" if urgency is not None else ""
    purpose_str = f"\nRe: {purpose}" if purpose else ""
    type_str = f"\nType: {caller_type}" if caller_type else ""
    msg = f"📞 Voicemail from {display}:{type_str}{purpose_str}{urgency_str}\n{clean}"
    print(f"[Voicemail] Notifying Scott: {msg}")
    send_sms(SCOTT_REAL_NUMBER, msg)

    speak(ccid,
        "Thank you. Your message has been saved and Scott will get back to you. Have a great day. Goodbye.",
        client_state="screened"
    )
    telnyx_action(ccid, "record_stop")
    telnyx_action(ccid, "hangup")
    call_sessions.pop(ccid, None)


# ─────────────────────────────────────────────
# ✨ UPGRADED: Full conversational AI screening
# ─────────────────────────────────────────────

def looks_like_greeting_echo(transcript):
    echo_phrases = ["scott lumley", "ai assistant", "how may i help", "you have reached",
                    "aria", "i am aria", "hello this is aria"]
    t = transcript.lower()
    return any(p in t for p in echo_phrases)

def extract_name_from_transcript(transcript):
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content":
                f"A caller said: \"{transcript}\"\n"
                f"What is their first name, if they mentioned one?\n"
                f"Reply ONLY with JSON: {{\"name\": \"Josh\"}} or {{\"name\": null}}"
            }],
            response_format={"type": "json_object"},
            max_tokens=20, temperature=0.0,
        )
        return json.loads(resp.choices[0].message.content).get("name")
    except Exception:
        return None


def ai_conversation_turn(history, caller_id, turns, session):
    """
    Full multi-turn conversational AI with dynamic questioning by caller type.
    ARIA gathers: name, purpose, caller type, urgency — then decides action.
    """
    contacts = load_contacts()
    names_list = ", ".join(contacts["approved_names"])

    caller_rec = caller_history["callers"].get(caller_id, {})
    caller_context = ""
    if caller_rec:
        name = caller_rec.get("name") or "unknown"
        count = caller_rec.get("call_count", 1)
        last_purpose = caller_rec.get("last_purpose") or "unknown"
        last_type = caller_rec.get("caller_type") or "unknown"
        last_vm = (caller_rec["voicemails"][-1]["summary"]
                   if caller_rec.get("voicemails") else "none")
        caller_context = (
            f"\nReturning caller: name={name}, calls={count}, "
            f"type={last_type}, last purpose=\"{last_purpose}\", "
            f"last voicemail=\"{last_vm}\""
        )

    known_name    = session.get("caller_name")
    known_purpose = session.get("caller_purpose")
    known_urgency = session.get("caller_urgency")
    known_type    = session.get("caller_type")

    gathered = []
    if known_name:    gathered.append(f"name={known_name}")
    if known_purpose: gathered.append(f"purpose={known_purpose}")
    if known_type:    gathered.append(f"type={known_type}")
    if known_urgency is not None: gathered.append(f"urgency={known_urgency}/10")
    gathered_str = ", ".join(gathered) if gathered else "nothing yet"

    # ✨ Dynamic follow-up question based on caller type
    type_followup = ""
    if known_type and not known_purpose:
        q = get_followup_for_type(known_type)
        type_followup = f"\nSince this is a {known_type}, after getting their name ask: \"{q}\""

    # ✨ Web lookup context
    lookup = session.get("lookup", {})
    lookup_context = ""
    if lookup:
        spam_score = lookup.get("spam_score", 0)
        biz_name   = lookup.get("business_name")
        summary    = lookup.get("summary", "")
        if spam_score >= 6:
            lookup_context = f"\n⚠️ LOOKUP WARNING: Spam score {spam_score}/10. {summary}"
        elif biz_name:
            lookup_context = f"\n📋 LOOKUP: This number belongs to {biz_name}. {summary}"
        elif summary and summary != "No public information found.":
            lookup_context = f"\n📋 LOOKUP: {summary}"

    # ✨ Relationship context
    relationship = get_relationship_summary(caller_id)
    rel_context = f"\nRelationship: {relationship}" if relationship else ""

    # ✨ Scheduling context
    now_str = current_time_str()

    system = f"""You are ARIA, Scott Lumley's personal AI assistant answering his phone.
You are warm, professional, and conversational — like a real executive assistant.
Current time: {now_str}
Approved contacts (always forward): {names_list}
Caller number: {caller_id}{caller_context}{rel_context}{lookup_context}
Turn: {turns + 1} of {MAX_TURNS}
Already gathered: {gathered_str}{type_followup}

YOUR GOAL — gather through natural conversation:
1. Caller's name
2. Caller type (contractor/recruiter/doctor/sales/legal/personal/business/media/government/unknown)
3. Purpose of the call
4. Urgency (1-10 you assign based on context)

DYNAMIC QUESTIONING — once you know their type, ask the right question:
- contractor: license, scope of work, timeline
- recruiter: company, role, salary range, remote/onsite
- doctor: which practice, appointment or test results, personal
- sales: what product, did Scott request the call
- legal: case reference, attorney name, is there a deadline
- personal: how do they know Scott, is everything okay
- business: company name, what deal or topic
- media: publication, story topic
- government: agency, reason, time sensitive

URGENCY GUIDE:
- emergency/hospital/accident/dying → 9-10
- legal deadline/doctor results → 7-8
- business opportunity/personal family → 5-6
- sales/routine → 1-4

SCHEDULING — if caller wants a callback time:
- Ask what day and time works for them (Central Time)
- Confirm the time back to them
- Use action=schedule in your response

DECISIONS (once you have name + purpose):
- FORWARD: approved contact OR emergency urgency >= 9
- RELAY: real human, unknown, urgency 5-8 → text Scott
- SCHEDULE: caller wants a callback at a specific time
- BLOCK: 100% confirmed robocall/spam
- VOICEMAIL: urgency 1-4 OR caller prefers a message

IMPORTANT:
- You are ARIA. Never reveal you are GPT or OpenAI.
- Never give out Scott's personal number.
- Keep responses SHORT — 1-2 sentences max.
- Ask ONE question at a time.
- If you have enough info, make a decision immediately.

Reply ONLY with JSON (no extra text):
{{"action":"speak","message":"Your words","name":null,"purpose":null,"urgency":null,"caller_type":null,"scheduled_time":null}}
{{"action":"forward","message":"One moment, connecting you to Scott now.","name":"Josh","purpose":"family matter","urgency":9,"caller_type":"personal","scheduled_time":null}}
{{"action":"relay","message":"Let me check if Scott is available. One moment.","name":"Josh","purpose":"business deal","urgency":6,"caller_type":"business","scheduled_time":null}}
{{"action":"schedule","message":"Perfect, I have you down for Thursday at 2 PM Central. Scott will call you then.","name":"Josh","purpose":"business proposal","urgency":4,"caller_type":"business","scheduled_time":"Thursday at 2 PM Central"}}
{{"action":"block","message":"Scott is unavailable for this call. Goodbye.","name":null,"purpose":"spam","urgency":0,"caller_type":"sales","scheduled_time":null}}
{{"action":"voicemail","message":"Scott is unavailable. Can I take a detailed message for you?","name":"Josh","purpose":"sales call","urgency":2,"caller_type":"sales","scheduled_time":null}}

All fields except message are optional and can be null."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}] + history,
            response_format={"type": "json_object"},
            max_tokens=150,
            temperature=0.2,
        )
        result = json.loads(resp.choices[0].message.content)
        print(f"[ARIA T{turns+1}] {result.get('action')} | {result.get('message')}")
        return result
    except Exception as e:
        print(f"[ARIA Error] {e}")
        return {
            "action": "speak",
            "message": "I'm sorry, could you say that again? Who am I speaking with?",
            "name": None, "purpose": None, "urgency": None,
            "caller_type": None, "scheduled_time": None,
        }


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return "ARIA Call Screener is running.", 200


@app.route("/sms", methods=["POST"])
def sms_webhook():
    """
    SMS commands Scott can text:
      FORWARD / F       → connect waiting caller
      VM                → send waiting caller to voicemail
      SCHEDULE          → book a callback for waiting caller (ARIA asks for time)
      ADD +1XXXXXXXXXX  → add to whitelist
      REMOVE +1XXXXXXXX → remove from whitelist
      STATUS            → system status
      APPTS             → list scheduled callbacks
      HISTORY +1XXXXXXX → caller history
    """
    data = request.json or {}
    print(f"[SMS Raw] {json.dumps(data)[:500]}")

    payload = data.get("data", {}).get("payload", {})

    # Telnyx can send "from" as a dict {"phone_number": "..."} or a plain string
    from_raw = payload.get("from", {})
    if isinstance(from_raw, dict):
        from_number = from_raw.get("phone_number", "")
    else:
        from_number = str(from_raw)

    text = payload.get("text", "").strip().upper()

    print(f"[SMS In] from={from_number} text='{text}' scott={SCOTT_REAL_NUMBER}")

    if not SCOTT_REAL_NUMBER:
        print("[SMS] WARNING: SCOTT_REAL_NUMBER env var not set — accepting all SMS for debug")
    elif from_number != SCOTT_REAL_NUMBER:
        print(f"[SMS] Ignored — not from Scott ({from_number} != {SCOTT_REAL_NUMBER})")
        return jsonify({"status": "ok"})

    contacts = load_contacts()

    # ── Live relay responses ──
    if text in ("FORWARD", "F", "VM", "VOICEMAIL", "SCHEDULE", "S"):
        if not pending_relay:
            send_sms(SCOTT_REAL_NUMBER, "ℹ️ No calls currently waiting for your decision.")
            return jsonify({"status": "ok"})

        relay_caller_id, relay_ccid = next(iter(pending_relay.items()))
        session = call_sessions.get(relay_ccid, {})
        name    = session.get("caller_name")
        purpose = session.get("caller_purpose")
        urgency = session.get("caller_urgency")
        caller_type = session.get("caller_type")

        if text in ("FORWARD", "F"):
            pending_relay.pop(relay_caller_id, None)
            briefing = build_briefing(relay_caller_id, name, purpose, urgency, caller_type)
            speak(relay_ccid, briefing, client_state="briefing")
            telnyx_action(relay_ccid, "transfer", to=SCOTT_REAL_NUMBER)
            update_caller_record(relay_caller_id, action="forwarded_by_scott",
                                  purpose=purpose, urgency=urgency, caller_type=caller_type,
                                  increment_count=False)
            send_sms(SCOTT_REAL_NUMBER, f"✅ Connecting {name or relay_caller_id} now.")
            call_sessions.pop(relay_ccid, None)

        elif text in ("VM", "VOICEMAIL"):
            pending_relay.pop(relay_caller_id, None)
            start_voicemail(relay_ccid, relay_caller_id, reason="scott chose voicemail via SMS")
            send_sms(SCOTT_REAL_NUMBER, f"📬 Sending {name or relay_caller_id} to voicemail.")

        elif text in ("SCHEDULE", "S"):
            # ✨ Tell ARIA to ask caller for their preferred callback time
            pending_relay.pop(relay_caller_id, None)
            session["scheduling"] = True
            speak(relay_ccid,
                "Scott would like to schedule a callback with you. "
                "What day and time works best for you? I am in the Central time zone.",
            )
            start_listening(relay_ccid)
            send_sms(SCOTT_REAL_NUMBER, f"📅 Asking {name or relay_caller_id} for their preferred callback time.")

    # ── ✨ List appointments ──
    elif text == "APPTS":
        send_sms(SCOTT_REAL_NUMBER, list_appointments())

    elif text.startswith("ADD "):
        number = text[4:].strip()
        if not number.startswith("+"):
            number = "+1" + number.replace("-", "").replace(" ", "")
        added = add_number_to_whitelist(number)
        reply = f"✅ {number} added to whitelist." if added else f"ℹ️ {number} already on whitelist."
        send_sms(SCOTT_REAL_NUMBER, reply)

    elif text.startswith("REMOVE "):
        number = text[7:].strip()
        if not number.startswith("+"):
            number = "+1" + number.replace("-", "").replace(" ", "")
        nums = contacts.get("approved_numbers", [])
        if number in nums:
            nums.remove(number)
            contacts["approved_numbers"] = nums
            save_contacts(contacts)
            send_sms(SCOTT_REAL_NUMBER, f"🗑️ {number} removed from whitelist.")
        else:
            send_sms(SCOTT_REAL_NUMBER, f"ℹ️ {number} was not on the whitelist.")

    elif text == "STATUS":
        nums  = contacts.get("approved_numbers", [])
        names = contacts.get("approved_names", [])
        quiet = "ON" if is_quiet_hours() else "OFF"
        dnd   = "🔕 ON" if is_dnd() else "OFF"
        total_callers  = len(caller_history.get("callers", {}))
        pending_count  = len(pending_relay)
        appt_count     = len(appointments)
        tz    = pytz.timezone(SCOTT_TIMEZONE)
        today = datetime.now(tz).strftime("%Y-%m-%d")
        calls_today = sum(1 for c in daily_call_log if c.get("date") == today)
        reply = (f"📋 ARIA status:\n"
                 f"Time: {current_time_str()}\n"
                 f"DND: {dnd}\n"
                 f"Quiet hours: {quiet} ({QUIET_START}pm–{QUIET_END}am)\n"
                 f"Approved numbers: {len(nums)}\n"
                 f"Approved names: {len(names)}\n"
                 f"VIP numbers: {len(vip_numbers)}\n"
                 f"Callers remembered: {total_callers}\n"
                 f"Calls today: {calls_today}\n"
                 f"Calls waiting decision: {pending_count}\n"
                 f"Scheduled callbacks: {appt_count}")
        send_sms(SCOTT_REAL_NUMBER, reply)

    elif text.startswith("HISTORY "):
        number = text[8:].strip()
        if not number.startswith("+"):
            number = "+1" + number.replace("-", "").replace(" ", "")
        rec = caller_history["callers"].get(number)
        if rec:
            vms = "\n".join(f"- {v['summary']}" for v in rec.get("voicemails", [])[-3:]) or "none"
            reply = (f"📋 {number}\n"
                     f"Name: {rec.get('name') or 'unknown'}\n"
                     f"Type: {rec.get('caller_type') or 'unknown'}\n"
                     f"Calls: {rec.get('call_count', 0)}\n"
                     f"Last purpose: {rec.get('last_purpose') or 'unknown'}\n"
                     f"Last urgency: {rec.get('last_urgency') or 'unknown'}\n"
                     f"Last: {rec.get('last_call', '?')[:10]}\n"
                     f"Voicemails:\n{vms}")
        else:
            reply = f"No history found for {number}"
        send_sms(SCOTT_REAL_NUMBER, reply)

    # ✨ NEW: NOTE command — add a note to a caller's record
    # Usage: NOTE +16155551234 They keep calling about the truck deal
    elif text.startswith("NOTE "):
        parts = text[5:].strip().split(" ", 1)
        if len(parts) == 2:
            number, note = parts
            if not number.startswith("+"):
                number = "+1" + number.replace("-", "").replace(" ", "")
            add_caller_note(number, note.lower())
            send_sms(SCOTT_REAL_NUMBER, f"📝 Note added for {number}:\n{note.lower()}")
        else:
            send_sms(SCOTT_REAL_NUMBER, "Usage: NOTE +1XXXXXXXXXX your note here")

    # ✨ NEW: STATS command — overall call stats
    elif text == "STATS":
        stats = get_caller_stats()
        freq  = ", ".join(stats["frequent_callers"]) or "none yet"
        reply = (
            f"📊 ARIA Stats:\n"
            f"Total callers known: {stats['total']}\n"
            f"Spam blocked: {stats['spam_blocked']}\n"
            f"Forwarded to Scott: {stats['forwarded']}\n"
            f"Left voicemails: {stats['voicemails_left']}\n"
            f"Frequent callers: {freq}"
        )
        send_sms(SCOTT_REAL_NUMBER, reply)

    # ✨ Feature 10: DND ON / DND OFF
    elif text in ("DND ON", "DND"):
        set_dnd(True)
        send_sms(SCOTT_REAL_NUMBER,
            "🔕 Do Not Disturb is ON.\n"
            "All calls will go straight to voicemail.\n"
            "Text DND OFF to turn it off.\n"
            "VIP callers still get through.")

    elif text == "DND OFF":
        set_dnd(False)
        send_sms(SCOTT_REAL_NUMBER, "🔔 Do Not Disturb is OFF. ARIA is screening calls normally.")

    # ✨ Feature 12: VIP list management
    elif text.startswith("VIP ADD "):
        number = text[8:].strip()
        if not number.startswith("+"):
            number = "+1" + number.replace("-", "").replace(" ", "")
        added = add_vip(number)
        reply = f"⭐ {number} added to VIP list. They will always get through, even during DND or quiet hours." if added else f"ℹ️ {number} is already a VIP."
        send_sms(SCOTT_REAL_NUMBER, reply)

    elif text.startswith("VIP REMOVE "):
        number = text[11:].strip()
        if not number.startswith("+"):
            number = "+1" + number.replace("-", "").replace(" ", "")
        removed = remove_vip(number)
        reply = f"⭐ {number} removed from VIP list." if removed else f"ℹ️ {number} was not on the VIP list."
        send_sms(SCOTT_REAL_NUMBER, reply)

    elif text == "VIP":
        if vip_numbers:
            reply = "⭐ VIP List:\n" + "\n".join(vip_numbers)
        else:
            reply = "⭐ No VIP numbers set.\nUse: VIP ADD +1XXXXXXXXXX"
        send_sms(SCOTT_REAL_NUMBER, reply)

    # ✨ Feature 11: Daily summary on demand
    elif text == "SUMMARY":
        send_daily_summary()

    # ✨ NEW: LOOKUP command — manually look up a number
    # Usage: LOOKUP +16155551234
    elif text.startswith("LOOKUP "):
        number = text[7:].strip()
        if not number.startswith("+"):
            number = "+1" + number.replace("-", "").replace(" ", "")
        send_sms(SCOTT_REAL_NUMBER, f"🔍 Looking up {number}...")
        result = lookup_number(number)
        biz    = f"\nBusiness: {result['business_name']}" if result.get("business_name") else ""
        reply  = (
            f"🔍 Lookup: {number}\n"
            f"Spam score: {result.get('spam_score', 0)}/10{biz}\n"
            f"{result.get('summary', 'No info found.')}"
        )
        send_sms(SCOTT_REAL_NUMBER, reply)

    else:
        send_sms(SCOTT_REAL_NUMBER,
            "ARIA Commands:\n"
            "FORWARD or F → connect caller\n"
            "VM → voicemail\n"
            "SCHEDULE or S → book callback\n"
            "APPTS → list callbacks\n"
            "ADD +1XXXXXXXXXX → whitelist\n"
            "REMOVE +1XXXXXXXXXX → unwhitelist\n"
            "VIP ADD +1XXXXXXXXXX → VIP tier\n"
            "VIP REMOVE +1XXXXXXXXXX\n"
            "VIP → list VIPs\n"
            "DND ON / DND OFF → do not disturb\n"
            "STATUS → system status\n"
            "STATS → call statistics\n"
            "SUMMARY → today's call log\n"
            "HISTORY +1XXXXXXXXXX\n"
            "LOOKUP +1XXXXXXXXXX\n"
            "NOTE +1XXXXXXXXXX your note")

    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    event_type = data.get("data", {}).get("event_type", "")
    payload    = data.get("data", {}).get("payload", {})
    ccid       = payload.get("call_control_id", "")
    caller_id  = payload.get("from", "Unknown")

    print(f"[{event_type}] from={caller_id}")

    session_exists = ccid in call_sessions
    direction  = payload.get("direction", "")
    is_outbound = (direction == "outgoing") or (not session_exists and caller_id == TELNYX_NUMBER)

    # ── 1. Incoming call ──
    if event_type == "call.initiated" and not is_outbound:
        call_sessions[ccid] = {
            "caller_id": caller_id, "history": [], "turns": 0,
            "deciding": False, "voicemail": False, "urgent_check": False,
            "relay_sent": False, "scheduling": False,
            "caller_name": None, "caller_purpose": None,
            "caller_urgency": None, "caller_type": None,
            "vm_turns": 0, "vm_history": [], "vm_transcript_parts": [],
        }
        telnyx_action(ccid, "answer")

    # ── 2. Call answered ──
    elif event_type == "call.answered" and not is_outbound:
        session = call_sessions.get(ccid)
        if not session:
            session = {
                "caller_id": caller_id, "history": [], "turns": 0,
                "deciding": False, "voicemail": False, "urgent_check": False,
                "relay_sent": False, "scheduling": False,
                "caller_name": None, "caller_purpose": None,
                "caller_urgency": None, "caller_type": None,
                "vm_turns": 0, "vm_history": [], "vm_transcript_parts": [],
            }
            call_sessions[ccid] = session

        update_caller_record(caller_id, increment_count=True)

        # ✨ Run web lookup in background context (non-blocking feel via early return)
        lookup = None
        if not number_in_whitelist(caller_id):
            lookup = lookup_number(caller_id)
            session["lookup"] = lookup
            if lookup.get("lookup_summary"):
                update_caller_record(caller_id, lookup_summary=lookup.get("summary"),
                                     increment_count=False)

        if number_in_whitelist(caller_id) or is_vip(caller_id):
            caller_rec = get_caller_record(caller_id)
            known_name = caller_rec.get("name") if caller_rec else None
            vip_note   = " You're on Scott's VIP list." if is_vip(caller_id) and not number_in_whitelist(caller_id) else ""
            msg = f"Welcome back, {known_name}!{vip_note} One moment." if known_name else "One moment, connecting you to Scott."
            speak(ccid, msg, client_state="screened")
            telnyx_action(ccid, "transfer", to=SCOTT_REAL_NUMBER)
            log_daily_call(caller_id, known_name, "forwarded_whitelist", "whitelisted/VIP contact")

        elif lookup and lookup.get("spam_score", 0) >= 9:
            # ✨ Confirmed high-confidence spam — block immediately, notify Scott
            print(f"[Lookup] Auto-blocking confirmed spam: {caller_id}")
            speak(ccid, "We're sorry, this number has been identified as spam. Goodbye.",
                  client_state="screened")
            telnyx_action(ccid, "hangup")
            update_caller_record(caller_id, action="auto_blocked_spam", increment_count=False)
            send_sms(SCOTT_REAL_NUMBER,
                f"🚫 ARIA auto-blocked spam call from {caller_id}.\n"
                f"Reason: {lookup.get('summary', 'Confirmed spam')}")

        elif is_quiet_hours():
            tz   = pytz.timezone(SCOTT_TIMEZONE)
            hour = datetime.now(tz).hour
            print(f"[Quiet hours] {hour}h — voicemail")
            start_voicemail(ccid, caller_id, reason="quiet hours")

        elif is_dnd():
            print(f"[DND] Active — sending {caller_id} to voicemail")
            start_voicemail(ccid, caller_id, reason="do not disturb")

        else:
            caller_rec  = get_caller_record(caller_id)
            known_name  = caller_rec.get("name") if caller_rec else None
            last_purpose = caller_rec.get("last_purpose") if caller_rec else None
            known_type   = caller_rec.get("caller_type") if caller_rec else None

            # ✨ If lookup identified a business, pre-set caller type
            if lookup and lookup.get("is_business") and lookup.get("business_name"):
                if not known_type:
                    session["caller_type"] = "business"
                    session["lookup_business"] = lookup["business_name"]
                    print(f"[Lookup] Pre-identified as business: {lookup['business_name']}")

            if known_name:
                session["caller_name"] = known_name
                if known_type:
                    session["caller_type"] = known_type
                if last_purpose:
                    greeting = (f"Welcome back, {known_name}! "
                                f"Last time you called about {last_purpose}. "
                                f"How can I help you today?")
                else:
                    greeting = f"Welcome back, {known_name}! How can I help you today?"
            else:
                greeting = (
                    "Thank you for calling. You've reached Scott Lumley's office. "
                    "I'm ARIA, his personal assistant. May I ask who's calling please?"
                )
            speak(ccid, greeting)
            start_listening(ccid)

    # ── 3. speak.ended ──
    elif event_type == "call.speak.ended":
        client_state = decode_state(payload.get("client_state", ""))

        if client_state == "voicemail_prompt":
            telnyx_action(ccid, "record_start",
                format="mp3", channels="single", play_beep=True,
                client_state=encode_state("recording_voicemail"),
            )
            start_listening(ccid)

        elif client_state in ("screened", "briefing"):
            pass

        elif client_state == "relay_hold":
            # After hold message, listen in case caller says "voicemail" or hangs up
            session = call_sessions.get(ccid)
            if session and session.get("relay_sent"):
                start_listening(ccid)

        else:
            session = call_sessions.get(ccid)
            if session and not session.get("deciding") and not session.get("voicemail"):
                telnyx_action(ccid, "transcription_start",
                    transcription_engine="Deepgram",
                    transcription_model="flux",
                    language="en",
                )

    # ── 4. Voicemail recording saved ──
    elif event_type == "call.record.saved":
        session = call_sessions.get(ccid, {})
        caller_id_stored = session.get("voicemail_caller", caller_id)
        print(f"[Voicemail] Recording saved for {caller_id_stored}")
        # Finalize if not already done via transcription
        if call_sessions.get(ccid):
            speak(ccid, "Thank you. Your message has been saved. Goodbye.", client_state="screened")
            telnyx_action(ccid, "hangup")

    # ── 5. Transcription ──
    elif event_type == "call.transcription":
        transcription_data = payload.get("transcription_data", {})
        transcript = transcription_data.get("transcript", "").strip()
        is_final   = transcription_data.get("is_final", False)

        print(f"[Transcript] final={is_final} | '{transcript}'")

        if not is_final or not transcript:
            return jsonify({"status": "ok"})

        session = call_sessions.get(ccid)
        if not session:
            session = {
                "caller_id": caller_id, "history": [], "turns": 0,
                "deciding": False, "voicemail": False, "urgent_check": False,
                "relay_sent": False, "scheduling": False,
                "caller_name": None, "caller_purpose": None,
                "caller_urgency": None, "caller_type": None,
                "vm_turns": 0, "vm_history": [], "vm_transcript_parts": [],
            }
            call_sessions[ccid] = session

        # ── ✨ Scheduling mode: caller is giving their preferred callback time ──
        if session.get("scheduling"):
            telnyx_action(ccid, "transcription_stop")
            session["scheduling"] = False
            name    = session.get("caller_name") or "the caller"
            purpose = session.get("caller_purpose") or "callback"
            book_appointment(name, caller_id, transcript, purpose)
            speak(ccid,
                f"Perfect! I have you scheduled. Scott will call you back at {transcript}. "
                f"Thank you for calling and have a wonderful day. Goodbye.",
                client_state="screened"
            )
            telnyx_action(ccid, "hangup")
            call_sessions.pop(ccid, None)
            return jsonify({"status": "ok"})

        # ── ✨ Voicemail mode with intelligent follow-up ──
        if session.get("voicemail"):
            print(f"[VM transcript] turn={session.get('vm_turns')} '{transcript}'")
            telnyx_action(ccid, "transcription_stop")

            # Store transcript part
            session.setdefault("vm_transcript_parts", []).append(transcript)
            session.setdefault("vm_history", [])

            # Detect caller type from first VM message if not yet known
            if not session.get("caller_type") and session.get("vm_turns") == 0:
                detected_type = detect_caller_type(transcript, session.get("caller_purpose") or "")
                if detected_type and detected_type != "unknown":
                    session["caller_type"] = detected_type
                    print(f"[Caller Type] Detected: {detected_type}")

            # ✨ Ask a smart follow-up question or wrap up
            followup = ai_voicemail_followup(transcript, session)
            session["vm_history"].append({"caller": transcript, "aria": followup.get("question")})
            session["vm_turns"] = session.get("vm_turns", 0) + 1

            if followup.get("done") or not followup.get("question"):
                # Enough info — finalize voicemail
                finalize_voicemail(ccid, session.get("voicemail_caller", caller_id), session)
            else:
                # Ask the follow-up question
                speak(ccid, followup["question"])
                start_listening(ccid)

            return jsonify({"status": "ok"})

        # ── Caller is on relay hold — only listen for "voicemail" keyword ──
        if session.get("relay_sent") and not session.get("voicemail"):
            t_lower = transcript.lower()
            if any(w in t_lower for w in ["voicemail", "message", "leave a message", "that's fine", "no problem", "okay"]):
                print(f"[Relay Hold] Caller opted for voicemail: '{transcript}'")
                pending_relay.pop(caller_id, None)
                session["relay_sent"] = False
                start_voicemail(ccid, caller_id, reason="caller chose voicemail while on hold")
            else:
                # Caller said something else — reassure them
                speak(ccid,
                    "Still checking with Scott. Thank you for your patience. "
                    "Say voicemail anytime if you'd like to leave a message.",
                    client_state="relay_hold"
                )
            return jsonify({"status": "ok"})

        if session.get("deciding"):
            print("[Skipping] already processing")
            return jsonify({"status": "ok"})

        session["deciding"] = True
        telnyx_action(ccid, "transcription_stop")

        # Skip greeting echo
        if looks_like_greeting_echo(transcript):
            print(f"[Echo] ignoring: '{transcript}'")
            session["deciding"] = False
            start_listening(ccid)
            return jsonify({"status": "ok"})

        # Fast-path: whitelisted name
        if name_in_whitelist(transcript):
            print(f"[Whitelist name] '{transcript}'")
            extracted = extract_name_from_transcript(transcript)
            briefing  = build_briefing(caller_id, extracted, "whitelisted contact", 10)
            speak(ccid, "One moment, connecting you to Scott now.", client_state="briefing")
            telnyx_action(ccid, "transfer", to=SCOTT_REAL_NUMBER)
            call_sessions.pop(ccid, None)
            return jsonify({"status": "ok"})

        session["history"].append({"role": "user", "content": transcript})

        result = ai_conversation_turn(session["history"], caller_id, session["turns"], session)
        session["turns"] += 1
        session["deciding"] = False

        # Update session with newly gathered info
        if result.get("name"):        session["caller_name"]    = result["name"]
        if result.get("purpose"):     session["caller_purpose"] = result["purpose"]
        if result.get("urgency") is not None: session["caller_urgency"] = result["urgency"]
        if result.get("caller_type"): session["caller_type"]    = result["caller_type"]

        # Backup name extraction
        if not session.get("caller_name"):
            extracted = extract_name_from_transcript(transcript)
            if extracted:
                session["caller_name"] = extracted

        # ✨ Detect caller type from transcript if not yet identified
        if not session.get("caller_type") and session.get("caller_purpose"):
            detected = detect_caller_type(transcript, session["caller_purpose"])
            if detected and detected != "unknown":
                session["caller_type"] = detected

        update_caller_record(
            caller_id,
            name=session.get("caller_name"),
            action=result.get("action"),
            purpose=session.get("caller_purpose"),
            urgency=session.get("caller_urgency"),
            caller_type=session.get("caller_type"),
            increment_count=False,
        )

        # ✨ Feature 11: Log for daily summary
        log_daily_call(caller_id, session.get("caller_name"),
                       result.get("action"), session.get("caller_purpose"))

        action  = result.get("action", "speak")
        message = result.get("message", "Could you please repeat that?")
        session["history"].append({"role": "assistant", "content": message})

        if action == "speak":
            speak(ccid, message)
            start_listening(ccid)

        elif action == "forward":
            briefing = build_briefing(
                caller_id,
                session.get("caller_name"),
                session.get("caller_purpose"),
                session.get("caller_urgency"),
                session.get("caller_type"),
            )
            speak(ccid, message, client_state="briefing")
            telnyx_action(ccid, "transfer", to=SCOTT_REAL_NUMBER)
            call_sessions.pop(ccid, None)

        elif action == "relay":
            if session.get("relay_sent"):
                # Already waiting — don't re-send, just stay quiet
                print(f"[Relay] Already waiting for Scott's decision, ignoring transcript")
                return jsonify({"status": "ok"})
            send_relay_sms(
                ccid, caller_id,
                session.get("caller_name"),
                session.get("caller_purpose"),
                session.get("caller_urgency") or 5,
                session.get("caller_type"),
            )
            session["relay_sent"] = True
            session["relay_time"] = datetime.utcnow().isoformat()
            # Tell caller we're checking, then offer voicemail option while they wait
            speak(ccid,
                "I'm checking with Scott right now. Please hold for just a moment. "
                "If you'd prefer, say voicemail and I can take a message instead.",
                client_state="relay_hold"
            )

        elif action == "schedule":
            # ✨ ARIA confirmed a time — book it
            scheduled_time = result.get("scheduled_time") or transcript
            book_appointment(
                session.get("caller_name") or "Unknown",
                caller_id,
                scheduled_time,
                session.get("caller_purpose") or "callback",
            )
            speak(ccid, message, client_state="screened")
            telnyx_action(ccid, "hangup")
            call_sessions.pop(ccid, None)

        elif action == "voicemail":
            session["voicemail"] = True
            session["voicemail_caller"] = caller_id
            speak(ccid, message, client_state="voicemail_prompt")

        else:
            # block
            speak(ccid, message, client_state="screened")
            telnyx_action(ccid, "hangup")
            call_sessions.pop(ccid, None)

    # ── 6. Hangup ──
    elif event_type == "call.hangup":
        session = call_sessions.get(ccid, {})
        cid = session.get("caller_id", caller_id)
        pending_relay.pop(cid, None)
        call_sessions.pop(ccid, None)
        print("[Hangup] session cleaned up")

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=False, port=5000)
