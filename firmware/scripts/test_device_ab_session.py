import binascii
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

import device_ab_session as session


def ota_entry(sequence: int, state: int = session.OTA_STATE_UNDEFINED) -> bytes:
    entry = bytearray(b"\xff" * session.OTA_ENTRY_SIZE)
    struct.pack_into("<I", entry, 0, sequence)
    struct.pack_into("<I", entry, 24, state)
    struct.pack_into("<I", entry, 28, session.ota_crc(sequence))
    return bytes(entry)


def app_image() -> tuple[bytes, bytes]:
    elf_sha = bytes(range(32))
    description = bytearray(b"\0" * 256)
    description[0:4] = session.APP_DESC_MAGIC
    struct.pack_into("<I", description, 4, 7)
    description[0x10:0x15] = b"2.2.6"
    description[0x30:0x37] = b"xiaozhi"
    description[0x50:0x58] = b"11:16:33"
    description[0x60:0x6b] = b"Aug  7 2026"
    description[0x70:0x76] = b"v5.5.2"
    description[session.APP_ELF_SHA_OFFSET : session.APP_ELF_SHA_OFFSET + 32] = elf_sha
    header = bytearray(b"\0" * session.APP_IMAGE_HEADER_SIZE)
    header[0] = session.APP_IMAGE_MAGIC
    header[1] = 1
    header[0x17] = 1
    image = header + struct.pack("<II", 0x3C000020, len(description)) + description
    image += b"\0"
    image += b"\0" * ((-len(image)) & 15)
    image += __import__("hashlib").sha256(image).digest()
    return bytes(image), elf_sha


class DeviceAbSessionTest(unittest.TestCase):
    def test_matching_asset_image_prefix_skips_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = root / "generated_assets.bin"
            partition = root / "assets-partition.bin"
            candidate.write_bytes(b"asset-image")
            partition.write_bytes(b"asset-image" + b"partition-residue")

            self.assertTrue(
                session.asset_image_matches_partition(candidate, partition)
            )

            partition.write_bytes(b"asset-IMAGE" + b"partition-residue")
            self.assertFalse(
                session.asset_image_matches_partition(candidate, partition)
            )

    def test_parse_app_image_records_boot_identity_and_ignores_slot_residue(self) -> None:
        image, elf_sha = app_image()
        parsed = session.parse_app_image(image + b"old-slot-residue")
        self.assertEqual("xiaozhi", parsed["project_name"])
        self.assertEqual("2.2.6", parsed["version"])
        self.assertEqual("v5.5.2", parsed["idf_version"])
        self.assertEqual(7, parsed["secure_version"])
        self.assertEqual(elf_sha.hex(), parsed["app_elf_sha256"])
        self.assertEqual(len(image), parsed["image_size"])
        self.assertGreater(parsed["trailing_non_ff_bytes"], 0)

    def test_parse_app_image_rejects_invalid_appended_hash(self) -> None:
        image, _ = app_image()
        corrupted = bytearray(image)
        corrupted[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "appended SHA256"):
            session.parse_app_image(bytes(corrupted))

    def test_extract_mmap_asset_verifies_table_and_payload(self) -> None:
        name = b"index.json".ljust(session.MMAP_NAME_LENGTH, b"\0")
        payload = b'{"version":1}'
        entry = name + struct.pack("<IIHH", len(payload), 0, 0, 0)
        combined = entry + b"ZZ" + payload
        image = struct.pack(
            "<III", 1, sum(combined) & 0xFFFF, len(combined)
        ) + combined
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "assets.bin"
            path.write_bytes(image)
            self.assertEqual(payload, session.extract_mmap_asset(path, "index.json"))

    def test_custom_multinet_requires_chinese_wake_route(self) -> None:
        index = {
            "srmodels": "srmodels.bin",
            "multinet_model": {
                "language": "cn",
                "commands": [
                    {"command": "ni hao xiao zhi", "text": "你好小智", "action": "wake"}
                ],
            },
        }
        payload = json.dumps(index, ensure_ascii=False).encode("utf-8")
        name = b"index.json".ljust(session.MMAP_NAME_LENGTH, b"\0")
        entry = name + struct.pack("<IIHH", len(payload), 0, 0, 0)
        combined = entry + b"ZZ" + payload
        image = struct.pack("<III", 1, sum(combined) & 0xFFFF, len(combined)) + combined
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "assets.bin"
            path.write_bytes(image)
            parsed = session.validate_speech_asset_route(
                {"audio_profile": "custom-multinet"}, path
            )
            self.assertEqual("cn", parsed["multinet_model"]["language"])

    def test_backup_reads_exactly_sixteen_mebibytes(self) -> None:
        command = session.backup_command("COM7", Path("backup.bin"))
        self.assertIn("read_flash", command)
        self.assertIn(hex(session.FLASH_SIZE), command)

    def test_prewrite_region_probe_is_read_only(self) -> None:
        command = session.read_region_command("COM7", 0xD000, 0x2000, Path("ota.bin"))
        self.assertIn("read_flash", command)
        self.assertNotIn("write_flash", command)

    @mock.patch("device_ab_session.time.sleep")
    @mock.patch("device_ab_session.subprocess.run")
    def test_read_guard_retries_transient_failure(
        self, run: mock.Mock, _sleep: mock.Mock
    ) -> None:
        run.side_effect = [subprocess.CalledProcessError(2, "read"), None]
        command = session.read_region_command(
            "COM7", 0xD000, 0x2000, Path("ota-retry.bin")
        )

        session.run_read_with_retries(command)

        self.assertEqual(2, run.call_count)

    def test_live_prewrite_guard_requires_exact_critical_state(self) -> None:
        image, _ = app_image()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            table = root / "partition-table.bin"
            otadata = root / "otadata.bin"
            active_app = root / "active-app.bin"
            assets = root / "assets.bin"
            table.write_bytes(b"partition-table")
            otadata.write_bytes(b"o" * 0x2000)
            active_app.write_bytes(image)
            assets.write_bytes(b"assets")
            candidate = {"partition_table_bytes": table.read_bytes()}
            analysis = {
                "original_otadata": otadata.read_bytes(),
                "original_assets_sha256": session.sha256(assets),
                "current_app_identity": session.parse_app_image(image),
            }
            evidence = session.validate_live_prewrite(
                candidate, analysis, table, otadata, active_app, assets
            )
            self.assertEqual(
                analysis["current_app_identity"]["image_sha256"],
                evidence["active_app_identity"]["image_sha256"],
            )
            otadata.write_bytes(b"x" * 0x2000)
            with self.assertRaisesRegex(ValueError, "OTA selection changed"):
                session.validate_live_prewrite(
                    candidate, analysis, table, otadata, active_app, assets
                )

    def test_validate_backup_rejects_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "partial.bin"
            path.write_bytes(b"short")
            with self.assertRaises(ValueError):
                session.validate_backup(path)

    def test_crc_matches_esp_idf_otatool_formula(self) -> None:
        sequence = 7
        expected = binascii.crc32(struct.pack("<I", sequence), 0xFFFFFFFF) & 0xFFFFFFFF
        self.assertEqual(expected, session.ota_crc(sequence))

    def test_active_slot_uses_highest_valid_sequence(self) -> None:
        data = bytearray(b"\xff" * 0x2000)
        data[0 : session.OTA_ENTRY_SIZE] = ota_entry(3)
        start = session.OTA_COPY_STRIDE
        data[start : start + session.OTA_ENTRY_SIZE] = ota_entry(4)
        active = session.active_ota_slot(bytes(data))
        self.assertEqual(1, active["slot"])
        self.assertEqual(1, active["copy"])

    def test_invalid_newer_entry_is_not_selected(self) -> None:
        data = bytearray(b"\xff" * 0x2000)
        data[0 : session.OTA_ENTRY_SIZE] = ota_entry(5)
        start = session.OTA_COPY_STRIDE
        data[start : start + session.OTA_ENTRY_SIZE] = ota_entry(
            6, session.OTA_STATE_ABORTED
        )
        active = session.active_ota_slot(bytes(data))
        self.assertEqual(0, active["slot"])
        self.assertEqual(0, active["copy"])

    def test_next_otadata_selects_inactive_slot_and_preserves_active_copy(self) -> None:
        data = bytearray(b"\xff" * 0x2000)
        data[0 : session.OTA_ENTRY_SIZE] = ota_entry(3)
        active = session.active_ota_slot(bytes(data))
        updated, switch = session.prepare_next_otadata(bytes(data), active, 1)
        self.assertEqual(data[0 : session.OTA_ENTRY_SIZE], updated[0 : session.OTA_ENTRY_SIZE])
        self.assertEqual(1, session.active_ota_slot(updated)["slot"])
        self.assertEqual(1, switch["target_copy"])
        self.assertEqual(
            session.OTA_STATE_NEW,
            session.parse_ota_entries(updated)[switch["target_copy"]]["state"],
        )

    def test_region_write_never_uses_zero_or_merged_image(self) -> None:
        command = session.write_region_command(
            "COM7", 0x410000, Path("bundle/xiaozhi.bin")
        )
        rendered = " ".join(command)
        self.assertIn("write_flash", command)
        self.assertIn("0x410000", command)
        self.assertNotIn("merged-binary.bin", rendered)
        self.assertNotIn(" 0x0 ", f" {rendered} ")

    def test_rollback_writes_only_original_assets_and_otadata(self) -> None:
        candidate = {
            "assets_partition": {"offset": 0x800000},
            "otadata": {"offset": 0xD000},
        }
        commands = session.rollback_region_commands(
            "COM7", candidate, Path("rollback-assets.bin"), Path("rollback-otadata.bin")
        )
        rendered = [" ".join(command) for command in commands]
        self.assertEqual(2, len(commands))
        self.assertIn("0x800000", rendered[0])
        self.assertIn("rollback-assets.bin", rendered[0])
        self.assertIn("0xd000", rendered[1])
        self.assertIn("rollback-otadata.bin", rendered[1])
        self.assertTrue(all("merged-binary.bin" not in item for item in rendered))

    def test_rollback_attempts_otadata_even_when_assets_restore_fails(self) -> None:
        commands = [["restore-assets"], ["restore-otadata"]]
        failure = __import__("subprocess").CalledProcessError(2, commands[0])
        with mock.patch.object(
            session.subprocess, "run", side_effect=[failure, mock.DEFAULT]
        ) as run:
            errors = session.execute_rollback(commands)
        self.assertEqual(2, run.call_count)
        self.assertEqual(1, len(errors))

    def test_serial_requires_device_reported_elf_hash(self) -> None:
        expected = "f43456d7c436d242fd5ace45b0be48b14d1222a49a8f49542c274403f1d185ea"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "serial.log"
            path.write_text(
                "# firmware_file_sha256=" + "a" * 64 + "\n"
                "I app_init: ELF file SHA256: f43456d7c436d242\n",
                encoding="utf-8",
            )
            self.assertTrue(session.serial_proves_candidate(path, expected))
            path.write_text("# expected_app_elf_sha256=" + expected, encoding="utf-8")
            self.assertFalse(session.serial_proves_candidate(path, expected))

    def test_boot_capture_reset_keeps_gpio0_high_and_pulses_enable(self) -> None:
        events = []

        class FakeSerial:
            @property
            def dtr(self):
                return None

            @dtr.setter
            def dtr(self, value):
                events.append(("dtr", value))

            @property
            def rts(self):
                return None

            @rts.setter
            def rts(self, value):
                events.append(("rts", value))

        with mock.patch.object(session.time, "sleep") as sleep:
            session.reset_for_boot_capture(FakeSerial())

        self.assertEqual(events, [("dtr", False), ("rts", True), ("rts", False)])
        sleep.assert_called_once_with(0.1)

    def test_combined_report_binds_build_facts_to_device_identity(self) -> None:
        expected = "f43456d7c436d242fd5ace45b0be48b14d1222a49a8f49542c274403f1d185ea"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = root / "xiaozhi.bin"
            app.write_bytes(b"app")
            serial_log = root / "serial.log"
            serial_log.write_text(
                f"I Application: App identity: project=xiaozhi version=1 ELF SHA256={expected}\n",
                encoding="utf-8",
            )
            candidate = {
                "variant": "a-wakenet",
                "app": app,
                "app_elf_sha256": expected,
                "assets_sha256": "b" * 64,
                "assets_index": {"srmodels": "srmodels.bin"},
                "partition_table_sha256": "c" * 64,
                "manifest": {"firmware_sha256": "d" * 64},
                "consistency": {
                    "configuration": {"effective": {"CONFIG_USE_AFE_WAKE_WORD": "1"}},
                    "models": {"build_log": {"wakenet": ["wn9"]}},
                },
            }
            analysis = {
                "target_slot": 1,
                "active": {"slot": 0},
                "original_assets_sha256": "e" * 64,
                "original_otadata_sha256": "f" * 64,
            }
            json_path, markdown_path, matched = session.write_device_consistency_report(
                root, candidate, serial_log, "0" * 64, analysis
            )
            self.assertTrue(matched)
            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())

    def test_combined_report_accepts_official_direct_model_inventory(self) -> None:
        expected = "9004b1e9b04993a4da2ed8ca7cb2129db800135b991cf1ff3346691336b9594f"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = root / "xiaozhi.bin"
            app.write_bytes(b"official-app")
            serial_log = root / "serial.log"
            serial_log.write_text(
                "I app_init: ELF file SHA256: 9004b1e9b04993a4\n",
                encoding="utf-8",
            )
            candidate = {
                "variant": "official-stackchan-zh",
                "app": app,
                "app_elf_sha256": expected,
                "assets_sha256": "b" * 64,
                "assets_index": {"srmodels": "srmodels.bin"},
                "partition_table_sha256": "c" * 64,
                "manifest": {"firmware_sha256": "d" * 64},
                "consistency": {
                    "configuration": {"language": "zh-CN"},
                    "models": {
                        "wakenet": [
                            "wn9_histackchan_tts3",
                            "wn9_xiaoluxiaolu_tts2",
                        ],
                        "multinet": [],
                    },
                },
            }
            analysis = {
                "target_slot": 0,
                "active": {"slot": 1},
                "original_assets_sha256": "e" * 64,
                "original_otadata_sha256": "f" * 64,
            }
            _, markdown_path, matched = session.write_device_consistency_report(
                root, candidate, serial_log, "0" * 64, analysis
            )
            self.assertTrue(matched)
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("wn9_histackchan_tts3", markdown)
            self.assertIn("Candidate OTA slot: `0`", markdown)


if __name__ == "__main__":
    unittest.main()
