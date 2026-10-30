import subprocess
import unittest
from unittest.mock import patch

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

from mx_ocr import MAX_IMAGE_BYTES, find_tesseract, ocr_image_bytes


class LocalOCRTests(unittest.TestCase):
    def test_missing_tesseract_keeps_image_unprocessed(self):
        with patch("mx_ocr.find_tesseract", return_value=None):
            self.assertEqual(ocr_image_bytes(b"image-bytes"), "")

    def test_successful_chinese_ocr_returns_clean_text(self):
        result = subprocess.CompletedProcess(
            args=["tesseract"], returncode=0, stdout="  生益电子低吸  \n\nPCB\n", stderr=""
        )
        with patch("mx_ocr.subprocess.run", return_value=result) as run:
            text = ocr_image_bytes(b"fake-image", executable="tesseract")
        self.assertEqual(text, "生益电子低吸\nPCB")
        args = run.call_args.args[0]
        self.assertEqual(args[0], "tesseract")
        self.assertIn("-l", args)
        self.assertIn("chi_sim", args)
        self.assertFalse(Path(args[1]).exists())

    def test_tesseract_is_available_only_when_chinese_language_is_installed(self):
        find_tesseract.cache_clear()
        result = subprocess.CompletedProcess(
            args=["tesseract", "--list-langs"], returncode=0,
            stdout="List of available languages (2):\neng\nchi_sim\n", stderr=""
        )
        with patch("mx_ocr.shutil.which", return_value="tesseract"), patch(
            "mx_ocr.subprocess.run", return_value=result
        ):
            self.assertEqual(find_tesseract(), "tesseract")
        find_tesseract.cache_clear()

    def test_oversized_image_is_skipped(self):
        self.assertEqual(ocr_image_bytes(b"x" * (MAX_IMAGE_BYTES + 1), executable="tesseract"), "")

    def test_timeout_is_non_fatal(self):
        with patch("mx_ocr.subprocess.run", side_effect=subprocess.TimeoutExpired("tesseract", 18)):
            self.assertEqual(ocr_image_bytes(b"fake-image", executable="tesseract"), "")


if __name__ == "__main__":
    unittest.main()
