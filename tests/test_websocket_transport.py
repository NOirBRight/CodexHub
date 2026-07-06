import io
import unittest

from websocket_transport import (
    WebSocketFrame,
    close_frame,
    read_frame,
    redacted_handshake_metadata,
    websocket_accept_value,
    websocket_upgrade_response_headers,
    write_frame,
)


def masked_client_frame(payload: bytes, *, opcode: int = 0x1, mask: bytes = b"\x01\x02\x03\x04") -> bytes:
    if len(payload) > 65535:
        raise ValueError("test helper only supports 16-bit payloads")
    if len(payload) <= 125:
        length_bytes = bytes([0x80 | len(payload)])
    else:
        length_bytes = b"\xfe" + len(payload).to_bytes(2, "big")
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes([0x80 | opcode]) + length_bytes + mask + masked


class WebSocketTransportTests(unittest.TestCase):
    def test_accept_value_matches_rfc_example(self):
        self.assertEqual(
            websocket_accept_value("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_upgrade_headers_include_accept_and_protocol(self):
        headers = dict(websocket_upgrade_response_headers("dGhlIHNhbXBsZSBub25jZQ==", protocol="codex"))

        self.assertEqual(headers["Upgrade"], "websocket")
        self.assertEqual(headers["Connection"], "Upgrade")
        self.assertEqual(headers["Sec-WebSocket-Accept"], "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertEqual(headers["Sec-WebSocket-Protocol"], "codex")

    def test_read_masked_client_text_frame(self):
        frame = read_frame(
            io.BytesIO(masked_client_frame(b'{"model":"openai/gpt-5.5"}')),
            expect_masked=True,
            max_payload_bytes=1024,
        )

        self.assertEqual(frame.opcode, 0x1)
        self.assertTrue(frame.fin)
        self.assertEqual(frame.payload, b'{"model":"openai/gpt-5.5"}')

    def test_write_server_close_frame_is_unmasked(self):
        stream = io.BytesIO()

        write_frame(stream, close_frame(1000, "done"), mask=False)

        self.assertEqual(stream.getvalue(), b"\x88\x06\x03\xe8done")

    def test_redacted_handshake_metadata_drops_secret_values(self):
        metadata = redacted_handshake_metadata(
            "/v1/responses?model=openai/gpt-5.5&thread_id=thread-1",
            {
                "Authorization": "Bearer secret-token",
                "Cookie": "sid=secret",
                "Sec-WebSocket-Key": "secret-key",
                "Sec-WebSocket-Protocol": "codex, realtime",
                "X-Codex-Client-Id": "codex-app",
            },
        )

        self.assertEqual(metadata["path"], "/v1/responses")
        self.assertEqual(metadata["query_keys"], ["model", "thread_id"])
        self.assertEqual(metadata["selected_subprotocol"], "codex")
        self.assertIn("authorization", metadata["header_names"])
        serialized = repr(metadata)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("sid=secret", serialized)
        self.assertNotIn("secret-key", serialized)


if __name__ == "__main__":
    unittest.main()
