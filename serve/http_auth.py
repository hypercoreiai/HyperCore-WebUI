#!/usr/bin/env python3
"""serve/http_auth.py - the API-key gate, defined once.

Two HTTP fronts answer the same question, "may this request in?". serve/server.py answers
it for every route a client uses; tools/flood_stub.py answers it for the load test that
runs in CI without a model pack. When the rule is written twice it drifts - one front
accepts "Bearer", the other only "bearer"; one compares in constant time, the other does
not - and then a pass on the stub is no longer evidence about the real server. The stub is
the standing integration target precisely because it needs no GPU and no weights, so the
gate it enforces has to be the same code, not the same idea.

The rule, in full:

  * no key configured -> every request is open. This is a machine-local server first;
    requiring a key by default would break the clients that already talk to it.
  * a key configured  -> "Authorization: Bearer <key>" or "x-api-key: <key>", compared in
    constant time. Anything else is a 401 with one body shape, so a client that probes
    both fronts gets the same error from either.

Health is deliberately not gated: a monitoring probe cannot supply a key, and the health
payload says whether a key is required rather than pretending it is not.
"""
from __future__ import annotations

import hmac
from collections.abc import Mapping

BEARER_PREFIX = "bearer "

# The header names, spelled the way the two families of SDKs send them.
AUTH_HEADER = "Authorization"
KEY_HEADER = "x-api-key"


def _header(headers: Mapping[str, str], name: str) -> str:
    """Read one header, whatever `headers` happens to be.

    http.client.HTTPHeaders is case-insensitive; a plain dict is not. The gate is called
    from a request handler, from the stub, and from tests that hand it a dict, so the
    lookup cannot assume which one it got.
    """
    value = headers.get(name)
    if value is None:
        wanted = name.lower()
        for key, candidate in headers.items():
            if key.lower() == wanted:
                return candidate
    return value or ""


def supplied_key(headers: Mapping[str, str]) -> str:
    """The key a client actually sent, from either accepted header.

    `Authorization` wins when both are present, which is what the SDKs do: an
    `x-api-key` on a request that also carries a bearer token is a mistake, not a
    second credential. Leading/trailing whitespace is stripped, so `Bearer  key` from a
    shell heredoc is the same key as `Bearer key`.
    """
    auth = _header(headers, AUTH_HEADER)
    if auth.lower().startswith(BEARER_PREFIX):
        return auth[len(BEARER_PREFIX):].strip()
    return _header(headers, KEY_HEADER)


def is_open(api_key: str) -> bool:
    """No key configured means no gate, and that is the documented default."""
    return not api_key


def key_matches(headers: Mapping[str, str], api_key: str) -> bool:
    """Whether a request may pass the gate.

    `headers` is anything with a `.get()` - http.client.HTTPHeaders in the real handler,
    a plain dict in a test. The comparison is constant time so the response time does not
    leak how many characters of the key were right.
    """
    if not api_key:
        return True
    return hmac.compare_digest(supplied_key(headers).encode(), api_key.encode())


def auth_error() -> dict[str, dict[str, str]]:
    """The 401 body both fronts return, so a client parses one shape, not two."""
    return {
        "error": {
            "type": "authentication_error",
            "message": "missing or wrong API key",
        }
    }
