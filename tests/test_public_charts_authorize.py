import unittest
from unittest.mock import patch

from public_charts_authorize import github_cli, secret_names


class AuthorizationTests(unittest.TestCase):
    def test_second_account_cannot_overwrite_existing_chart_secrets(self):
        original = set(secret_names("PUBLIC_CHARTS_DRIVE_"))
        secondary = set(secret_names("PUBLIC_CHARTS_DRIVE2_"))
        legacy = set(secret_names("PUBLIC_CHARTS_ICON_DRIVE_"))
        self.assertEqual(len(secondary), 5)
        self.assertFalse(secondary & original)
        self.assertFalse(secondary & legacy)
        self.assertIn("PUBLIC_CHARTS_DRIVE2_REFRESH_TOKEN", secondary)

    def test_only_dedicated_chart_prefixes_are_allowed(self):
        for prefix in ("GOOGLE_DRIVE_", "VTG_", "", "PUBLIC_CHARTS_"):
            with self.assertRaises(ValueError):
                secret_names(prefix)

    def test_installed_github_cli_path_is_respected(self):
        with patch("public_charts_authorize.shutil.which", return_value="/bin/gh"):
            self.assertEqual(github_cli(), "/bin/gh")
