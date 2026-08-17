<!-- This repo is a MIRROR. Do not edit these files here. -->
> **Mirror of the station files.** Source of truth is the `mic-test-1` repo;
> this holds only what a station installs, so a Pi pulls ~176KB instead of the
> ~125MB of enclosure CAD. Changes are made in `mic-test-1` and pushed here
> with `sync_deploy_repo.sh` — edits made directly in this repo get
> overwritten by the next sync.

# MiLa Test Rig — USB Mic Test (KRUU US)

Aaron Simo, with Claude (Anthropic) · Copyright © 2026 KRUU US INC. All rights reserved.

Automated pass/fail test for the USB mic units. The unit goes in the
sound-dampened box with the Raspberry Pi, next to a fixed reference
microphone mounted permanently in the box. When you plug the unit in, the Pi
plays a frequency sweep out the box speaker and records it on the unit's mic
and the reference mic at the same time, then scores the unit relative to the
reference instead of against a fixed number. That catches a dead mic, a
weak/low-sensitivity one, a muffled one (no highs), and a distorted/crackly
one -- and because everything the reference also hears (speaker aging, box
acoustics, temperature) cancels out of the ratio, the result stays accurate
without re-tuning. Result is shown on the LEDs and beeps:

- READY: both LEDs blink together (station just started up, no unit in the
  box yet)
- PASS: green LED (solid) + 2 high beeps
- FAIL: red LED (solid) + 3 low beeps -- this specific mic failed
- STATION ERROR: red LED flashing, NO beep -- a rig/reference problem, not a
  verdict on the mic in the box (reference mic not detected, no playback
  card, or a crash). Keeps flashing until the next test or calibration
  clears it.

The whole test takes several seconds and runs automatically every time a
unit is plugged in. No keyboard or screen needed once it is installed. To
retest a unit, just unplug it and plug it back in.

The button is only for calibration: hold it 2 seconds (see "Calibrating the
box"). Both LEDs come on once you have held long enough; let go then and
calibration starts. A short press does nothing.


## On-site runbook (quick reference)

For a station that is already built -- an install/update or a routine
on-site visit:

1. Make sure the reference mic is in the box and working: the built-in I2S
   mic (already fitted centrally), or a USB mic in any free port as a
   temporary retrofit.
2. Copy the latest files to the Pi and run `sudo bash install.sh` (Steps 2-3
   below have the full commands the first time).
3. Put a known-good mic in the box, hold the button 2 seconds -- 3 green
   blinks = calibrated.
4. Known-good mic -> green. Known-bad mic -> red. Done.

If anything looks wrong, run `--selftest` (see below) and send the output
back rather than troubleshooting blind.


## What you need

- Raspberry Pi 3B+ or newer
- Raspberry Pi OS (Bookworm, 64-bit Lite is fine), with the username set to `pi`
- The test box (Pi + speaker + foam + the two LEDs wired to the GPIO header)
- A fixed reference microphone mounted permanently in the box, close to where
  the unit under test sits (see "Reference mic" below) -- every unit is now
  scored against this mic, so the station cannot test anything without it
- A network connection on the Pi (needed once, during install, to download
  the audio tools)

LED and button wiring, if you are building a box from scratch:

- Green LED -> GPIO 17 (physical pin 11) -> resistor -> LED -> GND
- Red LED   -> GPIO 27 (physical pin 13) -> resistor -> LED -> GND
- Calibration button -> GPIO 22 (physical pin 15) -> button -> GND
  (no resistor needed; the Pi's internal pull-up is used)


## Reference mic

The code tells the reference apart from the unit under test automatically --
whichever capture card is not the product's own USB VID/PID is treated as
the reference. Two ways to fit it:

- Preferred, central builds: an INMP441 I2S digital MEMS mic, wired directly
  to the GPIO header (no USB port used):
  - BCLK -> GPIO 18, LRCLK -> GPIO 19, DATA -> GPIO 20, VDD -> 3V3,
    GND -> GND, L/R select -> GND
  - Does not conflict with the LEDs (GPIO 17/27) or the button (GPIO 22)
  - Needs I2S enabled in `config.txt` and a reboot -- the installer does this
    for you (see Step 3), but the exact overlay is still bench-unconfirmed;
    see "Open bench items"
  - Mount it fixed, close to where the unit under test sits, facing the same way
- Fallback, field retrofits: any USB mic, as long as its VID/PID is different
  from the product's. Plug it into any free USB port on the Pi. Use this for
  a station that cannot be brought in for the I2S mod.

Either way, the reference mic needs to be plugged in and working before you
install or calibrate -- without it every test reports a station error
(flashing red, no beep), never a pass or fail.


## Step 1: Prepare the Pi (first time only)

Skip this if the Pi is already flashed and you can SSH into it.

Flash the SD card using Raspberry Pi Imager, choosing Raspberry Pi OS (Bookworm,
64-bit Lite). Before writing, open the advanced settings (the gear icon) and set:

- Hostname: something like `kruu-mictest-01`
- Enable SSH (password authentication)
- Username: `pi`
- Password: the standard KRUU station password (ask Aaron if you do not have it)
- Configure WiFi if you are not using ethernet

Write the card, put it in the Pi, and power on. Give it a minute to boot, then
continue below. When `ssh` or `scp` asks for a password, use the one you set above.


## Step 2: Get the files onto the Pi

1. Download the zip from the Google Drive folder to your computer.
2. Unzip it. You will get a folder named `MiLa-Test-Rig` with these files:
   `install.sh`, `usb_audio_test.py`, `analysis.py`, `devices.py`,
   `usb-audio-test.service`, `usb-audio-test-button.service`,
   `99-usb-audio-test.rules`, and this `README.md`.
3. Copy the whole folder to the Pi. From your computer's terminal:

   ```
   scp -r MiLa-Test-Rig pi@<pi-hostname>.local:~/
   ```

   Replace `<pi-hostname>` with the Pi's hostname (the one set when the SD card
   was flashed, e.g. `kruu-mictest-01`). If `.local` does not resolve, use the
   Pi's IP address instead (`scp -r MiLa-Test-Rig pi@10.0.0.50:~/`).

If you cannot use `scp`, you can also copy the unzipped folder onto a USB stick
and move it to the Pi that way.


## Step 3: Install

SSH into the Pi (same password you set when flashing the card):

```
ssh pi@<pi-hostname>.local
```

Then run the installer:

```
cd MiLa-Test-Rig
sudo bash install.sh
```

It installs the audio tools, copies the test into `/opt/usb_audio_test/`,
enables I2S for the reference mic in the boot config, and sets the test to
run automatically on every USB plug-in. When it finishes it prints the Pi's
hostname and IP.

Note: the installer fires one test immediately. With the reference mic not
enabled yet (first install, before the reboot below) or no unit in the box,
you may see a station error (flashing red) or just unlit LEDs -- neither is a
broken install.

If this is a first-time install, or the installer printed "REBOOT REQUIRED",
reboot the Pi now:

```
sudo reboot
```

Then confirm the reference mic came up:

```
arecord -l
```

This should list the reference mic's capture card (an I2S card for a central
build, or a second USB audio card for a retrofit) alongside the Pi's other
audio. Then run the on-site diagnostic once to confirm everything sees each
other correctly (see "On-site diagnostic" below):

```
sudo /opt/usb_audio_test/usb_audio_test.py --selftest
```

Re-running `install.sh` later (for updates) is safe -- it will not duplicate
anything in `config.txt` or the systemd units.

That's it. The Pi is ready.


## Step 4: Use it

1. Put a unit in the box and close the lid.
2. The test runs on its own within a couple of seconds.
3. Watch the result:
   - Green LED (solid) + 2 high beeps = PASS
   - Red LED (solid) + 3 low beeps = FAIL
4. Take the unit out, drop in the next one. Repeat.

To see the detailed log of what the test measured:

```
sudo journalctl -u usb-audio-test -f
```

To run a test by hand (instead of plugging in):

```
sudo /opt/usb_audio_test/usb_audio_test.py
```


## Calibrating the box

The pass/fail limits are tuned relative to the reference mic, not to an
absolute number, so they mostly hold up as the box, speaker, and reference
mic age together -- but you should still calibrate any time you set up a new
box, change the foam or speaker, swap the reference mic, or start testing a
different product. No keyboard needed:

1. Put a known-good mic in the box and close the lid. Wait for its plug-in
   test to finish.
2. Hold the button for 2 seconds (the long hold is deliberate, so a bump or
   short press can't wipe your limits). Both LEDs light up once you have held
   long enough - let go then and calibration starts.
3. It plays 5 sweeps, reads the known-good mic and the reference mic at the
   same time, and writes new reference-relative limits to
   `/opt/usb_audio_test/calibration.json` automatically -- along with a floor
   on the reference itself, so a degraded reference trips a station error
   instead of silently shifting every verdict.
4. Watch the result:
   - 3 green blinks = calibration saved
   - Flashing red (no beep) = calibration failed (bad readings, no unit in
     the box, or the reference itself reads as unhealthy); fix the setup and
     hold the button again

You can also run it over SSH instead of using the button:

   ```
   sudo /opt/usb_audio_test/usb_audio_test.py --calibrate
   ```

From then on the test uses those limits. The calibration file is not touched by
reinstalling, so your settings stick. To go back to the built-in defaults, just
delete it:

   ```
   sudo rm /opt/usb_audio_test/calibration.json
   ```


## On-site diagnostic (--selftest)

If a station is behaving oddly and there is no one technical on site, run
this and paste the output back rather than troubleshooting over a phone call:

```
sudo /opt/usb_audio_test/usb_audio_test.py --selftest
```

It always finishes and never crashes silently -- even an internal error
prints a traceback instead of nothing. It prints, in order:

1. Which capture cards it found and which one it picked as the DUT vs. the
   reference (including each card's USB VID:PID, or "non-USB/I2S" for the
   I2S reference)
2. The room-noise ("ambient") level on the DUT and reference mics with
   nothing playing
3. One dry-run sweep's numbers (level ratio, high-frequency ratio, waveform
   match, DUT peak) and whether the reference itself reads as healthy
4. A full 3-sweep verdict (PASS / FAIL / STATION ERROR) with the reason,
   exactly like a real test would produce

This is the fastest way to tell "no unit seated," "reference mic not
detected," and "a real fail" apart without being in the room.


## Troubleshooting

- Flashing red LED, no beep (station error): the last run hit a rig problem,
  not a mic verdict -- most commonly the reference mic is not detected, there
  is no playback card, or a crash. It clears as soon as the next test or
  calibration runs. Check `arecord -l` for the expected cards, and see "Open
  bench items" below if the reference mic will not show up. Details are in
  the log (`sudo journalctl -u usb-audio-test -f`).
- "USB audio device not found": the unit is not plugged in or not seated.
  Reseat it and try again. Check `arecord -l` shows a `[USB...]` card for it.
- Button does nothing: hold it the full 2 seconds, and check the wiring
  (GPIO 22 to GND) and that the button service is running
  (`sudo systemctl status usb-audio-test-button`).
- Test always fails on known-good units (solid red + beep, not flashing): the
  box, reference mic, or mic model differs from the calibrated one. Run the
  one-command calibration (see "Calibrating the box").
- LEDs do not light at all: check the LED wiring (GPIO 17 green, GPIO 27 red)
  and that `python3-rpi.gpio` installed (the installer does this).
- See the live log any time with `sudo journalctl -u usb-audio-test -f`.


## Open bench items

Called out here on purpose -- these are the parts of this rollout that can
only be finished with hands on the actual station, not from a laptop:

- `PRODUCT_VIDPID` in `usb_audio_test.py` is still a placeholder. Run `lsusb`
  on the station with a product unit plugged in, find its VID:PID, and set it
  there -- until this is done the station can never recognize a unit as the
  DUT (it will just sit idle).
- The I2S overlay the installer writes to `config.txt`
  (`dtoverlay=googlevoicehat-soundcard`) is a reasonable default, not a
  confirmed one. After the first reboot, confirm `arecord -l` shows the
  reference mic; if it does not, or the capture format looks wrong, edit the
  clearly-marked block in `config.txt` (look for "MiLa reference mic
  (INMP441 I2S)") and reboot again.
- Pass/fail thresholds start from conservative built-in defaults and get
  replaced the first time you calibrate (see "Calibrating the box"). Run
  calibration against known-good units and, ideally, a few known-bad ones
  (dead / weak / muffled / distorted) to confirm the limits catch what they
  should before relying on the station for real.


## What gets installed

- `/opt/usb_audio_test/usb_audio_test.py` - the test
- `/opt/usb_audio_test/analysis.py`, `/opt/usb_audio_test/devices.py` -
  modules the test imports (signal analysis/scoring, card discovery)
- `/etc/systemd/system/usb-audio-test.service` - runs the test
- `/etc/systemd/system/usb-audio-test-button.service` - watches the
  calibration button
- `/etc/udev/rules.d/99-usb-audio-test.rules` - triggers the test on plug-in
- `dtparam=i2s=on` and a reference-mic overlay block added to the boot config
  (`/boot/firmware/config.txt` on Bookworm, or `/boot/config.txt`) to enable
  the reference mic -- see "Open bench items"
- `/opt/usb_audio_test/calibration.json` - created when you run `--calibrate`,
  holds the pass/fail limits for your box (delete it to use the built-in defaults)
