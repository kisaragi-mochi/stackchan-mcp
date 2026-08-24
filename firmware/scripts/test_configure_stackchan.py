from collections import OrderedDict
import json
from pathlib import Path
import tempfile
import unittest

import configure_stackchan as config


class ConfigureStackchanTest(unittest.TestCase):
    def test_replace_preserves_user_network_settings(self) -> None:
        existing = (
            'CONFIG_DEFAULT_WEBSOCKET_URL="ws://192.0.2.1:8765"\n'
            'CONFIG_DEFAULT_WEBSOCKET_TOKEN="secret"\n'
        )
        block = config.render_managed_block(
            "xiaozhi-conversational",
            "wakenet",
            "xiaozhi-plus-action",
            OrderedDict(
                [("CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL", "y")]
            ),
        )

        updated = config.replace_managed_block(existing, block)

        self.assertIn("CONFIG_DEFAULT_WEBSOCKET_URL", updated)
        self.assertIn("CONFIG_DEFAULT_WEBSOCKET_TOKEN", updated)
        self.assertIn(config.BEGIN_MARKER, updated)

    def test_sdkconfig_sync_changes_only_managed_keys(self) -> None:
        existing = (
            "CONFIG_BOARD_TYPE_STACKCHAN=y\n"
            "# CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL is not set\n"
            "CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT=y\n"
            'CONFIG_WIFI_SSID="keep-me"\n'
        )
        values = OrderedDict(
            [
                ("CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL", "y"),
                ("CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT", None),
            ]
        )
        updated = config.replace_sdkconfig_values(existing, values)

        self.assertIn(
            "CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL=y", updated
        )
        self.assertIn(
            "# CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT is not set", updated
        )
        self.assertIn('CONFIG_WIFI_SSID="keep-me"', updated)
        self.assertIn("CONFIG_BOARD_TYPE_STACKCHAN=y", updated)

    def test_replace_updates_one_managed_block(self) -> None:
        first = config.render_managed_block(
            "mcp-single-shot",
            "wakenet",
            "local-mcp",
            OrderedDict([("CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT", "y")]),
        )
        second = config.render_managed_block(
            "xiaozhi-conversational",
            "wakenet-device-aec",
            "xiaozhi-plus-action",
            OrderedDict(
                [("CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL", "y")]
            ),
        )

        updated = config.replace_managed_block(first + "\n", second)

        self.assertEqual(updated.count(config.BEGIN_MARKER), 1)
        self.assertIn("wakenet-device-aec", updated)
        self.assertNotIn("mcp-single-shot", updated)

    def test_profile_matrix_is_valid(self) -> None:
        for voice in config.available_profiles("voice"):
            for audio in config.available_profiles("audio_ab"):
                transport = (
                    "xiaozhi-plus-action"
                    if voice == "xiaozhi-conversational"
                    else "local-mcp"
                )
                values = config.merge_profiles(
                    config.language_values("zh-cn"),
                    config.load_profile("voice", voice),
                    config.load_profile("audio_ab", audio),
                    config.load_profile("transport", transport),
                )
                config.bind_transport_values(
                    values, transport, "ws://192.0.2.1:8765", "token"
                )
                self.assertEqual(config.validate(values), [], f"{voice}/{audio}")

    def test_xiaozhi_transport_moves_local_gateway_to_action_channel(self) -> None:
        values = config.merge_profiles(
            config.language_values("zh-cn"),
            config.load_profile("voice", "xiaozhi-conversational"),
            config.load_profile("audio_ab", "wakenet"),
            config.load_profile("transport", "xiaozhi-plus-action"),
        )
        config.bind_transport_values(
            values, "xiaozhi-plus-action", "ws://192.0.2.1:8765", "secret"
        )

        self.assertEqual(values[config.PRIMARY_URL], '""')
        self.assertEqual(values[config.ACTION_URL], '"ws://192.0.2.1:8765"')
        self.assertIsNone(values["CONFIG_FORCE_DEFAULT_WEBSOCKET_URL"])
        self.assertIsNone(values["CONFIG_DISABLE_OTA_WEBSOCKET_CONFIG"])
        self.assertEqual(config.validate(values), [])

    def test_language_profile_is_explicit_and_exclusive(self) -> None:
        values = config.language_values("zh-cn")

        self.assertEqual(values["CONFIG_LANGUAGE_ZH_CN"], "y")
        self.assertTrue(
            all(
                value is None
                for key, value in values.items()
                if key != "CONFIG_LANGUAGE_ZH_CN"
            )
        )

    def test_parser_defaults_to_simplified_chinese(self) -> None:
        args = config.build_parser().parse_args([])

        self.assertEqual(args.language, "zh-cn")
        self.assertEqual(args.agent_language, "zh")

    def test_agent_language_is_separate_from_firmware_language(self) -> None:
        profile = config.agent_profile("zh-cn", "zh")

        self.assertEqual(profile["firmware_language"], "zh-cn")
        self.assertEqual(profile["agent_language"], "zh")
        self.assertTrue(profile["must_match_bound_agent_before_device_scoring"])

    def test_verified_binding_is_preserved_only_for_same_languages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "xiaozhi-agent.local.json"
            binding = {
                "agent_id": 2209363,
                "device_mac": "80:45:6b:54:7d:10",
                "audit_path": "audit.json",
                "audit_sha256": "a" * 64,
            }
            path.write_text(
                json.dumps(
                    {
                        "firmware_language": "zh-cn",
                        "agent_language": "zh",
                        "official_token_proxy_endpoint": "http://stackchan/token",
                        "verified_binding": binding,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                config.reusable_verified_binding(path, "zh-cn", "zh"), binding
            )
            self.assertIsNone(
                config.reusable_verified_binding(path, "en-us", "zh")
            )
            self.assertIsNone(
                config.reusable_verified_binding(path, "zh-cn", "en")
            )
            self.assertEqual(
                config.reusable_agent_token_proxy(path), "http://stackchan/token"
            )

    def test_discovers_gateway_from_previous_action_profile(self) -> None:
        values = OrderedDict(
            [
                (config.PRIMARY_URL, '""'),
                (config.ACTION_URL, '"ws://192.0.2.2:8765"'),
                (config.ACTION_TOKEN, '"secret"'),
            ]
        )

        self.assertEqual(
            config.discover_local_gateway(values, "", None),
            ("ws://192.0.2.2:8765", "secret"),
        )

    def test_sensitive_values_are_masked(self) -> None:
        self.assertEqual(
            config.mask_line('CONFIG_DEFAULT_WEBSOCKET_TOKEN="secret"'),
            'CONFIG_DEFAULT_WEBSOCKET_TOKEN="<MASKED>"',
        )


if __name__ == "__main__":
    unittest.main()
