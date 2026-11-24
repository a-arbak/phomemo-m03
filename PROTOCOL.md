# Phomemo M03 Bluetooth protocol

Tested device: Phomemo M03 (black), 80 mm thermal paper, firmware 1.0.1.

How it was worked out:
- The command sequence was read from the official Android app, Phomemo v4.2.0 (`com.quyin.phomemo`), decompiled with jadx for interoperability.
- It was then confirmed with live tests from Windows 11 and Chrome Web Bluetooth.
- No vendor code is included in this repository.

## Hardware

| Property | Value | Where it comes from |
|---|---|---|
| Print head | 576 dots (72 bytes per row), 203 dpi | app class for the M03 is constructed with width `576` and `0.125` mm/dot |
| Paper | 80 mm (53 mm also fits) | device label |
| Related models | M03A, M03C, M03S, M03AS have separate classes in the app and may differ | |

## Transport

### Bluetooth LE (verified)

- Service `0000ff00-0000-1000-8000-00805f9b34fb`

| Characteristic | Properties | Use |
|---|---|---|
| `ff01` | read | |
| `ff02` | write, write without response | All commands and raster data |
| `ff03` | notify | Printer replies |

- Each command is sent as its own write without response.
- 180-byte packets work. The app requests MTU 512 and sends 509-byte packets.
- On connect the printer notifies `02 b6 00`, probably MTU or credit information (0xb6 = 182).
- Every packet is acknowledged with `01 01`. This is a link-layer ack, not a printer reply.

### Classic Bluetooth SPP

- The Android app talks to the M03 over RFCOMM/SPP (UUID `00001101-0000-1000-8000-00805f9b34fb`), writing the same raw bytes.
- Opening the port from Windows with Web Serial worked, but printing over SPP was not verified. During that test the printer was in the stuck state described below.

## Print job

The order is as the app sends it:

| # | Bytes | Meaning |
|---|---|---|
| 1 | `1B 40` | ESC @, reset |
| 2 | `1F 11 02 dd` | Density: `01` light, `02`, `04` normal, `0D` dark |
| 3 | `1F 11 37 96` | Heat parameter. The app sends `64` for the "light" setting. |
| 4 | `1F 11 0B` | Paper type continuous roll. Other values: `26` for black-mark paper, `38` for single sheet / tattoo paper. |
| 5 | `1F 11 35 00` | Compression off. `01` enables LZO. |
| 6 | `1F 11 88 03` | Anti-crease off. `01` is weak, `02` is strong. |
| 7 | `1D 76 30 00 48 00 LL HH` | GS v 0 raster header: 72 bytes wide, row count as little-endian `LL HH` |
| 8 | raster data | 1 bit per pixel, MSB is the leftmost pixel, **1 = black**, 72 × rows bytes |
| 9 | `1B 64 02` | ESC d 2, feed |

When the raster is complete the printer notifies `1A 0F 0C`, meaning the job is finished.

Minimal example, a half-width black bar 24 rows high:
```
1b40
1f110204
1f113796
1f110b
1f113500
1f118803
1d76300048001800  + 1728 bytes (each row: 36×ff, 36×00)
1b6402
1b6402
```

## Status queries and replies

Queries have the form `1F 11 xx`. Replies have the form `1A xx …`.

| Query | Reply | Meaning |
|---|---|---|
| `1F 11 07` | `1A 07 01 00 01` | Firmware 1.0.1 |
| `1F 11 08` | `1A 08` + 15 ASCII bytes | Serial number |
| — | `1A 04 nn` | Battery level. `A1`, `A2`, `A3` are low-battery warnings. |
| — | `1A 05 98` / `1A 05 99` | Cover closed / open |
| — | `1A 06 88` | Paper missing. Other values mean paper present. |
| — | `1A 03 A8` / `1A 03 A9` | Temperature normal / overheated |
| — | `1A 0F 0C` | Job finished |

## Pitfalls

1. **Stuck state.** A malformed job can leave the printer swallowing every later byte. Examples are a wrong header, missing data, or M02-style commands.
   - In that state it still acks each packet with `01 01`.
   - It no longer answers `1F 11 07` and prints nothing.
   - Fix: power-cycle the printer.
   - Health check: send `1F 11 07` and expect `1A 07 …`.
2. **Short prints stay inside the printer.** One or two `ESC d 2` are not enough to push a short print past the cutter edge. Sending four works well.
3. **One connection at a time.** The printer accepts only one connection. Disconnect the phone app before connecting from a PC, and the other way round.

## What did not work on the M03

| Command set | Result |
|---|---|
| The M02 job from [phomemo-tools](https://github.com/vivier/phomemo-tools): `1F 11 02 04` header and `1F 11 08/0E/07/09` footer | No print |
| The "m-series" profile used by [phomymo](https://github.com/transcriptionstream/phomymo): `ESC 7`, `GS \|`, `ESC J`, 432-dot width | No print |
| `10 FF 50 F1`, the M02 battery query | No reply |
| `ESC J n` feed on its own | Ignored |
