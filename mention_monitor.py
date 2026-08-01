#!/usr/bin/env python3
"""
mention_monitor.py -- a self-hosted, zero-dependency replacement for Syften.

Polls free public APIs (Hacker News, Reddit, Lobste.rs) for keyword mentions,
de-duplicates against a local seen.json, prints a digest to stdout and
optionally POSTs it to a Slack and/or Discord webhook.

Standard library only. Python 3.8+.

Usage:
    python3 mention_monitor.py
    python3 mention_monitor.py --config config.json --state seen.json
    python3 mention_monitor.py --seed          # prime state, report nothing
    python3 mention_monitor.py --dry-run       # don't write state, don't post
    python3 mention_monitor.py --hours 72      # override lookback window

Env vars (both optional):
    SLACK_WEBHOOK_URL
    DISCORD_WEBHOOK_URL
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape

# Matched titles/text come from the live internet and can contain arbitrary
# Unicode (emoji, etc). Force UTF-8 on stdout/stderr so a run doesn't crash
# on Windows, where the console defaults to a legacy codepage (e.g. cp1252)
# that can't encode it.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

VERSION = "1.0.0"

DEFAULT_USER_AGENT = (
    "mention-monitor/%s (self-hosted keyword monitor; "
    "+https://github.com/yourname/mention-monitor)" % VERSION
)

DEFAULT_CONFIG = {
    "user_agent": DEFAULT_USER_AGENT,
    "lookback_hours": 24,
    "request_delay_seconds": 2.0,
    "reddit_delay_seconds": 6.0,
    "timeout_seconds": 25,
    "max_retries": 3,
    "max_seen_ids": 5000,
    "strict_match": True,
    "sources": {"hackernews": True, "reddit": True, "reddit_rss": True, "lobsters": True},
    "exclude": [],
    "projects": {},
}

# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def log(msg):
    sys.stderr.write("[mention-monitor] %s\n" % msg)
    sys.stderr.flush()


def strip_html(text):
    if not text:
        return ""
    return WS_RE.sub(" ", unescape(TAG_RE.sub(" ", text))).strip()


def truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def utcnow():
    return datetime.now(timezone.utc)


def parse_ts(value):
    """Best-effort parse of the various timestamp formats these APIs emit."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_age(dt):
    if dt is None:
        return "unknown time"
    delta = utcnow() - dt
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return "%dm ago" % mins
    if mins < 60 * 48:
        return "%dh ago" % (mins // 60)
    return "%dd ago" % (mins // 1440)


# --------------------------------------------------------------------------
# HTTP layer -- every call goes through here
# --------------------------------------------------------------------------


class HttpError(Exception):
    pass


def http_get(url, cfg, accept="*/*", extra_headers=None):
    """GET a URL with retries, exponential backoff and 429/Retry-After handling.

    Raises HttpError on final failure. Never raises anything else.
    """
    headers = {
        "User-Agent": cfg["user_agent"],
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)

    attempts = max(1, int(cfg["max_retries"]))
    last = "unknown error"

    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = "HTTP %s" % exc.code
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts:
                wait = 5.0 * (2 ** (attempt - 1))
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after:
                    try:
                        wait = max(wait, min(float(retry_after), 120.0))
                    except (TypeError, ValueError):
                        pass
                log("  %s -- backing off %.0fs (attempt %d/%d)"
                    % (last, wait, attempt, attempts))
                time.sleep(wait)
                continue
            break
        except urllib.error.URLError as exc:
            last = "network error: %s" % (exc.reason,)
        except Exception as exc:  # noqa: BLE001 - nothing may escape
            last = "%s: %s" % (type(exc).__name__, exc)

        if attempt < attempts:
            time.sleep(3.0 * attempt)

    raise HttpError(last)


def http_get_json(url, cfg, **kwargs):
    raw = http_get(url, cfg, accept="application/json", **kwargs)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise HttpError("invalid JSON (%s)" % exc)


# --------------------------------------------------------------------------
# sources -- each returns a list of normalized item dicts, or raises HttpError
# --------------------------------------------------------------------------
# normalized item: {id, source, title, url, author, text, created, context}


def fetch_hackernews(term, cfg, since):
    """Hacker News via the Algolia search API (stories + comments)."""
    query = urllib.parse.urlencode(
        {
            "query": term,
            "tags": "(story,comment)",
            "numericFilters": "created_at_i>=%d" % int(since.timestamp()),
            "hitsPerPage": "50",
        }
    )
    url = "https://hn.algolia.com/api/v1/search_by_date?" + query
    data = http_get_json(url, cfg)

    items = []
    for hit in data.get("hits") or []:
        object_id = hit.get("objectID")
        if not object_id:
            continue
        title = hit.get("title") or hit.get("story_title") or "(comment)"
        body = strip_html(hit.get("comment_text") or hit.get("story_text") or "")
        items.append(
            {
                "id": "hn:%s" % object_id,
                "source": "Hacker News",
                "title": title,
                "url": "https://news.ycombinator.com/item?id=%s" % object_id,
                "author": hit.get("author") or "?",
                "text": body,
                "created": parse_ts(hit.get("created_at_i") or hit.get("created_at")),
                "context": "story" if hit.get("title") else "comment",
                "extra_url": hit.get("url") or hit.get("story_url") or "",
            }
        )
    return items


def _reddit_items_from_json(payload):
    items = []
    for child in (payload.get("data") or {}).get("children") or []:
        post = child.get("data") or {}
        name = post.get("name") or post.get("id")
        if not name:
            continue
        permalink = post.get("permalink") or ""
        items.append(
            {
                "id": "reddit:%s" % name,
                "source": "Reddit",
                "title": post.get("title") or "(untitled)",
                "url": "https://www.reddit.com" + permalink if permalink else (post.get("url") or ""),
                "author": post.get("author") or "?",
                "text": strip_html(post.get("selftext") or ""),
                "created": parse_ts(post.get("created_utc")),
                "context": "r/%s" % (post.get("subreddit") or "?"),
                "extra_url": "",
            }
        )
    return items


def _reddit_items_from_rss(raw):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    items = []
    for entry in root.findall("a:entry", ns):
        entry_id = entry.findtext("a:id", None, ns)
        if not entry_id:
            continue
        # Reddit search also returns subreddit results (t5_*); keep posts only.
        if entry_id.startswith("t5_"):
            continue
        link_el = entry.find("a:link", ns)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        author_el = entry.find("a:author/a:name", ns)
        cat_el = entry.find("a:category", ns)
        body = strip_html(
            entry.findtext("a:content", None, ns)
            or entry.findtext("a:summary", None, ns)
            or ""
        )
        items.append(
            {
                "id": "reddit:%s" % entry_id,
                "source": "Reddit",
                "title": entry.findtext("a:title", "(untitled)", ns),
                "url": link,
                "author": (author_el.text if author_el is not None else "?") or "?",
                "text": body,
                "created": parse_ts(entry.findtext("a:updated", None, ns)),
                "context": ("r/%s" % cat_el.attrib.get("term"))
                if cat_el is not None and cat_el.attrib.get("term")
                else "reddit",
                "extra_url": "",
            }
        )
    return items


def fetch_reddit(term, cfg, since):
    """Reddit public search.

    Primary: /search.json. Reddit blocks this from many datacenter/CI IP
    ranges with HTTP 403 even when the User-Agent is well-formed, so we
    transparently fall back to /search.rss, which is served far more
    permissively. Both are public and key-free.
    """
    params = {"q": term, "sort": "new", "limit": "50", "type": "link"}
    json_url = "https://www.reddit.com/search.json?" + urllib.parse.urlencode(params)
    try:
        return _reddit_items_from_json(http_get_json(json_url, cfg))
    except HttpError as exc:
        log("  reddit JSON failed (%s) -- falling back to RSS" % exc)

    time.sleep(cfg["reddit_delay_seconds"])
    rss_url = "https://www.reddit.com/search.rss?" + urllib.parse.urlencode(params)
    raw = http_get(rss_url, cfg, accept="application/atom+xml, application/xml")
    try:
        return _reddit_items_from_rss(raw)
    except ET.ParseError as exc:
        raise HttpError("could not parse Reddit RSS (%s)" % exc)


def _lobsters_item(story, why="search"):
    short_id = story.get("short_id")
    if not short_id:
        return None
    return {
        "id": "lobsters:%s" % short_id,
        "source": "Lobste.rs",
        "title": story.get("title") or "(untitled)",
        "url": story.get("comments_url")
        or story.get("short_id_url")
        or story.get("url")
        or "",
        "author": (story.get("submitter_user") or {}).get("username")
        if isinstance(story.get("submitter_user"), dict)
        else (story.get("submitter_user") or "?"),
        "text": strip_html(story.get("description_plain") or story.get("description") or ""),
        "created": parse_ts(story.get("created_at")),
        "context": ", ".join(story.get("tags") or []) or why,
        "extra_url": story.get("url") or "",
    }


def fetch_lobsters(term, cfg, since):
    """Lobste.rs.

    The documented `/search?...&format=json` endpoint is tried first, but as of
    this writing Lobste.rs rejects it with HTTP 400 "Unpermitted query or form
    parameter" -- its search has no JSON representation at all. We therefore
    fall back to /newest.json (a real, supported JSON endpoint) and filter it
    locally for the term. That covers recent submissions only, which is the
    honest limit of what Lobste.rs exposes without scraping HTML.
    """
    search_url = "https://lobste.rs/search?" + urllib.parse.urlencode(
        {"q": term, "what": "stories", "order": "newest", "format": "json"}
    )
    try:
        data = http_get_json(search_url, cfg)
        stories = data if isinstance(data, list) else (data.get("stories") or [])
        items = [_lobsters_item(s) for s in stories]
        return [i for i in items if i]
    except HttpError as exc:
        log("  lobsters search JSON unavailable (%s) -- using /newest.json" % exc)

    time.sleep(cfg["request_delay_seconds"])
    data = http_get_json("https://lobste.rs/newest.json", cfg)
    if not isinstance(data, list):
        raise HttpError("unexpected /newest.json payload")

    needle = term.lower()
    items = []
    for story in data:
        haystack = " ".join(
            [
                str(story.get("title") or ""),
                strip_html(story.get("description_plain") or story.get("description") or ""),
                str(story.get("url") or ""),
                " ".join(story.get("tags") or []),
            ]
        ).lower()
        if needle in haystack:
            item = _lobsters_item(story, why="newest")
            if item:
                items.append(item)
    return items


# Reddit via RSS is scoped to a fixed subreddit list for the RAG use case
# only, and only fed rag-post-processor keyword groups (not all 33 terms) -
# querying every term against every sub is 165 requests and gets
# rate-limited. See README for why this is a bridge, not a foundation.
REDDIT_RSS_SUBREDDITS = ["LocalLLaMA", "Rag", "LangChain", "vectordatabase", "MachineLearning"]
REDDIT_RSS_PROJECT_PREFIX = "rag-post-processor"


def _reddit_rss_items_from_xml(raw, subreddit):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise HttpError("could not parse RSS (%s)" % exc)
    items = []
    for entry in root.findall("a:entry", ns):
        entry_id = entry.findtext("a:id", None, ns)
        if not entry_id:
            continue
        link_el = entry.find("a:link", ns)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        author_el = entry.find("a:author/a:name", ns)
        body = strip_html(
            entry.findtext("a:content", None, ns)
            or entry.findtext("a:summary", None, ns)
            or ""
        )
        items.append(
            {
                "id": "rdt:%s" % entry_id,
                "source": "Reddit (RSS)",
                "title": entry.findtext("a:title", "(untitled)", ns),
                "url": link,
                "author": (author_el.text if author_el is not None else "?") or "?",
                "text": body,
                "created": parse_ts(entry.findtext("a:updated", None, ns)),
                "context": "r/%s" % subreddit,
                "extra_url": "",
            }
        )
    return items


def fetch_reddit_rss(term, cfg, since):
    """Reddit via old.reddit.com's still-unauthenticated RSS search.

    Reddit deprecated unauthenticated .json access on 2026-05-28; .rss still
    serves as of this writing, but Reddit has publicly signalled intent to
    restrict scraping surfaces generally -- treat this as a bridge, not a
    foundation (see README). Scoped to REDDIT_RSS_SUBREDDITS and, at the
    call-site in run(), to rag-post-processor keyword groups only.
    """
    items = []
    errors = []
    for index, sub in enumerate(REDDIT_RSS_SUBREDDITS):
        if index > 0:
            time.sleep(cfg["reddit_delay_seconds"])
        url = "https://old.reddit.com/r/%s/search.rss?%s" % (
            sub,
            urllib.parse.urlencode(
                {"q": term, "restrict_sr": "1", "sort": "new", "t": "week"}
            ),
        )
        try:
            raw = http_get(url, cfg, accept="application/atom+xml, application/xml")
        except HttpError as exc:
            log("  reddit_rss WARNING: r/%s / \"%s\": %s" % (sub, term, exc))
            errors.append("r/%s: %s" % (sub, exc))
            continue
        if not raw or not raw.strip():
            log("  reddit_rss WARNING: r/%s / \"%s\": empty response" % (sub, term))
            errors.append("r/%s: empty response" % sub)
            continue
        try:
            items.extend(_reddit_rss_items_from_xml(raw, sub))
        except HttpError as exc:
            log("  reddit_rss WARNING: r/%s / \"%s\": %s" % (sub, term, exc))
            errors.append("r/%s: %s" % (sub, exc))
            continue

    if not items and errors:
        # Every subreddit failed for this term - surface it as a genuine
        # failure in the digest footer rather than silently reporting zero
        # items, which would read as "no matches" instead of "Reddit
        # rejected every request".
        raise HttpError("; ".join(errors))
    return items


SOURCES = {
    "hackernews": fetch_hackernews,
    "reddit": fetch_reddit,
    "reddit_rss": fetch_reddit_rss,
    "lobsters": fetch_lobsters,
}


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------


def item_haystack(item):
    return " ".join(
        [
            item.get("title") or "",
            item.get("text") or "",
            item.get("url") or "",
            item.get("extra_url") or "",
            item.get("context") or "",
        ]
    ).lower()


def is_excluded(item, excludes):
    hay = item_haystack(item)
    for bad in excludes:
        if bad and bad.lower() in hay:
            return bad
    return None


def matches_term(item, term):
    """Strict local re-check.

    Both HN's Algolia index and Reddit's search OR-match individual words, so a
    query for "agent handoff" happily returns anything containing "agent".
    Requiring the literal phrase locally is what keeps the digest signal-heavy.
    """
    return term.lower() in item_haystack(item)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def load_state(path):
    if not os.path.exists(path):
        return {"ids": [], "last_run": None}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError) as exc:
        log("state file unreadable (%s) -- starting fresh" % exc)
        return {"ids": [], "last_run": None}
    ids = data.get("ids")
    if not isinstance(ids, list):
        ids = []
    return {"ids": [str(i) for i in ids], "last_run": data.get("last_run")}


def save_state(path, ids, cap):
    payload = {
        "ids": ids[-cap:],
        "last_run": utcnow().isoformat(),
        "version": VERSION,
    }
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

SAMPLE_CONFIG = {
    "user_agent": DEFAULT_USER_AGENT,
    "lookback_hours": 24,
    "request_delay_seconds": 2.0,
    "reddit_delay_seconds": 6.0,
    "timeout_seconds": 25,
    "max_retries": 3,
    "max_seen_ids": 5000,
    "strict_match": True,
    "sources": {"hackernews": True, "reddit": True, "reddit_rss": True, "lobsters": True},
    "exclude": [
        "onlyfans",
        "crypto airdrop",
        "promo code",
        "/r/teenagers",
    ],
    "projects": {
        "chatwillow": [
            "chatwillow",
            "chatgpt alternative",
            "free ai chat",
        ],
        "agent-handoff": [
            "agent handoff",
            "agent orchestration",
            "stripe metered billing",
        ],
        "flagcheck": [
            "ats resume",
            "job description red flags",
        ],
    },
}


def write_sample_config(path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(SAMPLE_CONFIG, handle, indent=2)
        handle.write("\n")


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(path):
        log("no config at %s -- writing a sample one" % path)
        write_sample_config(path)
    with open(path, "r", encoding="utf-8") as handle:
        user_cfg = json.load(handle)
    if not isinstance(user_cfg, dict):
        raise SystemExit("config.json must contain a JSON object")
    cfg.update(user_cfg)

    sources = dict(DEFAULT_CONFIG["sources"])
    sources.update(cfg.get("sources") or {})
    cfg["sources"] = sources

    if not isinstance(cfg.get("projects"), dict) or not cfg["projects"]:
        raise SystemExit('config.json needs a non-empty "projects" object')
    cfg["exclude"] = [str(x) for x in (cfg.get("exclude") or [])]
    return cfg


# --------------------------------------------------------------------------
# digest rendering
# --------------------------------------------------------------------------


def render_digest(hits, stats, cfg):
    now = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = []

    if not hits:
        lines.append("Mention monitor -- %s" % now)
        lines.append("No new mentions.")
    else:
        noun = "mention" if len(hits) == 1 else "mentions"
        lines.append("Mention monitor -- %d new %s (%s)" % (len(hits), noun, now))

        by_project = {}
        for hit in hits:
            by_project.setdefault(hit["project"], []).append(hit)

        for project in sorted(by_project):
            group = sorted(
                by_project[project],
                key=lambda h: h.get("created") or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            lines.append("")
            lines.append("=" * 62)
            lines.append("%s  (%d)" % (project.upper(), len(group)))
            lines.append("=" * 62)
            for hit in group:
                lines.append("")
                lines.append("  [%s] %s" % (hit["source"], truncate(hit["title"], 110)))
                lines.append(
                    "    match: %-28s  %s  %s"
                    % (
                        '"%s"' % hit["term"],
                        hit.get("context") or "-",
                        fmt_age(hit.get("created")),
                    )
                )
                if hit.get("author"):
                    lines.append("    by: %s" % hit["author"])
                snippet = truncate(hit.get("text") or "", 260)
                if snippet:
                    lines.append("    %s" % snippet)
                lines.append("    %s" % (hit.get("url") or "(no url)"))

    lines.append("")
    lines.append("-" * 62)
    ok = [s for s in stats if s["ok"]]
    bad = [s for s in stats if not s["ok"]]
    lines.append(
        "%d/%d source-queries succeeded, %d items seen, %d new after dedup."
        % (len(ok), len(stats), sum(s["count"] for s in ok), len(hits))
    )
    for stat in bad:
        lines.append("  ! %s / \"%s\": %s" % (stat["source"], stat["term"], stat["error"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# webhooks
# --------------------------------------------------------------------------


def post_json(url, payload, cfg):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": cfg["user_agent"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as resp:
        return resp.status


def chunk_text(text, limit):
    """Split on line boundaries so we never cut a URL in half."""
    chunks, current = [], ""
    for line in text.split("\n"):
        line = line if len(line) <= limit else line[:limit]
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


def send_webhooks(digest, cfg):
    targets = [
        ("Slack", os.environ.get("SLACK_WEBHOOK_URL"), 3500, lambda t: {"text": "```\n%s\n```" % t}),
        ("Discord", os.environ.get("DISCORD_WEBHOOK_URL"), 1800, lambda t: {"content": "```\n%s\n```" % t}),
    ]
    for name, url, limit, build in targets:
        if not url:
            continue
        try:
            for index, chunk in enumerate(chunk_text(digest, limit)):
                status = post_json(url, build(chunk), cfg)
                log("%s webhook chunk %d -> HTTP %s" % (name, index + 1, status))
                time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001 - a bad webhook must not fail the run
            log("%s webhook failed: %s" % (name, exc))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def run(args):
    cfg = load_config(args.config)
    if args.hours:
        cfg["lookback_hours"] = args.hours
    since = utcnow() - timedelta(hours=float(cfg["lookback_hours"]))

    state = load_state(args.state)
    seen_ids = list(state["ids"])
    seen_set = set(seen_ids)
    first_run = not os.path.exists(args.state)

    enabled = [name for name in SOURCES if cfg["sources"].get(name)]
    pairs = [
        (project, term)
        for project, terms in sorted(cfg["projects"].items())
        for term in terms
    ]
    log(
        "v%s: %d terms x %d sources, lookback %sh, %d ids in state%s"
        % (
            VERSION,
            len(pairs),
            len(enabled),
            cfg["lookback_hours"],
            len(seen_ids),
            " (first run)" if first_run else "",
        )
    )

    hits, stats = [], []
    fresh_ids = []
    dropped_excluded = 0
    dropped_loose = 0
    dropped_old = 0

    for source_name in enabled:
        fetcher = SOURCES[source_name]
        source_pairs = pairs
        if source_name == "reddit_rss":
            # Only the rag-post-processor keyword groups get queried against
            # Reddit - all 33 terms x 5 subs would be 165 requests and get
            # rate-limited (see README).
            source_pairs = [
                (project, term)
                for project, term in pairs
                if project.startswith(REDDIT_RSS_PROJECT_PREFIX)
            ]
        for project, term in source_pairs:
            delay = (
                cfg["reddit_delay_seconds"]
                if source_name in ("reddit", "reddit_rss")
                else cfg["request_delay_seconds"]
            )
            try:
                items = fetcher(term, cfg, since)
                stats.append(
                    {"source": source_name, "term": term, "ok": True,
                     "count": len(items), "error": ""}
                )
            except HttpError as exc:
                log("%s / \"%s\": FAILED (%s)" % (source_name, term, exc))
                stats.append(
                    {"source": source_name, "term": term, "ok": False,
                     "count": 0, "error": str(exc)}
                )
                time.sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001 - belt and braces
                log("%s / \"%s\": UNEXPECTED %s: %s" % (source_name, term, type(exc).__name__, exc))
                stats.append(
                    {"source": source_name, "term": term, "ok": False, "count": 0,
                     "error": "%s: %s" % (type(exc).__name__, exc)}
                )
                time.sleep(delay)
                continue

            for item in items:
                created = item.get("created")
                if created and created < since:
                    dropped_old += 1
                    continue
                if cfg.get("strict_match", True) and not matches_term(item, term):
                    dropped_loose += 1
                    continue
                if is_excluded(item, cfg["exclude"]):
                    dropped_excluded += 1
                    continue
                if item["id"] in seen_set:
                    continue
                seen_set.add(item["id"])
                fresh_ids.append(item["id"])
                item = dict(item)
                item["project"] = project
                item["term"] = term
                hits.append(item)

            time.sleep(delay)

    log(
        "filtered out: %d too old, %d loose-match, %d excluded"
        % (dropped_old, dropped_loose, dropped_excluded)
    )

    if args.seed:
        digest = "Seeded %d ids into %s. No mentions reported." % (
            len(fresh_ids),
            args.state,
        )
    else:
        digest = render_digest(hits, stats, cfg)

    print(digest)

    if args.dry_run:
        log("dry run: state not written, webhooks not called")
        return 0

    save_state(args.state, seen_ids + fresh_ids, int(cfg["max_seen_ids"]))

    if hits and not args.seed and not args.no_post:
        send_webhooks(digest, cfg)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Self-hosted mention monitor.")
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--state", default="seen.json", help="path to seen.json")
    parser.add_argument("--hours", type=float, default=None, help="override lookback hours")
    parser.add_argument("--seed", action="store_true",
                        help="record current matches without reporting them")
    parser.add_argument("--dry-run", action="store_true",
                        help="do not write state and do not post webhooks")
    parser.add_argument("--no-post", action="store_true", help="skip webhooks")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
