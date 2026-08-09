import json
from pathlib import Path
import tempfile
import unittest

import xiaozhi_agent_config as agent_config


class FakeResponse:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.value).encode("utf-8")


class XiaozhiAgentConfigTest(unittest.TestCase):
    def test_profile_keeps_agent_and_firmware_languages_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "agent.json"
            path.write_text(
                '{"firmware_language":"zh-cn","agent_language":"zh"}',
                encoding="utf-8",
            )
            profile = agent_config.load_profile(path)

        self.assertEqual(profile["firmware_language"], "zh-cn")
        self.assertEqual(profile["agent_language"], "zh")

    def test_apply_reads_current_agent_and_preserves_other_fields(self) -> None:
        requests = []
        responses = [
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "agent": {
                            "agent_name": "StackChan",
                            "assistant_name": "StackChan",
                            "llm_model": "model",
                            "tts_voice": "voice",
                            "language": "en",
                            "character": "friendly",
                            "mcp_endpoints": [7],
                        }
                    },
                }
            ),
            FakeResponse({"success": True, "data": {}}),
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "agent": {
                            "agent_name": "StackChan",
                            "assistant_name": "StackChan",
                            "llm_model": "model",
                            "tts_voice": "voice",
                            "language": "zh",
                            "character": "friendly",
                            "mcp_endpoints": [7],
                        }
                    },
                }
            ),
        ]

        def opener(request, timeout):
            requests.append((request, timeout))
            return responses.pop(0)

        result = agent_config.apply_agent_language(
            "https://xiaozhi.me", 42, "secret", "zh", opener=opener
        )

        self.assertEqual(result["before_language"], "en")
        self.assertEqual(result["after_language"], "zh")
        self.assertEqual(requests[0][0].method, "GET")
        self.assertEqual(requests[1][0].method, "POST")
        self.assertEqual(requests[2][0].method, "GET")
        payload = json.loads(requests[1][0].data)
        self.assertEqual(payload["language"], "zh")
        self.assertEqual(payload["character"], "friendly")
        self.assertEqual(payload["mcp_endpoints"], [7])
        self.assertEqual(requests[1][0].get_header("Authorization"), "Bearer secret")
        self.assertTrue(result["verified"])
        self.assertEqual(result["changed_protected_fields"], [])

    def test_token_proxy_accepts_stackchan_response_shape(self) -> None:
        def opener(request, timeout):
            self.assertEqual(request.method, "GET")
            self.assertIsNone(request.get_header("Authorization"))
            return FakeResponse({"code": 0, "message": "OK", "data": "x" * 40})

        self.assertEqual(
            agent_config.token_from_proxy("http://stackchan/token", opener=opener),
            "x" * 40,
        )


if __name__ == "__main__":
    unittest.main()
