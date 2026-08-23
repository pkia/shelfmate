"""ShelfMate tests: URL parsing, profile extraction, recommender, rate limiting.

Network is never touched — Open Library calls are monkeypatched, and the
Goodreads fixtures are synthetic pages built from the observed profile
markup (img alt="Title by Author" grids + bookTitle links).
"""
import sys
import time
import types
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import server  # noqa: E402


# ---------------------------------------------------------------- fixtures

PROFILE_HTML = """<!DOCTYPE html><html><head><title>Otis (otis) (1,652 books)</title></head><body>
<div class="bigBoxContent containerWithHeaderContent">
  <div class="imgGrid" style="text-align:center">
    <a href="/book/show/10365.Where_the_Red_Fern_Grows"><img alt="Where the Red Fern Grows by Wilson Rawls" title="x" class="inlineblock" src="https://example/10365.jpg"></a>
    <a href="/book/show/4099.The_Pragmatic_Programmer"><img alt="The Pragmatic Programmer by Andy Hunt" title="x" class="inlineblock" src="https://example/4099.jpg"></a>
    <a href="/book/show/2262.No_Ordinary_Moments"><img alt="No Ordinary Moments by Dan Millman" title="x" class="inlineblock" src="https://example/2262.jpg"></a>
  </div>
</div>
<div class="userShelves">
  <a class="bookTitle" href="/book/show/32074489-solve-for-happy">Solve for Happy: Engineer Your Path to Joy</a>
  <a class="bookTitle" href="/book/show/236047909-europa">Europa (City Spies #7)</a>
</div>
</body></html>"""

EMPTY_PROFILE_HTML = """<html><head><title>Dee (proudenglishmajor) (118 books)</title></head>
<body><div>profile with private shelves</div></body></html>"""

SIGNIN_HTML = """<html><head><title>Sign in</title></head><body>
Sign in to Goodreads</body></html>"""


# ------------------------------------------------------------ URL parsing

def test_parse_profile_url_shapes():
    ok = [
        "https://www.goodreads.com/user/show/12345",
        "https://www.goodreads.com/user/show/12345-jane-doe",
        "https://www.goodreads.com/user/show/12345?shelf=read",
        "http://goodreads.com/user/show/7",
        "www.goodreads.com/user/show/7",
        "goodreads.com/user/show/12345-jane",
    ]
    for u in ok:
        assert server.parse_profile_url(u), u
    assert server.parse_profile_url("https://www.goodreads.com/user/show/12345-jane-doe") == \
        "https://www.goodreads.com/user/show/12345"


def test_parse_profile_url_rejects():
    bad = [
        "https://www.goodreads.com/book/show/10365",       # a book, not a profile
        "https://www.goodreads.com/review/list/123",       # shelf page (login-walled)
        "https://evil.com/user/show/1",
        "https://www.goodreads.com/author/show/123",
        "not a url at all",
        "",
        None,
        "https://www.goodreads.com/user/show/",            # no id
    ]
    for u in bad:
        assert server.parse_profile_url(u) is None, u


# ------------------------------------------------------- profile parsing

def test_extract_name_variants():
    import importlib
    importlib.reload(server)
    cases = [
        ("Otis Chandler (otis) - San Francisco, CA (1,652 books)", "Otis Chandler"),
        ("Jane Doe (412 books)", "Jane Doe"),
        ("Dee (proudenglishmajor) (118 books)", "Dee"),
        ("Plain Name (no-books-count)", "Plain Name"),
    ]
    for title, want in cases:
        html = f"<html><title>{title}</title></html>"
        assert server.extract_name(html) == want, (title, server.extract_name(html))


def test_extract_favorites_and_reading():
    parsed = server.extract_books(PROFILE_HTML)
    titles = [b["title"] for b in parsed["books"]]
    assert "Where the Red Fern Grows" in titles
    assert "The Pragmatic Programmer" in titles
    assert "Solve for Happy: Engineer Your Path to Joy" in titles
    fav = next(b for b in parsed["books"] if b["title"] == "Where the Red Fern Grows")
    assert fav["author"] == "Wilson Rawls"
    assert fav["source"] == "favorite"
    assert parsed["name"] == "Otis"
    assert parsed["signin"] is False


def test_extract_dedupes_across_sections():
    html = PROFILE_HTML.replace(
        '<a class="bookTitle" href="/book/show/32074489-solve-for-happy">',
        '<a class="bookTitle" href="/book/show/32074489-solve-for-happy">'
    )
    # add a reading link whose title matches a favorite
    html = html.replace("</body>", '<a class="bookTitle" href="/book/show/999-x">Where the Red Fern Grows</a></body>')
    parsed = server.extract_books(html)
    assert sum(1 for b in parsed["books"] if b["title"] == "Where the Red Fern Grows") == 1


def test_extract_signin_wall():
    parsed = server.extract_books(SIGNIN_HTML)
    assert parsed["signin"] is True
    assert parsed["books"] == []


def test_extract_real_profile_not_signin():
    # "Sign in to Goodreads" sits in every page's nav — a real profile that
    # contains that string must NOT be mistaken for the login wall.
    html = PROFILE_HTML.replace(
        "<body>", '<body><a href="/user/sign_in">Sign in to Goodreads</a>')
    parsed = server.extract_books(html)
    assert parsed["signin"] is False
    assert parsed["books"], "nav sign-in link must not hide a real profile"


def test_extract_empty_profile():
    parsed = server.extract_books(EMPTY_PROFILE_HTML)
    assert parsed["signin"] is False
    assert parsed["books"] == []
    assert parsed["name"] == "Dee"


def test_numeric_title_1984_kept():
    html = '<img alt="1984 by George Orwell" class="inlineblock">'
    books = server._alt_books(html)
    assert books and books[0]["title"] == "1984"


# ------------------------------------------------------- RSS shelf feed

RSS_PAGE = """<?xml version="1.0"?>
<rss><channel><title>Jane's bookshelf: all</title>
<item><guid><![CDATA[x1]]></guid><title>The Sea, The Sea</title>
  <author_name>Iris Murdoch</author_name><user_rating>5</user_rating></item>
<item><guid><![CDATA[x2]]></guid><title><![CDATA[Wuthering Heights]]></title>
  <author_name>Emily Brontë</author_name><user_rating>0</user_rating></item>
<item><guid><![CDATA[x3]]></guid><title>The Sea, The Sea</title>
  <author_name>Iris Murdoch</author_name><user_rating>5</user_rating></item>
<item><guid><![CDATA[x4]]></guid><title>Writers &amp; Lovers</title>
  <author_name>Lily King</author_name><user_rating>3</user_rating></item>
</channel></rss>"""


def test_parse_rss_titles_authors_ratings():
    books = server.parse_rss(RSS_PAGE)
    assert len(books) == 3                      # dedupe: "The Sea, The Sea" twice
    assert books[0] == {"title": "The Sea, The Sea", "author": "Iris Murdoch",
                        "rating": "5", "source": "shelf"}
    assert books[1]["title"] == "Wuthering Heights"      # CDATA stripped
    assert books[2]["title"] == "Writers & Lovers"       # entity unescaped
    assert books[2]["rating"] == "3"


def test_fetch_shelf_rss_pagination(monkeypatch):
    pages = {1: server.parse_rss(RSS_PAGE),                          # 3 < 100 → stop
             2: [ {"title": "Never", "author": "x", "rating": "4", "source": "shelf"} ]}
    calls = []
    def fake_fetch(url, timeout=15, max_bytes=0):
        calls.append(url)
        page = 1 if "page=1" in url or "page=" not in url else 2
        return ("<rss><channel>" + "".join(
            f"<item><title>{b['title']}</title><author_name>{b['author']}</author_name>"
            f"<user_rating>{b['rating']}</user_rating></item>" for b in pages[page])
            + "</channel></rss>").encode()
    monkeypatch.setattr(server, "fetch_url", fake_fetch)
    books = server.fetch_shelf_rss("12345")
    assert len(books) == 3 and books[0]["title"] == "The Sea, The Sea"
    assert len(calls) == 1, "short first page must stop pagination"


def test_pick_seeds_favorites_and_ratings_first():
    shelf = [
        {"title": "Unrated Recent", "author": "", "rating": "0", "source": "shelf"},
        {"title": "Meh 1-star", "author": "", "rating": "1", "source": "shelf"},
        {"title": "Loved 5-star", "author": "", "rating": "5", "source": "shelf"},
    ]
    favorites = [{"title": "Fav Pick", "author": "A", "source": "favorite"}]
    seeds = server._pick_seeds(shelf, favorites, cap=10)
    assert [s["title"] for s in seeds] == ["Fav Pick", "Loved 5-star", "Meh 1-star", "Unrated Recent"]
    capped = server._pick_seeds(shelf, favorites, cap=2)
    assert [s["title"] for s in capped] == ["Fav Pick", "Loved 5-star"]


def test_seed_weight_scales_with_rating():
    assert server.seed_weight({"source": "favorite"}) == 1.5
    assert server.seed_weight({"source": "shelf", "rating": "5"}) == 2.0
    assert server.seed_weight({"source": "shelf", "rating": "1"}) == 0.8
    assert server.seed_weight({"source": "shelf", "rating": "0"}) == 1.0
    assert server.seed_weight({"source": "shelf", "rating": ""}) == 1.0
    assert server.seed_weight({"source": "manual"}) == 1.0


def test_extract_library_size():
    html = "<title>Jane Doe (156 books)</title>"
    assert server.extract_library_size(html) == 156
    html = "<title>Otis (1,652 books) — San Francisco</title>"
    assert server.extract_library_size(html) == 1652
    assert server.extract_library_size("<title>No count here</title>") is None


def test_recommend_excludes_whole_shelf(monkeypatch):
    # A book on the user's shelf (but not a seed) must not be recommended.
    doc = {"key": "/works/OL1W", "title": "Seed Book", "author_name": ["A"],
           "subject": ["love", "grief"], "edition_count": 9, "cover_id": 1}
    shelf_book = {"key": "/works/OL2W", "title": "Already On Shelf", "author_name": ["A"],
                  "subject": ["love", "grief"], "edition_count": 9, "cover_id": 2}
    _fake_ol(monkeypatch,
             {"Seed Book": doc, "Already On Shelf": shelf_book},
             {"love": [{"title": "Already On Shelf", "authors": [{"name": "A"}],
                        "edition_count": 9, "cover_id": 2, "key": "/works/OL2W"},
                       {"title": "New Suggestion", "authors": [{"name": "B"}],
                        "edition_count": 9, "cover_id": 3, "key": "/works/OL3W",
                        "subject": ["love"]}],
              "grief": []})
    result = server.recommend([{"title": "Seed Book", "source": "manual"}],
                              exclude_titles=["Already On Shelf"])
    titles = [r["title"] for r in result["recs"]]
    assert "Already On Shelf" not in titles
    assert "New Suggestion" in titles


# ---------------------------------------------------------- normalisation

def test_norm_title_strips_series():
    # parenthesised series info is dropped entirely
    assert server._norm_title("Europa (City Spies #7)") == "europa"
    assert server._norm_title("The Name of the Wind!") == "the name of the wind"


# ------------------------------------------------------- recommender core

DOC = {
    "key": "/works/OL1W",
    "title": "The Name of the Wind",
    "author_name": ["Patrick Rothfuss"],
    "subject": ["fantasy", "epic fiction", "protected daisy", "in library", "magic"],
    "edition_count": 120,
    "cover_i": 1,
}


def _fake_ol(monkeypatch, docs_by_title, subject_works, recent=None):
    def find(title, author=""):
        for t, d in docs_by_title.items():
            if server._norm_title(t) == server._norm_title(title):
                return d
        return None

    monkeypatch.setattr(server, "ol_find_work", find)
    monkeypatch.setattr(server, "ol_subject_works", lambda s, limit=30: subject_works.get(s, []))
    monkeypatch.setattr(server, "ol_recent_by_subject",
                        lambda s, limit=15: (recent or {}).get(s, []))


def test_recommend_ranks_and_reasons(monkeypatch):
    docs = {
        "The Name of the Wind": dict(DOC),
        "Mistborn": {
            "key": "/works/OL2W", "title": "Mistborn", "author_name": ["Brandon Sanderson"],
            "subject": ["fantasy", "magic", "epic fiction"], "edition_count": 200, "cover_i": 2,
        },
    }
    subject_works = {
        "fantasy": [
            {"title": "Mistborn", "authors": [{"name": "Brandon Sanderson"}],
             "edition_count": 200, "cover_id": 2, "key": "/works/OL2W"},
            {"title": "The Wizard of Earthsea", "authors": [{"name": "Ursula K. Le Guin"}],
             "edition_count": 90, "cover_id": 3, "key": "/works/OL3W"},
        ],
        "magic": [
            {"title": "Mistborn", "authors": [{"name": "Brandon Sanderson"}],
             "edition_count": 200, "cover_id": 2, "key": "/works/OL2W"},
            {"title": "The Wizard of Earthsea", "authors": [{"name": "Ursula K. Le Guin"}],
             "edition_count": 90, "cover_id": 3, "key": "/works/OL3W"},
        ],
        "epic fiction": [
            {"title": "Mistborn", "authors": [{"name": "Brandon Sanderson"}],
             "edition_count": 200, "cover_id": 2, "key": "/works/OL2W"},
            {"title": "A Game of Thrones", "authors": [{"name": "George R. R. Martin"}],
             "edition_count": 136, "cover_id": 4, "key": "/works/OL4W"},
        ],
        # a book that appears ONLY in a weak single-subject position:
        "storytellers": [
            {"title": "A Totally Unrelated Cookbook", "authors": [{"name": "Someone"}],
             "edition_count": 3, "cover_id": None, "key": "/works/OL9W"},
        ],
    }
    _fake_ol(monkeypatch, docs, subject_works)
    result = server.recommend([{"title": "The Name of the Wind", "author": "Patrick Rothfuss", "source": "favorite"}])
    assert result["reason"] == "ok"
    titles = [r["title"] for r in result["recs"]]
    assert "Mistborn" in titles           # shares 3 subjects -> strongest
    assert titles[0] == "Mistborn"
    assert "The Name of the Wind" not in titles  # seeds never recommended
    assert "A Totally Unrelated Cookbook" not in titles
    mist = next(r for r in result["recs"] if r["title"] == "Mistborn")
    assert "fantasy" in mist["why"] or "magic" in mist["why"] or "epic" in mist["why"]
    assert all(r["why"] for r in result["recs"])


def test_recommend_no_ol_match(monkeypatch):
    _fake_ol(monkeypatch, {}, {})
    result = server.recommend([{"title": "Some Obscure Book", "source": "manual"}])
    assert result["reason"] == "no_match"
    assert result["unmatched"] == ["Some Obscure Book"]


def test_recommend_diversifies_authors(monkeypatch):
    doc = {"key": "/works/OL1W", "title": "Seed Book", "author_name": ["X"],
           "subject": ["fantasy"], "edition_count": 50}
    same_author = lambda t, n: {  # noqa: E731
        "title": t, "authors": [{"name": "Prolific Author"}],
        "edition_count": 100 + n, "cover_id": None, "key": f"/works/OL{n}W",
    }
    _fake_ol(monkeypatch, {"Seed Book": doc},
             {"fantasy": [same_author(f"Book {i}", i) for i in range(10)]})
    result = server.recommend([{"title": "Seed Book", "source": "manual"}])
    assert len(result["recs"]) == 2  # author cap = 2
    assert all(r["author"] == "Prolific Author" for r in result["recs"])


def test_seed_never_recommended_even_fuzzy(monkeypatch):
    doc = dict(DOC)
    _fake_ol(monkeypatch, {"The Name of the Wind": doc},
             {"fantasy": [{"title": "The Name of the Wind!", "authors": [{"name": "P. Rothfuss"}],
                           "edition_count": 10, "cover_id": None, "key": "/works/OL5W"}]})
    result = server.recommend([{"title": "The Name of the Wind", "source": "manual"}])
    assert result["recs"] == []


# ------------------------------------------------------ recency + ranking

def test_recency_bonus_fades():
    year_now = server._current_year()
    assert server._recency_bonus(year_now) == server.RECENT_BONUS
    assert 0 < server._recency_bonus(year_now - 7) < server.RECENT_BONUS
    assert server._recency_bonus(year_now - 15) == 0.0
    assert server._recency_bonus(year_now - 90) == 0.0
    assert server._recency_bonus(None) == 0.0


def test_new_release_outranks_edition_giant(monkeypatch):
    """The bug this fixes: only classics ever surfaced.

    A new release matching MORE taste subjects must outrank the edition
    giant — previously the 500-edition prior made that impossible.
    """
    doc = dict(DOC)  # seed: fantasy + magic + epic fiction
    year_now = server._current_year()
    giant = {  # /subjects canon pick: 500 editions, matches one subject
        "title": "Wuthering Heights", "authors": [{"name": "Emily Brontë"}],
        "edition_count": 500, "cover_id": 7, "key": "/works/OL7W",
        "first_publish_year": 1850,
    }
    fresh = {  # search.json sort=new pick: 2 editions, matches two subjects
        "title": "The Tainted Cup", "authors": [{"name": "Robert Jackson Bennett"}],
        "edition_count": 2, "cover_id": 8, "key": "/works/OL8W",
        "first_publish_year": year_now,
        "subject": ["fantasy", "magic"],
    }
    _fake_ol(monkeypatch, {"The Name of the Wind": doc},
             {"fantasy": [giant]},
             recent={"fantasy": [fresh]})
    result = server.recommend([{"title": "The Name of the Wind", "source": "manual"}])
    titles = [r["title"] for r in result["recs"]]
    assert titles == ["The Tainted Cup", "Wuthering Heights"], titles
    top = result["recs"][0]
    assert top["first_publish_year"] == year_now
    assert top["why"].startswith(f"New {year_now} release")


def test_recency_beats_edition_count_on_equal_match(monkeypatch):
    """Same taste overlap: capped prior + recency beat raw edition count."""
    doc = dict(DOC)
    year_now = server._current_year()
    def book(title, key, editions, year):
        return {"title": title, "authors": [{"name": "X"}], "edition_count": editions,
                "cover_id": None, "key": key, "first_publish_year": year}
    giant = book("Edition Giant", "/works/OL7W", 500, 1850)
    fresh = book("Fresh Match", "/works/OL8W", 2, year_now)
    _fake_ol(monkeypatch, {"The Name of the Wind": doc},
             {"fantasy": [giant], "magic": [giant], "epic fiction": [giant]},
             recent={"fantasy": [fresh], "magic": [fresh], "epic fiction": [fresh]})
    result = server.recommend([{"title": "The Name of the Wind", "source": "manual"}])
    titles = [r["title"] for r in result["recs"]]
    assert titles[0] == "Fresh Match", titles


def test_stronger_taste_match_still_wins_over_recency(monkeypatch):
    """Recency boosts, it doesn't trump: 3-subject classic beats 2-subject new."""
    doc = dict(DOC)
    year_now = server._current_year()
    strong = {
        "title": "Deep Match Classic", "authors": [{"name": "X"}],
        "edition_count": 60, "cover_id": None, "key": "/works/OL7W",
        "first_publish_year": 1960,
    }
    fresh = {
        "title": "Shallow New Book", "authors": [{"name": "Y"}],
        "edition_count": 2, "cover_id": None, "key": "/works/OL8W",
        "first_publish_year": year_now,
        "subject": ["fantasy"],
    }
    _fake_ol(monkeypatch, {"The Name of the Wind": doc},
             {"fantasy": [strong], "magic": [strong], "epic fiction": [strong]},
             recent={"fantasy": [fresh]})
    result = server.recommend([{"title": "The Name of the Wind", "source": "manual"}])
    titles = [r["title"] for r in result["recs"]]
    assert titles[0] == "Deep Match Classic", titles
    assert "Shallow New Book" in titles  # but the new book still surfaces


def test_recency_gives_subject_credit_from_search_doc(monkeypatch):
    """search.json docs carry their own subjects — taste overlap counts them."""
    doc = dict(DOC)
    fresh = {
        "title": "New Fantasy Book", "authors": [{"name": "Someone"}],
        "edition_count": 1, "cover_id": None, "key": "/works/OL8W",
        "first_publish_year": server._current_year(),
        "subject": ["magic", "dragons"],   # only 'magic' was searched
    }
    _fake_ol(monkeypatch, {"The Name of the Wind": doc},
             {}, recent={"fantasy": [fresh]})
    result = server.recommend([{"title": "The Name of the Wind", "source": "manual"}])
    rec = next(r for r in result["recs"] if r["title"] == "New Fantasy Book")
    assert "magic" in rec["subjects"]


def test_ancient_book_no_recency_credit(monkeypatch):
    """An undated or ancient work gets no floor help and no bonus."""
    doc = dict(DOC)
    old = {
        "title": "Some 1920 Classic", "authors": [{"name": "X"}],
        "edition_count": 40, "cover_id": None, "key": "/works/OL7W",
        "first_publish_year": 1920,
    }
    _fake_ol(monkeypatch, {"The Name of the Wind": doc},
             {"fantasy": [old], "magic": [old], "epic fiction": [old]})
    result = server.recommend([{"title": "The Name of the Wind", "source": "manual"}])
    assert [r["title"] for r in result["recs"]] == ["Some 1920 Classic"]
    assert "New" not in result["recs"][0]["why"]


def test_subject_stoplist():
    assert server._clean_subjects(["Protected Daisy", "In Library", "fantasy", ""]) == ["fantasy"]


# -------------------------------------------------------- title splitting

def test_manual_title_author_split(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ol_find_work",
                        lambda t, a="": (calls.append((t, a)) or None))
    result = server.recommend_from_titles(
        ["Beloved by Toni Morrison", "Dune - Frank Herbert", "  Hamlet  "])
    # seed lookups run in a thread pool -> arrival order varies; compare as sets
    assert sorted(calls) == [("Beloved", "Toni Morrison"), ("Dune", "Frank Herbert"), ("Hamlet", "")]
    assert result["reason"] == "no_match"


def test_manual_empty():
    assert "error" in server.recommend_from_titles([])
    assert "error" in server.recommend_from_titles(["", "  "])


# ---------------------------------------------------------- rate limiting

def test_rate_limit():
    server._rate.clear()
    ip = "1.2.3.4"
    assert all(server.rate_ok(ip) for _ in range(server.RATE_MAX))
    assert not server.rate_ok(ip)
    assert server.rate_ok("5.6.7.8")  # other IPs unaffected


# ------------------------------------------------------------- cache TTL

def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE_DIR", str(tmp_path))
    server._cache_put("k", {"a": 1})
    assert server._cache_get("k", ttl=60) == {"a": 1}
    assert server._cache_get("k", ttl=-1) is None  # expired
