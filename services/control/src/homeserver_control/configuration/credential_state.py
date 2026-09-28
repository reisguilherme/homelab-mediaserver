"""Private persistent evidence for native APIs that mask credential read-back."""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path

from .native_config import NativeConfigurationError, environment


class CredentialState:
    def __init__(self, settings):
        root = (
            environment(settings).get("HOMESERVER_APPDATA_ROOT") if settings is not None else None
        )
        self.path = Path(root) / "native-config-fingerprints.json" if root else None
        self.data = None
        if self.path and self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
                raise NativeConfigurationError(
                    "Private native credential evidence permissions invalid"
                )
            try:
                self.data = json.loads(self.path.read_text())
                if not isinstance(self.data["credentials"], dict) or len(self.data["key"]) != 64:
                    raise ValueError
                bytes.fromhex(self.data["key"])
            except (ValueError, KeyError, TypeError):
                raise NativeConfigurationError(
                    "Private native credential evidence invalid"
                ) from None

    def digest(self, value):
        return hmac.new(
            bytes.fromhex(self.data["key"]),
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()

    def matches(self, identity, value):
        saved = self.data["credentials"].get(identity) if self.data else None
        return isinstance(saved, str) and hmac.compare_digest(saved, self.digest(value))

    def record(self, values):
        if not self.path:
            raise NativeConfigurationError("Persistent appdata required for masked credentials")
        if self.data is None:
            self.data = {"key": secrets.token_hex(32), "credentials": {}}
        self.data["credentials"].update({key: self.digest(value) for key, value in values.items()})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".native-credentials-", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self.data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
