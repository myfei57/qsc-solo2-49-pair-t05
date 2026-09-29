"""Signed compact tokens carried by a paperless permit ticket.

A ticket handed to the operator is two URL-safe base64 parts joined by ``.``:
the canonical JSON payload and its HMAC-SHA256 tag.  The signature proves the
ticket came from an office holding the secret and makes a copied or edited
ticket detectable at the gate; the server-side register is still the source of
truth for the ticket's current status (used, revoked, superseded).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .errors import PermitError, PermitErrorCode

#: Marked on every payload so a future token format is distinguishable.
TOKEN_VERSION = 1


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    try:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as error:
        raise PermitError(PermitErrorCode.BAD_TICKET, "ticket is not base64") from error


def encode_payload(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _b64encode(body.encode("utf-8"))


def issue_token(secret: bytes, payload: Mapping[str, Any]) -> str:
    body = encode_payload(payload)
    tag = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(tag)}"


def _decode_payload(body: str) -> dict[str, Any]:
    raw = _b64decode(body)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PermitError(PermitErrorCode.BAD_TICKET, "ticket payload is not JSON") from error
    if not isinstance(payload, dict):
        raise PermitError(PermitErrorCode.BAD_TICKET, "ticket payload is not an object")
    return payload


def verify_token(secret: bytes, token: str) -> dict[str, Any]:
    """Return the payload of a well-formed, correctly signed ticket."""

    if not isinstance(token, str) or token.count(".") != 1 or not token.strip():
        raise PermitError(PermitErrorCode.BAD_TICKET, "malformed ticket")
    body, tag = token.split(".", 1)
    if not body or not tag:
        raise PermitError(PermitErrorCode.BAD_TICKET, "malformed ticket")
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    provided = _b64decode(tag)
    if not hmac.compare_digest(expected, provided):
        raise PermitError(PermitErrorCode.BAD_SIGNATURE, "ticket signature does not match")
    payload = _decode_payload(body)
    if payload.get("v") != TOKEN_VERSION:
        raise PermitError(PermitErrorCode.BAD_TICKET, "unsupported ticket version")
    return payload


def stamp(value: datetime) -> str:
    return value.isoformat()
