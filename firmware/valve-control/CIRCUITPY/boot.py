# Expose a second USB CDC serial channel for the valve host bridge.
#   console -> the REPL (prints, tracebacks; debugging)
#   data    -> the clean "<topic> <payload>" line protocol bush_valve_serial speaks
# Radio-less RP2350 carriers (Waveshare RP2350-CAN, reflashed BridgePlate) use USB-serial
# as their host link, so the data channel must exist. boot.py runs only on a HARD reset
# (power cycle or microcontroller.reset()) -- a soft reload (Ctrl-D / file save) won't re-run it.
import usb_cdc
usb_cdc.enable(console=True, data=True)
