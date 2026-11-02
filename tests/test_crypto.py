import pytest
from nacl.exceptions import CryptoError

from app.crypto import (
    lookup_hash,
    new_token,
    open_with,
    public_key_for,
    seal_for,
)


def test_round_trip_with_own_token():
    token = new_token()
    ciphertext = seal_for(public_key_for(token), "42")
    assert open_with(token, ciphertext) == "42"


def test_other_token_cannot_open():
    ciphertext = seal_for(public_key_for(new_token()), "42")
    with pytest.raises(CryptoError):
        open_with(new_token(), ciphertext)


def test_ciphertext_does_not_contain_plaintext():
    token = new_token()
    ciphertext = seal_for(public_key_for(token), "Alice Wonderland")
    assert b"Alice" not in ciphertext


def test_public_key_is_deterministic_from_token():
    token = new_token()
    assert public_key_for(token) == public_key_for(token)


def test_lookup_hash_does_not_equal_token():
    token = new_token()
    assert lookup_hash(token) != token
    assert lookup_hash(token) == lookup_hash(token)
