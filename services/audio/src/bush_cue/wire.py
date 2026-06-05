"""Binary BLE frame format for streamed valve playback.

The valve firmware's line protocol is newline-framed UTF-8 ("bush/... payload\\n").
Stream frames are distinguished by a sentinel first byte (0xF5, never the start of
a text line, which begins with 'b'=0x62). The firmware peeks the first RX byte and
takes the binary path when it sees the sentinel, else assembles a text line.

  frame = SENTINEL(1) TYPE(1) LEN(2 BE) PAYLOAD(LEN) CRC(1 = sum(prev) & 0xFF)

Uplink (firmware -> host) stays text: `bush/fire/valve/pong <token> <ticks_ms>` and
`bush/fire/valve/streampos <play_ms> <pos>` ride the existing telemetry path.

Positions are u8 = round(pos * 255) over the absolute 0..1 valve travel (already
clamped to [pos_min,pos_max] in the sheet). Samples are globally indexed at the
START rate, so a batch is self-locating: sample i plays at i * 1000 / rate ms.
"""
from __future__ import annotations

SENTINEL = 0xF5
T_START = 0x01    # rate_hz(u16) base_play_ms(u32): set epoch, clear buffer
T_SAMPLES = 0x02  # start_index(u32) count(u16) positions(u8 * count)
T_STOP = 0x03     # empty: flush + hold
T_PING = 0x05     # token(u16): firmware replies pong


def _frame(ftype: int, payload: bytes) -> bytes:
    body = bytes([SENTINEL, ftype]) + len(payload).to_bytes(2, "big") + payload
    return body + bytes([sum(body) & 0xFF])


def start(rate_hz: int, base_play_ms: int) -> bytes:
    return _frame(T_START, rate_hz.to_bytes(2, "big") + base_play_ms.to_bytes(4, "big"))


def samples(start_index: int, positions_u8: list[int]) -> bytes:
    p = bytes(max(0, min(255, int(v))) for v in positions_u8)
    return _frame(T_SAMPLES, start_index.to_bytes(4, "big") + len(p).to_bytes(2, "big") + p)


def stop() -> bytes:
    return _frame(T_STOP, b"")


def ping(token: int) -> bytes:
    return _frame(T_PING, (token & 0xFFFF).to_bytes(2, "big"))


def quantize(pos_fracs: list[float]) -> list[int]:
    return [max(0, min(255, int(round(p * 255)))) for p in pos_fracs]
