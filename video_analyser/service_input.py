"""Initial fail-closed KEN input evidence."""
try:
    from ken_attestation import ATTESTATION_VERSION
except ModuleNotFoundError:
    from app.ken_attestation import ATTESTATION_VERSION


def evidence(body):
    return {"version": ATTESTATION_VERSION, "status": "failed",
            **{k: body.get(k) for k in ("asset_id", "run_id", "input_fingerprint", "request_nonce")}}
