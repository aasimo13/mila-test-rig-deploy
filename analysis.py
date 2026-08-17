#!/usr/bin/env python3
"""Pure DSP + decision core for the MiLa test rig. No hardware/IO side effects.
Aaron Simo, with Claude (Anthropic). Copyright (c) 2026 KRUU US INC. All rights reserved."""
import math, wave, array

SWEEP_START_HZ = 300
SWEEP_END_HZ = 4000
LOW_BAND = (400, 900)
HIGH_BAND = (2000, 3800)
_COMB_SPACING_HZ = 25
# waveform_match decimates to about this rate for its coarse pass only: low
# enough that the chirp's correlation peak widens into something a coarse grid
# cannot skip, high enough to still carry most of the sweep. Full-rate samples
# do the actual precision work afterwards.
_COARSE_SEARCH_HZ = 4000

def load_wav_float(path):
    try:
        with wave.open(path, "r") as wf:
            n, sw, ch, rate = wf.getnframes(), wf.getsampwidth(), wf.getnchannels(), wf.getframerate()
            raw = wf.readframes(n)
    except Exception:
        return [], 0
    if n == 0:
        return [], 0
    if sw == 2:
        a = array.array("h"); full = 32768.0
    elif sw == 4:
        a = array.array("i"); full = 2147483648.0
    else:
        return [], 0
    a.frombytes(raw)
    if ch > 1:
        a = a[0::ch]
    return [s / full for s in a], rate

def decimate(samples, rate, factor):
    """Boxcar-average blocks of `factor` samples, then keep one per block.
    Every downstream analysis call is O(n) pure Python with no numpy, so a
    48kHz I2S reference costs 3x what a 16kHz one does (kruu-mictest-01:
    waveform_match on a 48kHz reference measured 9.91s vs 1.99s at 16kHz for
    the same 1.2s chirp). The stimulus never exceeds SWEEP_END_HZ, so nothing
    downstream needs the extra bandwidth -- averaging (not a bare `[::factor]`
    stride) rolls off content above the new Nyquist first, so it doesn't
    alias noise back into the analysis band and erode the reference-match
    margin the lag-window fix above exists to protect."""
    if factor <= 1:
        return samples, rate
    n = len(samples) // factor
    out = [sum(samples[i*factor:(i+1)*factor]) / factor for i in range(n)]
    return out, rate // factor

def generate_chirp_float(f_start, f_end, duration, rate):
    n = int(rate * duration)
    k = math.log(f_end / f_start) / duration
    out = []
    for i in range(n):
        t = i / rate
        out.append(math.sin(2.0*math.pi*f_start*(math.exp(t*k)-1.0)/k))
    return out

def _goertzel_power(samples, freq, rate):
    n = len(samples)
    if n == 0:
        return 0.0
    w = 2.0*math.pi*(int(0.5 + n*freq/rate))/n
    coeff = 2.0*math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff*s1 - s2
        s2, s1 = s1, s0
    return s2*s2 + s1*s1 - coeff*s1*s2

def band_power(samples, f_lo, f_hi, rate):
    n = len(samples)
    if n == 0:
        return 0.0
    total = 0.0
    f = f_lo
    while f <= f_hi:
        total += max(_goertzel_power(samples, f, rate), 0.0)
        f += _COMB_SPACING_HZ
    return total / (n * n)   # normalize: rate/window independent, comparable across recordings

def gated(samples, onset, length):
    onset = max(0, onset)
    window = samples[onset:onset+length]
    if len(window) < length:
        window = window + [0.0]*(length - len(window))
    return window

def _corr_at_onset(samples, template, onset, length):
    """Normalized cross-correlation of samples[onset:onset+length] against the
    template. The whole chirp shape only lines up at the true onset, so noise
    (which does not correlate) cannot win -- this is what makes onset location
    robust on a noisy reference."""
    lo = max(0, onset)
    hi = min(len(samples), onset + length)
    if hi - lo < 2:
        return -1.0
    num = den_a = den_b = 0.0
    for i in range(lo, hi):
        a = samples[i]; b = template[i - onset]
        num += a*b; den_a += a*a; den_b += b*b
    if den_a <= 0.0 or den_b <= 0.0:
        return 0.0
    return num / math.sqrt(den_a*den_b)

def find_sweep_onset(samples, rate, expected_sec, sweep_dur, search_sec=0.10):
    # Correlation, not an energy threshold. The reference has a high broadband
    # noise floor and the chirp opens quietly at 300Hz, so an energy-rise
    # detector locks onto noise instead of the chirp.
    #
    # 1ms coarse step, not 5ms: the correlation peak is 1-2ms wide and a
    # coarser grid straddles it, anchoring several ms off and eating into
    # waveform_match's lag budget.
    length = int(rate*sweep_dur)
    if length <= 0 or not samples:
        return int(rate*expected_sec)
    template = generate_chirp_float(SWEEP_START_HZ, SWEEP_END_HZ, sweep_dur, rate)
    template = template[:length] + [0.0]*max(0, length-len(template))
    expected = int(rate*expected_sec)
    lo = max(0, expected - int(rate*search_sec))
    hi = min(max(lo, len(samples)-1), expected + int(rate*search_sec))
    coarse = max(1, int(rate*0.001))
    best_on, best_c = expected, _corr_at_onset(samples, template, expected, length)
    on = lo
    while on <= hi:
        c = _corr_at_onset(samples, template, on, length)
        if c > best_c:
            best_c, best_on = c, on
        on += coarse
    fstep = max(1, int(rate*0.001))
    on = max(lo, best_on - coarse)
    fine_hi = min(hi, best_on + coarse)
    while on <= fine_hi:
        c = _corr_at_onset(samples, template, on, length)
        if c > best_c:
            best_c, best_on = c, on
        on += fstep
    return max(0, min(best_on, max(0, len(samples)-1)))

def recording_bands(samples, rate, onset, sweep_dur):
    window = gated(samples, onset, int(rate*sweep_dur))
    low = band_power(window, *LOW_BAND, rate)
    high = band_power(window, *HIGH_BAND, rate)
    return {"low": low, "high": high, "total": low + high}

def _lagged_corr(window, template, length, lag):
    # window[i] corresponds to template[i - lag]; lag>0 delays the template so it
    # lines back up with a window whose onset was detected early (see waveform_match).
    lo, hi = max(0, lag), min(length, length + lag)
    if lo >= hi:
        return 0.0
    num = den_a = den_b = 0.0
    for i in range(lo, hi):
        a, b = window[i], template[i - lag]
        num += a*b; den_a += a*a; den_b += b*b
    if den_a <= 0.0 or den_b <= 0.0:
        return 0.0
    return num / math.sqrt(den_a*den_b)

def waveform_match(samples, rate, onset, sweep_dur):
    # Absorbs find_sweep_onset's error, which goes both ways, so the range is
    # symmetric. +-15ms is ~1.5x the worst onset deviation measured on the
    # current enclosure (+5..+10ms over 20 reps). Each ms costs per-sweep time;
    # widen it again if a capture ever misses by more than that.
    length = int(rate*sweep_dur)
    window = gated(samples, onset, length)
    template = generate_chirp_float(SWEEP_START_HZ, SWEEP_END_HZ, sweep_dur, rate)
    template = template[:length] + [0.0]*max(0, length-len(template))

    max_lag = int(rate*0.015)
    min_lag = -max_lag

    # The chirp's correlation peak is about 1/bandwidth wide, under half a
    # millisecond, so a coarse grid can straddle it entirely and lock onto
    # noise instead. Peak width scales inversely with bandwidth, so find the
    # neighbourhood on a heavily decimated copy where the peak is wide, then
    # refine at full rate. Same answer as scanning everything finely, ~3x less
    # work. Do not coarsen the fine step: skipping the peak scored real
    # captures 0.13 that an exhaustive search scored 0.43.
    dec = max(1, rate // _COARSE_SEARCH_HZ)
    if dec > 1:
        dwindow, drate = decimate(window, rate, dec)
        dtemplate, _ = decimate(template, rate, dec)
        dlength = min(len(dwindow), len(dtemplate))
        dmax_lag = max(1, int(drate*0.015))
        dstep = max(1, int(drate*0.0005))
        best_dlag, best_dc = 0, -2.0
        lag = -dmax_lag
        while lag <= dmax_lag:
            c = _lagged_corr(dwindow, dtemplate, dlength, lag)
            if c > best_dc:
                best_dc, best_dlag = c, lag
            lag += dstep
        center = best_dlag * dec
        span = dec * dstep * 2       # cover the decimated grid's own uncertainty
    else:
        center, span = 0, max_lag

    fine_step = max(1, int(rate*0.00005))
    lo, hi = max(min_lag, center - span), min(max_lag, center + span)
    best_lag, best_corr = center, -2.0
    lag = lo
    while lag <= hi:
        c = _lagged_corr(window, template, length, lag)
        if c > best_corr:
            best_corr, best_lag = c, lag
        lag += fine_step

    for lag in range(max(min_lag, best_lag - fine_step),
                     min(max_lag, best_lag + fine_step) + 1):
        c = _lagged_corr(window, template, length, lag)
        if c > best_corr:
            best_corr, best_lag = c, lag

    return max(0.0, min(1.0, best_corr))

def score_dut(dut_bands, dut_match):
    """Judge the unit's mic on its OWN recording. Nothing here is divided by
    the reference.

    These used to be ratios against the reference, to cancel speaker-output
    variation between units. That backfired: the unit plays the chirp through
    its own speaker, so the reference's reading carries that unit's SPEAKER
    characteristics, and dividing imported them into the mic's verdict. Two
    known-good units on kruu-mictest-01 had near-identical mics (dut_total 4%
    apart) but speakers 1.24x apart, which pushed the divided hf metric 2x
    apart and failed the second unit as "muffled" against thresholds
    calibrated on the first.

    The unit's mic sits a fixed distance from its own speaker inside the
    product, so its own reading is stable across seatings and across units --
    which is what makes it the right thing to threshold. The speaker is
    judged separately, by the reference (see attribute_silence and
    reference_healthy), which is what the reference is actually for."""
    low = dut_bands["low"]
    return {
        "level": dut_bands["total"],
        "tilt": (dut_bands["high"] / low) if low > 0 else 0.0,
        "match": dut_match,
    }

def attribute_silence(dut_bands, ref_bands, ref_match, floors):
    """Work out WHAT was quiet: the unit's speaker, the unit's mic, or the rig.

    The unit under test is both the sound source and one of the two
    microphones, so a quiet reference on its own is ambiguous -- either this
    unit's speaker produced nothing, or the rig has stopped hearing. The
    unit's own mic settles it: it sits a fixed distance from its own speaker,
    so if it picked the chirp up then sound was definitely made.

        reference heard it + mic heard it   -> ok      (test the details)
        reference heard it + mic silent     -> mic     (speaker proven good)
        both silent                         -> speaker (no sound was produced)
        reference silent + mic heard it     -> rig     (deaf reference)

    Returns (verdict, reason). Only 'rig' is a station fault; 'mic' and
    'speaker' are defects in the unit and must fail it rather than blaming
    the rig, which is what every quiet reference used to do.

    Calibrations written before min_dut_total existed do not carry a floor for
    the unit's mic, and the reference's floor is NOT a stand-in for it -- the
    two mics sit at very different absolute levels (on kruu-mictest-01 the
    reference reads ~3.7x hotter), so borrowing it fails good units as
    'dead mic'. Without a calibrated floor this declines to judge the unit's
    mic at all and falls back to reference-only gating; recalibrate to get
    the full attribution."""
    ref_heard = ref_bands["total"] >= floors["min_total"] and ref_match >= floors["min_match"]

    dut_floor = floors.get("min_dut_total")
    if dut_floor is None:
        if ref_heard:
            return "ok", "ok"
        return "rig", (f"the reference heard no clean chirp (total={ref_bands['total']:.3e}, "
                       f"match={ref_match:.2f}); recalibrate to tell a dead unit speaker "
                       f"from a rig fault")
    dut_heard = dut_bands["total"] >= dut_floor

    if ref_heard:
        if dut_heard:
            return "ok", "ok"
        # Reference heard it, so the speaker works and the mic is what failed.
        # A weak speaker is caught by ref_heard above, from the reference's
        # level. Do not try to infer it from the unit's own spectrum instead:
        # the same faulty unit measured tilt 0.42 and 0.111 on consecutive
        # insertions, while its reference level held to 1.13x.
        return "mic", (f"the unit's mic heard nothing (total={dut_bands['total']:.3e}) "
                       f"while the reference heard the chirp -- dead mic")
    if dut_heard:
        # The reference did not hear it but the unit did. Whether that is the
        # rig's fault turns on how MUCH the unit heard. Its mic sits a fixed
        # distance from its own speaker, so a working speaker gives a normal
        # reading wherever the unit sits; a low-but-present one means the unit
        # is under-producing and the fault is its speaker. The lost-driver unit
        # read 2.12e-06 against 6.65e-06 for a misseated good one.
        normal = floors.get("min_dut_normal")
        if normal is not None and dut_bands["total"] < normal:
            return "speaker", (f"the unit is producing sound but well under a good one "
                               f"(total={dut_bands['total']:.3e}, expected at least "
                               f"{normal:.3e}) and the reference barely heard it "
                               f"(total={ref_bands['total']:.3e}) -- weak speaker")
        # Seating first, hardware second. A misplaced unit drops what the
        # reference hears by more than 2x, which looks identical to a failed
        # reference from levels alone, and on the one real occurrence it WAS
        # the seating.
        return "rig", (f"the reference heard no clean chirp (total={ref_bands['total']:.3e}, "
                       f"match={ref_match:.2f}) but the unit's own mic did. "
                       f"Check the unit is seated properly first, then the reference mic. "
                       f"Not a verdict on this unit")
    return "speaker", (f"neither the reference nor the unit's own mic heard a chirp "
                       f"(ref total={ref_bands['total']:.3e}, unit total="
                       f"{dut_bands['total']:.3e}) -- the unit's speaker produced no sound")


def reference_healthy(ref_bands, ref_match, floors):
    if ref_bands["total"] < floors["min_total"]:
        return False, f"reference heard no/low chirp (total={ref_bands['total']:.3e})"
    if ref_match < floors["min_match"]:
        return False, f"reference chirp unclean (match={ref_match:.2f})"
    return True, "ok"

def evaluate_dut(score, dut_peak_float, thresholds):
    reasons = []
    if dut_peak_float >= thresholds["max_peak"]:
        reasons.append(f"clipping (peak={dut_peak_float:.2f})")
    if score["level"] < thresholds["min_level"]:
        reasons.append(f"level too low ({score['level']:.3e}), dead or weak")
    if score["level"] > thresholds["max_level"]:
        reasons.append(f"level too high ({score['level']:.3e})")
    if score["tilt"] < thresholds["min_tilt"]:
        reasons.append(f"no highs (tilt={score['tilt']:.3f}), muffled")
    if score["match"] < thresholds["min_match"]:
        reasons.append(f"waveform unclean (match={score['match']:.2f}), distorted")
    return (len(reasons) == 0), reasons

def default_thresholds():
    return {"min_level": 0.35, "max_level": 3.0, "min_tilt": 0.45,
            "min_match": 0.55, "max_peak": 0.98}

def _trimmed_low(values, trim):
    """Lowest value after discarding the bottom `trim` fraction."""
    s = sorted(values)
    return s[min(int(len(s)*trim), len(s)-1)]


def _trimmed_high(values, trim):
    s = sorted(values)
    return s[max(len(s)-1 - int(len(s)*trim), 0)]


def compute_calibration(good_scores, margins, trim=0.0):
    """Derive DUT thresholds from sweeps of a known-good unit.

    `trim` discards that fraction of the extremes at each end before taking
    the floor/ceiling. It exists for position-sampling calibration: when the
    unit is deliberately moved through a range of seatings during the run, the
    sample includes captures taken mid-movement and at the edge of usable
    placement, and a raw min() would hand the worst of those to every future
    unit as "acceptable" -- quietly widening the gate until bad units pass.
    Trimming keeps the thresholds anchored to the bulk of the positions.

    Default 0.0 is exactly min()/max(): the right choice when the unit is held
    in ONE good position and the spread is only measurement noise."""
    levels = [s["level"] for s in good_scores]
    hfs = [s["tilt"] for s in good_scores]
    matches = [s["match"] for s in good_scores]
    lo_level, hi_level = _trimmed_low(levels, trim), _trimmed_high(levels, trim)
    lo_hf, lo_match = _trimmed_low(hfs, trim), _trimmed_low(matches, trim)
    return {
        "min_level": lo_level*margins["level"],
        "max_level": hi_level/margins["level"],
        "min_tilt": lo_hf*margins["hf"],
        "min_match": lo_match*margins["match"],
        "max_peak": default_thresholds()["max_peak"],
        "observed": {"level": [lo_level, hi_level], "hf_min": lo_hf, "match_min": lo_match,
                     "level_full_range": [min(levels), max(levels)],
                     "match_full_range": [min(matches), max(matches)],
                     "trim": trim},
        "samples": len(good_scores),
    }
