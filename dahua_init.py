#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["cryptography"]
# ///
"""Initialise a factory-fresh Dahua/Imou camera: create the admin password.

A camera that reports "NOT initialised" in dahua_discover.py has no account
yet. This performs the same exchange as CLIENT_InitDevAccountByIP in Dahua's
NetSDK, over DHIP (UDP 37810):

  1. Security.getEncryptInfo  -> the camera's RSA public key and cipher list
  2. DevInit.account          -> {"name","pwd"} encrypted with a random AES key,
                                 the AES key itself wrapped with the RSA key

Usage:
    DAHUA_PASSWORD=... dahua_init.py --ip <CAMERA_IP> [--user admin]
"""
import argparse
import base64
import json
import os
import secrets
import string
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dahua_discover import query  # noqa: E402

IV = b"0000000000000000"


def ask(ip, obj):
    for src, reply in query(obj, [ip], timeout=5.0):
        if src == ip:
            return reply
    sys.exit(f"no answer from {ip} to {obj['method']}")


def encrypt(plain: bytes, pub: str, ciphers) -> dict:
    """Mirror CCryptoUtilAES::EncryptData: returns the salt/cipher/content fields."""
    n, e = (int(part.split(":")[1], 16) for part in pub.split(","))
    key = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)).encode()
    if "ROAE" in ciphers:      # RSA-OAEP + AES-ECB
        name, mode, wrap = "ROAE-256", modes.ECB(), padding.OAEP(padding.MGF1(hashes.SHA1()), hashes.SHA1(), None)
    elif "RPAC" in ciphers:    # RSA-PKCS1 + AES-CBC
        name, mode, wrap = "RPAC-256", modes.CBC(IV), padding.PKCS1v15()
    else:                      # RSA-PKCS1 + AES-ECB, 128-bit key
        name, mode, wrap, key = "AES-128", modes.ECB(), padding.PKCS1v15(), key[:16]
    plain += b"\0" * (-len(plain) % 16)  # zero padding, as the SDK does
    enc = Cipher(algorithms.AES(key), mode).encryptor()
    salt = rsa.RSAPublicNumbers(e, n).public_key().encrypt(key, wrap)
    return {"salt": salt.hex().upper(), "cipher": name,
            "content": base64.b64encode(enc.update(plain) + enc.finalize()).decode()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", required=True)
    ap.add_argument("--user", default="admin")
    a = ap.parse_args()
    password = os.environ.get("DAHUA_PASSWORD")
    if not password or not 8 <= len(password) <= 32:
        sys.exit("set DAHUA_PASSWORD to the new password (8-32 characters)")

    found = ask(a.ip, {"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}})
    mac = found["mac"]
    init = found["params"]["deviceInfo"].get("Init", 0)
    if init & 3 != 1:
        sys.exit(f"{a.ip} does not report itself as uninitialised (Init={init}); not touching it")

    info = ask(a.ip, {"method": "Security.getEncryptInfo", "params": {"mac": mac, "uni": 1}, "mac": mac, "id": 1})["params"]
    account = json.dumps({"name": a.user, "pwd": password}, separators=(",", ":")).encode() + b"\n"
    params = dict(encrypt(account, info["pub"], info.get("cipher", [])), uni=1)
    reply = ask(a.ip, {"method": "DevInit.account", "params": params, "mac": mac, "id": 2})
    print(json.dumps(reply))
    ok = reply.get("params", {}).get("result") is True
    print("initialised" if ok else "camera refused the request")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
