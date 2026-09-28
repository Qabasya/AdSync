"""Тесты проверки подписи запросов сайта (`auth.py`)."""

import hashlib
import hmac

from auth import SignatureVerifier, canonical

_NOW = 1_700_000_000.0


def _verifier(secret: str = "topsecret", *, now: float = _NOW) -> SignatureVerifier:
    return SignatureVerifier(secret, max_skew_seconds=300, now=lambda: now)


def _sign(
    body: bytes, *, method: str = "POST", path: str = "/v1/jobs", ts: str = "1700000000"
) -> str:
    return hmac.new(b"topsecret", canonical(method, path, ts, body), hashlib.sha256).hexdigest()


def test_canonical_matches_site_formula() -> None:
    assert canonical("post", "/v1/jobs", "1700000000", b'{"a":1}') == (
        b'POST\n/v1/jobs\n1700000000\n{"a":1}'
    )


def test_accepts_reference_vector_from_site() -> None:
    # Тот же эталон, что в юнит-тесте сайта:
    # `AdRequestSignerTest::test_signature_matches_reference_hmac`.
    signature = hmac.new(
        b"topsecret", b'POST\n/v1/jobs\n1700000000\n{"a":1}', hashlib.sha256
    ).hexdigest()

    assert _verifier().verify(
        method="POST", path="/v1/jobs", timestamp="1700000000", signature=signature, body=b'{"a":1}'
    )


def test_rejects_tampered_body() -> None:
    signature = _sign(b'{"a":1}')

    assert not _verifier().verify(
        method="POST", path="/v1/jobs", timestamp="1700000000", signature=signature, body=b'{"a":2}'
    )


def test_rejects_signature_replayed_on_other_path_or_method() -> None:
    signature = _sign(b"{}")

    assert not _verifier().verify(
        method="POST", path="/v1/reconcile", timestamp="1700000000", signature=signature, body=b"{}"
    )
    assert not _verifier().verify(
        method="GET", path="/v1/jobs", timestamp="1700000000", signature=signature, body=b"{}"
    )


def test_rejects_stale_timestamp() -> None:
    signature = _sign(b"{}")

    assert not _verifier(now=_NOW + 301).verify(
        method="POST", path="/v1/jobs", timestamp="1700000000", signature=signature, body=b"{}"
    )
    assert _verifier(now=_NOW + 299).verify(
        method="POST", path="/v1/jobs", timestamp="1700000000", signature=signature, body=b"{}"
    )


def test_rejects_missing_headers_and_empty_secret() -> None:
    signature = _sign(b"{}")

    assert not _verifier().verify(
        method="POST", path="/v1/jobs", timestamp="", signature=signature, body=b"{}"
    )
    assert not _verifier().verify(
        method="POST", path="/v1/jobs", timestamp="1700000000", signature="", body=b"{}"
    )
    assert not _verifier("").verify(
        method="POST", path="/v1/jobs", timestamp="1700000000", signature=signature, body=b"{}"
    )
