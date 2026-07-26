import unittest

from sse_events import (
    DEFAULT_MAX_FRAME_BYTES,
    SseAssemblerClosedError,
    SseEventAssembler,
    SseFrameTooLargeError,
)


class SseEventAssemblerTests(unittest.TestCase):
    def test_complete_event_is_emitted_with_exact_raw_bytes(self):
        assembler = SseEventAssembler()

        self.assertEqual(assembler.feed(b"data: {\"type\":"), ())
        events = assembler.feed(b"\"response.created\"}\r\n\r\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].raw,
            b'data: {"type":"response.created"}\r\n\r\n',
        )
        self.assertEqual(events[0].data, b'{"type":"response.created"}')

    def test_semantics_are_invariant_across_line_endings_and_chunk_boundaries(self):
        for ending in (b"\n", b"\r\n", b"\r"):
            raw = ending.join(
                (
                    b": keepalive",
                    b"event: update",
                    b"id:evt-7",
                    b"retry: 1500",
                    b"data:first",
                    b"data: second",
                    b"x-extension: opaque",
                    b"",
                    b"",
                )
            )
            expected_line_kinds = (
                "comment",
                "field",
                "field",
                "field",
                "field",
                "field",
                "field",
            )
            partitions = (
                (raw,),
                tuple(bytes((byte,)) for byte in raw),
                (raw[:1], raw[1:]),
                (raw[:-1], raw[-1:]),
            )
            if ending == b"\r\n":
                split_crlf = raw.index(b"\r\n") + 1
                partitions += ((raw[:split_crlf], raw[split_crlf:]),)

            for chunks in partitions:
                with self.subTest(ending=ending, chunk_lengths=tuple(map(len, chunks))):
                    assembler = SseEventAssembler()
                    events = ()
                    for chunk in chunks:
                        emitted = assembler.feed(chunk)
                        self.assertFalse(events and emitted)
                        events += emitted
                    events += assembler.finish().events

                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertEqual(event.raw, raw)
                    self.assertEqual(event.data, b"first\nsecond")
                    self.assertEqual(event.event, b"update")
                    self.assertEqual(event.id, b"evt-7")
                    self.assertEqual(event.retry, 1500)
                    self.assertEqual(
                        tuple(line.kind for line in event.lines),
                        expected_line_kinds,
                    )
                    self.assertEqual(event.lines[-1].name, b"x-extension")
                    self.assertEqual(event.lines[-1].value, b"opaque")

    def test_bom_malformed_fields_and_invalid_utf8_remain_lossless(self):
        raw = (
            b"\xef\xbb\xbfdata:  first\r\n"
            b"data:\xff\r\n"
            b"id: ignored\0id\r\n"
            b"retry: 12ms\r\n"
            b"field-without-colon\r\n"
            b"\r\n"
        )

        event = SseEventAssembler().feed(raw)[0]

        self.assertEqual(event.raw, raw)
        self.assertEqual(event.data, b" first\n\xff")
        self.assertIsNone(event.id)
        self.assertIsNone(event.retry)
        self.assertEqual(event.lines[-1].name, b"field-without-colon")
        self.assertEqual(event.lines[-1].value, b"")
        self.assertEqual(tuple(line.raw for line in event.lines), tuple(raw.splitlines(keepends=True)[:-1]))

    def test_oversized_numeric_retry_is_ignored_without_blocking_a_later_valid_value(self):
        oversized_retry = b"9" * 5000
        raw = b"retry:" + oversized_retry + b"\nretry: 1500\ndata: ok\n\n"

        event = SseEventAssembler().feed(raw)[0]

        self.assertEqual(event.raw, raw)
        self.assertEqual(event.retry, 1500)
        self.assertEqual(event.data, b"ok")

    def test_empty_events_are_emitted_and_multiple_frames_release_storage(self):
        assembler = SseEventAssembler()

        events = assembler.feed(b"\n\ndata: one\n\ndata: two\n\n")

        self.assertEqual([event.raw for event in events], [b"\n", b"\n", b"data: one\n\n", b"data: two\n\n"])
        self.assertEqual([event.data for event in events], [b"", b"", b"one", b"two"])
        self.assertEqual(assembler.buffered_bytes, 0)

    def test_eof_discards_incomplete_frame_without_emitting_it(self):
        assembler = SseEventAssembler()
        self.assertEqual(assembler.feed(b"data: {\"type\":\"response.completed\"}\r\n"), ())

        termination = assembler.finish()

        self.assertEqual(termination.events, ())
        self.assertEqual(termination.disposition, "incomplete")
        self.assertEqual(termination.discarded_bytes, 37)
        self.assertEqual(assembler.buffered_bytes, 0)
        with self.assertRaises(SseAssemblerClosedError) as raised:
            assembler.feed(b"\r\n")
        self.assertEqual(raised.exception.disposition, "incomplete")

    def test_completion_bytes_are_exact_across_line_endings_and_split_crlf(self):
        cases = (
            ("partial_line", (b"data: ok",), b"\n\n"),
            ("lf_line", (b"data: ok\n",), b"\n"),
            ("lf_event", (b"data: ok\n\n",), b""),
            ("crlf_line", (b"data: ok\r\n",), b"\n"),
            ("crlf_event", (b"data: ok\r\n\r\n",), b""),
            ("cr_line", (b"data: ok\r",), b"\r"),
            ("split_crlf_line", (b"data: ok\r", b"\n"), b"\n"),
            ("cr_event", (b"data: ok\r\r",), b""),
            ("split_consecutive_cr_event", (b"data: ok\r", b"\r"), b""),
            ("empty_cr_event", (b"\r",), b""),
        )

        for name, chunks, expected in cases:
            with self.subTest(name=name):
                assembler = SseEventAssembler()
                for chunk in chunks:
                    assembler.feed(chunk)

                self.assertEqual(assembler.completion_bytes(), expected)

    def test_size_limit_fails_closed_with_counts_only(self):
        self.assertEqual(DEFAULT_MAX_FRAME_BYTES, 16 * 1024 * 1024)
        assembler = SseEventAssembler(max_frame_bytes=12)
        secret = b"data: SECRET"

        with self.assertRaises(SseFrameTooLargeError) as raised:
            assembler.feed(secret + b"!")

        error = raised.exception
        self.assertEqual(error.classification, "sse_frame_too_large")
        self.assertEqual(error.pending_bytes, 13)
        self.assertEqual(error.max_frame_bytes, 12)
        self.assertNotIn("SECRET", str(error))
        self.assertEqual(assembler.buffered_bytes, 0)
        with self.assertRaises(SseAssemblerClosedError) as closed:
            assembler.feed(b"\n\n")
        self.assertEqual(closed.exception.disposition, "size_limit")

    def test_size_limit_applies_to_each_frame_not_the_whole_input_chunk(self):
        assembler = SseEventAssembler(max_frame_bytes=8)

        events = assembler.feed(b"data:a\n\ndata:b\n\n")

        self.assertEqual([event.data for event in events], [b"a", b"b"])
        self.assertEqual(assembler.buffered_bytes, 0)

    def test_cancel_and_reset_have_bounded_deterministic_outcomes(self):
        assembler = SseEventAssembler()
        assembler.feed(b"data: secret")

        cancelled = assembler.cancel()
        self.assertEqual(cancelled.disposition, "cancelled")
        self.assertEqual(cancelled.discarded_bytes, 12)
        self.assertEqual(cancelled.events, ())
        with self.assertRaises(SseAssemblerClosedError):
            assembler.feed(b"\n\n")

        reset = assembler.reset()
        self.assertEqual(reset.disposition, "reset")
        self.assertEqual(reset.discarded_bytes, 0)
        self.assertEqual(assembler.feed(b"data: ready\n\n")[0].data, b"ready")

    def test_long_line_remains_chunk_invariant_under_one_byte_fragmentation(self):
        payload = b"x" * (64 * 1024)
        assembler = SseEventAssembler()

        events = ()
        for byte in b"data: " + payload + b"\n\n":
            events += assembler.feed(bytes((byte,)))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, payload)
        self.assertEqual(assembler.buffered_bytes, 0)

    def test_many_short_lines_in_one_chunk_assemble_as_one_frame(self):
        line_count = 8192
        assembler = SseEventAssembler()

        events = assembler.feed((b"data:x\n" * line_count) + b"\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].lines), line_count)
        self.assertEqual(events[0].data.count(b"\n"), line_count - 1)
        self.assertEqual(assembler.buffered_bytes, 0)


if __name__ == "__main__":
    unittest.main()
