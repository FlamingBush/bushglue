package com.bushglue.valve;

/**
 * Binary BLE stream frames for valve waveform playback. Mirrors the Python encoder
 * in services/audio/src/bush_cue/wire.py and the firmware decoder (valve.handle_stream).
 *
 *   frame = SENTINEL(1) TYPE(1) LEN(2 BE) PAYLOAD(LEN) CRC(1 = sum(prev) & 0xFF)
 *
 * The sentinel (0xF5) never starts a text topic line, so the firmware can tell the
 * two framings apart on one byte stream.
 */
final class Wire {
    static final int SENTINEL = 0xF5;
    static final int T_START = 0x01, T_SAMPLES = 0x02, T_STOP = 0x03, T_PING = 0x05;

    private Wire() {}

    private static byte[] frame(int type, byte[] payload) {
        int n = payload.length;
        byte[] f = new byte[4 + n + 1];
        f[0] = (byte) SENTINEL;
        f[1] = (byte) type;
        f[2] = (byte) ((n >> 8) & 0xFF);
        f[3] = (byte) (n & 0xFF);
        System.arraycopy(payload, 0, f, 4, n);
        int sum = 0;
        for (int i = 0; i < 4 + n; i++) sum += (f[i] & 0xFF);
        f[4 + n] = (byte) (sum & 0xFF);
        return f;
    }

    static byte[] start(int rateHz, int basePlayMs) {
        byte[] p = new byte[6];
        p[0] = (byte) ((rateHz >> 8) & 0xFF);
        p[1] = (byte) (rateHz & 0xFF);
        p[2] = (byte) ((basePlayMs >> 24) & 0xFF);
        p[3] = (byte) ((basePlayMs >> 16) & 0xFF);
        p[4] = (byte) ((basePlayMs >> 8) & 0xFF);
        p[5] = (byte) (basePlayMs & 0xFF);
        return frame(T_START, p);
    }

    /** Pack posU8[off .. off+len) at global sample index startIndex. */
    static byte[] samples(int startIndex, int[] posU8, int off, int len) {
        byte[] p = new byte[6 + len];
        p[0] = (byte) ((startIndex >> 24) & 0xFF);
        p[1] = (byte) ((startIndex >> 16) & 0xFF);
        p[2] = (byte) ((startIndex >> 8) & 0xFF);
        p[3] = (byte) (startIndex & 0xFF);
        p[4] = (byte) ((len >> 8) & 0xFF);
        p[5] = (byte) (len & 0xFF);
        for (int i = 0; i < len; i++) p[6 + i] = (byte) (posU8[off + i] & 0xFF);
        return frame(T_SAMPLES, p);
    }

    static byte[] stop() {
        return frame(T_STOP, new byte[0]);
    }

    static byte[] ping(int token) {
        return frame(T_PING, new byte[]{(byte) ((token >> 8) & 0xFF), (byte) (token & 0xFF)});
    }
}
