# secrets.example.py — copy to secrets.py and fill in your credentials.
# Do NOT commit secrets.py to version control.

secrets = {
    # ── Wi-Fi (Pico 2 W node) ────────────────────────────────────────────────
    # Single network (back-compatible):
    "SSID":        "your-wifi-ssid",
    "PASSWORD":    "your-wifi-password",
    # Also accepted as lowercase (used by other firmware sharing this secrets.py):
    "ssid":        "your-wifi-ssid",
    "password":    "your-wifi-password",
    # ...or several networks — the firmware tries each (visible ones first).
    # If set, NETWORKS overrides SSID/PASSWORD:
    # "NETWORKS": [
    #     {"ssid": "primary-ap", "password": "pw1"},
    #     {"ssid": "backup-ap",  "password": "pw2"},
    # ],

    # ── MQTT broker(s) ───────────────────────────────────────────────────────
    "MQTT_BROKER": "192.168.1.x",   # single broker (back-compatible)
    # ...or several — tried in order before the /24 subnet scan.
    # If set, MQTT_BROKERS overrides MQTT_BROKER:
    # "MQTT_BROKERS": ["192.168.1.10", "192.168.1.11"],
    "MQTT_PORT":   1883,             # 1883 = plain, 8883 = TLS
    # "MQTT_USER":     "username",   # uncomment if broker requires auth
    # "MQTT_PASSWORD": "password",

    # ── CAN carrier ──────────────────────────────────────────────────────────
    # CAN board profile (see BOARD_PROFILES in code.py): "canberry" (default) = CanBerry
    # MCP2515 on the Pico 2 W GPIO; "waveshare" = Waveshare RP2350-CAN (XL2515).
    "BOARD":       "canberry",
}
