import os
import unittest
from unittest.mock import patch

from tests.legacy import _private_live_credentials_configured


class LiveTestSafetyRegressionTestCase(unittest.TestCase):
    def test_placeholder_defaults_never_authorize_private_live_login(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_private_live_credentials_configured())

    def test_explicit_session_or_username_password_authorizes_private_live_login(self):
        with patch.dict(os.environ, {"IG_SESSIONID": "test-session"}, clear=True):
            self.assertTrue(_private_live_credentials_configured())
        with patch.dict(
            os.environ,
            {"IG_USERNAME": "test-user", "IG_PASSWORD": "test-password"},
            clear=True,
        ):
            self.assertTrue(_private_live_credentials_configured())

    def test_partial_username_password_pair_does_not_authorize_private_live_login(self):
        with patch.dict(os.environ, {"IG_USERNAME": "test-user"}, clear=True):
            self.assertFalse(_private_live_credentials_configured())
        with patch.dict(os.environ, {"IG_PASSWORD": "test-password"}, clear=True):
            self.assertFalse(_private_live_credentials_configured())


if __name__ == "__main__":
    unittest.main()
