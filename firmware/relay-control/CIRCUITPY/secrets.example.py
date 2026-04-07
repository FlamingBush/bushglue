# secrets.example.py — copy to secrets.py and fill in your credentials.
# Do NOT commit secrets.py to version control.

secrets = {
    "SSID":        "your-wifi-ssid",
    "PASSWORD":    "your-wifi-password",
    # Also accepted as lowercase (used by websockets.py / non-websock.py):
    "ssid":        "your-wifi-ssid",
    "password":    "your-wifi-password",
    "MQTT_BROKER": "192.168.1.x",   # Preferred broker IP; after 3 failures the firmware scans the /24 subnet
    "MQTT_PORT":   1883,             # 1883 = plain, 8883 = TLS
    # "MQTT_USER":     "username",   # uncomment if broker requires auth
    # "MQTT_PASSWORD": "password",
}
