# dahua-tools

Small scripts for setting up Dahua / Imou consumer Wi-Fi cameras from a Linux
machine without the phone app. Written against an IPC-C46 (firmware 2.621) by
reading the DMSS Android app; other models are untested.

| Script | What it does |
|---|---|
| `dahua_smartconfig.py` | Sends Wi-Fi credentials to a camera in pairing mode (blinking green LED) over UDP broadcast + multicast. |
| `dahua_pairing_sound.py` | Generates the "sound wave" pairing audio by running the app's own encoder in an emulator. |
| `dahua_discover.py` | Finds cameras on the LAN and reports whether they are initialised. |
| `dahua_init.py` | Creates the admin password on a camera that is not initialised yet. |

## Pairing a factory-reset camera

1. Reset the camera and wait for the LED to blink green.
2. Read the serial number from the label (the `SN:` field of the QR code).
3. Run both channels at once, with the camera close to the speakers:

   ```sh
   ./dahua_smartconfig.py --sn <SERIAL> --seconds 90 &
   ./dahua_pairing_sound.py --lib libSmartConfig.so --sn <SERIAL> --out pair.wav
   for i in $(seq 30); do pw-play pair.wav; done
   rm pair.wav            # it encodes your Wi-Fi password
   ```

4. `./dahua_discover.py` shows the camera once it has joined, as "NOT initialised".
5. Set the admin password:

   ```sh
   DAHUA_PASSWORD='...' ./dahua_init.py --ip <CAMERA_IP>
   ```

After that the streams are at
`rtsp://admin:<password>@<ip>:554/cam/realmonitor?channel=1&subtype=0` (main)
and `subtype=1` (sub).

The Wi-Fi password comes from `$WIFI_PSK`, or is read from NetworkManager for
the given (or currently active) SSID. The camera is 2.4 GHz only; if the
machine is on 5 GHz, sending from a 2.4 GHz interface (`--iface`) may help.

What worked on the IPC-C46: sound at ~80 % volume together with packets sent
from a 2.4 GHz adapter. Packets alone (from 5 GHz and from 2.4 GHz) and sound
at 40 % volume did not. Which of the two channels the camera actually used was
not isolated.

`libSmartConfig.so` is not included. Get the DMSS APK (`com.mm.android.DMSS`),
unzip the `armeabi-v7a` split and take `lib/armeabi-v7a/libSmartConfig.so`.

## Protocol notes

Payload (`build_payload`):

    0x10, security, len(ssid), ssid, len(psk), psk, len(SN), SN (upper case), CRC32 (big endian)

Security codes: 0 open, 2/3 WEP open/shared, 4 WPA-PSK-TKIP, 5 WPA-PSK-CCMP,
6 WPA2-PSK-TKIP, 7 WPA2-PSK-AES, 12 WPA2-PSK-CCMP.

**Broadcast** - zero-filled UDP datagrams to `255.255.255.255:53556`, 15 ms
apart; the *length* carries the data. The stream encodes
`[len-4, 0x20, 0x0c] + payload[2:-4]` in groups of six bytes:

- lengths 1,2,3,4 repeated four times (sync)
- length `8 + group` eight times
- six datagrams of length `(byte & 0x7f) | (position << 7)`, position 1..6
- one datagram of length `crc8(group, six bytes) | 0x380` (Dallas/Maxim CRC-8)

**Multicast** - 16-byte datagrams to port 34567; the *group address* carries
the data. For byte `d` at index `i` of `[len] + payload + [sum & 0xff]` the
destination is `239.X.i.d`, with
`X = 0x60 | ((6 + (i >> 4) + i + (d >> 4) + d) & 0x0f)`.

**Sound** - the same payload, FSK-modulated around 11 kHz with Reed-Solomon
coding (`fsk_tx` in the library); not re-implemented here, only emulated.

**Discovery** - DHIP JSON over UDP 37810 (`DHDiscover.search`). In the reply,
`Init & 3` is 1 for a camera whose admin account has not been created yet and
2 once it has.

**Initialisation** - also DHIP on UDP 37810, unicast to the camera:

1. `Security.getEncryptInfo` returns `pub` (`N:<hex>,E:<hex>`) and a `cipher`
   list (`AES`, `RPAC`, `ROAE`).
2. `DevInit.account` carries `params: {uni: 1, salt, cipher, content}` where
   `content` is base64 of the AES-encrypted JSON `{"name","pwd"}` (zero padded)
   and `salt` is the hex RSA encryption of the random AES key.
   - `RPAC-256`: RSA PKCS#1 v1.5, AES-256-CBC with the ASCII IV `0000000000000000`
   - `ROAE-256`: RSA OAEP (SHA-1), AES-256-ECB (not tested)
   - `AES-128`: RSA PKCS#1 v1.5, AES-128-ECB (not tested)

The camera answers `client.notifyDevInit` with `result: true`, and `Init`
changes from 405 to 406. Only the RPAC path has been run against a real camera.
