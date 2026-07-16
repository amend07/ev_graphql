"""SMS delivery (Sprint W7).

Mirrors Django's ``EMAIL_BACKEND``: an interface plus swappable adapters, chosen
by setting. There is no SMS provider on this platform yet — no Twilio, no Africa's
Talking, no account, no budget — so the decision (recorded in
docs/IDENTITY_ARCHITECTURE.md §9.2) is to build the flow end-to-end against this
seam and add a real adapter when one exists.

THE HONESTY RULE: when no provider is configured, this **refuses**. It does not
pretend to send. A phone login whose OTP silently goes nowhere is worse than a
disabled button — the user waits for a code that was never sent, and support
cannot tell them why.

  SMS_BACKEND = 'console'   # dev: prints the code to the log
  SMS_BACKEND = 'disabled'  # prod-until-configured: refuses, loudly
  SMS_BACKEND = 'twilio'    # not written — see TwilioSmsBackend below
"""

import logging

from django.conf import settings

from ev_backend.errors import APIError

logger = logging.getLogger('accounts.sms')


class SmsUnavailable(APIError):
    """No SMS provider is configured, so the code cannot be delivered.

    A typed failure rather than a generic one: the client needs to distinguish
    "we could not send" (our problem, retrying will not help) from "wrong code"
    (the user's problem). Surfaces as `extensions.code = 'sms_unavailable'`.
    """

    code = 'sms_unavailable'


class SmsBackend:
    """Send one message. Adapters implement this and nothing else."""

    def send(self, *, to_e164: str, body: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleSmsBackend(SmsBackend):
    """Development only: writes the message to the log instead of sending it.

    The message body contains the OTP in clear text, which is exactly why this
    must never be selected in production — the code would sit in the log
    aggregator for anyone with log access. `settings.py` refuses to start on a
    non-DEBUG deploy that selects it (see the production check).
    """

    def send(self, *, to_e164: str, body: str) -> None:
        logger.warning('[console-sms] to=%s body=%s', to_e164, body)


class DisabledSmsBackend(SmsBackend):
    """The honest default until a provider exists.

    Every send raises. That is the point: the phone flow is fully built, and it
    tells the truth about not being deliverable rather than accepting a number
    and silently dropping the message.
    """

    def send(self, *, to_e164: str, body: str) -> None:
        raise SmsUnavailable(
            "Phone sign-in is not available yet. Use email sign-in for now."
        )


#: Adapters by setting value. A real provider is added here as one class:
#:
#:   class TwilioSmsBackend(SmsBackend):
#:       def send(self, *, to_e164, body):
#:           # client.messages.create(to=to_e164, from_=..., body=body)
#:
#: It is not written because there is no account, and a class that cannot be run
#: or tested against anything is not an implementation — it is a guess with an
#: import statement.
BACKENDS = {
    'console': ConsoleSmsBackend,
    'disabled': DisabledSmsBackend,
}


def get_sms_backend() -> SmsBackend:
    """The configured adapter. Defaults to `disabled` — never to a silent no-op."""
    name = getattr(settings, 'SMS_BACKEND', 'disabled')
    backend = BACKENDS.get(name)
    if backend is None:
        # An unknown name is a deploy mistake. Fail closed: refusing to send is
        # recoverable, silently dropping verification codes is not.
        logger.error('Unknown SMS_BACKEND %r; refusing to send.', name)
        return DisabledSmsBackend()
    return backend()


def sms_configured() -> bool:
    """Whether a phone code can actually be delivered.

    Lets the API tell a client "phone sign-in is unavailable" up front, instead
    of after the user has typed their number and waited for a code.
    """
    return not isinstance(get_sms_backend(), DisabledSmsBackend)
