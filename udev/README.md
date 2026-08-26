# udev rules

## USB audio codec init

ODROID-M2 deploy detail. The C-Media USB codec on the bush rig occasionally
boots into a low-volume / muted state. These udev rules invoke `bush-codec-init`
on hotplug to set sane defaults via amixer.

To install:
  sudo cp udev/90-usb-codec-init.rules /etc/udev/rules.d/
  sudo cp udev/bush-codec-init /usr/local/bin/
  sudo udevadm control --reload

Salvaged from middog/bushglue main, commit 543cebc (2026-04-02), pre-uv-workspace era.


## GPIO access for the push-to-talk button

`/dev/gpiochip*` is root-only by default, so `bush-ptt` cannot read the button.
`95-gpio.rules` hands the character devices to a `gpio` group.

To install:
  sudo groupadd -f gpio
  sudo usermod -aG gpio $USER
  sudo cp udev/95-gpio.rules /etc/udev/rules.d/
  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=gpio

The unit sets `SupplementaryGroups=gpio` so the service picks the group up
without waiting for a re-login. Default wiring on the Orange Pi 5 Ultra is
header pin 18 (`GPIO1_A4` = `/dev/gpiochip1` line 4) with pin 20 as its GND;
the button shorts the line to ground against the internal pull-up.
