#!/usr/bin/env python3
"""
USB audio card test.
Plays a frequency sweep out the card and records it back to check the mic.

MiLa Test Rig
Aaron Simo, with Claude (Anthropic)
Copyright (c) 2026 KRUU US INC. All rights reserved.
"""

import subprocess
import sys
import os
import time
import math
import wave
import tempfile
import array
import json
import signal
import statistics
import traceback
import multiprocessing

import analysis
import devices

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# Config
# Stimulus is a frequency sweep. Measuring energy across a band (not single
# tones) averages over the box resonances, so readings stay stable regardless
# of exactly how the mic sits each time.
# Sweep band lives in analysis.py (it builds the waveform-match template); source
# it from there so the played chirp and the template can never drift out of lockstep.
SWEEP_START_HZ = analysis.SWEEP_START_HZ
SWEEP_END_HZ = analysis.SWEEP_END_HZ
SWEEP_DURATION_SEC = 1.2
SAMPLE_RATE = 16000           # 16kHz is fine for voice
TONE_AMPLITUDE = 0.1          # playback level (0-1), kept low so the mic doesn't clip

# DUT identification is VID:PID-based (devices.select_cards), never card index
# or USB port -- ports/indices differ per unit and even per boot. Real value
# comes from `lsusb` on the station (see task-6-report.md bench checklist);
# until then this can never match a real card, so select_cards() -> dut=None.
PRODUCT_VIDPID = "0c76:1203"

# Dual-capture ratiometric pipeline (devices.select_cards + analysis.*): both
# arecords start together, then aplay fires this many seconds later so the
# chirp lands at a known, small offset in each recording -- analysis.
# find_sweep_onset only needs to search a narrow window around it, in either
# recording, regardless of the two cards' sample rates.
RECORD_HEAD_START = 0.3
REFERENCE_EXPECTED_SEC = RECORD_HEAD_START
# Passed explicitly so record_duration below is derived from the same number.
# Keep it in step with waveform_match's lag window in analysis.py: narrowing
# one without the other leaves a wide miss recoverable in only one of them.
ONSET_SEARCH_SEC = 0.05
# Nothing downstream reads past onset+SWEEP_DURATION_SEC+ONSET_SEARCH_SEC --
# gated() only ever extracts exactly rate*sweep_dur samples from the detected
# onset -- so this only needs to cover process/IO jitter, not analysis needs.
RECORD_TAIL_MARGIN_SEC = 0.3
N_SWEEPS = 3                   # sweeps per run; evaluate_run judges on the median
# Sweep measurements are independent, so they fan out across the Pi 3B's four
# cores. Capped rather than using all four so the capture/IO and the rest of
# the system keep a core free.
ANALYSIS_WORKERS = 3

# Mixer resets to defaults on every replug, so re-apply each run to keep gain
# and speaker level fixed. Raw ALSA values for this device (None to skip).
MIC_CAPTURE_CONTROL = "Mic Capture Volume"
# 150 chosen at the bench (gain.py sweep, kruu-mictest-01): at the old 408 the
# DUT mic railed against its own speaker (peak 1.0, clipping, match ~0.47); 150
# lands peak ~0.47 with headroom and the best match (~0.59). Raw ALSA value.
MIC_CAPTURE_VALUE = "150"
SPEAKER_CONTROL = "Speaker Playback Volume"
SPEAKER_VALUE = "688"
# The I2S reference (INMP441-class) is normally a fixed-gain digital MEMS mic
# with no software capture control; leave unset (skipped gracefully) unless a
# specific station's reference card exposes one.
REF_CAPTURE_CONTROL = None
REF_CAPTURE_VALUE = None

# --calibrate runs N_CAL sweeps against a known-good unit and derives the
# thresholds from them. Calibrate at the seated position and leave the unit
# alone: a reseat moves the score about 2%, which the margins cover. Use
# --calibrate --add to fold in more good units; one unit's spread does not
# cover the next one's.
# 8 sweeps, not 15. 15 dates from an enclosure that let the unit move, when
# calibration had to sample positions to mean anything. The current box holds
# it to about 2% and a run's sweeps agree to ~1.05x, so the extra sweeps were
# re-measuring a known number for another 40 seconds. 8 still leaves trim a
# sweep to discard at each end, and --add pools across units anyway.
N_CAL = 8
CAL_MIN_GOOD_SWEEPS = 3
# Discard this fraction of the extremes at each end before taking the
# floor/ceiling. Sampling a range means some captures land mid-movement or at
# the edge of usable placement; a raw min() would hand the worst of those to
# every future unit as acceptable and quietly widen the gate until bad units
# pass. See analysis.compute_calibration.
CAL_TRIM_FRAC = 0.15
CAL_MARGINS = {"level": 0.6, "hf": 0.6, "match": 0.8}
# min_total = min(observed reference total) * this. Separates a weak speaker
# from a dead mic, and the two populations are only 1.56x apart: the quietest
# good reading seen is 1.42e-05, the loudest lost-driver reading 9.1e-06. 0.6
# lands between them. 0.8 was tried and failed a known-good unit as a speaker
# fault, so widen this rather than tighten it if that shows up.
CAL_REF_FLOOR_FRACTION = 0.6
# The unit's own mic floor only has to catch a genuinely deaf mic (2.5e-11
# against ~6.4e-06 good). The level gate does the finer work.
CAL_DUT_FLOOR_FRACTION = 0.3
# A healthy reference matches the template at only ~0.40, not the ~1.0 the
# synthetic fixtures reach, because the chirp reverberates in the box. 0.5
# rejected good references and blocked calibration.
CAL_REF_MIN_MATCH = 0.25
CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")

# Used only before a station has ever been calibrated. It has to be loose
# enough that a healthy reference passes so calibration can run at all, and
# only needs to trip on true silence. min_match does the real work.
UNCALIBRATED_REF_FLOORS = {"min_total": 1e-7, "min_match": CAL_REF_MIN_MATCH,
                           # same order-of-magnitude reasoning as min_total, for the
                           # unit's own mic: loose enough that any real recording clears
                           # it, tight enough to catch true silence.
                           "min_dut_total": 1e-7}

PASS_BEEP_FREQ = 880
FAIL_BEEP_FREQ = 200
RESULT_REPEATS_PASS = 2
RESULT_REPEATS_FAIL = 3

GPIO_GREEN_PIN = 17
GPIO_RED_PIN = 27
GPIO_BUTTON_PIN = 22          # calibration button: GPIO 22 (pin 15) to GND
BUTTON_HOLD_SEC = 2.0         # hold time, so a bump can't trigger calibration
ERROR_FLASH_INTERVAL = 0.5

TEMP_DIR = tempfile.gettempdir()
TEST_TONE_FILE = os.path.join(TEMP_DIR, "test_tone.wav")
DUT_RECORDING_FILE = os.path.join(TEMP_DIR, "dut_recording.wav")
REF_RECORDING_FILE = os.path.join(TEMP_DIR, "ref_recording.wav")
RESULT_TONE_FILE = os.path.join(TEMP_DIR, "result_tone.wav")
AMBIENT_DUT_FILE = os.path.join(TEMP_DIR, "selftest_ambient_dut.wav")
AMBIENT_REF_FILE = os.path.join(TEMP_DIR, "selftest_ambient_ref.wav")
AMBIENT_CAPTURE_SEC = 1.0      # --selftest: length of the no-playback room-noise capture
PID_FILE = os.path.join(TEMP_DIR, "usb_audio_test.pid")


def setup_gpio():
    if not GPIO_AVAILABLE:
        return
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_GREEN_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(GPIO_RED_PIN, GPIO.OUT, initial=GPIO.LOW)


def set_leds(green, red):
    if not GPIO_AVAILABLE:
        return
    try:
        GPIO.output(GPIO_GREEN_PIN, GPIO.HIGH if green else GPIO.LOW)
        GPIO.output(GPIO_RED_PIN, GPIO.HIGH if red else GPIO.LOW)
    except Exception:
        # never let an LED problem take down the test or the error flasher
        pass


def blink_green(times, on_sec=0.3, off_sec=0.3):
    for _ in range(times):
        set_leds(True, False)
        time.sleep(on_sec)
        set_leds(False, False)
        time.sleep(off_sec)


def signal_calibration_saved(play_card):
    """Confirm a finished calibration loudly enough to notice.

    Calibration runs for about 90 seconds, so whoever started it has usually
    stopped watching by the time it lands. Three short green blinks were easy
    to miss entirely, which reads as 'nothing happened'. This holds green solid
    for a beat, then blinks it, and beeps -- so it carries whether you are
    looking at the box or not.

    Four beeps, deliberately not the two of a PASS or the three of a FAIL, so
    it cannot be mistaken for a verdict on the unit sitting in the box."""
    set_leds(True, False)
    time.sleep(1.5)
    blink_green(5, on_sec=0.15, off_sec=0.15)
    set_leds(True, False)
    try:
        generate_result_tone(RESULT_TONE_FILE, PASS_BEEP_FREQ, 4, beep_duration=0.12,
                             gap_duration=0.06)
        subprocess.run(["aplay", "-D", f"plughw:{play_card},0", RESULT_TONE_FILE],
                       timeout=10, capture_output=True)
    except Exception:
        pass
    time.sleep(1.0)
    set_leds(False, False)


def signal_calibration_failed(play_card):
    """Same idea for a failed calibration: red, and a low burst you can hear
    from across the room. main() still flashes red afterwards until the next
    run clears it."""
    for _ in range(4):
        set_leds(False, True)
        time.sleep(0.15)
        set_leds(False, False)
        time.sleep(0.15)
    try:
        generate_result_tone(RESULT_TONE_FILE, FAIL_BEEP_FREQ, 4, beep_duration=0.12,
                             gap_duration=0.06)
        subprocess.run(["aplay", "-D", f"plughw:{play_card},0", RESULT_TONE_FILE],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def blink_ready(times=3, on_sec=0.15, off_sec=0.15):
    """Blink both LEDs together to signal the station is up and ready to test.

    Both LEDs at once is distinct from the single-colour pass/fail/error and
    the green calibration-saved cues, so it reads clearly as 'ready'."""
    for _ in range(times):
        set_leds(True, True)
        time.sleep(on_sec)
        set_leds(False, False)
        time.sleep(off_sec)


def flash_red_until_cleared():
    """Flash the red LED until another run takes over.

    Station errors land here. The process stays alive flashing; the next run
    (a unit being plugged in, or a button-triggered calibration) kills it via
    the pid file, which is what 'clears' the error."""
    if not GPIO_AVAILABLE:
        return
    log("Flashing red until the next test or calibration (Ctrl+C to stop)")
    red_on = True
    try:
        while True:
            set_leds(False, red_on)
            red_on = not red_on
            time.sleep(ERROR_FLASH_INTERVAL)
    except KeyboardInterrupt:
        set_leds(False, False)


def take_over_pid_file():
    """Kill any previous test/calibration instance (e.g. one flashing an
    error) so a new run always clears it, then record our own pid."""
    try:
        with open(PID_FILE) as f:
            old_pid = int(f.read().strip())
    except (OSError, ValueError):
        old_pid = None

    if old_pid and old_pid != os.getpid():
        try:
            # only kill if the pid is still one of us (guards pid reuse)
            with open(f"/proc/{old_pid}/cmdline", "rb") as f:
                if b"usb_audio_test" in f.read():
                    os.kill(old_pid, signal.SIGTERM)
                    time.sleep(0.2)
        except OSError:
            pass

    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        log(f"Could not write pid file: {e}")


def log(msg):
    print(f"[TEST] {msg}", flush=True)


def generate_chirp_wav(filepath, f_start, f_end, duration, sample_rate):
    """Write a logarithmic chirp (frequency sweep) from f_start to f_end."""
    samples = array.array("h")
    n = int(sample_rate * duration)
    rate = math.log(f_end / f_start) / duration
    for i in range(n):
        t = i / sample_rate
        phase = 2.0 * math.pi * f_start * (math.exp(t * rate) - 1.0) / rate
        value = TONE_AMPLITUDE * math.sin(phase)
        samples.append(int(value * 32767))
    with wave.open(filepath, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def generate_result_tone(filepath, frequency, beep_count, beep_duration=0.25,
                         gap_duration=0.1, sample_rate=16000):
    """Generate pass/fail beeps."""
    samples = array.array("h")
    two_pi_f = 2.0 * math.pi * frequency
    for beep_idx in range(beep_count):
        n = int(sample_rate * beep_duration)
        for i in range(n):
            value = 0.8 * math.sin(two_pi_f * i / sample_rate)
            samples.append(int(value * 32767))
        if beep_idx < beep_count - 1:
            samples.extend(array.array("h", [0] * int(sample_rate * gap_duration)))
    with wave.open(filepath, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def lock_card_gains(play_card, rec_card, ref_card=None):
    """Pin capture gain and speaker level so the test runs at a known state.
    These reset to device defaults on every replug, so we re-apply each run.
    Play and record are normally the same card, but set each control on the
    card it belongs to in case they ever differ. If a reference card is given
    and REF_CAPTURE_CONTROL is set, pin its capture gain too; left unset (the
    default) this is skipped gracefully -- most I2S MEMS references have no
    software gain control at all."""
    controls = [(rec_card, MIC_CAPTURE_CONTROL, MIC_CAPTURE_VALUE),
                (play_card, SPEAKER_CONTROL, SPEAKER_VALUE)]
    if ref_card is not None:
        controls.append((ref_card, REF_CAPTURE_CONTROL, REF_CAPTURE_VALUE))
    for card, name, value in controls:
        if not name or value is None:
            continue
        try:
            result = subprocess.run(["amixer", "-c", str(card), "cset", f"name={name}", value],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                log(f"Could not set '{name}': {result.stderr.strip()}")
        except Exception as e:
            log(f"Could not set '{name}': {e}")


def play_and_record_dual(play_card, dut_card, ref_card, ref_kind, chirp_path, dut_wav, ref_wav):
    """Simultaneously record the DUT and the reference mic, then play the chirp.

    Drives two arecords at once: the DUT (USB, S16_LE @ SAMPLE_RATE) and the
    reference (S32_LE @ 48k for an I2S card; S16_LE @ SAMPLE_RATE, like the
    DUT, for a USB retrofit reference). Both captures start together; aplay
    fires RECORD_HEAD_START later so the chirp lands at a known offset in each
    recording (analysis.find_sweep_onset searches around
    REFERENCE_EXPECTED_SEC independently in each -- the two cards need not
    share a sample rate). Kills all three processes and returns False on
    timeout or any non-zero exit (e.g. a unit pulled mid-test)."""
    # Head start + sweep + the onset detector's forward search margin + a pad
    # for IO jitter, rounded up because arecord -d takes whole seconds.
    # Nothing downstream reads past that, so anything more is wasted capture.
    record_duration = math.ceil(RECORD_HEAD_START + SWEEP_DURATION_SEC
                                + ONSET_SEARCH_SEC + RECORD_TAIL_MARGIN_SEC)

    if ref_kind == "i2s":
        ref_format, ref_rate = "S32_LE", 48000
    else:
        ref_format, ref_rate = "S16_LE", SAMPLE_RATE

    dut_proc = subprocess.Popen([
        "arecord", "-D", f"plughw:{dut_card},0",
        "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1",
        "-d", str(record_duration), dut_wav,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    ref_proc = subprocess.Popen([
        "arecord", "-D", f"plughw:{ref_card},0",
        "-f", ref_format, "-r", str(ref_rate), "-c", "1",
        "-d", str(record_duration), ref_wav,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    time.sleep(RECORD_HEAD_START)

    play_proc = subprocess.Popen([
        "aplay", "-D", f"plughw:{play_card},0", chirp_path,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    procs = (play_proc, dut_proc, ref_proc)
    try:
        _, play_err = play_proc.communicate(timeout=SWEEP_DURATION_SEC + 5)
        _, dut_err = dut_proc.communicate(timeout=record_duration + 5)
        _, ref_err = ref_proc.communicate(timeout=record_duration + 5)
    except subprocess.TimeoutExpired:
        # arecord/aplay hang if a USB device disappears under them (e.g. the
        # unit was pulled mid-test); kill all three so nothing holds a device
        # busy for the next run.
        for p in procs:
            p.kill()
        for p in procs:
            p.wait()
        log("Play/record timed out (unit unplugged mid-test?)")
        return False

    if play_proc.returncode != 0:
        log(f"Playback error: {play_err.decode().strip()}")
        return False
    if dut_proc.returncode != 0:
        log(f"DUT recording error: {dut_err.decode().strip()}")
        return False
    if ref_proc.returncode != 0:
        log(f"Reference recording error: {ref_err.decode().strip()}")
        return False
    return True


def load_thresholds():
    """Read calibration.json if present and it parses; else conservative,
    stdlib-only defaults for a not-yet-calibrated station.

    Returns (thresholds, ref_floors): thresholds is analysis' DUT-gate dict
    (min_level/max_level/min_tilt/min_match/max_peak); ref_floors is the
    reference-health floor dict (min_total/min_match) consumed by
    analysis.reference_healthy. Both come straight from the schema
    run_calibration() writes below. If the file is missing, unreadable, or
    doesn't match that schema, falls back to analysis.default_thresholds() +
    UNCALIBRATED_REF_FLOORS so the station still runs (conservatively) before
    its first calibration."""
    try:
        with open(CALIBRATION_FILE) as f:
            data = json.load(f)
        return data["thresholds"], data["ref_floors"]
    except (OSError, ValueError, KeyError, TypeError):
        return analysis.default_thresholds(), dict(UNCALIBRATED_REF_FLOORS)


def _load_calibration_pool():
    """The raw per-sweep pool from a previous calibration, or None if the file
    is missing/unreadable or predates the pool being stored."""
    try:
        with open(CALIBRATION_FILE) as f:
            data = json.load(f)
        pool = data["pool"]
        return (pool["scores"], pool["ref_totals"],
                pool["ref_matches"], pool["dut_totals"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _derive_and_save(scores, ref_totals, ref_matches, dut_totals):
    """Derive thresholds from a pool of good-unit sweeps and persist them.

    Split out so the thresholds can be recomputed from an existing pool
    without recording anything -- which is what you want when a constant
    changes, and the only safe option when the unit sitting in the box is
    a known-bad one."""
    thresholds = analysis.compute_calibration(scores, CAL_MARGINS, trim=CAL_TRIM_FRAC)
    ref_total_med = statistics.median(ref_totals)
    ref_floors = {
        # From the QUIETEST good unit seen, not the median. This floor is what
        # separates a weak speaker from a dead mic: the lost-driver unit reads
        # 8.1e-06 where the quietest good unit reads 1.45e-05, so a floor
        # derived from the median (and a 0.3 fraction) sat at 5e-06 and waved
        # the bad speaker straight through as "the reference heard it fine".
        "min_total": min(ref_totals) * CAL_REF_FLOOR_FRACTION,
        "min_match": CAL_REF_MIN_MATCH,
        # Absolute "did the unit's mic hear anything at all" floor. Separate
        # from min_level (a ratio): when the reference is silent the ratio
        # means nothing, so attributing a dead speaker vs a deaf rig needs an
        # absolute reading from the unit's own mic.
        "min_dut_total": statistics.median(dut_totals) * CAL_DUT_FLOOR_FRACTION,
        # What a healthy unit reads. Used to tell a weak speaker (producing,
        # but well under this) from a rig fault or a misseated unit (which
        # both still read normally, because the unit's mic is a fixed distance
        # from its own speaker).
        "min_dut_normal": thresholds["min_level"],
    }
    data = {
        "thresholds": thresholds,
        "ref_floors": ref_floors,
        "observed": {
            "ref_total": {"median": ref_total_med, "min": min(ref_totals), "max": max(ref_totals)},
            "ref_match": {"median": statistics.median(ref_matches),
                          "min": min(ref_matches), "max": max(ref_matches)},
        },
        "samples": len(scores),
        # The raw pool, so `--calibrate --add` can fold in another known-good
        # unit rather than starting over. Unit-to-unit spread is real: two good
        # units measured 1.68x apart on tilt, which is wider than the margin,
        # so thresholds from a single unit do not reliably cover the next one.
        "pool": {
            "scores": scores,
            "ref_totals": ref_totals,
            "ref_matches": ref_matches,
            "dut_totals": dut_totals,
        },
    }

    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        log(f"Calibration FAILED: could not write {CALIBRATION_FILE}: {e}")
        return False

    log("=== CALIBRATION SAVED ===")
    log(f"  thresholds: {thresholds}")
    log(f"  ref_floors: {ref_floors}")
    log(f"  ({len(scores)} samples) written to {CALIBRATION_FILE}")
    return True


def recompute_calibration():
    """Re-derive thresholds from the stored pool, recording nothing.

    Constants that shape the floors (CAL_REF_FLOOR_FRACTION and friends) get
    revised as bench data accumulates, and the pool of good sweeps is already
    on disk -- so there is no reason to make someone re-run 45 sweeps, and no
    reason to need a good unit in the box to do it."""
    pool = _load_calibration_pool()
    if pool is None:
        log("Nothing to recompute: no usable calibration.json with a stored pool.")
        return False
    scores, ref_totals, ref_matches, dut_totals = pool
    log(f"Recomputing thresholds from {len(scores)} stored sweeps (no recording).")
    return _derive_and_save(scores, ref_totals, ref_matches, dut_totals)


def run_calibration(cards, add=False):
    """Fire N_CAL dual-capture sweeps against a known-good mic seated as the
    DUT, score each against the reference (analysis.score_dut), and persist
    reference-relative thresholds (analysis.compute_calibration) plus
    reference-health floors derived from the good reference readings to
    CALIBRATION_FILE.

    Each sweep that played/recorded is additionally gated on
    analysis.reference_healthy (against UNCALIBRATED_REF_FLOORS -- the only
    floors that exist pre-calibration) before being accepted into the
    good-sweep pool. A marginal/dead reference during calibration would
    otherwise poison both the derived thresholds (ratios use ref_bands as the
    denominator) and ref_floors itself (also derived from that same bad
    reference's readings).

    Requires at least CAL_MIN_GOOD_SWEEPS sweeps that both played/recorded
    successfully and passed the reference-health gate: analysis.
    compute_calibration takes min()/max() over the score list and divides by
    the margins, so empty/near-empty input raises (Task 4 note) -- below the
    guard this logs why and fails without writing the file. Returns True on
    success (file written), False on any failure. Does not touch LEDs/exit
    codes itself -- main() blinks green and returns 0/2 based on this return
    value, same as the flow it replaces."""
    prior_scores, prior_ref_totals, prior_ref_matches, prior_dut_totals = [], [], [], []
    if add:
        prior = _load_calibration_pool()
        if prior is None:
            log("Cannot add to calibration: no usable calibration.json yet. "
                "Run a normal --calibrate first.")
            return False
        prior_scores, prior_ref_totals, prior_ref_matches, prior_dut_totals = prior
        log(f"Adding to the existing calibration ({len(prior_scores)} sweeps already "
            f"pooled, presumably from other units). Seat the NEXT known-good unit.")

    log(f"Calibrating from {N_CAL} sweeps, recorded back to back.")
    log("Seat a known-good unit properly, close the lid, and LEAVE IT. The "
        "enclosure holds position well enough that reseating moves the score "
        "about 2% (bench, 2026-08-14); the margins absorb that. Waving the "
        "unit around during calibration teaches the station that a badly "
        "seated unit is acceptable.")

    sweeps = collect_sweeps(cards, N_CAL)
    if len(sweeps) < N_CAL:
        log(f"  {N_CAL - len(sweeps)} of {N_CAL} sweeps failed to play/record")

    scores = []
    ref_totals = []
    ref_matches = []
    dut_totals = []
    for i, sweep in enumerate(sweeps):
        ok, reason = analysis.reference_healthy(sweep["ref_bands"], sweep["ref_match"], UNCALIBRATED_REF_FLOORS)
        if not ok:
            log(f"calibration sweep rejected, reference unhealthy: {reason}")
            continue
        score = analysis.score_dut(sweep["dut_bands"], sweep["dut_match"])
        scores.append(score)
        ref_totals.append(sweep["ref_bands"]["total"])
        ref_matches.append(sweep["ref_match"])
        dut_totals.append(sweep["dut_bands"]["total"])
        log(f"  sweep {i + 1}/{len(sweeps)}: level={score['level']:.3e} "
            f"tilt={score['tilt']:.3f} match={score['match']:.2f} "
            f"ref_total={sweep['ref_bands']['total']:.3e}")

    scores = prior_scores + scores
    ref_totals = prior_ref_totals + ref_totals
    ref_matches = prior_ref_matches + ref_matches
    dut_totals = prior_dut_totals + dut_totals
    if add:
        log(f"Pooled {len(scores)} sweeps across all units calibrated so far.")

    if len(scores) < CAL_MIN_GOOD_SWEEPS:
        log(f"Calibration FAILED: only {len(scores)}/{N_CAL} good sweeps "
            f"(need >= {CAL_MIN_GOOD_SWEEPS}). Is a known-good mic seated, "
            f"and the reference healthy?")
        return False

    return _derive_and_save(scores, ref_totals, ref_matches, dut_totals)


def play_result(card, passed):
    if passed:
        generate_result_tone(RESULT_TONE_FILE, PASS_BEEP_FREQ, RESULT_REPEATS_PASS)
    else:
        generate_result_tone(RESULT_TONE_FILE, FAIL_BEEP_FREQ, RESULT_REPEATS_FAIL)
    try:
        subprocess.run(["aplay", "-D", f"plughw:{card},0", RESULT_TONE_FILE], timeout=10)
    except Exception:
        pass


def _sweep_wav_paths(index):
    """Per-sweep capture filenames. Batch capture needs every sweep on disk at
    once, so they cannot share one fixed pair of names."""
    base, ext = os.path.splitext(DUT_RECORDING_FILE)
    rbase, rext = os.path.splitext(REF_RECORDING_FILE)
    return f"{base}_{index}{ext}", f"{rbase}_{index}{rext}"


def analyze_sweep_files(paths):
    """Measure one already-captured sweep. Takes a (dut_wav, ref_wav) tuple so
    it stays picklable -- collect_sweeps hands this to a process pool.

    Returns {'dut_bands','dut_match','dut_peak','ref_bands','ref_match'} built
    from analysis' onset-detection, band-power, and waveform-match primitives
    -- 'dut_peak' is the max abs of the float-normalized DUT samples (i.e. in
    [0,1], comparable to thresholds['max_peak']). Returns None if either WAV
    comes back unreadable/empty."""
    dut_wav, ref_wav = paths
    dut_samples, dut_rate = analysis.load_wav_float(dut_wav)
    ref_samples, ref_rate = analysis.load_wav_float(ref_wav)
    if not dut_samples or not ref_samples:
        log("DUT or reference recording unreadable/empty")
        return None

    # The reference records at 48kHz for fidelity, but the analysis below is
    # O(n) pure Python and the chirp tops out at SWEEP_END_HZ. Decimating to
    # the DUT's rate costs nothing measurable and saves ~3x the work.
    if ref_rate > SAMPLE_RATE and ref_rate % SAMPLE_RATE == 0:
        ref_samples, ref_rate = analysis.decimate(ref_samples, ref_rate, ref_rate // SAMPLE_RATE)

    dut_onset = analysis.find_sweep_onset(dut_samples, dut_rate, REFERENCE_EXPECTED_SEC,
                                          SWEEP_DURATION_SEC, search_sec=ONSET_SEARCH_SEC)
    ref_onset = analysis.find_sweep_onset(ref_samples, ref_rate, REFERENCE_EXPECTED_SEC,
                                          SWEEP_DURATION_SEC, search_sec=ONSET_SEARCH_SEC)

    dut_bands = analysis.recording_bands(dut_samples, dut_rate, dut_onset, SWEEP_DURATION_SEC)
    ref_bands = analysis.recording_bands(ref_samples, ref_rate, ref_onset, SWEEP_DURATION_SEC)
    dut_match = analysis.waveform_match(dut_samples, dut_rate, dut_onset, SWEEP_DURATION_SEC)
    ref_match = analysis.waveform_match(ref_samples, ref_rate, ref_onset, SWEEP_DURATION_SEC)
    dut_peak = max(abs(s) for s in dut_samples)

    return {
        "dut_bands": dut_bands,
        "dut_match": dut_match,
        "dut_peak": dut_peak,
        "ref_bands": ref_bands,
        "ref_match": ref_match,
    }


def run_one_sweep(cards):
    """Capture one sweep and measure it. Returns None if playback/recording
    failed or the recordings are unusable. Used where sweeps must be judged
    one at a time (calibration gates each sweep on reference health before
    accepting it); the per-unit test path uses collect_sweeps instead."""
    dut_wav, ref_wav = _sweep_wav_paths(0)
    if not capture_sweep(cards, dut_wav, ref_wav):
        return None
    return analyze_sweep_files((dut_wav, ref_wav))


def capture_sweep(cards, dut_wav, ref_wav):
    """One dual capture to the given files. True on success."""
    generate_chirp_wav(TEST_TONE_FILE, SWEEP_START_HZ, SWEEP_END_HZ,
                       SWEEP_DURATION_SEC, SAMPLE_RATE)
    return play_and_record_dual(cards["play"], cards["dut"], cards["ref"], cards["ref_kind"],
                                TEST_TONE_FILE, dut_wav, ref_wav)


def collect_sweeps(cards, n):
    """Capture n sweeps back to back, then measure them across the Pi's cores.

    Capture is IO-bound (arecord/aplay) and the measurement is CPU-bound pure
    Python, and the sweeps are independent of each other -- so doing all the
    recording first lets the analysis fan out over a process pool instead of
    running one sweep at a time on one core. Measured on kruu-mictest-01 (4
    cores, 3 sweeps = 6 channel jobs): analysis 24.07s sequential vs 9.45s
    pooled, byte-identical results, taking a run from ~30.5s to ~15.9s.

    Returns the list of successfully measured sweeps (may be shorter than n)."""
    captured = []
    for i in range(n):
        dut_wav, ref_wav = _sweep_wav_paths(i)
        if capture_sweep(cards, dut_wav, ref_wav):
            captured.append((dut_wav, ref_wav))

    if not captured:
        return []
    if len(captured) == 1:
        return [s for s in [analyze_sweep_files(captured[0])] if s is not None]

    try:
        with multiprocessing.Pool(processes=min(ANALYSIS_WORKERS, len(captured))) as pool:
            measured = pool.map(analyze_sweep_files, captured)
    except OSError as e:
        # A pool needs to fork; if that fails (memory pressure, ulimits) the
        # verdict still matters more than the speed.
        log(f"Parallel analysis unavailable ({e}); measuring sequentially")
        measured = [analyze_sweep_files(p) for p in captured]
    return [s for s in measured if s is not None]


def evaluate_run(cards, thresholds, ref_floors):
    """Run N_SWEEPS dual captures and render a verdict.

    Takes the median of each metric across sweeps, so one noisy capture can't
    swing the result. Gates on reference health FIRST -- a bad rig/stimulus is
    always a station error, never blamed on the DUT -- then applies
    analysis.evaluate_dut. Returns (status, detail): status is one of 'pass',
    'fail', 'station_error'; detail is a human-readable string for logging
    ('ok' for pass, the failing reasons joined together for fail, or the
    station-error reason)."""
    sweeps = collect_sweeps(cards, N_SWEEPS)
    if not sweeps:
        return "station_error", "all sweeps failed to play/record"
    if len(sweeps) < N_SWEEPS:
        log(f"Only {len(sweeps)}/{N_SWEEPS} sweeps recorded cleanly; judging on those")

    def med(values):
        return statistics.median(values)

    dut_bands = {k: med([s["dut_bands"][k] for s in sweeps]) for k in ("low", "high", "total")}
    ref_bands = {k: med([s["ref_bands"][k] for s in sweeps]) for k in ("low", "high", "total")}
    dut_match = med([s["dut_match"] for s in sweeps])
    ref_match = med([s["ref_match"] for s in sweeps])
    dut_peak = med([s["dut_peak"] for s in sweeps])

    # Who was quiet: this unit's speaker, this unit's mic, or the rig? Only
    # the last is a station fault. A dead speaker used to land here as a
    # STATION_ERROR, blaming the rig for a defect in the unit.
    verdict, reason = analysis.attribute_silence(dut_bands, ref_bands, ref_match, ref_floors)
    if verdict == "rig":
        return "station_error", reason
    if verdict in ("speaker", "mic"):
        return "fail", reason

    score = analysis.score_dut(dut_bands, dut_match)
    ok, reasons = analysis.evaluate_dut(score, dut_peak, thresholds)
    return ("pass", "ok") if ok else ("fail", "; ".join(reasons))


def _capture_only(card, out_wav, fmt, rate, seconds=AMBIENT_CAPTURE_SEC):
    """Record `seconds` of audio on `card` with nothing playing -- used for
    the --selftest ambient/room-noise reading. Returns True on success."""
    try:
        result = subprocess.run([
            "arecord", "-D", f"plughw:{card},0",
            "-f", fmt, "-r", str(rate), "-c", "1",
            "-d", str(math.ceil(seconds)), out_wav,
        ], capture_output=True, text=True, timeout=seconds + 5)
        if result.returncode != 0:
            log(f"Ambient capture on card {card} failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        log(f"Ambient capture on card {card} failed: {e}")
        return False


def _ambient_level(card, out_wav, fmt, rate):
    """Capture + measure one ambient (no-playback) window on `card`.
    Returns (peak, rms) floats in [0,1] (see analysis.load_wav_float's
    normalization), or None if the capture/read failed or came back empty."""
    if not _capture_only(card, out_wav, fmt, rate):
        return None
    samples, _rate = analysis.load_wav_float(out_wav)
    if not samples:
        return None
    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return peak, rms


def run_selftest():
    """On-site paste-back diagnostic (design spec Sec.9): prints detected
    cards + each capture card's USB VID:PID + which resolved as DUT/ref, an
    ambient (no-playback) level so the operator can see room noise, and one
    dry-run (a single run_one_sweep for the metrics/reference-health, plus a
    full evaluate_run for the final pass/fail/station_error verdict).

    Always returns 0 -- this is a diagnostic, not a pass/fail test, and it's
    meant to be run by a non-expert on an unreachable box, so it also never
    lets an internal error escape as a crash/traceback with no output.

    Takes over the pid file first, same as main() -- selftest captures audio
    on the single shared USB device and writes the same temp WAV paths a
    normal run/calibration uses, so a deliberate --selftest must claim
    exclusive ownership and supersede any in-flight automatic run rather than
    race it for the device/files."""
    try:
        log("=== SELFTEST ===")
        # Claim exclusive ownership before touching any card/audio state: a
        # deliberate --selftest supersedes an in-flight udev-triggered run or
        # button-triggered calibration, exactly as main() does for its entry
        # points.
        take_over_pid_file()

        cards = devices.select_cards(PRODUCT_VIDPID)
        log(f"select_cards({PRODUCT_VIDPID}): play={cards['play']} dut={cards['dut']} "
            f"ref={cards['ref']} ref_kind={cards['ref_kind']}")
        for role in ("dut", "ref"):
            card = cards[role]
            if card is None:
                log(f"  {role}: not present")
                continue
            vidpid = devices.card_usb_vidpid(card)
            log(f"  {role}: card={card} usb_vidpid={vidpid or '(none -- non-USB/I2S)'}")

        log("--- Ambient level (no playback) ---")
        if cards["dut"] is not None:
            level = _ambient_level(cards["dut"], AMBIENT_DUT_FILE, "S16_LE", SAMPLE_RATE)
            if level:
                log(f"  DUT:  peak={level[0]:.4f} rms={level[1]:.4f}")
            else:
                log("  DUT:  capture failed or empty")
        else:
            log("  DUT:  skipped (no DUT card)")

        if cards["ref"] is not None:
            ref_fmt, ref_rate = ("S32_LE", 48000) if cards["ref_kind"] == "i2s" else ("S16_LE", SAMPLE_RATE)
            level = _ambient_level(cards["ref"], AMBIENT_REF_FILE, ref_fmt, ref_rate)
            if level:
                log(f"  REF:  peak={level[0]:.4f} rms={level[1]:.4f}")
            else:
                log("  REF:  capture failed or empty")
        else:
            log("  REF:  skipped (no reference card)")

        log("--- Dry run ---")
        if cards["play"] is None or cards["dut"] is None or cards["ref"] is None:
            log("  skipped: need play + DUT + reference cards all present for a dry run")
        else:
            lock_card_gains(cards["play"], cards["dut"], cards["ref"])
            thresholds, ref_floors = load_thresholds()

            sweep = run_one_sweep(cards)
            if sweep is None:
                log("  single sweep: play/record failed")
            else:
                score = analysis.score_dut(sweep["dut_bands"], sweep["dut_match"])
                healthy, reason = analysis.reference_healthy(sweep["ref_bands"], sweep["ref_match"], ref_floors)
                log(f"  single sweep: level={score['level']:.3e} "
                    f"tilt={score['tilt']:.3f} match={score['match']:.3f} "
                    f"dut_peak={sweep['dut_peak']:.3f}")
                log(f"  reference_healthy={healthy} ({reason})")

            status, detail = evaluate_run(cards, thresholds, ref_floors)
            log(f"  evaluate_run ({N_SWEEPS} sweeps): {status.upper()} - {detail}")

        log("=== SELFTEST DONE ===")
    except Exception:
        log("SELFTEST CRASHED:\n" + traceback.format_exc())
    finally:
        for f in [TEST_TONE_FILE, DUT_RECORDING_FILE, REF_RECORDING_FILE, AMBIENT_DUT_FILE, AMBIENT_REF_FILE]:
            try:
                os.remove(f)
            except OSError:
                pass
    return 0


def run_button_daemon():
    """Watch the button forever; runs as its own systemd service.

    Only a deliberate hold of BUTTON_HOLD_SEC calibrates; a short press does
    nothing (unplug and replug a unit to retest it). The calibration launch's
    pid-file takeover kills any previous run, so it also clears a stuck/
    flashing error."""
    if not GPIO_AVAILABLE:
        log("FATAL: RPi.GPIO not available, button daemon cannot run")
        return 2

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    setup_gpio()  # also drive the LEDs, for the "hold armed" cue
    blink_ready()  # both LEDs blink: station is up and ready to test
    log(f"Button daemon up (GPIO {GPIO_BUTTON_PIN}): "
        f"hold {BUTTON_HOLD_SEC:.0f}s to calibrate")

    poll = 0.05
    child = None
    while True:
        time.sleep(poll)
        if GPIO.input(GPIO_BUTTON_PIN):  # pulled up = not pressed
            continue

        # Button is down: time it until release. Only once the hold threshold
        # is crossed do we light both LEDs (cue: release now to calibrate) and
        # arm calibration. A shorter press falls through and does nothing.
        held = 0.0
        armed = False
        while not GPIO.input(GPIO_BUTTON_PIN):
            time.sleep(poll)
            held += poll
            if held >= BUTTON_HOLD_SEC and not armed:
                armed = True
                set_leds(True, True)
        set_leds(False, False)

        if not armed:
            continue

        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        log("Button held: starting calibration")
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--calibrate"])


def main():
    """Run the test and return the process exit code.

    0 = PASS, and also idle/ready when only the reference mic is present (no
    DUT seated yet -- e.g. at boot; not an error). 1 = FAIL (a normal test
    outcome). 2 = station error: no playback card, no reference card, an
    unhealthy reference (bad rig/stimulus -- never blamed on the DUT), a
    crash, or failed calibration. The service treats 1 as success so only
    station errors show up as systemd failures; exit 2 also makes the
    launcher flash red until the next run clears it."""
    start = time.time()
    calibrate = "--calibrate" in sys.argv
    log("USB audio test start" + (" (CALIBRATE)" if calibrate else ""))

    try:
        take_over_pid_file()
        setup_gpio()
        set_leds(False, False)

        if calibrate:
            # Reference-relative calibration: needs the same play/DUT/ref
            # trio as a normal run (a known-good mic seated where the DUT
            # goes) so run_calibration can score it against the reference.
            cards = devices.select_cards(PRODUCT_VIDPID)
            log(f"Cards: play={cards['play']} dut={cards['dut']} "
                f"ref={cards['ref']} ({cards['ref_kind']})")
            if cards["play"] is None or cards["dut"] is None or cards["ref"] is None:
                log("FATAL: calibration needs playback + DUT + reference cards, "
                    "one is missing!")
                set_leds(False, True)
                return 2
            lock_card_gains(cards["play"], cards["dut"], cards["ref"])
            if run_calibration(cards, add="--add" in sys.argv):
                log("Calibration complete.")
                signal_calibration_saved(cards["play"])
                return 0
            signal_calibration_failed(cards["play"])
            return 2

        cards = devices.select_cards(PRODUCT_VIDPID)
        log(f"Cards: play={cards['play']} dut={cards['dut']} "
            f"ref={cards['ref']} ({cards['ref_kind']})")

        if cards["play"] is None:
            log("FATAL: no playback card found!")
            set_leds(False, True)
            return 2

        if cards["ref"] is None:
            log("FATAL: reference mic not found!")
            set_leds(False, True)
            return 2

        if cards["dut"] is None:
            # Only the reference is enumerated (e.g. at boot, before a unit
            # is seated in the box) -- idle/ready, not an error.
            log("No DUT present, idle")
            return 0

        lock_card_gains(cards["play"], cards["dut"], cards["ref"])

        thresholds, ref_floors = load_thresholds()
        status, detail = evaluate_run(cards, thresholds, ref_floors)

        elapsed = time.time() - start
        log(f"Result: {status.upper()} ({elapsed:.1f}s) - {detail}")

        if status == "pass":
            set_leds(True, False)
            play_result(cards["play"], True)
            return 0
        if status == "fail":
            set_leds(False, True)
            play_result(cards["play"], False)
            return 1

        # station_error: rig/stimulus fault, not a DUT verdict -- flashing
        # red only, no beep (a beep implies a completed test of the DUT).
        set_leds(False, True)
        return 2

    except Exception:
        # Without this a crash would exit with the LEDs dark and nothing in
        # the journal to explain it.
        log("CRASHED:\n" + traceback.format_exc())
        set_leds(False, True)
        return 2
    finally:
        leftovers = [TEST_TONE_FILE, DUT_RECORDING_FILE, REF_RECORDING_FILE, RESULT_TONE_FILE]
        for i in range(max(N_SWEEPS, N_CAL)):
            leftovers.extend(_sweep_wav_paths(i))
        for f in leftovers:
            try:
                os.remove(f)
            except OSError:
                pass


if __name__ == "__main__":
    if "--button-daemon" in sys.argv:
        sys.exit(run_button_daemon())
    if "--recompute" in sys.argv:
        sys.exit(0 if recompute_calibration() else 2)
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    code = main()
    if code == 2:
        flash_red_until_cleared()
    sys.exit(code)
