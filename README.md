# News Digest Autopilot

Free, cloud-only news digest. At the times you choose, it reads everything your news sources posted
since the previous digest (RSS feeds, X profiles, Instagram profiles, Facebook pages via FetchRSS),
transcribes videos, groups the same story from different outlets, sorts stories under your topic
headings, shortens them in English without dropping any fact, and emails the digest to you. Every digest
is also on a mobile-friendly page in the dashboard.

Nothing runs on your computer. Everything uses free tiers that don't need a card.

## How it runs

```
Cloudflare Worker (free)                        GitHub Actions (free, public repo)
- dashboard with login code                     - reads RSS / FetchRSS feeds
- every 5 min: starts a collection run    --->  - reads X profiles (spare-account cookies)
  shortly before each delivery time             - reads Instagram via the Worker (Meta Business Discovery)
- stores digests and settings (D1)        <---  - transcribes videos (faster-whisper on CPU)
- emails the digest at the delivery time        - Gemini (free) groups, sorts and writes the stories
  (Gmail through Composio)
```

## Dashboard

Open the Worker URL and log in with your login code.

- **Digests**: every delivery, readable in the browser. "Email it now" resends one.
- **Sources**: add websites/RSS feeds, X profiles, Instagram profiles and Facebook pages (as FetchRSS links).
  Turn sources on/off, test them, edit or delete them.
  **Research prompts** (same tab): write a request such as "Latest on Pakistan's IMF talks"; each run the AI
  turns it into Google News searches for the digest's time window, keeps only matching articles, reads them
  and merges them into the digest (optionally under a heading you pick).
- **Topics**: add, rename, reorder and delete the headings stories are sorted under.
- **Writing**: digest title, language, paragraph or bullets, length, the writing prompt and extra sorting rules.
- **Schedule**: number of deliveries per day and the time of each, time zone, and which posts each digest
  covers ("since the previous digest" or "the last X hours").
- **Delivery**: email recipients, subject, test email.
- **Connections**: X cookies, Instagram reading, system status.
- **Account**: change the login code, run history with GitHub logs.

The switch at the top turns scheduled digests on or off. **Run now** collects immediately and emails the
result; **Preview** does the same without email.

## Sources and their limits

| Source | How it is read | Notes |
|---|---|---|
| Websites / RSS | the site's RSS feed | most reliable |
| Facebook pages | FetchRSS free feed of the page | free plan limits apply; may lag |
| X profiles | twscrape with a spare X account's cookies | may break when X changes; the spare account can be locked |
| Instagram | Meta Business Discovery (official) | public Business/Creator accounts only; Reels whose video Instagram hides use the caption only |
| Research prompts | Google News RSS search + AI relevance check | free; articles are read from the publisher's site |

## Free limits (approximate)

| Part | Free allowance |
|---|---|
| Gemini text | daily cap per model; the system moves down a chain of Gemini models, then Cloudflare AI |
| GitHub Actions | unlimited minutes for a public repository (code only; keys are encrypted secrets) |
| Cloudflare Worker / D1 | 100k requests/day, 5 GB storage |
| Composio | 20k tool calls/month (one per email) |

Gemini quota is per Google Cloud project. If Naya News Autopilot uses the same key, create a new key in
a new project at aistudio.google.com and put it in `.env` as `GEMINI_API_KEY`, then run
`python scripts/setup.py --skip-worker`.

## Setup (already done once; re-run after changing keys)

1. `.env` (git-ignored) holds the keys. Missing `GEMINI_API_KEY`, `GH_TOKEN` and `DASHBOARD_PASSWORD` are
   copied from `../Project14/.env`; Composio values come from the Composio CLI login.
2. `cd worker && npm install && npx wrangler login`
3. `.venv/bin/python scripts/setup.py` creates the D1 database, deploys the Worker, sets its secrets,
   copies the Instagram reading connection from Naya News Autopilot, creates the GitHub repository,
   pushes the code and sets the Actions secrets.

## Forgot the login code

```bash
cd worker && npx wrangler d1 execute news-digest-db --remote --command "DELETE FROM settings WHERE key = 'panel_password_hash'"
```

Then log in with `DASHBOARD_PASSWORD` from `.env` and set a new code in Account.
