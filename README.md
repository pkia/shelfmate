# ShelfMate 📚

Paste a Goodreads profile link, get book recommendations. No account, no
tracking, no stored profiles — anonymous for everyone.

**Try it:** https://

## How it works

```
Goodreads profile link ──▶ Pi fetches public profile page
                             (favorites + currently-reading)
                          ──▶ Open Library subject lookups (parallel)
                          ──▶ taste profile (weighted subjects)
                          ──▶ candidate works from top subjects
                          ──▶ scored: subject overlap + popularity
                          ──▶ 12 recommendations, each with a reason
```

1. **Goodreads profile pages are public** and fetch fine server-side
   (shelf/RSS pages are login-walled, so those aren't used).
2. Seed books are matched on **Open Library** to pull their subject tags.
3. A **taste profile** is built from those subjects (favorites weighted
   over currently-reading), with catalogue noise ("protected daisy",
   "in library", …) filtered out.
4. Candidates come from Open Library's per-subject work lists, scored on
   subject overlap (absolute + relative floors) plus an edition-count
   popularity prior, capped at two books per author.
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
