# Backend Engineering Principles

Rules for this codebase, each earned from something that actually went wrong here.
Every rule states **why it exists** (the real incident) and **how it is enforced**.

Where a rule is enforced by a test, CI fails on violation — `ruff check`,
`makemigrations --check`, `manage.py check`, and `coverage run manage.py test` all
run on every PR (`.github/workflows/ci.yml`). Where a rule is **review-only**, it
says so plainly. Claiming enforcement we do not have would repeat the exact
mistake these rules exist to prevent.

> **The one idea behind all of this.** The Sprint B1 audit found that any
> anonymous caller could read every customer's identity and movements via
> `stationById { bookings { user { email } } }`. Nobody decided to publish that.
> `fields = "__all__"` published it, and no test asked who was allowed to read it.
> The bug was not a wrong decision — it was an **absent** one. So we prefer rules
> that make the wrong thing *impossible* over rules that make it *discouraged*.

---

## 1. Never use `fields = "__all__"` or `exclude` in a GraphQL type

List every published field explicitly.

**Why.** `__all__`/`exclude` fail **open**: every field and reverse relation you
add to a model later joins the public API silently, and no code review of the
model change shows it. This is the direct cause of the B1 vulnerability —
`StationType` published `bookings`, `reviews` and `favorited_by` because nobody
listed them, and `UserType`'s `exclude = ("password",)` published `is_superuser`,
`last_login` and `passwordresetotpSet` for the same reason. An allow-list fails
**closed**: a new model field is invisible until someone chooses to publish it.

**Enforced.** `accounts/test_b1_security.py::SchemaHygiene`
- `test_no_graphql_type_uses_an_open_field_policy` — source scan of every `schema.py`
- `test_no_django_type_publishes_a_reverse_relation` — semantic backstop over the
  built schema, so it catches an open policy however it is introduced

## 2. Every root field declares exactly one authorization policy

Use exactly one decorator from `accounts/permission.py`: `@public`,
`@login_required`, `@active_required`, `@station_owner_required`, `@admin_required`.
Do not stack them — the inner ones already assert authentication, and stacking
makes the effective policy ambiguous.

`@public` is a **marker, not a no-op**. It is the difference between "anyone may
read this" and "nobody remembered to check", and it is only for genuinely public
data (station discovery).

**Why.** Before B1, `stationBookings` was owner-gated while the identical rows
were readable anonymously one hop sideways. **Reachability is not authorization.**
A field that is hard to reach is not protected; it is lucky.

**Enforced.** `accounts/test_b1_authorization_policy.py` — every root field must
appear in `QUERY_POLICY`/`MUTATION_POLICY` with the policy its resolver actually
carries. **A new query or mutation cannot merge until someone writes down who may
call it.** The test fails on an unclassified field, a missing decorator, or a
mismatch between the table and the code.

## 3. Prefer audience-specific types over conditionally hiding fields

If two audiences may see different amounts of a record, that is **two types**, not
one type with a check inside a resolver.

- `PublicUserType` (id, username) — a station's owner, a review's author
- `BookingCustomerType` (+ email) — the customer on a booking
- `UserType` (full) — **only** reachable from self/admin roots

**Why.** A conditional hides data *if the check runs*. A narrow type has nothing
to hide: the field does not exist on that path, so no future resolver, fragment or
refactor can reach it. It also cannot be forgotten, which is the failure mode we
actually observed. Conditional hiding has a second trap: a nullable-on-demand
field breaks non-null contracts for the clients allowed to see it.

**Partly enforced** (rule 1's reverse-relation guard, plus pinned field lists in
`UserTypeExposure`). Choosing the right audience for a *new* type is review-only.

## 4. Every destructive action emits an audit event

Deletes, deactivations and state changes go through `accounts/administration.py`,
which records via `accounts/audit.py`. **Never build an `AuditLog` in a resolver.**

Two non-obvious requirements, both learned the hard way:

- **The record must outlive its subject.** `actor` is `SET_NULL` with the username
  denormalised, and the target is deliberately **not a foreign key** — an FK would
  be cascaded away by the very delete it is evidence of.
- **Recording must not break the action.** A logging fault is logged and swallowed:
  losing the trail of a completed delete is bad; failing *after* the rows are gone
  and telling the caller it did not happen is worse.

**Why.** `deleteUser`, `toggleUserActive` and `approveStationOwner` left no trace
anywhere — the destructive actions were the only ones with no audit trail.

**Enforced by construction** (a resolver cannot delete without recording, because
it does not do the deleting) and by tests in `test_b1_administration.py`. That a
*new* admin mutation routes through the service layer is review-only.

## 5. Every collection endpoint is paginated

Use `paginate()` from `ev_backend/pagination.py` and return `items`/`totalCount`/
`hasNext`. Never return an unbounded queryset. `hard_cap()` exists only to bound
legacy list fields; it truncates **silently**, which is a bug you are deferring,
not a solution.

**Also:** always tie-break the sort on a unique column. `usersPage` orders by
`(field, id)` because `date_joined` collides for accounts created in the same
instant, and an unstable sort silently repeats or drops rows across pages.

**Why.** `usersByRole` returned the entire table — the one collection that escaped
the pagination layer. It is now capped and deprecated in favour of `usersPage`.

**Review-only.** B2.1 did **not** automate this, so every collection it added
(`stationsPageAdmin`, `bookingsPage`, `reviewsPage`) is paginated and tie-broken
only because a human checked. Still worth automating (assert every list field
takes `limit`/`offset`) — B3.

## 6. New admin functionality requires regression tests

Every admin query/mutation needs coverage for **anonymous, customer, owner and
admin** — not just the happy path. Every fixed bug gets a test that fails without
the fix.

**Why.** The B1 audit found `approveStationOwner` had *zero* tests and had been a
no-op since it was written. Nobody noticed because nothing asked it to work.

**Enforced.** CI runs the full suite with coverage. Rule 2's policy test forces at
least a classification for every new field. Test *depth* is review-only.

## 7. Business workflow state must never live in authentication state

`is_active` means "may this account authenticate". It must not encode "is this
application approved", "is this account suspended pending review", or any other
workflow. Give the workflow its own field.

**Why.** `approveStationOwner` set `is_active = True` on accounts that were
already active — a silent no-op for the platform's entire life. Fixing it by
making pending owners `is_active = False` would have been worse: graphql_jwt
rejects an inactive user's token *at decode time*, so a pending owner could not
sign in at all — not even to see that they were pending, and the
register→auto-login flow in both clients would break for owner sign-ups.
Approval lives on `owner_status` (pending/approved/rejected), so a pending owner
is a real, signed-in customer whose station management is simply closed.

**Review-only**, and the most important rule here to hold in your head: the two
concepts look similar and are not.

## 8. Narrowing a type must never widen its nullability

When you replace a field's type, keep `required=` matching the model. A Django
non-null FK is `Type!`; `graphene.Field(X)` defaults to **nullable**.

**Why.** B1's first pass changed `booking.user` from `UserType!` to
`BookingCustomerType` — silently optional. Nothing in the backend noticed. The
**web client's `tsc` caught it** against the exported schema.

**Enforced** by the compatibility check in rule 9.

## 9. Client compatibility is proven by exporting the schema, never reasoned about

Before claiming a change is backward compatible:

```bash
python manage.py graphql_schema --schema ev_backend.schema.schema --out <web>/schema.graphql
cd <web> && npx graphql-codegen --config codegen.ts && npx tsc --noEmit && npx vitest run
```

Codegen validates **every** client document against the real schema.

**Why.** Exporting the schema has now caught three production-breaking defects
that review missed: two auth bugs in W0, and the nullability drift in rule 8.
Reading a diff and reasoning "that looks additive" has caught zero.

**Review-only** (a human must run it), but non-negotiable for any schema change.

## 10. Do not ship code that cannot run

No placeholder endpoints, no aspirational modules, no functions with no caller.

**Why.** `accounts/tasks.py` declared a Celery task for OTP email. Celery was
never in `requirements.txt`, there was no app module, and nothing imported it —
it would have raised `ImportError` if anything had. It read as "email is async"
for anyone skimming the codebase. Email is in fact sent **synchronously inside the
request**. Deleted in B1. Code that looks like a feature and isn't is worse than
an absent feature, because it misleads the next reader.

**Enforced** for imports only (`ruff` catches unused ones). Dead modules are
review-only.

## 11. Intentional refusals are typed; everything else is a bug

Raise from `ev_backend/errors.py` (`AuthenticationRequired`, `PermissionDenied`,
`NotFound`, `ValidationError`, `Conflict`) for any caller-visible refusal. These
carry a stable `code` surfaced as `extensions.code`, with the human message
unchanged. Anything else that escapes a resolver is a bug and is masked to a
generic message by `HardenedGraphQLView` — internal exceptions must never reach a
client.

**Why.** Callers previously had to string-match prose to tell "forbidden" from
"not found", so every message change was a silent breaking change.

---

## Checklist: adding a GraphQL field

1. Which audience is this for? Pick or create the **narrowest** type (rule 3).
2. List its fields explicitly (rule 1).
3. Choose exactly one authorization decorator and add the field to
   `QUERY_POLICY`/`MUTATION_POLICY` (rule 2).
4. Is it a collection? Paginate it with a tie-broken sort (rule 5).
5. Is it destructive? Route it through `administration.py` so it is audited, and
   consider whether it needs a safety interlock (rules 4 and 12 below).
6. Tests for anonymous / customer / owner / admin (rule 6).
7. Export the schema and run the web client's codegen + tsc + tests (rule 9).

## 12. Interlocks on anything unrecoverable

Refuse self-deactivation, self-delete, and removing the last active administrator.
There is **no API to create an admin** (`create_admin_user.py` is a shell script),
so locking out the last one is unrecoverable without server access. Before a
destructive action, tell the caller what it will destroy —
`userDeletionPreview` exists because deleting one owner erases *other* customers'
booking history, and nothing else in the API said so.

---

## The administrative API surface (Sprint B2.1)

Every operation below is `@admin_required` and listed in `QUERY_POLICY` /
`MUTATION_POLICY`. Grouped by the question it answers.

### People

| Operation | Notes |
|---|---|
| `usersPage(role, search, isActive, ownerStatus, orderBy, limit, offset)` | The user directory. Unchanged in B2.1 — it already met the spec. |
| `usersByRole(role)` | **Deprecated.** Truncates silently; use `usersPage`. |
| `approveStationOwner(userId)` | Sets `approved`, records `reviewer`/`reviewedAt`, clears any stale `rejectionReason`. Idempotent. |
| `rejectStationOwner(userId, reason: String!)` | Sets `rejected`, records reviewer/time/reason. **`reason` is now required.** |
| `toggleUserActive(userId, isActive)` | Interlocked (rule 12). |
| `deleteUser(userId)` / `userDeletionPreview(userId)` | Interlocked; returns the blast radius. |

**There is deliberately no `pendingOwners` query.** `usersPage(role:
"station_owner", ownerStatus: "pending")` already answers it exactly, with the
paging, search and ordering a bespoke version would not have. A second endpoint
answering a question an existing one answers is a second thing to keep correct,
and the two drift.

**`rejectStationOwner.reason` went from `String` to `String!` — a breaking
change, made deliberately.** B1 accepted `reason: null` and stored `""`,
producing rejections that neither the applicant nor the next admin could account
for. It was made while it was still free: no shipped client calls this mutation.
This is the *only* breaking change in B2.1 (see rule 9 — verify, do not reason).

### Stations

| Operation | Notes |
|---|---|
| `stationsPageAdmin(search, ownerId, isActive, chargerType, minRating, orderBy, limit, offset)` | Sees **inactive** stations; the public `stationsPage` never does. |
| `activateStation(stationId, reason)` | Closes B1's "no reactivation path" deviation. |
| `deactivateStation(stationId, reason)` | Withdraws from discovery, blocks new bookings. |

`activateStation` is the *only* route back from a soft delete: `deleteStation`
(owner) and `deactivateStation` (admin) both clear the same `is_active` flag, and
before B2.1 nothing could set it back without database access. Deactivating
deliberately leaves existing bookings alone — cancelling other people's plans is
a separate decision with a customer-visible consequence.

**There is no `restoreStation`.** The model has one flag, so "restore" and
"activate" would be two names for one operation — see the deviations table.

### Bookings — read-only, completely

| Operation | Notes |
|---|---|
| `bookingsPage(status, customerId, ownerId, stationId, dateFrom, dateTo, search, orderBy, limit, offset)` | `dateFrom`/`dateTo` filter **`startTime`** (the slot). |
| `bookingById(bookingId)` | |

**No override, no forced cancellation, no refund.** `Booking` carries no money,
no price snapshot and no payment reference, so a refund mutation could not do
anything except lie about having issued one — rule 10, in its worst form: a
mutation that *runs* but does not *do the thing* is more misleading than an
absent one. `bookingsPage` is the most sensitive read in the API: it returns every
customer's identity and movements platform-wide, which is the exact data the B1
vulnerability leaked. Admin is the only defensible policy, and an **owner is
refused even when they pass their own `ownerId`** — a filter is not an
authorization check.

### Reviews — moderation

| Operation | Notes |
|---|---|
| `reviewsPage(stationId, ownerId, customerId, rating, isHidden, search, orderBy, limit, offset)` | The one read that **does** see hidden reviews. |
| `hideReview(reviewId, reason)` / `restoreReview(reviewId, reason)` | Reversible. Reach for these first. |
| `adminDeleteReview(reviewId, reason: String!)` | Irreversible. Content is snapshotted into the audit record before it goes. |

**`adminDeleteReview` is deliberately not named `deleteReview`.** `deleteReview`
already exists, is author-only (`POLICY_ACTIVE`), and both clients call it.
Reusing the name would either break them or silently widen an author-only action
into an admin one on a field they already have documents for.

**Hiding is only real if it reaches every read.** `Review.is_hidden` is enforced
through two single definitions — `Review.visible()` for row reads and
`review_stats()` for rating aggregates — because **four** separate paths put a
rating in front of someone: the live public list, the *cached* public list, an
owner's own stations, and a station's detail page. A hidden review is filtered
from all of them and from `dashboardSummary`. The failure mode guarded against is
a new resolver hand-rolling `Count("reviews")` and quietly counting the hidden
rows back in — so import `review_stats()` instead of writing that. Hiding
deliberately does **not** free the one-review-per-user constraint: otherwise
moderation is defeated by hide, re-post, repeat.

### Dashboard

`dashboardSummary(newestLimit)` — customers, owners, pendingOwners,
rejectedOwners, stations, activeStations, inactiveStations, bookingsToday,
bookingsThisWeek, bookingsThisMonth, reviews, averageRating, newestUsers,
newestStations. Six queries, fixed, whatever the platform's size
(`test_it_answers_in_a_bounded_number_of_queries` pins it).

Two definitions that were **choices**, documented because the field names do not
reveal them and the two endpoints genuinely differ:

- `bookingsToday/Week/Month` count bookings **created** in the period (platform
  volume). `bookingsPage(dateFrom/dateTo)` filters the **slot**. Both are
  defensible; neither is guessable from the name.
- Periods are local midnight / Monday / the 1st, in the project timezone.

**No revenue, no utilisation, no growth rate, no conversion.** Not an oversight:
the data does not exist, so each would have to be fabricated — and *a fabricated
number on a dashboard is indistinguishable from a real one at a glance*. Nobody
re-derives a summary figure, which is exactly why an invented one does more
damage here than anywhere else in the API.

### Audit

`auditLogsPage(actorId, action, targetType, targetId, dateFrom, dateTo, orderBy,
limit, offset)` — B2.1 added `actorId`, `targetType`, `dateFrom`, `dateTo` and
`orderBy`. Purely additive; a B1 client's document still runs unchanged
(`test_the_b1_call_shape_still_works_unchanged`). The trail now spans three target
types (`user`, `station`, `review`).

`actorId` filters the FK, which is `SET_NULL`: entries whose actor has since been
deleted are unreachable *by that filter* by construction, though they remain in
the trail with `actorUsername` intact. That is the price of rule 4's "the record
outlives its subject", and it is the right trade.

---

## Known deviations (tracked, not forgotten)

Honest list of places the codebase does not yet meet these rules:

| Rule | Deviation | Plan |
|---|---|---|
| 5 | `usersByRole` truncates at 500 without telling the caller | Deprecated; migrate W3 to `usersPage`, then delete (B3) |
| 3 | A rejected owner's existing stations stay live and bookable | **Still needs a product decision** — do not invent one. B2.1 gives an admin a manual lever (`deactivateStation`); that is a workaround, not the decision (B3) |
| 5 | Enforcement of "collections are paginated" is review-only | **Not done in B2.1** — still review-only, and every collection added this sprint is paginated only because a human checked. Automate (B3) |
| 12 | Admin creation/promotion is shell-only | Add a bootstrap path (B3) |
| 10 | Email is sent synchronously in-request with `fail_silently=False` | Wire a real queue, or keep sync deliberately (B3) |
| 5 | `stationsPageAdmin`/`reviewsPage` resolve `averageRating`/`numOfReviews` per row (N+1) | Bounded by the page cap (≤100) and admin-only, so it is slow, not dangerous. Read the annotation `review_stats()` already adds (B3) |
| — | `dashboardSummary` runs the `newestUsers`/`newestStations` queries even when the caller selects neither | Two queries nobody asked for; move them into field resolvers (B3) |
| — | No `restoreStation`. `Station` has one `is_active` flag, so "restore" and "activate" would be **two names for one operation**, and an owner's soft delete is indistinguishable in the database from an admin takedown | **Needs a product decision.** If an admin takedown must survive an owner undoing it, that is a *second field* (e.g. `suspended_by_admin`), not a second mutation. Do not add the alias to make the API look complete (B3) |

### Fixed since B1

| Rule | Was | Now |
|---|---|---|
| — | No station reactivation path: a soft-deleted station needed database access to recover | `activateStation` (admin, audited) |
| 4 | Owner approval recorded the event but not the decision — nothing said who decided, when, or why | `reviewer` / `reviewedAt` / `rejectionReason` on the record; `reason` mandatory on reject |
| 4 | The audit trail covered users only | Extends to stations and reviews; filterable by actor, target type and date |
