#!/usr/bin/env python3
"""ALSA card discovery + USB VID/PID identification for the MiLa rig.
Copyright (c) 2026 KRUU US INC. All rights reserved."""
import os, subprocess

def _cards(tool, run):
    try:
        res = run([tool, "-l"], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    out = []
    for line in res.stdout.splitlines():
        low = line.lower()
        if low.startswith("card "):
            tok = line.split(":")[0].split()[-1]
            if tok.isdigit():
                out.append(int(tok))
    return out

def list_capture_cards(run=subprocess.run):
    return _cards("arecord", run)

def list_playback_cards(run=subprocess.run):
    return _cards("aplay", run)

def card_usb_vidpid(card, sysfs_root="/sys/class/sound"):
    start = os.path.join(sysfs_root, f"card{card}", "device")
    try:
        path = os.path.realpath(start)
    except Exception:
        return None
    for _ in range(8):  # walk up toward the USB device node
        v = os.path.join(path, "idVendor"); p = os.path.join(path, "idProduct")
        if os.path.isfile(v) and os.path.isfile(p):
            try:
                with open(v) as fv, open(p) as fp:
                    return f"{fv.read().strip().lower()}:{fp.read().strip().lower()}"
            except Exception:
                return None
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None

def select_cards(product_vidpid, run=subprocess.run, sysfs_root="/sys/class/sound"):
    product_vidpid = product_vidpid.lower()
    playback = list_playback_cards(run)
    capture = list_capture_cards(run)
    dut = ref = None
    ref_kind = None
    others = []
    for c in capture:
        if card_usb_vidpid(c, sysfs_root) == product_vidpid and dut is None:
            dut = c
        else:
            others.append(c)
    # The MiLa product plays the test tone out of its OWN speaker -- the same
    # USB card that carries its mic -- and the reference mic judges that speaker
    # acoustically. So play through the DUT's card when it exposes a playback
    # endpoint. Falling back to "first playback card" (the Pi's onboard jack)
    # sends the chirp to a dead output: that is silence, not a test.
    if dut is not None and dut in playback:
        play = dut
    else:
        play = (playback or [None])[0]
    # reference: prefer a non-USB (I2S) card; else a USB card with a different VID:PID
    for c in others:
        if card_usb_vidpid(c, sysfs_root) is None:
            ref, ref_kind = c, "i2s"; break
    if ref is None:
        for c in others:
            v = card_usb_vidpid(c, sysfs_root)
            if v is not None and v != product_vidpid:
                ref, ref_kind = c, "usb"; break
    return {"play": play, "dut": dut, "ref": ref, "ref_kind": ref_kind}
