# Benrey Library

A small book lending library run entirely from a customized Django admin: catalogue, members,
loans, two staff roles, covers in object storage, and an AI help widget that explains the forms.

- **Live:** https://web-production-fb1ad.up.railway.app (the home page redirects to the admin login)
- **Access:** demo accounts for both roles (`admin_demo`, `librarian_demo`) are sent by email,
  not stored in this repo. To run your own copy, see [Quick start](#quick-start).

---

## What to look at

| Role | Sees |
| --- | --- |
| **Admin** (`admin_demo`) | Everything in the library, plus Users, Groups and the login-lockout log. Can add/edit/delete books and authors, suspend members, withdraw books. |
| **Librarian** (`librarian_demo`) | Only Authors, Books, Loans, Members. Books and authors are read-only ("Select book to *view*", no Add button). Fewer book columns. Can lend, return and extend loans and register members. No delete anywhere, cannot suspend members. |

A short tour:
1. **Members** → filter "Has overdue loans" → open a member: lend a book from the
   **Lend a book** inline, see their loans (open first), follow the link to their open loans.
2. **Loans** → filter "Due: Overdue" → select some → **Mark selected loans as returned**. The
   result message says how many were returned and lists any that were skipped and why.
3. **Books** → availability badges, filters (genre, availability, publication period), search by
   title / ISBN / author, loan history inline on each book.
4. Click **? Help** (bottom right) on any form and ask, for example, "how long can I extend
   the due date?"
5. Log in as the librarian and try `/<admin>/library/book/add/` or a delete URL: both return 403.

## Architecture

```
Browser ──HTTPS──▶ Railway (gunicorn + WhiteNoise, Django 5.2)
                     │            │                    │
                     ▼            ▼                    ▼
         Supabase Postgres   Supabase Storage     OpenRouter
         (data, sessions,    (S3 API, private     (free models,
          cache, rate limit)  bucket, signed URLs) fallback chain)
```

- `library/`: the domain. `models.py` (schema, constraints, computed values),
  `services.py` (checkout / return / extend with row locks), `admin.py` (all staff UI),
  `validators.py` (ISBN, cover uploads), `management/commands/seed_demo.py`.
- `assistant/`: the help widget. `context.py` (grounding from model metadata),
  `client.py` (OpenRouter + fallback), `views.py` (endpoint, validation, rate limit,
  non-AI fallback), `static/assistant/` (widget JS/CSS).
- `config/`: settings split into `base` / `dev` / `test` / `prod`, URLs, `/healthz`.
- `docs/DEFENSE_NOTES.md`: why each piece is built the way it is, phase by phase.

## Why this stack

| Concern | Choice | Why, and what I didn't pick |
| --- | --- | --- |
| Framework | **Python 3.12 + Django 5.2 LTS** | The brief grades a *generated* admin. Django's admin is the most complete one I know: list filters, search, inlines, actions, autocomplete and per-model permissions that the admin itself enforces. Roles become data instead of `if role ==` checks. Rails + ActiveAdmin is similar, but I am faster in Python. Headless CMSs (Strapi, Payload) are more moving parts and have weaker per-action permissions. 5.2 is the LTS line. |
| Database | **Supabase Postgres** | Real managed Postgres with a free tier. The model leans on Postgres features: partial unique constraints, check constraints, `SELECT ... FOR UPDATE`. Using the same provider for storage means one less account and dashboard. |
| Object storage | **Supabase Storage via its S3 API** (`django-storages`) | Standard S3 protocol, so the provider can be swapped through env vars (R2, B2, AWS). Private bucket, signed URLs. |
| Hosting | **Railway** | Deploys from GitHub, HTTPS by default, a pre-deploy migrate step and a health check gate. Render was the alternative; Railway's pre-deploy command and health-checked deploys made it simpler. |
| Auth | Django auth, **Argon2**, DB sessions, **django-axes** | Framework auth is audited; Argon2 is memory-hard (more resistant to GPU cracking than the PBKDF2 default). DB sessions can be revoked server-side. Axes adds lockout. |
| AI | **OpenRouter** with a chain of `:free` models | One OpenAI-compatible API, many models, free tier. A free model being rate-limited is normal, so falling through to the next model is designed in. |
| Tests | pytest, pytest-django, factory_boy, time-machine | Short, readable tests; time-machine freezes "today" for overdue/fee tests. |

## Data model and business rules

`Author` ←M2M→ `Book` ←FK─ `Loan` ─FK→ `Member`. `Book.cover_image` is the upload field.

Rules (in `library/models.py` and `library/services.py`, never in views or templates):
- A loan needs a free copy (`copies_available > 0`), a book that is not withdrawn, and an
  active member with fewer than `MAX_ACTIVE_LOANS` (5) open loans and no copy of that book yet.
- Default due date is 14 days out. A loan can be extended once, by at most 7 days, and not
  once it is overdue.
- Status goes `ACTIVE → RETURNED` only; returning sets `returned_at`. "Overdue" is computed
  (`ACTIVE` and past due), as are `days_overdue` and the late fee (€0.50/day). Nothing derived
  is stored, so nothing can drift.
- Checkout locks the book row, then the member row (`SELECT ... FOR UPDATE`), so two librarians
  cannot lend out the last copy at the same moment. Tested with two real threads against
  Postgres.
- The database enforces what must always hold: due date after loan date, `returned_at` set
  if and only if status is returned, one active loan per book+member (partial unique index),
  case-insensitive unique member email, at least one copy.

## Roles and permissions

Roles are Django groups created by a data migration (`library/migrations/0002_role_groups.py`),
so they exist in every environment. Everything is enforced server-side by the admin's permission
checks; nothing is just hidden with CSS.

| | Admin | Librarian |
| --- | --- | --- |
| Books, Authors | add / change / delete | view only (read-only form, 403 on add/change POST) |
| Members | add / change / delete, suspend & reactivate | add / change; status field locked, no suspend action |
| Loans | everything | lend, return, extend (no delete) |
| Users, Groups, lockout log | manage (only a superuser can grant superuser or per-user permissions) | not visible |

## Security

- Passwords: Argon2, validators (min 12 chars, common, numeric, similarity). Demo passwords
  come from env vars only.
- Sessions: DB-backed, 8 hours, `HttpOnly`, `Secure`, `SameSite=Lax`, rotated at login,
  deleted at logout. A leaked session can be revoked by deleting its row.
- Brute force: django-axes locks a username+IP pair for 15 minutes after 5 failures.
- CSRF on every POST, including the assistant (`X-CSRFToken`); no `csrf_exempt` anywhere.
- HTTPS redirect, HSTS (1 year, no preload), `X-Frame-Options: DENY`, nosniff,
  `Referrer-Policy`, and a CSP without inline scripts (`script-src 'self'`).
- Uploads: allow-listed extensions, 2 MB, content verified with Pillow, max 4000px, no SVG,
  random UUID filenames, stored only in the bucket.
- Config fails fast: production will not start without a strong `SECRET_KEY`, `ALLOWED_HOSTS`,
  `DATABASE_URL` and all bucket settings. It never falls back to SQLite or local disk, and it
  never reads a `.env` file.
- Admin served at a configurable `ADMIN_URL`. That is obscurity, not security. On the demo
  the home page redirects to it for the reviewers' convenience, so here it is not hidden at all;
  the real protection is authentication, lockout and permissions.
- Clean 403/404/500 pages, no stack traces (`DEBUG=False`).
- CI: `check --deploy`, `pip-audit`, `bandit`, gitleaks over the full history, Dependabot.

## AI help assistant

- **Grounding:** each request builds context from the live model definitions: field id
  (`loan.due_date`), label, type, required, choices, default, `help_text`. The context also
  includes the business rules, generated from the same constants the code enforces. The form
  the user has open comes first, and the field they last clicked is marked.
- **Answer format:** the model must reply `{"answer", "sources"}`. Sources that are not real
  field ids are dropped, and the UI shows the remaining ones as "Based on: Loan: due date"
  chips. An answer with no valid source is marked as not grounded.
- **When the provider fails:** models are tried in order (`OPENROUTER_MODELS`), with one retry
  for 429/5xx, 3s connect / 12s read timeouts and a 20s total budget. If all fail, the endpoint
  still returns 200 with that field's `help_text` (or all fields on the page), labelled "AI
  unavailable". With no API key, the same fallback is shown and labelled "not configured".
- **Abuse:** staff only, CSRF, 3-500 characters, control characters stripped, 10/min and
  100/day per user. Replies are rendered with `textContent`, so returned HTML is shown as text,
  never run. Only form metadata is sent to the model, never member data.
- **Free-tier caveats:** free models are rate limited and sometimes removed. Pick current ones
  from openrouter.ai/models and list two or three.

## Quick start

Requirements: Python 3.12, Docker (for local Postgres).

```bash
git clone <this repo> && cd benrey-library
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pre-commit install
docker compose up -d                      # Postgres on localhost:54329
cp .env.example .env                      # then set, at minimum:
#   DATABASE_URL=postgres://postgres:postgres@localhost:54329/library
#   USE_LOCAL_STORAGE=1                   (or fill in the bucket settings)
python manage.py migrate && python manage.py createcachetable
DEMO_ADMIN_PASSWORD='...' DEMO_LIBRARIAN_PASSWORD='...' python manage.py seed_demo --reset
python manage.py createsuperuser          # optional, for yourself
python manage.py runserver                # http://localhost:8000/admin/
```

Tests and checks:

```bash
pytest --cov          # ~150 tests, needs DATABASE_URL (Postgres for the locking test)
make lint             # ruff check + format check
make check            # system checks + "no missing migrations"
```

Seed options: `--seed N` (deterministic), `--small` (tiny set), `--covers N`, `--reset`.
`--reset` refuses to run with `DEBUG` off unless you also pass `--allow-production`.

### Deploying (Railway + Supabase)

1. Supabase: create a project. Copy the **session pooler** connection string (add
   `?sslmode=require`). The direct `db.<ref>.supabase.co` host is IPv6-only, which Railway's
   outbound network does not reach; the pooler is IPv4. Create a private Storage bucket `covers` and an S3 access key.
2. Railway: new service from the GitHub repo, region EU West (close to the Supabase
   database), with these service settings:
   - Build command: `python manage.py collectstatic --noinput`
   - Pre-deploy command: `python manage.py migrate --noinput && python manage.py createcachetable`
   - Start command: `gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 30 --access-logfile -`
   - Health check path: `/healthz` (a deploy only takes traffic once this returns 200)
   - Restart policy: on failure, 3 retries
3. Set the variables from `.env.example` in Railway (`DJANGO_SETTINGS_MODULE=config.settings.prod`,
   a 50+ character `SECRET_KEY`, hosts, `ADMIN_URL`, DB, bucket, OpenRouter).
4. Seed once from a machine with the production env: `seed_demo --reset --allow-production`.

If the live URL returns a 500: check Railway's deploy logs and runtime logs first (Django logs
to stdout). `/healthz` tells you whether the app can reach the database (503 if it cannot).

## What I deliberately skipped, and why

- **Public REST API / SPA frontend**: the brief is about the generated admin; an API would be
  surface area with no user.
- **Email notifications for overdue loans, fines payment, reservations/holds, multiple
  branches**: each is a feature on its own; overdue and fees are computed and visible, which
  covers the business logic the brief asks for.
- **OAuth login**: not built. If added (django-allauth, GitHub), new OAuth users would be
  created with `is_staff=False` and **no group**. An Admin approves them and assigns a role.
  Auto-granting a role on first OAuth login would let anyone with a GitHub account into the
  admin. Accounts would never be auto-linked by unverified email.
- **2FA**: would be my next security step (django-otp for staff).
- **Background jobs (Celery, cron)**: nothing needs one yet. "Overdue" is computed at query
  time rather than set by a nightly job.
- **Chat history**: the assistant stores nothing; less data to protect.
- **Full-text search engine, i18n**: admin `icontains` search is fine at this size.

## What I would do differently with more time

- **10x traffic:** first more gunicorn workers/instances (stateless app; sessions, cache and
  files are already external), then the Supabase transaction pooler for connections, then Redis
  for cache and rate limiting, then a read replica for the admin's list pages.
- **10x data:** a `pg_trgm` GIN index for admin search (`icontains` scans), and estimated counts
  on the biggest changelists.
- **Bulk actions on tens of thousands of rows:** run them as background jobs with a progress
  page instead of inside one web request.
- **Observability:** Sentry for errors, structured JSON logs, an uptime check on `/healthz`.
- **Audit trail:** django-simple-history on Loan and Member (the admin log only covers
  admin-made changes).
- **Staging environment** (Railway environments + a Supabase branch) and branch protection with
  required CI before deploy.
- **Rate limiting** with an atomic counter (Redis `INCR`) instead of the DB cache.

## Known limitations

- Free tiers: cold starts on the first request, and free AI models can be slow or rate limited
  (the fallback covers it).
- Signed cover URLs expire after an hour, so a page left open overnight shows broken thumbnails
  until it is refreshed.
- The demo `admin_demo` account is in the Admin group, not a superuser, so it cannot grant
  superuser rights. That is intended.
