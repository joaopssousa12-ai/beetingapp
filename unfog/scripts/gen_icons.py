"""Generate PWA PNG icons (solid gradient, no deps beyond stdlib)."""
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
TOP = (0x5B, 0x5B, 0xD6)
BOT = (0x8C, 0x8C, 0xF0)


def png(size):
    rows = b""
    for y in range(size):
        t = y / (size - 1)
        r = int(TOP[0] + (BOT[0] - TOP[0]) * t)
        g = int(TOP[1] + (BOT[1] - TOP[1]) * t)
        b = int(TOP[2] + (BOT[2] - TOP[2]) * t)
        rows += b"\x00" + bytes((r, g, b)) * size

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


for s in (192, 512):
    path = os.path.join(OUT, f"icon-{s}.png")
    with open(path, "wb") as f:
        f.write(png(s))
    print("wrote", path)
