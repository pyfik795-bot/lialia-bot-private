import unittest
from unittest.mock import patch

import main


class TelegramConnectionTestCase(unittest.TestCase):
    def test_telethon_client_uses_direct_connection_without_proxy(self):
        with patch("main.TelegramClient") as client_class:
            client = main.build_akk_client()

        client_class.assert_called_once_with("BOT", main.config.API_ID, main.config.API_HASH)
        self.assertIs(client, client_class.return_value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
