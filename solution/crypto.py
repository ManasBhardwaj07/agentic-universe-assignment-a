"""
Cryptographic services: AES-256-GCM field encryption and HMAC-SHA256 lookup tokens.
Protects PII at rest and enables deterministic phone matching without plaintext storage.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_KEYS_PATH = Path(".local/keys.json")


class CryptoManager:
    """Manages secret keys, authenticated PII encryption/decryption, and HMAC phone tokens."""

    def __init__(self, aes_key: bytes, hmac_key: bytes):
        if len(aes_key) != 32:
            raise ValueError(f"AES key must be exactly 32 bytes (256 bits), got {len(aes_key)}")
        if len(hmac_key) != 32:
            raise ValueError(f"HMAC key must be exactly 32 bytes (256 bits), got {len(hmac_key)}")
        self._aes_key = aes_key
        self._hmac_key = hmac_key
        self._aesgcm = AESGCM(aes_key)

    @classmethod
    def generate(cls) -> "CryptoManager":
        """Generate fresh cryptographically random 256-bit keys."""
        aes_key = os.urandom(32)
        hmac_key = os.urandom(32)
        return cls(aes_key, hmac_key)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_KEYS_PATH) -> "CryptoManager":
        """Load keys from path. Fails if key file is missing (fail-closed for existing db)."""
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Cryptographic key file not found at: {p}")
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        aes_key = base64.b64decode(data["aes_key"])
        hmac_key = base64.b64decode(data["hmac_key"])
        return cls(aes_key, hmac_key)

    @classmethod
    def load_or_create(cls, path: Path | str = DEFAULT_KEYS_PATH) -> "CryptoManager":
        """Load keys from local path or generate them if absent (first-time initialization)."""
        p = Path(path).resolve()
        if p.exists():
            return cls.load(p)

        # Create and persist new keys
        p.parent.mkdir(parents=True, exist_ok=True)
        manager = cls.generate()
        manager.save(p)
        return manager

    def save(self, path: Path | str = DEFAULT_KEYS_PATH) -> None:
        """Persist keys to disk encoded in base64."""
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "aes_key": base64.b64encode(self._aes_key).decode("ascii"),
            "hmac_key": base64.b64encode(self._hmac_key).decode("ascii"),
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def encrypt_field(self, plaintext: str, aad_context: str = "") -> str:
        """
        Encrypt plaintext string using AES-256-GCM with a fresh random 96-bit nonce.
        Binds aad_context to prevent ciphertext transplanting across fields.
        Returns base64 encoded string: base64(nonce || ciphertext || tag).
        """
        nonce = os.urandom(12)  # 96-bit nonce
        aad = aad_context.encode("utf-8") if aad_context else None
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
        # In cryptography library, AESGCM.encrypt appends 16-byte tag to ciphertext
        combined = nonce + ciphertext
        return base64.b64encode(combined).decode("ascii")

    def decrypt_field(self, token: str, aad_context: str = "") -> str:
        """
        Decrypt and authenticate base64 encoded token: base64(nonce || ciphertext || tag).
        """
        raw = base64.b64decode(token.encode("ascii"))
        if len(raw) < 12 + 16:
            raise ValueError("Ciphertext token too short")
        nonce = raw[:12]
        ciphertext = raw[12:]
        aad = aad_context.encode("utf-8") if aad_context else None
        plaintext_bytes = self._aesgcm.decrypt(nonce, ciphertext, aad)
        return plaintext_bytes.decode("utf-8")

    def phone_lookup_hmac(self, canonical_phone: str) -> str:
        """
        Compute deterministic HMAC-SHA256 token for phone matching.
        Canonical phone must be formatted as +91XXXXXXXXXX.
        """
        return hmac.new(
            self._hmac_key,
            canonical_phone.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
