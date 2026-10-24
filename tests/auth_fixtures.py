"""A software passkey for the login tests: ES256, "none" attestation, discoverable,
user verified. It answers the options the server sends exactly as a browser with a
platform authenticator would, so the server's checks run unchanged."""
from __future__ import annotations

import hashlib
import json
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, encode_cbor


class SoftPasskey:
    def __init__(self, rp_id: str = "localhost", origin: str = "http://localhost:8766"):
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = secrets.token_bytes(32)
        self.sign_count = 0
        self.user_handle = b""

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge,
                           "origin": origin or self.origin, "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str | None = None,
               rp_id: str | None = None) -> dict:
        """navigator.credentials.create() for these creation options."""
        self.user_handle = base64url_to_bytes(options["user"]["id"])
        nums = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"),
                -3: nums.y.to_bytes(32, "big")}
        auth_data = (hashlib.sha256((rp_id or self.rp_id).encode()).digest()
                     + bytes([0x45]) + self.sign_count.to_bytes(4, "big")
                     + bytes(16) + len(self.id).to_bytes(2, "big") + self.id
                     + encode_cbor(cose))
        att = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cd = self._client_data("webauthn.create", options["challenge"], origin)
        return {"id": bytes_to_base64url(self.id), "rawId": bytes_to_base64url(self.id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cd),
                             "attestationObject": bytes_to_base64url(att),
                             "transports": ["internal"]}}

    def get(self, options: dict, origin: str | None = None) -> dict:
        """navigator.credentials.get() for these request options."""
        self.sign_count += 1
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05])
                     + self.sign_count.to_bytes(4, "big"))
        cd = self._client_data("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(),
                            ec.ECDSA(hashes.SHA256()))
        return {"id": bytes_to_base64url(self.id), "rawId": bytes_to_base64url(self.id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cd),
                             "authenticatorData": bytes_to_base64url(auth_data),
                             "signature": bytes_to_base64url(sig),
                             "userHandle": bytes_to_base64url(self.user_handle)}}
