# Security audit

**Date:** 2026-10-18 · **Scope:** the deployed app (Railway), its code, dependencies and git history.
**Method:** route inventory, automated access tests, manual probing of the live site, code
search for risky patterns, and scanners (pip-audit, bandit, Django `check --deploy`, gitleaks).

## 1. Every endpoint, and who can reach it

The app has 75 URL routes: the admin's model pages plus the ones below. A test
(`config/tests/test_no_open_endpoints.py`) walks **every** route Django knows about, so a route
added later is checked automatically:

- as an **anonymous visitor**, GET and POST: everything except the allow-list below must
  redirect to the login page or refuse (401/403/404/405);
- as a **staff account with no role**: no library, user or audit page returns 200.

The test was checked by adding a fake leaking route: it failed as expected.

| Route | Anonymous | Staff, no role | Librarian / Admin |
| --- | --- | --- | --- |
| `/healthz` | `{"status": "ok"}` only (allow-listed) | same | same |
| `/<admin>/login/`, `/logout/` | login form (allow-listed); logout is POST-only | — | — |
| `/<admin>/…` model pages, autocomplete, search, history | 302 → login | 403 | per role permissions |
| `/<admin>/guide/`, dashboard | 302 → login | allowed, but shows no data panels | role-aware |
| `/assistant/ask/`, `/history/`, `/history/clear/` | 403 JSON (405 on GET for ask) | lookups list empty, no data | allow-listed lookups for the role |
| `/covers/<uuid>.<ext>` | 403 | 200 only for files that belong to a book | same |
| `/admin/` (the default path) | 404 | 404 | 404 |

Live probes on 2026-10-18 matched this table (see section 5).

## 2. Brute force, scraping and abuse

| Threat | Control | Where |
| --- | --- | --- |
| Password guessing on one account | axes: 5 failures per username+IP → 15 min lock | `AXES_LOCKOUT_PARAMETERS` |
| Credential stuffing (many usernames, one IP) | axes: 5 failures per IP → 15 min lock | same |
| Locking real staff out on purpose | never lock by username alone | same |
| Flooding the login form | 10 POSTs/min per IP → 429 | `config/ratelimit.py` |
| Scraping pages | 300 requests/min per IP → 429 | same |
| Guessing cover URLs | 128-bit random names, staff only, must belong to a book, 300/min per IP | `library/views.py` |
| Abusing the AI (cost, data mining) | 10/min and 100/day per user, 30/min per IP, $5 cap on the key, identical requests cached | `assistant/views.py` |
| Reading data through the AI | the model only picks an allow-listed lookup; the server checks the permission and runs it | `assistant/lookups.py` |
| Prompt injection | user text only in user messages; worst case is choosing another allowed lookup | tests in `assistant/tests/` |
| Spoofed client IP | `X-Real-IP` trusted only in production, where Railway's edge sets it; validated | `config/client_ip.py` |
| Checking whether a record exists | users without permission get 403 whether the object exists or not (Django admin checks permission first) | admin |
| Finding out which usernames exist | no password-reset or sign-up flow; failed logins show one generic message; Django runs the password hasher for unknown users too | Django auth |

## 3. Code checks

- No raw SQL apart from the health check's constant `SELECT 1`; no `.extra()`, `RawSQL`,
  `mark_safe`, `|safe`, `csrf_exempt`, `eval` or `innerHTML` (search on 2026-10-18).
- Every state change is a POST with CSRF; the suspend button's GET only shows a confirmation.
- Uploads: allow-listed types, content checked with Pillow, 2 MB, random names, private bucket.
- No user-supplied URL is ever fetched by the server (no SSRF surface). The login `next=`
  parameter is validated by Django (no open redirect).

## 4. Configuration and headers (live)

`Content-Security-Policy` (`default-src 'self'`, `img-src 'self' data:`, `frame-ancestors
'none'`, `object-src 'none'`), `Strict-Transport-Security: max-age=31536000; includeSubDomains`,
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
`Cross-Origin-Opener-Policy: same-origin`, `Permissions-Policy` (camera, microphone, location,
payment, USB off). Cookies: `Secure`, `HttpOnly`, `SameSite=Lax`; sessions in the database,
8 hours. HTTP redirects to HTTPS. `DEBUG=False`; error pages show no details. The `Server`
header is Railway's edge, not the app server.

**Single origin:** the browser only talks to the app's own URL. Cover images used to be signed
links to the storage provider (which exposed its project ID); they are now streamed through
`/covers/…`. Checked live: every `src`/`href` on the book list is same-origin.

## 5. Scans and live verification (2026-10-18)

| Check | Result |
| --- | --- |
| `pip-audit -r requirements.txt` | no known vulnerabilities |
| `bandit -r library assistant config` | no issues |
| `manage.py check --deploy --fail-level WARNING` (prod settings) | no issues (HSTS preload silenced on purpose) |
| gitleaks over the full git history (27 commits) | no leaks |
| Anonymous probes of `/assistant/*`, `/covers/*`, model pages, autocomplete, search | 403/405 or 302 → login |
| 34 rapid requests to `/assistant/history/` | 429 with `Retry-After: 60` after the per-IP limit |
| Cover fetch as librarian | 200 same-origin, `private, max-age=31536000, immutable`; revalidation 304 |
| Lockout log records the visitor's IP | yes (public address, not the `100.64.x.x` proxy) |

## 6. Findings

**Fixed in this audit**

| # | Finding | Fix |
| --- | --- | --- |
| 1 | Cover image URLs exposed the storage provider and its project ID | covers served same-origin; CSP `img-src 'self'` |
| 2 | No per-IP request limits outside login and the assistant | `RateLimitMiddleware` |
| 3 | Lockout only per username+IP, so trying many usernames from one IP was not stopped | added IP-wide lockout |
| 4 | CSRF failures showed Django's long explanation page | short 403 (JSON for the assistant) |
| 5 | No `Permissions-Policy` header | added |

Fixed earlier: the lockout counted Railway's proxy address instead of the visitor
(`config/client_ip.py`); AI answers could not reach member data (server-side lookups).

**Accepted, with reasons**

- `script-src 'unsafe-eval'`: required by Alpine.js in the Unfold theme. Scripts still load only
  from our origin; an attacker would first need an HTML-injection bug, and Django escapes output.
- `style-src 'unsafe-inline'`: the admin uses inline style attributes; style injection is low risk.
- The broad per-IP limit is per worker process (2 workers → up to 2× looser); login and
  assistant limits are shared in the database cache. Redis would make all of them exact.
- Cache-based counters are not atomic; a burst can slip one or two requests past a limit.
- Ask AI answers (which may include member names) stay in the login session row for up to
  8 hours; only that user can read them and logout deletes them.
- Access logs contain request paths, including admin search terms.
- The admin path is not secret on the demo (the home page redirects to it); that is obscurity
  anyway, not a control.
- **The two demo logins are published in the README** so reviewers can log in without an
  email round-trip (owner's decision). They protect only demo data. `admin_demo` is not a
  superuser, so it cannot grant itself more rights, and the lockout and rate limits still apply.
  Anyone could change the demo data, and `seed_demo --reset --allow-production` restores it.
