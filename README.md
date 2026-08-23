# ShelfMate 📚

[![CI](https://github.com/pkia/shelfmate/actions/workflows/ci.yml/badge.svg)](https://github.com/pkia/shelfmate/actions/workflows/ci.yml)

Paste a Goodreads profile link, get book recommendations. No account, no
tracking, no stored profiles — anonymous for everyone.

**Try it:** https://

## How it works

```
Goodreads profile link ──▶ Pi fetches public profile page (name, favorites)
                          ──▶ whole public shelf via RSS feed (with ratings)
                          ──▶ every shelf book a seed (favorites first)
                          ──▶ Open Library subject lookups (parallel)
                          ──▶ taste profile (subjects weighted by your ratings)
                          ──▶ candidates: subject canon + new releases (≤5y)
                          ──▶ scored: subject overlap + capped popularity + recency
                          ──▶ 12 recommendations, each with a reason
```

1. **Goodreads RSS shelf feeds are public** (`/review/list_rss/<id>`,
   100 books/page): the whole library with your own star ratings, no login
   needed. The web shelf pages (`/review/list`) are login-walled, so the
   RSS feed is the primary source; the profile page supplies the display
   name, favorites, and total library size.
2. Seed books are matched on **Open Library** to pull their subject tags.
3. A **taste profile** is built from those subjects — books you rated 5★
   count double, 1★ books barely count — with catalogue noise ("protected
   daisy", "in library", …) filtered out.
4. Candidates come from two pools: Open Library's per-subject work lists
   (the subject canon) **and** `search.json` sorted by publish date for
   recent releases (last 5 years) that carry a top taste subject. Scoring
   is subject overlap (absolute + relative floors) + an edition-count
   popularity prior **capped at 30 editions** + a recency bonus that fades
   over ~15 years — so a brand-new book can outrank a 500-edition classic
   on an equal match, but a strongly-matching classic still beats a
   weakly-matching new release. Capped at two books per author; books
   already on your shelf are excluded — no recommending what you've read.
5. Every recommendation carries a **human-readable reason**.

If a profile has no public books, ShelfMate falls back to a manual mode:
type 2–3 books you love instead.

## Running locally

```bash
python3 server.py        # stdlib only, listens on 127.0.0.1:8086
```

Or with Docker / any static host: the frontend is one file (`index.html`)
and talks to `POST /api/recommend` (`{"url": ...}` or `{"books": [...]}`).

## Tests

```bash
python3 -m pytest tests/ -q
```

19 tests: URL parsing, profile parsing (sign-in wall, empty profile,
nav-link false positive), name extraction, recommender scoring/ranking/
diversity/seed-exclusion, manual title splitting, rate limiting, caching.

## Privacy

- No accounts, no logs of profile URLs (standard access log only, no query strings).
- Profile fetches and Open Library lookups are cached on disk (7 days /
  6 hours) to be kind to upstream APIs — that's the only storage.
- Rate limited per IP (6 requests/minute) to keep the Pi healthy.

## Deployment (house style)

- `deploy/deploy.sh` — pull-based CD: fast-forward to origin/main,
  byte-compile + import gate, restart systemd service, health check,
  auto-rollback on failure.
- `deploy/shelfmate.service` — systemd unit (port 8086).
- `deploy/shelfmate-deploy.timer` — 3-minute poll timer.

## License

MIT
