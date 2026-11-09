"""HMAC-SHA256 check for X-Ramp-Signature. Missing secret never counts as valid."""

import hashlib
import hmac


def signature_matches(body: bytes, header: str, secret: str) -> bool:
    if not secret or not header:
        return False
    supplied = header.removeprefix("sha256=")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, supplied)
