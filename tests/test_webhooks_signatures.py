"""Unit tests for plancraft.webhooks.signatures.

The differential suite proves the extraction decides like the original. These
prove the properties the package promises on its own terms -- most importantly
the ones that are invariants rather than behaviours: the secret is never read
from the environment, comparison is constant-time, and an unset secret fails
closed.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import inspect
from pathlib import Path

import pytest

from plancraft import webhooks
from plancraft.webhooks import (
    DEFAULT_TOLERANCE_SECONDS,
    SignatureVerificationError,
    WebhookError,
    compute_signature,
    derive_key,
    signed_content,
    verify_standard_webhook,
)

NOW = 1_700_000_000.0
SECRET = base64.b64encode(b"unit-test-signing-key").decode()
BODY = b'{"action":"membership.activated"}'


def header(payload=BODY, wid="msg_1", ts=int(NOW), secret=SECRET) -> str:
    return "v1," + compute_signature(payload, wid, ts, secret)


def verify(**over):
    kwargs = {
        "payload": BODY,
        "webhook_id": "msg_1",
        "webhook_timestamp": str(int(NOW)),
        "webhook_signature": header(),
        "secret": SECRET,
        "now": NOW,
    }
    kwargs.update(over)
    return verify_standard_webhook(**kwargs)


# ------------------------------------------------------------------
# The secret is the caller's, always
# ------------------------------------------------------------------


def test_secret_is_a_required_argument():
    """Omitting it must be a TypeError, not a silent fallback.

    If ``secret`` had a default, "the caller forgot to pass it" and "the
    operator configured one" would be the same call, and the endpoint would
    verify against whatever the package happened to find.
    """
    with pytest.raises(TypeError):
        verify_standard_webhook(BODY, "msg_1", str(int(NOW)), header())  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "env_name",
    [
        "WHOP_WEBHOOK_SECRET",
        "PLANCRAFT_WEBHOOK_SECRET",
        "WEBHOOK_SECRET",
        "STANDARD_WEBHOOKS_SECRET",
    ],
)
def test_environment_is_never_consulted(monkeypatch, env_name):
    """A populated environment must not rescue an empty secret.

    Mozbridge keeps its secret in the process environment on purpose, so that
    verification survives Vault being sealed. That is the host's decision. A
    package that reached for it would overrule that choice everywhere else and
    would make "unset" indistinguishable from "not passed".
    """
    monkeypatch.setenv(env_name, SECRET)
    with pytest.raises(SignatureVerificationError):
        verify(secret="")


def test_no_module_in_the_package_can_read_the_environment():
    """Structural guard: behaviour tests cannot prove a negative here.

    A future edit could add an ``os.getenv`` fallback on a path this suite does
    not reach. Checked over the AST rather than the raw text, so the prose in
    the module docstrings -- which names ``os.getenv`` precisely to explain why
    it is absent -- does not trip it.
    """
    pkg_dir = Path(webhooks.__file__).parent
    for path in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
                assert "os" not in names, f"{path.name} imports os"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "os", f"{path.name} imports from os"
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"getenv", "environ"}, f"{path.name} reads the environment"


def test_the_signatures_module_defines_no_classes():
    """Functions only, so there is nowhere for a secret to live between calls.

    A ``Verifier(secret=...)`` is the obvious next refactor and it is the one
    thing this module must not have: it would hold the secret in instance
    state for the process lifetime. Asserted structurally because "we agreed
    not to" is not a constraint.
    """
    tree = ast.parse(Path(webhooks.signatures.__file__).read_text())
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert classes == [], f"signatures.py grew a class that could retain state: {classes}"


def test_secret_parameter_has_no_default():
    """Belt to the TypeError test's braces, and it is the invariant that matters."""
    param = inspect.signature(verify_standard_webhook).parameters["secret"]
    assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize("secret", ["", None])
def test_unset_secret_fails_closed(secret):
    with pytest.raises(SignatureVerificationError):
        verify(secret=secret)


# ------------------------------------------------------------------
# Constant-time comparison
# ------------------------------------------------------------------


def test_comparison_is_constant_time():
    """Structural, because a timing property is not observable from a unit test.

    ``==`` on the MAC would pass every functional test in this file and leak
    the expected signature byte by byte. The only reliable check is that the
    short-circuiting operator is not in the source at all.
    """
    src = inspect.getsource(webhooks.signatures)
    assert "hmac.compare_digest" in src
    body = src.split("def verify_standard_webhook")[1]
    assert "== expected" not in body
    assert "candidate == " not in body


# ------------------------------------------------------------------
# Happy path and header parsing
# ------------------------------------------------------------------


def test_valid_signature_returns_none():
    """Returns None on success rather than True.

    A bool return is one missing ``not`` away from accepting everything, and
    that mistake reads fine in review.
    """
    assert verify() is None


def test_any_matching_v1_part_accepts():
    good = header()
    assert verify(webhook_signature=f"v1,AAAA {good}") is None
    assert verify(webhook_signature=f"{good} v1,AAAA") is None
    assert verify(webhook_signature=f"v2,AAAA {good} v3,BBBB") is None


def test_unknown_versions_alone_are_rejected_not_crashed():
    with pytest.raises(SignatureVerificationError):
        verify(webhook_signature="v2," + compute_signature(BODY, "msg_1", int(NOW), SECRET))


@pytest.mark.parametrize(
    "bad", ["", "   ", ",", "v1", "v1,", "nonsense", "v1,!!!!", "v1,éé", "v9,x"]
)
def test_malformed_signature_headers_raise_package_error(bad):
    """Never a TypeError, ValueError or binascii error escaping to the host.

    Anything other than the package's own type reaches a route as a 500, and a
    verifier that 500s gets "fixed" by being wrapped in try/except.
    """
    with pytest.raises(SignatureVerificationError):
        verify(webhook_signature=bad)


@pytest.mark.parametrize("wid,wts,wsig", [("", "x", "y"), ("a", "", "y"), ("a", "x", "")])
def test_missing_headers_rejected(wid, wts, wsig):
    with pytest.raises(SignatureVerificationError):
        verify(webhook_id=wid, webhook_timestamp=wts, webhook_signature=wsig)


# ------------------------------------------------------------------
# Replay window
# ------------------------------------------------------------------


def test_default_tolerance_is_five_minutes():
    assert DEFAULT_TOLERANCE_SECONDS == 300


@pytest.mark.parametrize("offset", [0, 299, -299, 300, -300])
def test_within_window_accepted(offset):
    ts = int(NOW) + offset
    assert verify(webhook_timestamp=str(ts), webhook_signature=header(ts=ts)) is None


@pytest.mark.parametrize("offset", [301, -301, 86_400, -86_400])
def test_outside_window_rejected_in_both_directions(offset):
    """A future timestamp is as suspicious as a stale one.

    A one-sided check is trivially defeated: send a timestamp far enough ahead
    and the delivery stays replayable for as long as you like.
    """
    ts = int(NOW) + offset
    with pytest.raises(SignatureVerificationError):
        verify(webhook_timestamp=str(ts), webhook_signature=header(ts=ts))


def test_tolerance_is_caller_overridable():
    ts = int(NOW) - 1000
    sig = header(ts=ts)
    with pytest.raises(SignatureVerificationError):
        verify(webhook_timestamp=str(ts), webhook_signature=sig)
    assert verify(webhook_timestamp=str(ts), webhook_signature=sig, tolerance_seconds=2000) is None


def test_zero_tolerance_permits_only_the_exact_second():
    ts = int(NOW)
    assert (
        verify(webhook_timestamp=str(ts), webhook_signature=header(ts=ts), tolerance_seconds=0)
        is None
    )
    with pytest.raises(SignatureVerificationError):
        verify(
            webhook_timestamp=str(ts - 1),
            webhook_signature=header(ts=ts - 1),
            tolerance_seconds=0,
        )


def test_now_defaults_to_the_wall_clock():
    """The injectable clock must not become the only clock.

    Signed against real time with ``now`` omitted, so a default that silently
    resolved to 0 or None would fail here.
    """
    import time as _time

    ts = int(_time.time())
    assert (
        verify_standard_webhook(
            BODY, "msg_1", str(ts), "v1," + compute_signature(BODY, "msg_1", ts, SECRET), SECRET
        )
        is None
    )


@pytest.mark.parametrize("bad", ["abc", "", "1.5", "1e5", "0x1f", "nan", "inf"])
def test_non_integer_timestamp_rejected(bad):
    with pytest.raises(SignatureVerificationError):
        verify(webhook_timestamp=bad)


# ------------------------------------------------------------------
# Key derivation and the signed byte string
# ------------------------------------------------------------------


def test_derive_key_base64_decodes():
    assert derive_key(base64.b64encode(b"hello").decode()) == b"hello"


def test_derive_key_strips_the_whsec_prefix_before_decoding():
    assert derive_key("whsec_" + base64.b64encode(b"hello").decode()) == b"hello"


def test_derive_key_falls_back_to_raw_bytes_for_non_base64():
    """A hand-typed plaintext secret must work rather than 401 mysteriously."""
    assert derive_key("not+base64!!") == b"not+base64!!"


def test_derive_key_fallback_keeps_the_prefix():
    """Pinned quirk, inherited from the original -- see derive_key's docstring.

    Wrong on the merits, but every already-configured plaintext secret was
    validated against it. Changing it would invalidate live signatures
    silently, so it is a test rather than a fix.
    """
    assert derive_key("whsec_plaintext!") == b"whsec_plaintext!"


def test_signed_content_is_id_dot_timestamp_dot_body():
    assert signed_content("msg_1", 1700000000, b"BODY") == b"msg_1.1700000000.BODY"


def test_signed_content_uses_the_parsed_integer():
    """So padded, plus-signed, underscored and Unicode-digit headers all agree."""
    assert signed_content("m", int(" 1_700 "), b"") == b"m.1700."


def test_compute_signature_matches_a_hand_rolled_hmac():
    """Independent recomputation from the spec, not a call back into the module."""
    key = base64.b64decode(SECRET, validate=True)
    expected = base64.b64encode(
        hmac.new(key, b"msg_1.1700000000." + BODY, hashlib.sha256).digest()
    ).decode()
    assert compute_signature(BODY, "msg_1", 1700000000, SECRET) == expected


def test_signature_covers_the_body_byte_for_byte():
    """Re-serialised JSON never matches; only the raw bytes do."""
    for tampered in (BODY + b" ", b'{"action": "membership.activated"}', BODY[:-1], b""):
        with pytest.raises(SignatureVerificationError):
            verify(payload=tampered)


def test_signature_is_bound_to_the_delivery_id():
    with pytest.raises(SignatureVerificationError):
        verify(webhook_id="msg_2")


def test_signature_is_bound_to_the_timestamp():
    ts = int(NOW) + 10
    with pytest.raises(SignatureVerificationError):
        verify(webhook_timestamp=str(ts))  # header still signed for int(NOW)


def test_signature_is_bound_to_the_secret():
    other = base64.b64encode(b"a-different-key").decode()
    with pytest.raises(SignatureVerificationError):
        verify(secret=other)


# ------------------------------------------------------------------
# Error surface
# ------------------------------------------------------------------


def test_signature_error_is_a_webhook_error():
    assert issubclass(SignatureVerificationError, WebhookError)
    assert issubclass(WebhookError, Exception)


def test_client_detail_is_uniform_across_every_rejection_reason():
    """The response body must not distinguish the failure modes.

    A specific 4xx is a probe oracle: it tells a forger which check they got
    wrong, one at a time. The reason goes to logs via str(exc); only
    client_detail goes over the wire.
    """
    reasons = []
    for kwargs in (
        {"secret": ""},
        {"webhook_id": ""},
        {"webhook_timestamp": "nope"},
        {"webhook_timestamp": str(int(NOW) - 9999)},
        {"webhook_signature": "v1,AAAA"},
    ):
        with pytest.raises(SignatureVerificationError) as exc:
            verify(**kwargs)
        assert exc.value.client_detail == "invalid signature"
        reasons.append(str(exc.value))

    # The log-facing messages, by contrast, must actually distinguish them --
    # otherwise operators cannot tell a clock skew from a wrong secret.
    assert len(set(reasons)) == len(reasons), reasons
