#!/usr/bin/env python3
"""Find Dahua/Imou devices on the LAN and show whether they are initialised.

Sends a DHIP "DHDiscover.search" (UDP 37810) by broadcast, multicast and,
optionally, unicast, and prints one line per device that answers.

Usage:
    dahua_discover.py [--ip CAMERA_IP] [--json]
"""
import argparse
import json
import socket
import struct

PORT = 37810
GROUP = "239.255.255.251"
INIT_STATE = {0: "legacy (no init)", 1: "NOT initialised", 2: "initialised"}


def dhip(obj) -> bytes:
    body = json.dumps(obj).encode()
    return struct.pack("<I4sIIIIII", 0x20, b"DHIP", 0, 0, len(body), 0, len(body), 0) + body


def query(obj, targets, timeout=3.0):
    """Yield (ip, reply_dict) for every DHIP answer received within the timeout."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    for t in targets:
        s.sendto(dhip(obj), (t, PORT))
    try:
        while True:
            data, addr = s.recvfrom(65535)
            if data[4:8] != b"DHIP":
                continue
            try:
                yield addr[0], json.loads(data[32:].rstrip(b"\0\n"))
            except ValueError:
                pass
    except socket.timeout:
        return


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", help="also ask this address directly")
    ap.add_argument("--json", action="store_true", help="print the raw device records")
    a = ap.parse_args()

    targets = ["255.255.255.255", GROUP] + ([a.ip] if a.ip else [])
    seen = set()
    for ip, reply in query({"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}}, targets):
        info = reply.get("params", {}).get("deviceInfo", {})
        key = reply.get("mac") or ip
        if key in seen:
            continue
        seen.add(key)
        if a.json:
            print(json.dumps(reply, indent=2))
            continue
        init = info.get("Init")
        state = INIT_STATE.get(init & 3, "?") if isinstance(init, int) else "?"
        print(f"{ip:15}  {reply.get('mac', '?'):17}  {info.get('DeviceType', '?'):12}  "
              f"sn={info.get('SerialNo', '?')}  fw={info.get('Version', '?')}  {state} (Init={init})")
    if not seen:
        print("no Dahua devices answered")


if __name__ == "__main__":
    main()
