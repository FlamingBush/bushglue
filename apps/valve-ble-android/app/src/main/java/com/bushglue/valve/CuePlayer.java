package com.bushglue.valve;

/**
 * Streams a valve position waveform AHEAD of a playhead clock over BLE. The firmware
 * buffers the samples and executes each on its own clock, so BLE jitter doesn't move
 * motion timing -- this just keeps the buffer filled to a lookahead depth and lets the
 * firmware play. Valve-only (no flame on BLE).
 */
final class CuePlayer {

    interface Writer { void writeStream(byte[] frame); }

    interface Clock {
        long playheadMs();   // current audio position
        boolean finished();  // playback ended/stopped
    }

    private static final long LOOKAHEAD_MS = 1200;

    private final CueSheet sheet;
    private final Writer writer;
    private final Clock clock;
    private final int latencyLeadMs;
    private volatile boolean running = false;
    private Thread thread;

    CuePlayer(CueSheet sheet, Writer writer, Clock clock, int latencyLeadMs) {
        this.sheet = sheet;
        this.writer = writer;
        this.clock = clock;
        this.latencyLeadMs = latencyLeadMs;
    }

    void start() {
        running = true;
        thread = new Thread(this::run, "cue-player");
        thread.start();
    }

    void stop() {
        running = false;
        if (thread != null) thread.interrupt();
    }

    private void run() {
        int rate = sheet.rateHz;
        int n = sheet.posU8.length;
        int batch = Math.max(1, (int) (0.4 * rate));     // ~0.4 s of samples per frame
        long endMs = (long) (n / (double) rate * 1000);
        writer.writeStream(Wire.start(rate, latencyLeadMs));
        int sent = 0;
        try {
            while (running) {
                long ph = clock.playheadMs();
                if (clock.finished() || ph > endMs + 500) break;
                int want = (int) Math.min(n, ((ph + LOOKAHEAD_MS) * rate) / 1000);
                while (sent < want) {
                    int m = Math.min(batch, want - sent);
                    writer.writeStream(Wire.samples(sent, sheet.posU8, sent, m));
                    sent += m;
                }
                Thread.sleep(20);
            }
        } catch (InterruptedException ignored) {
        } finally {
            try { writer.writeStream(Wire.stop()); } catch (Exception ignored) {}
        }
    }
}
