"""
BMS Seat Watcher — CI/Headless mode for GitHub Actions.
Runs once, checks specific seats (e.g. row J seat 12) in a theatre's shows,
emails when a watched seat becomes available.
State is persisted via seat_state.json.

Configure via environment variables (see README).
"""

import os
import re
import sys
import json
import base64
from html import escape
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from main import (
    REGION_MAP, TIME_PERIODS,
    RESEND_API_KEY, RESEND_TO_EMAIL, RESEND_FROM_EMAIL,
)

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — set via env vars
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    # Seat-layout URL (…/seat-layout/ET…/VENUE/SESSION/DATE) or
    # cinema URL (…/cinemas/HYD/…/buytickets/VENUE/DATE)
    "url": os.getenv("BMS_SEAT_URL", ""),
    "event": os.getenv("BMS_SEAT_EVENT", ""),      # ET code, required with a cinema URL
    "dates": os.getenv("BMS_SEAT_DATES", ""),      # comma-separated YYYYMMDD, empty = from URL
    "screen": os.getenv("BMS_SEAT_SCREEN", ""),    # substring of screen name, e.g. "PCX"
    "time_period": os.getenv("BMS_SEAT_TIME", ""), # e.g. "morning,evening"
    "seats": os.getenv("BMS_SEATS", ""),           # e.g. "J12,J13"
}

STATE_FILE = "seat_state.json"

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

SHOWS_API_URL = "https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary"
DOTRANS_URL = "https://services-in.bookmyshow.com/doTrans.aspx"

# AES key the BMS web app uses to decrypt the GETSEATLAYOUT response
SEAT_LAYOUT_KEY = b"kYp3s6v9y$B&E)H+MbQeThWmZq4t7w!z"

SEAT_STATUS_MAP = {
    0: "GANGWAY",
    1: "AVAILABLE",
    2: "BOOKED",
    3: "OTHER CATEGORY",
    4: "AVAILABLE",       # "best seat", still bookable
    7: "WHEELCHAIR",
    8: "COMPANION",
    9: "BLOCKED",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


# ──────────────────────────────────────────────────────────────────────
# CONFIG PARSING
# ──────────────────────────────────────────────────────────────────────
def parse_seat_url(url):
    parts = urlparse(url).path.strip("/").split("/")
    result = {"region": "", "event": "", "venue": "",
              "session": "", "date": ""}
    if "seat-layout" in parts:
        idx = parts.index("seat-layout")
        result["region"] = parts[idx - 1] if idx > 0 else ""
        tail = parts[idx + 1:idx + 5] + [""] * 4
        (result["event"], result["venue"],
         result["session"], result["date"]) = tail[:4]
    elif parts and parts[0] == "cinemas":
        result["region"] = parts[1] if len(parts) > 1 else ""
        if "buytickets" in parts:
            idx = parts.index("buytickets")
            tail = parts[idx + 1:idx + 3] + [""] * 2
            result["venue"], result["date"] = tail[:2]
    return result


def resolve_region(region):
    """Accepts a slug ("hyderabad") or a region code ("HYD")."""
    key = (region or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key][0], REGION_MAP[key][1]
    for code, slug, *_ in REGION_MAP.values():
        if code.lower() == key:
            return code, slug
    return key.upper(), key


def parse_seat_list(raw):
    seats = []
    for item in raw.split(","):
        m = re.match(r"^\s*([A-Za-z]+)\s*-?\s*(\d+)\s*$", item)
        if m:
            seats.append((m.group(1).upper(), str(int(m.group(2)))))
        elif item.strip():
            print(f"  ⚠️  Ignoring invalid seat '{item.strip()}' "
                  f"(expected e.g. J12)")
    return seats


# ──────────────────────────────────────────────────────────────────────
# BMS API
# ──────────────────────────────────────────────────────────────────────
def fetch_venue_shows(event_code, venue_code, date_code,
                      region_code, region_slug):
    """All showtimes of one movie at one venue on one date."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code,
        "regionCode": region_code,
        "venueCode": venue_code,
    }
    try:
        resp = requests.get(SHOWS_API_URL, headers=headers,
                            params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("data", {})
        print(f"  HTTP {resp.status_code} fetching shows for {date_code}")
    except (requests.RequestException, ValueError) as e:
        print(f"  Show fetch failed: {e}")
    return None


def fetch_seat_layout(venue_code, session_id):
    """Raw decrypted seat layout string for a session, or None."""
    fields = {
        "strAppCode": "WEB",
        "lngTransactionIdentifier": "0",
        "strCommand": "GETSEATLAYOUT",
        "strVenueCode": venue_code,
        "strParam1": session_id,
        "strParam2": "WEB",
        "strParam3": "", "strParam4": "",
        "strParam5": "Y",
        "strParam6": "", "strParam7": "", "strParam8": "",
        "strParam9": "", "strParam10": "",
        "strFormat": "json",
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://in.bookmyshow.com",
        "Referer": "https://in.bookmyshow.com/",
    }
    try:
        resp = requests.post(
            DOTRANS_URL, headers=headers, timeout=30,
            files={k: (None, v) for k, v in fields.items()},
        )
        data = resp.json().get("BookMyShow", {})
    except (requests.RequestException, ValueError) as e:
        print(f"  Seat layout fetch failed: {e}")
        return None

    if data.get("blnSuccess") != "true":
        print(f"  Seat layout error: {data.get('strException') or resp.status_code}")
        return None

    raw = data.get("strData", "")
    if not isinstance(raw, str) or not raw:
        return None
    if "||" in raw:
        return raw
    return decrypt_layout(raw)


def decrypt_layout(encrypted):
    try:
        decryptor = Cipher(algorithms.AES(SEAT_LAYOUT_KEY),
                           modes.CBC(b"\0" * 16)).decryptor()
        padded = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except ValueError as e:
        print(f"  Could not decrypt seat layout: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────
def parse_layout(raw):
    """
    Layout format: "<areas>||<rows>"
      areas: "GOLD:A:0000000003:1:N:0|…"  (description:areaId:…)
      rows:  "1:N:A000:A2011+47:…|…"      (rowId:rowLabel:seat:seat:…)
      seat:  "<areaId><status><internalNo>+<printedSeatNo>"
    Returns {(row_label, seat_no): (status_code, area_name)}.
    """
    if not raw or "||" not in raw:
        return {}
    areas_part, rows_part = raw.split("||", 1)

    areas = {}
    for area in areas_part.split("|"):
        f = area.split(":")
        if len(f) >= 2:
            areas[f[1]] = f[0]

    seats = {}
    for row in rows_part.split("|"):
        f = row.split(":")
        if len(f) < 3 or not f[1]:
            continue
        label = f[1].upper()
        for token in f[2:]:
            code, _, rest = token.partition("+")
            seat_no = rest.split("+")[0].lstrip("0")
            if len(code) < 2 or not seat_no or not code[1].isdigit():
                continue
            seats[(label, seat_no)] = (int(code[1]), areas.get(code[0], ""))
    return seats


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def select_shows(shows, screen_filter, time_periods, session_id):
    kws = [k.strip().lower() for k in screen_filter.split(",")
           if k.strip()] if screen_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    # A session in the URL means "just this show" unless filters widen it
    only_session = session_id if not kws and not periods else ""

    result = []
    for s in shows:
        if only_session and s.get("sessionId") != only_session:
            continue

        if kws:
            screen = (s.get("attributes", "") or s.get("screenAttr", "")).lower()
            if not any(k in screen for k in kws):
                continue

        if periods:
            try:
                tc = int(s.get("showTimeCode", "0"))
            except ValueError:
                tc = 0
            if not any(p in TIME_PERIODS
                       and TIME_PERIODS[p][0] <= tc < TIME_PERIODS[p][1]
                       for p in periods):
                continue

        result.append(s)
    return result


def is_past_cutoff(show):
    cutoff = show.get("cutOffDateTime", "")
    now = datetime.now(IST).strftime("%Y%m%d%H%M")
    return bool(cutoff) and now >= cutoff


# ──────────────────────────────────────────────────────────────────────
# STATE (for change detection between runs)
# ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def state_key(r):
    return f"{r['date']}|{r['session']}|{r['row']}{r['seat']}"


# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION (Resend)
# ──────────────────────────────────────────────────────────────────────
def send_email(subject, opened, results, info):
    api_key = RESEND_API_KEY.strip()
    to = RESEND_TO_EMAIL.strip()
    frm = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"

    if not api_key or not to:
        print("  ⚠️  Skipping email — RESEND_API_KEY or RESEND_TO_EMAIL not set.")
        return

    now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")

    def show_label(r):
        screen = f" [{r['screen']}]" if r["screen"] else ""
        return f"{r['date']} {r['time']}{screen}"

    opened_html = "".join(
        f'<li style="padding:3px 0;font-size:14px;">'
        f'🟢 Row {escape(r["row"])} Seat {escape(r["seat"])} — '
        f'{escape(show_label(r))} '
        f'(<a href="{escape(r["url"])}">book</a>)</li>'
        for r in opened
    )

    status_rows = "".join(
        f'<tr>'
        f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;font-size:13px;">'
        f'{escape(show_label(r))}</td>'
        f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;font-size:13px;">'
        f'{escape(r["row"])}{escape(r["seat"])}</td>'
        f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;font-size:13px;">'
        f'{escape(r["status"])}</td>'
        f'</tr>'
        for r in results
    )

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;
             font-size:14px;color:#333;background:#fff;">
    <h2 style="margin:0 0 4px 0;font-size:18px;color:#111;">
        Seat Alert: {escape(info['movie'])}
    </h2>
    <p style="margin:0 0 20px 0;font-size:13px;color:#666;">
        {escape(info['venue'])} · {escape(now_str)}
    </p>
    <hr style="border:none;border-top:1px solid #ddd;margin:0 0 20px 0;">
    <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
        Seats Now Available
    </h3>
    <ul style="margin:0 0 20px 0;padding-left:20px;line-height:1.6;color:#333;">
        {opened_html}
    </ul>
    <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
        All Watched Seats
    </h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <tr style="background:#f5f5f5;">
            <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;">Show</th>
            <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;">Seat</th>
            <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;">Status</th>
        </tr>
        {status_rows}
    </table>
    <p style="margin:24px 0 0 0;font-size:12px;color:#999;">
        This is an automated alert from BMS Ticket Notifier.
    </p>
</body>
</html>"""

    plain_lines = [subject, "", f"{info['venue']}", f"Checked at: {now_str}", "",
                   "Seats Now Available:"]
    plain_lines.extend(
        f"  - Row {r['row']} Seat {r['seat']} — {show_label(r)} — {r['url']}"
        for r in opened
    )
    plain_lines.extend(["", "All Watched Seats:"])
    plain_lines.extend(
        f"  {show_label(r)}  {r['row']}{r['seat']}  {r['status']}"
        for r in results
    )
    plain_lines.extend(["", "This is an automated alert from BMS Ticket Notifier."])

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": frm, "to": [to],
                "subject": subject,
                "text": "\n".join(plain_lines), "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  ✅ Email sent to {to}")
        else:
            print(f"  ❌ Resend {resp.status_code}: {resp.text}")
            sys.exit(1)
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Seat Watcher — CI mode")

    parsed = parse_seat_url(CONFIG["url"])
    event_code = CONFIG["event"].strip() or parsed["event"]
    venue_code = parsed["venue"]

    if not venue_code:
        print("  ❌ Invalid BMS_SEAT_URL. Use a seat-layout or cinema URL.")
        sys.exit(1)
    if not event_code:
        print("  ❌ No movie code. Set BMS_SEAT_EVENT (e.g. ET00493076) "
              "or use a seat-layout URL.")
        sys.exit(1)

    wanted = parse_seat_list(CONFIG["seats"])
    if not wanted:
        print("  ❌ BMS_SEATS is empty. Set it to e.g. J12,J13")
        sys.exit(1)

    region_code, region_slug = resolve_region(parsed["region"])

    raw_dates = CONFIG["dates"].strip()
    if raw_dates:
        date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
    elif parsed["date"]:
        date_list = [parsed["date"]]
    else:
        date_list = [datetime.now(IST).strftime("%Y%m%d")]

    print(f"  Event: {event_code}  Venue: {venue_code}  Region: {region_code}  "
          f"Dates: {date_list}")
    print(f"  Seats: {', '.join(r + s for r, s in wanted)}"
          f"{'  Screen: ' + CONFIG['screen'] if CONFIG['screen'] else ''}")

    info = {"movie": event_code, "venue": venue_code}
    results = []
    failed_sessions = set()

    for dc in date_list:
        data = fetch_venue_shows(event_code, venue_code, dc,
                                 region_code, region_slug)
        if not data:
            print(f"  ⚠️  No shows for {dc}")
            continue

        info["movie"] = data.get("eventData", {}).get("eventName") or info["movie"]
        info["venue"] = data.get("venue", {}).get("venueName") or info["venue"]

        shows = select_shows(data.get("showTimes", []), CONFIG["screen"],
                             CONFIG["time_period"], parsed["session"])
        if not shows:
            print(f"  ⚠️  No matching shows on {dc}")

        for show in shows:
            session_id = show.get("sessionId", "")
            label = f"{dc} {show.get('showTime', '')} {show.get('attributes', '')}".strip()

            if is_past_cutoff(show):
                print(f"  ⏭️  {label}: booking closed")
                continue

            layout = parse_layout(fetch_seat_layout(venue_code, session_id))
            if not layout:
                print(f"  ⚠️  {label}: could not read seat layout")
                failed_sessions.add(session_id)
                continue

            for row, seat in wanted:
                code, area = layout.get((row, seat), (None, ""))
                status = (SEAT_STATUS_MAP.get(code, f"UNKNOWN({code})")
                          if code is not None else "NOT IN LAYOUT")
                results.append({
                    "date": dc,
                    "session": session_id,
                    "time": show.get("showTime", ""),
                    "screen": show.get("attributes", ""),
                    "row": row, "seat": seat,
                    "area": area,
                    "status": status,
                    "url": (f"https://in.bookmyshow.com/movies/{region_slug}/"
                            f"seat-layout/{event_code}/{venue_code}/"
                            f"{session_id}/{dc}"),
                })

    old_state = load_state()
    new_state = {k: v for k, v in old_state.items()
                 if k.split("|")[1] in failed_sessions}

    opened = []
    for r in results:
        key = state_key(r)
        new_state[key] = r["status"]
        if r["status"] == "AVAILABLE" and old_state.get(key) != "AVAILABLE":
            opened.append(r)

    save_state(new_state)

    print(f"\n  🎬 {info['movie']} @ {info['venue']}")
    if opened:
        print(f"\n  ⚡ {len(opened)} watched seat(s) now available:")
        for r in opened:
            print(f"     🟢 {r['row']}{r['seat']} — {r['date']} {r['time']} {r['screen']}")
        send_email(
            f"Seat Alert: {len(opened)} seat(s) open — {info['movie']}",
            opened, results, info,
        )
    else:
        print("  ✅ No newly available watched seats.")

    print(f"\n  Current status ({len(results)} seat checks):")
    for r in results:
        area = f" {r['area']}" if r["area"] else ""
        print(f"    {r['date']} {r['time']} [{r['screen']}] — "
              f"{r['row']}{r['seat']}{area}: {r['status']}")

    print("\n  Done.")


if __name__ == "__main__":
    main()
