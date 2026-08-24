"""Fernet encryption for platform cookie blobs at rest.

Key lives in CONFIG_DIR/secret.key (config volume), created on first use.
Module-level lazy singleton so per-test config reloads resolve correctly.
"""

from cryptography.fernet import Fernet

from app import config as cfg_mod

# Module-level alias so tests can monkeypatch crypto.CONFIG_DIR directly.
CONFIG_DIR = cfg_mod.CONFIG_DIR

_fernet: Fernet | None = None


def get_fernet() -> Fernet:
    global _fernet, CONFIG_DIR
    if _fernet is not None:
        return _fernet
    key_file = CONFIG_DIR / "secret.key"
    if key_file.exists():
        key = key_file.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        try:
            key_file.chmod(0o600)
        except OSError:
            pass  # Windows: best-effort
    _fernet = Fernet(key)
    return _fernet


def encrypt_cookie_text(text: str) -> str:
    return get_fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_cookie_blob(blob: str) -> str:
    return get_fernet().decrypt(blob.encode("ascii")).decode("utf-8")


# Test hook: allow resetting the singleton when config is reloaded
def _reset_for_tests() -> None:
    global _fernet
    _fernet = None


cfg_mod = cfg_mod  # keep reference for test reload patterns
