"""Incremental, bounded-memory parsing of streamed archive submissions.

When ``stream=1`` is used on ``POST /runs`` (and its aliases / the ``PUT``
routes) the request body is a single JSON *array of segments*::

    [
      [[t, v], [t, v], ...],   # segment 0: non-empty array of point pairs
      [[t, v], [t, v], ...]    # segment 1; gaps between segments are preserved
    ]

Each point is exactly a two-element ``[integer milliseconds, integer
temperature]`` pair -- the streaming form is strict on purpose, so that no
tolerant-input parsing ever needs to look at the whole body.  Times must
strictly increase inside one segment; segments are independent, so no ordering
is required across them.

The parser below is a hand-rolled incremental state machine.  It consumes
arbitrary byte chunks (including mid-integer and mid-token boundaries) and
hands each completed segment to a callback.  Its working memory is one
in-flight segment plus a small carry buffer, independent of how many segments
or points the whole body contains.

Two error kinds are distinguished:

* :class:`StreamTruncated` -- the transport ended before the body was closed;
* :class:`StreamInputError` -- any malformed or semantically invalid body.

Both are reported to the client as input errors (HTTP 422); keeping them
separate lets the endpoint distinguish a broken upload from a bad document.
"""

from __future__ import annotations

from typing import Callable, Sequence

__all__ = [
    "StreamInputError",
    "StreamTruncated",
    "StreamingBodyParser",
]


class StreamInputError(ValueError):
    """The streamed body is valid-ish JSON but fails the strict input rules."""


class StreamTruncated(StreamInputError):
    """The connection ended while more bytes were needed to close the body."""


# Parser states.  Times and temperatures are read by the same integer rule, so
# the state machine only needs to remember which slot of the pair it is in.
_S_OUTER_OPEN = 0       # expect '[' opening the whole body
_S_SEGMENT_OPEN = 1     # expect '[' opening a segment
_S_POINT_OPEN = 2       # expect '[' opening a point pair
_S_TIME = 3             # expect the millisecond integer
_S_COMMA_AFTER_TIME = 4  # expect the ',' inside a point
_S_VALUE = 5            # expect the temperature integer
_S_POINT_CLOSE = 6      # expect ']' closing a point
_S_AFTER_POINT = 7      # expect ',' (more points) or ']' (segment ends)
_S_AFTER_SEGMENT = 8    # expect ',' (more segments) or ']' (body ends)
_S_DONE = 9             # body closed; only trailing whitespace is allowed

_WHITESPACE = b" \t\r\n"
# Enough room for a sign and any integer that fits in a normal Python int;
# the carry only ever spans one token, never a segment.
_TOKEN_LIMIT = 4096


class StreamingBodyParser:
    """Parse a chunked JSON array-of-segments body incrementally.

    Parameters
    ----------
    on_segment:
        Called once per fully received segment with the list of
        ``(milliseconds, temperature)`` integer pairs, in arrival order.
    """

    def __init__(
        self, on_segment: Callable[[Sequence[tuple[int, int]]], None]
    ) -> None:
        self._on_segment = on_segment
        self._buffer = bytearray()
        self._pos = 0
        self._state = _S_OUTER_OPEN
        self._segment: list[tuple[int, int]] = []
        self._segment_index = 0
        self._point_index = 0
        self._last_time: int | None = None
        self._pending_time: int | None = None

    # -- public feed API ----------------------------------------------

    def feed(self, chunk: bytes) -> None:
        """Consume one network chunk. May invoke the segment callback."""
        if not isinstance(chunk, (bytes, bytearray)):  # pragma: no cover - defensive
            raise TypeError("stream chunks must be bytes")
        if not chunk:
            return
        self._buffer.extend(chunk)
        self._drive()

    def finish(self) -> None:
        """Assert the body was fully consumed after the last chunk."""
        self._drive()
        if self._state is not _S_DONE:
            raise StreamTruncated("stream ended before the JSON body was closed")
        self._compact()
        if self._pos < len(self._buffer):
            raise StreamInputError("unexpected trailing data after the segment array")

    # -- low-level buffer helpers -------------------------------------

    def _compact(self) -> None:
        """Drop bytes already consumed so the carry never grows with the body."""
        if self._pos:
            del self._buffer[: self._pos]
            self._pos = 0

    def _peek(self) -> int:
        return self._buffer[self._pos]

    def _advance(self) -> None:
        self._pos += 1

    def _skip_ws(self) -> None:
        buf = self._buffer
        while self._pos < len(buf) and buf[self._pos] in _WHITESPACE:
            self._pos += 1

    def _fail(self, message: str) -> None:
        raise StreamInputError(
            f"{message} (segment {self._segment_index}, point {self._point_index})"
        )

    def _read_integer(self) -> int | None:
        """Read one JSON integer at the buffer position, or None if cut off.

        Grammar: ``-?(0 | [1-9][0-9]*)`` -- JSON integers only, no fractions,
        exponents, leading zeros or ``true``/``false``/``null``.
        """
        start = self._pos
        buf = self._buffer
        negative = False
        if self._pos < len(buf) and buf[self._pos] == 0x2D:  # '-'
            negative = True
            self._pos += 1
        if self._pos >= len(buf):
            self._pos = start
            return None
        first = buf[self._pos]
        if first == 0x30:  # '0' must stand alone
            self._pos += 1
            if self._pos >= len(buf):
                self._pos = start
                return None
            if 0x30 <= buf[self._pos] <= 0x39:
                self._fail("integer must not have leading zeros")
        elif 0x31 <= first <= 0x39:  # [1-9], then any digits
            while self._pos < len(buf) and 0x30 <= buf[self._pos] <= 0x39:
                self._pos += 1
            if self._pos >= len(buf):
                # Still digits coming (or the token is genuinely cut off):
                # wait for more bytes, but cap the carry to avoid unbounded use.
                if self._pos - start >= _TOKEN_LIMIT:
                    self._fail("integer literal is too long")
                self._pos = start
                return None
        else:
            self._fail("expected an integer")

        text = bytes(buf[start:self._pos])
        try:
            value = int(text)
        except ValueError as exc:  # pragma: no cover - grammar already checked
            self._fail(f"invalid integer {text!r}")
            raise StreamInputError(str(exc)) from exc
        return value

    # -- state machine -------------------------------------------------

    def _drive(self) -> None:
        while True:
            self._skip_ws()
            if self._pos >= len(self._buffer):
                self._compact()
                return
            state = self._state
            byte = self._peek()

            if state == _S_OUTER_OPEN:
                if byte != 0x5B:  # '['
                    self._fail("stream body must be a JSON array of segments")
                self._advance()
                self._state = _S_SEGMENT_OPEN
                continue

            if state == _S_SEGMENT_OPEN:
                if byte != 0x5B:
                    self._fail("each segment must be a JSON array of point pairs")
                self._advance()
                self._segment = []
                self._point_index = 0
                self._last_time = None
                self._state = _S_POINT_OPEN
                continue

            if state == _S_POINT_OPEN:
                if byte != 0x5B:
                    self._fail("each point must be a [milliseconds, temperature] pair")
                self._advance()
                self._state = _S_TIME
                continue

            if state == _S_TIME:
                number = self._read_integer()
                if number is None:
                    self._compact()
                    return
                self._pending_time = number
                self._state = _S_COMMA_AFTER_TIME
                continue

            if state == _S_COMMA_AFTER_TIME:
                if byte != 0x2C:  # ','
                    self._fail("a point pair needs exactly two values separated by ','")
                self._advance()
                self._state = _S_VALUE
                continue

            if state == _S_VALUE:
                number = self._read_integer()
                if number is None:
                    self._compact()
                    return
                assert self._pending_time is not None
                time_ms = self._pending_time
                if self._last_time is not None and time_ms <= self._last_time:
                    self._fail(
                        "sample times must strictly increase within a segment "
                        f"(got {time_ms} after {self._last_time})"
                    )
                self._last_time = time_ms
                self._segment.append((time_ms, number))
                self._point_index += 1
                self._pending_time = None
                self._state = _S_POINT_CLOSE
                continue

            if state == _S_POINT_CLOSE:
                if byte != 0x5D:  # ']'
                    self._fail("a point pair must contain exactly two integers")
                self._advance()
                self._state = _S_AFTER_POINT
                continue

            if state == _S_AFTER_POINT:
                if byte == 0x2C:  # ',' -> another point in this segment
                    self._advance()
                    self._state = _S_POINT_OPEN
                    continue
                if byte == 0x5D:  # ']' -> segment ends
                    self._advance()
                    if not self._segment:
                        self._fail("each segment must contain at least one point")
                    segment = self._segment
                    self._segment = []
                    self._on_segment(segment)
                    self._segment_index += 1
                    self._state = _S_AFTER_SEGMENT
                    continue
                self._fail("expected ',' between points or ']' to close the segment")

            if state == _S_AFTER_SEGMENT:
                if byte == 0x2C:  # ',' -> another segment
                    self._advance()
                    self._state = _S_SEGMENT_OPEN
                    continue
                if byte == 0x5D:  # ']' -> whole body ends
                    self._advance()
                    if self._segment_index == 0:
                        self._fail("at least one non-empty segment is required")
                    self._state = _S_DONE
                    continue
                self._fail("expected ',' between segments or ']' to close the body")

            if state == _S_DONE:
                # Whitespace is consumed at loop top; anything else is junk.
                self._fail("unexpected trailing data after the segment array")

            raise AssertionError(f"unknown parser state {state}")  # pragma: no cover
