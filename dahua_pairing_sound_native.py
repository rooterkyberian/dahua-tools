#!/usr/bin/env python3
"""Generate the "sound wave" Wi-Fi pairing audio for a Dahua/Imou camera - no APK needed.

Pure-Python port of the encoder that dahua_pairing_sound.py runs in an emulator
(fsk_tx in libSmartConfig.so, transmit mode 1, which is what the app uses).
Standard library only.

The payload from dahua_smartconfig.build_payload() is sent a nibble at a time,
low nibble first, as one of 16 tones (17.0 kHz + 100 Hz * nibble):

    sync   18.6 kHz, 2352 samples
    byte   low nibble, high nibble   588 samples each (75 baud)
    gap    18.6 kHz, 294 samples     between bytes, not after the last one
    end    18.7 kHz, 4704 samples

at 44.1 kHz, 16 bit, mono. Every symbol starts at phase 0 and is shaped with a
half-sine window. Where the library puts its "cuckoo" jingle in front of the
data, this puts sounds/owl_hoot.wav (public domain; --no-hoot leaves it out).

Usage:
    dahua_pairing_sound_native.py --sn <SERIAL> [--out pair.wav] [--repeat N] [--no-hoot]
    dahua_pairing_sound_native.py --decode some.wav      # check a WAV, e.g. one from the emulator

The Wi-Fi password is taken from $WIFI_PSK, or else read from NetworkManager.
NOTE: the WAV encodes your Wi-Fi password. Delete it when you are done.
"""
import argparse
import array
import math
import os
import sys
import wave
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dahua_smartconfig as sc  # noqa: E402

RATE = 44100
BASE_HZ, STEP_HZ = 17000, 100
SYNC, END = 16, 17                       # tone indices above the 16 data tones
SYNC_LEN = RATE * 16 // 300              # 2352
NIBBLE_LEN, GAP_LEN, END_LEN = SYNC_LEN // 4, SYNC_LEN // 8, SYNC_LEN * 2
HALF_TURN = 1 << 31                      # phases are 32-bit, 2**32 per cycle
HOOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds", "owl_hoot.wav")


def hz(index: int) -> int:
    return BASE_HZ + STEP_HZ * index


def symbols(payload: bytes):
    """Yield (tone index, samples, window phase step) for the whole transmission."""
    per_sample = HALF_TURN // RATE
    yield SYNC, SYNC_LEN, HALF_TURN // SYNC_LEN
    for i, byte in enumerate(payload):
        if i:
            yield SYNC, GAP_LEN, 150 * per_sample
        yield byte & 15, NIBBLE_LEN, 75 * per_sample
        yield byte >> 4, NIBBLE_LEN, 75 * per_sample
    yield END, END_LEN, HALF_TURN // END_LEN


def _sin(phase: int) -> int:
    """Q15 sine of a 32-bit phase, scaled by 7/8 as the library does."""
    return int(32767 * math.sin(phase * (math.pi / HALF_TURN))) * 7 >> 3


def render(payload: bytes) -> array.array:
    if len(payload) < 2:
        raise ValueError("payload too short")
    pcm = array.array("h")
    for index, count, window_step in symbols(payload):
        tone_step = int(hz(index) * 2 ** 32 / RATE + 0.5)
        for k in range(count):
            pcm.append(_sin(k * tone_step & 0xFFFFFFFF) * _sin(k * window_step) >> 15)
    return pcm


def write_wav(path: str, pcm: array.array, repeat: int = 1, pause: float = 0.5, intro=None):
    """Write `repeat` copies of [intro +] pcm, each followed by `pause` seconds of silence."""
    pcm = (intro or array.array("h")) + pcm
    if sys.byteorder == "big":
        pcm.byteswap()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        for _ in range(repeat):
            w.writeframes(pcm.tobytes())
            w.writeframes(bytes(2 * int(RATE * pause)))


# --- decoder: only here to check a WAV (ours or the library's) against this description ---

def power(pcm, start: int, count: int, freq: float) -> float:
    """Goertzel: signal power at `freq` in pcm[start:start+count]."""
    coeff = 2 * math.cos(2 * math.pi * freq / RATE)
    s1 = s2 = 0.0
    for x in pcm[start:start + count]:
        s1, s2 = x + coeff * s1 - s2, s1
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def decode(pcm) -> bytes:
    """Recover the payload from 44.1 kHz mono samples. Raises ValueError if there is none."""
    sync_hz, end_hz = hz(SYNC), hz(END)
    blocks = [power(pcm, i, GAP_LEN, sync_hz) for i in range(0, len(pcm) - GAP_LEN, GAP_LEN)]
    width = SYNC_LEN // GAP_LEN
    if len(blocks) < width:
        raise ValueError("recording too short")
    coarse = max(range(len(blocks) - width + 1), key=lambda b: sum(blocks[b:b + width])) * GAP_LEN
    start = max(range(max(0, coarse - GAP_LEN), coarse + GAP_LEN, 14),
                key=lambda s: power(pcm, s, SYNC_LEN, sync_hz))
    pos = start + SYNC_LEN

    def nibble(at):
        return max(range(16), key=lambda t: power(pcm, at, NIBBLE_LEN, hz(t)))

    out = bytearray()
    while pos + 2 * NIBBLE_LEN <= len(pcm):
        out.append(nibble(pos) | nibble(pos + NIBBLE_LEN) << 4)
        pos += 2 * NIBBLE_LEN
        # Gap or end tone? Compare amplitude estimates; the end-tone window is the longest
        # that cannot reach the real end tone when another byte follows.
        ahead = GAP_LEN + 2 * NIBBLE_LEN
        if (math.sqrt(power(pcm, pos, ahead, end_hz)) / ahead
                > math.sqrt(power(pcm, pos, GAP_LEN, sync_hz)) / GAP_LEN):
            return bytes(out)
        pos += GAP_LEN
    raise ValueError("no end tone found")


def read_wav(path: str) -> array.array:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getsampwidth()) != (RATE, 2):
            sys.exit(f"{path}: need 16-bit {RATE} Hz audio")
        pcm = array.array("h", w.readframes(w.getnframes()))
        channels = w.getnchannels()
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm[::channels]


def describe(payload: bytes) -> str:
    """Summarise a decoded payload without printing the password."""
    try:
        if payload[0] != 0x10:
            raise ValueError
        fields, at = [], 2
        for _ in range(3):
            n = payload[at]
            fields.append(payload[at + 1:at + 1 + n])
            at += 1 + n
        ok = len(payload) == at + 4 and zlib.crc32(payload[:at]).to_bytes(4, "big") == payload[at:]
    except (IndexError, ValueError):
        return f"{len(payload)} bytes, not a pairing payload"
    ssid, psk, sn = fields
    return (f"ssid={ssid.decode(errors='replace')!r} sn={sn.decode(errors='replace')} security={payload[1]} "
            f"password={len(psk)} chars, CRC {'ok' if ok else 'BAD'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sn", help="camera serial number")
    ap.add_argument("--ssid", help="Wi-Fi name (default: the network this machine is on)")
    ap.add_argument("--security", default="wpa2-psk-ccmp", choices=sorted(sc.SECURITY))
    ap.add_argument("--out", default="pair.wav")
    ap.add_argument("--repeat", type=int, default=1, help="repetitions to put in the file (0.5 s apart)")
    ap.add_argument("--no-hoot", action="store_true", help="leave out the owl hoot in front of the data")
    ap.add_argument("--decode", metavar="WAV", help="decode a pairing WAV instead of writing one")
    a = ap.parse_args()

    if a.decode:
        try:
            print(describe(decode(read_wav(a.decode))))
        except ValueError as e:
            sys.exit(f"{a.decode}: {e}")
        return
    if not a.sn:
        ap.error("--sn is required")

    ssid, psk = sc.wifi_credentials(a.ssid, a.security)
    pcm = render(sc.build_payload(a.sn, ssid, psk, sc.SECURITY[a.security]))
    intro = None if a.no_hoot else read_wav(HOOT)
    write_wav(a.out, pcm, a.repeat, intro=intro)
    print(f"wrote {a.out} ({(len(pcm) + len(intro or ())) / RATE:.1f} s x {a.repeat}) - play it next to the camera, then delete it")


if __name__ == "__main__":
    main()
