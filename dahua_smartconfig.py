#!/usr/bin/env python3
"""Send Wi-Fi credentials to a Dahua/Imou camera in pairing mode (blinking green LED).

Re-implements the network half of libSmartConfig.so from the DMSS Android app
(LCSmartConfig, ConfigType ALL minus the audio channel). The camera sniffs the
air while unassociated; the data is carried by UDP *packet lengths* (broadcast)
and by *multicast group addresses*, both of which stay visible under WPA2.

Usage:
    dahua_smartconfig.py --sn <SERIAL> [--ssid NAME] [--iface IFACE] [--seconds 120]

The Wi-Fi password is taken from $WIFI_PSK, or else read from NetworkManager.
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import zlib

GAP = 0.015  # the app sleeps 15 ms after every packet
BROADCAST_PORT = 53556
MULTICAST_PORT = 34567

SECURITY = {  # codes from the native library's capability-string parser
    "open": 0, "wep-open": 2, "wep-shared": 3,
    "wpa-psk-tkip": 4, "wpa-psk-ccmp": 5,
    "wpa2-psk-tkip": 6, "wpa2-psk-aes": 7, "wpa2-psk-ccmp": 12,
}


def build_payload(sn: str, ssid: str, psk: str, security: int) -> bytes:
    s, p, d = ssid.encode(), psk.encode(), sn.upper().encode()
    if len(s) + len(p) + len(d) + 9 >= 0xFE:
        raise ValueError("ssid + password + serial too long")
    body = bytes([0x10, security, len(s)]) + s + bytes([len(p)]) + p + bytes([len(d)]) + d
    return body + zlib.crc32(body).to_bytes(4, "big")


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        x, crc = crc ^ b, 0
        for _ in range(8):
            if (crc ^ x) & 1:
                crc = ((crc ^ 0x18) >> 1) | 0x80
            else:
                crc >>= 1
            x >>= 1
    return crc


def broadcast_lengths(payload: bytes):
    """One full pass of the length-encoded broadcast stream."""
    n = len(payload)
    buf = bytes([n - 4, 0x20, 0x0C]) + payload[2:n - 4]
    out = []
    for g in range((len(buf) + 5) // 6):
        out += [1, 2, 3, 4] * 4          # sync
        out += [g + 8] * 8               # group index
        chunk = [g]
        for i in range(6):
            idx = g * 6 + i
            v = buf[idx] & 0x7F if idx < len(buf) else 0
            chunk.append(v)
            out.append(v | ((i + 1) << 7))
        out.append(crc8(bytes(chunk)) | 0x380)
    return out


def multicast_groups(payload: bytes, flag: int = 0x40):
    """One full pass of the address-encoded multicast stream."""
    buf = bytes([len(payload)]) + payload
    buf += bytes([sum(buf) & 0xFF])
    f = flag | 0x20
    out = []
    for i, d in enumerate(buf):
        x = f | (((f >> 4) + (i >> 4) + i + (d >> 4) + d) & 0x0F)
        out.append(f"239.{x}.{i}.{d}")
    return out


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the default route
        return s.getsockname()[0]
    finally:
        s.close()


def nm_value(args):
    return subprocess.run(["nmcli", *args], capture_output=True, text=True).stdout.strip()


def wifi_credentials(ssid, security):
    """Return (ssid, password), defaulting to the active network and NetworkManager's stored key."""
    ssid = ssid or nm_value(["-t", "-f", "active,ssid", "dev", "wifi"]).split("yes:", 1)[-1].split("\n")[0]
    psk = os.environ.get("WIFI_PSK") or nm_value(
        ["-s", "-g", "802-11-wireless-security.psk", "connection", "show", ssid])
    if not ssid or (not psk and security != "open"):
        sys.exit("could not determine SSID/password; pass --ssid and set WIFI_PSK")
    return ssid, psk


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sn", required=True, help="camera serial number (SN on the label / in the QR code)")
    ap.add_argument("--ssid", help="Wi-Fi name (default: the network this machine is on)")
    ap.add_argument("--security", default="wpa2-psk-ccmp", choices=sorted(SECURITY))
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--iface", help="network interface to send from (default: the default-route interface)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without sending")
    a = ap.parse_args()

    ssid, psk = wifi_credentials(a.ssid, a.security)

    payload = build_payload(a.sn, ssid, psk, SECURITY[a.security])
    lengths, groups = broadcast_lengths(payload), multicast_groups(payload)
    print(f"ssid={ssid!r} sn={a.sn.upper()} password={len(psk)} chars; "
          f"{len(lengths)} broadcast + {len(groups)} multicast packets per pass")
    if a.dry_run:
        return

    if a.iface:
        ip = nm_value(["-g", "IP4.ADDRESS", "dev", "show", a.iface]).split("/")[0]
        if not ip:
            sys.exit(f"{a.iface} has no IPv4 address")
    else:
        ip = local_ip()

    def pin(s):
        if a.iface:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, a.iface.encode())
    deadline = time.monotonic() + a.seconds
    zeros = bytes(1024)

    def run_broadcast():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        pin(s)
        s.bind((ip, 0))
        while time.monotonic() < deadline:
            for n in lengths:
                s.sendto(zeros[:n], ("255.255.255.255", BROADCAST_PORT))
                time.sleep(GAP)

    def run_multicast():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        pin(s)
        s.bind((ip, 0))
        while time.monotonic() < deadline:
            for g in groups:
                s.sendto(zeros[:16], (g, MULTICAST_PORT))
                time.sleep(GAP)

    threads = [threading.Thread(target=f, daemon=True) for f in (run_broadcast, run_multicast)]
    for t in threads:
        t.start()
    print(f"sending from {ip} for {a.seconds}s - watch for the LED to go solid green")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
