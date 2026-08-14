"""Differential identity: the extracted verifier vs the Mozbridge original.

This is the test that matters. ``plancraft.webhooks.signatures`` is an
extraction of ``backend/app/services/whop.py``, and an extraction is only
correct if it decides the same way. "The new tests pass" proves nothing about
that -- the new tests were written against the new code. So both
implementations are run against the same inputs and their accept/reject
decisions compared directly, over the seven known cases and then over fuzzed
input designed to hit the parsing edges (``int('5_0') == 50``, Unicode digits,
padded headers, non-ASCII signature parts, non-base64 secrets).

The original is loaded by file path rather than imported as ``app.services.whop``:
``app/services`` has no ``__init__.py``, and going through the package would
execute ``app/__init__.py`` and drag the host's dependency tree into a test for
a package that must stay standalone. ``whop.py`` imports only stdlib, so a
file-path spec loads it cleanly.

If the host tree is not present -- i.e. plancraft has been vendored elsewhere
or installed from a wheel -- these tests skip. That is honest: the differential
claim is only checkable where both sides exist.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import random
import types
from pathlib import Path

import pytest

from plancraft.webhooks import verify_standard_webhook

# ------------------------------------------------------------------
# Load the reference implementation (read-only; never modified by this suite)
# ------------------------------------------------------------------

# A FROZEN copy of the pre-extraction implementation, captured from git at the
# commit before Mozbridge began delegating to this package.
#
# It deliberately does NOT point at `backend/app/services/whop.py` any more.
# That file now calls `verify_standard_webhook` itself, so comparing against it
# would compare this package with itself and pass no matter what -- a test that
# cannot fail, guarding the one thing most worth guarding. The whole value of a
# differential test is a second, independent implementation, so the reference
# has to stop tracking the thing it is meant to check.
#
# Do not "update" this fixture to match a change in the package. If they
# diverge, either the package regressed or the divergence is intentional and
# belongs in the docstring of `verify_standard_webhook` alongside the non-ASCII
# case already recorded there.
_REFERENCE_PATH = Path(__file__).resolve().parent / "fixtures" / "whop_reference_frozen.py"


def _load_reference() -> types.ModuleType | None:
    if not _REFERENCE_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_whop_reference", _REFERENCE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


whop = _load_reference()

pytestmark = pytest.mark.skipif(
    whop is None,
    reason=f"reference implementation not present at {_REFERENCE_PATH}",
)

# A clock both sides agree on. Without this, each implementation calls
# time.time() at a slightly different instant and an input sitting exactly on
# the tolerance boundary would flake -- which would train everyone to rerun the
# suite instead of reading it.
FIXED_NOW = 1_700_000_000.0
TOLERANCE = 300


@pytest.fixture(autouse=True)
def _freeze_reference_clock(monkeypatch):
    """Pin the reference module's clock.

    Rebinds the module-level ``time`` name inside the loaded reference only.
    Patching ``time.time`` itself would be a process-global change and would
    leak into unrelated tests.
    """
    if whop is None:
        return
    fake = types.SimpleNamespace(time=lambda: FIXED_NOW)
    monkeypatch.setattr(whop, "time", fake)


# ------------------------------------------------------------------
# Harness
# ------------------------------------------------------------------


def _decide_reference(payload, wid, wts, wsig, secret):
    """Run the original. Returns 'accept', or 'reject:<ExcType>'."""
    try:
        # `secret` is always passed explicitly. The original falls back to
        # os.getenv("WHOP_WEBHOOK_SECRET") only when it is None, and this suite
        # must never depend on the ambient environment.
        assert secret is not None
        whop.verify_signature(payload, wid, wts, wsig, secret, tolerance_seconds=TOLERANCE)
        return "accept"
    except Exception as exc:  # noqa: BLE001 - classifying every escape is the point
        return f"reject:{type(exc).__name__}"


def _decide_extracted(payload, wid, wts, wsig, secret):
    """Run the extraction. Returns 'accept', or 'reject:<ExcType>'."""
    try:
        verify_standard_webhook(
            payload, wid, wts, wsig, secret, tolerance_seconds=TOLERANCE, now=FIXED_NOW
        )
        return "accept"
    except Exception as exc:  # noqa: BLE001
        return f"reject:{type(exc).__name__}"


def assert_same_decision(payload, wid, wts, wsig, secret, *, label=""):
    """Both sides must agree on accept vs not-accept.

    Exception *type* is deliberately not compared: the extraction raises its
    own package-owned error, and it converts the original's one escaping
    TypeError into a rejection. What must never differ is whether the request
    gets in.
    """
    ref = _decide_reference(payload, wid, wts, wsig, secret)
    got = _decide_extracted(payload, wid, wts, wsig, secret)
    assert (ref == "accept") == (got == "accept"), (
        f"divergence{' [' + label + ']' if label else ''}: "
        f"reference={ref} extracted={got} "
        f"payload={payload!r} id={wid!r} ts={wts!r} sig={wsig!r} secret={secret!r}"
    )
    return ref, got


def sign(payload: bytes, wid: str, ts: int, secret: str) -> str:
    """Produce a valid header, computed independently of both implementations.

    Written out longhand from the spec rather than calling either module's
    helper. A signer that shares code with the verifier under test can agree
    with it while both are wrong.
    """
    raw = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        key = secret.encode()
    signed = f"{wid}.{ts}.".encode() + payload
    mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return f"v1,{mac}"


SECRET = base64.b64encode(b"plancraft-differential-test-key").decode()
BODY = b'{"action":"membership.activated","data":{"id":"mem_123"}}'
NOW_S = str(int(FIXED_NOW))


# ------------------------------------------------------------------
# The seven known cases
# ------------------------------------------------------------------


def test_case_1_valid_signature_accepted_by_both():
    sig = sign(BODY, "msg_1", int(FIXED_NOW), SECRET)
    ref, got = assert_same_decision(BODY, "msg_1", NOW_S, sig, SECRET, label="valid")
    assert ref == "accept" and got == "accept"


def test_case_2_empty_secret_fails_closed_in_both():
    sig = sign(BODY, "msg_1", int(FIXED_NOW), SECRET)
    ref, got = assert_same_decision(BODY, "msg_1", NOW_S, sig, "", label="empty secret")
    assert ref != "accept" and got != "accept"


@pytest.mark.parametrize(
    "wid,wts,wsig",
    [
        ("", NOW_S, "v1,x"),
        ("msg_1", "", "v1,x"),
        ("msg_1", NOW_S, ""),
        ("", "", ""),
    ],
    ids=["no-id", "no-timestamp", "no-signature", "none-at-all"],
)
def test_case_3_missing_headers_rejected_by_both(wid, wts, wsig):
    ref, got = assert_same_decision(BODY, wid, wts, wsig, SECRET, label="missing header")
    assert ref != "accept" and got != "accept"


@pytest.mark.parametrize("wts", ["not-a-number", "12.5", "1e9", "0x10", "١٢٣abc", "-", "nan"])
def test_case_4_non_integer_timestamp_rejected_by_both(wts):
    sig = sign(BODY, "msg_1", int(FIXED_NOW), SECRET)
    ref, got = assert_same_decision(BODY, "msg_1", wts, sig, SECRET, label="bad timestamp")
    assert ref != "accept" and got != "accept"


def test_case_5_stale_timestamp_rejected_by_both():
    old = int(FIXED_NOW) - TOLERANCE - 1
    sig = sign(BODY, "msg_1", old, SECRET)  # signature itself is perfectly valid
    ref, got = assert_same_decision(BODY, "msg_1", str(old), sig, SECRET, label="stale")
    assert ref != "accept" and got != "accept"


def test_case_6_future_timestamp_rejected_by_both():
    ahead = int(FIXED_NOW) + TOLERANCE + 1
    sig = sign(BODY, "msg_1", ahead, SECRET)
    ref, got = assert_same_decision(BODY, "msg_1", str(ahead), sig, SECRET, label="future")
    assert ref != "accept" and got != "accept"


@pytest.mark.parametrize(
    "wsig",
    [
        "v1,ZGVhZGJlZWY=",  # well-formed base64, wrong MAC
        "v2,{good}",  # right MAC, unknown version tag
        "{good_naked}",  # right MAC, no version tag at all
        "v1{good_naked}",  # version and MAC not comma-separated
        "garbage",
        "v1,",
        ",",
    ],
    ids=["wrong-mac", "v2-only", "untagged", "no-comma", "garbage", "empty-mac", "bare-comma"],
)
def test_case_7_no_matching_v1_rejected_by_both(wsig):
    good = sign(BODY, "msg_1", int(FIXED_NOW), SECRET)
    naked = good.removeprefix("v1,")
    wsig = wsig.replace("{good_naked}", naked).replace("{good}", naked)
    ref, got = assert_same_decision(BODY, "msg_1", NOW_S, wsig, SECRET, label="no v1 match")
    assert ref != "accept" and got != "accept"


# ------------------------------------------------------------------
# Documented behaviours that must survive extraction
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        base64.b64encode(b"binary-secret").decode(),  # plain base64
        "whsec_" + base64.b64encode(b"binary-secret").decode(),  # prefixed base64
        "not+valid+base64!!",  # falls back to raw utf-8 bytes
        "whsec_plaintext-typed-by-hand",  # prefixed AND not base64
        "whsec_",  # decodes to an empty key
        "ünïcode-secret",  # non-ascii, forces the fallback path
    ],
    ids=["b64", "prefixed-b64", "not-b64", "prefixed-plaintext", "prefix-only", "non-ascii"],
)
def test_secret_derivation_agrees(secret):
    """Including the quirk where the fallback keeps the whsec_ prefix.

    If the two implementations derived keys differently, a secret in the
    fallback class would verify under one and 401 under the other -- the exact
    silent break an extraction is supposed to avoid.
    """
    sig = sign(BODY, "msg_1", int(FIXED_NOW), secret)
    ref, got = assert_same_decision(BODY, "msg_1", NOW_S, sig, secret, label="secret form")
    assert ref == "accept" and got == "accept"


@pytest.mark.parametrize(
    "wts_header,signed_as",
    [
        (" 1700000000 ", 1700000000),  # int() strips surrounding whitespace
        ("+1700000000", 1700000000),  # int() accepts a leading plus
        ("1_700_000_000", 1700000000),  # int() accepts PEP 515 underscores
        ("١٧٠٠٠٠٠٠٠٠", 1700000000),  # int() accepts Unicode decimal digits
    ],
    ids=["padded", "plus-sign", "underscores", "unicode-digits"],
)
def test_timestamp_normalisation_agrees(wts_header, signed_as):
    """The signed content uses the parsed int, not the raw header string.

    Easy to "clean up" during extraction by signing over ``webhook_timestamp``.
    That change accepts every case here in isolation and breaks all four in
    combination, so it is pinned.
    """
    sig = sign(BODY, "msg_1", signed_as, SECRET)
    ref, got = assert_same_decision(BODY, "msg_1", wts_header, sig, SECRET, label="ts normalise")
    assert ref == "accept" and got == "accept"


def test_signature_rotation_agrees():
    """Any matching v1 part accepts, wherever it sits in the header."""
    good = sign(BODY, "msg_1", int(FIXED_NOW), SECRET)
    stale = "v1,ZGVhZGJlZWY="
    for header in (
        f"{stale} {good}",
        f"{good} {stale}",
        f"v2,aGVsbG8= {good}",
        f"  {good}   {stale}  ",  # split() collapses arbitrary whitespace
    ):
        ref, got = assert_same_decision(BODY, "msg_1", NOW_S, header, SECRET, label="rotation")
        assert ref == "accept" and got == "accept"


def test_body_is_covered_byte_for_byte():
    """Whitespace-only body changes must break the signature in both."""
    sig = sign(BODY, "msg_1", int(FIXED_NOW), SECRET)
    for tampered in (BODY + b" ", b" " + BODY, BODY.replace(b'"id"', b'"id" '), b"", BODY[:-1]):
        ref, got = assert_same_decision(tampered, "msg_1", NOW_S, sig, SECRET, label="tamper")
        assert ref != "accept" and got != "accept"


def test_id_is_bound_into_the_signature():
    """A signature is not transferable to a different delivery id."""
    sig = sign(BODY, "msg_1", int(FIXED_NOW), SECRET)
    ref, got = assert_same_decision(BODY, "msg_2", NOW_S, sig, SECRET, label="id swap")
    assert ref != "accept" and got != "accept"


def test_the_one_known_divergence_is_the_non_ascii_signature_part():
    """Non-ASCII in a v1 part: original escapes as TypeError, extraction rejects.

    Recorded rather than papered over. ``hmac.compare_digest`` refuses str it
    cannot compare byte-wise, and in the original that TypeError escapes the
    verifier and reaches the route as a 500. Both refuse the request, so the
    accept set is unchanged -- which is the property under test everywhere else
    in this file. This asserts the divergence is exactly the one documented,
    and nothing more.
    """
    header = "v1,éééé"
    ref = _decide_reference(BODY, "msg_1", NOW_S, header, SECRET)
    got = _decide_extracted(BODY, "msg_1", NOW_S, header, SECRET)
    assert ref == "reject:TypeError", ref
    assert got == "reject:SignatureVerificationError", got

    # And the ordering must still match: a valid part BEFORE the bad one wins
    # in both, because both stop at the first match.
    good = sign(BODY, "msg_1", int(FIXED_NOW), SECRET)
    ref, got = assert_same_decision(
        BODY, "msg_1", NOW_S, f"{good} {header}", SECRET, label="good-then-bad"
    )
    assert ref == "accept" and got == "accept"


# ------------------------------------------------------------------
# Fuzz
# ------------------------------------------------------------------

_IDS = ["msg_1", "", "a.b", "a,b", "msg_" + "x" * 200, "ünïcode", "0", "..", "msg 1"]
_SECRETS = [
    SECRET,
    "whsec_" + SECRET,
    "plaintext",
    "whsec_plaintext",
    "not+base64!!",
    "",
    "whsec_",
    "ünïcode",
]
_BODIES = [
    BODY,
    b"",
    b"{}",
    b"\x00\xff\xfe",  # not valid utf-8
    b'{"a": 1}',
    b'{"a":1}',
    b"x" * 5000,
]
_TS_JUNK = [
    "",
    "abc",
    "12.5",
    "1e9",
    " 1700000000 ",
    "+1700000000",
    "1_700_000_000",
    "١٧٠٠٠٠٠٠٠٠",
    "-1700000000",
    "0",
    "9" * 5000,  # trips CPython's int-str conversion length guard
    "99999999999999999999999999",
]
_SIG_JUNK = [
    "",
    "v1,",
    "v1",
    ",",
    "garbage",
    "v1,ZGVhZGJlZWY=",
    "v2,ZGVhZGJlZWY=",
    "v1,éé",
    "v1,%%%",
    "   ",
    "v1,a v1,b v1,c",
]


def _fuzz_case(rng: random.Random):
    """Build one input, valid roughly half the time so accepts are exercised."""
    payload = rng.choice(_BODIES)
    wid = rng.choice(_IDS)
    secret = rng.choice(_SECRETS)

    if rng.random() < 0.5:
        # Start from a well-formed delivery, then corrupt one dimension. Pure
        # random input almost never reaches the MAC comparison, so the
        # interesting half of the branch tree would go unexercised.
        ts_int = int(FIXED_NOW) + rng.randint(-TOLERANCE - 5, TOLERANCE + 5)
        wts = str(ts_int)
        wsig = sign(payload, wid, ts_int, secret)
        roll = rng.random()
        if roll < 0.15:
            payload = payload + b"!"
        elif roll < 0.30:
            wid = wid + "z"
        elif roll < 0.45:
            wts = rng.choice(_TS_JUNK)
        elif roll < 0.60:
            wsig = rng.choice(_SIG_JUNK) + " " + wsig
        elif roll < 0.70:
            secret = rng.choice(_SECRETS)
        elif roll < 0.80:
            wsig = wsig.replace("v1,", "v2,")
    else:
        wts = rng.choice(_TS_JUNK + [str(int(FIXED_NOW) + rng.randint(-400, 400))])
        wsig = rng.choice(_SIG_JUNK)

    return payload, wid, wts, wsig, secret


def test_fuzz_decisions_are_identical():
    """2000 generated inputs; every accept/reject decision must match.

    The accept/reject counters are asserted, not just reported. A differential
    test in which nothing is ever accepted passes trivially while proving
    nothing -- it is the failure mode this style of test is most prone to.
    """
    rng = random.Random(20260811)
    accepts = rejects = 0
    for i in range(2000):
        payload, wid, wts, wsig, secret = _fuzz_case(rng)
        ref, got = assert_same_decision(payload, wid, wts, wsig, secret, label=f"fuzz#{i}")
        if ref == "accept":
            accepts += 1
        else:
            rejects += 1

    assert accepts > 200, f"fuzz never exercised the accept path ({accepts} accepts)"
    assert rejects > 200, f"fuzz never exercised the reject path ({rejects} rejects)"


@pytest.mark.parametrize("seed", [1, 7, 42, 1234, 99991])
def test_fuzz_decisions_are_identical_across_seeds(seed):
    """Extra seeds, so a fix tuned to one RNG stream does not look green."""
    rng = random.Random(seed)
    for i in range(400):
        payload, wid, wts, wsig, secret = _fuzz_case(rng)
        assert_same_decision(payload, wid, wts, wsig, secret, label=f"seed{seed}#{i}")
