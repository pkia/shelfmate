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


def test_extract_empty_profile():
    parsed = server.extract_books(EMPTY_PROFILE_HTML)
    assert parsed["signin"] is False
    assert parsed["books"] == []
    assert parsed["name"] == "Dee"


def test_numeric_title_1984_kept():
    html = '<img alt="1984 by George Orwell" class="inlineblock">'
    books = server._alt_books(html)
    assert books and books[0]["title"] == "1984"


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


def _fake_ol(monkeypatch, docs_by_title, subject_works):
    def find(title, author=""):
        for t, d in docs_by_title.items():
            if server._norm_title(t) == server._norm_title(title):
                return d
        return None

    monkeypatch.setattr(server, "ol_find_work", find)
    monkeypatch.setattr(server, "ol_subject_works", lambda s, limit=30: subject_works.get(s, []))


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


def test_subject_stoplist():
    assert server._clean_subjects(["Protected Daisy", "In Library", "fantasy", ""]) == ["fantasy"]


# -------------------------------------------------------- title splitting

def test_manual_title_author_split(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ol_find_work",
                        lambda t, a="": (calls.append((t, a)) or None))
    result = server.recommend_from_titles(
        ["Beloved by Toni Morrison", "Dune - Frank Herbert", "  Hamlet  "])
    assert calls == [("Beloved", "Toni Morrison"), ("Dune", "Frank Herbert"), ("Hamlet", "")]
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
