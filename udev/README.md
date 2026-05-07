# USB audio codec init rules

ODROID-M2 deploy detail. The C-Media USB codec on the bush rig occasionally
boots into a low-volume / muted state. These udev rules invoke `bush-codec-init`
on hotplug to set sane defaults via amixer.

To install:
  sudo cp udev/90-usb-codec-init.rules /etc/udev/rules.d/
  sudo cp udev/bush-codec-init /usr/local/bin/
  sudo udevadm control --reload

Salvaged from middog/bushglue main, commit 543cebc (2026-04-02), pre-uv-workspace era.
