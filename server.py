#!/usr/bin/env python3
"""ShelfMate: paste a Goodreads profile link, get book recommendations.

Anonymous, no accounts, no tracking. Runs on a Raspberry Pi behind a
Tailscale funnel. Data sources:
  - Goodreads RSS shelf feed (the user's whole public library + ratings)
  - Goodreads public profile page (name + favorite books, fallback)
  - Open Library (subjects, similar works, covers)

Stdlib only. See README.md for the architecture notes.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SHELFMATE_PORT", "8086"))
ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")
CACHE_TTL = 7 * 24 * 3600          # Open Library data is stable
PROFILE_TTL = 6 * 3600             # Goodreads profiles change slowly
PROFILE_FETCH_TIMEOUT = 15
OL_FETCH_TIMEOUT = 12
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0")

# Per-IP request budget for the public API.
RATE_WINDOW = 60
RATE_MAX = 6
_rate_lock = threading.Lock()
_rate: dict[str, list[float]] = {}

# Subjects that carry no taste signal (cataloguing artifacts, mega-genres).
SUBJECT_STOPLIST = {
    "protected daisy", "in library", "accessible book", "overdrive",
    "large type books", "internet archive wishlist", "bookgorilla",
    "open library staff picks", "reading level-2", "fiction",
    "nonfiction", "general", "juvenile fiction", "juvenile literature",
    "popular print disabled books", "internet archive",
}
MAX_SEEDS = 30
MAX_SUBJECTS_PER_BOOK = 14
TOP_SUBJECTS = 6
MAX_RECS = 12
MIN_OVERLAP = 0.99       # absolute floor: at least one shared subject of unit weight
REL_OVERLAP = 0.35       # ...and keep only candidates within this fraction of the best match
EDITIONS_PRIOR_CAP = 30  # popularity prior cap: 500-edition classics can't bank it
RECENT_SUBJECTS = 3      # top-N taste subjects also searched for recent releases
RECENT_WINDOW_YEARS = 5  # "recent" = first published within this many years
RECENT_BONUS = 1.2       # max score bonus for a brand-new book (fades over ~15y)


def _current_year() -> int:
    return time.localtime().tm_year


def _recency_bonus(year: int | None) -> float:
    """Score boost for recent first-publication, fading to zero over ~15 years.

    Keeps recommendations from collapsing into the most-reprinted classics
    (Open Library's /subjects ranking is edition-heavy). Applied to the
    relevance floor too, so fresh releases clear it without lowering the
    bar for everything else.
    """
    if not year:
        return 0.0
    age = max(0, _current_year() - int(year))
    return RECENT_BONUS * max(0.0, 1.0 - age / 15.0)


# --------------------------------------------------------------------------
# Goodreads profile: URL parsing, fetching, extraction
# --------------------------------------------------------------------------

def parse_profile_url(url: str) -> str | None:
    """Accept any goodreads profile URL shape, return canonical /user/show/N URL.

    Handles:
      https://www.goodreads.com/user/show/12345
      https://www.goodreads.com/user/show/12345-jane-doe
      https://www.goodreads.com/user/show/12345?shelf=read
      goodreads.com/user/show/12345 (no scheme)
    Returns None for anything that isn't a profile URL.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host.endswith("goodreads.com"):
        return None
    m = re.match(r"^/user/show/(\d+)", parts.path)
    if not m:
        return None
    return f"https://www.goodreads.com/user/show/{m.group(1)}"


def fetch_url(url: str, timeout: int = PROFILE_FETCH_TIMEOUT, max_bytes: int = 3_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes)


def looks_like_signin(html: str) -> bool:
    """True when Goodreads served the login wall instead of the profile.

    Note: 'Sign in to Goodreads' appears in every page's nav — the reliable
    signal is the <title> (wall pages are titled 'Sign in') and the fact
    that wall pages are tiny stubs (~3 KB vs ~140 KB for a profile).
    """
    m = re.search(r"<title>([^<]*)</title>", html, re.I)
    if m and m.group(1).strip().lower().startswith("sign in"):
        return True
    return len(html) < 12_000 and "ap/signin" in html


def extract_name(html: str) -> str | None:
    m = re.search(r"<title>([^<]+)</title>", html)
    if not m:
        return None
    title = html_mod.unescape(m.group(1)).strip()
    # Strip in order: trailing "(N books)", " - Location" suffix, "(handle)":
    # "Otis Chandler (otis) - San Francisco, CA (1,652 books)" -> "Otis Chandler"
    title = re.sub(r"\s*\(\d[\d,.]*\s+books?\)\s*$", "", title)
    title = re.sub(r"\s+-\s+[^-]+$", "", title)        # " - City, State"
    title = re.sub(r"\s*\(([^)]*)\)\s*$", "", title)    # "(handle)"
    return re.sub(r"\s+", " ", title).strip() or None


def _alt_books(html: str) -> list[dict]:
    """Favorite books: <img alt="Title by Author" ...> inside cover grids."""
    books = []
    seen = set()
    for m in re.finditer(
        r'<img[^>]+alt="([^"]*?) by ([^"]*?)"[^>]*>', html
    ):
        title = html_mod.unescape(m.group(1)).strip()
        author = re.sub(r"\s+", " ", html_mod.unescape(m.group(2)).strip())
        if not title or len(title) > 200 or title.lower() in seen:
            continue
        if re.fullmatch(r"[\d.,#/ -]+", title):  # "1984" ok, "3.9" not
            if title != "1984":
                continue
        seen.add(title.lower())
        books.append({"title": title, "author": author, "source": "favorite"})
    return books


def _reading_books(html: str) -> list[dict]:
    """Currently-reading: <a class="bookTitle" href="/book/show/N-slug">Title</a>."""
    books = []
    seen = set()
    for m in re.finditer(
        r'<a[^>]+class="bookTitle"[^>]+href="/book/show/\d+[^"]*"[^>]*>([^<]+)</a>', html
    ):
        title = html_mod.unescape(m.group(1)).strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        books.append({"title": title, "author": "", "source": "reading"})
    return books


def extract_books(html: str) -> dict:
    """Parse a Goodreads profile page into name + seed books."""
    if looks_like_signin(html):
        return {"name": None, "books": [], "signin": True}
    favorites = _alt_books(html)
    reading = _reading_books(html)
    fav_titles = {b["title"].lower() for b in favorites}
    books = favorites + [b for b in reading if b["title"].lower() not in fav_titles]
    return {
        "name": extract_name(html),
        "books": books[:MAX_SEEDS],
        "signin": False,
    }


# --------------------------------------------------------------------------
# Goodreads RSS shelf feed: the user's WHOLE public library, anonymously
# --------------------------------------------------------------------------
# review/list_rss/<id> serves the full shelf (100 books/page, paginated),
# with per-book ratings, without requiring a login — unlike /review/list
# which redirects to sign-in. Primary source; the profile page above is
# the fallback (and supplies the display name + favorites).

RSS_PAGE_SIZE = 100
RSS_MAX_PAGES = 5               # 500 books is plenty of taste signal


def _rss_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
    return html_mod.unescape(m.group(1)).strip() if m else ""


def parse_rss(xml: str) -> list[dict]:
    """Parse one Goodreads list_rss page into seed books with ratings."""
    books, seen = [], set()
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = _rss_text(block, "title")
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        books.append({
            "title": title,
            "author": _rss_text(block, "author_name"),
            "rating": _rss_text(block, "user_rating"),
            "source": "shelf",
        })
    return books


def fetch_shelf_rss(user_id: str, shelf: str = "") -> list[dict]:
    """Pull the user's whole public shelf via review/list_rss, all pages."""
    books, seen = [], set()
    for page in range(1, RSS_MAX_PAGES + 1):
        url = f"https://www.goodreads.com/review/list_rss/{user_id}"
        if shelf:
            url += f"?shelf={shelf}&page={page}"
        else:
            url += f"?page={page}"
        got = parse_rss(fetch_url(url, timeout=20).decode("utf-8", "replace"))
        fresh = [b for b in got if b["title"].lower() not in seen]
        seen.update(b["title"].lower() for b in fresh)
        books.extend(fresh)
        if len(got) < RSS_PAGE_SIZE:
            break
    return books


def _pick_seeds(shelf: list[dict], favorites: list[dict], cap: int | None = None) -> list[dict]:
    """All shelf books as seeds, favorites first, then high-rated, then recent.

    Favorites are the user's explicit picks (higher taste weight); ratings
    tell us how much a shelf book actually mattered; the feed is
    newest-first, so unrated books keep a recency order. `cap` is only
    used by callers that must bound work (tests, fallbacks).
    """
    def rating(b: dict) -> int:
        try:
            return int(b.get("rating") or 0)
        except (TypeError, ValueError):
            return 0

    ranked = sorted(shelf, key=lambda b: -rating(b))   # stable: unrated stay newest-first
    fav_titles = {b["title"].lower() for b in favorites}
    picked = favorites + [b for b in ranked if b["title"].lower() not in fav_titles]
    return picked if cap is None else picked[:cap]


def extract_library_size(html: str) -> int | None:
    """'"Jane Doe (156 books)"' -> 156, from the profile page title."""
    m = re.search(r"\(([\d,]+)\s+books?\)", html, re.I)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Open Library client (with on-disk cache)
# --------------------------------------------------------------------------

def _cache_get(key: str, ttl: int):
    path = os.path.join(CACHE_DIR, key + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
        if time.time() - entry["t"] < ttl:
            return entry["v"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _cache_put(key: str, value) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, key + ".json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "v": value}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _ol_get(url: str):
    key = "ol-" + hashlib.sha1(url.encode()).hexdigest()[:20]
    cached = _cache_get(key, CACHE_TTL)
    if cached is not None:
        return cached
    req = urllib.request.Request(url, headers={"User-Agent": "ShelfMate/1.0 (book recommendations)"})
    with urllib.request.urlopen(req, timeout=OL_FETCH_TIMEOUT) as resp:
        data = json.loads(resp.read(2_000_000))
    _cache_put(key, data)
    return data


def ol_find_work(title: str, author: str) -> dict | None:
    """Best Open Library work for a title(+author). Cached, None-caching too.

    NB: OL's search API doesn't handle quoted multi-word author values
    (author:"Walter Isaacson" returns 0 hits / errors) — use the bare
    first name only, then verify against the full name in results.
    """
    q = f'title:"{title}"'
    if author:
        first = re.sub(r"[^A-Za-z'-]", " ", author).split()[0] if author.strip() else ""
        if first:
            q += f" author:{first}"
    key = "work-" + hashlib.sha1(q.lower().encode()).hexdigest()[:20]
    cached = _cache_get(key, CACHE_TTL)
    if cached is not None:
        return cached or None
    url = (
        "https://openlibrary.org/search.json?per_page=5&fields=key,title,author_name,subject,"
        "edition_count,first_publish_year,cover_i&sort=editions&q="
        + urllib.parse.quote(q)
    )
    try:
        data = _ol_get(url)
        docs = data.get("docs") or []
        best = _pick_best_doc(docs, title, author)
        _cache_put(key, best or {})
        return best
    except Exception:
        return None  # network hiccup: don't negative-cache, retry next time


def _norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\([^)]*\)", "", t)       # series "(City Spies #7)"
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _pick_best_doc(docs: list[dict], title: str, author: str) -> dict | None:
    want = _norm_title(title)
    author_l = (author or "").lower()
    for doc in docs:  # docs arrive sorted by editions; prefer exact title match
        if _norm_title(doc.get("title", "")) == want:
            if author_l and doc.get("author_name") and author_l.split()[0] not in {
                a.lower() for a in doc["author_name"]
            }:
                continue
            return doc
    for doc in docs:
        if want and want in _norm_title(doc.get("title", "")):
            return doc
    return None


def ol_subject_works(subject: str, limit: int = 30) -> list[dict]:
    url = (
        "https://openlibrary.org/subjects/"
        + urllib.parse.quote(subject.lower())
        + f".json?details=false&limit={limit}"
    )
    try:
        data = _ol_get(url)
        return data.get("works") or []
    except Exception:
        return []


def ol_recent_by_subject(subject: str, limit: int = 15) -> list[dict]:
    """Recent works for a taste subject via search.json (sort=new).

    The /subjects endpoint ranks by editions, so it buries new releases;
    search.json sorts by first-publish date and supports subject: +
    first_publish_year filters. Fields normalised to match /subjects
    works so both sources feed one candidate pool.
    """
    params = urllib.parse.urlencode({
        "q": f'subject:"{subject}"',
        "sort": "new",
        "fields": "key,title,author_name,first_publish_year,edition_count,subject,cover_i",
        "per_page": limit,
    })
    url = "https://openlibrary.org/search.json?" + params
    try:
        data = _ol_get(url)
    except Exception:
        return []
    out = []
    for doc in data.get("docs") or []:
        year = doc.get("first_publish_year")
        if not isinstance(year, int):
            continue  # sort=new surfaces undated scans; skip them
        if _current_year() - year > RECENT_WINDOW_YEARS:
            continue
        authors = doc.get("author_name") or [""]
        out.append({
            "title": doc.get("title", ""),
            "authors": [{"name": a} for a in authors],
            "edition_count": doc.get("edition_count", 0),
            "cover_id": doc.get("cover_i"),
            "key": doc.get("key", ""),
            "first_publish_year": year,
        })
    return out


# --------------------------------------------------------------------------
# Recommender
# --------------------------------------------------------------------------

def _clean_subjects(raw: list[str]) -> list[str]:
    out, seen = [], set()
    for s in raw or []:
        s = re.sub(r"\s+", " ", (s or "").strip().lower())
        if not s or len(s) < 3 or s in SUBJECT_STOPLIST or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= MAX_SUBJECTS_PER_BOOK:
            break
    return out


def seed_weight(seed: dict) -> float:
    """How strongly a seed book should drive the taste profile.

    Favorites are explicit picks (1.5). Shelf books scale with the user's
    own rating: 1★→0.8, 3★→1.4, 5★→2.0; unrated shelf books are neutral.
    """
    if seed.get("source") == "favorite":
        return 1.5
    r = str(seed.get("rating") or "0").strip()
    if r.isdigit() and int(r) > 0:
        return 0.5 + int(r) * 0.3
    return 1.0


def build_taste(seeds_with_subjects: list[dict]) -> Counter:
    taste = Counter()
    for seed in seeds_with_subjects:
        weight = seed_weight(seed)
        for s in seed["subjects"]:
            taste[s] += weight
    return taste


def recommend(seeds: list[dict], exclude_titles: list[str] | None = None) -> dict:
    """seeds: [{title, author?, source?, rating?}] -> {taste, recs, matched, unmatched}

    exclude_titles: books to never recommend (e.g. the user's whole shelf,
    not just the seed subset) — no point suggesting what they already read.
    """
    # Look up all seeds on Open Library in parallel (6 workers is polite
    # to the free API and plenty for a full shelf).
    with ThreadPoolExecutor(max_workers=6) as pool:
        docs = list(pool.map(
            lambda s: ol_find_work(s["title"], s.get("author", "")), seeds))
    seeds_with_subjects = []
    unmatched = []
    for seed, doc in zip(seeds, docs):
        if doc:
            seeds_with_subjects.append({**seed, "doc": doc, "subjects": _clean_subjects(doc.get("subject"))})
        else:
            unmatched.append(seed["title"])

    if not seeds_with_subjects:
        return {"taste": {}, "recs": [], "matched": [], "unmatched": unmatched, "reason": "no_match"}

    taste = build_taste(seeds_with_subjects)
    library = {_norm_title(s["title"]) for s in seeds}
    library.update(_norm_title(t) for t in (exclude_titles or []) if t)

    top_subjects = [s for s, _ in taste.most_common(TOP_SUBJECTS)]

    # Candidate pool, two sources:
    #   /subjects   — edition-ranked canon (matches taste, skews classic)
    #   search.json sort=new — new releases carrying a top taste subject
    candidates: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        subject_lists = list(pool.map(
            lambda sub: ol_subject_works(sub), top_subjects))
    with ThreadPoolExecutor(max_workers=6) as pool:
        recent_lists = list(pool.map(
            lambda sub: ol_recent_by_subject(sub),
            top_subjects[:RECENT_SUBJECTS]))
    for subject, works in zip(top_subjects, subject_lists + recent_lists):
        for work in works:
            title = work.get("title", "")
            key = _norm_title(title)
            if not key or key in library:
                continue
            authors = [a.get("name", "") for a in work.get("authors", [])] or [""]
            entry = candidates.setdefault(key, {
                "title": title,
                "author": authors[0],
                "editions": work.get("edition_count", 0),
                "cover_id": work.get("cover_id"),
                "ol_key": work.get("key", ""),
                "first_publish_year": work.get("first_publish_year"),
                "subjects": set(),
                "score": 0.0,
            })
            # search.json works carry their own subject list — credit every
            # subject that carries taste weight, not just the searched one
            extra = {s.strip().lower() for s in work.get("subject") or []}
            for s in {subject} | extra:
                if s in taste:
                    entry["subjects"].add(s)

    # score: taste overlap + capped popularity prior + recency. Two quality
    # floors: an absolute one (must genuinely share taste) and a relative
    # one (top-heavy lists read better than padded ones). The recency
    # bonus counts toward the absolute floor so new releases can clear it.
    overlaps = {
        key: sum(min(taste.get(s, 0), 4.0) for s in entry["subjects"] if taste.get(s, 0))
        for key, entry in candidates.items()
    }
    best = max(overlaps.values(), default=0.0)
    scored = []
    for key, entry in candidates.items():
        overlap = overlaps[key]
        recency = _recency_bonus(entry.get("first_publish_year"))
        # the recency bonus lets genuinely-shared-subject new releases
        # clear the absolute floor — without lowering it for old junk
        if overlap + recency < MIN_OVERLAP or overlap < best * REL_OVERLAP:
            continue
        entry["score"] = (overlap
                          + math.log10(min(int(entry["editions"] or 0), EDITIONS_PRIOR_CAP) + 1)
                          + recency)
        scored.append(entry)

    # reasons before sorting (needs subject sets), then order + diversify
    top_subject_names = [s for s, _ in taste.most_common(TOP_SUBJECTS)]
    for entry in scored:
        matched = [s for s in top_subject_names if s in entry["subjects"]]
        entry["why"] = _reason_phrase(entry, matched, seeds_with_subjects)

    ranked = sorted(scored, key=lambda e: e["score"], reverse=True)
    recs, per_author = [], Counter()
    for entry in ranked:
        if per_author[entry["author"].lower()] >= 2:
            continue
        per_author[entry["author"].lower()] += 1
        entry["subjects"] = sorted(entry["subjects"])[:5]
        recs.append(entry)
        if len(recs) >= MAX_RECS:
            break

    return {
        "taste": taste.most_common(TOP_SUBJECTS),
        "recs": recs,
        "matched": [s["title"] for s in seeds_with_subjects],
        "unmatched": unmatched,
        "reason": "ok",
    }


def _reason_phrase(entry: dict, matched: list[str], seeds: list[dict]) -> str:
    bits = []
    year = entry.get("first_publish_year")
    if isinstance(year, int) and _current_year() - year <= RECENT_WINDOW_YEARS:
        bits.append(f"New {year} release")
    if matched:
        joined = ", ".join(matched[:2])
        bits.append(f"matches your taste for {joined}")
    # name a seed book that shares a subject with this candidate
    for seed in seeds:
        shared = [s for s in seed["subjects"] if s in entry["subjects"]]
        if shared:
            bits.append(f"you liked {seed['title']} ({shared[0]})")
            break
    if entry["editions"] > 100:
        bits.append(f"a widely-read work ({entry['editions']} editions)")
    return "; ".join(bits) if bits else "Rounds out the mix from your shelves"


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

def rate_ok(ip: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    with _rate_lock:
        hits = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
        if len(hits) >= RATE_MAX:
            _rate[ip] = hits
            return False
        hits.append(now)
        _rate[ip] = hits
        if len(_rate) > 5000:  # crude memory guard
            _rate.clear()
        return True


# --------------------------------------------------------------------------
# API orchestration
# --------------------------------------------------------------------------

def recommend_from_profile(url: str) -> dict:
    canonical = parse_profile_url(url)
    if not canonical:
        return {"error": "That doesn't look like a Goodreads profile link. "
                         "It should look like https://www.goodreads.com/user/show/12345"}
    user_id = canonical.rsplit("/", 1)[-1]
    ckey = "profile-" + user_id
    parsed = _cache_get(ckey, PROFILE_TTL)
    if parsed is None:
        # 1) Profile page: display name + favorites (cheap, one request).
        try:
            html = fetch_url(canonical).decode("utf-8", "replace")
        except Exception as exc:
            return {"error": f"Couldn't reach Goodreads right now ({exc.__class__.__name__}). Try again in a moment."}
        name = extract_name(html)
        signin = looks_like_signin(html)
        favorites = [] if signin else _alt_books(html)
        library_size = None if signin else extract_library_size(html)

        # 2) Whole shelf via the public RSS feed (paginated, has ratings).
        shelf: list[dict] = []
        if not signin:
            try:
                shelf = fetch_shelf_rss(user_id)
            except Exception:
                shelf = []   # fall back to favorites below

        parsed = {
            "name": name,
            "signin": signin,
            "library_size": library_size,
            "books": _pick_seeds(shelf, favorites),
            "shelf_titles": [b["title"] for b in shelf],
        }
        _cache_put(ckey, parsed)
    if parsed.get("signin"):
        return {"error": "Goodreads asked us to sign in for that profile — it's probably private."}
    if not parsed["books"]:
        return {
            "error": "No public books found on that profile. Their shelves may be "
                     "private — try the manual option below.",
        }

    result = recommend(parsed["books"], exclude_titles=parsed.get("shelf_titles") or [])
    result["profile_name"] = parsed.get("name")
    result["library_size"] = parsed.get("library_size")
    result["shelf_total"] = len(parsed.get("shelf_titles") or [])
    return result


def recommend_from_titles(titles: list[str]) -> dict:
    seeds = []
    for raw in titles:
        raw = (raw or "").strip()
        if not raw:
            continue
        # allow "Title - Author", "Title by Author", or bare "Title"
        m = re.split(r"\s+by\s+| - ", raw, maxsplit=1)
        seed = {"title": m[0].strip(), "author": m[1].strip() if len(m) > 1 else "", "source": "manual"}
        seeds.append(seed)
    seeds = seeds[:8]
    if len(seeds) < 1:
        return {"error": "Add at least one book you love."}
    return recommend(seeds)


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ShelfMate/1.0"

    def log_message(self, fmt, *args):  # privacy: no URLs/query strings in logs
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args if False else ""))

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/recommend":
            self._json(404, {"error": "not found"})
            return
        ip = self.client_address[0]
        if not rate_ok(ip):
            self._json(429, {"error": "Too many requests — give it a minute."})
            return
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 10_000)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "Malformed request."})
            return
        if "url" in payload:
            result = recommend_from_profile(payload["url"])
        elif "books" in payload:
            result = recommend_from_titles(payload.get("books") or [])
        else:
            result = {"error": "Send {\"url\": ...} or {\"books\": [...]}"}
        code = 200 if "error" not in result else 400
        self._json(code, result)


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"shelfmate listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
