# ISS radio-range bot

Posts to Bluesky when the International Space Station is about to come into
radio range of a ground station, so people have time to open a WebSDR and
actually hear it.

It checks every 5 minutes and posts roughly 4-6 times a day: once per usable
pass, about 12 minutes before the pass begins. Most runs do nothing, which is
deliberate. A bot that posts every 5 minutes gets muted.

## What a post looks like

```
🛰 ISS in radio range in 6 min — 08:13 UTC

Peaks at 40° above the horizon · 6 min of listening
Tune 145.800 MHz FM, no licence needed:
https://vhf-goonhilly.batc.org.uk/?tune=145800fm

https://isstracker.neocities.org/
```

## Setup

1. **Make an App Password.** In Bluesky: Settings → Privacy and Security →
   App Passwords → Add App Password. Call it `iss-bot`. Copy the password it
   gives you (it looks like `abcd-efgh-ijkl-mnop`).

   Use an App Password. Never your real account password. An App Password can
   be revoked on its own without touching your account, and cannot change your
   email or delete your account.

2. **Put it in GitHub.** In your repository: Settings → Secrets and variables →
   Actions → New repository secret. Add two:

   | Name                | Value                                  |
   |---------------------|----------------------------------------|
   | `BSKY_HANDLE`       | `yourname.bsky.social`                 |
   | `BSKY_APP_PASSWORD` | the app password from step 1           |

   Secrets are encrypted and are not visible in logs, to you or to anyone else.

3. **Push this folder** to the repository, keeping the layout:

   ```
   iss_bot.py
   state.json
   README.md
   .github/workflows/iss-bot.yml
   ```

4. **Turn it on.** Actions tab → enable workflows if prompted → pick
   "ISS radio-range bot" → "Run workflow" to try it once by hand.

## Trying it before you let it loose

```bash
pip install requests
python iss_bot.py --dry-run     # works everything out, posts nothing
python iss_bot.py --force --dry-run   # same, ignoring timing and history
```

`--dry-run` never touches Bluesky and needs no credentials, so it is safe to
run as often as you like.

## Tuning it

All of this is at the top of `iss_bot.py`:

| Setting              | Default | What it does                                      |
|----------------------|---------|---------------------------------------------------|
| `STATION`            | Goonhilly | Which ground station to watch. Change the lat/lon and SDR link to use another. |
| `MIN_ELEVATION`      | `10.0`  | How high it must get before the pass counts as started. |
| `MIN_PEAK_ELEVATION` | `20.0`  | Skip passes that never get higher than this. Raise to 40 for only the excellent ones. |
| `LEAD_MINUTES`       | `12`    | How far ahead of the pass to post.                |
| `WEBSITE`            | your site | Appended to each post. Set to `None` to drop it. |

Posting less often is usually the right call. `MIN_PEAK_ELEVATION = 40` gets
you down to one or two posts a day, and every one of them is a pass worth
dropping what you are doing for.

## How it works

`wheretheiss.at` will tell you where the station is at any timestamp, past or
future, so the bot asks where it will be over the next 75 minutes, works out
the elevation above the horizon from the ground station for each of those
moments, and finds the next time it climbs above `MIN_ELEVATION`. It scans
coarsely first, then refines around the crossing to pin down the rise and set
times.

Each pass is keyed by its rise time, recorded in `state.json`, and committed
back to the repository after posting. That is what stops the same pass being
announced five times as it approaches.

## Notes

- GitHub's scheduled runs are best-effort and are frequently late, especially
  on the hour. The lead time absorbs this. It also means an occasional pass
  will be missed entirely; that is the price of free hosting.
- The position API is free and needs no key. Be decent to it: the bot makes
  about four requests per run and pauses between them.
- If `state.json` stops being committed, check that the workflow has
  `permissions: contents: write`, and that Settings → Actions → General →
  Workflow permissions is set to read and write.
