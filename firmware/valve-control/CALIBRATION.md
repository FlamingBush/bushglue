# Needle Valve Calibration

## Prerequisites

- MKS SERVO42C-MT V1.1 with UART enabled at 115200 baud
- Motor mounted and coupled to valve knob via dog clutch
- Pico 2 W running current firmware with valve.py
- MQTT broker running

## Step 1: Configure the MKS On-Board Menu

Before the Pico can talk to the motor, set these in the MKS display menu:

| Setting | Value | Why |
|---|---|---|
| `UartBaud` | 6 (115200) | Match Pico UART speed |
| `UartAddr` | 0 (0xE0) | Default address, matches firmware |
| `MotorType` | 1 (1.8 deg) | Standard NEMA 17 |

The firmware sets CR_UART mode, microstepping, and current via serial on boot, so those don't need to be set in the menu.

## Step 2: Determine open_steps

`open_steps` is the number of microstep pulses from fully open to fully closed. This depends on the specific valve's thread pitch and number of turns.

### Manual measurement

1. With the motor disengaged (clutch lifted), manually turn the valve to the fully open position (CCW limit, stem bottomed out in bonnet threads).
2. Count the number of full turns from fully open to just barely closed (needle touches seat — do NOT force past this point).
3. Calculate: `open_steps = turns * 3200` (at 16x microstepping, 1.8 deg motor = 3200 steps/rev).

Example: 5 turns lock-to-lock -> `open_steps = 5 * 3200 = 16000`.

### Set via MQTT

```bash
mosquitto_pub -t bush/fire/valve/calibrate -m '16000'
```

This takes effect immediately but does not persist across reboots. To change the default, edit `OPEN_STEPS` in `valve.py`.

### Fine-tuning

The calculated value should be a bit conservative (fewer steps than the real range) to keep the motor from driving into the needle seat. Start with 90% of the calculated value and increase until the valve just barely reaches minimum flow at target 0.0.

## Step 3: Motor Current

The default current is 200mA (gear 1) — the absolute minimum the MKS supports. This implements the stall-as-fuse safety philosophy: the motor stalls before it can damage the valve.

If the motor can't overcome static friction at 200mA:
1. Increase one gear at a time (400mA, 600mA, ...) by editing `CURRENT_GEAR` in `valve.py`.
2. Test homing after each increase — the motor should drive to the open stop and stall there gently.
3. Use the lowest current that reliably completes homing and normal moves.

Never exceed the torque needed to damage the valve seat. If the valve is in good condition and well-lubricated, 200-400mA should suffice.

## Step 4: Test Homing

1. Power on the Pico. It will initialize the motor but NOT home automatically.
2. Send a home command:
   ```bash
   mosquitto_pub -t bush/fire/valve/home -m ''
   ```
3. Watch the motor. It should spin CCW (when looking at the back of the motor) toward the fully-open stop.
4. When it stalls at the open stop, the firmware sets this as position zero (= open_steps in our convention) and reports `idle` state.
5. Check status:
   ```bash
   mosquitto_sub -t 'bush/fire/valve/status' -C 1
   ```
   Should show `{"state":"idle","pos":1.0,"target":1.0,"homed":true,...}`.

### If homing fails

- **Motor doesn't move:** Check UART wiring (GP4 -> MKS RX, GP5 -> MKS TX). Check baud rate matches. Check motor is not mechanically seized.
- **Motor moves wrong direction:** The zero direction is set to CCW in firmware. If your motor is wired with reversed coils, swap one coil pair (A+/A- or B+/B-) or change `CMD_SET_ZERO_DIR` parameter from `0x01` to `0x00` in `valve.py:cmd_home()`.
- **Motor stalls immediately:** Current too low for static friction. Increase `CURRENT_GEAR`.
- **Homing times out (30s):** Motor is spinning freely without hitting a stop. Check mechanical coupling — is the clutch engaged? Is the valve installed?

## Step 5: Test Movement

After homing:

```bash
# Move to 50% open
mosquitto_pub -t bush/fire/valve/target -m '0.5'

# Move to fully closed (minimum flame)
mosquitto_pub -t bush/fire/valve/target -m '0.0'

# Move back to fully open
mosquitto_pub -t bush/fire/valve/target -m '1.0'

# Emergency stop
mosquitto_pub -t bush/fire/valve/stop -m ''
```

Monitor position:
```bash
mosquitto_sub -t 'bush/fire/valve/actual'
```

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| No UART response | Wiring, baud mismatch, UART not enabled in MKS menu | Check wiring, verify `UartBaud` setting |
| Motor hums but doesn't turn | Current too low for load | Increase `CURRENT_GEAR` |
| Motor overshoots position | PID tuning (MKS defaults) | Adjust via MKS menu or serial PID commands |
| Position drifts over time | Encoder miscalibration | Run MKS encoder calibration (hold motor still, send `E0 80 00 60`) |
| Valve sticks at low positions | Needle seat friction | Increase current slightly; check valve for debris |
| Motor doesn't respond after dust exposure | Conductive alkaline dust shorting the driver | Remove motor, clean with compressed air, check for visible shorts. Replace if necessary. This is a known failure mode at Burning Man. |
| "stalled" state won't clear | Motor hit an obstruction or current too low | Send `bush/fire/valve/home` to re-home and clear the stall state |
