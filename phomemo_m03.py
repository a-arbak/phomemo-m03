#!/usr/bin/env python3
"""
phomemo_m03.py - print to a Phomemo M03 thermal printer over Bluetooth LE.

The M03 is an 80 mm / 203 dpi printer with a 576-dot print head.
No driver and no vendor app are needed.

Install:
    pip install bleak pillow

Examples:
    python phomemo_m03.py scan
    python phomemo_m03.py info
    python phomemo_m03.py text "Hello M03!" "Second line" --size 40
    python phomemo_m03.py image photo.jpg --dither
    python phomemo_m03.py feed --lines 4
    python phomemo_m03.py raw "1f1107 1f1108"

Notes:
- The printer accepts one connection at a time. Close the Phomemo app on
  your phone first.
- If the printer stops answering status queries (you only see '01 01'
  acknowledgements), power-cycle it. See PROTOCOL.md.
"""
import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff03-0000-1000-8000-00805f9b34fb"

WIDTH_DOTS = 576            # 80 mm paper
WIDTH_BYTES = WIDTH_DOTS // 8
CHUNK = 180                 # BLE packet size (printer announces 02 b6 00 = 182 on connect)
CHUNK_DELAY = 0.02          # seconds between packets
CMD_DELAY = 0.12            # seconds between commands

# Density levels as mapped by the official app (T0 method)
DENSITY = {1: 0x01, 2: 0x02, 3: 0x04, 4: 0x0D}


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


def job_header(density: int = 3) -> list[bytes]:
    heat = 0x96
    return [
        h("1b40"),                                      # ESC @  reset
        bytes([0x1F, 0x11, 0x02, DENSITY[density]]),    # density
        bytes([0x1F, 0x11, 0x37, heat]),                # heat
        h("1f110b"),                                    # paper type: continuous roll
        h("1f113500"),                                  # compression off (raw raster)
        h("1f118803"),                                  # anti-crease off
    ]


def raster_header(width_bytes: int, lines: int) -> bytes:
    """GS v 0 raster bit image header, little-endian width/height."""
    return bytes([0x1D, 0x76, 0x30, 0x00,
                  width_bytes & 0xFF, width_bytes >> 8,
                  lines & 0xFF, lines >> 8])


FEED = h("1b6402")          # ESC d 2

STATUS_NAMES = {
    0x03: "temperature", 0x04: "battery", 0x05: "cover", 0x06: "paper",
    0x07: "firmware", 0x08: "serial", 0x0F: "job",
}


def describe(data: bytes) -> str:
    if data == b"\x01\x01":
        return ""           # per-packet BLE acknowledgement, not a printer reply
    if len(data) >= 2 and data[0] == 0x1A:
        k, v = data[1], data[2:]
        if k == 0x04 and v:
            return f"battery: {v[0]}"
        if k == 0x05 and v:
            return "cover: " + ("open" if v[0] == 0x99 else "closed")
        if k == 0x06 and v:
            return "paper: " + ("missing" if v[0] == 0x88 else "present")
        if k == 0x07 and len(v) >= 3:
            return f"firmware: {v[0]}.{v[1]}.{v[2]}"
        if k == 0x08:
            return "serial: " + v.decode(errors="replace")
        if k == 0x0F and v and v[0] == 0x0C:
            return "job finished"
        return f"{STATUS_NAMES.get(k, hex(k))}: {v.hex(' ')}"
    return data.hex(" ")


# ------------------------------------------------------------------ imaging

def to_raster(img, dither: bool, threshold: int = 128) -> tuple[bytes, int]:
    """Convert a PIL image to 1-bit raster rows, 1 = black, MSB first."""
    from PIL import Image
    img = img.convert("L")
    if img.width != WIDTH_DOTS:
        img = img.resize((WIDTH_DOTS, round(img.height * WIDTH_DOTS / img.width)),
                         Image.LANCZOS)
    if dither:
        bw = img.convert("1")                       # Floyd-Steinberg
    else:
        bw = img.point(lambda p: 255 if p >= threshold else 0, mode="1")
    # In PIL mode '1', 0 is black; the printer wants 1 = black, so invert.
    data = bytes(b ^ 0xFF for b in bw.tobytes())
    return data, bw.height


def render_text(lines: list[str], size: int, bold: bool, font_path: str | None):
    from PIL import Image, ImageDraw, ImageFont
    candidates = [font_path] if font_path else []
    candidates += [
        "arialbd.ttf" if bold else "arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    ]
    font = None
    for c in candidates:
        try:
            font = ImageFont.truetype(c, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    # word wrap
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    wrapped = []
    for ln in lines:
        cur = ""
        for w in ln.split(" "):
            t = f"{cur} {w}".strip()
            if probe.textlength(t, font=font) > WIDTH_DOTS - 8 and cur:
                wrapped.append(cur)
                cur = w
            else:
                cur = t
        wrapped.append(cur)

    lh = int(size * 1.25)
    img = Image.new("L", (WIDTH_DOTS, lh * len(wrapped) + 16), 255)
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(wrapped):
        d.text((4, 8 + i * lh), ln, font=font, fill=0)
    return img


# ------------------------------------------------------------------ BLE

class M03:
    def __init__(self, address: str | None, verbose: bool):
        self.address = address
        self.verbose = verbose
        self.client: BleakClient | None = None
        self.done = asyncio.Event()

    async def __aenter__(self):
        addr = self.address
        if not addr:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or "").startswith("M03"), timeout=10)
            if not dev:
                sys.exit("M03 not found. Is it on, and not connected to a phone?")
            addr = dev.address
        self.client = BleakClient(addr)
        await self.client.connect()
        await self.client.start_notify(NOTIFY_UUID, self._on_notify)
        print(f"Connected: {addr}")
        await asyncio.sleep(0.3)
        return self

    async def __aexit__(self, *exc):
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    def _on_notify(self, _, data: bytearray):
        data = bytes(data)
        if data[:3] == b"\x1a\x0f\x0c":
            self.done.set()
        msg = describe(data)
        if msg and (self.verbose or data[:1] == b"\x1a"):
            print("<-", msg)

    async def write(self, data: bytes, delay: float = CHUNK_DELAY):
        for i in range(0, len(data), CHUNK):
            await self.client.write_gatt_char(WRITE_UUID, data[i:i + CHUNK], response=False)
            await asyncio.sleep(delay)

    async def cmds(self, items: list[bytes]):
        for c in items:
            if self.verbose:
                print("->", c.hex(" "))
            await self.write(c)
            await asyncio.sleep(CMD_DELAY)

    async def print_raster(self, data: bytes, lines: int, density: int, feed: int):
        self.done.clear()
        await self.cmds(job_header(density))
        step = 4000         # split very long images into several GS v 0 blocks
        for y in range(0, lines, step):
            n = min(step, lines - y)
            await self.write(raster_header(WIDTH_BYTES, n))
            await self.write(data[y * WIDTH_BYTES:(y + n) * WIDTH_BYTES])
        await asyncio.sleep(0.3)
        await self.cmds([FEED] * feed)
        try:
            await asyncio.wait_for(self.done.wait(), timeout=10 + lines / 100)
            print("Print finished.")
        except asyncio.TimeoutError:
            print("Warning: no 'job finished' reply. If the printer is stuck, power-cycle it.")

    async def info(self):
        await self.cmds([h("1f1107"), h("1f1108"), h("1f1104"), h("1f1106")])
        await asyncio.sleep(1.5)


# ------------------------------------------------------------------ CLI

async def main():
    p = argparse.ArgumentParser(description="Print to a Phomemo M03 over Bluetooth LE")
    p.add_argument("--address", help="BLE address (default: scan for a device named 'M03')")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="List nearby BLE devices")
    sub.add_parser("info", help="Query firmware, battery, cover and paper state")

    t = sub.add_parser("text", help="Print text")
    t.add_argument("lines", nargs="+")
    t.add_argument("--size", type=int, default=36)
    t.add_argument("--regular", action="store_true", help="Regular instead of bold")
    t.add_argument("--font", help="Path to a .ttf font")

    im = sub.add_parser("image", help="Print an image")
    im.add_argument("path")
    im.add_argument("--dither", action="store_true")
    im.add_argument("--threshold", type=int, default=128)

    for sp in (t, im):
        sp.add_argument("--density", type=int, choices=[1, 2, 3, 4], default=3)
        sp.add_argument("--feed", type=int, default=4, help="Number of ESC d 2 at the end")
        sp.add_argument("--preview", help="Save the raster as a PNG before sending")

    f = sub.add_parser("feed", help="Feed paper")
    f.add_argument("--lines", type=int, default=2)

    r = sub.add_parser("raw", help="Send raw hex command(s), space separated")
    r.add_argument("hex")

    a = p.parse_args()

    if a.cmd == "scan":
        for d in await BleakScanner.discover(timeout=6):
            print(f"{d.address}  {d.name}")
        return

    payload = None
    if a.cmd in ("text", "image"):
        from PIL import Image
        img = (render_text(a.lines, a.size, not a.regular, a.font) if a.cmd == "text"
               else Image.open(a.path))
        data, lines = to_raster(img, getattr(a, "dither", False), getattr(a, "threshold", 128))
        if a.preview:
            prev = Image.frombytes("1", (WIDTH_DOTS, lines), bytes(b ^ 0xFF for b in data))
            prev.save(a.preview)
            print(f"Preview saved: {a.preview}")
        payload = (data, lines)

    async with M03(a.address, a.verbose) as pr:
        if a.cmd == "info":
            pr.verbose = True
            await pr.info()
        elif a.cmd == "feed":
            await pr.cmds([h("1b40")] + [FEED] * a.lines)
        elif a.cmd == "raw":
            pr.verbose = True
            await pr.cmds([h(x) for x in a.hex.split()])
            await asyncio.sleep(1.5)
        else:
            await pr.print_raster(*payload, density=a.density, feed=a.feed)


if __name__ == "__main__":
    asyncio.run(main())
