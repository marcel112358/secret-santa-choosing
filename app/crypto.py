import hashlib
import hmac
import secrets

from nacl.public import PrivateKey, PublicKey, SealedBox


def new_token() -> str:
    return secrets.token_urlsafe(32)


def lookup_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _private_key(token: str) -> PrivateKey:
    seed = hmac.new(token.encode(), b"sk-x25519", hashlib.sha256).digest()
    return PrivateKey(seed)


def public_key_for(token: str) -> bytes:
    return bytes(_private_key(token).public_key)


def seal_for(public_key: bytes, plaintext: str) -> bytes:
    return SealedBox(PublicKey(public_key)).encrypt(plaintext.encode())


def open_with(token: str, ciphertext: bytes) -> str:
    return SealedBox(_private_key(token)).decrypt(ciphertext).decode()
