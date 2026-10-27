#!/usr/bin/env python3
"""Gap Finder: which daily.dev tags in a niche have more reader engagement than publishers.

For reviewers:
  * Every request is a GET to https://api.daily.dev/public/v1, and redirects are never
    followed. All network access is in Api.get.
  * The token comes from DAILY_DEV_TOKEN or ~/.config/gap-finder/.env and is never printed.
    `setup` creates that file with an empty value; the user pastes the token in themselves.
  * Output is a short JSON summary, never raw API responses. Between phases the run is
    cached as compact posts in the system temp folder. The token is never cached.

Phases (the agent drops off-topic tags between them):
  gapfinder.py setup
  gapfinder.py start --seeds houdini,vex,procedural [--own simstim] [--control react]
                     [--window 30] [--max-tags 20] [--sources]
  gapfinder.py neighbours --run RUN --tags houdini,vex,procedural
  gapfinder.py score --run RUN --tags 3d,vfx

Exit codes: 0 done, 1 error, 2 setup needed, 3 rate limited (run the same command again).
Standard library only, Python 3.8+.
"""

import argparse
import json
import math
import os
import re
import secrets
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://api.daily.dev/public/v1"
ENV_PATH = Path.home() / ".config" / "gap-finder" / ".env"
RUN_DIR = Path(tempfile.gettempdir()) / "gap-finder"

PAGE_SIZE = 50
EXTRA_PAGES_PER_TAG = 2
EXTRA_PAGES_PER_RUN = 10
BUSY_MATURE_POSTS = 10       # a tag gets extra pages while it has fewer mature in-window posts
MIN_POSTS = 5                # fewer in-window posts: Too quiet; fewer mature ones: young sample
MATURE_AGE = timedelta(hours=48)
EARLIEST_DATE = datetime(2018, 1, 1, tzinfo=timezone.utc)   # a createdAt before daily.dev existed is bad data
LATE_GRACE = timedelta(days=1)                              # later phases see posts newer than the run's start
RESHARE_AGE = timedelta(days=365)   # published this long before it reached daily.dev: an old article circulating again
MIN_NEIGHBOUR_POSTS = 3
MIN_BASELINE_TAGS = 3
SHRINK_K = 10                # each tag's reaction share is pulled toward the niche share by this many posts
B_SUBSAMPLE = 20             # B is the distinct sources expected among this many of a tag's posts
UNDER_SERVED_B = 5           # in 20 posts, 5 equally active sources show about 4.9
SATURATED_B = 11             # and 15 show about 11
CLOSE_MATCHES = 3
CO_TAGS = 5                  # per seed tag, for the agent's check that it means what the user meant
SEED_TITLES = 3
TOP_CARDS = 5
MAX_WAIT = 120               # longer rate-limit waits are reported, not slept through
WINDOWS = (7, 30, 365)
WEEK = 7 * 24 * 3600

EXIT_OK, EXIT_ERROR, EXIT_SETUP, EXIT_RATE_LIMITED = 0, 1, 2, 3


class SetupNeeded(Exception):
    """No token, or the API rejected it."""


class RateLimited(Exception):
    def __init__(self, wait, body):
        super().__init__("rate limited")
        self.wait = wait
        self.body = body


class UsageError(Exception):
    pass


# ---------- token ----------

def load_token():
    """The token from the environment, else from ENV_PATH."""
    token = os.environ.get("DAILY_DEV_TOKEN", "").strip()
    if not token and ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "DAILY_DEV_TOKEN":
                token = value.strip().strip("'\"")
    if not token:
        raise SetupNeeded()
    return token


def setup_info():
    return {
        "setup": True,
        "env_path": str(ENV_PATH),
        "steps": [
            "Create a personal access token named 'Gap Finder' at https://daily.dev/settings/api, "
            "with a 30- or 90-day expiry.",
            "Open %s and paste the token after DAILY_DEV_TOKEN= yourself. "
            "Never paste it into the chat." % ENV_PATH,
            "Run Gap Finder again.",
        ],
    }


def cmd_setup():
    """Create the .env placeholder if it doesn't exist. Never reads or overwrites it."""
    created = not ENV_PATH.exists()
    if created:
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        ENV_PATH.write_text("# Gap Finder: paste your daily.dev token at the end of the next line.\n"
                            "DAILY_DEV_TOKEN=\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(ENV_PATH, 0o600)
    return dict(setup_info(), created=created)


# ---------- network: GET only, api.daily.dev only ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the token is only ever sent to BASE_URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Api:
    """All network access. There is no method other than GET."""

    def __init__(self, token, state):
        self._token = token
        self.state = state

    def get(self, path, params=None):
        url = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in (1, 2):
            self._pace()
            request = urllib.request.Request(url, method="GET", headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/json",
                "User-Agent": "gap-finder/1.0",
            })
            self.state["calls"] += 1
            try:
                with _opener.open(request, timeout=30) as response:
                    self._note_limits(response.headers)
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                self._note_limits(err.headers)
                body = _read_json(err)
                err.close()
                if err.code == 401:
                    raise SetupNeeded()
                if err.code == 404:
                    return {"data": []}
                if err.code != 429:
                    raise
                wait = _retry_after(err.headers, body)
                if attempt == 2 or wait > MAX_WAIT:
                    raise RateLimited(wait, body)
                time.sleep(wait)

    def _pace(self):
        """Sleep until the reset when the rate-limit headers say only 2 calls remain."""
        limits = self.state["rate_limit"]
        if limits.get("remaining") is not None and limits["remaining"] <= 2 and limits.get("reset") is not None:
            time.sleep(min(_seconds_until(limits["reset"]), 65))
            limits["remaining"] = None

    def _note_limits(self, headers):
        if headers is None:
            return
        for key in ("limit", "remaining", "reset"):
            value = headers.get("x-ratelimit-" + key)
            try:
                self.state["rate_limit"][key] = float(value)
            except (TypeError, ValueError):
                pass


def _read_json(err):
    try:
        data = json.loads(err.read().decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _retry_after(headers, body):
    for value in ((headers or {}).get("retry-after"), body.get("retryAfter")):
        try:
            return max(float(value), 1.0)
        except (TypeError, ValueError):
            continue
    return 60.0


def _seconds_until(reset):
    """x-ratelimit-reset may be seconds to wait, or an epoch time in seconds or milliseconds."""
    value = float(reset)
    if value > 1e12:
        value /= 1000
    if value > 1e9:
        value -= time.time()
    return max(value, 1.0)


# ---------- run state ----------

def normalise(tag):
    return re.sub(r"\s+", "-", tag.strip().lower())


def new_state(seeds, own=None, controls=(), window=30, max_tags=20, sources=False, now=None):
    return {
        "run": secrets.token_hex(6),
        "now": (now or datetime.now(timezone.utc)).isoformat(),
        "seeds": list(seeds),
        "own": (own or "").strip().lower().lstrip("@") or None,
        "controls": _unique(normalise(c) for c in controls),
        "window": window,
        "max_tags": max_tags,
        "want_sources": sources,
        "calls": 0,
        "extra_pages": 0,
        "rate_limit": {},
        "seed_candidates": None,
        "seed_tags": None,
        "neighbour_candidates": None,
        "samples": {},
        "discussed": {},
        "sources_found": [],
        "errors": {},
    }


def run_path(run_id):
    if not re.fullmatch(r"[0-9a-f]{12}", run_id or ""):
        raise UsageError("not a run id: %s" % run_id)
    return RUN_DIR / (run_id + ".json")


def load_state(run_id):
    path = run_path(run_id)
    if not path.is_file():
        raise UsageError("no such run: %s (start a new run)" % run_id)
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = run_path(state["run"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _clock(state):
    """(now, window start, maturity cutoff), fixed when the run started."""
    now = datetime.fromisoformat(state["now"])
    return now, now - timedelta(days=state["window"]), now - MATURE_AGE


# ---------- posts ----------

def compact(post, now):
    """The fields Gap Finder uses. Everything else in a post is dropped."""
    source = post.get("source") or {}
    published = parse_date(post.get("publishedAt"))
    return {
        "id": post.get("id"),
        "title": post.get("title"),
        "url": post.get("url"),
        "permalink": post.get("commentsPermalink"),
        "type": post.get("type"),
        "date": _created_date(post, now),
        "published": published.isoformat() if published else None,     # shown on the cards, never measured
        "up": post.get("numUpvotes") or 0,
        "com": post.get("numComments") or 0,
        "source_id": source.get("id"),
        "source_handle": source.get("handle"),
        "source_name": source.get("name") or source.get("handle") or source.get("id"),
        "tags": post.get("tags") or [],
    }


def parse_date(value):
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    for candidate in (text, re.sub(r"\.\d+", "", text)):
        try:
            date = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    return None


def _created_date(post, now):
    """createdAt, when the post reached daily.dev, as an ISO date. It's the feed's clock, so it's the one date for
    the window, maturity, velocity and page order; publishedAt is the open web's. A createdAt before daily.dev
    existed or after the run is bad data, so the post is undated (None)."""
    date = parse_date(post.get("createdAt"))
    if date is not None and EARLIEST_DATE <= date <= now + LATE_GRACE:
        return date.isoformat()
    return None


def _reshare(post):
    """Whether the article was published more than a year before it reached daily.dev."""
    published, created = parse_date(post["published"]), parse_date(post["date"])
    return published is not None and created is not None and created - published > RESHARE_AGE


def _dated(posts):
    """(post, date) for posts with a usable date."""
    pairs = ((p, parse_date(p["date"])) for p in posts)
    return [(p, d) for p, d in pairs if d is not None]


def _in_window(posts, state):
    """(post, date) for posts in the window. A post with no usable date stays in, with date None: it's on the
    tag's feed, so it counts toward n, B and C, but it's left out of everything that needs a date."""
    _, start, _ = _clock(state)
    pairs = ((p, parse_date(p["date"])) for p in posts)
    return [(p, d) for p, d in pairs if d is None or d >= start]


def _is_own(post, state):
    own = state["own"]
    return bool(own) and own in ((post["source_handle"] or "").lower(), (post["source_id"] or "").lower())


def _newest_first(dates):
    """Whether a page's dates, in feed order, run newest first. None when fewer than two are known."""
    dates = [d for d in dates if d is not None]
    if len(dates) < 2:
        return None
    return all(a >= b for a, b in zip(dates, dates[1:]))


def _second_oldest(page):
    """The page's second-oldest date, so one stray date can't decide coverage on its own."""
    dates = sorted(d for _, d in _dated(page))
    return dates[1].isoformat() if len(dates) >= 2 else None


def _middle(dates):
    """The middle 80% of the dates, oldest first: the oldest and newest tenth are dropped."""
    dates = sorted(dates)
    cut = len(dates) // 10
    return dates[cut:len(dates) - cut]


def _span_weeks(dates):
    return (dates[-1] - dates[0]).total_seconds() / WEEK if dates else 0.0


def _covered(sample, state):
    """Whether the sample reaches the window start. The feed isn't date-ordered, so a page's dates say so
    only when that page came back newest first; otherwise only the feed's last page does."""
    _, start, _ = _clock(state)
    if not sample["has_next"]:
        return True
    return any(ordered and reach is not None and parse_date(reach) < start
               for ordered, reach in zip(sample["newest_first"], sample["second_oldest"]))


def _engagement(post):
    return post["up"] + 2 * post["com"]


# ---------- sampling ----------

def sample_tag(api, state, tag):
    """Fetch page 1 of a tag's feed, unless the run cache has it. Extra pages come from fetch_extra_pages."""
    sample = state["samples"].setdefault(tag, {
        "posts": [], "pages": 0, "cursor": None, "has_next": True, "newest_first": [], "second_oldest": [],
        "published_newest_first": [], "error": None})
    if sample["pages"] == 0 and sample["error"] is None:
        _fetch_page(api, state, tag, sample)
    return sample


def fetch_extra_pages(api, state, tags):
    """Spend the run's extra pages on busy tags, the shortest date span first. Those post the most a day, so
    their samples cover the least of the window; quiet tags on mixed pages wait behind them. The order is
    worked out again after each page, since a page widens its tag's span."""
    while True:
        busy = [t for t in tags if _needs_more(state["samples"][t], state)]
        if not busy:
            return
        if state["extra_pages"] >= EXTRA_PAGES_PER_RUN:
            state["errors"]["extra_page_cap"] = busy
            return
        tag = min(busy, key=lambda t: _span_weeks(_middle(d for _, d in _dated(state["samples"][t]["posts"]))))
        if _fetch_page(api, state, tag, state["samples"][tag]):
            state["extra_pages"] += 1


def _fetch_page(api, state, tag, sample):
    """Add the sample's next page. False when the request failed; the error stays on the sample."""
    params = {"limit": PAGE_SIZE}
    if sample["cursor"]:
        params["cursor"] = sample["cursor"]
    try:
        response = api.get("/feeds/tag/" + urllib.parse.quote(tag, safe=""), params)
    except (OSError, ValueError) as err:
        sample["error"] = str(err)
        return False
    now, _, _ = _clock(state)
    page = [compact(p, now) for p in response.get("data") or []]
    sample["pages"] += 1
    sample["newest_first"].append(_newest_first(parse_date(p["date"]) for p in page))
    sample["published_newest_first"].append(_newest_first(parse_date(p["published"]) for p in page))  # notes only
    sample["second_oldest"].append(_second_oldest(page))
    known = {p["id"] for p in sample["posts"]}
    sample["posts"].extend(p for p in page if p["id"] not in known)
    pagination = response.get("pagination") or {}
    sample["cursor"] = pagination.get("cursor")
    sample["has_next"] = bool(pagination.get("hasNextPage") and sample["cursor"])
    return True


def _needs_more(sample, state):
    """A busy tag: it has extra pages left, its sample doesn't cover the window, and it's short of mature
    in-window posts."""
    if sample["error"] is not None or sample["pages"] > EXTRA_PAGES_PER_TAG or _covered(sample, state):
        return False
    _, start, mature_cutoff = _clock(state)
    dated = _dated(sample["posts"])
    mature = sum(1 for p, d in dated if start <= d <= mature_cutoff and not _is_own(p, state))
    return bool(dated) and mature < BUSY_MATURE_POSTS


# ---------- metrics and scoring ----------

def _reacted(post):
    """Any upvote or comment. Most daily.dev posts get neither, so a median of counts is 0."""
    return post["up"] > 0 or post["com"] > 0


def rarefied_sources(counts, m):
    """Expected distinct sources in a random subsample of m posts (rarefaction), from each source's post
    count: the sum over sources of the chance the subsample holds at least one of its posts. Exact, with no
    random sampling. With m at or above the number of posts, it's the plain count."""
    total = sum(counts)
    m = min(m, total)
    if m == 0:
        return 0.0
    every = math.comb(total, m)
    return sum(1 - math.comb(total - c, m) / every for c in counts)


def tag_metrics(tag, sample, state):
    """One tag's numbers from the spec's metrics table. Own-source posts are set aside first."""
    now, _, mature_cutoff = _clock(state)
    in_window = _in_window(sample["posts"], state)
    posts = [(p, d) for p, d in in_window if not _is_own(p, state)]
    n = len(posts)
    mature = [p for p, d in posts if d is not None and d <= mature_cutoff]
    young = n >= MIN_POSTS and len(mature) < MIN_POSTS
    scored = [p for p, _ in posts] if young else mature
    reacted = sum(1 for p in scored if _reacted(p))
    sources = Counter(p["source_id"] for p, _ in posts)
    names = {p["source_id"]: p["source_name"] for p, _ in posts}
    top_id, top_count = sources.most_common(1)[0] if sources else (None, 0)
    dates = sorted(d for _, d in posts if d is not None)
    covered = _covered(sample, state)
    continuous = bool(sample["newest_first"]) and all(sample["newest_first"]) and len(dates) >= 2
    if covered:
        velocity = n / (state["window"] / 7)
    elif continuous:                     # every post since the oldest sampled one: the posts after it, over
        velocity = (len(dates) - 1) / max((now - dates[0]).total_seconds() / WEEK, 1 / 7)   # the time to the run
    else:                                # mixed pages, a floor: the posts in the middle 80% of dates, over their span
        middle = _middle(dates)
        velocity = len(middle) / max(_span_weeks(middle), 1 / 7)
    return {
        "tag": tag,
        "control": tag in state["controls"],
        "n": n,
        "n_floor": not covered,
        "young": young,
        "reacted": reacted,
        "scored": len(scored),
        "share": reacted / len(scored) if scored else None,
        "B": rarefied_sources(list(sources.values()), B_SUBSAMPLE),
        "sources_seen": len(sources),
        "C": top_count / n if n else 0.0,  # also the top source's expected share of a B_SUBSAMPLE subsample
        "top_source": names.get(top_id),
        "V": velocity,
        "V_floor": not covered and not continuous,
        "own_posts": len(in_window) - n,
        "error": sample.get("error"),
    }


def verdict(row, thin):
    """The spec's verdicts, checked in order."""
    if row["n"] < MIN_POSTS:
        return "Too quiet"
    if thin:
        return "Withheld"
    if row["C"] >= 0.5:                  # owned, whether readers love it or ignore it; vs_niche says which
        return "Single publisher"
    ratio = row["ratio"]
    if ratio >= 1.0 and row["B"] <= UNDER_SERVED_B:
        return "Under-served"
    if (row["B"] >= SATURATED_B or row["V"] >= 20) and ratio < 1.0:
        return "Saturated"
    return "Normal"


def score(rows):
    """Add D, D/D0, O, verdict and rank to each row. Returns (rows in rank order, D0, thin).
    D0 is the niche share. D shrinks each tag's share toward it, so a 5-post tag stays near the niche
    and a 200-post tag keeps its own rate. When the baseline is thin, D is the raw share."""
    baseline = [r["share"] for r in rows
                if r["n"] >= MIN_POSTS and not r["young"] and not r["control"] and r["share"] is not None]
    thin = len(baseline) < MIN_BASELINE_TAGS or statistics.median(baseline) == 0
    d0 = None if thin else statistics.median(baseline)
    for r in rows:
        r["D"] = r["share"] if thin else (r["reacted"] + SHRINK_K * d0) / (r["scored"] + SHRINK_K)
        r["ratio"] = None if thin else r["D"] / d0
        r["O"] = r["ratio"] / math.log2(2 + r["B"]) if r["ratio"] is not None else None
        r["verdict"] = verdict(r, thin)
    ranked = [r for r in rows if r["verdict"] != "Too quiet"]
    ranked.sort(key=lambda r: (r["O"] if r["O"] is not None else -1.0, r["D"] or 0), reverse=True)
    quiet = [r for r in rows if r["verdict"] == "Too quiet"]
    for rank, r in enumerate(ranked, 1):
        r["rank"] = rank
    for r in quiet:
        r["rank"] = None
    return ranked + quiet, d0, thin


# ---------- phases ----------

def run_start(api, state):
    """Step 1: expand each seed into its tag and close matches, for the agent's first check."""
    candidates = []
    for seed in state["seeds"]:
        tag = normalise(seed)
        try:
            names = [t.get("name") for t in api.get("/search/tags", {"q": tag}).get("data") or []]
        except (OSError, ValueError) as err:
            state["errors"]["search:" + tag] = str(err)
            names = []
        close = [n for n in names if n and n != tag and (tag in n or n in tag)][:CLOSE_MATCHES]
        candidates.append({"seed": seed, "tag": tag, "listed_by_tag_search": tag in names, "close": close})
    if state["want_sources"]:
        for seed in state["seeds"]:
            try:
                found = api.get("/search/sources", {"q": seed, "limit": 20}).get("data") or []
            except (OSError, ValueError) as err:
                state["errors"]["sources:" + seed] = str(err)
                continue
            known = {s["id"] for s in state["sources_found"]}
            state["sources_found"] += [{"id": s.get("id"), "handle": s.get("handle"), "name": s.get("name")}
                                       for s in found if s.get("id") not in known]
    state["seed_candidates"] = candidates
    return {
        "run": state["run"],
        "phase": "start",
        "seed_tags": candidates,
        "next": "Keep every seed's own tag, even when listed_by_tag_search is false: tag search misses tags "
                "that have posts. Drop close matches that are a different topic, then run: "
                "neighbours --run %s --tags <kept tags>" % state["run"],
    }


def run_neighbours(api, state, tags):
    """Steps 2-3: sample the seed tags the agent kept, then list neighbour candidates for check 2."""
    tags = _unique(normalise(t) for t in tags)
    if not tags:
        raise UsageError("pass the kept seed tags with --tags")
    state["seed_tags"] = tags[:state["max_tags"]]
    for tag in state["seed_tags"]:
        sample_tag(api, state, tag)
    counts, seen = Counter(), set()
    for tag in state["seed_tags"]:
        for p in state["samples"][tag]["posts"]:
            if p["id"] not in seen:
                seen.add(p["id"])
                counts.update(set(p["tags"]))
    skip = set(state["seed_tags"]) | set(state["controls"])
    slots = state["max_tags"] - len(state["seed_tags"])
    limit = max(2 * slots, 10) if slots > 0 else 0
    candidates = [{"tag": t, "posts": c} for t, c in counts.most_common()
                  if t not in skip and c >= MIN_NEIGHBOUR_POSTS][:limit]
    state["neighbour_candidates"] = candidates
    return {
        "run": state["run"],
        "phase": "neighbours",
        "seed_tags": [_seed_summary(t, state) for t in state["seed_tags"]],
        "free_slots": slots,
        "candidates": candidates,
        "next": "If a seed tag's co_tags and titles show a different topic, run neighbours again without it "
                "(its pages are cached, so no new calls). Then drop generic or off-topic candidates, keep the "
                "ones this niche's readers follow, best first, and run: score --run %s --tags <kept tags>"
                % state["run"],
    }


def _seed_summary(tag, state):
    """What the agent needs to spot a seed tag that names a different topic, such as vex the security standard."""
    sample = state["samples"][tag]
    posts = [p for p in sample["posts"] if not _is_own(p, state)]
    co_tags = Counter(t for p in posts for t in p["tags"] if t != tag)
    best = sorted(posts, key=_engagement, reverse=True)[:SEED_TITLES]
    return {
        "tag": tag,
        "in_window": tag_metrics(tag, sample, state)["n"],
        "co_tags": [t for t, _ in co_tags.most_common(CO_TAGS)],
        "titles": [p["title"] for p in best],
    }


def run_score(api, state, tags):
    """Steps 4-7: sample neighbours and controls, score, then build the top-five cards."""
    if state.get("seed_tags") is None:
        raise UsageError("run the neighbours phase first")
    slots = max(state["max_tags"] - len(state["seed_tags"]), 0)
    wanted = [t for t in _unique(normalise(t) for t in tags)
              if t not in state["seed_tags"] and t not in state["controls"]]
    neighbours, dropped = wanted[:slots], wanted[slots:]
    all_tags = _unique(state["seed_tags"] + neighbours + state["controls"])
    for tag in all_tags:
        sample_tag(api, state, tag)
    fetch_extra_pages(api, state, all_tags)
    rows, d0, thin = score([tag_metrics(t, state["samples"][t], state) for t in all_tags])
    cards = []
    for row in [r for r in rows if r["rank"] is not None and not r["control"]][:TOP_CARDS]:
        discussed = _discussed(api, state, row["tag"])
        if discussed is not None:
            row["H"] = sum(1 for p in discussed if not _is_own(p, state))
        cards.append(_card(row["tag"], state["samples"][row["tag"]], discussed or [], state))
    return {
        "run": state["run"],
        "phase": "score",
        "table": [_table_row(r) for r in rows],
        "cards": cards,
        "rare_publishers": _rare_publishers(state, all_tags),
        "notes": _notes(state, rows, d0, thin, dropped),
    }


def _discussed(api, state, tag):
    if tag not in state["discussed"]:
        params = {"tag": tag, "period": state["window"], "limit": PAGE_SIZE}
        now, _, _ = _clock(state)
        try:
            state["discussed"][tag] = [compact(p, now) for p in api.get("/feeds/discussed", params).get("data") or []]
        except (OSError, ValueError) as err:
            state["errors"]["discussed:" + tag] = str(err)
            return None
    return state["discussed"][tag]


def _card(tag, sample, discussed, state):
    in_window = [p for p, _ in _in_window(sample["posts"], state) if not _is_own(p, state)]
    pool = {}
    for p, _ in _in_window(sample["posts"] + discussed, state):
        if not _is_own(p, state):
            pool.setdefault(p["id"], p)
    best = sorted(pool.values(), key=_engagement, reverse=True)[:3]
    sources = Counter(p["source_name"] for p in in_window)
    return {
        "tag": tag,
        "posts": [{"title": p["title"], "source": p["source_name"], "upvotes": p["up"],
                   "comments": p["com"], "url": p["url"], "discussion": p["permalink"],
                   "published": (p["published"] or "")[:10] or None, "reshare": _reshare(p)} for p in best],
        "sources": [{"name": name, "posts": count} for name, count in sources.most_common(8)],
    }


def _rare_publishers(state, tags):
    """Searched sources that post little or nothing in this run's samples."""
    if not state["sources_found"]:
        return []
    counts = Counter()
    for tag in tags:
        for p, _ in _in_window(state["samples"][tag]["posts"], state):
            counts[p["source_id"]] += 1
    rare = [dict(s, posts_in_sample=counts[s["id"]]) for s in state["sources_found"] if counts[s["id"]] <= 2]
    return sorted(rare, key=lambda s: s["posts_in_sample"])[:10]


def _table_row(r):
    return {
        "rank": r["rank"], "tag": r["tag"], "verdict": r["verdict"], "control": r["control"],
        "vs_niche": None if r["ratio"] is None else ("above" if r["ratio"] >= 1.0 else "below"),
        "O": _round(r["O"]), "D_ratio": _round(r["ratio"]), "D": _round(r["D"]),
        "share": _round(r["share"]), "scored": r["scored"],
        "n": r["n"], "n_floor": r["n_floor"], "B": _round(r["B"]), "sources_seen": r["sources_seen"],
        "top_source": r["top_source"], "top_share": _round(r["C"]), "V_per_week": _round(r["V"]),
        "V_floor": r["V_floor"],
        "H": r.get("H"), "own_posts": r["own_posts"], "young": r["young"], "error": r["error"],
    }


def _notes(state, rows, d0, thin, dropped):
    order = {field: [f for s in state["samples"].values() for f in s[key] if f is not None]
             for field, key in (("createdAt", "newest_first"), ("publishedAt", "published_newest_first"))}
    samples = {r["tag"]: state["samples"][r["tag"]] for r in rows}
    floors = [r["tag"] for r in rows if r["n_floor"] and r["n"]]
    v_floors = [r["tag"] for r in rows if r["V_floor"] and r["n"]]
    return {
        "window_days": state["window"],
        "measured_at": state["now"],
        "calls": state["calls"],
        "extra_pages": state["extra_pages"],
        "extra_pages_by_tag": {t: s["pages"] - 1 for t, s in samples.items() if s["pages"] > 1},
        "undated_posts": _count_posts(samples, lambda p: p["date"] is None),
        "niche_share": _round(d0),
        "B_subsample": B_SUBSAMPLE,
        "baseline_thin": thin,
        "seeds_dropped": _unique(c["tag"] for c in state["seed_candidates"] or []
                                 if c["tag"] not in state["seed_tags"]),
        "feed_pages_checked": {field: len(flags) for field, flags in order.items()},
        "feed_pages_newest_first": {field: sum(flags) for field, flags in order.items()},
        "young_samples": [r["tag"] for r in rows if r["young"]],
        "n_is_floor": floors,
        "V_is_floor": v_floors,
        "empty_or_errored": [r["tag"] for r in rows if r["n"] == 0 or r["error"]],
        "errors": state["errors"],
        "rate_limit": state["rate_limit"],
        "own_source": state["own"],
        "neighbours_dropped_over_max": dropped,
        "caveats": [
            "Numbers come from a sample of at most 150 posts per tag, not platform totals.",
            "Tag feeds may leave out posts you've already clicked or seen, and tags or sources you've blocked. "
            "This hasn't been checked yet. If they do, the tags you read most are undercounted.",
            "Views aren't exposed, so a tag with many silent readers looks weaker than it is.",
        ] + ([
            "n is a floor for %s: those samples stopped before the window start, at different depths (see "
            "extra_pages_by_tag), so one \"at least\" can't be compared directly with another. n decides only the "
            "Too quiet check. B is counted per %d posts, so it doesn't depend on depth." % (", ".join(floors), B_SUBSAMPLE)
        ] if floors else []) + ([
            "V is a floor for %s: their pages weren't newest first, so the rate comes from the middle 80%% of "
            "their dates, and the posts between those dates may not all have been fetched." % ", ".join(v_floors)
        ] if v_floors else []),
    }


def _count_posts(samples, test):
    """Per tag, the fetched posts that pass the test; tags with none are left out."""
    counts = {t: sum(1 for p in s["posts"] if test(p)) for t, s in samples.items()}
    return {t: c for t, c in counts.items() if c}


def _round(value):
    return None if value is None else round(value, 2)


def _unique(items):
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _split(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


# ---------- command line ----------

class _Parser(argparse.ArgumentParser):
    def error(self, message):            # exit code 2 means "setup needed", so usage errors raise
        raise UsageError(message)


def _parse_args(argv):
    parser = _Parser(prog="gapfinder.py", description="Find under-served daily.dev tags in a niche.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="create the .env placeholder for the token")
    start = sub.add_parser("start", help="phase 1: expand seeds into tags")
    start.add_argument("--seeds", required=True, help="comma-separated keywords or tags")
    start.add_argument("--own", help="your own source handle, excluded from every metric")
    start.add_argument("--control", default="", help="comma-separated control tags")
    start.add_argument("--window", type=int, choices=WINDOWS, default=30, help="look-back window in days")
    start.add_argument("--max-tags", type=int, default=20, help="tags to score, controls not counted")
    start.add_argument("--sources", action="store_true", help="also search sources per seed")
    for name, text in (("neighbours", "phase 2: sample seed tags, list neighbours"),
                       ("score", "phase 3: sample neighbours, score, build cards")):
        phase = sub.add_parser(name, help=text)
        phase.add_argument("--run", required=True)
        phase.add_argument("--tags", default="")
    args = parser.parse_args(argv)
    if args.command == "start":
        if not _split(args.seeds):
            raise UsageError("--seeds needs at least one seed")
        if not 1 <= args.max_tags <= 50:
            raise UsageError("--max-tags must be between 1 and 50")
    return args


def _emit(result, code):
    print(json.dumps(result))
    return code


def main(argv=None, make_api=None):
    token, state = None, None
    try:
        args = _parse_args(argv)
        if args.command == "setup":
            return _emit(cmd_setup(), EXIT_OK)
        token = load_token()
        if args.command == "start":
            state = new_state(_split(args.seeds), args.own, _split(args.control),
                              args.window, args.max_tags, args.sources)
        else:
            state = load_state(args.run)
        api = (make_api or Api)(token, state)
        if args.command == "start":
            return _emit(run_start(api, state), EXIT_OK)
        if args.command == "neighbours":
            return _emit(run_neighbours(api, state, _split(args.tags)), EXIT_OK)
        return _emit(run_score(api, state, _split(args.tags)), EXIT_OK)
    except SetupNeeded:
        return _emit(setup_info(), EXIT_SETUP)
    except RateLimited as err:
        return _emit({"partial": True, "run": state["run"] if state else None,
                      "retry_after_seconds": round(err.wait),
                      "message": "Rate limited. Progress is saved; run the same command again after the wait."},
                     EXIT_RATE_LIMITED)
    except UsageError as err:
        return _emit({"error": str(err)}, EXIT_ERROR)
    except Exception as err:
        message = "%s: %s" % (type(err).__name__, err)
        return _emit({"error": message.replace(token, "<hidden>") if token else message}, EXIT_ERROR)
    finally:
        if state is not None:
            save_state(state)


if __name__ == "__main__":
    sys.exit(main())
