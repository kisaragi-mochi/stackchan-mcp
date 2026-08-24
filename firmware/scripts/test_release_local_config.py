from pathlib import Path
import tempfile
import unittest

import release


class ReleaseLocalConfigTest(unittest.TestCase):
    def test_local_defaults_preserve_explicit_unsets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sdkconfig.defaults.local"
            path.write_text(
                "CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL=y\n"
                "# CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT is not set\n",
                encoding="utf-8",
            )

            entries = release._read_local_sdkconfig_defaults(path)

        self.assertEqual(
            entries,
            [
                "CONFIG_STACKCHAN_VOICE_MODE_XIAOZHI_CONVERSATIONAL=y",
                "# CONFIG_STACKCHAN_VOICE_MODE_MCP_SINGLE_SHOT is not set",
            ],
        )

    def test_later_unset_overrides_assignment_for_same_key(self) -> None:
        merged = release._merge_sdkconfig_overrides(
            ["CONFIG_EXAMPLE=y"],
            ["# CONFIG_EXAMPLE is not set"],
        )

        self.assertEqual(merged, ["# CONFIG_EXAMPLE is not set"])


if __name__ == "__main__":
    unittest.main()
