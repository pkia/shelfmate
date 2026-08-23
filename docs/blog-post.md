# Book recommendations without the Goodreads API

Goodreads has been winding down its public API for years. New developer keys stopped being issued, the old ones aged out, and the standard advice became "export your CSV and import it somewhere else." That works, but it's a chore, and it means handing your reading history to yet another account on yet another service.

I wanted the opposite thing: paste a link, get book recommendations. No account, no import, no tracking. So I built [ShelfMate](https://github.com/pkia/shelfmate). It's one Python file and one HTML page, running on a Raspberry Pi at home, and it works entirely off public data.

This is the story of how it works, including the lucky discovery at the center of it.

## The feed Goodreads forgot to wall off

The obvious data source, the shelf pages at `/review/list`, is login-walled. Fetch one while logged out and you get a sign-in screen. The profile page is public, but it only exposes a few favorites and whatever you're currently reading. Not much of a taste profile.

Then I found the RSS feed. Every shelf has one, at `/review/list_rss/<user-id>`, and it is completely public. Ask for it with no session and you get the whole shelf: title, author, the rating the user gave each book, read dates, page counts. A hundred items per page, a `shelf=` parameter if you only want `read` or `to-read`. It looks like a leftover from the era when every site wanted widgets embedded everywhere, and it is the entire foundation of the app. The login wall guards the HTML; the machine-readable version waves you in.

The obvious flip side deserves saying out loud: if my app can read your shelf without logging in, so can anyone or anything else, and that's been true for years. Within the app I keep this as clean as I can. Shelf fetches are cached a few hours to stay off Goodreads' nerves, then dropped. Nothing is stored about who asked. But the real takeaway is between you and Goodreads' idea of privacy.

## How the recommendations work

The pipeline is boring on purpose:

1. Read the whole shelf from the RSS feed, following pagination, up to 500 books.
2. Look each book up on [Open Library](https://openlibrary.org) for its subject tags and cover. These lookups are cached for seven days, because Open Library asks you to be gentle and there's no reason to ask twice.
3. Build a taste profile from all those subjects, weighted by how much you liked the book. Favorites count for extra, and a shelf book's weight scales with the rating you gave it. A five-star book shouts. A two-star book mumbles.
4. Pull candidate books from Open Library's "similar works" links and from the subjects themselves.
5. Score candidates by subject overlap with your profile, throw out anything already anywhere on your shelf, and keep the top twelve, each with a one-line reason.

Two details matter more than they sound. First, the exclusion check runs against the entire library, not just favorites. Big-name services have recommended me books I finished last month. Second, ratings shape the profile, so a shelf full of grudging three-stars reads differently than a shelf of enthusiasms, even when the titles overlap.

Open Library's subject tags are a mess in a lovable way. "New York Times bestseller" is apparently a subject. "Fiction, women" is a subject. Individually they're noise, but a few hundred of them averaged over a whole shelf turn out to carry real signal about what someone reads.

## The parts that took longer than they should have

**Title matching.** Matching free-text shelf titles against Open Library works about seven times in ten on a real shelf. You can push that up with fuzzier search, but you start confusing reprints and translations, so I stopped. The unmatched books get reported in the results ("a few titles couldn't be matched") instead of silently vanishing. A recommender that lies about how much it read is worse than one that admits it skimmed.

**The cap I shouldn't have shipped.** The first version analysed only the top 30 books by weight, a speed guard from when every lookup was a fresh network call. Once the seven-day cache existed, the cap was just throwing away signal, so I removed it. A shelf of about 150 books takes roughly 45 seconds the first time it's ever analysed, and 0.04 seconds ever after. Lesson repeated for the hundredth time in my career: cache first, cap never.

**The wait.** Forty-five seconds is an eternity on the web, and a fake spinner makes it worse. The loading screen shows a little shelf filling up with book spines, but the parts that matter are honest: the progress bar tracks what the server is actually doing (fetching the shelf, matching titles, reading subjects), the elapsed timer is real, and the estimate up front says "up to about 45 seconds the first time." Once the honest phases run out, the status lines hand over to a librarian who is "definitely not judging your five-star picks." People forgive a wait when you tell them how long it is.

## A Raspberry Pi in a cupboard

The server is Python standard library only. No framework, no dependencies, nothing to pip install; it runs on the bare system Python of a Pi that has better things to do than babysit a package manager. Tests are plain unittest, 25 of them. CI runs pytest and ruff on Python 3.11 and 3.13.

Deploys are pull-based: a systemd timer on the Pi fetches from GitHub every few minutes, health-checks the new version, and rolls back if it doesn't serve. It's exposed to the internet through Tailscale Funnel, so there's no port open on the router and no dynamic DNS ritual. The whole arrangement idles at approximately nothing, which feels correct for a tool that does one small thing.

## Where it's weak

Content-based recommenders can only suggest things adjacent to what you already read, and this one is no exception. There's no collaborative filtering, so don't expect it to surface the blind-spot book that changes your life. It's English-centric, because Open Library is. And my test shelf, heavy on modern fiction, came back thick with Austens and Brontës. I have theories about why (older books carry denser subject metadata), but I haven't proven any of them. The picks are sound, just more classic than the shelf warranted.

None of that bothers me much. The app does what it set out to do: paste a profile link, wait a well-labeled moment, get twelve books with reasons, hand over no personal information in the process.

Paste your profile link and see what your shelf says about you.
