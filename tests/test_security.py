"""
Security and privacy tests:
Verifies AES-256-GCM authenticated encryption, fresh nonces, AAD tamper protection,
HMAC phone lookup consistency, and absence of plaintext PII in database.
"""
import base64
import os
import tempfile
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from solution.crypto import CryptoManager
from solution.database import get_connection, init_db
from solution.leads import LeadIngestor
from solution.schemas import SchemaRegistry


def test_aes_gcm_fresh_nonce_per_encryption():
    crypto = CryptoManager.generate()
    c1 = crypto.encrypt_field("SecretName", "ctx")
    c2 = crypto.encrypt_field("SecretName", "ctx")

    # Distinct ciphertexts due to fresh 96-bit nonce
    assert c1 != c2

    # Both decrypt correctly
    assert crypto.decrypt_field(c1, "ctx") == "SecretName"
    assert crypto.decrypt_field(c2, "ctx") == "SecretName"


def test_aes_gcm_tampering_and_aad_mismatch():
    crypto = CryptoManager.generate()
    token = crypto.encrypt_field("SensitiveValue", "workflow:field:ref1")

    # Decrypt with correct AAD succeeds
    assert crypto.decrypt_field(token, "workflow:field:ref1") == "SensitiveValue"

    # Decrypt with altered AAD fails (tampering/transplanting prevention)
    with pytest.raises(Exception):
        crypto.decrypt_field(token, "workflow:field:ref2")

    # Corrupting ciphertext byte fails authentication tag
    raw = bytearray(base64.b64decode(token))
    raw[-1] ^= 0x01  # Flip tag bit
    corrupted_token = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(Exception):
        crypto.decrypt_field(corrupted_token, "workflow:field:ref1")


def test_hmac_deterministic_lookup_and_key_isolation():
    k1 = os.urandom(32)
    k2 = os.urandom(32)
    c1 = CryptoManager(k1, k1)
    c2 = CryptoManager(k1, k2)

    phone = "+919876543210"
    token1 = c1.phone_lookup_hmac(phone)
    token1_again = c1.phone_lookup_hmac(phone)
    token2 = c2.phone_lookup_hmac(phone)

    # Deterministic under same key
    assert token1 == token1_again
    # Isolated under different key
    assert token1 != token2


def test_no_plaintext_pii_in_database():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_p = Path(tmpdir)
        db_path = tmp_p / "audit.db"
        init_db(db_path)
        conn = get_connection(db_path)
        crypto = CryptoManager.generate()
        registry = SchemaRegistry("assignment-a-data/schemas")

        ingestor = LeadIngestor(conn, crypto, registry)
        csv_file = tmp_p / "batch_001.csv"
        secret_name = "SuperSecretCustomer"
        secret_phone = "9876543210"
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            f.write("customer_name,phone,policy_number\n")
            f.write(f"{secret_name},{secret_phone},MOT/2026/999\n")

        ingestor.ingest_csv(csv_file, "motor_renewal", "drop_1")
        conn.close()

        # Read raw database binary content
        with open(db_path, "rb") as f:
            db_bytes = f.read()

        assert secret_name.encode("utf-8") not in db_bytes
        assert secret_phone.encode("utf-8") not in db_bytes
        assert b"+919876543210" not in db_bytes


def test_missing_key_file_fail_closed():
    """Verify that attempting to load a missing key file fails safely and never regenerates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nonexistent = Path(tmpdir) / "keys.json"
        with pytest.raises(FileNotFoundError):
            CryptoManager.load(nonexistent)
