"""Set a new password for the local studio (prints it once)."""

import hashlib
import json
import os
import secrets
from pathlib import Path


path = Path(__file__).resolve().parent / "local_data" / "access.json"
path.parent.mkdir(parents=True, exist_ok=True)
password = secrets.token_urlsafe(18)
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 250_000)
path.write_text(json.dumps({"salt": salt.hex(), "hash": digest.hex()}), encoding="utf-8")
os.chmod(path, 0o600)
print("Логин: studio")
print("Пароль:", password)
