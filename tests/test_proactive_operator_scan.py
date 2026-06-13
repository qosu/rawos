"""tests/test_proactive_operator_scan.py — TDD for Milestone 6 (Autonomous Operator Loop).

Wires the proven `operate_on_file` gate (kernel/operator.py) into the proactive
scheduler so the being autonomously detects config-anomalies on its
owner-allowlisted managed_file_targets and proposes/applies reversible fixes.

Mocks ONLY the LLM boundary (rawos.kernel.summarizer._complete) for the fix
generator — everything else (DB, validator subprocess, FileOperator) runs
against real code on real temp-file targets.

CRITICAL invariant under test: the autonomous operator loop runs under the
OWNER's human user.id (resolved via db.get_user_by_email(telegram_owner_email)),
NEVER RAWOS_ENTITY_USER_ID.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time

import pytest

import rawos.db as db
import rawos.scheduler.proactive as proactive
from rawos.config import settings
from rawos.kernel.track_record import GRADUATION_THRESHOLD
from rawos.models import User

OPERATOR_CLASS = "file_edit"


# ---------------------------------------------------------------------------
# _generate_config_fix — mocks ONLY summarizer._complete
# ---------------------------------------------------------------------------

class TestGenerateConfigFix:
    async def test_returns_cleaned_bytes_from_llm(self, monkeypatch):
        async def fake_complete(system_prompt, user_text):
            return "```\nfixed content\n```"

        monkeypatch.setattr(proactive.summarizer, "_complete", fake_complete)

        result = await proactive._generate_config_fix(
            "/etc/x.conf", b"broken content\n", "error: bad syntax",
        )

        assert result == b"fixed content"

    async def test_returns_none_when_llm_output_empty(self, monkeypatch):
        async def fake_complete(system_prompt, user_text):
            return ""

        monkeypatch.setattr(proactive.summarizer, "_complete", fake_complete)

        result = await proactive._generate_config_fix(
            "/etc/x.conf", b"broken\n", "error",
        )

        assert result is None

    async def test_returns_none_when_identical_to_current(self, monkeypatch):
        async def fake_complete(system_prompt, user_text):
            return "same content\n"

        monkeypatch.setattr(proactive.summarizer, "_complete", fake_complete)

        result = await proactive._generate_config_fix(
            "/etc/x.conf", b"same content\n", "error",
        )

        assert result is None

    async def test_returns_none_on_llm_raise(self, monkeypatch):
        async def fake_complete(system_prompt, user_text):
            raise RuntimeError("llm boom")

        monkeypatch.setattr(proactive.summarizer, "_complete", fake_complete)

        result = await proactive._generate_config_fix(
            "/etc/x.conf", b"broken\n", "error",
        )

        assert result is None


# ---------------------------------------------------------------------------
# _is_operator_cooldown — per-target OPERATOR_SCAN cooldown
# ---------------------------------------------------------------------------

class TestOperatorCooldown:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.owner = db.create_user(User(
            email=f"cooldown-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))

    def test_false_for_target_never_scanned(self):
        assert proactive._is_operator_cooldown("/etc/never-touched.conf") is False

    def test_true_within_window_after_operator_scan_record(self):
        target = "/etc/recently-scanned.conf"
        proactive._log_episodic(
            self.owner.id, "OPERATOR_SCAN", target,
            "fix config drift", "signal", "proposed fix for " + target,
        )

        assert proactive._is_operator_cooldown(target) is True

    def test_unaffected_by_other_trigger_types(self):
        target = "/etc/other-trigger.conf"
        proactive._log_episodic(
            self.owner.id, "SERVER_SCAN", target,
            "unrelated", "signal", "unrelated record",
        )

        assert proactive._is_operator_cooldown(target) is False


# ---------------------------------------------------------------------------
# _run_operator_scan_cycle — identity, gating, outcome routing
# ---------------------------------------------------------------------------

class TestRunOperatorScanCycle:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.owner = db.create_user(User(
            email=f"owner-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))

    def _set_owner(self, monkeypatch):
        monkeypatch.setattr(settings, "telegram_owner_email", self.owner.email)

    def _graduate(self, target_path: str) -> None:
        now = int(time.time())
        for _ in range(GRADUATION_THRESHOLD * 2):
            db.update_operator_track_record(
                self.owner.id, OPERATOR_CLASS, target_path,
                verified=True, now=now,
            )

    async def test_noop_when_owner_email_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "telegram_owner_email", "")

        async def fail_fix(*a, **k):
            raise AssertionError("must not generate a fix with no owner")

        monkeypatch.setattr(proactive, "_generate_config_fix", fail_fix)

        await proactive._run_operator_scan_cycle()

    async def test_noop_when_owner_email_has_no_matching_user(self, monkeypatch):
        monkeypatch.setattr(settings, "telegram_owner_email", "nobody@nowhere.test")

        async def fail_fix(*a, **k):
            raise AssertionError("must not generate a fix with unresolved owner")

        monkeypatch.setattr(proactive, "_generate_config_fix", fail_fix)

        await proactive._run_operator_scan_cycle()

    async def test_noop_when_owner_has_no_managed_targets(self, monkeypatch):
        self._set_owner(monkeypatch)

        await proactive._run_operator_scan_cycle()

        with db._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodic_memory WHERE user_id=?", (self.owner.id,),
            ).fetchall()
        assert rows == []

    async def test_skips_healthy_target_no_llm_call(self, monkeypatch):
        self._set_owner(monkeypatch)
        target = os.path.join(self.tmp, "healthy.conf")
        with open(target, "wb") as f:
            f.write(b"good\n")
        db.add_managed_file_target(self.owner.id, target, "true")

        async def fail_fix(*a, **k):
            raise AssertionError("fix generator must not run for a healthy target")

        monkeypatch.setattr(proactive, "_generate_config_fix", fail_fix)

        await proactive._run_operator_scan_cycle()

        with db._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodic_memory WHERE user_id=?", (self.owner.id,),
            ).fetchall()
        assert rows == []

    async def test_broken_target_invokes_operate_on_file_under_owner_id(self, monkeypatch):
        self._set_owner(monkeypatch)
        target = os.path.join(self.tmp, "broken.conf")
        with open(target, "wb") as f:
            f.write(b"broken\n")
        db.add_managed_file_target(self.owner.id, target, "false")  # always-failing validator

        async def fake_fix(target_path, current_content, validator_error):
            return b"fixed\n"

        monkeypatch.setattr(proactive, "_generate_config_fix", fake_fix)

        captured: dict = {}
        real_operate = proactive.operate_on_file

        def spy_operate(user_id, target_path, new_content, **kw):
            captured["user_id"] = user_id
            captured["target_path"] = target_path
            return real_operate(user_id, target_path, new_content, **kw)

        monkeypatch.setattr(proactive, "operate_on_file", spy_operate)

        await proactive._run_operator_scan_cycle()

        assert captured["user_id"] == self.owner.id
        assert captured["user_id"] != proactive.RAWOS_ENTITY_USER_ID
        assert captured["target_path"] == target

    async def test_proposed_outcome_records_artifact_and_episodic_without_writing(self, monkeypatch):
        self._set_owner(monkeypatch)
        # operator_enabled defaults False -> propose-only regardless of graduation
        target = os.path.join(self.tmp, "broken2.conf")
        with open(target, "wb") as f:
            f.write(b"broken\n")
        db.add_managed_file_target(self.owner.id, target, "false")

        async def fake_fix(target_path, current_content, validator_error):
            return b"fixed\n"

        monkeypatch.setattr(proactive, "_generate_config_fix", fake_fix)

        await proactive._run_operator_scan_cycle()

        with db._conn() as conn:
            episodic = conn.execute(
                "SELECT * FROM episodic_memory WHERE user_id=? AND trigger_type='OPERATOR_SCAN'",
                (self.owner.id,),
            ).fetchall()
            artifacts = conn.execute(
                "SELECT * FROM proactive_artifacts WHERE user_id=?", (self.owner.id,),
            ).fetchall()
        assert len(episodic) == 1
        assert len(artifacts) == 1
        with open(target, "rb") as f:
            assert f.read() == b"broken\n"

    async def test_auto_applied_outcome_writes_file_and_records(self, monkeypatch):
        self._set_owner(monkeypatch)
        monkeypatch.setattr(settings, "operator_enabled", True)

        target = os.path.join(self.tmp, "broken3.conf")
        with open(target, "wb") as f:
            f.write(b"bad\n")
        validator_cmd = f"grep -q good {target}"
        db.add_managed_file_target(self.owner.id, target, validator_cmd)
        self._graduate(target)

        async def fake_fix(target_path, current_content, validator_error):
            return b"good\n"

        monkeypatch.setattr(proactive, "_generate_config_fix", fake_fix)

        await proactive._run_operator_scan_cycle()

        with open(target, "rb") as f:
            assert f.read() == b"good\n"
        with db._conn() as conn:
            episodic = conn.execute(
                "SELECT * FROM episodic_memory WHERE user_id=? AND trigger_type='OPERATOR_SCAN'",
                (self.owner.id,),
            ).fetchall()
        assert len(episodic) == 1

    async def test_refusal_on_one_target_does_not_block_others(self, monkeypatch):
        self._set_owner(monkeypatch)
        monkeypatch.setattr(settings, "operator_enabled", True)

        protected = "/etc/systemd/system/rawos.service"
        db.add_managed_file_target(self.owner.id, protected, "false")
        self._graduate(protected)

        target2 = os.path.join(self.tmp, "ok2.conf")
        with open(target2, "wb") as f:
            f.write(b"bad\n")
        validator_cmd2 = f"grep -q good {target2}"
        db.add_managed_file_target(self.owner.id, target2, validator_cmd2)
        self._graduate(target2)

        async def fake_fix(target_path, current_content, validator_error):
            return b"good\n"

        monkeypatch.setattr(proactive, "_generate_config_fix", fake_fix)

        await proactive._run_operator_scan_cycle()  # must not raise

        with open(target2, "rb") as f:
            assert f.read() == b"good\n"

    async def test_broken_target_in_cooldown_is_skipped(self, monkeypatch):
        self._set_owner(monkeypatch)
        target = os.path.join(self.tmp, "cooldown.conf")
        with open(target, "wb") as f:
            f.write(b"broken\n")
        db.add_managed_file_target(self.owner.id, target, "false")
        proactive._log_episodic(
            self.owner.id, "OPERATOR_SCAN", target,
            "fix config drift", "signal", "already proposed",
        )

        async def fail_fix(*a, **k):
            raise AssertionError("must not generate a fix while target is on cooldown")

        monkeypatch.setattr(proactive, "_generate_config_fix", fail_fix)

        await proactive._run_operator_scan_cycle()


# ---------------------------------------------------------------------------
# rawos_operator_scan_loop — gating
# ---------------------------------------------------------------------------

class TestOperatorScanLoop:
    async def test_loop_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "operator_scan_enabled", False)
        calls = {"n": 0}

        async def fake_cycle():
            calls["n"] += 1

        monkeypatch.setattr(proactive, "_run_operator_scan_cycle", fake_cycle)

        await proactive.rawos_operator_scan_loop()

        assert calls["n"] == 0
