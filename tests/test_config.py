import os
import tempfile
import unittest
from pathlib import Path

from qrp.config import load_env_file


class ConfigTests(unittest.TestCase):
    def test_load_env_file_without_overwriting_existing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("ALPHA='from file'\nBETA=second\n", encoding="utf-8")
            previous = os.environ.get("ALPHA")
            os.environ["ALPHA"] = "existing"
            try:
                loaded = load_env_file(path)
                self.assertEqual(loaded["ALPHA"], "from file")
                self.assertEqual(os.environ["ALPHA"], "existing")
                self.assertEqual(os.environ["BETA"], "second")
            finally:
                if previous is None:
                    os.environ.pop("ALPHA", None)
                else:
                    os.environ["ALPHA"] = previous
                os.environ.pop("BETA", None)
