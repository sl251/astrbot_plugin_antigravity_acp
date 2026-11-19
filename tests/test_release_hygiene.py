"""版本一致性、分发归档配置、敏感扫描增强与历史二分裁剪等价性测试。"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import astrbot_stub  # noqa: F401
from agy_provider._version import PLUGIN_VERSION
from agy_provider.messages import digest, prompt_blocks, replay
from tools import check_sensitive

REPO_ROOT = Path(__file__).resolve().parents[1]


def _linear_replay_reference(messages: list[dict], budget: int) -> tuple[str, int]:
    """原始逐条推进的历史重放参考实现，用于验证二分查找字节级等价性。"""
    latest = messages[-1]["content"]
    if len(messages) == 1:
        return latest, 0

    def encode(history, omitted):
        body = json.dumps(
            {"omitted_messages": omitted, "messages": history}, ensure_ascii=False
        )
        body = body.replace("<", "\\u003c").replace(">", "\\u003e")
        return f"<astrbot_history>\n{body}\n</astrbot_history>\n\n{latest}"

    history = messages[:-1]
    omitted = 0
    while history:
        packed = encode(history, omitted)
        if len(packed.encode()) <= budget:
            return packed, omitted
        history = history[1:]
        omitted += 1
        while history and history[0]["role"] == "assistant":
            history = history[1:]
            omitted += 1
    return encode([], omitted), omitted


class ReleaseHygieneTests(unittest.TestCase):
    def test_single_source_version_matches_metadata_and_runtime_modules(self):
        meta_text = (REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        match = re.search(r"^version:\s*(\S+)\s*$", meta_text, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), PLUGIN_VERSION)

        for rel_path in (
            "main.py",
            "agy_provider/transport.py",
            "agy_provider/mcp_bridge.py",
        ):
            source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertIn("PLUGIN_VERSION", source, f"{rel_path} 应引用 PLUGIN_VERSION")

    def test_gitattributes_excludes_dev_artifacts_and_keeps_runtime_files(self):
        gitattrs = REPO_ROOT / ".gitattributes"
        self.assertTrue(gitattrs.is_file())
        if shutil.which("git") and (REPO_ROOT / ".git").exists():
            ignored = [
                "tests",
                "docs",
                "docs/archive",
                "docs/archive/review-bundle",
                "tools",
                ".github",
                "AGENTS.md",
                "docs/archive/HANDOFF_0.4.0.md",
            ]
            kept = [
                "main.py",
                "metadata.yaml",
                "_conf_schema.json",
                "agy_provider",
                "agy_provider/provider.py",
                "views/setup/app.js",
            ]
            proc = subprocess.run(
                ["git", "check-attr", "export-ignore", *ignored, *kept],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            lines = proc.stdout.strip().splitlines()
            for line in lines[: len(ignored)]:
                self.assertTrue(line.endswith(": set"), line)
            for line in lines[len(ignored) :]:
                self.assertFalse(line.endswith(": set"), line)

    def test_sensitive_scanner_catches_oauth_and_proxy_creds_without_echoing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.txt"
            tok_google = "ya29." + "A1b2C3d4E5f6G7h8I9j0K1l2M3"
            tok_refresh = "1//" + "0gAbCdEfGhIjKlMnOpQrStUvWxYz"
            tok_gho = "gho_" + "9" * 26
            proxy_real = "socks5h://" + "ops_user" + ":" + "RealPass987" + "@10.0.0.2:1080"
            proxy_fake = "http://user:fake-password" + "@127.0.0.1:7890"
            sample.write_text(
                "\n".join([tok_google, tok_refresh, tok_gho, proxy_real, proxy_fake]),
                encoding="utf-8",
            )
            hits = check_sensitive.scan(sample)
            self.assertEqual(len(hits), 4)
            joined = "\n".join(hits)
            self.assertNotIn("RealPass987", joined)
            self.assertNotIn(tok_google, joined)

    def test_binary_search_replay_and_prompt_blocks_match_linear_behavior(self):
        messages = []
        for i in range(12):
            messages.append({"role": "user", "content": f"问题 {i}：" + ("内容" * 20)})
            messages.append({"role": "assistant", "content": f"回答 {i}：" + ("回复" * 25)})
        messages.append({"role": "user", "content": "当前最新提问"})

        for budget in (300, 600, 1200, 2500, 10000):
            with self.subTest(budget=budget):
                self.assertEqual(
                    replay(messages, budget),
                    _linear_replay_reference(messages, budget),
                )

        img_part = {"type": "image", "mimeType": "image/png", "data": "QUJD" * 30}
        mm_messages = [
            {"role": "user", "content": [{"type": "text", "text": "旧图1 " * 20}, img_part]},
            {"role": "assistant", "content": "看到图1 " * 20},
            {"role": "user", "content": [{"type": "text", "text": "旧图2 " * 20}, img_part]},
            {"role": "assistant", "content": "看到图2 " * 20},
            {"role": "user", "content": "最新追问"},
        ]
        blocks, omitted = prompt_blocks(mm_messages, 650)
        self.assertGreater(omitted, 0)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn(f'"omitted_messages": {omitted}', blocks[0]["text"])
        expected_sha = digest(img_part)
        if any(b.get("type") == "image" for b in blocks):
            self.assertIn(expected_sha, blocks[0]["text"])

        with self.subTest(budget=900, scenario="historical_image_retained_as_placeholder"):
            blocks_900, omitted_900 = prompt_blocks(mm_messages, 900)
            self.assertEqual(omitted_900, 2)
            self.assertFalse(any(b.get("type") == "image" for b in blocks_900))
            self.assertIn(expected_sha, blocks_900[0]["text"])
            self.assertIn('"status": "historical"', blocks_900[0]["text"])
            self.assertNotIn("Conversation attachment", blocks_900[0]["text"])

        with self.subTest(budget=1000, scenario="current_turn_image_under_binary_trim"):
            cur_img = {"type": "image", "mimeType": "image/png", "data": "WFla" * 30}
            mm_cur_messages = mm_messages[:-1] + [
                {"role": "user", "content": [{"type": "text", "text": "当前追问"}, cur_img]}
            ]
            cur_blocks, cur_omitted = prompt_blocks(mm_cur_messages, 1000)
            self.assertEqual(cur_omitted, 2)
            self.assertEqual([b.get("type") for b in cur_blocks], ["text", "text", "image"])
            self.assertEqual(cur_blocks[1], {"type": "text", "text": "Conversation attachment 1:"})
            self.assertEqual(cur_blocks[2], cur_img)
            self.assertIn(expected_sha, cur_blocks[0]["text"])
            self.assertIn('"status": "historical"', cur_blocks[0]["text"])
            self.assertIn(digest(cur_img), cur_blocks[0]["text"])
            self.assertIn('"attachment": 1', cur_blocks[0]["text"])


if __name__ == "__main__":
    unittest.main()
