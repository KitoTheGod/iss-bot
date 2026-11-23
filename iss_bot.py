#!/usr/bin/env python3
"""
ISS radio-range bot for Bluesky.

Checks whether the International Space Station is about to come into radio
range of a ground station, and if so posts a heads-up with the listening
window and a link to a WebSDR tuned to 145.800 MHz.

It does NOT post every run. It posts once per pass, shortly before the pass
begins. Most runs do nothing at all, which is the point.

Environment:
    BSKY_HANDLE           your handle, e.g. isstracker.bsky.social
    BSKY_APP_PASSWORD     an APP PASSWORD (Settings > App Passwords).
                          Never your real account password.

Usage:
    python iss_bot.py            # check, and post if there is a pass
    python iss_bot.py --dry-run  # check and print, never post
    python iss_bot.py --force    # post about the next pass regardless of
                                 # timing or state (for testing)
"""

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse

import requests

# ----------------------------------------------------------------------------
# CONFIGURATION - edit this bit
# ----------------------------------------------------------------------------

# The receivers we watch. The ISS footprint is about 4,500 km across, so
# several of these hear the same pass; the bot works that out and sends ONE
# post naming all of them rather than one post each.
#
# To add your own: name, latitude, longitude, and a link that opens the
# receiver. Anything with a VHF antenna and a web interface will do.
STATIONS = [
    # One receiver per region. Deliberately NOT a long list: stations close
    # together hear the same pass, which adds nothing but noise. These three
    # are far enough apart that each one represents a genuinely different
    # event, and each has a real VHF antenna.
    {"name": "Goonhilly",  "short": "Goonhilly, Cornwall",
     "lat": 50.0524, "lon": -5.1823,
     "sdr": "https://vhf-goonhilly.batc.org.uk/?tune=145800fm"},
    {"name": "Utah",       "short": "Salt Lake City, USA",
     "lat": 40.7000, "lon": -111.8000,
     "sdr": "http://slc2meters.sdrutah.org:8901/?tune=145800fm"},
    {"name": "R4UAB",      "short": "R4UAB, Russia",
     "lat": 54.3282, "lon": 48.3866,
     "sdr": "http://websdr.r4uab.ru/"},
    # Swaps, if you want them: Farnham 51.2149/-0.7987, Dwingeloo
    # 52.8122/6.3964, Washington DC 38.9200/-77.0700 (http://websdr.us:8902/).
    # Washington is the one to add if you want US east-coast coverage too.
]

# Minimum elevation above the horizon to bother posting about, in degrees.
# Below about 5 degrees the signal is scraping through a lot of atmosphere
# and buildings. Raise this to 15 or 20 if you only want the good passes.
MIN_ELEVATION = 10.0

# Only announce a pass whose peak reaches at least this high. Filters out
# the brief, low, barely-audible passes.
MIN_PEAK_ELEVATION = 20.0

# Post this many minutes before the pass starts. GitHub Actions cron is
# often late, so leave some slack.
LEAD_MINUTES = 12

# How far ahead to look for the next pass, in minutes.
HORIZON_MINUTES = 75

# Never post twice inside this many minutes, however many passes there are.
# Stops a busy afternoon turning into a flood.
MIN_GAP_MINUTES = 45

# Hard ceiling on posts per calendar day (UTC). With receivers spread across
# several continents the ISS is in range of SOMEBODY most of the time, so this
# is what stops the account becoming unfollowable. Lower it to 4 or 5 if the
# timeline still feels busy.
MAX_POSTS_PER_DAY = 6

# Your website, appended to posts. Set to None to leave it off.
WEBSITE = "https://isstracker.neocities.org/"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

API = "https://api.wheretheiss.at/v1/satellites/25544/positions"
EARTH_RADIUS_KM = 6371.0
BSKY = "https://bsky.social/xrpc"


# ----------------------------------------------------------------------------
# Orbital geometry
# ----------------------------------------------------------------------------

def elevation_deg(obs_lat, obs_lon, sat_lat, sat_lon, sat_alt_km):
    """Elevation of the satellite above the observer's horizon, in degrees.

    Negative means below the horizon, i.e. the Earth is in the way.
    """
    p1, p2 = math.radians(obs_lat), math.radians(sat_lat)
    dlon = math.radians(sat_lon - obs_lon)
    cos_gamma = (math.sin(p1) * math.sin(p2)
                 + math.cos(p1) * math.cos(p2) * math.cos(dlon))
    cos_gamma = max(-1.0, min(1.0, cos_gamma))
    gamma = math.acos(cos_gamma)
    re, rs = EARTH_RADIUS_KM, EARTH_RADIUS_KM + sat_alt_km
    return math.degrees(math.atan2(math.cos(gamma) - re / rs, math.sin(gamma)))


def fetch_positions(timestamps):
    """Ask the API where the station is (or will be) at each unix timestamp.

    The endpoint accepts at most 10 timestamps per call, so we chunk.
    """
    out = []
    for i in range(0, len(timestamps), 10):
        chunk = timestamps[i:i + 10]
        url = API + "?" + urllib.parse.urlencode({
            "timestamps": ",".join(str(t) for t in chunk),
            "units": "kilometers",
        })
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "iss-radio-bot/1.0"})
        r.raise_for_status()
        data = r.json()
        out.extend(data if isinstance(data, list) else [data])
        time.sleep(0.4)          # be polite to a free API
    return sorted(out, key=lambda p: p["timestamp"])


def sample(timestamps):
    """Fetch positions and return [(timestamp, {station: elevation}, pos), ...]."""
    rows = []
    for pos in fetch_positions(timestamps):
        els = {}
        for st in STATIONS:
            els[st["name"]] = elevation_deg(st["lat"], st["lon"],
                                            pos["latitude"], pos["longitude"],
                                            pos["altitude"])
        rows.append((pos["timestamp"], els, pos))
    return rows


def best_elevation(els):
    """The highest elevation any of our stations sees at this instant."""
    return max(els.values())


def find_next_pass(now):
    """Find the next time the ISS is in range of ANY of our stations.

    Several stations usually hear the same pass, so this returns one event
    with a list of which stations can hear it and how high it gets for each.
    Stations too far apart to share a pass simply end up in separate events.
    """
    coarse_step = 180
    coarse = sample([now + s for s in
                     range(0, HORIZON_MINUTES * 60 + 1, coarse_step)])

    idx = next((i for i, (_, els, _) in enumerate(coarse)
                if best_elevation(els) >= MIN_ELEVATION), None)
    if idx is None:
        return None

    win_start = coarse[max(0, idx - 1)][0]
    win_end = coarse[min(len(coarse) - 1, idx + 4)][0]
    fine = sample(list(range(win_start, win_end + 1, 20)))

    above = [(t, els, pos) for t, els, pos in fine
             if best_elevation(els) >= MIN_ELEVATION]
    if not above:
        return None

    rise_t, set_t = above[0][0], above[-1][0]

    # per-station: how high does each one see this pass get?
    peaks = {}
    for _, els, _ in above:
        for name, el in els.items():
            if el > peaks.get(name, -90.0):
                peaks[name] = el

    # only name the stations that actually get a usable look at it
    heard_by = [st for st in STATIONS if peaks.get(st["name"], -90) >= MIN_ELEVATION]
    heard_by.sort(key=lambda st: peaks[st["name"]], reverse=True)

    overall_peak = max(peaks.values())
    peak_t = max(above, key=lambda r: best_elevation(r[1]))[0]

    return {
        "rise": rise_t,
        "set": set_t,
        "peak_elevation": overall_peak,
        "peak_time": peak_t,
        "duration_min": (set_t - rise_t) / 60.0,
        "stations": heard_by,
        "peaks": peaks,
        "sunlit": above[0][2].get("visibility") == "daylight",
        "key": int(round(rise_t / 60.0)),
    }


# ----------------------------------------------------------------------------
# State, so a pass is only announced once
# ----------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"announced": [], "last_post": 0, "per_day": {}}


def save_state(state):
    state["announced"] = sorted(state.get("announced", []))[-80:]
    per_day = state.get("per_day", {})
    for day in sorted(per_day)[:-14]:      # keep a fortnight
        del per_day[day]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)
        f.write("\n")


# ----------------------------------------------------------------------------
# Composing the post
# ----------------------------------------------------------------------------

def hhmm(ts):
    return time.strftime("%H:%M", time.gmtime(ts))


def compose(p, now):
    mins = max(0, int(round((p["rise"] - now) / 60.0)))
    when = "NOW" if mins <= 1 else "in %d min" % mins

    stations = p["stations"]
    best = stations[0] if stations else STATIONS[0]
    heard = " \u00B7 ".join("%s %d\u00B0" % (st["name"], round(p["peaks"][st["name"]]))
                            for st in stations)

    def build(include_site, station_list):
        heard_txt = " \u00B7 ".join(
            "%s %d\u00B0" % (st["name"], round(p["peaks"][st["name"]]))
            for st in station_list)
        out = ["\U0001F6F0 ISS in radio range %s \u2014 %s UTC" % (when, hhmm(p["rise"])),
               "",
               "Audible from: " + heard_txt,
               "%d min of listening \u00B7 tune 145.800 MHz FM:" % round(p["duration_min"]),
               best["sdr"]]
        if include_site and WEBSITE:
            out += ["", WEBSITE]
        return "\n".join(out)

    # Bluesky caps a post at 300 characters. Shed the website first, then
    # the quieter stations, until it fits.
    text = build(True, stations)
    if len(text) > 300:
        text = build(False, stations)
    while len(text) > 300 and len(stations) > 1:
        stations = stations[:-1]
        text = build(False, stations)
    return text


URL_RE = re.compile(rb"https?://[^\s\)\]]+")


def facets_for(text):
    """Bluesky needs byte offsets to make links clickable."""
    raw = text.encode("utf-8")
    facets = []
    for m in URL_RE.finditer(raw):
        uri = m.group(0).decode("utf-8").rstrip(".,;")
        facets.append({
            "index": {"byteStart": m.start(),
                      "byteEnd": m.start() + len(uri.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": uri}],
        })
    return facets


# ----------------------------------------------------------------------------
# Bluesky
# ----------------------------------------------------------------------------

def post_to_bluesky(text):
    handle = os.environ.get("BSKY_HANDLE", "").strip()
    password = os.environ.get("BSKY_APP_PASSWORD", "").strip()
    if not handle or not password:
        raise SystemExit("BSKY_HANDLE and BSKY_APP_PASSWORD must both be set.")

    s = requests.Session()
    r = s.post(BSKY + "/com.atproto.server.createSession",
               json={"identifier": handle, "password": password}, timeout=20)
    if r.status_code != 200:
        raise SystemExit("Could not sign in to Bluesky (%s). Check the handle, "
                         "and make sure you used an App Password."
                         % r.status_code)
    session = r.json()

    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "facets": facets_for(text),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "langs": ["en"],
    }
    r = s.post(BSKY + "/com.atproto.repo.createRecord",
               headers={"Authorization": "Bearer " + session["accessJwt"]},
               json={"repo": session["did"],
                     "collection": "app.bsky.feed.post",
                     "record": record},
               timeout=20)
    if r.status_code != 200:
        raise SystemExit("Bluesky refused the post (%s): %s"
                         % (r.status_code, r.text[:300]))
    return r.json()


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="work it all out and print it, but never post")
    ap.add_argument("--force", action="store_true",
                    help="ignore the lead time and the already-posted list")
    args = ap.parse_args()

    now = int(time.time())
    print("checking at %s UTC across %d receivers: %s"
          % (hhmm(now), len(STATIONS),
             ", ".join(st["name"] for st in STATIONS)))

    try:
        p = find_next_pass(now)
    except requests.RequestException as e:
        print("could not reach the position API: %s" % e)
        return 0          # a failed check is not a failed build

    if not p:
        print("no pass above %g° in the next %d minutes. Nothing to say."
              % (MIN_ELEVATION, HORIZON_MINUTES))
        return 0

    mins_away = (p["rise"] - now) / 60.0
    print("next pass: rises %s UTC (%.1f min away), peaks %.0f°, lasts %.0f min"
          % (hhmm(p["rise"]), mins_away, p["peak_elevation"], p["duration_min"]))

    if p["peak_elevation"] < MIN_PEAK_ELEVATION and not args.force:
        print("peak is below %g°, too low to be worth a post." % MIN_PEAK_ELEVATION)
        return 0

    state = load_state()
    if p["key"] in state.get("announced", []) and not args.force:
        print("already posted about this pass. Nothing to do.")
        return 0

    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    posted_today = state.get("per_day", {}).get(today, 0)
    if posted_today >= MAX_POSTS_PER_DAY and not args.force:
        print("already posted %d times today (cap is %d). Staying quiet."
              % (posted_today, MAX_POSTS_PER_DAY))
        return 0

    since = (now - state.get("last_post", 0)) / 60.0
    if since < MIN_GAP_MINUTES and not args.force:
        print("posted only %.0f min ago, holding off (minimum gap is %d min)."
              % (since, MIN_GAP_MINUTES))
        return 0

    if mins_away > LEAD_MINUTES and not args.force:
        print("still %.1f min away, waiting until it is within %d."
              % (mins_away, LEAD_MINUTES))
        return 0

    text = compose(p, now)
    print("-" * 56)
    print(text)
    print("-" * 56)
    print("%d characters" % len(text))

    if args.dry_run:
        print("dry run, nothing posted.")
        return 0

    post_to_bluesky(text)
    print("posted.")

    state.setdefault("announced", []).append(p["key"])
    state["last_post"] = now
    per_day = state.setdefault("per_day", {})
    per_day[today] = per_day.get(today, 0) + 1
    print("that is %d of %d posts for today." % (per_day[today], MAX_POSTS_PER_DAY))
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
