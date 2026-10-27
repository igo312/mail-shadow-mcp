from __future__ import annotations

import base64
import socket
import threading
import unittest

from app.imap_check import (
    ImapAuthError,
    ImapUnreachable,
    _decode_modified_utf7,
    _parse_list_entry,
    check_login,
    list_folders,
)
from app.providers import ImapSettings


class FakeIMAPServer:
    def __init__(
        self,
        *,
        auth_ok: bool = True,
        advertise_plain: bool = False,
        folders: list[str] | None = None,
    ) -> None:
        self.auth_ok = auth_ok
        self.advertise_plain = advertise_plain
        self.folders = folders
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.commands: list[str] = []
        self.thread.start()

    def _send(self, connection: socket.socket, data: bytes) -> None:
        connection.sendall(data)

    def _serve(self) -> None:
        try:
            connection, _ = self.socket.accept()
        except OSError:
            return
        connection.settimeout(5)
        caps = "IMAP4rev1 IDLE UIDPLUS" + (" AUTH=PLAIN" if self.advertise_plain else "")
        self._send(connection, f"* OK [CAPABILITY {caps}] fake imap ready\r\n".encode())
        reader = connection.makefile("rb")
        try:
            for raw_line in reader:
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                self.commands.append(line)
                if not line:
                    continue
                parts = line.split()
                tag = parts[0]
                command = parts[1].upper() if len(parts) > 1 else ""
                if command == "CAPABILITY":
                    self._send(connection, f"* CAPABILITY {caps}\r\n{tag} OK CAPABILITY completed\r\n".encode())
                elif command == "ID":
                    self._send(connection, f"{tag} BAD ID not supported\r\n".encode())
                elif command == "LOGIN":
                    if self.auth_ok:
                        self._send(connection, f"{tag} OK LOGIN completed\r\n".encode())
                    else:
                        self._send(connection, f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials\r\n".encode())
                elif command == "AUTHENTICATE":
                    self._send(connection, b"+ \r\n")
                    challenge = reader.readline()
                    self.commands.append(challenge.decode("utf-8", "replace").strip())
                    if self.auth_ok:
                        self._send(connection, f"{tag} OK AUTHENTICATE completed\r\n".encode())
                    else:
                        self._send(connection, f"{tag} NO [AUTHENTICATIONFAILED] invalid\r\n".encode())
                elif command == "LIST":
                    for entry in self.folders or ['(\\HasNoChildren) "/" "INBOX"']:
                        self._send(connection, f"* LIST {entry}\r\n".encode())
                    self._send(connection, f"{tag} OK LIST completed\r\n".encode())
                elif command == "LOGOUT":
                    self._send(connection, f"* BYE\r\n{tag} OK LOGOUT completed\r\n".encode())
                    break
                else:
                    self._send(connection, f"{tag} BAD unknown command\r\n".encode())
        except (OSError, ValueError):
            pass
        finally:
            try:
                reader.close()
            except OSError:
                pass
            connection.close()

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass
        self.thread.join(timeout=2)


def settings(port: int, **overrides) -> ImapSettings:
    base = dict(
        host="127.0.0.1",
        port=port,
        tls_mode="none",
        username="user@example.com",
        provider_key="custom",
        provider_name="custom",
        hint="",
    )
    base.update(overrides)
    return ImapSettings(**base)


class ImapCheckTest(unittest.TestCase):
    def test_successful_login(self) -> None:
        server = FakeIMAPServer()
        try:
            check_login(settings(server.port), "app-password", timeout=5)
        finally:
            server.close()
        joined = "\n".join(server.commands)
        self.assertIn("ID", joined)
        self.assertIn("LOGIN", joined)

    def test_auth_failure(self) -> None:
        server = FakeIMAPServer(auth_ok=False, advertise_plain=False)
        try:
            with self.assertRaises(ImapAuthError) as ctx:
                check_login(settings(server.port), "wrong", timeout=5)
        finally:
            server.close()
        self.assertIn("登录失败", str(ctx.exception))

    def test_plain_fallback_when_login_is_rejected(self) -> None:
        # Advertise PLAIN and make LOGIN fail; the checker should still try
        # AUTHENTICATE PLAIN and succeed.
        class PlainOnlyServer(FakeIMAPServer):
            def _serve(self) -> None:
                connection, _ = self.socket.accept()
                connection.settimeout(5)
                caps = "IMAP4rev1 AUTH=PLAIN"
                self._send(connection, f"* OK [CAPABILITY {caps}] ready\r\n".encode())
                reader = connection.makefile("rb")
                try:
                    for raw_line in reader:
                        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                        self.commands.append(line)
                        parts = line.split()
                        tag, command = parts[0], parts[1].upper()
                        if command == "CAPABILITY":
                            self._send(connection, f"* CAPABILITY {caps}\r\n{tag} OK CAPABILITY completed\r\n".encode())
                        elif command == "ID":
                            self._send(connection, f"{tag} BAD ID not supported\r\n".encode())
                        elif command == "LOGIN":
                            self._send(connection, f"{tag} NO LOGIN disabled\r\n".encode())
                        elif command == "AUTHENTICATE":
                            self._send(connection, b"+ \r\n")
                            payload = reader.readline().strip()
                            decoded = base64.b64decode(payload).decode("utf-8", "replace")
                            self.commands.append("PLAIN:" + decoded)
                            self._send(connection, f"{tag} OK AUTHENTICATE completed\r\n".encode())
                        elif command == "LOGOUT":
                            self._send(connection, f"* BYE\r\n{tag} OK LOGOUT completed\r\n".encode())
                            break
                        else:
                            self._send(connection, f"{tag} BAD unknown command\r\n".encode())
                except (OSError, ValueError):
                    pass
                finally:
                    reader.close()
                    connection.close()

        server = PlainOnlyServer()
        try:
            check_login(settings(server.port), "app-password", timeout=5)
        finally:
            server.close()
        self.assertTrue(any(cmd.startswith("PLAIN:") for cmd in server.commands))

    def test_unreachable(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(ImapUnreachable):
            check_login(settings(port), "app-password", timeout=1)


class ListFoldersTest(unittest.TestCase):
    def test_decodes_modified_utf7(self) -> None:
        self.assertEqual(_decode_modified_utf7("&XfJT0ZAB-"), "已发送")
        self.assertEqual(_decode_modified_utf7("INBOX"), "INBOX")
        self.assertEqual(_decode_modified_utf7("A&-B"), "A&B")
        self.assertEqual(_decode_modified_utf7("&ZeVnLIqe-"), "日本語")

    def test_parses_list_entries(self) -> None:
        self.assertEqual(
            _parse_list_entry(b'(\\HasNoChildren) "/" "INBOX"'), "INBOX"
        )
        self.assertEqual(
            _parse_list_entry(b'(\\HasNoChildren) "/" "&XfJT0ZAB-"'), "已发送"
        )
        self.assertIsNone(_parse_list_entry(b'(\\Noselect) "/" "Parent"'))
        self.assertIsNone(_parse_list_entry(b"garbage"))

    def test_parses_literal_list_entries(self) -> None:
        # imaplib returns (header, literal) for a server that uses an IMAP
        # literal instead of a quoted mailbox name.
        self.assertEqual(
            _parse_list_entry((b'(\\HasNoChildren) "/" {7}', b"Archive")), "Archive"
        )
        self.assertEqual(
            _parse_list_entry((b'(\\HasNoChildren) "/" {12}', b"&XfJT0ZAB-")),
            "已发送",
        )
        self.assertIsNone(
            _parse_list_entry((b'(\\Noselect) "/" {6}', b"Parent"))
        )

    def test_list_folders_returns_inbox_first(self) -> None:
        server = FakeIMAPServer(
            folders=[
                '(\\HasNoChildren) "/" "&XfJT0ZAB-"',
                '(\\HasNoChildren) "/" "INBOX"',
                '(\\HasNoChildren) "/" "Archive"',
            ]
        )
        try:
            names = list_folders(settings(server.port), "app-password", timeout=5)
        finally:
            server.close()
        self.assertEqual(names, ["INBOX", "Archive", "已发送"])

    def test_list_folders_auth_failure(self) -> None:
        server = FakeIMAPServer(auth_ok=False)
        try:
            with self.assertRaises(ImapAuthError):
                list_folders(settings(server.port), "wrong", timeout=5)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
