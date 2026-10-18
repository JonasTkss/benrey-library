# Defense notes

Short notes per phase so I can explain every decision in the call. Format: what I built, why,
what the alternative was, what breaks at scale.

## Phase 1: skeleton and deploy setup

- **Settings split into base / dev / test / prod.** `base.py` holds what is shared; `prod.py`
  refuses to boot if `SECRET_KEY` is missing or a placeholder, if `ALLOWED_HOSTS` is empty, or if
  any bucket variable is missing (`config/settings/base.py` `s3_storage_options`). That is the
  answer to "graceful handling of missing config": fail at boot with a clear message, never fall
  back to local disk or SQLite. Tested in `config/tests/test_settings.py`.
- **Prod never reads `.env`.** Only dev/test load a local `.env`; prod relies on Railway variables.
  A stray `.env` in the build cannot silently configure production.
- **DB connections:** `CONN_MAX_AGE=60` reuses connections, `CONN_HEALTH_CHECKS=True` pings a
  reused connection before handing it to a request, so a connection Supabase dropped while idle
  is replaced instead of causing a 500. If the DB drops *mid*-request, the transaction rolls back
  and the user gets the clean 500 page; `/healthz` reports 503 so Railway sees the outage.
- **Railway + Supabase instead of Render + Neon:** one place for data (Postgres and S3-compatible
  storage both in Supabase), one place for compute. The Railway service runs `collectstatic` at build,
  `migrate` as a pre-deploy step (runs once, not per worker), gunicorn with 2 workers, and
  `/healthz` as the deploy health check so a broken build never takes traffic. (These are
  service settings listed in the README; Railway deprecated `railway.toml`.) The app runs in
  Railway's EU West region, next to the Supabase database in eu-west-1, so each query is a
  short hop. The database is reached through Supabase's IPv4 session pooler because the
  direct host is IPv6-only.
- **DB cache instead of locmem:** gunicorn runs 2 worker processes; a per-process cache would make
  the assistant rate limit 2x as loose. At 10x traffic I would switch to Redis.

## Phase 2: data model and roles

- **Four models, FK + M2M + image:** `Book.authors` is M2M, `Loan` has FKs to `Book` and `Member`
  with `on_delete=PROTECT` (loan history must never vanish when someone deletes a book; withdraw
  it with `is_withdrawn` instead). `library/models.py`.
- **Constraints live in Postgres, not only in Python:** due date after loan date, `returned_at`
  set if and only if status is RETURNED, one active loan per book+member (a *partial* unique
  constraint, `WHERE status='active'`), case-insensitive unique email (`Lower("email")`).
  Even a bad shell script or bulk insert cannot break them. Tested in `test_models.py`.
- **Nothing derived is stored:** `copies_available`, `is_overdue`, `days_overdue`, `late_fee`
  are computed. A stored counter drifts the first time someone edits a row by hand. "Overdue" is
  not a status either; it is `ACTIVE and due_date < today`, so no nightly job is needed. The
  admin list uses `Book.objects.with_availability()`, one `COUNT ... FILTER` annotation instead
  of one query per row.
- **Indexes for the queries the admin actually runs:** `(status, due_date)` for overdue lists,
  `(member, status)` for "this member's open loans", `(book, status)` for availability.
- **Roles are a data migration (`0002_role_groups.py`), so every environment has them.** They are
  plain Django permissions, which the admin already enforces (buttons, actions, URLs, read-only
  forms). The one custom permission is `change_member_status` (suspend/reactivate), because
  "can edit a member" and "can suspend a member" are different jobs.
- **At 2x/10x books:** the availability `COUNT` per changelist page is bounded by the page size
  (100 rows), so it scales with the page, not the table. Admin search (`icontains` on title)
  becomes a sequential scan first; the fix is a `pg_trgm` GIN index.

## Phase 3: services (business logic)

- **All loan state changes go through `library/services.py`:** `checkout_book`, `return_loan`,
  `extend_loan`. The admin form, the inline and the bulk actions all call these, so each rule
  exists in one place. Admin classes contain no business rules.
- **Race on the last copy:** checkout runs in `transaction.atomic()` and locks the book row, then
  the member row, with `SELECT ... FOR UPDATE`. The second librarian waits, then sees 0 free
  copies. Same lock order everywhere means no deadlock. Real two-thread test against Postgres:
  `test_two_concurrent_checkouts_of_last_copy_only_one_wins`.
- **`borrowing_rule_violations()` returns *all* problems** (suspended + no copies + cap), so the
  librarian sees every reason at once instead of fixing one error at a time.
- **Bulk operations are per-row transactions on purpose** (`apply_to_each`): in a 500-loan
  "mark returned", one already-returned loan should be reported, not roll back the other 499.
- **Alternative I skipped:** a state-machine library (django-fsm). Two states and one transition
  do not need a dependency.

## Phase 4: admin customization and roles

- **Who the admin is for:** a front-desk librarian whose day is "find member → see their loans →
  lend or take back books". So the member page has a "Lend a book" inline (add-only, book
  autocomplete) and a read-only loan list with open loans first, plus a link to that member's
  open loans in the Loan list, where the bulk "Mark as returned" / "Extend 7 days" actions live.
  `library/admin.py`.
- **Roles are permissions, and the admin enforces them server-side.** Librarian has no delete
  permission, so `delete_selected` is gone from the action list and the delete URL returns 403
  (`test_librarian_cannot_delete_by_url`). Only view on Book/Author, so the catalogue renders
  read-only and a forged POST gets 403. No `auth` permissions, so Users/Groups are not on the
  index at all. On top of that: `get_list_display` hides stock/record columns from librarians,
  `get_readonly_fields` locks member `status` without `change_member_status`, and the suspend
  actions declare `permissions=["change_status"]` so Django hides them.
- **New loans are validated under row locks inside the admin's own transaction.** Django wraps
  each add/change view in `transaction.atomic()`. `CheckoutForm.clean()` calls
  `services.checkout_problems()`, which locks the book and member rows; the locks are held until
  `save_model` / `save_formset` saves through `services.checkout_book()`. A rule break is a
  normal form error, never a 500 (`test_rule_violation_is_a_form_error_not_a_crash`).
- **Existing loans are read-only apart from notes.** State changes only through the actions, so
  nobody can type `status=returned` without setting `returned_at`, or reopen a returned loan.
- **500-record bulk update:** select all across pages → action. `apply_to_each` runs each row in
  its own transaction and reports "N done, M skipped: reasons" (first 10 shown). For 50k rows I
  would move the action into a background job, because a web request should not run for minutes.
- **N+1 guarded by tests:** the changelists for Book (prefetch authors, annotate availability),
  Member (annotated counts) and Loan (`list_select_related`) run the same number of queries for 3
  rows and 30 rows (`test_changelist_query_count_does_not_grow_with_rows`). Loan list sets
  `show_full_result_count=False` to skip an extra `COUNT(*)`.
- **Privilege escalation guard:** non-superuser Admins can manage staff accounts but cannot tick
  `is_superuser` or grant per-user permissions (`StaffUserAdmin`).
- **Auth:** Argon2, 12-char validators, 8h DB sessions, session key rotated at login, row deleted
  at logout, axes locks username+IP after 5 failures for 15 min (HTTP 429). `config/tests/test_auth.py`.

## Phase 5: object storage and uploads

- **Covers go to Supabase Storage through its S3-compatible API** (`django-storages` S3 backend,
  `STORAGES["default"]` in `config/settings/prod.py`). Private bucket, `querystring_auth=True`:
  every image URL is a signed URL valid for 1 hour, generated locally (no network call per row).
- **Why not local disk:** Railway's container filesystem is ephemeral. It is wiped on every
  deploy, not shared between instances, and has no backups or CDN. Files would disappear on
  the next push.
- **No silent fallback:** prod refuses to boot without the bucket variables. Dev uses local disk
  only with an explicit `USE_LOCAL_STORAGE=1`.
- **Upload checks (`library/validators.py`):** extension allow-list (jpg/png/webp, so no SVG
  and its scripts), 2 MB cap, Pillow opens and `verify()`s the bytes (an `.exe` renamed `.png`
  fails), format re-checked from content, 4000px cap plus decompression-bomb guard. The stored
  name is `covers/<uuid4>.<ext>`; the user's filename never becomes a path. Request body capped
  at 2.5 MB (`DATA_UPLOAD_MAX_MEMORY_SIZE`).

## Phase 6: seed data

- `python manage.py seed_demo --reset --seed 42`: 300 authors, 2000 books, 600 members,
  6000 loans (80% returned history over two years, ~1200 open, ~1 in 8 of those overdue), and
  100 generated gradient covers uploaded to the bucket. `--small` for tests, `--covers N`.
- **The generator obeys the same rules as the app** (copies, 5-loan cap, one copy per member,
  suspended members do not get new loans), so the admin never shows impossible states.
  `test_seed_respects_the_borrowing_rules` checks it.
- `bulk_create` in batches; the full seed takes a few seconds locally.
- **Safety:** refuses to run twice without `--reset`; refuses `--reset` with `DEBUG` off unless
  `--allow-production` is passed. Demo user passwords come only from `DEMO_*_PASSWORD` env vars,
  are run through the password validators, and are never printed or committed.
- `admin_demo` is a member of the **Admin group, not a superuser**, so the demo shows the role
  system, not a superuser bypassing it.

## Phase 7: AI help assistant

- **Grounding (`assistant/context.py`):** the prompt context is generated from the live Django
  field definitions: id (`loan.due_date`), label, type, required, max length, choices, default
  and `help_text`. Nothing is copied by hand, so it cannot drift from the schema. The rules text
  is built from the same constants the services enforce (`MAX_ACTIVE_LOANS`, 14 days, 7 days,
  fee). A test fails if any editable field has no `help_text`.
- **Page awareness:** the widget sends the model from the URL and the last focused field. The
  server accepts them only if they are real ids (allow-list), puts that model first and marks the
  field "the field the user is looking at". The whole schema is small (~25 fields), so the other
  models go in too.
- **The exact prompt:** `SYSTEM_PROMPT` in `assistant/views.py`, plus the generated context.
  The user's text goes only into the `user` message, never into the system prompt
  (`test_prompt_injection_stays_in_the_user_message`). Temperature 0.2, max 800 tokens (reasoning models spend part of it thinking; a reply cut off with `finish_reason: length` counts as a failure and the next model is tried).
- **What I left out of the context:** row data (member names, emails, loans). The assistant
  explains forms; it does not need personal data, and sending it to a third-party free model
  would be a privacy problem.
- **Anti-hallucination:** the model must return `{"answer", "sources"}`. Any source id that is not
  a real field is dropped; with no valid source the UI says "not grounded". Invalid JSON →
  raw text, no sources.
- **Provider failure (`assistant/client.py`):** ordered chain of free models from
  `OPENROUTER_MODELS`. 429/5xx → one retry after 0.5s, then next model; timeout, network error,
  4xx, empty or malformed reply → next model. Connect timeout 3s, read 12s, hard budget 20s
  overall. If everything fails the endpoint still returns **200** with a deterministic answer
  built from `help_text` (`degraded: true`), and the widget labels it. No key configured → same
  fallback, `disabled: true`. The admin itself never depends on the assistant.
- **Abuse/cost/security:** staff only (403 JSON for anyone else), CSRF required (header from
  the page; no `csrf_exempt`), JSON only, question 3-500 chars with control characters stripped,
  10/min and 100/day per user in the shared DB cache (429). Only free models, so the cost risk
  is the free-tier quota, not money. Replies are inserted with `textContent`, so a reply that
  contains `<script>` is shown as text. The key stays on the server and is never logged; logs hold
  model, outcome and latency only.
- **Known limit:** the DB-cache counter is get-then-set, not atomic, so two simultaneous requests
  can slip one over the limit. Good enough for a soft limit; Redis `INCR` would fix it.
- **Skipped on purpose:** chat history (no server storage of questions), streaming, tool calling.
