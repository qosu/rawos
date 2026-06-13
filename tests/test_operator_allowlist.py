"""tests/test_operator_allowlist.py — TDD for managed_file_targets DB accessors (Step 4).

Tests: add/get/remove roundtrip; upsert updates validator_cmd; user_id scoping;
operator_enabled defaults False in config.
"""
from __future__ import annotations

import hashlib
import os
import tempfile

import rawos.db as db
from rawos.models import User


class TestManagedFileTargetsDB:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"allowlist-{id(self)}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))

    def test_get_returns_none_for_unregistered_target(self):
        result = db.get_managed_file_target(self.user.id, "/etc/caddy/Caddyfile")
        assert result is None

    def test_add_then_get_roundtrip(self):
        db.add_managed_file_target(
            self.user.id, "/etc/caddy/Caddyfile", "caddy validate --config /etc/caddy/Caddyfile"
        )
        row = db.get_managed_file_target(self.user.id, "/etc/caddy/Caddyfile")
        assert row is not None
        assert row["target_path"] == "/etc/caddy/Caddyfile"
        assert row["validator_cmd"] == "caddy validate --config /etc/caddy/Caddyfile"

    def test_add_upserts_validator_cmd(self):
        db.add_managed_file_target(self.user.id, "/etc/target.conf", "validate-v1")
        db.add_managed_file_target(self.user.id, "/etc/target.conf", "validate-v2")
        row = db.get_managed_file_target(self.user.id, "/etc/target.conf")
        assert row["validator_cmd"] == "validate-v2"

    def test_remove_makes_target_absent(self):
        db.add_managed_file_target(self.user.id, "/etc/target.conf", "true")
        db.remove_managed_file_target(self.user.id, "/etc/target.conf")
        assert db.get_managed_file_target(self.user.id, "/etc/target.conf") is None

    def test_remove_noop_for_unregistered_target(self):
        db.remove_managed_file_target(self.user.id, "/etc/never-added.conf")

    def test_targets_scoped_to_user_id(self):
        other_user = db.create_user(User(
            email=f"other-{id(self)}@test.com",
            password_hash=hashlib.sha256(b"pass2").hexdigest(),
        ))
        db.add_managed_file_target(self.user.id, "/etc/target.conf", "true")
        assert db.get_managed_file_target(other_user.id, "/etc/target.conf") is None


def test_operator_enabled_defaults_false():
    from rawos.config import settings
    assert settings.operator_enabled is False
