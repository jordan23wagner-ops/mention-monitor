# mention-monitor

A self-hosted replacement for [Syften](https://syften.com/) (~$15–49/mo) in a single
Python file. It polls free public APIs for keyword mentions of your products,
de-duplicates against previous runs, prints a digest, and optionally pushes it to
Slack or Discord.

- **Zero dependencies.** Standard library only (`urllib`, `json`, `xml.etree`, …). Python 3.8+.
- **No API keys.** Every source is a public, key-free endpoint.
- **Free forever.** Runs on GitHub Actions' free tier in a few seconds per run.

```
mention_monitor.py                       the whole program
config.json                              your keywords (auto-created on first run)
seen.json                                dedup state (auto-created)
.github/workflows/mention-monitor.yml    scheduled CI run, every 6 hours
```

## Quick start

```bash
python3 mention_monitor.py            # writes a sample config.json, then runs
$EDITOR config.json                   # put your real keywords in
python3 mention_monitor.py --seed     # prime state so you don't get a backlog flood
python3 mention_monitor.py            # from here on, only NEW mentions
```

Optional webhooks — set either, both, or neither:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

### CLI

| Flag | Meaning |
| --- | --- |
| `--config PATH` | Config file (default `config.json`) |
| `--state PATH` | Dedup state file (default `seen.json`) |
| `--hours N` | Override `lookback_hours` for this run |
| `--seed` | Record current matches without reporting them |
| `--dry-run` | Don't write state, don't post webhooks |
| `--no-post` | Run normally but skip webhooks |

The digest goes to **stdout**; progress and errors go to **stderr**, so
`python3 mention_monitor.py > digest.txt` gives you a clean file.

## Configuration

```json
{
  "lookback_hours": 24,
  "request_delay_seconds": 2.0,
  "reddit_delay_seconds": 6.0,
  "max_seen_ids": 5000,
  "strict_match": true,
  "sources": { "hackernews": true, "reddit": true, "lobsters": true },
  "exclude": ["onlyfans", "crypto airdrop", "promo code"],
  "projects": {
    "chatwillow":    ["chatwillow", "chatgpt alternative", "free ai chat"],
    "agent-handoff": ["agent handoff", "agent orchestration", "stripe metered billing"],
    "flagcheck":     ["ats resume", "job description red flags"]
  }
}
```

- **`projects`** — keyword groups. Each term is queried against every enabled
  source; the digest is grouped by project.
- **`exclude`** — case-insensitive substrings. If any appears in an item's
  title, body, URL, or subreddit, the item is dropped. This is your main noise knob.
- **`strict_match`** (default `true`) — see below. Leave it on.
- **`max_seen_ids`** — the state file keeps only the most recent N ids (default
  5000), so it never grows without bound.

### Why `strict_match` matters

Both HN's Algolia index and Reddit's search OR-match individual words: a query for
`"agent handoff"` returns anything containing *agent*. With `strict_match` on, an
item is only reported if the literal phrase appears in its title, body, or URL.

In a real run of the sample config, this dropped **458 of 467 fetched items** as
loose matches. Turning it off gives you Syften-style recall with far more noise.

## Sources, and what actually works

Verified live on 2026-07-27; **Reddit's status below is stale as of 2026-08-01,
see the note in that section.** Read this section before trusting a silent run.

### Hacker News — works exactly as documented

`https://hn.algolia.com/api/v1/search_by_date` with `tags=(story,comment)` and a
`created_at_i>=` numeric filter. Covers both stories and comments, no key, no
meaningful rate limit at this volume. This is the most reliable source of the three.

### Reddit — DISABLED as of 2026-08-01, unauthenticated access is gone

**Do not re-enable this by flipping `sources.reddit` back to `true`** without
also implementing OAuth. Reddit deprecated unauthenticated access to
`.json`/`.rss` endpoints on **2026-05-28**; anonymous requests now get
403/429 by design, not as a rate-limiting quirk. This isn't a User-Agent
problem or a delay-tuning problem — the 2026-07-27 "works via RSS fallback"
note below described a window that has since closed. Confirmed by re-running
against live Reddit on 2026-08-01: every single term failed with 403/429 on
both the JSON and RSS paths, from both a CI-like environment and a
residential IP, regardless of `reddit_delay_seconds`.

Reddit coverage requires the OAuth `client_credentials` flow
(`https://www.reddit.com/api/v1/access_token` for a token, then
`https://oauth.reddit.com/...` with a bearer token) using an app you create
yourself at reddit.com/prefs/apps. That's the next phase of this tool; until
it ships, `sources.reddit` stays `false` and coverage is Hacker News +
Lobste.rs only.

Historical notes from when unauthenticated access still worked, kept for
context once OAuth lands:

- `sort=new` searches post bodies, not just titles. Hits whose titles look
  unrelated usually do contain your phrase further down. This is correct
  behaviour, but it's why `exclude` earns its keep.
- Reddit search returns subreddit results (`t5_*`) alongside posts; those
  were filtered out.
- Comments are not covered by Reddit's public search - posts only.

### Lobste.rs — the documented JSON search does not exist

The spec'd `https://lobste.rs/search?q=…&format=json` **does not work**. It returns:

```
HTTP 400  {"error":"400 Unpermitted query or form parameter"}
```

The same is true of `/search.json?q=…`. Lobste.rs' search has no JSON
representation at all — only HTML. I'm not papering over this: the script still
attempts the documented URL first (so it starts working for free if Lobste.rs ever
adds it), then falls back to **`https://lobste.rs/newest.json`**, a real supported
endpoint, and filters those stories locally for your term.

The honest limitation: the fallback only sees the **~25 most recent submissions**,
so Lobste.rs coverage is shallow. At a 6-hour poll interval you'll catch most of
its (low) volume, but a burst of activity can push a match out of the window before
you see it. Scraping the HTML search page would fix this at the cost of fragility.
Set `"lobsters": false` in `sources` if you'd rather not bother.

## Deduplication

Every item gets a stable id (`hn:49062508`, `reddit:t3_1v89fp6`, `lobsters:xoxury`)
which is appended to `seen.json`. Ids are checked before reporting, so a given item
is reported exactly once, ever — even though `sort=new` returns the same 50 posts on
consecutive runs. The store is trimmed to the most recent `max_seen_ids` entries and
written atomically via a temp file + `os.replace`, so an interrupted run can't
corrupt it.

## GitHub Actions

`.github/workflows/mention-monitor.yml` runs on a schedule once this repo is
pushed. Add whichever secrets you want under
**Settings → Secrets and variables → Actions**:

- `SLACK_WEBHOOK_URL`
- `DISCORD_WEBHOOK_URL`

The workflow runs every 6 hours, commits the updated `seen.json` back to the repo
(so dedup persists across runs), also caches it as a backstop, writes the digest to
the run's job summary, and uploads it as an artifact. It needs `permissions:
contents: write`, which is already set. Commits are tagged `[skip ci]` so state
updates don't trigger other workflows.

You can also trigger it by hand from the Actions tab, with an adjustable lookback
window and a seed-only mode.

Caveats for CI:

- GitHub's cron is best-effort and can be delayed under load. The default lookback
  is 12h against a 6h schedule, so a skipped run doesn't lose mentions — dedup makes
  the overlap free.
- As noted above, Reddit's JSON API is blocked from Actions runners; the RSS
  fallback carries it. If Reddit ever blocks RSS from CI too, the run will report
  the failure per-term and keep going rather than dying.

## Robustness

Every network call goes through one helper with a timeout, up to `max_retries`
attempts, exponential backoff, and `Retry-After` support for 429/5xx. Each
(source, term) pair is wrapped independently: a source that is completely dead —
DNS failure, 403, malformed JSON — costs you that source's results and nothing
else. The run exits 0 and lists what broke in the digest footer:

```
1/6 source-queries succeeded, 50 items seen, 7 new after dedup.
  ! hackernews / "agent orchestration": network error: Tunnel connection failed: 502 Bad Gateway
  ! reddit / "ats resume": HTTP 429
  ! lobsters / "ats resume": network error: Tunnel connection failed: 502 Bad Gateway
```

Watch that footer. A run reporting "No new mentions" with 0/N successful queries
means something is broken, not that the internet went quiet.

Webhook failures are caught too — a dead Slack URL logs a warning and doesn't lose
you the digest, which has already gone to stdout.

## Tuning notes

- Start with `--seed`, or the first run dumps everything in the lookback window at you.
- Long, specific phrases beat single words. `"chatwillow"` is perfect; `"agent"`
  would be unusable.
- Build the `exclude` list reactively: when junk shows up, add a substring from it.
  Hiring posts, cert-dump spam, and crossposted vendor announcements are the usual suspects.
- Cross-posts appear as separate items in different subreddits and dedup treats them
  as distinct, because their ids differ. Excluding by author or title is the workaround.
- Adding sources means writing one function that returns the normalized item dict
  (`id, source, title, url, author, text, created, context`) and registering it in
  the `SOURCES` map.
