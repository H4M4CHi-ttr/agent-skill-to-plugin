from __future__ import annotations

from unittest import mock
import unittest

from agent_skill_to_plugin.fetchers import http as http_module


PUBLIC_IP = "93.184.216.34"


class PinnedHttpTransportTests(unittest.TestCase):
    def test_connection_uses_validated_ip_and_original_tls_name(self) -> None:
        plain_socket = mock.Mock()
        tls_socket = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = tls_socket
        connection = http_module._PinnedHTTPSConnection(
            "public.example",
            timeout=3,
            context=context,
            pinned_addresses=(PUBLIC_IP,),
            server_hostname="public.example",
        )
        with mock.patch.object(
            http_module.socket,
            "create_connection",
            return_value=plain_socket,
        ) as create_connection:
            connection.connect()

        create_connection.assert_called_once_with((PUBLIC_IP, 443), 3, None)
        context.wrap_socket.assert_called_once_with(
            plain_socket,
            server_hostname="public.example",
        )
        self.assertIs(connection.sock, tls_socket)


if __name__ == "__main__":
    unittest.main()
