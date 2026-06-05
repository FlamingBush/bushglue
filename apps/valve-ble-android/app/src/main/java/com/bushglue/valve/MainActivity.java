package com.bushglue.valve;

import android.Manifest;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothStatusCodes;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.ToggleButton;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

/**
 * Bush Valve — handheld BLE controller for the XIAO nRF52840 valve node.
 *
 * Talks the same newline-framed "<topic> <payload>" line protocol the firmware
 * (firmware/valve-control) speaks over a Nordic UART Service, so this exposes the
 * same controls as bush-monitor: target, home/stop, breath (amp/period/skew/on),
 * calibrate (open_steps), maxtorque, nudge — plus live actual/status telemetry.
 */
public class MainActivity extends Activity {

    static final UUID SVC  = UUID.fromString("6e400001-b5a3-f393-e0a9-e50e24dcca9e");
    static final UUID RX   = UUID.fromString("6e400002-b5a3-f393-e0a9-e50e24dcca9e"); // write
    static final UUID TX   = UUID.fromString("6e400003-b5a3-f393-e0a9-e50e24dcca9e"); // notify
    static final UUID CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    static final String DEVICE_NAME = "bushvalve";

    static final String T_TARGET = "bush/fire/valve/target";
    static final String T_HOME   = "bush/fire/valve/home";
    static final String T_STOP   = "bush/fire/valve/stop";
    static final String T_CALIB  = "bush/fire/valve/calibrate";
    static final String T_BREATH = "bush/fire/valve/breath";
    static final String T_MAXT   = "bush/fire/valve/maxtorque";
    static final String T_NUDGE  = "bush/fire/valve/nudge";
    static final String T_ACTUAL = "bush/fire/valve/actual";
    static final String T_STATUS = "bush/fire/valve/status";

    static final int REQ_PERMS = 1;

    final Handler ui = new Handler(Looper.getMainLooper());

    BluetoothAdapter adapter;
    BluetoothLeScanner scanner;
    BluetoothGatt gatt;
    BluetoothGattCharacteristic rxChar;
    volatile boolean ready = false;
    boolean scanning = false;

    final ArrayDeque<byte[]> writeQ = new ArrayDeque<>();
    boolean writing = false;
    final StringBuilder rxBuf = new StringBuilder();

    // UI
    TextView statusView, teleView;
    Button connectBtn;
    SeekBar targetBar; TextView targetLbl;
    ToggleButton breathEnabled;
    SeekBar ampBar, periodBar, skewBar; TextView ampLbl, periodLbl, skewLbl;
    EditText calibEdit, maxtEdit, nudgeEdit;
    long lastTargetSend = 0;

    // Music → valve
    static final int REQ_PICK = 2;
    static final String[] PRESETS = {"swell", "pulse", "drama"};
    Uri pickedAudio;
    String pickedName = "(none)";
    CueSheet currentSheet;
    MediaPlayer player;
    CuePlayer cuePlayer;
    volatile boolean playbackDone = false;
    int presetIdx = 1;
    EditText hostEdit, userEdit, passEdit;
    TextView musicStatus;
    Button presetBtn;

    // ── Lifecycle / UI ──────────────────────────────────────────────────────

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        BluetoothManager bm = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
        adapter = bm != null ? bm.getAdapter() : null;
        setContentView(buildUi());
        setStatus("disconnected");
        ensurePermissions();
    }

    View buildUi() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout root = col();
        int pad = dp(14);
        root.setPadding(pad, pad, pad, pad);

        statusView = new TextView(this);
        statusView.setTextSize(16f);
        root.addView(statusView);

        connectBtn = new Button(this);
        connectBtn.setText("Connect");
        connectBtn.setOnClickListener(v -> { if (ready || scanning) disconnect(); else startScan(); });
        root.addView(connectBtn);

        teleView = new TextView(this);
        teleView.setTextSize(13f);
        teleView.setPadding(0, dp(6), 0, dp(10));
        teleView.setText("telemetry: —");
        root.addView(teleView);

        // Target ───────────────────────────────
        root.addView(header("Target position"));
        targetLbl = new TextView(this);
        root.addView(targetLbl);
        targetBar = new SeekBar(this);
        targetBar.setMax(1000);
        targetBar.setProgress(0);
        targetBar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            public void onProgressChanged(SeekBar s, int p, boolean fromUser) {
                targetLbl.setText(String.format(Locale.US, "target = %.3f", p / 1000.0));
                if (fromUser) {
                    long now = System.currentTimeMillis();
                    if (now - lastTargetSend >= 100) { lastTargetSend = now; sendTarget(p); }
                }
            }
            public void onStartTrackingTouch(SeekBar s) {}
            public void onStopTrackingTouch(SeekBar s) { sendTarget(s.getProgress()); }
        });
        targetLbl.setText("target = 0.000");
        root.addView(targetBar);

        LinearLayout hs = row();
        Button homeBtn = new Button(this); homeBtn.setText("Home");
        homeBtn.setOnClickListener(v -> sendLine(T_HOME, ""));
        Button stopBtn = new Button(this); stopBtn.setText("STOP");
        stopBtn.setOnClickListener(v -> sendLine(T_STOP, ""));
        addWeighted(hs, homeBtn); addWeighted(hs, stopBtn);
        root.addView(hs);

        // Breath ───────────────────────────────
        root.addView(header("Breath"));
        breathEnabled = new ToggleButton(this);
        breathEnabled.setTextOn("breath ENABLED"); breathEnabled.setTextOff("breath disabled");
        breathEnabled.setChecked(true);
        root.addView(breathEnabled);

        ampLbl = new TextView(this); root.addView(ampLbl);
        ampBar = new SeekBar(this); ampBar.setMax(500); ampBar.setProgress(40); // 0.000..0.500
        ampBar.setOnSeekBarChangeListener(simpleLabel(() ->
            ampLbl.setText(String.format(Locale.US, "amplitude = %.3f", ampBar.getProgress() / 1000.0))));
        ampLbl.setText("amplitude = 0.040");
        root.addView(ampBar);

        periodLbl = new TextView(this); root.addView(periodLbl);
        periodBar = new SeekBar(this); periodBar.setMax(600); periodBar.setProgress(50); // *100 ms
        periodBar.setOnSeekBarChangeListener(simpleLabel(() ->
            periodLbl.setText("period = " + periodMs() + " ms")));
        periodLbl.setText("period = 5000 ms");
        root.addView(periodBar);

        skewLbl = new TextView(this); root.addView(skewLbl);
        skewBar = new SeekBar(this); skewBar.setMax(100); skewBar.setProgress(50); // /100, clamp
        skewBar.setOnSeekBarChangeListener(simpleLabel(() ->
            skewLbl.setText(String.format(Locale.US, "skew = %.2f", skewVal()))));
        skewLbl.setText("skew = 0.50");
        root.addView(skewBar);

        Button applyBreath = new Button(this);
        applyBreath.setText("Apply breath");
        applyBreath.setOnClickListener(v -> sendBreath());
        root.addView(applyBreath);

        // Calibrate / MaxTorque / Nudge ────────
        root.addView(header("Calibrate open_steps"));
        calibEdit = numberField("16000");
        root.addView(fieldWithSet(calibEdit, "Set", () -> {
            String s = calibEdit.getText().toString().trim();
            if (!s.isEmpty()) sendLine(T_CALIB, s);
        }));

        root.addView(header("Max torque (0–1200)"));
        maxtEdit = numberField("1200");
        root.addView(fieldWithSet(maxtEdit, "Set", () -> {
            String s = maxtEdit.getText().toString().trim();
            if (!s.isEmpty()) sendLine(T_MAXT, s);
        }));

        root.addView(header("Nudge (degrees)"));
        nudgeEdit = numberField("5");
        LinearLayout nrow = row();
        Button nClose = new Button(this); nClose.setText("Nudge + (close)");
        nClose.setOnClickListener(v -> nudge(+1));
        Button nOpen = new Button(this); nOpen.setText("Nudge − (open)");
        nOpen.setOnClickListener(v -> nudge(-1));
        addWeighted(nrow, nClose); addWeighted(nrow, nOpen);
        root.addView(nudgeEdit);
        root.addView(nrow);

        buildMusicSection(root);

        scroll.addView(root);
        return scroll;
    }

    // ── Commands ────────────────────────────────────────────────────────────

    void sendTarget(int milli) {
        sendLine(T_TARGET, String.format(Locale.US, "%.3f", milli / 1000.0));
    }

    int periodMs() { return Math.max(100, periodBar.getProgress() * 100); }
    double skewVal() { return Math.max(0.05, Math.min(0.95, skewBar.getProgress() / 100.0)); }

    void sendBreath() {
        try {
            JSONObject j = new JSONObject();
            j.put("amplitude", ampBar.getProgress() / 1000.0);
            j.put("period_ms", periodMs());
            j.put("skew", Math.round(skewVal() * 100.0) / 100.0);
            j.put("enabled", breathEnabled.isChecked());
            sendLine(T_BREATH, j.toString());
        } catch (Exception e) { toast("breath json error"); }
    }

    void nudge(int sign) {
        String s = nudgeEdit.getText().toString().trim();
        if (s.isEmpty()) return;
        try {
            int deg = Integer.parseInt(s);
            sendLine(T_NUDGE, String.valueOf(sign * Math.abs(deg)));
        } catch (NumberFormatException e) { toast("bad nudge value"); }
    }

    void sendLine(String topic, String payload) {
        if (!ready || gatt == null || rxChar == null) { toast("not connected"); return; }
        String line = payload.isEmpty() ? topic : topic + " " + payload;
        byte[] data = (line + "\n").getBytes(StandardCharsets.UTF_8);
        synchronized (writeQ) {
            for (int i = 0; i < data.length; i += 20) {
                writeQ.add(Arrays.copyOfRange(data, i, Math.min(i + 20, data.length)));
            }
            drainLocked();
        }
    }

    void drainLocked() {
        if (writing || !ready || writeQ.isEmpty()) return;
        byte[] chunk = writeQ.peek();
        writing = true;
        if (doWrite(chunk)) {
            writeQ.poll();
        } else {
            writing = false; // leave queued; next send retries
        }
    }

    @SuppressWarnings({"deprecation", "MissingPermission"})
    boolean doWrite(byte[] chunk) {
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                return gatt.writeCharacteristic(rxChar, chunk,
                        BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothStatusCodes.SUCCESS;
            } else {
                rxChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
                rxChar.setValue(chunk);
                return gatt.writeCharacteristic(rxChar);
            }
        } catch (SecurityException e) {
            return false;
        }
    }

    // ── Scan / connect ──────────────────────────────────────────────────────

    @SuppressWarnings("MissingPermission")
    void startScan() {
        if (adapter == null) { toast("no Bluetooth adapter"); return; }
        if (!hasScanPerms()) { ensurePermissions(); return; }
        if (!adapter.isEnabled()) {
            startActivity(new Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE));
            return;
        }
        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) { toast("LE scanner unavailable"); return; }
        ScanSettings settings = new ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        scanning = true;
        setStatus("scanning for " + DEVICE_NAME + "…");
        connectBtn.setText("Cancel");
        try {
            // Unfiltered: a 128-bit service UUID may not fit the 31-byte advert alongside
            // the name, so we match by name OR UUID in the callback instead of a hard filter.
            scanner.startScan(null, settings, scanCb);
        } catch (SecurityException e) { toast("scan permission denied"); scanning = false; return; }
        ui.postDelayed(() -> {
            if (scanning && !ready) { stopScan(); setStatus("no valve found — tap Connect to retry"); connectBtn.setText("Connect"); }
        }, 12000);
    }

    @SuppressWarnings("MissingPermission")
    void stopScan() {
        scanning = false;
        try { if (scanner != null) scanner.stopScan(scanCb); } catch (SecurityException ignored) {}
    }

    final ScanCallback scanCb = new ScanCallback() {
        @Override public void onScanResult(int type, ScanResult result) {
            if (!scanning) return;
            android.bluetooth.le.ScanRecord rec = result.getScanRecord();
            String advName = rec != null ? rec.getDeviceName() : null;
            String devName = null;
            try { devName = result.getDevice().getName(); } catch (SecurityException ignored) {}
            boolean nameMatch = DEVICE_NAME.equals(advName) || DEVICE_NAME.equals(devName);
            boolean uuidMatch = rec != null && rec.getServiceUuids() != null
                    && rec.getServiceUuids().contains(new ParcelUuid(SVC));
            android.util.Log.d("bushvalve", "scan " + result.getDevice().getAddress()
                    + " name=" + advName + "/" + devName
                    + " uuids=" + (rec != null ? rec.getServiceUuids() : "null"));
            if (!nameMatch && !uuidMatch) return;
            stopScan();
            BluetoothDevice dev = result.getDevice();
            ui.post(() -> setStatus("connecting…"));
            connect(dev);
        }
        @Override public void onScanFailed(int errorCode) {
            ui.post(() -> { scanning = false; setStatus("scan failed (" + errorCode + ")"); connectBtn.setText("Connect"); });
        }
    };

    @SuppressWarnings("MissingPermission")
    void connect(BluetoothDevice dev) {
        try {
            gatt = dev.connectGatt(this, false, gattCb, BluetoothDevice.TRANSPORT_LE);
        } catch (SecurityException e) { toast("connect permission denied"); }
    }

    @SuppressWarnings("MissingPermission")
    void disconnect() {
        stopScan();
        ready = false;
        synchronized (writeQ) { writeQ.clear(); writing = false; }
        try { if (gatt != null) { gatt.disconnect(); gatt.close(); } } catch (SecurityException ignored) {}
        gatt = null; rxChar = null;
        setStatus("disconnected");
        connectBtn.setText("Connect");
    }

    final BluetoothGattCallback gattCb = new BluetoothGattCallback() {
        @Override @SuppressWarnings("MissingPermission")
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (newState == BluetoothGatt.STATE_CONNECTED) {
                try { g.discoverServices(); } catch (SecurityException ignored) {}
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                ready = false;
                ui.post(() -> { setStatus("disconnected"); connectBtn.setText("Connect"); });
            }
        }

        @Override @SuppressWarnings("MissingPermission")
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            BluetoothGattService svc = g.getService(SVC);
            if (svc == null) { ui.post(() -> setStatus("NUS service not found")); return; }
            rxChar = svc.getCharacteristic(RX);
            BluetoothGattCharacteristic txChar = svc.getCharacteristic(TX);
            if (rxChar == null || txChar == null) { ui.post(() -> setStatus("NUS chars not found")); return; }
            try {
                g.setCharacteristicNotification(txChar, true);
                BluetoothGattDescriptor cccd = txChar.getDescriptor(CCCD);
                if (cccd != null) {
                    if (Build.VERSION.SDK_INT >= 33) {
                        g.writeDescriptor(cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                    } else {
                        cccd.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                        g.writeDescriptor(cccd);
                    }
                }
            } catch (SecurityException ignored) {}
            try { g.requestMtu(185); } catch (SecurityException ignored) {}  // bigger writes for streaming
            ready = true;
            ui.post(() -> { setStatus("connected to " + DEVICE_NAME); connectBtn.setText("Disconnect"); });
        }

        // API <33
        @Override @SuppressWarnings("deprecation")
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c) {
            if (TX.equals(c.getUuid())) onRx(c.getValue());
        }
        // API 33+
        @Override
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value) {
            if (TX.equals(c.getUuid())) onRx(value);
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            synchronized (writeQ) { writing = false; drainLocked(); }
        }

        @Override
        public void onMtuChanged(BluetoothGatt g, int mtu, int status) {
            // A larger MTU just means fewer 20-byte chunks; the writer is correct either way.
        }
    };

    // ── Telemetry ───────────────────────────────────────────────────────────

    void onRx(byte[] value) {
        if (value == null || value.length == 0) return;
        synchronized (rxBuf) {
            rxBuf.append(new String(value, StandardCharsets.UTF_8));
            int nl;
            while ((nl = rxBuf.indexOf("\n")) >= 0) {
                String line = rxBuf.substring(0, nl).trim();
                rxBuf.delete(0, nl + 1);
                if (!line.isEmpty()) handleTelemetry(line);
            }
        }
    }

    String lastActual = "—", lastStatus = "—";

    void handleTelemetry(String line) {
        int sp = line.indexOf(' ');
        String topic = sp < 0 ? line : line.substring(0, sp);
        String payload = sp < 0 ? "" : line.substring(sp + 1);
        if (T_ACTUAL.equals(topic)) {
            lastActual = payload;
        } else if (T_STATUS.equals(topic)) {
            try {
                JSONObject j = new JSONObject(payload);
                lastStatus = String.format(Locale.US, "state=%s  homed=%s  pos=%s  target=%s%s",
                        j.optString("state", "?"), j.optBoolean("homed", false),
                        j.opt("pos"), j.opt("target"),
                        j.isNull("last_error") ? "" : "  err=" + j.optString("last_error"));
            } catch (Exception e) { lastStatus = payload; }
        } else {
            return;
        }
        ui.post(() -> teleView.setText("actual = " + lastActual + "\n" + lastStatus));
    }

    // ── Permissions ─────────────────────────────────────────────────────────

    boolean hasScanPerms() {
        if (Build.VERSION.SDK_INT >= 31) {
            return granted(Manifest.permission.BLUETOOTH_SCAN) && granted(Manifest.permission.BLUETOOTH_CONNECT);
        }
        return granted(Manifest.permission.ACCESS_FINE_LOCATION);
    }

    boolean granted(String p) {
        return checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED;
    }

    void ensurePermissions() {
        if (hasScanPerms()) return;
        String[] perms = Build.VERSION.SDK_INT >= 31
                ? new String[]{Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT}
                : new String[]{Manifest.permission.ACCESS_FINE_LOCATION};
        requestPermissions(perms, REQ_PERMS);
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] results) {
        if (req == REQ_PERMS && !hasScanPerms()) toast("Bluetooth permission needed to scan");
    }

    // ── Music → valve ───────────────────────────────────────────────────────

    void buildMusicSection(LinearLayout root) {
        root.addView(header("Music → valve"));
        musicStatus = new TextView(this);
        musicStatus.setTextSize(13f);
        musicStatus.setText("track: (none)");
        root.addView(musicStatus);

        Button pick = new Button(this);
        pick.setText("Pick track");
        pick.setOnClickListener(v -> pickTrack());
        root.addView(pick);

        presetBtn = new Button(this);
        presetBtn.setText("Preset: " + PRESETS[presetIdx]);
        presetBtn.setOnClickListener(v -> {
            presetIdx = (presetIdx + 1) % PRESETS.length;
            presetBtn.setText("Preset: " + PRESETS[presetIdx]);
        });
        root.addView(presetBtn);

        root.addView(musicLabel("odroid host (ssh)"));
        hostEdit = new EditText(this);
        hostEdit.setText("odroid-local");
        root.addView(hostEdit);
        root.addView(musicLabel("ssh user"));
        userEdit = new EditText(this);
        userEdit.setText("odroid");
        root.addView(userEdit);
        root.addView(musicLabel("ssh password"));
        passEdit = new EditText(this);
        passEdit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        root.addView(passEdit);

        Button convert = new Button(this);
        convert.setText("Convert on odroid");
        convert.setOnClickListener(v -> doConvert());
        root.addView(convert);

        LinearLayout pr = row();
        Button play = new Button(this);
        play.setText("Play");
        play.setOnClickListener(v -> doPlay());
        Button stopM = new Button(this);
        stopM.setText("Stop");
        stopM.setOnClickListener(v -> doStop());
        addWeighted(pr, play);
        addWeighted(pr, stopM);
        root.addView(pr);
    }

    TextView musicLabel(String t) {
        TextView tv = new TextView(this);
        tv.setText(t);
        tv.setPadding(0, dp(6), 0, 0);
        return tv;
    }

    /** Binary write path for stream frames: chunk to 20 B (no newline), reuse the queue. */
    void sendBytes(byte[] data) {
        if (!ready || gatt == null || rxChar == null) return;
        synchronized (writeQ) {
            for (int i = 0; i < data.length; i += 20) {
                writeQ.add(Arrays.copyOfRange(data, i, Math.min(i + 20, data.length)));
            }
            drainLocked();
        }
    }

    void pickTrack() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("audio/*");
        try {
            startActivityForResult(i, REQ_PICK);
        } catch (Exception e) {
            toast("no file picker available");
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req == REQ_PICK && res == RESULT_OK && data != null && data.getData() != null) {
            pickedAudio = data.getData();
            pickedName = pickedAudio.getLastPathSegment();
            currentSheet = null;
            ui.post(() -> musicStatus.setText("track: " + pickedName + " (not converted)"));
        }
    }

    void doConvert() {
        if (pickedAudio == null) { toast("pick a track first"); return; }
        final String host = hostEdit.getText().toString().trim();
        final String user = userEdit.getText().toString().trim();
        final String pass = passEdit.getText().toString();
        final String preset = PRESETS[presetIdx];
        ui.post(() -> musicStatus.setText("converting on " + host + " (fallback odroid)…"));
        new Thread(() -> {
            try {
                byte[] audio = readAll(pickedAudio);
                String cmd = "~/bushglue/.venv/bin/bush-cue analyze - --preset " + preset + " -o -";
                String json = SshConvert.convert(host, "odroid", user, pass, cmd, audio);
                final CueSheet sheet = CueSheet.parse(json);
                currentSheet = sheet;
                ui.post(() -> musicStatus.setText(String.format(Locale.US,
                        "ready: %s · %s · %.0fs · %d samples @ %dHz",
                        pickedName, sheet.preset, sheet.durationS, sheet.posU8.length, sheet.rateHz)));
            } catch (Exception e) {
                ui.post(() -> musicStatus.setText("convert failed: " + e.getMessage()));
            }
        }, "ssh-convert").start();
    }

    byte[] readAll(Uri uri) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (InputStream in = getContentResolver().openInputStream(uri)) {
            if (in == null) throw new Exception("cannot open track");
            byte[] buf = new byte[16384];
            int r;
            while ((r = in.read(buf)) > 0) out.write(buf, 0, r);
        }
        return out.toByteArray();
    }

    void doPlay() {
        if (currentSheet == null) { toast("convert a track first"); return; }
        if (!ready) { toast("connect to the valve first"); return; }
        doStop();
        try {
            player = new MediaPlayer();
            player.setDataSource(this, pickedAudio);
            player.setOnCompletionListener(mp -> playbackDone = true);
            player.prepare();
            playbackDone = false;
            player.start();
        } catch (Exception e) {
            toast("audio error: " + e.getMessage());
            return;
        }
        CuePlayer.Clock clock = new CuePlayer.Clock() {
            public long playheadMs() {
                try { return player != null ? player.getCurrentPosition() : 0; }
                catch (Exception e) { return 0; }
            }
            public boolean finished() { return playbackDone; }
        };
        cuePlayer = new CuePlayer(currentSheet, this::sendBytes, clock, 60);
        cuePlayer.start();
        ui.post(() -> musicStatus.setText("playing: " + pickedName));
    }

    void doStop() {
        if (cuePlayer != null) { cuePlayer.stop(); cuePlayer = null; }
        if (player != null) {
            try { player.stop(); } catch (Exception ignored) {}
            try { player.release(); } catch (Exception ignored) {}
            player = null;
        }
        sendBytes(Wire.stop());
    }

    // ── Small UI helpers ────────────────────────────────────────────────────

    void setStatus(String s) { statusView.setText("● " + s); }
    void toast(String s) { ui.post(() -> Toast.makeText(this, s, Toast.LENGTH_SHORT).show()); }
    int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    LinearLayout col() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        return l;
    }

    LinearLayout row() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.HORIZONTAL);
        return l;
    }

    void addWeighted(LinearLayout rowLayout, View v) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        rowLayout.addView(v, lp);
    }

    TextView header(String t) {
        TextView tv = new TextView(this);
        tv.setText(t);
        tv.setTextColor(Color.parseColor("#1565C0"));
        tv.setTextSize(15f);
        tv.setPadding(0, dp(14), 0, dp(2));
        return tv;
    }

    EditText numberField(String def) {
        EditText e = new EditText(this);
        e.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_SIGNED);
        e.setText(def);
        return e;
    }

    LinearLayout fieldWithSet(EditText field, String btn, Runnable onSet) {
        LinearLayout r = row();
        r.setGravity(Gravity.CENTER_VERTICAL);
        addWeighted(r, field);
        Button b = new Button(this);
        b.setText(btn);
        b.setOnClickListener(v -> onSet.run());
        r.addView(b);
        return r;
    }

    interface OnChange { void run(); }

    SeekBar.OnSeekBarChangeListener simpleLabel(OnChange onChange) {
        return new SeekBar.OnSeekBarChangeListener() {
            public void onProgressChanged(SeekBar s, int p, boolean fromUser) { onChange.run(); }
            public void onStartTrackingTouch(SeekBar s) {}
            public void onStopTrackingTouch(SeekBar s) {}
        };
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        doStop();
        disconnect();
    }
}
