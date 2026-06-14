"""TDD — frontdoor auto-refresh: _decode_jwt_exp + _refresh_if_expired.

RED phase: all tests fail until production code exists.
"""
from __future__ import annotations

import base64
import json
import time
import unittest.mock as mock
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers

def _make_jwt(exp: float) -> str:
    """Create a minimal well-formed JWT with a known exp claim."""
    header  = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "test-user", "exp": int(exp)}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


# ---------------------------------------------------------------------------
# _decode_jwt_exp

class TestDecodeJwtExp:
    def _fn(self, token: str):
        from rawos.cli.main import _decode_jwt_exp
        return _decode_jwt_exp(token)

    def test_returns_exp_from_valid_jwt(self):
        future = time.time() + 3600
        token = _make_jwt(future)
        exp = self._fn(token)
        assert exp is not None
        assert abs(exp - int(future)) < 1

    def test_returns_none_for_garbage_string(self):
        assert self._fn("not.a.jwt") is None

    def test_returns_none_for_empty_string(self):
        assert self._fn("") is None

    def test_returns_none_for_malformed_payload(self):
        assert self._fn("header.!!!.sig") is None

    def test_returns_none_when_exp_missing_from_payload(self):
        header  = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"sub":"x"}').rstrip(b"=").decode()
        token = f"{header}.{payload}.sig"
        assert self._fn(token) is None


# ---------------------------------------------------------------------------
# _refresh_if_expired

class TestRefreshIfExpired:
    def _fn(self, creds: dict, *, post_return=None, save_creds=None):
        """Call _refresh_if_expired with mocked httpx.post and _save_creds."""
        from rawos.cli.main import _refresh_if_expired

        post_mock = MagicMock(return_value=post_return or MagicMock(status_code=400))
        save_mock = save_creds or MagicMock()

        with patch("rawos.cli.main.httpx.post", post_mock), \
             patch("rawos.cli.main._save_creds", save_mock):
            result = _refresh_if_expired(creds)
        return result, post_mock, save_mock

    def _ok_response(self) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
        }
        return resp

    # --- no-op cases ---

    def test_noop_when_no_refresh_token(self):
        creds = {"access_token": "some-token"}
        result, post_mock, _ = self._fn(creds)
        assert result == creds
        post_mock.assert_not_called()

    def test_noop_when_token_valid_and_not_near_expiry(self):
        future = time.time() + 7200   # 2 hours ahead
        creds = {
            "access_token": _make_jwt(future),
            "refresh_token": "refresh-tok",
        }
        result, post_mock, _ = self._fn(creds)
        assert result == creds
        post_mock.assert_not_called()

    # --- refresh cases ---

    def test_refreshes_when_access_token_expired(self):
        past = time.time() - 60
        creds = {
            "email": "u@test.com",
            "access_token": _make_jwt(past),
            "refresh_token": "refresh-tok",
        }
        result, post_mock, save_mock = self._fn(creds, post_return=self._ok_response())
        post_mock.assert_called_once()
        assert result["access_token"] == "new-access-token"
        assert result["refresh_token"] == "new-refresh-token"
        save_mock.assert_called_once()

    def test_refreshes_when_token_expires_within_5_minutes(self):
        near = time.time() + 240   # 4 minutes — within 5-min window
        creds = {
            "email": "u@test.com",
            "access_token": _make_jwt(near),
            "refresh_token": "refresh-tok",
        }
        result, post_mock, _ = self._fn(creds, post_return=self._ok_response())
        post_mock.assert_called_once()
        assert result["access_token"] == "new-access-token"

    def test_refreshes_when_access_token_missing(self):
        creds = {
            "email": "u@test.com",
            "refresh_token": "refresh-tok",
        }
        result, post_mock, _ = self._fn(creds, post_return=self._ok_response())
        post_mock.assert_called_once()
        assert result["access_token"] == "new-access-token"

    def test_preserves_email_in_new_creds(self):
        creds = {
            "email": "owner@rawos.io",
            "access_token": _make_jwt(time.time() - 60),
            "refresh_token": "refresh-tok",
        }
        result, _, _ = self._fn(creds, post_return=self._ok_response())
        assert result["email"] == "owner@rawos.io"

    # --- fail-open cases ---

    def test_fails_open_when_api_returns_non_200(self):
        past = time.time() - 60
        creds = {
            "access_token": _make_jwt(past),
            "refresh_token": "refresh-tok",
        }
        bad_resp = MagicMock(status_code=401)
        result, _, save_mock = self._fn(creds, post_return=bad_resp)
        assert result == creds          # original returned unchanged
        save_mock.assert_not_called()

    def test_fails_open_when_httpx_raises(self):
        past = time.time() - 60
        creds = {
            "access_token": _make_jwt(past),
            "refresh_token": "refresh-tok",
        }
        from rawos.cli.main import _refresh_if_expired
        with patch("rawos.cli.main.httpx.post", side_effect=Exception("network down")), \
             patch("rawos.cli.main._save_creds") as save_mock:
            result = _refresh_if_expired(creds)
        assert result == creds
        save_mock.assert_not_called()

    def test_posts_refresh_token_to_auth_endpoint(self):
        past = time.time() - 60
        creds = {
            "access_token": _make_jwt(past),
            "refresh_token": "my-refresh-tok",
        }
        result, post_mock, _ = self._fn(creds, post_return=self._ok_response())
        call_kwargs = post_mock.call_args
        assert "auth/refresh" in call_kwargs[0][0]
        assert call_kwargs[1]["json"]["refresh_token"] == "my-refresh-tok"
