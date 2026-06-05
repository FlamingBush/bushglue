package com.bushglue.valve;

import com.jcraft.jsch.ChannelExec;
import com.jcraft.jsch.JSch;
import com.jcraft.jsch.Session;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.Properties;

/**
 * Runs the odroid-hosted converter over SSH: pipes the audio file to
 * `bush-cue analyze - ... -o -` on the odroid and returns the cue sheet JSON.
 *
 * Tries primaryHost first (odroid-local), falls back to fallbackHost (odroid) when
 * the connection fails -- matching the "available by sshing to odroid-local; only
 * fall back to odroid if that ssh fails" requirement. Blocking; call off the UI thread.
 */
final class SshConvert {

    private SshConvert() {}

    static String convert(String primaryHost, String fallbackHost, String user, String password,
                          String remoteCmd, byte[] audio) throws Exception {
        Exception first = null;
        for (String host : new String[]{primaryHost, fallbackHost}) {
            if (host == null || host.trim().isEmpty()) continue;
            try {
                return run(host.trim(), user, password, remoteCmd, audio);
            } catch (Exception e) {
                if (first == null) first = e;
            }
        }
        throw (first != null ? first : new Exception("no SSH host configured"));
    }

    private static String run(String host, String user, String password,
                              String remoteCmd, byte[] audio) throws Exception {
        JSch jsch = new JSch();
        Session session = jsch.getSession(user, host, 22);
        session.setPassword(password);
        Properties cfg = new Properties();
        cfg.put("StrictHostKeyChecking", "no");   // first-connect convenience on the LAN
        session.setConfig(cfg);
        session.connect(8000);
        try {
            ChannelExec ch = (ChannelExec) session.openChannel("exec");
            ch.setCommand(remoteCmd);
            OutputStream stdin = ch.getOutputStream();
            InputStream stdout = ch.getInputStream();
            ByteArrayOutputStream err = new ByteArrayOutputStream();
            ch.setErrStream(err);
            ch.connect();

            stdin.write(audio);
            stdin.flush();
            stdin.close();

            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            while (true) {
                while (stdout.available() > 0) {
                    int r = stdout.read(buf);
                    if (r < 0) break;
                    out.write(buf, 0, r);
                }
                if (ch.isClosed()) {
                    if (stdout.available() > 0) continue;
                    break;
                }
                Thread.sleep(20);
            }
            int code = ch.getExitStatus();
            ch.disconnect();
            String json = out.toString("UTF-8").trim();
            if (json.isEmpty()) {
                throw new Exception("converter failed (exit " + code + "): "
                        + err.toString("UTF-8").trim());
            }
            return json;
        } finally {
            session.disconnect();
        }
    }
}
