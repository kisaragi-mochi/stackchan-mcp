from pathlib import Path
import tempfile
import unittest

import build_stackchan_ab as ab


class BuildStackchanAbTest(unittest.TestCase):
    def test_default_matrix_contains_hidden_mn6_and_aec_variants(self) -> None:
        variants = ab.selected_variants(None)
        names = [item[0] for item in variants]

        self.assertIn("b-wakenet-mn6-flag", names)
        self.assertIn("d-wakenet-device-aec", names)
        self.assertIn("e-wakenet-server-aec", names)

    def test_build_command_never_contains_flash_tools(self) -> None:
        command = ab.build_command()
        rendered = " ".join(command).lower()

        self.assertIn("release.py stackchan", rendered)
        self.assertNotIn("esptool", rendered)
        self.assertNotIn("write_flash", rendered)

    def test_complete_bundle_contains_offline_merged_image(self) -> None:
        self.assertIn("merged-binary.bin", ab.FLASH_BUNDLE_FILES)
        self.assertIn("generated_assets.bin", ab.FLASH_BUNDLE_FILES)
        self.assertIn("flasher_args.json", ab.FLASH_BUNDLE_FILES)

    def test_build_directory_guard_accepts_only_direct_build_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "firmware"
            root.mkdir()
            ab.validate_build_dir(root / "build", root)
            with self.assertRaises(RuntimeError):
                ab.validate_build_dir(root.parent, root)

    def test_single_variant_selection_is_stable(self) -> None:
        self.assertEqual(
            ab.selected_variants(["a-wakenet"]),
            [
                (
                    "a-wakenet",
                    "xiaozhi-conversational",
                    "wakenet",
                    "xiaozhi-plus-action",
                )
            ],
        )

    def test_byte_diff_ranges_coalesces_contiguous_offsets(self) -> None:
        self.assertEqual(
            ab.byte_diff_ranges(b"abcdef", b"aXYdeZ"),
            [
                {"start": 1, "end": 2, "length": 2},
                {"start": 5, "end": 5, "length": 1},
            ],
        )

    def test_compare_variants_marks_metadata_only_pair_as_negative_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            left = bytearray(256)
            right = bytearray(left)
            left[32:36] = ab.build_consistency_report.APP_DESC_MAGIC
            right[32:36] = ab.build_consistency_report.APP_DESC_MAGIC
            right[32 + 80] = 1
            right[32 + 144] = 2
            right[-1] = 3
            for name, image in (("a", left), ("b", right)):
                candidate = run_dir / name
                (candidate / "flash-bundle").mkdir(parents=True)
                (candidate / "xiaozhi.bin").write_bytes(image)
                (candidate / "flash-bundle" / "generated_assets.bin").write_bytes(b"same")

            comparison = ab.compare_variants(run_dir, "a", "b")

            self.assertTrue(comparison["app_differences_metadata_only"])
            self.assertTrue(comparison["assets_equal"])
            self.assertTrue(comparison["behavior_payload_equal"])

    def test_compare_variants_rejects_code_or_asset_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            left = bytearray(256)
            right = bytearray(left)
            left[32:36] = ab.build_consistency_report.APP_DESC_MAGIC
            right[32:36] = ab.build_consistency_report.APP_DESC_MAGIC
            right[12] = 1
            for name, image, assets in (("a", left, b"one"), ("b", right, b"two")):
                candidate = run_dir / name
                (candidate / "flash-bundle").mkdir(parents=True)
                (candidate / "xiaozhi.bin").write_bytes(image)
                (candidate / "flash-bundle" / "generated_assets.bin").write_bytes(assets)

            comparison = ab.compare_variants(run_dir, "a", "b")

            self.assertFalse(comparison["app_differences_metadata_only"])
            self.assertFalse(comparison["assets_equal"])
            self.assertFalse(comparison["behavior_payload_equal"])


if __name__ == "__main__":
    unittest.main()
