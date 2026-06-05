package com.bushglue.valve;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * A parsed cue sheet (produced by `bush-cue analyze` on the odroid). Android plays
 * the continuous valve waveform only -- flame is odroid-only (MQTT to the Pico), not
 * reachable over the BLE valve node.
 */
final class CueSheet {
    final int rateHz;
    final int[] posU8;       // valve position per sample, quantized 0..255 (0..1 travel)
    final double durationS;
    final String preset;

    private CueSheet(int rateHz, int[] posU8, double durationS, String preset) {
        this.rateHz = rateHz;
        this.posU8 = posU8;
        this.durationS = durationS;
        this.preset = preset;
    }

    static CueSheet parse(String json) throws Exception {
        JSONObject d = new JSONObject(json);
        if (d.optInt("version", 0) != 1) throw new Exception("unsupported cue sheet version");
        JSONObject v = d.getJSONObject("valve");
        int rate = v.getInt("rate_hz");
        JSONArray pos = v.getJSONArray("pos");
        int[] q = new int[pos.length()];
        for (int i = 0; i < q.length; i++) {
            int u = (int) Math.round(pos.getDouble(i) * 255.0);
            q[i] = u < 0 ? 0 : (u > 255 ? 255 : u);
        }
        double dur = d.optDouble("duration_s", q.length / (double) rate);
        return new CueSheet(rate, q, dur, d.optString("preset", "?"));
    }
}
