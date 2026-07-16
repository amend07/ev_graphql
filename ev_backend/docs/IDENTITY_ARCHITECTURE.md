# Identity & Authentication Architecture (W7)

Status: **DESIGN — not implemented.** This document is the agreed shape of the
permanent identity model. It is deliberately written before any code, because
this decision touches the backend, the web client and the Flutter client at once,
and the expensive mistakes here are the ones you cannot migrate away from later.

Nothing in this document is built yet. Three of its pillars **cannot** be built
without external credentials and a product decision — those are named explicitly
in §8 and §9 rather than stubbed.

---

## 1. Why the current model has to change

Today's identity is `username` + a 6-digit PIN, with email as a recovery channel.

| Property | Today | Problem |
|---|---|---|
| Canonical ID | `username` | Users do not know their username. It is an artefact of Django's `AbstractUser`, not a thing a driver has. |
| Credential | 6-digit PIN | 10⁶ keyspace. It is survivable only because of the rate limiter (`AUTH_RATELIMIT`), which is per-process without `REDIS_URL`. |
| Recovery | Email OTP | Sent **synchronously in-request** with `fail_silently=False`. |
| Session revocation | **None** | JWT is stateless, `graphql_jwt.refresh_token` is NOT installed. A leaked token is valid for its full 30 minutes and **cannot be revoked**. |
| Second factor | None | — |

The PIN is the headline weakness but not the deepest one. The deepest one is that
**the platform has no way to invalidate a session**, which W7 is asked to fix
("logout everywhere", "session revocation") and which is not achievable by adding
screens.

## 2. The identity model

```
                 ┌──────────────────────────────┐
                 │            User              │   internal, never public
                 │  id (PK)                     │
                 │  phone_e164   UNIQUE, NULL   │   ← canonical identity
                 │  phone_verified_at   NULL    │   ← verification is SEPARATE
                 │  email        (secondary)    │
                 │  username     (legacy)       │   ← retained, de-emphasised
                 │  role, owner_status ...      │
                 └──────────────┬───────────────┘
                                │ 1:N
                 ┌──────────────▼───────────────┐
                 │      AuthIdentity            │
                 │  user_id      FK             │
                 │  provider     phone|google|  │
                 │               apple|password │
                 │  subject      provider's ID  │   google `sub`, apple `sub`,
                 │  verified_at                 │   E.164 for phone
                 │  linked_at, last_used_at     │
                 │  UNIQUE(provider, subject)   │   ← the anti-duplicate rule
                 └──────────────────────────────┘
```

### The load-bearing decisions

**Phone lives on `User`, not only in `AuthIdentity`.** It is the *canonical
identity* — the thing that answers "who is this person" — not merely one way to
log in. A unique constraint on the column is what makes "one human, one account"
enforceable by the database rather than by application code that can be bypassed.

**`phone_verified_at` is separate from `phone_e164`, and is a timestamp not a
boolean.** A claimed phone and a proven phone are different facts. This is the
same lesson as B1's `owner_status`: encoding a workflow state in a field that
means something else (there, `is_active`) produced a no-op mutation that nobody
noticed for two sprints. "When was this proven" also survives a policy change
that says proof expires after N months; a boolean does not.

**`phone_e164` is NULLable.** This is forced by reality, not preference: ~every
existing account has no phone. See §4 — a NOT NULL column here would make the
migration unrunnable.

**`AuthIdentity.UNIQUE(provider, subject)` is the whole anti-duplicate story.**
A Google `sub` can belong to exactly one user, forever. This is a database
constraint, not a check, because the check-then-insert race is precisely how
duplicate accounts get created under concurrent sign-in.

**Username is retained and de-emphasised, not deleted.** It is Django's
`USERNAME_FIELD` and is load-bearing for `create_user`, the admin, and
`graphql_jwt`'s `get_by_natural_key`. Removing it is a separate migration with
its own risk; W7 removes it from the *experience*, and W8+ can remove it from
the public API once no client selects it. Deleting it in the same sprint that
introduces three new providers would mean two irreversible changes at once.

## 3. Provider linking rules

This is the security core of the sprint. Every rule below exists because its
absence is an account-takeover.

| Scenario | Rule |
|---|---|
| Sign in, provider `subject` known | Log into the linked user. Never create. |
| Sign in, `subject` unknown, phone unknown | Create a user. Link the identity. |
| Sign in with Google, `subject` unknown, **email matches an existing user** | **DO NOT auto-link.** See below. |
| Link Google to the *current* session's user | Allowed, only if that `subject` is linked to no one. |
| Link a phone already verified on another account | **Refuse.** Conflict. |
| Unlink the last identity | **Refuse.** An account with no way in is a support ticket forever. |
| Unlink an identity that is the canonical phone | **Refuse** while it is canonical. |

### The rule that matters most: never auto-link on email

The tempting shortcut is "Google says the email is `x@y.com`, we have a user with
that email, log them in." **This is a takeover primitive.** It trusts that the
provider verified the email *and* that our stored email was verified. Ours was
never verified — the current `createUser` accepts any string. So anyone who
registers with a victim's email and later signs in with a real Google account for
that address inherits the victim's account, or vice versa.

Linking therefore requires **proof of both sides in one session**: you must
already be authenticated as the user you are linking to, or you must verify the
phone. Email is never sufficient evidence of anything in this system.

### Provider trust boundary

A Google ID token is verified **server-side** against Google's public keys, with
`aud` checked against our client ID and `iss`/`exp` validated. The client sends
the token; it never sends "I am user X". Same for Apple. A client claim of
identity is not evidence, and this is the difference between OAuth and theatre.

## 4. Migration strategy

The constraint is absolute: **every existing account keeps working, and no login
breaks on deploy day.**

| Phase | Backend | Clients |
|---|---|---|
| **0 — now** | username + PIN | unchanged |
| **1 — additive** | `phone_e164` (NULL), `phone_verified_at`, `AuthIdentity` added. A data migration writes one `AuthIdentity(provider='password', subject=username)` per existing user, so today's login becomes *one provider among several* rather than a special case. `tokenAuth` unchanged. | unchanged; nothing to do |
| **2 — dual-run** | Phone OTP sign-in works alongside PIN. New accounts require a verified phone. | Web/mobile show phone-first, with "use email instead" |
| **3 — prompt** | Legacy users prompted to add a phone at sign-in; **skippable**. | banner/interstitial |
| **4 — enforce** | Phone becomes required for legacy users too — **only when adoption justifies it** (see §9) | — |
| **5 — retire** | `username` removed from the public schema | after no client selects it |

**Phase 1 is the only phase this sprint should ship.** It is pure addition: no
column becomes required, no mutation changes signature, no client notices. That
is what makes it safe to deploy on a Friday and what makes every later phase
reversible.

**The migration must backfill `AuthIdentity` for existing users** — the same
lesson as B1's migration 0005, which grandfathered every live owner to
`approved`. Shipping the schema without the backfill would leave every existing
account with zero linked identities, which the "cannot unlink your last identity"
rule would then read as an account that cannot be touched.

## 5. Sessions and revocation

**This is the part that cannot be done by adding screens.**

W7 asks for "logout everywhere" and "session revocation". Today's JWT is
stateless and unrevocable. There are exactly two honest options:

**(a) Install `graphql_jwt.refresh_token`.** Long-running refresh tokens become
database rows; revoking a row kills the session. `accounts/services.py` already
contains `_invalidate_other_sessions`, which *tries* to revoke
`RefreshToken.objects` inside a `try/except ImportError` — **dead code today**,
written for an app that was never installed. It becomes live under this option.
Cost: a real schema change and a client change (refresh token handling), and the
web client's W0 auth deliberately assumes the single-token sliding-window model.

**(b) A `token_version` integer on `User`, asserted in the JWT payload.**
"Logout everywhere" increments it; every existing token fails its next request.
Cost: one integer column and a payload hook. It cannot revoke *one* session — it
is all-or-nothing.

**Recommendation: (b) now, (a) later if per-device revocation is a product
requirement.** (b) delivers the security property that matters — a compromised
credential can be cut off — for a fraction of the blast radius, and it does not
touch either client. "Active sessions" as a *list* (the profile screen in the
brief) requires (a): you cannot enumerate stateless tokens. **That screen cannot
be built under (b), and should not be faked with a single row saying "this
device".**

## 6. API contract (proposed, additive)

```graphql
# Phone
sendPhoneOtp(phone: String!): SendPhoneOtp        # E.164; anti-enumeration generic reply
verifyPhoneOtp(phone: String!, otp: String!): TokenPayload   # signs in OR completes linking
# OAuth — the client sends a provider token, never an identity claim
signInWithGoogle(idToken: String!): TokenPayload
signInWithApple(identityToken: String!, nonce: String!): TokenPayload
# Linking (requires an authenticated session)
linkGoogle(idToken: String!): AuthIdentityPayload
linkApple(identityToken: String!, nonce: String!): AuthIdentityPayload
linkPhone(phone: String!, otp: String!): AuthIdentityPayload
unlinkProvider(provider: String!): AuthIdentityPayload
# Sessions
logoutEverywhere: LogoutPayload                    # option (b): bumps token_version
# Existing, unchanged — legacy compatibility
tokenAuth(username: String!, pin: String): ...
```

`me` gains `phoneE164`, `phoneVerifiedAt`, `linkedProviders`. All additive. Every
new field needs an entry in `QUERY_POLICY`/`MUTATION_POLICY` (principles rule 2)
and a narrow audience type (rule 3) — `AuthIdentity.subject` is a provider's
user ID and must **never** be public.

## 7. Security review of the proposed surface

| Threat | Mitigation |
|---|---|
| OTP brute force | Reuse `PasswordResetOTP`'s design: hashed OTP, constant-time compare, attempt cap, cooldown, per-IP + per-account rate limit. It is good; do not rewrite it. |
| **Account enumeration via phone** | `sendPhoneOtp` must return the same generic reply whether or not the number exists — the `send_reset_otp` pattern. A phone list is worth more than an email list here. |
| **SMS pumping / toll fraud** | **New risk with no precedent in this codebase.** Every OTP send costs money to an attacker-chosen number. Needs per-phone, per-IP and **global** send caps, plus a spend alarm. This is the risk most likely to be forgotten. |
| Stolen Google token | Verify `aud`/`iss`/`exp` server-side against Google's keys. A token minted for another app must be rejected — checking only the signature is the classic hole. |
| Apple replay | `nonce` bound to the request and single-use. |
| Provider linking takeover | §3: never auto-link on email; require an authenticated session or phone proof. |
| Session revocation | §5. **Not currently possible.** |
| Rate limiter bypass | `AUTH_RATELIMIT` is per-process without `REDIS_URL`. With N web workers the real limit is N×. **This is already true today** and phone OTP makes it expensive rather than merely unsafe. |

## 8. External dependencies — BLOCKING, cannot be invented

Verified against the codebase, not assumed:

1. **No SMS provider exists.** No Twilio/Africa's Talking/Vonage anywhere in the
   dependencies or code. Phone OTP is the centre of this sprint and **the OTP
   cannot reach a phone** without an account, credentials, and a per-message
   budget. The code can be written against a pluggable delivery backend (mirroring
   Django's `EMAIL_BACKEND`, with console delivery in dev) — but a phone login
   that cannot send an SMS is not a working flow, and the brief forbids
   pretending otherwise.
2. **No Google OAuth client ID.** Server-side verification needs `aud`; without
   a Google Cloud project there is nothing to verify against.
3. **No Apple Services ID / signing key.** Requires a paid Apple Developer
   account. Apple Sign In is also **mandatory on iOS** if Google Sign In ships —
   an App Store review rule, so the Flutter client cannot ship one without the
   other.

Until these exist, the honest deliverable is: the identity model, the linking
rules, the migration, and the flows that need no third party — **and a clear
"phone sign-in is not yet available" rather than a button that fails**.

## 9. Product decisions — DECIDED (W7)

Recorded here because the reasoning matters more than the answer, and the next
person to read this file will otherwise re-litigate it.

1. **Legacy accounts with no phone → PROMPT, BUT SKIPPABLE.** New accounts require
   a verified phone; existing users are asked at sign-in and may decline. Nobody
   is locked out of their bookings. **The cost, stated plainly: two identity
   classes coexist indefinitely, and "every account has a phone" is never true.
   Nothing may be built on that assumption** — any code that treats phone as
   guaranteed is a bug waiting for a legacy user. Phase 4 stays unscheduled until
   real adoption numbers justify it, and it needs the support escape hatch in
   §9.1a first.
   - 9.1a **Prerequisite for any future enforcement**: there is no admin
     "set/reset a user's phone" path. Without one, an enforced cut-off would lock
     users out with no way for support to rescue them. Build that before phase 4
     is ever considered.
2. **SMS delivery → PLUGGABLE ADAPTER, console in dev.** Mirrors Django's
   `EMAIL_BACKEND`. The flow is wired end-to-end; when no provider is configured
   it refuses honestly rather than pretending to send. A real adapter (Twilio /
   Africa's Talking — the latter is generally better for Ethiopian numbers) is one
   class, added when an account exists.
3. **Session revocation → `token_version` column.** One integer on `User`,
   asserted in the JWT payload; `logoutEverywhere` increments it and every live
   token dies on its next request. Delivers the property the platform actually
   lacks — a compromised credential can be cut off — for one column and zero
   client changes.
   - **Consequence, accepted: the profile "Active sessions" list is NOT built.**
     Stateless tokens cannot be enumerated. A list showing one row saying "this
     device" would be fabricated UI, so the screen is deferred to W8 with
     `refresh_token` rather than faked.

## 9b. Decisions still open

1. **Which SMS provider, and what is the per-message budget?** The adapter is
   provider-agnostic, so this blocks only the real send — not the design. The
   budget sets the abuse ceiling in §7 (SMS pumping), which becomes urgent the
   day a real provider is wired.
2. **One phone per account, or one phone across all accounts?** The design says
   globally unique (a phone identifies a human). This means a shared family phone
   cannot hold two accounts. That is a real product constraint, deliberately
   chosen, and worth confirming.

## 10. What W7 should actually ship

Given §8 and §9, the defensible scope for one sprint:

- **Phase 1 migration**: `phone_e164`, `phone_verified_at`, `AuthIdentity` +
  backfill. Additive, no client impact, unblocks everything else.
- **Phone verification service** behind a pluggable delivery adapter, reusing the
  existing OTP hardening. Console delivery until a provider exists; the flow is
  wired end-to-end and refuses honestly when unconfigured.
- **Linking rules as a service** with the §3 table as its test suite. This is pure
  domain logic, needs no third party, and is where the takeovers live.
- **`token_version` + `logoutEverywhere`** (§5b) — the one security property the
  platform is missing today, at low cost.
- **Web**: the new sign-in screen, with Google/Apple present but **disabled and
  labelled "not yet available"** rather than wired to nothing.

Deferred to W8 with reasons: Google/Apple verification (no credentials), the
Active Sessions list (needs §5a), Flutter native sign-in (needs both, plus the
App Store rule above), and phase 4 enforcement (needs decision §9.1).

---

## Appendix: what this reuses rather than rebuilds

`PasswordResetOTP` is well-built — hashed, constant-time, attempt-limited,
cooldown-gated, enumeration-safe. The phone OTP is the same design with a
different delivery channel and a different subject. `ratelimit.py` already has
the scopes. `accounts/services.py` already has the service-layer split.
`_invalidate_other_sessions` already contains the dead revocation branch that
§5(a) would bring to life.

This sprint adds an identity model. It does not need a new auth stack.
