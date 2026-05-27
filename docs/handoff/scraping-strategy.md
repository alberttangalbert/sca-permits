# Scraping strategy

San Carlos is a **JSON REST API behind an AngularJS SPA** (Tyler EnerGov CSS) —
scrapeable with plain `requests` + `json`, no Selenium, no BeautifulSoup for the
core data. This is **simpler** than either Accela city. The strategy is mostly
"be a well-behaved API client."

## The two calls

| Step | Call | Notes |
|---|---|---|
| 0 search | `POST …/api/energov/search/search` (⚠️ VERIFY) | paged JSON; cache each page |
| 2 detail | `GET …/api/energov/permit/permit?id=<GUID>` (+ sub-resources) | stateless, parallel-safe |

Both run in a single `requests.Session()` for connection reuse and to carry any
cookies the SPA bootstrap sets — but unlike Accela, **no session-held record
context is required**, so you are not forced into one-session-per-record.

## Anti-bot posture (expected: much lighter than Accela)

- **No Cloudflare expected.** Accela's `aca-prod.accela.com` is Cloudflare-fronted
  and `CapDetail` flickers 1015/502 per-IP — that's why cu-permits has the whole
  `MIN_CF_COOLDOWN_SECONDS` cooldown gate, `cf_probe.py` preflight, flicker
  requeue, and abort-on-hard-block. **Tyler's `tylerhost.net` is a different
  host with no Cloudflare challenge observed.** ⚠️ Confirm in recon — but plan
  on **not** needing the CF machinery. Don't port it speculatively; add
  rate-limit handling only if you actually see 429/503.
- **Watch for these instead** (EnerGov/Tyler specifics — ⚠️ VERIFY):
  - an **anti-forgery / `RequestVerificationToken`** header the SPA may require
    on POST (grab it from the bootstrap response or a cookie if search 403s);
  - a per-IP **rate limit** surfacing as HTTP **429** — back off and retry;
  - WAF/throttle if you fire requests too fast — keep it polite.
- **Headers to send** (⚠️ confirm from the captured cURL): a real browser
  `User-Agent`, `Accept: application/json`, `Content-Type: application/json` on
  the search POST, `X-Requested-With: XMLHttpRequest`, and `Referer`/`Origin`
  pointing at the portal.

## Pacing & politeness

There's no `robots.txt` carve-out you can rely on — be conservative:

| Step | Delay | Workers |
|---|---|---|
| 0 search | 0.5–1 s between pages | 1 |
| 2 detail | 0.5–1 s between records | 1–2 |

Even though the API can probably take more, **a small city's entire corpus is
tiny** (San Carlos, post-June-2025 only — likely a few thousand records total),
so there's no need to push throughput. A polite single-threaded pass will finish
a full backfill in minutes, not hours. If you do see 429s, add exponential
backoff (1/2/4 s) and a per-run consecutive-error abort budget — the same
defensive pattern cu-permits uses for CF blocks, just triggered by 429 instead
of 1015.

## Run direct — but for a different reason than Accela

cu-permits runs step 2 **direct, never proxied** because Accela's PageMethods
need session-cookie continuity that a rotating proxy strips. San Carlos has **no
such session requirement** (stateless GETs), so the *reason* is gone — but the
*conclusion* still holds: **run direct.** A small, polite, single-IP scrape of a
public API needs no proxy, and adding one only invites the cookie/anti-forgery
breakage cu-permits documented. Don't proxy.

## Off-peak / scheduling

No diurnal CF pattern to dodge (no CF). Schedule the daily tick whenever is
convenient, but if `sca-permits` runs on the same box as the Accela cities,
**stagger the cron** so they don't all fire at once (Fremont 4:30, Santa Clara
5:30, Cupertino 6:30 Pacific — put San Carlos at e.g. 7:30). This is courtesy /
load-spreading, not CF-edge avoidance.

## No browser / headless needed

Everything is reachable via `requests`. The AngularJS SPA is just a client for
the same JSON API you're calling — you never need to render it. (If a *future*
sub-resource turns out to be SPA-only with no JSON endpoint — unlikely in
EnerGov — fall back to capturing that one call's network request, not to
driving a browser.)

## Ethics & scope guardrails

- Public records, public API, read-only — fine. Keep it that way: **no writes,
  no auth bypass, no scraping of anything behind login.** Only hit endpoints the
  anonymous SPA itself calls.
- `.env` (any future D1 creds) stays gitignored — same rule as every city repo.
