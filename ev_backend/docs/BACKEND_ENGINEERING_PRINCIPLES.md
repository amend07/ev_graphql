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

Use `window()` and `apply_ordering()` from `ev_backend/pagination.py` rather than
re-typing either. Both were duplicated — the window three times, the ordering
five — and each copy is a chance to drop the tie-break or the cap, silently.

**Enforced (B3).** `accounts/test_b3_pagination.py`, three independent layers:

- `EveryCollectionIsClassified` — every list/page field the schema publishes must
  appear in `COLLECTIONS` declaring how it is bounded (PAGE / WINDOW / CAPPED,
  and CAPPED costs a written justification). A new collection cannot merge
  unclassified.
- `EveryCollectionIsBounded` — shrinks the caps to 5, seeds 8 rows, asserts every
  collection returns at most 5. It tests the **property, not the spelling**: any
  bounding mechanism passes, and only an actually-unbounded field fails. A test
  that grepped for a `paginate()` call would just be a spelling test. Verified by
  reintroducing the exact `usersByRole` defect and watching it fail.
- `EveryPagedCollectionHonoursLimit` — bounded is not pageable. A field that caps
  at 500 but ignores `limit` makes the client's paging decorative.

This rule spent two sprints as "worth automating next sprint" while B2.1 added
three more collections under it. It cost ~200 lines. **The gap between a rule and
its test is where the next `usersByRole` lives.**

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

## 13. A guard you have not seen fail is not a guard

After writing a test that enforces a rule, **break the rule on purpose and watch
the test fail**, then revert. Put the failure in the commit message.

**Why.** This is the most valuable habit of B1–B3 and it is worth more than any
single rule above. A guard that cannot fail is worse than no guard: it reports
safety it never checked, and it is indistinguishable from a working one until the
day it matters. Every guard in this codebase has been verified this way —
`SchemaHygiene` by republishing `Station.bookings`, `EveryCollectionIsBounded` by
restoring the unbounded `usersByRole`, the N+1 tests by reverting the annotation
fix. Two of those were written by an author who believed the rule was already
enforced.

The corollary bites hardest on thresholds: a fixed `assertNumQueries(4)` passes an
N+1 that happens to equal 4 at the size you tested. B3's query-count tests compare
**two** page sizes for that reason — they assert the shape of the cost, not a
number that has to be maintained.

**Review-only, permanently.** No test can check that you tested your test.

## 14. Resolvers read annotations; they never re-query per row

If a queryset annotates a value, the resolver reads the annotation. Per-row
queries in a list resolver are N+1 by construction.

**Why.** `stationsPageAdmin` annotated `review_stats()` and then its resolvers
ignored it and re-queried each station's rating anyway: the aggregate was computed
in SQL, discarded, and paid for twice more per row — **22 queries for 10 stations**,
measured. Invisible in review, invisible in a ten-row dev database, and invisible
in tests that only assert content.

**Enforced (B3).** `accounts/test_b3_query_counts.py` runs every page at two sizes
and fails if the query count grows with the row count.

## 15. Administrator privilege is not grantable over the API

`role` is writable from the server only — `manage.py promote_admin`, through
`administration.promote_to_admin`, audited. There is no `promoteToAdmin` mutation
and this is a decision, not an omission.

**Why.** A stolen admin session today buys damage. If promotion were an API call
it would also buy **persistence**: the attacker mints a second admin and revoking
the one you noticed achieves nothing. Requiring server access to grant privilege
means recovery is always possible. The zero-admin bootstrap could not be a
mutation anyway — you would need an admin to call it. It is a rare, high-blast-
radius operation, and needing a shell is the safeguard, not the inconvenience.

**Enforced (B3).** `test_b3_admin_bootstrap.py::PrivilegeIsNotGrantableOverTheApi`
asserts no role-writing mutation exists, and that registration cannot mint an admin.

## 16. Undecided behaviour gets a characterization test, not a guess

When the code does something only because nobody has decided otherwise, pin it
with a test that says so in its docstring — what the options are, what each costs,
and the one function where the decision goes.

**Why.** "A rejected owner's stations stay online" is not a design; it is the
absence of one. Left unpinned it becomes a design by accident — someone changes it
in an unrelated refactor and nobody notices, or everyone assumes it was decided.
The test cannot tell a deliberate change from a mistake, which is exactly why it
must fail loudly and force a human to say which it was.

**Enforced (B3).** `accounts/test_b3_product_decisions.py`. See rule 7's cousin:
the decision belongs to the business, and inventing one in code is the failure.

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

Honest list of where the codebase does not meet its own rules. Anything marked
**product decision** must not be resolved in code — see rule 16.

| Rule | Deviation | Plan |
|---|---|---|
| 3 | A rejected owner's stations stay live and bookable | **Product decision.** Pinned by `test_b3_product_decisions.py`, which documents both options, their costs, and the one function the decision goes in (`administration.reject_owner`). The lever Option A needs already exists and is audited. Do not resolve this in code (B4) |
| — | No `restoreStation`: `Station` has one `is_active` flag, so "restore" and "activate" would be two names for one operation, and an owner's soft delete is indistinguishable from an admin takedown | **Product decision.** If a takedown must survive an owner undoing it, that is a *second field* (`suspended_by_admin`), not a second mutation. Do not add the alias to make the API look complete (B4) |
| 5 | `usersByRole` truncates at 500 silently | **Now callerless**: W4 migrated the web console to `usersPage`, and no mobile client ever used it. Delete once W4 ships — it is the last silent truncation in the schema (B4) |
| 10 | Email is sent synchronously in-request with `fail_silently=False` | A slow SMTP server blocks an OTP mutation. Wire a real queue, or keep it synchronous *deliberately* and write down why (B4) |
| — | `dashboardSummary` runs the `newestUsers`/`newestStations` queries even when the caller selects neither | Two queries nobody asked for. Constant cost, not N+1 — measured at 6 queries regardless of platform size. Move them to field resolvers (B4) |
| 4 | That a *new* admin mutation routes through the service layer is review-only | Audit is unskippable for existing paths by construction, but nothing stops a new resolver writing to the DB directly. Plausibly automatable: assert no resolver imports a model's `.save()`/`.delete()` outside a service (B4) |
| 9 | Client compatibility is proven by exporting the schema, but a human must remember to run it | The check is real and has caught three production-breaking defects; the trigger is a habit. Could be CI: export, diff, fail on a breaking change without an explicit override (B4) |
| 12 | Object-level ownership (`station.owner_id != user.id`) is checked per resolver | The policy test proves a decorator exists, not that it checked the *right* station. Four call sites, two different error messages. Consolidating would change client-visible strings, so it stays deliberate and manual (B4) |
| 13 | Nothing can verify that a guard was verified | Permanent. Rule 13 is a habit, not a mechanism |

### Fixed since B1

| Rule | Was | Now |
|---|---|---|
| — | No station reactivation path: a soft-deleted station needed database access to recover | `activateStation` (admin, audited) |
| 4 | Owner approval recorded the event but not the decision — nothing said who decided, when, or why | `reviewer` / `reviewedAt` / `rejectionReason` on the record; `reason` mandatory on reject |
| 4 | The audit trail covered users only | Extends to stations and reviews; filterable by actor, target type and date |
| 5 | "Collections are paginated" was enforced by review, and review had already lost once | Automated: schema-vs-contract, a behavioural bounds test that shrinks the caps and over-seeds, and a limit-honoured test (B3) |
| 5 | The window idiom existed 3× and the ordering idiom 5×, each copy free to forget the cap or the `id` tie-break | `window()` and `apply_ordering()` in `ev_backend/pagination.py` (B3) |
| 14 | `stationsPageAdmin` re-queried each row's rating despite the queryset annotating it — 22 queries for 10 stations | Resolvers read the annotation: **2 queries**, pinned by a two-size query-count test (B3) |
| 12 | Admin privilege came from an interactive script at the repo root that wrote no audit record | `manage.py promote_admin` through the service layer, audited, idempotent, `--dry-run` (B3) |
| 12 | The last-admin interlock told operators to "promote another administrator first" — an action no API offered | The message now names the command that does it (B3) |
| 16 | "A rejected owner's stations stay online" was undecided *and* unpinned | Pinned by characterization tests that name the options, the costs, and the decision point (B3) |
