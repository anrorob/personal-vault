from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.federation import FEDERATION_PROTOCOL_VERSION, canonical_json, sign_envelope, verify_envelope


def envelope() -> dict[str, object]:
    return {
        "protocol_version": FEDERATION_PROTOCOL_VERSION,
        "event_id": str(uuid4()),
        "origin_vault_id": str(uuid4()),
        "target_vault_id": str(uuid4()),
        "event_type": "share_activated",
        "timestamp": datetime.now(UTC).isoformat(),
        "share": {"share_id": str(uuid4()), "asset_id": str(uuid4())},
    }


def test_federation_envelope_is_canonical_and_tamper_evident() -> None:
    event = envelope()
    signature = sign_envelope(event, "pairing-key-which-is-long-enough-for-stage-six")
    assert verify_envelope(event, signature, "pairing-key-which-is-long-enough-for-stage-six")
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    event["event_type"] = "share_revoked"
    assert not verify_envelope(event, signature, "pairing-key-which-is-long-enough-for-stage-six")


def test_pairing_signature_never_accepts_another_vault_key() -> None:
    event = envelope()
    signature = sign_envelope(event, "a" * 32)
    assert not verify_envelope(event, signature, "b" * 32)


def test_pairing_signature_is_order_independent_but_rejects_extra_fields() -> None:
    event = envelope()
    signature = sign_envelope(event, "a" * 32)
    assert verify_envelope(dict(reversed(list(event.items()))), signature, "a" * 32)
    assert not verify_envelope({**event, "unexpected": True}, signature, "a" * 32)
