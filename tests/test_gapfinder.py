"""Tests for gapfinder.py. Run from the gap-finder folder: python -m unittest -v"""
import contextlib
import email.message
import http.server
import io
import json
import math
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gapfinder as gf  # noqa: E402

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
LISP_1980 = "1980-08-28T05:59:38.000Z"   # ai's oldest post in the second live run: an HN share of a 1980 article
PLACEHOLDER = "1970-01-01T00:00:00.000Z"  # what a missing date looks like when it comes through as zero
_next_id = iter(range(1, 10**9))


class FrozenClock(datetime):
    """The script's clock, pinned to NOW: fixture dates are relative to NOW, so the real clock ages them."""

    @classmethod
    def now(cls, tz=None):
        return NOW


# ---------- fixtures ----------

def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def post(days_ago, source="Some Source", up=0, com=0, tags=("t",), published=True, title=None):
    """A post with every field the API documents (OpenAPI def-19)."""
    handle = source.lower().replace(" ", "-")
    when = iso(days_ago)
    pid = "p%d" % next(_next_id)
    return {
        "id": pid,
        "title": title or "Post " + pid,
        "url": "https://example.com/" + pid,
        "image": "https://img.example.com/%s.png" % pid,
        "summary": "SUMMARY-TEXT that the script must never print.",
        "type": "article",
        "publishedAt": when if published else None,
        "createdAt": when,
        "commentsPermalink": "https://app.daily.dev/posts/" + pid,
        "source": {"id": handle, "name": source, "handle": handle, "image": None},
        "tags": list(tags),
        "readTime": 3,
        "numUpvotes": up,
        "numComments": com,
        "author": {"name": "Someone", "image": None},
    }


def redated(p, published, created):
    """The same post with its publishedAt and createdAt replaced."""
    return dict(p, publishedAt=published, createdAt=created)


def make_state(**kw):
    args = dict(seeds=["t"], own=None, controls=[], window=30, max_tags=20, sources=False, now=NOW)
    args.update(kw)
    return gf.new_state(**args)


def make_sample(posts, has_next=False):
    """One page holding the posts in the order given, as _fetch_page would record it."""
    page = [gf.compact(p, NOW) for p in posts]
    return {"posts": page, "pages": 1, "cursor": None, "has_next": has_next,
            "newest_first": [gf._newest_first(gf.parse_date(p["date"]) for p in page)],
            "second_oldest": [gf._second_oldest(page)], "published_newest_first": [], "error": None}


def metrics(posts, has_next=False, **state_kw):
    return gf.tag_metrics("t", make_sample(posts, has_next), make_state(**state_kw))


def paged(*pages, always_more=False):
    """A tag-feed route serving the given pages by cursor."""
    def handler(params):
        i = int(params.get("cursor", "p0")[1:])
        more = always_more or i + 1 < len(pages)
        return {"data": pages[min(i, len(pages) - 1)],
                "pagination": {"hasNextPage": more, "cursor": "p%d" % (i + 1) if more else None}}
    return handler


def sample_all(api, state, *tags):
    """Page 1 of each tag, then the run's extra pages, as the score phase does."""
    for tag in tags:
        gf.sample_tag(api, state, tag)
    gf.fetch_extra_pages(api, state, list(tags))


def tag_search(results):
    return lambda params: {"data": [{"name": n} for n in results.get(params["q"], [])]}


def discussed(by_tag):
    return lambda params: {"data": by_tag.get(params["tag"], []),
                           "pagination": {"hasNextPage": False, "cursor": None}}


class FakeApi:
    """Stands in for gf.Api: same get(path, params) contract, no network."""

    def __init__(self, state, routes, fail_on_call=None):
        self.state = state
        self.routes = routes
        self.fail_on_call = fail_on_call
        self.requests = []
        self.served = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.requests.append((path, params))
        if self.fail_on_call is not None and len(self.requests) == self.fail_on_call:
            self.fail_on_call = None
            raise gf.RateLimited(30, {})
        self.state["calls"] += 1
        handler = self.routes.get(path)
        if handler is None:
            result = {"data": [], "pagination": {"hasNextPage": False, "cursor": None}}
        else:
            result = handler(params)
        self.served.append((path, params.get("cursor"), params.get("tag") or params.get("q")))
        return result


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = json.dumps(body).encode("utf-8")
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def http_error(code, headers=None, body=None):
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    fp = io.BytesIO(json.dumps(body or {}).encode("utf-8"))
    return urllib.error.HTTPError("https://api.daily.dev/x", code, "err", hdrs, fp)


class TempHome(unittest.TestCase):
    """Points the .env path and the run cache at a temp folder, with no token in the environment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.env_path = tmp / ".config" / "gap-finder" / ".env"
        patches =[mock.patch.object(gf, "ENV_PATH", self.env_path),
                   mock.patch.object(gf, "RUN_DIR", tmp / "runs"),
                   mock.patch.object(gf, "datetime", FrozenClock),
                   mock.patch.dict(os.environ, {}, clear=False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("DAILY_DEV_TOKEN", None)

    def write_env(self, text):
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        self.env_path.write_text(text, encoding="utf-8")


def run_main(argv, make_api=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = gf.main(argv, make_api=make_api)
    return code, out.getvalue()


# ---------- token and setup ----------

class TokenTests(TempHome):
    def test_reads_token_from_env_file(self):
        self.write_env("# Gap Finder\nDAILY_DEV_TOKEN=abc123\n")
        self.assertEqual(gf.load_token(), "abc123")

    def test_environment_variable_wins_over_file(self):
        self.write_env("DAILY_DEV_TOKEN=from-file\n")
        os.environ["DAILY_DEV_TOKEN"] = "from-env"
        self.assertEqual(gf.load_token(), "from-env")

    def test_quoted_value_is_unquoted(self):
        self.write_env('DAILY_DEV_TOKEN="abc123"\n')
        self.assertEqual(gf.load_token(), "abc123")

    def test_empty_placeholder_means_setup_needed(self):
        self.write_env("DAILY_DEV_TOKEN=\n")
        with self.assertRaises(gf.SetupNeeded):
            gf.load_token()

    def test_missing_file_means_setup_needed(self):
        with self.assertRaises(gf.SetupNeeded):
            gf.load_token()

    def test_setup_creates_an_empty_placeholder(self):
        result = gf.cmd_setup()
        self.assertTrue(result["created"])
        self.assertTrue(self.env_path.is_file())
        with self.assertRaises(gf.SetupNeeded):
            gf.load_token()

    def test_setup_never_overwrites_an_existing_file(self):
        self.write_env("DAILY_DEV_TOKEN=keep-me\n")
        result = gf.cmd_setup()
        self.assertFalse(result["created"])
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), "DAILY_DEV_TOKEN=keep-me\n")

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX only")
    def test_setup_restricts_the_file_to_the_user(self):
        gf.cmd_setup()
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)


# ---------- the network boundary ----------

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.sleeps = []
        p = mock.patch.object(gf.time, "sleep", self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def use(self, outcomes):
        opener = FakeOpener(outcomes)
        p = mock.patch.object(gf, "_opener", opener)
        p.start()
        self.addCleanup(p.stop)
        return opener

    def test_sends_get_with_bearer_token_to_api_host(self):
        opener = self.use([FakeResponse({"data": []})])
        gf.Api("tok", self.state).get("/feeds/tag/houdini", {"limit": 50})
        req = opener.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, "https://api.daily.dev/public/v1/feeds/tag/houdini?limit=50")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(self.state["calls"], 1)

    def test_401_means_setup_needed(self):
        self.use([http_error(401)])
        with self.assertRaises(gf.SetupNeeded):
            gf.Api("tok", self.state).get("/feeds/tag/x")

    def test_404_is_an_empty_feed(self):
        self.use([http_error(404)])
        self.assertEqual(gf.Api("tok", self.state).get("/feeds/tag/x"), {"data": []})

    def test_429_waits_retry_after_then_retries_once(self):
        opener = self.use([http_error(429, headers={"retry-after": "7"}), FakeResponse({"data": [1]})])
        self.assertEqual(gf.Api("tok", self.state).get("/feeds/tag/x"), {"data": [1]})
        self.assertEqual(self.sleeps, [7])
        self.assertEqual(len(opener.requests), 2)

    def test_second_429_raises_rate_limited_using_body_retry_after(self):
        opener = self.use([http_error(429, body={"retryAfter": 12}), http_error(429, body={"retryAfter": 12})])
        with self.assertRaises(gf.RateLimited) as ctx:
            gf.Api("tok", self.state).get("/feeds/tag/x")
        self.assertEqual(ctx.exception.wait, 12)
        self.assertEqual(self.sleeps, [12])
        self.assertEqual(len(opener.requests), 2)

    def test_paces_when_two_calls_remain(self):
        self.use([FakeResponse({}, {"x-ratelimit-remaining": "2", "x-ratelimit-reset": "20"}), FakeResponse({})])
        api = gf.Api("tok", self.state)
        api.get("/a")
        api.get("/b")
        self.assertEqual(self.sleeps, [20])

    def test_long_rate_limit_wait_is_reported_without_sleeping(self):
        opener = self.use([http_error(429, headers={"retry-after": "3600"})])
        with self.assertRaises(gf.RateLimited) as ctx:
            gf.Api("tok", self.state).get("/feeds/tag/x")
        self.assertEqual(ctx.exception.wait, 3600)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(opener.requests), 1)

    def test_paces_until_an_epoch_reset_time(self):
        reset = str(int(time.time()) + 30)
        self.use([FakeResponse({}, {"x-ratelimit-remaining": "1", "x-ratelimit-reset": reset}), FakeResponse({})])
        api = gf.Api("tok", self.state)
        api.get("/a")
        api.get("/b")
        self.assertEqual(len(self.sleeps), 1)
        self.assertTrue(25 <= self.sleeps[0] <= 31, self.sleeps)

    def test_does_not_pace_when_calls_remain(self):
        self.use([FakeResponse({}, {"x-ratelimit-remaining": "30", "x-ratelimit-reset": "20"}), FakeResponse({})])
        api = gf.Api("tok", self.state)
        api.get("/a")
        api.get("/b")
        self.assertEqual(self.sleeps, [])


class RedirectTests(unittest.TestCase):
    """A real local server answers with a redirect; the token must not follow it."""

    def test_redirect_is_not_followed(self):
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/public/v1/start"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:%d/target" % self.server.server_port)
                    self.end_headers()
                else:
                    hits.append(self.headers.get("Authorization"))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d/public/v1" % server.server_port
        with mock.patch.object(gf, "BASE_URL", base), mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            with self.assertRaises(urllib.error.HTTPError):
                gf.Api("tok", make_state()).get("/start")
        self.assertEqual(hits, [])


# ---------- per-tag metrics ----------

class MetricsTests(unittest.TestCase):
    def test_velocity_uses_the_window_when_the_sample_covers_it(self):
        row = metrics([post(d) for d in (1.0, 1.5, 2.0, 2.5, 3.0)], has_next=False, window=30)
        self.assertAlmostEqual(row["V"], 5 / (30 / 7), places=4)  # 1.17 a week, not 11.7
        self.assertFalse(row["n_floor"])

    def test_velocity_on_a_newest_first_sample_is_a_measured_rate(self):
        row = metrics([post(1 + 0.5 * i) for i in range(10)], has_next=True, window=30)
        # every post from the oldest (5.5 days back) to the run: 9 after the oldest, over 5.5 days
        self.assertAlmostEqual(row["V"], 9 / (5.5 / 7), places=4)
        self.assertTrue(row["n_floor"])
        self.assertFalse(row["V_floor"])

    def test_velocity_on_mixed_pages_is_a_floor(self):
        days = [0.5 + 7 * i / 49 for i in range(50)][::-1]  # 50 posts spanning exactly one week, oldest first
        row = metrics([post(d) for d in days], has_next=True, window=30)
        self.assertAlmostEqual(row["V"], 40 / (39 / 49), places=4)  # the middle 40 posts span 39/49 of a week
        self.assertTrue(row["V_floor"])

    def test_floor_velocity_uses_the_middle_80_percent_of_dates(self):
        days = [30] + [0.5 * (i + 1) for i in range(9)]           # one stray post a month back, out of order
        row = metrics([post(d) for d in days], has_next=True, window=365)
        self.assertAlmostEqual(row["V"], 8 / 0.5, places=4)       # 8 posts over 3.5 days, not 10 over 29.5
        self.assertTrue(row["V_floor"])

    def test_velocity_span_is_at_least_one_day(self):
        row = metrics([post(0.01 * (i + 1)) for i in range(10)][::-1], has_next=True, window=30)
        self.assertAlmostEqual(row["V"], 8 * 7.0, places=4)       # the middle 8 posts, over one day

    def test_source_breadth_is_the_expected_count_in_20_posts(self):
        posts = ([post(1 + 0.01 * i, source="Big") for i in range(25)]
                 + [post(2 + 0.01 * i, source="S%d" % i) for i in range(25)])
        row = metrics(posts)
        # each one-post source is in a random 20 of the 50 posts with probability 20/50
        self.assertAlmostEqual(row["B"], 25 * 20 / 50 + 1 - math.comb(25, 20) / math.comb(50, 20))
        self.assertEqual(row["sources_seen"], 26)

    def test_source_breadth_does_not_grow_with_sample_depth(self):
        def pages(k):    # each page: 30 posts from three regular sources, 20 from sources seen once
            return [post(1 + 0.01 * i, source="Regular %d" % (i % 3)) for i in range(30 * k)] + \
                   [post(1 + 0.01 * i, source="Once %d" % i) for i in range(20 * k)]
        shallow, deep = metrics(pages(1)), metrics(pages(3))
        self.assertEqual((shallow["sources_seen"], deep["sources_seen"]), (23, 63))
        self.assertAlmostEqual(shallow["B"], deep["B"], delta=0.1)

    def test_a_tag_with_fewer_than_20_posts_counts_every_source(self):
        row = metrics([post(3 + i, source="S%d" % (i % 6)) for i in range(8)])
        self.assertAlmostEqual(row["B"], 6)

    def test_reaction_share_counts_mature_posts_with_any_upvote_or_comment(self):
        mature = [post(3 + i, up=u, com=c) for i, (u, c) in enumerate([(0, 0), (1, 0), (0, 1), (0, 0), (5, 2)])]
        young = [post(0.5, up=1000, com=100) for _ in range(5)]
        row = metrics(mature + young)
        self.assertEqual(row["n"], 10)
        self.assertFalse(row["young"])
        self.assertEqual((row["reacted"], row["scored"]), (3, 5))
        self.assertAlmostEqual(row["share"], 0.6)

    def test_young_sample_uses_all_in_window_posts(self):
        posts = [post(5, up=1), post(6)] + [post(0.5, com=1), post(0.5), post(0.5), post(0.5)]
        row = metrics(posts)
        self.assertTrue(row["young"])
        self.assertEqual((row["reacted"], row["scored"]), (2, 6))

    def test_tag_without_posts_has_no_share(self):
        row = metrics([])
        self.assertEqual((row["reacted"], row["scored"], row["share"]), (0, 0, None))

    def test_quiet_mature_tag_is_not_young(self):
        row = metrics([post(5 + i, up=10) for i in range(6)])
        self.assertFalse(row["young"])

    def test_own_source_is_set_aside_and_counted(self):
        posts = ([post(3, source="SimStim") for _ in range(3)]
                 + [post(4, source="Alpha") for _ in range(3)]
                 + [post(5, source="Beta") for _ in range(2)])
        row = metrics(posts, own="simstim")
        self.assertEqual(row["n"], 5)
        self.assertEqual(row["own_posts"], 3)
        self.assertEqual(row["B"], 2)
        self.assertEqual(row["top_source"], "Alpha")
        self.assertAlmostEqual(row["C"], 0.6)

    def test_posts_outside_the_window_are_ignored(self):
        row = metrics([post(3) for _ in range(3)] + [post(20) for _ in range(2)], window=7)
        self.assertEqual(row["n"], 3)

    def test_a_post_without_published_at_still_counts(self):
        row = metrics([post(3, published=False)])
        self.assertEqual(row["n"], 1)


# ---------- dates: createdAt is the feed's clock ----------

class DateTests(unittest.TestCase):
    def date(self, published, created):
        return gf.parse_date(gf.compact(redated(post(0), published, created), NOW)["date"])

    def test_created_at_is_the_date(self):
        self.assertEqual(self.date("2021-03-01T00:00:00.000Z", iso(1)), NOW - timedelta(days=1))

    def test_created_at_before_2018_is_bad_data(self):
        self.assertIsNone(self.date(iso(3), PLACEHOLDER))

    def test_created_at_after_the_run_is_bad_data(self):
        self.assertIsNone(self.date(iso(3), "2099-01-01T00:00:00.000Z"))

    def test_a_post_newer_than_the_run_start_keeps_its_date(self):
        # later phases of a run fetch posts that reached daily.dev after the run started
        self.assertEqual(self.date(None, iso(-0.1)), NOW + timedelta(days=0.1))

    def test_published_at_never_stands_in_for_created_at(self):
        self.assertIsNone(self.date(iso(3), None))

    def test_an_old_article_shared_this_week_is_in_the_window(self):
        row = metrics([redated(post(0), "2021-03-01T00:00:00.000Z", iso(2))], window=30)
        self.assertEqual(row["n"], 1)

    def test_maturity_is_counted_from_created_at(self):
        row = metrics([redated(post(0, up=1), iso(10), iso(1)) for _ in range(6)])   # published 10 days ago, shared yesterday
        self.assertTrue(row["young"])

    def test_velocity_is_measured_from_created_at(self):
        posts = [redated(post(0), iso(100 * (i + 1)), iso(0.5 * (i + 1))) for i in range(10)]
        row = metrics(posts, has_next=True, window=365)
        self.assertAlmostEqual(row["V"], 9 / (5 / 7), places=4)   # they reached daily.dev over the last 5 days

    def test_undated_post_counts_toward_n_and_sources_but_no_date_metric(self):
        posts = ([post(d, source="Alpha", up=1) for d in (3, 4, 5, 6, 7)]
                 + [redated(post(0, source="Beta", up=1), iso(1), PLACEHOLDER)])
        row = metrics(posts, has_next=True)
        self.assertEqual((row["n"], row["B"]), (6, 2))
        self.assertEqual(row["scored"], 5)                 # its age is unknown, so it isn't a mature post
        self.assertAlmostEqual(row["V"], 4 / (7 / 7))      # the 5 dated posts: 4 after the oldest, over its 7 days

    def test_notes_count_posts_with_a_bad_created_at(self):
        state = make_state()
        page = [post(3), post(4, published=False), redated(post(0), iso(3), PLACEHOLDER),
                redated(post(0), iso(3), None), redated(post(0), LISP_1980, iso(2))]
        api = FakeApi(state, {"/feeds/tag/t": paged(page)})
        gf.run_neighbours(api, state, ["t"])
        notes = gf.run_score(api, state, [])["notes"]
        self.assertEqual(notes["undated_posts"], {"t": 2})     # an old publishedAt is an article's own date, not bad data
        self.assertNotIn("published_at_replaced", notes)


# ---------- sampling and busy tags ----------

class SamplingTests(unittest.TestCase):
    def test_quiet_tag_reaching_past_the_window_gets_no_extra_page(self):
        state = make_state(window=30)
        page1 = [post(5), post(10), post(40), post(45)]    # newest first, two posts past the start
        api = FakeApi(state, {"/feeds/tag/t": paged(page1, [post(60)])})
        sample_all(api, state, "t")
        self.assertEqual(len(api.requests), 1)

    def test_busy_tag_gets_at_most_two_extra_pages(self):
        state = make_state()
        pages = [[post(0.1) for _ in range(50)] for _ in range(5)]
        api = FakeApi(state, {"/feeds/tag/t": paged(*pages)})
        sample_all(api, state, "t")
        self.assertEqual(len(api.requests), 3)
        self.assertEqual(state["extra_pages"], 2)

    def test_extra_pages_are_capped_per_run(self):
        state = make_state()
        routes = {"/feeds/tag/t%d" % i: paged([post(0.1) for _ in range(50)], always_more=True) for i in range(6)}
        api = FakeApi(state, routes)
        sample_all(api, state, *["t%d" % i for i in range(6)])
        self.assertEqual(state["extra_pages"], 10)
        self.assertEqual(len(api.requests), 16)
        self.assertEqual(state["errors"]["extra_page_cap"], ["t5"])

    def test_extra_pages_go_to_the_shortest_date_span_first(self):
        state = make_state(window=365)
        quiet = [post(d) for d in (40, 5, 100, 20, 200)]                  # mixed order: can't show coverage
        busy = [[post(0.01 * (i + 1) + 0.5 * k) for i in range(50)] for k in range(3)]
        api = FakeApi(state, {"/feeds/tag/quiet": paged(quiet, always_more=True),
                              "/feeds/tag/busy": paged(*busy, always_more=True)})
        with mock.patch.object(gf, "EXTRA_PAGES_PER_RUN", 2):
            sample_all(api, state, "quiet", "busy")
        self.assertEqual([path for path, params in api.requests if "cursor" in params], ["/feeds/tag/busy"] * 2)
        self.assertEqual(state["errors"]["extra_page_cap"], ["quiet"])

    def test_newest_first_is_judged_by_created_at(self):
        state = make_state(window=30)
        page1 = [redated(post(0), iso(p), iso(c)) for p, c in ((40, 5), (5, 10), (45, 40), (10, 45))]
        api = FakeApi(state, {"/feeds/tag/t": paged(page1, [post(60)])})
        sample_all(api, state, "t")
        self.assertEqual(state["samples"]["t"]["newest_first"], [True])
        self.assertEqual(len(api.requests), 1)        # two posts reached daily.dev before the window start

    def test_dummy_date_on_a_newest_first_page_does_not_end_sampling(self):
        state = make_state(window=7)
        page1 = [post(0.02 * (i + 1)) for i in range(48)] + [post(12), redated(post(0), None, PLACEHOLDER)]
        more = [[post(1 + 0.5 * k + 0.01 * i) for i in range(50)] for k in range(2)]
        api = FakeApi(state, {"/feeds/tag/t": paged(page1, *more, always_more=True)})
        sample_all(api, state, "t")
        self.assertEqual(state["samples"]["t"]["newest_first"][0], True)
        self.assertEqual(len(api.requests), 3)        # one post past the start, plus a dummy, isn't coverage
        self.assertTrue(gf.tag_metrics("t", state["samples"]["t"], state)["n_floor"])

    def test_dummy_date_on_a_mixed_page_does_not_end_sampling(self):
        state = make_state(window=365)                # the second live run's ai page, plus a placeholder date
        recent = [post(0.02 * (i + 1)) for i in range(47)]
        lisp = redated(post(0), LISP_1980, iso(1.2))
        page1 = (recent[:7] + [lisp] + recent[7:33] + [post(12)] + recent[33:]
                 + [redated(post(0), None, PLACEHOLDER)])
        more = [[post(1 + 0.5 * k + 0.01 * i) for i in range(50)] for k in range(2)]
        api = FakeApi(state, {"/feeds/tag/t": paged(page1, *more, always_more=True)})
        sample_all(api, state, "t")
        self.assertEqual(state["samples"]["t"]["newest_first"][0], False)
        self.assertEqual(len(api.requests), 3)
        row = gf.tag_metrics("t", state["samples"]["t"], state)
        self.assertEqual(row["n"], 150)               # the 1980 article counts from its createdAt; the undated post counts too
        self.assertTrue(row["n_floor"])
        self.assertGreater(row["V"], 100)             # it was 0.94 a week

    def test_mixed_page_reaching_past_the_window_start_does_not_cover_it(self):
        state = make_state(window=30)
        api = FakeApi(state, {"/feeds/tag/t": paged([post(d) for d in (5, 40, 3, 50, 4, 60)], [post(70)])})
        sample_all(api, state, "t")
        self.assertEqual(len(api.requests), 2)        # only the feed's last page shows coverage

    def test_records_whether_pages_come_newest_first(self):
        state = make_state()
        api = FakeApi(state, {"/feeds/tag/a": paged([post(1), post(2), post(9)]),
                              "/feeds/tag/b": paged([post(9), post(1), post(2)])})
        self.assertEqual(gf.sample_tag(api, state, "a")["newest_first"], [True])
        self.assertEqual(gf.sample_tag(api, state, "b")["newest_first"], [False])

    def test_tag_names_are_escaped_in_the_path(self):
        state = make_state()
        api = FakeApi(state, {})
        gf.sample_tag(api, state, "a/b")
        self.assertEqual(api.requests[0][0], "/feeds/tag/a%2Fb")

    def test_feed_error_marks_the_tag_and_it_reads_as_too_quiet(self):
        state = make_state()

        def boom(params):
            raise urllib.error.URLError("connection reset")

        sample = gf.sample_tag(FakeApi(state, {"/feeds/tag/t": boom}), state, "t")
        self.assertIn("connection reset", sample["error"])
        rows, _, _ = gf.score([gf.tag_metrics("t", sample, state)])
        self.assertEqual(rows[0]["verdict"], "Too quiet")


# ---------- run notes and cards ----------

def score_run(routes, tags):
    """The neighbours and score phases for the given seed tags, as one run."""
    state = make_state(seeds=list(tags))
    api = FakeApi(state, routes)
    gf.run_neighbours(api, state, list(tags))
    return gf.run_score(api, state, [])


class RunNotesTests(unittest.TestCase):
    def test_notes_count_newest_first_pages_under_each_field(self):
        shared = [redated(post(0), iso(p), iso(c)) for p, c in ((400, 1), (3, 2), (900, 3))]   # newest first by createdAt only
        notes = score_run({"/feeds/tag/t": paged(shared)}, ["t"])["notes"]
        self.assertEqual(notes["feed_pages_newest_first"], {"createdAt": 1, "publishedAt": 0})
        self.assertEqual(notes["feed_pages_checked"], {"createdAt": 1, "publishedAt": 1})

    def test_notes_say_which_counts_are_floors(self):
        routes = {"/feeds/tag/a": paged([post(1 + i) for i in range(6)]),
                  "/feeds/tag/b": paged([post(1 + i) for i in range(6)], always_more=True)}
        notes = score_run(routes, ["a", "b"])["notes"]
        floors = [c for c in notes["caveats"] if "floor" in c]
        self.assertEqual(len(floors), 1)              # b's pages are newest first, so its V is a measured rate
        self.assertTrue(floors[0].startswith("n is a floor for b:"), floors[0])
        self.assertEqual((notes["n_is_floor"], notes["V_is_floor"]), (["b"], []))
        self.assertEqual(notes["B_subsample"], 20)

    def test_notes_say_when_velocity_is_a_floor(self):
        routes = {"/feeds/tag/c": paged([post(d) for d in (3, 1, 6, 2, 5, 4)], always_more=True)}
        notes = score_run(routes, ["c"])["notes"]
        floors = [c for c in notes["caveats"] if "floor" in c]
        self.assertEqual(len(floors), 2)
        self.assertTrue(floors[1].startswith("V is a floor for c:"), floors[1])
        self.assertEqual(notes["V_is_floor"], ["c"])

    def test_notes_always_state_that_seen_posts_may_be_missing(self):
        routes = {"/feeds/tag/a": paged([post(1 + i) for i in range(6)])}
        limits = [c for c in score_run(routes, ["a"])["notes"]["caveats"] if "clicked or seen" in c]
        self.assertEqual(len(limits), 1)
        self.assertIn("hasn't been checked", limits[0])
        self.assertIn("undercounted", limits[0])

    def test_no_floor_warning_when_every_count_is_exact(self):
        routes = {"/feeds/tag/a": paged([post(1 + i) for i in range(6)])}
        self.assertEqual([c for c in score_run(routes, ["a"])["notes"]["caveats"] if "floor" in c], [])


class CardTests(unittest.TestCase):
    def test_cards_mark_old_articles_circulating_again(self):
        page = ([redated(post(0, up=30, title="LISP Comes of Age (1980)"), LISP_1980, iso(2)),
                 redated(post(0, up=20, title="Over a year"), iso(368), iso(2)),
                 redated(post(0, up=10, title="Under a year"), iso(300), iso(2))]
                + [post(3 + i) for i in range(3)])
        card = score_run({"/feeds/tag/t": paged(page)}, ["t"])["cards"][0]
        self.assertEqual([(p["title"], p["published"], p["reshare"]) for p in card["posts"]],
                         [("LISP Comes of Age (1980)", "1980-08-28", True),
                          ("Over a year", iso(368)[:10], True),
                          ("Under a year", iso(300)[:10], False)])


# ---------- baseline, verdicts, ranking ----------

def row(tag, reacted, scored=10, n=None, B=3, C=0.3, V=1.0, young=False, control=False):
    """A tag_metrics row: `reacted` of `scored` posts got a reaction; n defaults to `scored`."""
    return {"tag": tag, "reacted": reacted, "scored": scored, "share": reacted / scored if scored else None,
            "n": scored if n is None else n, "B": B, "sources_seen": B, "C": C, "V": V, "young": young,
            "control": control, "n_floor": False, "V_floor": False, "top_source": "x", "own_posts": 0, "error": None}


class ScoringTests(unittest.TestCase):
    def test_niche_share_is_the_median_share_of_eligible_tags(self):
        rows = [row("a", 1), row("b", 2), row("c", 3),
                row("ctl", 10, control=True), row("yng", 9, young=True), row("quiet", 3, scored=3)]
        _, d0, thin = gf.score(rows)
        self.assertAlmostEqual(d0, 0.2)
        self.assertFalse(thin)

    def test_demand_is_shrunk_toward_the_niche_share(self):
        rows = [row("a", 1), row("b", 3), row("c", 5),           # niche share 0.3
                row("small", 5, scored=5, control=True), row("big", 100, scored=200, control=True),
                row("none", 0, scored=20, control=True)]
        by_tag = {r["tag"]: r for r in gf.score(rows)[0]}
        self.assertAlmostEqual(by_tag["small"]["D"], 8 / 15)     # raw 1.0 stays near the niche
        self.assertAlmostEqual(by_tag["big"]["D"], 103 / 210)    # raw 0.5 keeps its own rate
        self.assertAlmostEqual(by_tag["none"]["D"], 3 / 30)      # never reaches 0
        self.assertAlmostEqual(by_tag["small"]["ratio"], (8 / 15) / 0.3)

    def test_thin_baseline_withholds_verdicts(self):
        rows, d0, thin = gf.score([row("a", 1), row("b", 2), row("q", 1, scored=2)])
        self.assertTrue(thin)
        self.assertIsNone(d0)
        self.assertEqual({r["tag"]: r["verdict"] for r in rows}, {"a": "Withheld", "b": "Withheld", "q": "Too quiet"})
        self.assertEqual([r["tag"] for r in rows], ["b", "a", "q"])  # ranked by raw share when withheld

    def test_baseline_without_any_reaction_is_thin(self):
        rows, d0, thin = gf.score([row("a", 0), row("b", 0), row("c", 0)])
        self.assertTrue(thin)
        self.assertIsNone(d0)
        self.assertEqual({r["verdict"] for r in rows}, {"Withheld"})

    def test_table_rows_say_whether_demand_is_above_or_below_the_niche(self):
        rows, _, _ = gf.score([row("lo", 1), row("mid", 2), row("hi", 5)])
        table = {r["tag"]: gf._table_row(r) for r in rows}
        self.assertEqual((table["lo"]["vs_niche"], table["hi"]["vs_niche"]), ("below", "above"))
        self.assertEqual((table["lo"]["share"], table["lo"]["scored"]), (0.1, 10))

    def test_verdicts_follow_the_rules_in_order(self):
        cases = [
            # n,  C,    ratio, B,  V,  expected
            (4, 1.0, 3.0, 1, 0, "Too quiet"),
            (10, 0.5, 0.2, 1, 0, "Single publisher"),       # owned and ignored still counts
            (10, 0.94, 0.68, 4, 0, "Single publisher"),     # houdini in the first live run
            (10, 0.49, 0.75, 2, 0, "Normal"),
            (10, 0.6, 2.0, 2, 0, "Single publisher"),
            (10, 0.3, 1.0, 5, 0, "Under-served"),
            (10, 0.3, 1.0, 6, 0, "Normal"),
            (10, 0.1, 0.99, 15, 0, "Saturated"),
            (10, 0.1, 0.99, 11, 0, "Saturated"),            # B is per 20 posts: 15 even sources give about 11
            (10, 0.1, 0.99, 10.9, 0, "Normal"),
            (10, 0.1, 0.5, 3, 20, "Saturated"),
            (10, 0.1, 1.2, 20, 30, "Normal"),
        ]
        for n, C, ratio, B, V, expected in cases:
            with self.subTest(n=n, C=C, ratio=ratio, B=B, V=V):
                r = row("t", 1, n=n, B=B, C=C, V=V)
                r["ratio"] = ratio
                self.assertEqual(gf.verdict(r, thin=False), expected)

    def test_ranked_by_opportunity_with_too_quiet_last_and_unranked(self):
        rows, d0, _ = gf.score([row("a", 40, scored=100, B=6), row("b", 20, scored=100, B=0),
                                row("c", 10, scored=100, B=2), row("q", 1, scored=1)])
        self.assertAlmostEqual(d0, 0.2)
        self.assertEqual([r["tag"] for r in rows], ["b", "a", "c", "q"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3, None])
        self.assertAlmostEqual(rows[0]["O"], (22 / 110) / 0.2 / 1)   # D = (20 + 10 x 0.2) / 110; log2(2) = 1
        self.assertAlmostEqual(rows[1]["O"], (42 / 110) / 0.2 / 3)   # log2(8) = 3
        self.assertAlmostEqual(rows[2]["O"], (12 / 110) / 0.2 / 2)   # log2(4) = 2


# ---------- seeds and neighbours ----------

class SeedTests(unittest.TestCase):
    def test_keeps_exact_match_up_to_three_close_matches_and_unknown_seeds(self):
        state = make_state(seeds=["houdini", "vex"])
        results = {"houdini": ["houdini", "houdini-engine", "sidefx-houdini", "houdinifx", "houdini-vex", "blender"]}
        out = gf.run_start(FakeApi(state, {"/search/tags": tag_search(results)}), state)
        by_seed = {c["seed"]: c for c in out["seed_tags"]}
        self.assertEqual(by_seed["houdini"]["tag"], "houdini")
        self.assertTrue(by_seed["houdini"]["listed_by_tag_search"])
        self.assertNotIn("found", by_seed["houdini"])
        self.assertEqual(by_seed["houdini"]["close"], ["houdini-engine", "sidefx-houdini", "houdinifx"])
        self.assertEqual(by_seed["vex"]["tag"], "vex")
        self.assertFalse(by_seed["vex"]["listed_by_tag_search"])
        self.assertEqual(by_seed["vex"]["close"], [])

    def test_seed_with_spaces_matches_the_hyphenated_tag(self):
        state = make_state(seeds=["Claude Code"])
        out = gf.run_start(FakeApi(state, {"/search/tags": tag_search({"claude-code": ["claude-code"]})}), state)
        self.assertEqual(out["seed_tags"][0]["tag"], "claude-code")
        self.assertTrue(out["seed_tags"][0]["listed_by_tag_search"])


class NeighbourTests(unittest.TestCase):
    def test_candidates_need_three_posts_and_skip_seed_and_control_tags(self):
        state = make_state(seeds=["houdini"], controls=["react"])
        page = ([post(3, tags=["houdini", "3d"]) for _ in range(4)]
                + [post(3, tags=["houdini", "vfx", "react"]) for _ in range(3)]
                + [post(3, tags=["houdini", "sidefx"]) for _ in range(2)])
        out = gf.run_neighbours(FakeApi(state, {"/feeds/tag/houdini": paged(page)}), state, ["houdini"])
        self.assertEqual(out["candidates"], [{"tag": "3d", "posts": 4}, {"tag": "vfx", "posts": 3}])

    def test_candidate_list_is_twice_the_free_slots_and_at_least_ten(self):
        page = [post(3, tags=["seed", "n%02d" % i]) for i in range(15) for _ in range(3)]
        # 1 seed tag, 15 candidates available. Free slots = max_tags - 1; show 2 x slots, at least 10.
        for max_tags, expected in ((6, 10), (8, 14), (20, 15)):
            with self.subTest(max_tags=max_tags):
                state = make_state(seeds=["seed"], max_tags=max_tags)
                out = gf.run_neighbours(FakeApi(state, {"/feeds/tag/seed": paged(page)}), state, ["seed"])
                self.assertEqual(len(out["candidates"]), expected)

    def test_seed_tags_show_their_co_occurring_tags_and_top_titles(self):
        state = make_state(seeds=["vex"])
        page = ([post(4, tags=["vex", "security", "docker"]) for _ in range(2)]       # feed order isn't engagement order
                + [post(5, tags=["vex", "kubernetes"])]
                + [post(3, up=6 + i, tags=["vex", "security", "sbom"], title="SBOM post %d" % i) for i in range(4)])
        out = gf.run_neighbours(FakeApi(state, {"/feeds/tag/vex": paged(page)}), state, ["vex"])
        seed = out["seed_tags"][0]
        self.assertEqual((seed["tag"], seed["in_window"]), ("vex", 7))
        self.assertEqual(seed["co_tags"], ["security", "sbom", "docker", "kubernetes"])
        self.assertEqual(seed["titles"], ["SBOM post 3", "SBOM post 2", "SBOM post 1"])

    def test_seed_co_tags_are_capped_at_five(self):
        state = make_state(seeds=["s"])
        page = [post(3, tags=["s"] + ["c%d" % i for i in range(7)])]
        out = gf.run_neighbours(FakeApi(state, {"/feeds/tag/s": paged(page)}), state, ["s"])
        self.assertEqual(out["seed_tags"][0]["co_tags"], ["c0", "c1", "c2", "c3", "c4"])

    def test_running_neighbours_again_without_a_seed_tag_costs_no_calls(self):
        state = make_state(seeds=["houdini", "vex"])
        routes = {"/feeds/tag/houdini": paged([post(3, tags=["houdini", "3d"]) for _ in range(3)]),
                  "/feeds/tag/vex": paged([post(3, tags=["vex", "security"]) for _ in range(3)])}
        api = FakeApi(state, routes)
        first = gf.run_neighbours(api, state, ["houdini", "vex"])
        self.assertEqual([c["tag"] for c in first["candidates"]], ["3d", "security"])
        calls = len(api.requests)
        again = gf.run_neighbours(api, state, ["houdini"])
        self.assertEqual(len(api.requests), calls)
        self.assertEqual([c["tag"] for c in again["candidates"]], ["3d"])
        self.assertEqual([s["tag"] for s in again["seed_tags"]], ["houdini"])

    def test_notes_list_seed_tags_dropped_at_check_two(self):
        state = make_state(seeds=["houdini", "vex"])
        api = FakeApi(state, {"/search/tags": tag_search({"houdini": ["houdini", "houdini-engine"]})})
        gf.run_start(api, state)
        gf.run_neighbours(api, state, ["houdini"])          # vex dropped; the close match was never a seed
        out = gf.run_score(api, state, [])
        self.assertEqual(out["notes"]["seeds_dropped"], ["vex"])
        self.assertNotIn("vex", [r["tag"] for r in out["table"]])

    def test_score_keeps_the_agent_order_up_to_the_max(self):
        state = make_state(seeds=["a", "b"], max_tags=3)
        api = FakeApi(state, {})
        gf.run_neighbours(api, state, ["a", "b"])
        out = gf.run_score(api, state, ["vfx", "3d"])
        requested = [p for p, _ in api.requests if p.startswith("/feeds/tag/")]
        self.assertIn("/feeds/tag/vfx", requested)
        self.assertNotIn("/feeds/tag/3d", requested)
        self.assertEqual(out["notes"]["neighbours_dropped_over_max"], ["3d"])


class RarePublisherTests(unittest.TestCase):
    def test_searched_sources_with_few_posts_are_listed_as_rare(self):
        state = make_state(seeds=["houdini"], sources=True)
        sources = [{"id": "80-level", "name": "80 Level", "handle": "80-level", "image": None, "description": None},
                   {"id": "quiet-blog", "name": "Quiet Blog", "handle": "quiet-blog", "image": None, "description": None}]
        routes = {"/search/tags": tag_search({"houdini": ["houdini"]}),
                  "/search/sources": lambda params: {"data": sources},
                  "/feeds/tag/houdini": paged([post(3, source="80 Level") for _ in range(6)]
                                              + [post(4, source="Quiet Blog")])}
        api = FakeApi(state, routes)
        gf.run_start(api, state)
        gf.run_neighbours(api, state, ["houdini"])
        out = gf.run_score(api, state, [])
        self.assertEqual(out["rare_publishers"],
                         [{"id": "quiet-blog", "handle": "quiet-blog", "name": "Quiet Blog", "posts_in_sample": 1}])


# ---------- whole runs through main() ----------

def validation_routes():
    """The spec's validation case, shaped like the first live run: 80 Level owns houdini and readers mostly
    ignore it (2 of 12 posts reacted to), vex has no posts, 3d is crowded and quiet, vfx has few publishers
    and keen readers, react is busy. Niche share: median of 2/12, 4/10, 3/20, 6/8 = 0.283."""
    houdini = ([post(5 + 20 * i, source="80 Level", up=3 if i == 0 else 0,
                     tags=["houdini", "3d"] + (["vfx"] if i < 4 else [])) for i in range(9)]
               + [post(15 + 30 * i, source="CG Channel", up=1 - i, tags=["houdini", "3d", "sidefx"]) for i in range(2)]
               + [post(100, source="Blog A", tags=["houdini", "3d"])]
               + [post(400, source="80 Level", up=30, com=2, tags=["houdini", "3d"])])
    procedural = [post(10 + 30 * i, source="Src %d" % (i % 6), up=2 if i < 4 else 0,
                       tags=["procedural", "3d"] + (["webdev"] if i < 5 else [])) for i in range(10)]
    three_d = [post(5 + 15 * i, source="3D %d" % i, up=1 if i < 3 else 0, tags=["3d"]) for i in range(18)] + \
              [post(300, source="3D 0", tags=["3d"]), post(310, source="3D 1", tags=["3d"])]
    vfx = [post(10 + 40 * i, source="VFX %d" % (i % 4), up=4 if i < 6 else 0, com=1 if i < 6 else 0, tags=["vfx"])
           for i in range(8)]
    react = [[post(0.2, source="React %d" % (i % 40), up=5, com=0, tags=["react"]) for i in range(50)] for _ in range(4)]
    hot = post(30, source="VFX Forum", up=80, com=40, tags=["vfx"], title="VFX discussion")
    return {
        "/search/tags": tag_search({"houdini": ["houdini", "houdini-engine"],
                                    "procedural": ["procedural", "procedural-programming"]}),
        "/feeds/tag/houdini": paged(houdini, [post(500)]),
        "/feeds/tag/procedural": paged(procedural),
        "/feeds/tag/3d": paged(three_d),
        "/feeds/tag/vfx": paged(vfx),
        "/feeds/tag/react": paged(*react, always_more=True),
        "/feeds/discussed": discussed({"vfx": [hot]}),
    }


class EndToEndTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["DAILY_DEV_TOKEN"] = "tok-SECRET-42"
        self.routes = validation_routes()
        self.apis = []

    def factory(self, fail_on_call=None):
        def make(token, state):
            api = FakeApi(state, self.routes, fail_on_call=fail_on_call)
            self.apis.append(api)
            return api
        return make

    def full_run(self):
        code, out = run_main(["start", "--seeds", "houdini,vex,procedural", "--control", "react",
                              "--window", "365"], self.factory())
        self.assertEqual(code, 0, out)
        start = json.loads(out)
        run = start["run"]
        code, out = run_main(["neighbours", "--run", run, "--tags", "houdini,vex,procedural"], self.factory())
        self.assertEqual(code, 0, out)
        neighbours = json.loads(out)
        code, out = run_main(["score", "--run", run, "--tags", "3d,vfx"], self.factory())
        self.assertEqual(code, 0, out)
        return start, neighbours, json.loads(out), out

    def test_validation_case_gives_the_expected_verdicts(self):
        _, neighbours, result, _ = self.full_run()
        self.assertEqual([c["tag"] for c in neighbours["candidates"]], ["3d", "webdev", "vfx"])
        self.assertEqual({s["tag"]: s["co_tags"] for s in neighbours["seed_tags"]},
                         {"houdini": ["3d", "vfx", "sidefx"], "vex": [], "procedural": ["3d", "webdev"]})
        table = {r["tag"]: r for r in result["table"]}
        self.assertEqual(table["houdini"]["verdict"], "Single publisher")
        self.assertEqual(table["houdini"]["top_source"], "80 Level")
        self.assertEqual(table["houdini"]["vs_niche"], "below")              # owned and ignored
        self.assertEqual((table["houdini"]["share"], table["houdini"]["scored"]), (0.17, 12))
        self.assertEqual(table["vex"]["verdict"], "Too quiet")
        self.assertEqual(table["vfx"]["verdict"], "Under-served")
        self.assertEqual(table["3d"]["verdict"], "Saturated")
        self.assertTrue(table["react"]["control"])
        self.assertTrue(table["react"]["young"])
        self.assertNotEqual(table["react"]["verdict"], "Under-served")
        self.assertEqual(result["notes"]["niche_share"], 0.28)
        self.assertEqual(result["notes"]["seeds_dropped"], [])
        self.assertEqual(result["notes"]["extra_pages_by_tag"], {"react": 2})
        self.assertEqual(result["notes"]["undated_posts"], {})
        # react's 3 pages show 40 sources, but any 20 of its posts hold about 16, so its B no longer grows with depth
        self.assertEqual([r["tag"] for r in result["table"]], ["react", "vfx", "procedural", "houdini", "3d", "vex"])

    def test_cards_cover_the_top_non_control_tags_and_use_the_discussed_feed(self):
        _, _, result, _ = self.full_run()
        self.assertEqual([c["tag"] for c in result["cards"]], ["vfx", "procedural", "houdini", "3d"])
        vfx = result["cards"][0]
        self.assertEqual(vfx["posts"][0]["title"], "VFX discussion")
        self.assertEqual(len(vfx["posts"]), 3)
        self.assertEqual({r["tag"]: r["H"] for r in result["table"]}["vfx"], 1)

    def test_calls_are_counted_and_no_page_is_fetched_twice(self):
        _, _, result, _ = self.full_run()
        served = [s for api in self.apis for s in api.served]
        self.assertEqual(result["notes"]["calls"], len(served))
        self.assertEqual(result["notes"]["calls"], 15)  # 3 searches, 6 tag pages + 2 extra for react, 4 discussed
        self.assertEqual(len(served), len(set(served)))

    def test_output_is_a_summary_without_raw_fields_or_the_token(self):
        _, _, _, raw = self.full_run()
        self.assertNotIn("SUMMARY-TEXT", raw)
        self.assertNotIn("img.example.com", raw)
        self.assertNotIn("tok-SECRET-42", raw)

    def test_rate_limited_phase_resumes_without_refetching(self):
        code, out = run_main(["start", "--seeds", "houdini,vex,procedural", "--window", "365"], self.factory())
        run = json.loads(out)["run"]
        code, out = run_main(["neighbours", "--run", run, "--tags", "houdini,vex,procedural"], self.factory(fail_on_call=2))
        self.assertEqual(code, 3)
        self.assertTrue(json.loads(out)["partial"])
        code, out = run_main(["neighbours", "--run", run, "--tags", "houdini,vex,procedural"], self.factory())
        self.assertEqual(code, 0, out)
        tag_pages = [s for api in self.apis for s in api.served if s[0].startswith("/feeds/tag/")]
        self.assertEqual(len(tag_pages), 3)
        self.assertEqual(len(tag_pages), len(set(tag_pages)))


class MainTests(TempHome):
    def test_no_token_exits_with_setup(self):
        code, out = run_main(["start", "--seeds", "houdini"], lambda token, state: FakeApi(state, {}))
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(out)["setup"])

    def test_errors_never_print_the_token(self):
        os.environ["DAILY_DEV_TOKEN"] = "tok-SECRET-42"

        def broken(token, state):
            class Broken:
                def get(self, path, params=None):
                    raise RuntimeError("bad header: Bearer " + token)
            return Broken()

        code, out = run_main(["start", "--seeds", "houdini"], broken)
        self.assertEqual(code, 1)
        self.assertNotIn("tok-SECRET-42", out)

    def test_usage_errors_exit_1_not_the_setup_code(self):
        os.environ["DAILY_DEV_TOKEN"] = "tok"
        code, out = run_main(["start"], lambda t, s: FakeApi(s, {}))
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(out))

    def test_unknown_run_is_an_error(self):
        os.environ["DAILY_DEV_TOKEN"] = "tok"
        code, out = run_main(["score", "--run", "abcdef012345", "--tags", "x"], lambda t, s: FakeApi(s, {}))
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(out))

    def test_run_id_cannot_escape_the_cache_folder(self):
        os.environ["DAILY_DEV_TOKEN"] = "tok"
        code, out = run_main(["score", "--run", "../../etc/passwd", "--tags", "x"], lambda t, s: FakeApi(s, {}))
        self.assertEqual(code, 1)


# ---------- packaging ----------

class SkillTests(unittest.TestCase):
    def test_skill_md_embeds_the_current_script(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        script = (ROOT / "gapfinder.py").read_text(encoding="utf-8")
        begin = skill.index("<!-- gapfinder.py begin -->")
        start = skill.index("```python\n", begin) + len("```python\n")
        end = skill.index("```\n<!-- gapfinder.py end -->", start)
        self.assertEqual(skill[start:end], script)


if __name__ == "__main__":
    unittest.main()
