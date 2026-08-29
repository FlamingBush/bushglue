# MIDI node on Windows 11

`bush-midi` runs off the rig now: plug the keyboard into a laptop, point it at
the rig's broker, and it publishes `bush/flame/pulse` over the network exactly
as it does on the orangepi. Same driver, same mode gate, same hold ceilings —
only the input backend differs. On Linux it reads the ALSA rawmidi character
device directly; off Linux there is no `/dev/snd` and `select()` only works on
sockets, so it uses `python-rtmidi` (WinMM). `uv sync` installs rtmidi on
Windows and skips it on the boards, which is why nothing on the rig has to
build a C extension.

The rig still decides whether anything burns: notes are ignored unless
`bush/mode` is `midi`, and every open is a bounded pulse the relay firmware
closes on its own timer. A laptop that sleeps, drops Wi-Fi, or dies mid-note
cannot hold a solenoid open.

## Install

```powershell
winget install --id=astral-sh.uv -e     # or: pip install uv
git clone <repo> C:\bushglue
cd C:\bushglue
uv python install 3.12
uv sync --package bush-core --python 3.12   # --all-packages pulls in the audio stack too
```

Pin 3.12 (what `.python-version` already says): python-rtmidi ships Windows
wheels only for cp311 and cp312, and on anything newer uv falls back to the
sdist and wants Visual Studio Build Tools.

Find the keyboard's port name:

```powershell
.venv\Scripts\bush-midi.exe --list
```

## Run it

```powershell
$env:BUSH_MQTT_BROKER = "192.168.1.50"   # the odroid; a laptop cannot guess this
$env:MIDI_PORT = "VI25"                  # substring of the --list name; empty = first port
.venv\Scripts\bush-midi.exe
```

Everything else is the same env as the systemd unit: `MIDI_BASE_NOTE`
(default 62 — the valves are the top seven *white* keys of the 25-key board,
D E F G A B C; black keys do nothing), `MIDI_VALVES`,
`MIDI_HOLD_MS`, `MIDI_POOF_HOLD_MS`, `MIDI_RETRIGGER_MS`, `MIDI_CC_*`.
`MIDI_BACKEND=rawmidi|rtmidi` forces a backend if the automatic choice is
wrong.

## Lid closed, on battery

Windows fights this three ways by default, and the scheduled task has two
checkboxes that would defeat it. Run once, elevated:

```powershell
powershell -ExecutionPolicy Bypass -File tools\windows\setup-midi-node.ps1 `
  -Broker 192.168.1.50 -MidiPort VI25
```

That does:

- **Lid close = do nothing**, on AC and battery. This is the setting that
  makes closed-lid operation work at all — with it set, closing the lid never
  enters Modern Standby.
- **Sleep and hibernate timeouts off.**
- **Wi-Fi power saving off.** The radio's power-save drops the association
  during idle stretches, and an MQTT subscriber is idle by nature — this shows
  up as the rig going deaf a few minutes after you stop playing.
- **USB selective suspend off**, so the keyboard stays enumerated.
- **Auto Energy Saver off**, since it re-throttles the radio on battery.
- **A `bush-midi` scheduled task** that starts at logon and restarts a minute
  after any failure, with `AllowStartIfOnBatteries` and
  `DontStopIfGoingOnBatteries` set. Those two are *off* by default in Task
  Scheduler, which is why a hand-made task dies the moment you unplug.

Settings live in `%LOCALAPPDATA%\bush\midi.env` (plain `KEY=VALUE` lines, no
comments); `tools\windows\run-bush-midi.cmd` reads them and appends output to
`%LOCALAPPDATA%\bush\bush-midi.log`. Re-running the setup script merges new
values and replaces the task.

```powershell
Start-ScheduledTask -TaskName bush-midi
Stop-ScheduledTask  -TaskName bush-midi
Get-Content -Wait "$env:LOCALAPPDATA\bush\bush-midi.log"
```

Note the task triggers **at logon**, so an unattended reboot needs the laptop
to reach a logged-in desktop — enable automatic sign-in, or start the task by
hand after a reboot. Battery life is the real limit on a closed lid with sleep
disabled: expect it to sip rather than idle at zero, so plan on a few hours,
not a night.
