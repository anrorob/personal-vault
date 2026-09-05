"""Byte-exact Vault Supplier LAN server-authentication protocol v1."""

from __future__ import annotations

from base64 import b64decode, b64encode, urlsafe_b64decode
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


LAN_PROTOCOL_VERSION = 1
LAN_KEY_ALGORITHM = "ECDSA_P256_SHA256"
VERIFY_PATH = "/api/vault-supplier/lan/verify"
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DNS_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z", re.ASCII | re.IGNORECASE)


class LanEndpointHintConfigurationError(RuntimeError):
    """A configured TLS listener cannot yield an unambiguous location hint."""


def lan_endpoint_hint() -> str | None:
    """Read location from the listener's certificate SAN and existing port.

    This deliberately does not authenticate the certificate or the endpoint.
    The client still needs normal TLS validation and pinned LAN identity proof.
    No configured listener certificate means no hint, even if a port is set.
    """
    certificate_path = os.getenv("PV_VAULT_SUPPLIER_LAN_CERTIFICATE_PATH")
    if certificate_path is None:
        return None
    try:
        if not certificate_path or certificate_path != certificate_path.strip():
            raise ValueError("invalid certificate path")
        # Do not invent a port using the LAN protocol's historical default.
        configured_port = os.getenv("PV_VAULT_SUPPLIER_LAN_PORT", "")
        if not re.fullmatch(r"[1-9][0-9]{0,4}", configured_port):
            raise ValueError("explicit listener port required")
        port = lan_port()
        certificate = x509.load_pem_x509_certificate(Path(certificate_path).read_bytes())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        if len(names) != 1 or len(names[0]) > 253 or not _DNS_HOST_RE.fullmatch(names[0]):
            raise ValueError("one unambiguous DNS SAN required")
        return f"https://{names[0].lower()}:{port}"
    except (OSError, ValueError, x509.ExtensionNotFound, x509.DuplicateExtension, ServerIdentityUnavailable):
        raise LanEndpointHintConfigurationError("Invalid Vault Supplier LAN endpoint hint configuration") from None


class ServerIdentityUnavailable(RuntimeError):
    """The deployment did not provide a usable server-authentication key."""


def lan_port() -> int:
    value = os.getenv("PV_VAULT_SUPPLIER_LAN_PORT", "")
    try:
        port = int(value)
    except ValueError as error:
        raise ServerIdentityUnavailable("Invalid Vault Supplier LAN port") from error
    if not 1 <= port <= 65535:
        raise ServerIdentityUnavailable("Invalid Vault Supplier LAN port")
    return port


@dataclass(frozen=True)
class LanServerIdentity:
    private_key: ec.EllipticCurvePrivateKey
    public_key_spki_der: bytes
    key_id_sha256: str

    @classmethod
    def load(cls) -> "LanServerIdentity":
        configured_path = os.getenv("PV_VAULT_SUPPLIER_SERVER_IDENTITY_KEY_PATH")
        if not configured_path:
            raise ServerIdentityUnavailable("Vault Supplier signing identity is not configured")
        path = Path(configured_path)
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except Exception as error:
            raise ServerIdentityUnavailable("Vault Supplier LAN server identity is unavailable") from error
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ServerIdentityUnavailable("Vault Supplier LAN server identity must be ECDSA P-256")
        public_der = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return cls(key, public_der, hashlib.sha256(public_der).hexdigest())

    def pairing_response(self) -> dict[str, str]:
        return {
            "key_algorithm": LAN_KEY_ALGORITHM,
            "public_key_spki_der_base64": b64encode(self.public_key_spki_der).decode("ascii"),
            "key_id_sha256": self.key_id_sha256,
        }

    def sign(self, payload: bytes) -> str:
        return b64encode(self.private_key.sign(payload, ec.ECDSA(hashes.SHA256()))).decode("ascii")


def get_lan_server_identity() -> LanServerIdentity:
    return LanServerIdentity.load()


def canonical_vault_id(vault_id: UUID) -> str:
    return str(vault_id)


def validate_nonce(value: str) -> str:
    if not _NONCE_RE.fullmatch(value):
        raise ValueError("invalid nonce")
    try:
        decoded = urlsafe_b64decode(value.encode("ascii") + b"=")
    except Exception as error:
        raise ValueError("invalid nonce") from error
    if len(decoded) != 32:
        raise ValueError("invalid nonce")
    return value


def canonical_payload(*, vault_id: UUID, nonce: str, key_id: str, port: int, receiver_available: bool, resumable_upload_supported: bool) -> bytes:
    return (
        "PV-VS-LAN-1\n"
        f"vault_id={canonical_vault_id(vault_id)}\n"
        f"nonce={nonce}\n"
        f"server_key_id={key_id}\n"
        f"port={port}\n"
        f"receiver_available={int(receiver_available)}\n"
        f"resumable_upload_supported={int(resumable_upload_supported)}\n"
    ).encode("utf-8")


def verify_signature(public_key_spki_der: bytes, payload: bytes, signature_der_base64: str) -> bool:
    """Test/support helper matching the .NET-compatible DER protocol signature."""
    try:
        key = serialization.load_der_public_key(public_key_spki_der)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            return False
        key.verify(b64decode(signature_der_base64, validate=True), payload, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False
