/**
 * ISS radio-range bot - Cloudflare Worker
 * ---------------------------------------
 * Posts to Bluesky when the International Space Station is about to come
 * into radio range of a ground station, so people have time to open a
 * WebSDR and actually hear it.
 *
 * Runs on a Cron Trigger. Most runs do nothing: it only speaks when there
 * is a pass worth announcing.
 *
 * There is no database. The bot reads its OWN Bluesky feed to work out what
 * it has already said - which gives deduplication, the daily cap and the
 * minimum gap between posts, with nothing to configure and nothing to
 * corrupt. If a post exists, it happened; that is the only source of truth
 * that matters.
 *
 * Secrets (Worker Settings -> Variables and Secrets, set as "Secret"):
 *   BSKY_HANDLE        e.g. isstracker.neocities.org
 *   BSKY_APP_PASSWORD  an App Password, never the account password
 *
 * Visiting the Worker's URL in a browser runs a DRY RUN and prints what it
 * would do, without posting. Handy for testing.
 */

// ---------------------------------------------------------------------------
// CONFIGURATION
// ---------------------------------------------------------------------------

// One receiver per region. Receivers close together hear the same pass and
// add nothing but noise, so these are deliberately far apart.
const STATIONS = [
  { name: "Goonhilly", lat: 50.0524, lon: -5.1823,
    sdr: "https://vhf-goonhilly.batc.org.uk/?tune=145800fm" },
  { name: "Utah",      lat: 40.7000, lon: -111.8000,
    sdr: "http://slc2meters.sdrutah.org:8901/?tune=145800fm" },
  { name: "R4UAB",     lat: 54.3282, lon: 48.3866,
    sdr: "http://websdr.r4uab.ru/" },
  // Swaps: Farnham 51.2149/-0.7987 · Dwingeloo 52.8122/6.3964
  //        Washington DC 38.9200/-77.0700 http://websdr.us:8902/
];

const MIN_ELEVATION      = 10;   // degrees: when a pass counts as started
const MIN_PEAK_ELEVATION = 20;   // skip passes that never get this high
const LEAD_MINUTES       = 15;   // post this far ahead of the pass
const HORIZON_MINUTES    = 75;   // how far ahead to look
const MIN_GAP_MINUTES    = 45;   // never two posts inside this window
const MAX_POSTS_PER_DAY  = 6;    // hard ceiling per UTC day
const WEBSITE            = "https://isstracker.neocities.org/";

const EARTH_RADIUS_KM = 6371;
const POS_API = "https://api.wheretheiss.at/v1/satellites/25544/positions";
const BSKY = "https://bsky.social/xrpc";

// ---------------------------------------------------------------------------
// Orbital geometry
// ---------------------------------------------------------------------------

const rad = (d) => (d * Math.PI) / 180;
const deg = (r) => (r * 180) / Math.PI;

/** Elevation of the satellite above an observer's horizon, in degrees.
 *  Negative means the Earth is in the way. */
function elevationDeg(obsLat, obsLon, satLat, satLon, satAltKm) {
  const p1 = rad(obsLat), p2 = rad(satLat), dlon = rad(satLon - obsLon);
  let c = Math.sin(p1) * Math.sin(p2) +
          Math.cos(p1) * Math.cos(p2) * Math.cos(dlon);
  c = Math.max(-1, Math.min(1, c));
  const gamma = Math.acos(c);
  const re = EARTH_RADIUS_KM, rs = EARTH_RADIUS_KM + satAltKm;
  return deg(Math.atan2(Math.cos(gamma) - re / rs, Math.sin(gamma)));
}

/** Where is the ISS at each unix timestamp? The API propagates the orbit,
 *  so future timestamps work. It takes at most 10 per call. */
async function fetchPositions(timestamps) {
  const out = [];
  for (let i = 0; i < timestamps.length; i += 10) {
    const chunk = timestamps.slice(i, i + 10);
    const url = `${POS_API}?timestamps=${chunk.join(",")}&units=kilometers`;
    const r = await fetch(url, {
      headers: { "User-Agent": "iss-radio-bot/2.0 (cloudflare-worker)" },
    });
    if (!r.ok) throw new Error(`position API returned ${r.status}`);
    const data = await r.json();
    out.push(...(Array.isArray(data) ? data : [data]));
  }
  return out.sort((a, b) => a.timestamp - b.timestamp);
}

/** Fetch positions and attach each station's elevation. */
async function sample(timestamps) {
  const positions = await fetchPositions(timestamps);
  return positions.map((pos) => {
    const els = {};
    for (const st of STATIONS) {
      els[st.name] = elevationDeg(st.lat, st.lon,
                                  pos.latitude, pos.longitude, pos.altitude);
    }
    return { t: pos.timestamp, els, pos };
  });
}

const bestElevation = (els) => Math.max(...Object.values(els));

/** Find the next time the ISS is in range of ANY station.
 *  Coarse 3-minute scan to locate the crossing, then 20-second scan to pin
 *  down the rise and set times. */
async function findNextPass(now) {
  const coarseTimes = [];
  for (let s = 0; s <= HORIZON_MINUTES * 60; s += 180) coarseTimes.push(now + s);
  const coarse = await sample(coarseTimes);

  const idx = coarse.findIndex((r) => bestElevation(r.els) >= MIN_ELEVATION);
  if (idx === -1) return null;

  const winStart = coarse[Math.max(0, idx - 1)].t;
  const winEnd = coarse[Math.min(coarse.length - 1, idx + 4)].t;
  const fineTimes = [];
  for (let t = winStart; t <= winEnd; t += 20) fineTimes.push(t);
  const fine = await sample(fineTimes);

  const above = fine.filter((r) => bestElevation(r.els) >= MIN_ELEVATION);
  if (!above.length) return null;

  const rise = above[0].t, set = above[above.length - 1].t;

  // how high does each station see this pass get?
  const peaks = {};
  for (const r of above) {
    for (const [name, el] of Object.entries(r.els)) {
      if (el > (peaks[name] ?? -90)) peaks[name] = el;
    }
  }

  const stations = STATIONS
    .filter((st) => (peaks[st.name] ?? -90) >= MIN_ELEVATION)
    .sort((a, b) => peaks[b.name] - peaks[a.name]);

  return {
    rise, set,
    peakElevation: Math.max(...Object.values(peaks)),
    durationMin: (set - rise) / 60,
    stations, peaks,
    sunlit: above[0].pos.visibility === "daylight",
  };
}

// ---------------------------------------------------------------------------
// Composing the post
// ---------------------------------------------------------------------------

const hhmm = (ts) => new Date(ts * 1000).toISOString().slice(11, 16);
const utcDay = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);

function compose(pass, now) {
  const mins = Math.max(0, Math.round((pass.rise - now) / 60));
  const when = mins <= 1 ? "NOW" : `in ${mins} min`;
  const best = pass.stations[0] ?? STATIONS[0];

  const build = (withSite, list) => {
    const heard = list
      .map((st) => `${st.name} ${Math.round(pass.peaks[st.name])}°`)
      .join(" · ");
    const lines = [
      `\u{1F6F0} ISS in radio range ${when} — ${hhmm(pass.rise)} UTC`,
      "",
      `Audible from: ${heard}`,
      `${Math.round(pass.durationMin)} min of listening · tune 145.800 MHz FM:`,
      best.sdr,
    ];
    if (withSite && WEBSITE) lines.push("", WEBSITE);
    return lines.join("\n");
  };

  // Bluesky caps posts at 300 characters. Shed the website first, then the
  // quieter stations, until it fits.
  let list = pass.stations.slice();
  let text = build(true, list);
  if (text.length > 300) text = build(false, list);
  while (text.length > 300 && list.length > 1) {
    list = list.slice(0, -1);
    text = build(false, list);
  }
  return text;
}

/** Bluesky needs BYTE offsets, not character offsets, to make links
 *  clickable. Emoji and degree signs are multi-byte, so this matters. */
function facetsFor(text) {
  const enc = new TextEncoder();
  const facets = [];
  const re = /https?:\/\/[^\s)\]]+/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const uri = m[0].replace(/[.,;]+$/, "");
    const byteStart = enc.encode(text.slice(0, m.index)).length;
    facets.push({
      index: { byteStart, byteEnd: byteStart + enc.encode(uri).length },
      features: [{ $type: "app.bsky.richtext.facet#link", uri }],
    });
  }
  return facets;
}

// ---------------------------------------------------------------------------
// Bluesky
// ---------------------------------------------------------------------------

async function login(env) {
  const handle = (env.BSKY_HANDLE || "").trim();
  const password = (env.BSKY_APP_PASSWORD || "").trim();
  if (!handle || !password) {
    throw new Error("BSKY_HANDLE and BSKY_APP_PASSWORD must both be set as " +
                    "Worker secrets.");
  }
  const r = await fetch(`${BSKY}/com.atproto.server.createSession`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identifier: handle, password }),
  });
  if (!r.ok) {
    throw new Error(`Bluesky sign-in failed (${r.status}). Check the handle, ` +
                    `and make sure you used an App Password.`);
  }
  return r.json();
}

/** The bot's own recent posts ARE its memory. No database needed. */
async function recentPosts(session) {
  const url = `${BSKY}/app.bsky.feed.getAuthorFeed` +
              `?actor=${encodeURIComponent(session.did)}&limit=30&filter=posts_no_replies`;
  const r = await fetch(url, {
    headers: { Authorization: `Bearer ${session.accessJwt}` },
  });
  if (!r.ok) return [];          // never block posting because history failed
  const data = await r.json();
  return (data.feed || [])
    .filter((item) => item.post?.author?.did === session.did)
    .map((item) => ({
      text: item.post.record?.text || "",
      createdAt: Date.parse(item.post.record?.createdAt || 0) / 1000,
    }))
    .filter((p) => p.createdAt > 0);
}

async function postToBluesky(session, text) {
  const r = await fetch(`${BSKY}/com.atproto.repo.createRecord`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${session.accessJwt}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      repo: session.did,
      collection: "app.bsky.feed.post",
      record: {
        $type: "app.bsky.feed.post",
        text,
        facets: facetsFor(text),
        createdAt: new Date().toISOString(),
        langs: ["en"],
      },
    }),
  });
  if (!r.ok) {
    throw new Error(`Bluesky refused the post (${r.status}): ` +
                    (await r.text()).slice(0, 300));
  }
  return r.json();
}

// ---------------------------------------------------------------------------
// The decision
// ---------------------------------------------------------------------------

async function run(env, { dryRun = false } = {}) {
  const log = [];
  const say = (s) => { log.push(s); };
  const now = Math.floor(Date.now() / 1000);

  say(`checking at ${hhmm(now)} UTC across ${STATIONS.length} receivers: ` +
      STATIONS.map((s) => s.name).join(", "));

  let pass;
  try {
    pass = await findNextPass(now);
  } catch (e) {
    say(`could not reach the position API: ${e.message}`);
    return { log, posted: false };
  }

  if (!pass) {
    say(`no pass above ${MIN_ELEVATION}° in the next ${HORIZON_MINUTES} ` +
        `minutes. Nothing to say.`);
    return { log, posted: false };
  }

  const minsAway = (pass.rise - now) / 60;
  say(`next pass: rises ${hhmm(pass.rise)} UTC (${minsAway.toFixed(1)} min away), ` +
      `lasts ${pass.durationMin.toFixed(0)} min`);
  for (const st of pass.stations) {
    say(`   ${st.name.padEnd(11)} peaks ${pass.peaks[st.name].toFixed(1)}°`);
  }

  if (pass.peakElevation < MIN_PEAK_ELEVATION) {
    say(`peak is below ${MIN_PEAK_ELEVATION}°, too low to be worth a post.`);
    return { log, posted: false };
  }
  if (minsAway > LEAD_MINUTES) {
    say(`still ${minsAway.toFixed(1)} min away, waiting until it is within ` +
        `${LEAD_MINUTES}.`);
    return { log, posted: false };
  }

  const text = compose(pass, now);

  // --- the feed is the memory -------------------------------------------
  let session = null, history = [];
  if (!dryRun || (env.BSKY_HANDLE && env.BSKY_APP_PASSWORD)) {
    try {
      session = await login(env);
      history = await recentPosts(session);
    } catch (e) {
      say(`Bluesky: ${e.message}`);
      if (!dryRun) return { log, posted: false, error: e.message };
    }
  }

  if (session) {
    const stamp = `${hhmm(pass.rise)} UTC`;
    if (history.some((p) => p.text.includes(stamp))) {
      say(`already posted about the ${stamp} pass. Nothing to do.`);
      return { log, posted: false };
    }
    const today = utcDay(now);
    const todayCount = history.filter((p) => utcDay(p.createdAt) === today).length;
    if (todayCount >= MAX_POSTS_PER_DAY) {
      say(`already posted ${todayCount} times today (cap ${MAX_POSTS_PER_DAY}). ` +
          `Staying quiet.`);
      return { log, posted: false };
    }
    const newest = history.length ? Math.max(...history.map((p) => p.createdAt)) : 0;
    const sinceMin = newest ? (now - newest) / 60 : Infinity;
    if (sinceMin < MIN_GAP_MINUTES) {
      say(`posted only ${sinceMin.toFixed(0)} min ago, holding off ` +
          `(minimum gap ${MIN_GAP_MINUTES} min).`);
      return { log, posted: false };
    }
    say(`${todayCount} post(s) so far today; last was ` +
        `${sinceMin === Infinity ? "never" : sinceMin.toFixed(0) + " min ago"}.`);
  }

  say("-".repeat(52));
  say(text);
  say("-".repeat(52));
  say(`${text.length} characters, ${facetsFor(text).length} link(s)`);

  if (dryRun) {
    say("DRY RUN - nothing was posted.");
    return { log, posted: false };
  }

  await postToBluesky(session, text);
  say("posted.");
  return { log, posted: true };
}

// ---------------------------------------------------------------------------

export default {
  // the Cron Trigger calls this
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      run(env).then((r) => {
        console.log(r.log.join("\n"));
      }).catch((e) => {
        console.error("bot failed:", e.stack || e.message);
      })
    );
  },

  // visiting the Worker URL in a browser: dry run, never posts
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/favicon.ico") return new Response(null, { status: 204 });
    let body;
    try {
      const r = await run(env, { dryRun: true });
      body = r.log.join("\n");
    } catch (e) {
      body = `error: ${e.stack || e.message}`;
    }
    return new Response(
      "ISS radio-range bot — dry run (this page never posts)\n" +
      "=".repeat(52) + "\n" + body + "\n",
      { headers: { "Content-Type": "text/plain; charset=utf-8" } }
    );
  },
};
