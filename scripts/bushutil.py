"""Shared utilities for Bush Glue service scripts."""
import subprocess


def mqtt_broker() -> str:
    """Return the MQTT broker host.

    Under WSL2 the broker runs on the Windows host; detect this via
    /proc/version and resolve the gateway IP.  On native Linux return
    localhost.
    """
    try:
        with open("/proc/version") as f:
            if "microsoft" not in f.read().lower():
                return "localhost"
    except OSError:
        return "localhost"
    result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    return "localhost"
