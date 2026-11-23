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

# The ground station we care about. Default is the Goonhilly WebSDR in
# Cornwall, which is the one the website recommends for UK passes.
STATION = {
    "name": "Goonhilly, Cornwall",
    "lat": 50.0524,
    "lon": -5.1823,
    "sdr": "https://vhf-goonhilly.batc.org.uk/?tune=145800fm",
}

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
    """Fetch positions and return [(timestamp, elevation, position), ...]."""
    rows = []
    for p in fetch_positions(timestamps):
        el = elevation_deg(STATION["lat"], STATION["lon"],
                           p["latitude"], p["longitude"], p["altitude"])
        rows.append((p["timestamp"], el, p))
    return rows


def find_next_pass(now):
    """Find the next pass over the station. Returns a dict, or None.

    Two stages: a coarse 3-minute scan to find roughly where the station
    rises above the horizon, then a 20-second scan to pin down the exact
    rise and set times.
    """
    coarse_step = 180
    coarse = sample([now + s for s in
                     range(0, HORIZON_MINUTES * 60 + 1, coarse_step)])

    # find the first coarse sample that is above our threshold
    idx = next((i for i, (_, el, _) in enumerate(coarse)
                if el >= MIN_ELEVATION), None)
    if idx is None:
        return None

    # refine: scan the window around the crossing at 20-second resolution
    win_start = coarse[max(0, idx - 1)][0]
    win_end = min(coarse[-1][0], coarse[min(len(coarse) - 1, idx + 4)][0])
    fine = sample(list(range(win_start, win_end + 1, 20)))

    above = [(t, el, p) for t, el, p in fine if el >= MIN_ELEVATION]
    if not above:
        return None

    rise_t = above[0][0]
    set_t = above[-1][0]
    peak_t, peak_el, peak_p = max(above, key=lambda r: r[1])

    return {
        "rise": rise_t,
        "set": set_t,
        "peak_elevation": peak_el,
        "peak_time": peak_t,
        "duration_min": (set_t - rise_t) / 60.0,
        "altitude": peak_p["altitude"],
        "sunlit": peak_p.get("visibility") == "daylight",
        # the pass is identified by its rise time rounded to the minute,
        # so repeated runs agree on what to call it
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
        return {"announced": []}


def save_state(state):
    state["announced"] = sorted(state.get("announced", []))[-80:]
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

    lines = []
    lines.append("\U0001F6F0 ISS in radio range %s — %s UTC" % (when, hhmm(p["rise"])))
    lines.append("")
    lines.append("Peaks at %d° above the horizon · %d min of listening"
                 % (round(p["peak_elevation"]), round(p["duration_min"])))
    lines.append("Tune 145.800 MHz FM, no licence needed:")
    lines.append(STATION["sdr"])
    if WEBSITE:
        lines.append("")
        lines.append(WEBSITE)
    return "\n".join(lines)


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
    print("checking at %s UTC for %s" % (hhmm(now), STATION["name"]))

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
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
