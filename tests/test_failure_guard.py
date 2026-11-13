import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.failure_guard import FailureGuard
from agy_provider.messages import digest, normalize
from agy_provider.process import LaunchInfo, launch_args as real_launch_args
from agy_provider.sessions import Runtime

FIXTURE = Path(__file__).with_name("fake_cli.py")


class FailureGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-guard-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log_path = self.root / "cli.jsonl"
        self.scenario_file = self.root / "scenario.txt"

    def _read_prompts(self):
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line)["event"] == "prompt"
        ]

    async def test_empty_first_executes_once_duplicate_zero_and_recovers_after_ttl(self):
        now = [100.0]
        self.scenario_file.write_text("empty", encoding="utf-8")

        settings = Settings(auth_bridge=False, empty_dedupe_ttl=30)
        runtime = Runtime(settings, self.root / "rt-empty", clock=lambda: now[0])
        self.addAsyncCleanup(runtime.close)

        def fake_launch(info, home, use_default_agent=False):
            args = real_launch_args(info, home, use_default_agent)
            return [sys.executable, str(FIXTURE), "empty", *args[1:]]

        req = normalize([], "你好", "sys", [], settings.history_bytes)
        fp = digest(["p1", "sess-1", req.hashes, req.system_hash, "gemini-test", "low"])

        with (
            patch.dict(
                os.environ,
                {
                    "AGY_TEST_LOG": str(self.log_path),
                    "AGY_SCENARIO_FILE": str(self.scenario_file),
                },
            ),
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
        ):
            # 1) First call actually invokes fake_cli once and raises EMPTY
            with self.assertRaises(AgyError) as c1:
                async for _ in runtime.stream(
                    req,
                    "sess-1",
                    "gemini-test",
                    "low",
                    provider_id="p1",
                    request_fingerprint=fp,
                ):
                    pass
            self.assertEqual(c1.exception.code, "EMPTY")
            self.assertEqual(len(self._read_prompts()), 1)
            origin_turn_id = runtime.telemetry.history[-1]["turn_id"]

            # 2) Duplicate within 30s does NOT hit fake_cli (0 new prompts!)
            now[0] += 10.0
            with self.assertRaises(AgyError) as c2:
                async for _ in runtime.stream(
                    req,
                    "sess-1",
                    "gemini-test",
                    "low",
                    provider_id="p1",
                    request_fingerprint=fp,
                ):
                    pass
            self.assertEqual(c2.exception.code, "EMPTY")
            self.assertEqual(c2.exception.origin_turn_id, origin_turn_id)
            self.assertTrue(c2.exception.no_retry)
            self.assertEqual(len(self._read_prompts()), 1)
            short_record = runtime.telemetry.history[-1]
            self.assertEqual(short_record["status"], "short_circuited")
            self.assertEqual(short_record["usage_status"], "not_sent")
            self.assertEqual(short_record["origin_turn_id"], origin_turn_id)

            # 3) After TTL expires and upstream returns answer -> recovers and executes CLI
            now[0] += 25.0  # total +35s > 30s
            self.scenario_file.write_text("usage", encoding="utf-8")
            chunks = []
            async for ev in runtime.stream(
                req,
                "sess-1",
                "gemini-test",
                "low",
                provider_id="p1",
                request_fingerprint=fp,
            ):
                if ev.kind == "text":
                    chunks.append(ev.text)
            self.assertEqual("".join(chunks), "fake answer")
            self.assertEqual(len(self._read_prompts()), 2)

    async def test_quota_cooldown_model_isolation_single_probe_and_recovery(self):
        now = [500.0]
        guard = FailureGuard(clock=lambda: now[0])

        # Record QUOTA for (p1, model-a) with retry_after=900 (clamped to 300s)
        guard.record_failure(
            ("p1", "model-a"),
            "fp1",
            "QUOTA",
            origin_turn_id="turn-orig",
            retry_after=900,
            quota_ttl=60,
        )
        # Blocked immediately within cooldown
        r_blocked = await guard.check(("p1", "model-a"), "fp2", quota_ttl=60)
        self.assertFalse(r_blocked.allowed)
        self.assertEqual(r_blocked.code, "QUOTA")
        self.assertEqual(r_blocked.origin_turn_id, "turn-orig")
        self.assertEqual(r_blocked.retry_after, 300)

        # Different model on same provider is NOT affected
        r_other_model = await guard.check(("p1", "model-b"), "fp2", quota_ttl=60)
        self.assertTrue(r_other_model.allowed)

        # Advance past 300s -> half-open allows ONLY 1 concurrent probe
        now[0] += 300.0
        probe1 = await guard.check(("p1", "model-a"), "fp1", quota_ttl=60)
        probe2 = await guard.check(("p1", "model-a"), "fp3", quota_ttl=60)
        self.assertTrue(probe1.allowed)
        self.assertEqual(probe1.half_open_scope, ("p1", "model-a"))
        self.assertFalse(probe2.allowed)
        self.assertEqual(probe2.code, "QUOTA")

        # If probe succeeds -> cooldown cleared completely
        guard.record_success(("p1", "model-a"))
        after_ok = await guard.check(("p1", "model-a"), "fp4", quota_ttl=60)
        self.assertTrue(after_ok.allowed)

        # If half-open probe fails with QUOTA -> renews cooldown
        guard.record_failure(
            ("p1", "model-a"), "fp1", "QUOTA", origin_turn_id="t1", quota_ttl=60
        )
        now[0] += 60.0
        p_fail = await guard.check(("p1", "model-a"), "fp1", quota_ttl=60)
        self.assertTrue(p_fail.allowed)
        guard.record_failure(
            ("p1", "model-a"), "fp1", "QUOTA", origin_turn_id="t2", quota_ttl=60
        )
        reblocked = await guard.check(("p1", "model-a"), "fp5", quota_ttl=60)
        self.assertFalse(reblocked.allowed)
        self.assertEqual(reblocked.origin_turn_id, "t2")

    async def test_tool_continuation_anonymous_isolation_bounded_and_no_success_cache(self):
        now = [1.0]
        guard = FailureGuard(clock=lambda: now[0], max_entries=1024)

        # Anonymous caller lacking session_key & logical_request_id does NOT cross-dedupe EMPTY
        guard.record_failure(
            ("p1", "m1"),
            "same-fp",
            "EMPTY",
            origin_turn_id="anon-1",
            session_key=None,
            logical_request_id=None,
            empty_ttl=30,
        )
        c_anon = await guard.check(
            ("p1", "m1"), "same-fp", None, session_key=None, empty_ttl=30
        )
        self.assertTrue(c_anon.allowed)

        # Tool continuation bypasses guard
        guard.record_failure(
            ("p1", "m1"),
            "fp",
            "EMPTY",
            origin_turn_id="t1",
            session_key="s1",
            empty_ttl=30,
        )
        cont = await guard.check(
            ("p1", "m1"), "fp", None, session_key="s1", empty_ttl=30, continuation=True
        )
        self.assertTrue(cont.allowed)

        # Tool continuation failure does NOT poison non-continuation identical user prompts
        guard.record_failure(
            ("p1", "m1"),
            "fp-cont",
            "EMPTY",
            origin_turn_id="t-cont",
            session_key="s2",
            empty_ttl=30,
            continuation=True,
        )
        non_cont = await guard.check(
            ("p1", "m1"), "fp-cont", None, session_key="s2", empty_ttl=30
        )
        self.assertTrue(non_cont.allowed)

        # Map stays bounded to 1024 entries
        for i in range(1200):
            guard.record_failure(
                ("p1", "m1"),
                f"fp-{i}",
                "EMPTY",
                origin_turn_id=f"t-{i}",
                session_key=f"s-{i}",
                empty_ttl=30,
            )
            guard.record_failure(
                ("p1", f"m-{i}"),
                None,
                "QUOTA",
                origin_turn_id=f"q-{i}",
                quota_ttl=60,
            )
        self.assertLessEqual(len(guard._empty), 1024)
        self.assertLessEqual(len(guard._quota), 1024)

    async def test_success_answers_never_cached_with_same_prompt(self):
        self.scenario_file.write_text("echo", encoding="utf-8")
        settings = Settings(auth_bridge=False)
        runtime = Runtime(settings, self.root / "rt-nosucc")
        self.addAsyncCleanup(runtime.close)

        def fake_launch(info, home, use_default_agent=False):
            args = real_launch_args(info, home, use_default_agent)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        req = normalize([], "重复问候", "sys", [], settings.history_bytes)
        with (
            patch.dict(
                os.environ,
                {
                    "AGY_TEST_LOG": str(self.log_path),
                    "AGY_SCENARIO_FILE": str(self.scenario_file),
                },
            ),
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
        ):
            out1 = "".join(
                [
                    e.text
                    async for e in runtime.stream(
                        req, "s1", "gemini-test", "low", provider_id="p1"
                    )
                    if e.kind == "text"
                ]
            )
            # Clear session so second turn also creates fresh turn cleanly and executes CLI again
            out2 = "".join(
                [
                    e.text
                    async for e in runtime.stream(
                        req, "s2", "gemini-test", "low", provider_id="p1"
                    )
                    if e.kind == "text"
                ]
            )
        self.assertEqual(out1, "reply:1")
        self.assertEqual(out2, "reply:1")
        self.assertEqual(len(self._read_prompts()), 2)


if __name__ == "__main__":
    unittest.main()
