from pathlib import Path
import tempfile
import unittest

import build_consistency_report as report


class BuildConsistencyReportTest(unittest.TestCase):
    def test_config_parser_handles_enabled_and_unset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sdkconfig"
            path.write_text(
                "CONFIG_USE_AFE_WAKE_WORD=y\n# CONFIG_USE_CUSTOM_WAKE_WORD is not set\n",
                encoding="utf-8",
            )
            values = report.parse_sdkconfig(path)

        self.assertEqual(values["CONFIG_USE_AFE_WAKE_WORD"], "y")
        self.assertIsNone(values["CONFIG_USE_CUSTOM_WAKE_WORD"])

    def test_header_parser_reads_build_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sdkconfig.h"
            path.write_text(
                "#define CONFIG_STACKCHAN_TOUCH_PTT 1\n"
                '#define CONFIG_DEFAULT_WEBSOCKET_URL "ws://example"\n',
                encoding="utf-8",
            )
            values = report.parse_sdkconfig_header(path)

        self.assertEqual(values["CONFIG_STACKCHAN_TOUCH_PTT"], "1")
        self.assertEqual(values["CONFIG_DEFAULT_WEBSOCKET_URL"], '"ws://example"')

    def test_secret_values_are_redacted(self) -> None:
        values = {
            "CONFIG_DEFAULT_WEBSOCKET_TOKEN": '"secret"',
            "CONFIG_DEFAULT_WEBSOCKET_URL": '"ws://example"',
        }
        selected = report.selected_config(values)

        self.assertEqual(selected["CONFIG_DEFAULT_WEBSOCKET_TOKEN"], "<MASKED>")
        self.assertEqual(selected["CONFIG_DEFAULT_WEBSOCKET_URL"], '"ws://example"')

    def test_config_equivalence_normalizes_enabled_values_only(self) -> None:
        self.assertTrue(report.config_equivalent("y", "1"))
        self.assertFalse(report.config_equivalent('"ws://local"', '""'))

    def test_agent_config_and_firmware_language_remain_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "xiaozhi-agent.local.json"
            path.write_text(
                '{"firmware_language":"zh-cn","agent_language":"zh"}\n',
                encoding="utf-8",
            )
            agent = report.parse_agent_config(path)

        self.assertEqual(agent["firmware_language"], "zh-cn")
        self.assertEqual(agent["agent_language"], "zh")
        self.assertEqual(
            report.effective_firmware_language({"CONFIG_LANGUAGE_ZH_CN": "1"}),
            "zh-cn",
        )

    def test_verified_agent_binding_audit(self) -> None:
        hashes = {"llm_model": "a" * 64, "memory": "b" * 64}
        agent_config = {
            "agent_language": "zh",
            "verified_binding": {
                "agent_id": 2209363,
                "device_mac": "80:45:6b:54:7d:10",
            },
        }
        audit = {
            "device": {"mac_address": "80:45:6B:54:7D:10"},
            "agent": {"agent_id": 2209363, "after_language": "zh"},
            "protected_field_sha256_before": hashes,
            "protected_field_sha256_after": dict(hashes),
            "verification": {
                "language_verified": True,
                "protected_fields_verified": True,
            },
            "safety": {
                "access_token_stored": False,
                "developer_secret_stored": False,
                "flash_invoked": False,
            },
        }
        self.assertEqual(report.agent_binding_errors(agent_config, audit), [])
        audit["agent"]["after_language"] = "en"
        self.assertIn(
            "audited Agent language does not match the build policy",
            report.agent_binding_errors(agent_config, audit),
        )

    def test_device_only_error_does_not_block_preflash_review(self) -> None:
        findings = [
            {
                "severity": "error",
                "scope": "device",
                "code": "serial_elf_identity_mismatch",
                "message": "candidate has not run yet",
            }
        ]
        self.assertTrue(report.preflash_ready(findings))
        findings.append(
            {
                "severity": "error",
                "scope": "preflash",
                "code": "local_effective_config_mismatch",
                "message": "wrong build profile",
            }
        )
        self.assertFalse(report.preflash_ready(findings))

    def test_extracts_wakenet_and_skipped_multinet(self) -> None:
        models = report.extract_model_names(
            "wakenet models: wn9_nihaoxiaozhi_tts\n"
            "Found multinet models ['mn6_cn'] but skipping\n"
        )

        self.assertEqual(models["wakenet"], ["wn9_nihaoxiaozhi_tts"])
        self.assertEqual(models["skipped_multinet"], ["mn6_cn"])

    def test_latest_log_accepts_multinet_only_asset_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "log"
            log_dir.mkdir()
            generic = log_dir / "idf_py_stdout_output_1"
            generic.write_text("generic compile output\n", encoding="utf-8")
            model = log_dir / "idf_py_stdout_output_2"
            model.write_text(
                "multinet models: mn6_cn (will be packaged)\n",
                encoding="utf-8",
            )

            selected = report.latest_build_log(Path(temp_dir))

        self.assertEqual(selected, model)

    def test_model_evidence_keeps_only_model_lines(self) -> None:
        evidence = report.extract_model_evidence(
            'CONFIG_DEFAULT_WEBSOCKET_TOKEN="secret"\n'
            "multinet models: mn6_cn (will be packaged)\n"
        )

        self.assertEqual(
            evidence, ["multinet models: mn6_cn (will be packaged)"]
        )

    def test_app_elf_sha256_reads_esp_app_description(self) -> None:
        expected = bytes(range(32))
        image = bytearray(b"\xff" * 512)
        start = 0x20
        image[start : start + 4] = report.APP_DESC_MAGIC
        digest_start = start + report.APP_ELF_SHA_OFFSET
        image[digest_start : digest_start + 32] = expected
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "app.bin"
            path.write_bytes(image)
            self.assertEqual(expected.hex(), report.app_elf_sha256(path))

    def test_serial_extracts_only_device_reported_elf_identity(self) -> None:
        expected = "f43456d7c436d242fd5ace45b0be48b14d1222a49a8f49542c274403f1d185ea"
        serial = report.extract_serial(
            "# firmware_sha256=" + "a" * 64 + "\n"
            "I Application: App identity: project=xiaozhi version=2.2.6 "
            f"ELF SHA256={expected}\n"
        )
        self.assertEqual([expected], serial["device_elf_sha256"])


if __name__ == "__main__":
    unittest.main()
