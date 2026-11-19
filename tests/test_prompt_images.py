"""Issue #24：prompt_blocks() 历史图片占位与当前轮附件边界测试。"""

import base64
import json
import unittest

from tests import astrbot_stub  # noqa: F401
from agy_provider.errors import AgyError
from agy_provider.messages import digest, prompt_blocks


def _img(tag: str) -> dict[str, str]:
    payload = base64.b64encode((f"fake-png-{tag}-" * 8).encode("ascii")).decode("ascii")
    return {"type": "image", "mimeType": "image/png", "data": payload}


def _parse_transcript(text: str) -> dict:
    start_marker = "<astrbot_history>\n"
    end_marker = "\n</astrbot_history>"
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    return json.loads(text[start:end])


class PromptImagesTests(unittest.TestCase):
    def test_1_historical_image_not_reattached_on_plain_text_turn(self):
        img1 = _img("one")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "看看这张图"}, img1]},
            {"role": "assistant", "content": "已看到图1"},
            {"role": "user", "content": "纯文本追问"},
        ]

        blocks, omitted = prompt_blocks(messages, 160000)

        self.assertEqual(omitted, 0)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertFalse(any(b.get("type") == "image" for b in blocks))
        self.assertNotIn("Conversation attachment", blocks[0]["text"])

        transcript = _parse_transcript(blocks[0]["text"])
        self.assertEqual(transcript["omitted_messages"], 0)
        hist_content = transcript["messages"][0]["content"]
        self.assertEqual(
            hist_content[1],
            {"image_sha256": digest(img1), "status": "historical"},
        )
        self.assertNotIn("attachment", hist_content[1])
        self.assertEqual(
            transcript["messages"][-1],
            {"role": "user", "content": "纯文本追问"},
        )

    def test_2_multiple_historical_turns_with_multiple_images_plain_text_turn(self):
        img1 = _img("hist-1")
        img2 = _img("hist-2")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "图一"}, img1]},
            {"role": "assistant", "content": "收到图一"},
            {"role": "user", "content": [{"type": "text", "text": "图二"}, img2]},
            {"role": "assistant", "content": "收到图二"},
            {"role": "user", "content": "现在只问纯文本问题"},
        ]

        blocks, omitted = prompt_blocks(messages, 160000)

        self.assertEqual(omitted, 0)
        self.assertEqual([b["type"] for b in blocks], ["text"])
        self.assertNotIn("Conversation attachment", blocks[0]["text"])

        transcript = _parse_transcript(blocks[0]["text"])
        self.assertEqual(
            transcript["messages"][0]["content"][1],
            {"image_sha256": digest(img1), "status": "historical"},
        )
        self.assertEqual(
            transcript["messages"][2]["content"][1],
            {"image_sha256": digest(img2), "status": "historical"},
        )
        self.assertNotIn("attachment", transcript["messages"][0]["content"][1])
        self.assertNotIn("attachment", transcript["messages"][2]["content"][1])

    def test_3_current_multi_images_with_historical_image_only_attaches_current(self):
        img_old = _img("old")
        img_a = _img("cur-a")
        img_b = _img("cur-b")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "旧图"}, img_old]},
            {"role": "assistant", "content": "旧图回答"},
            {"role": "user", "content": [{"type": "text", "text": "对比新图"}, img_a, img_b]},
        ]

        blocks, omitted = prompt_blocks(messages, 160000)

        self.assertEqual(omitted, 0)
        self.assertEqual(
            [b["type"] for b in blocks],
            ["text", "text", "image", "text", "image"],
        )
        self.assertEqual(blocks[1], {"type": "text", "text": "Conversation attachment 1:"})
        self.assertEqual(blocks[2], img_a)
        self.assertEqual(blocks[3], {"type": "text", "text": "Conversation attachment 2:"})
        self.assertEqual(blocks[4], img_b)
        self.assertNotIn(img_old, blocks[1:])

        transcript = _parse_transcript(blocks[0]["text"])
        self.assertEqual(
            transcript["messages"][0]["content"][1],
            {"image_sha256": digest(img_old), "status": "historical"},
        )
        self.assertEqual(
            transcript["messages"][2]["content"][1:],
            [
                {"image_sha256": digest(img_a), "attachment": 1},
                {"image_sha256": digest(img_b), "attachment": 2},
            ],
        )

    def test_4_same_image_resent_in_current_turn_is_not_deduped_by_history(self):
        img_same = _img("same-across-turns")
        img_same_reupload = dict(img_same)
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "第一次发"}, img_same]},
            {"role": "assistant", "content": "第一次回复"},
            {"role": "user", "content": [{"type": "text", "text": "再看一次"}, img_same_reupload]},
        ]

        blocks, omitted = prompt_blocks(messages, 160000)

        self.assertEqual(omitted, 0)
        self.assertEqual([b["type"] for b in blocks], ["text", "text", "image"])
        self.assertEqual(blocks[1], {"type": "text", "text": "Conversation attachment 1:"})
        self.assertEqual(blocks[2], img_same_reupload)

        transcript = _parse_transcript(blocks[0]["text"])
        sha = digest(img_same)
        self.assertEqual(
            transcript["messages"][0]["content"][1],
            {"image_sha256": sha, "status": "historical"},
        )
        self.assertEqual(
            transcript["messages"][2]["content"][1],
            {"image_sha256": sha, "attachment": 1},
        )

    def test_5_duplicate_images_within_current_turn_deduped_independently(self):
        img_old = _img("hist-before-dup")
        img_a = _img("dup-a")
        img_a_copy = dict(img_a)
        img_b = _img("unique-b")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "旧图"}, img_old]},
            {"role": "assistant", "content": "收到"},
            {"role": "user", "content": [img_a, img_a_copy, img_b]},
        ]

        blocks, omitted = prompt_blocks(messages, 160000)

        self.assertEqual(omitted, 0)
        self.assertEqual(
            [b["type"] for b in blocks],
            ["text", "text", "image", "text", "image"],
        )
        self.assertEqual(blocks[1], {"type": "text", "text": "Conversation attachment 1:"})
        self.assertEqual(blocks[2], img_a)
        self.assertEqual(blocks[3], {"type": "text", "text": "Conversation attachment 2:"})
        self.assertEqual(blocks[4], img_b)

        transcript = _parse_transcript(blocks[0]["text"])
        self.assertEqual(
            transcript["messages"][0]["content"][1],
            {"image_sha256": digest(img_old), "status": "historical"},
        )
        self.assertEqual(
            transcript["messages"][2]["content"],
            [
                {"image_sha256": digest(img_a), "attachment": 1},
                {"image_sha256": digest(img_a), "attachment": 1},
                {"image_sha256": digest(img_b), "attachment": 2},
            ],
        )

    def test_6_budget_trimming_preserves_current_image_index_alignment(self):
        img_old1 = _img("trim-old-1")
        img_old2 = _img("trim-old-2")
        img_cur = _img("trim-current")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "第一轮长文 " * 30}, img_old1]},
            {"role": "assistant", "content": "第一轮答复 " * 30},
            {"role": "user", "content": [{"type": "text", "text": "第二轮长文 " * 30}, img_old2]},
            {"role": "assistant", "content": "第二轮答复 " * 30},
            {"role": "user", "content": [{"type": "text", "text": "当前轮问题"}, img_cur]},
        ]
        last_user_idx = len(messages) - 1

        # Compute exact window byte sizes for partial retention (cut=2) and current-only (cut=4).
        # Use a large budget to inspect full serialized message lengths, then choose exact bounds.
        full_blocks, _ = prompt_blocks(messages, 160000)
        self.assertEqual([b["type"] for b in full_blocks], ["text", "text", "image"])

        # Binary-search verification: budget=1600 keeps turn 2 (img_old2) + turn 4 (img_cur),
        # while omitting turn 0..1 (img_old1).
        partial_budget = 1600
        blocks_partial, omitted_partial = prompt_blocks(messages, partial_budget)
        self.assertGreater(omitted_partial, 0)
        self.assertLess(omitted_partial, last_user_idx)
        self.assertEqual([b["type"] for b in blocks_partial], ["text", "text", "image"])
        self.assertEqual(blocks_partial[1], {"type": "text", "text": "Conversation attachment 1:"})
        self.assertEqual(blocks_partial[2], img_cur)

        partial_transcript = _parse_transcript(blocks_partial[0]["text"])
        self.assertEqual(partial_transcript["omitted_messages"], omitted_partial)
        self.assertEqual(
            partial_transcript["messages"][0]["content"][1],
            {"image_sha256": digest(img_old2), "status": "historical"},
        )
        self.assertEqual(
            partial_transcript["messages"][-1]["content"][1],
            {"image_sha256": digest(img_cur), "attachment": 1},
        )

        # Tighter budget keeps only the current user turn (omitted == last_user_idx).
        tight_budget = 450
        blocks_tight, omitted_tight = prompt_blocks(messages, tight_budget)
        self.assertEqual(omitted_tight, last_user_idx)
        self.assertEqual([b["type"] for b in blocks_tight], ["text", "text", "image"])
        self.assertEqual(blocks_tight[1], {"type": "text", "text": "Conversation attachment 1:"})
        self.assertEqual(blocks_tight[2], img_cur)

        tight_transcript = _parse_transcript(blocks_tight[0]["text"])
        self.assertEqual(len(tight_transcript["messages"]), 1)
        self.assertEqual(
            tight_transcript["messages"][0]["content"][1],
            {"image_sha256": digest(img_cur), "attachment": 1},
        )

        # Budget smaller than even the current turn raises INPUT_LIMIT.
        with self.assertRaises(AgyError) as caught:
            prompt_blocks(messages, 100)
        self.assertEqual(caught.exception.code, "INPUT_LIMIT")

    def test_7_tool_exchange_history_and_tool_continuation_after_image_turn(self):
        img_old = _img("tool-hist-img")
        call_1 = {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"q":"x"}'},
        }

        # 7a. Closed tool exchange + historical image + current plain text turn.
        hist_messages = [
            {"role": "user", "content": [{"type": "text", "text": "看图查资料"}, img_old]},
            {"role": "assistant", "content": "", "tool_calls": [call_1]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
            {"role": "assistant", "content": "查到了"},
            {"role": "user", "content": "总结一下"},
        ]
        blocks_a, omitted_a = prompt_blocks(hist_messages, 160000)
        self.assertEqual(omitted_a, 0)
        self.assertEqual([b["type"] for b in blocks_a], ["text"])
        transcript_a = _parse_transcript(blocks_a[0]["text"])
        self.assertEqual(
            transcript_a["messages"][0]["content"][1],
            {"image_sha256": digest(img_old), "status": "historical"},
        )
        self.assertEqual(transcript_a["messages"][1]["tool_calls"], [call_1])
        self.assertEqual(transcript_a["messages"][2]["role"], "tool")

        # 7b. Latest user turn carries img_cur followed by assistant(tool_calls) + tool(result).
        img_cur = _img("tool-cur-img")
        call_2 = {
            "id": "call-2",
            "type": "function",
            "function": {"name": "ocr", "arguments": '{"mode":"fast"}'},
        }
        cont_messages = [
            {"role": "user", "content": [{"type": "text", "text": "更早的图"}, img_old]},
            {"role": "assistant", "content": "已看更早的图"},
            {"role": "user", "content": [{"type": "text", "text": "识别当前图"}, img_cur]},
            {"role": "assistant", "content": "", "tool_calls": [call_2]},
            {"role": "tool", "tool_call_id": "call-2", "content": "OCR结果"},
        ]
        blocks_b, omitted_b = prompt_blocks(cont_messages, 160000)
        self.assertEqual(omitted_b, 0)
        self.assertEqual([b["type"] for b in blocks_b], ["text", "text", "image"])
        self.assertEqual(blocks_b[1], {"type": "text", "text": "Conversation attachment 1:"})
        self.assertEqual(blocks_b[2], img_cur)

        transcript_b = _parse_transcript(blocks_b[0]["text"])
        self.assertEqual(
            transcript_b["messages"][0]["content"][1],
            {"image_sha256": digest(img_old), "status": "historical"},
        )
        self.assertEqual(
            transcript_b["messages"][2]["content"][1],
            {"image_sha256": digest(img_cur), "attachment": 1},
        )

    def test_8_single_message_shortcut_and_no_user_defensive_boundary(self):
        img1 = _img("single-turn")
        single = [{"role": "user", "content": [{"type": "text", "text": "看图"}, img1]}]
        blocks_single, omitted_single = prompt_blocks(single, 160000)
        self.assertEqual(omitted_single, 0)
        self.assertEqual(blocks_single, single[0]["content"])

        no_user_messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "非user带图"}, img1]},
            {"role": "assistant", "content": "补充文本"},
        ]
        blocks_no_user, omitted_no_user = prompt_blocks(no_user_messages, 160000)
        self.assertEqual(omitted_no_user, 0)
        self.assertEqual([b["type"] for b in blocks_no_user], ["text"])
        transcript = _parse_transcript(blocks_no_user[0]["text"])
        self.assertEqual(
            transcript["messages"][0]["content"][1],
            {"image_sha256": digest(img1), "status": "historical"},
        )


if __name__ == "__main__":
    unittest.main()
