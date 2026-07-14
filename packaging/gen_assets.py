"""
Generates the desktop/store art from code — no image libraries needed.

Draws the app mark (the hub's ◈ diamond in the app's accent gradient on a
dark rounded tile) and writes:

  assets/app.ico                 window/taskbar/exe icon (16..256px)
  assets/Square44x44Logo.png     MSIX taskbar/start icon
  assets/Square150x150Logo.png   MSIX medium tile
  assets/Wide310x150Logo.png     MSIX wide tile
  assets/StoreLogo.png           MSIX store listing icon (50x50)

Run:  python gen_assets.py
"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets")

# Brand colors (mirrors frontend/style.css tokens).
BG_TOP = (0x1B, 0x21, 0x31)
BG_BOT = (0x0F, 0x13, 0x1C)
ACC_TOP = (0x7C, 0x8C, 0xFF)   # --accent
ACC_BOT = (0xB0, 0x7B, 0xFF)   # --accent-2
INNER = (0x12, 0x16, 0x20)

SS = 4  # supersampling factor for smooth edges


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _render(w, h):
    """Render the tile at w x h (RGBA bytes), supersampled then box-averaged."""
    W, H = w * SS, h * SS
    side = min(W, H)
    radius = side * 0.21
    cx, cy = W / 2.0, H / 2.0
    dw, dh = side * 0.30, side * 0.34          # outer diamond half-extents
    big = bytearray(W * H * 4)

    for y in range(H):
        ty = y / (H - 1) if H > 1 else 0
        bg = _lerp(BG_TOP, BG_BOT, ty)
        for x in range(W):
            # rounded-rect tile mask
            qx = max(abs(x - cx) - (W / 2.0 - radius), 0.0)
            qy = max(abs(y - cy) - (H / 2.0 - radius), 0.0)
            if (qx * qx + qy * qy) > radius * radius:
                continue  # transparent corner
            r, g, b = bg
            d = abs(x - cx) / dw + abs(y - cy) / dh
            if d <= 1.0:
                if d <= 0.42:
                    r, g, b = INNER            # the ◈ hollow center
                else:
                    r, g, b = _lerp(ACC_TOP, ACC_BOT, ty)
            i = (y * W + x) * 4
            big[i:i + 4] = bytes((r, g, b, 255))

    # box-average down to target size
    out = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            rs = gs = bs = as_ = 0
            for sy in range(SS):
                for sx in range(SS):
                    i = ((y * SS + sy) * W + (x * SS + sx)) * 4
                    rs += big[i]; gs += big[i + 1]; bs += big[i + 2]; as_ += big[i + 3]
            n = SS * SS
            i = (y * w + x) * 4
            out[i:i + 4] = bytes((rs // n, gs // n, bs // n, as_ // n))
    return bytes(out)


def _png(w, h, rgba):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _ico(pngs):
    """pngs: list of (size, png_bytes). ICO with PNG-compressed entries."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    entries, blobs = b"", b""
    offset = len(header) + 16 * len(pngs)
    for size, data in pngs:
        s = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main():
    os.makedirs(OUT, exist_ok=True)

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = [(s, _png(s, s, _render(s, s))) for s in ico_sizes]
    with open(os.path.join(OUT, "app.ico"), "wb") as f:
        f.write(_ico(pngs))
    print("app.ico (%s)" % ", ".join(str(s) for s in ico_sizes))

    for name, w, h in [("Square44x44Logo", 44, 44), ("Square150x150Logo", 150, 150),
                       ("Wide310x150Logo", 310, 150), ("StoreLogo", 50, 50)]:
        with open(os.path.join(OUT, name + ".png"), "wb") as f:
            f.write(_png(w, h, _render(w, h)))
        print(name + ".png")


if __name__ == "__main__":
    main()
