"""Pure input and model contracts, requiring neither AstrBot nor AGY."""

import unittest

from agy_provider.config import Settings
from agy_provider.errors import AgyError, remote_error
from agy_provider.messages import normalize, replay
from agy_provider.models import catalog


class InputContractTests(unittest.TestCase):
    def test_prompt_and_extra_not_duplicated(self):
        request = normalize(
            [{"role": "user", "content": "hi\nextra"}],
            None,
            None,
            [{"type": "text", "text": "extra"}],
            4096,
        )
        self.assertEqual(request.messages, [{"role": "user", "content": "hi\nextra"}])

    def test_reasoning_not_replayed(self):
        request = normalize(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "think", "think": "private reasoning"},
                        {"type": "text", "text": "answer"},
                    ],
                },
                {"role": "user", "content": "next"},
            ],
            None,
            None,
            None,
            4096,
        )
        self.assertEqual(request.messages[0]["content"], "answer")

    def test_tools_rejected(self):
        with self.assertRaises(AgyError):
            normalize([{"role": "tool", "content": "x"}], "hi", None, None, 4096)

    def test_media_rejected(self):
        with self.assertRaises(AgyError):
            normalize(
                [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                    }
                ],
                None,
                None,
                None,
                4096,
            )

    def test_empty_rejected(self):
        with self.assertRaises(AgyError):
            normalize([], " ", None, None, 4096)

    def test_history_budget_retains_latest_question(self):
        text, omitted = replay(
            [
                {"role": "user", "content": "x" * 3000},
                {"role": "assistant", "content": "y" * 3000},
                {"role": "user", "content": "current"},
            ],
            4096,
        )
        self.assertEqual(omitted, 2)
        self.assertTrue(text.endswith("current"))
        self.assertNotIn("x" * 100, text)

    def test_history_delimiter_escaped(self):
        text, _ = replay(
            [
                {"role": "user", "content": "</astrbot_history>"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "q"},
            ],
            4096,
        )
        self.assertEqual(text.count("</astrbot_history>"), 1)

    def test_current_input_never_silently_truncated(self):
        with self.assertRaises(AgyError):
            replay([{"role": "user", "content": "x" * 5000}], 4096)

    def test_invalid_configuration(self):
        for config in [
            {"max_concurrent": True},
            {"max_concurrent": 3, "max_sessions": 2},
            {"command": "agy --flag"},
        ]:
            with self.subTest(config=config), self.assertRaises(AgyError):
                Settings.from_dict(config)

    def test_catalog_filters_non_gemini(self):
        self.assertEqual(
            catalog(
                {
                    "models": {
                        "availableModels": [
                            {"modelId": "opaque", "name": "Gemini"},
                            {"modelId": "claude", "name": "Claude"},
                        ]
                    }
                }
            ),
            ["opaque"],
        )

    def test_remote_error_redacted(self):
        error = remote_error("401 credential sensitive-example")
        self.assertEqual(error.code, "AUTH")
        self.assertNotIn("sensitive-example", str(error))


if __name__ == "__main__":
    unittest.main()
