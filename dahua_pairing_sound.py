#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["unicorn", "pyelftools"]
# ///
"""Generate the "sound wave" Wi-Fi pairing audio for a Dahua/Imou camera.

The DMSS app's libSmartConfig.so contains an FSK encoder that turns the same
payload used by dahua_smartconfig.py into a short WAV. Rather than port the
encoder, this runs the library's own createVoiceConfigFile() in the Unicorn
CPU emulator. You must supply the library yourself: unzip the DMSS APK (the
armeabi-v7a split) and take lib/armeabi-v7a/libSmartConfig.so.

Usage:
    dahua_pairing_sound.py --lib libSmartConfig.so --sn <SERIAL> --out pair.wav

The Wi-Fi password is taken from $WIFI_PSK, or else read from NetworkManager.
NOTE: the WAV encodes your Wi-Fi password. Delete it when you are done.
"""
import argparse
import math
import os
import struct
import sys

from elftools.elf.elffile import ELFFile
from unicorn import UC_ARCH_ARM, UC_HOOK_CODE, UC_MODE_THUMB, Uc
from unicorn.arm_const import (UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC, UC_ARM_REG_LR,
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_SP)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dahua_smartconfig as sc  # noqa: E402

BASE, TRAP, SCRATCH, HEAP, STACK = 0x1000000, 0x2000000, 0x2100000, 0x3000000, 0x8000000
BX_LR = struct.pack("<I", 0xE12FFF1E)
FREQ, TX_MODE, BG_MUSIC = 11000, 1, 1  # what the app passes


def render(lib_path: str, payload: bytes) -> bytes:
    mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    for addr, size in ((BASE, 0x40000), (TRAP, 0x1000), (SCRATCH, 0x10000),
                       (HEAP, 0x4000000), (STACK - 0x100000, 0x100000)):
        mu.mem_map(addr, size)

    raw = open(lib_path, "rb").read()
    elf = ELFFile(open(lib_path, "rb"))
    for seg in elf.iter_segments():
        if seg["p_type"] == "PT_LOAD":
            mu.mem_write(BASE + seg["p_vaddr"], raw[seg["p_offset"]:seg["p_offset"] + seg["p_filesz"]])

    def rd(a): return struct.unpack("<I", mu.mem_read(a, 4))[0]
    def wr(a, v): mu.mem_write(a, struct.pack("<I", v & 0xFFFFFFFF))
    def reg(i): return mu.reg_read(UC_ARM_REG_R0 + i)

    def cstr(a):
        out = bytearray()
        while (c := mu.mem_read(a, 1)[0]):
            out.append(c)
            a += 1
        return bytes(out)

    heap_top = [HEAP]
    written = bytearray()

    def malloc(n):
        p = heap_top[0]
        heap_top[0] += (n + 15) & ~15
        return p

    def calloc():
        n = reg(0) * reg(1)
        p = malloc(n)
        mu.mem_write(p, bytes(n))
        return p

    def memcpy():
        mu.mem_write(reg(0), bytes(mu.mem_read(reg(1), reg(2))))
        return reg(0)

    def memset():
        mu.mem_write(reg(0), bytes([reg(1) & 255]) * reg(2))
        return reg(0)

    def memclr():
        mu.mem_write(reg(0), bytes(reg(1)))
        return reg(0)

    def strcpy():
        mu.mem_write(reg(0), cstr(reg(1)) + b"\0")
        return reg(0)

    def fwrite():
        written.extend(mu.mem_read(reg(0), reg(1) * reg(2)))
        return reg(2)

    def double_arg():  # softfp ABI: double in r0:r1
        return struct.unpack("<d", struct.pack("<II", reg(0), reg(1)))[0]

    def double_ret(v):
        lo, hi = struct.unpack("<II", struct.pack("<d", v))
        mu.reg_write(UC_ARM_REG_R1, hi)
        return lo

    libc = {
        "malloc": lambda: malloc(reg(0)), "calloc": calloc, "free": lambda: 0,
        "memcpy": memcpy, "__memcpy_chk": memcpy, "memmove": memcpy,
        "memset": memset, "__memset_chk": memset, "__aeabi_memclr": memclr,
        "__strlen_chk": lambda: len(cstr(reg(0))), "__strcpy_chk": strcpy,
        "cos": lambda: double_ret(math.cos(double_arg())),
        "sin": lambda: double_ret(math.sin(double_arg())),
        "fopen": lambda: 1, "fwrite": fwrite, "fclose": lambda: 0, "fflush": lambda: 0,
        "__android_log_print": lambda: 0, "__cxa_atexit": lambda: 0, "__cxa_finalize": lambda: 0,
    }

    slots = {}

    def slot(name):
        if name not in slots:
            slots[name] = TRAP + 4 * len(slots)
            mu.mem_write(slots[name], BX_LR)
        return slots[name]

    wr(SCRATCH, 0xDEADBEEF)  # __stack_chk_guard
    dynsym = elf.get_section_by_name(".dynsym")
    entry = None
    for sym in dynsym.iter_symbols():
        if sym.name.startswith("_Z21createVoiceConfigFile"):
            entry = BASE + sym["st_value"]
    if entry is None:
        sys.exit("createVoiceConfigFile not found - is this libSmartConfig.so (armeabi-v7a)?")
    for section in (".rel.dyn", ".rel.plt"):
        for rel in elf.get_section_by_name(section).iter_relocations():
            where = BASE + rel["r_offset"]
            if rel["r_info_type"] == 23:  # R_ARM_RELATIVE
                wr(where, rd(where) + BASE)
                continue
            sym = dynsym.get_symbol(rel["r_info_sym"])
            if sym["st_shndx"] != "SHN_UNDEF":
                wr(where, BASE + sym["st_value"])
            elif sym.name == "__stack_chk_guard":
                wr(where, SCRATCH)
            elif sym.name == "__sF":
                wr(where, SCRATCH + 0x100)
            else:
                wr(where, slot(sym.name))

    by_addr = {addr: name for name, addr in slots.items()}

    def on_import(_uc, addr, _size, _user):
        name = by_addr.get(addr)
        if name is None:
            return
        if name not in libc:
            raise RuntimeError(f"library called unexpected import {name}")
        mu.reg_write(UC_ARM_REG_R0, libc[name]() & 0xFFFFFFFF)

    mu.hook_add(UC_HOOK_CODE, on_import, begin=TRAP, end=TRAP + 0x7FF)
    mu.reg_write(UC_ARM_REG_C1_C0_2, mu.reg_read(UC_ARM_REG_C1_C0_2) | (0xF << 20))  # enable VFP
    mu.reg_write(UC_ARM_REG_FPEXC, 0x40000000)

    stop = TRAP + 0x800
    mu.mem_write(stop, BX_LR)
    data, path = SCRATCH + 0x1000, SCRATCH + 0x2000
    mu.mem_write(data, payload)
    mu.mem_write(path, b"/pair.wav\0")
    sp = STACK - 0x100
    wr(sp, FREQ)
    wr(sp + 4, TX_MODE)
    mu.reg_write(UC_ARM_REG_SP, sp)
    mu.reg_write(UC_ARM_REG_LR, stop)
    for i, v in enumerate((data, len(payload), path, BG_MUSIC)):
        mu.reg_write(UC_ARM_REG_R0 + i, v)
    mu.emu_start(entry | 1, stop)
    if mu.reg_read(UC_ARM_REG_R0) != 0 or not written:
        sys.exit("encoder reported an error")
    return bytes(written)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lib", required=True, help="path to libSmartConfig.so (armeabi-v7a) from the DMSS APK")
    ap.add_argument("--sn", required=True, help="camera serial number")
    ap.add_argument("--ssid", help="Wi-Fi name (default: the network this machine is on)")
    ap.add_argument("--security", default="wpa2-psk-ccmp", choices=sorted(sc.SECURITY))
    ap.add_argument("--out", default="pair.wav")
    a = ap.parse_args()

    ssid, psk = sc.wifi_credentials(a.ssid, a.security)
    wav = render(a.lib, sc.build_payload(a.sn, ssid, psk, sc.SECURITY[a.security]))
    with open(a.out, "wb") as f:
        f.write(wav)
    print(f"wrote {a.out} ({len(wav)} bytes) - play it on loop next to the camera, then delete it")


if __name__ == "__main__":
    main()
