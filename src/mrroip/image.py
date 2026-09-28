# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
1-bit images in the display profile's wire format, "1bpp-row-msb" with Base64: row-major, MSB = leftmost
pixel, 1 = lit, ceil(width / 8) × height bytes. That is exactly what Pillow's mode "1" tobytes() produces.

pattern() needs nothing; text() and picture() import Pillow when called.

Partial updates send rectangles of an image in the same format: crop() cuts them, changed_rects() finds them,
paste() predicts the result.

Colour endpoints (../../../esp32/oled/MRROIP-PLAN.md question 16) use "rgb565be" instead: two bytes per pixel, most
significant byte first, row-major. Their pictures are too large for a message, so they go with PUT /objects/image
and the device remembers them as a CRC32 per tile: tile_crcs() and image_id() compute the same figures here,
changed_tiles() finds what to send.
"""

import base64
import zlib


def image_size(definition):
    """(width, height) of the "image" object in a /definition document."""
    for obj in definition.get("objects", []):
        if obj.get("id") == "image":
            return obj["profile"]["width_px"], obj["profile"]["height_px"]
    raise ValueError("this endpoint has no image object")


def encode(data):
    """Base64 text for an image object's desired state."""
    return base64.b64encode(data).decode()


def crc32(data):
    """The checksum a display reports for its image (state.profile.image.crc32)."""
    return "%08x" % zlib.crc32(data)


def crop(data, width, x, y, w, h):
    """The w×h rectangle at (x, y) of an image, as an image of its own."""
    stride, rstride = (width + 7) // 8, (w + 7) // 8
    out = bytearray(rstride * h)
    for ry in range(h):
        for rx in range(w):
            if data[(y + ry) * stride + (x + rx) // 8] & (0x80 >> ((x + rx) % 8)):
                out[ry * rstride + rx // 8] |= 0x80 >> (rx % 8)
    return bytes(out)


def paste(data, width, x, y, w, h, rect):
    """The image with its w×h rectangle at (x, y) replaced by `rect`."""
    stride, rstride = (width + 7) // 8, (w + 7) // 8
    out = bytearray(data)
    for ry in range(h):
        for rx in range(w):
            i, bit = (y + ry) * stride + (x + rx) // 8, 0x80 >> ((x + rx) % 8)
            if rect[ry * rstride + rx // 8] & (0x80 >> (rx % 8)):
                out[i] |= bit
            else:
                out[i] &= ~bit & 0xFF
    return bytes(out)


def changed_rects(old, new, width, height, tile=8, max_rects=8):
    """Rectangles (x, y, w, h) covering every pixel that differs between two images. Built from tile×tile squares:
    runs of changed squares per row, stacked where the next row has the same run. More than max_rects rectangles
    are merged into their bounding box. [] if nothing changed."""
    stride = (width + 7) // 8
    diff = bytes(a ^ b for a, b in zip(old, new))

    def changed(tx, ty):
        return any(diff[y * stride + x // 8] & (0x80 >> (x % 8))
                   for y in range(ty, min(ty + tile, height)) for x in range(tx, min(tx + tile, width)))

    rects, above = [], {}           # above: (x, w) of a run in the previous tile row -> index in rects
    for ty in range(0, height, tile):
        th, row, x = min(tile, height - ty), {}, 0
        while x < width:
            if not changed(x, ty):
                x += tile
                continue
            x0 = x
            while x < width and changed(x, ty):
                x += tile
            run = (x0, min(x, width) - x0)
            if run in above:
                i = above[run]
                rx, ry, rw, rh = rects[i]
                rects[i] = (rx, ry, rw, rh + th)
            else:
                i = len(rects)
                rects.append((run[0], ty, run[1], th))
            row[run] = i
        above = row
    if len(rects) > max_rects:
        x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
        x1, y1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
        rects = [(x0, y0, x1 - x0, y1 - y0)]
    return rects


def pattern(kind, width, height):
    """Test patterns without Pillow: "border" (frame and a diagonal) or "checker" (4 px squares)."""
    if kind not in ("border", "checker"):
        raise ValueError(f"unknown pattern {kind!r}")
    stride = (width + 7) // 8
    buf = bytearray(stride * height)
    for y in range(height):
        for x in range(width):
            if kind == "border":
                on = x in (0, width - 1) or y in (0, height - 1) or x == y * (width - 1) // (height - 1)
            else:
                on = (x // 4 + y // 4) % 2 == 0
            if on:
                buf[y * stride + x // 8] |= 0x80 >> (x % 8)
    return bytes(buf)


def text(string, width, height, font=None, size=12):
    """Text from the top-left corner; "\\n" starts a new line. font: a TrueType file, or None for Pillow's default."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("1", (width, height))
    face = ImageFont.truetype(font, size) if font else ImageFont.load_default()
    ImageDraw.Draw(img).multiline_text((0, 0), string, font=face, fill=1)
    return img.tobytes()


def picture(path, width, height):
    """A picture file, scaled to fit, centred and thresholded at 50 %."""
    from PIL import Image, ImageOps
    fitted = ImageOps.contain(Image.open(path).convert("L"), (width, height))
    canvas = Image.new("L", (width, height))
    canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return canvas.point(lambda v: 255 if v >= 128 else 0).convert("1").tobytes()


# -- colour, rgb565be (plan question 16) ---------------------------------------------------------------------

def pack_rgb565(rgb, width, height):
    """rgb565be bytes from RGB888 bytes (3 per pixel), row-major."""
    out = bytearray(width * height * 2)
    for i in range(width * height):
        r, g, b = rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]
        value = (r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3
        out[2 * i] = value >> 8
        out[2 * i + 1] = value & 0xFF
    return bytes(out)


def rgb565(r, g, b):
    """One pixel as two bytes, most significant first."""
    value = (r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3
    return bytes((value >> 8, value & 0xFF))


def pattern_rgb565(kind, width, height):
    """Test pictures without Pillow: "bars", "gradient" or "checker"."""
    bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
            (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            if kind == "bars":
                row += rgb565(*bars[x * 8 // width])
            elif kind == "gradient":
                v = 255 * y // max(1, height - 1)
                row += rgb565(v, 255 - v, (x * 255) // max(1, width - 1))
            elif kind == "checker":
                lit = (x // 20 + y // 20) % 2 == 0
                row += rgb565(255, 255, 255) if lit else rgb565(0, 0, 60)
            else:
                raise ValueError(f"unknown pattern {kind!r}")
        rows.append(bytes(row))
    return b"".join(rows)


def picture_rgb565(path, width, height, rotate=0, fill=False):
    """A picture file as rgb565be. rotate turns it by 90, 180 or 270 degrees first, for a landscape photo on a
    portrait panel; fill crops to cover the whole panel instead of fitting it inside black borders."""
    from PIL import Image, ImageOps
    picture = Image.open(path).convert("RGB")
    if rotate:
        picture = picture.rotate(-rotate, expand=True)       # clockwise, like turning the display
    if fill:
        canvas = ImageOps.fit(picture, (width, height))
    else:
        fitted = ImageOps.contain(picture, (width, height))
        canvas = Image.new("RGB", (width, height))
        canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return pack_rgb565(canvas.tobytes(), width, height)


def crop_rgb565(data, width, x, y, w, h):
    """The w x h rectangle at (x, y), rows packed one after another."""
    stride = width * 2
    return b"".join(data[(y + row) * stride + x * 2: (y + row) * stride + (x + w) * 2] for row in range(h))


def tile_crcs(data, width, height, tile=20):
    """CRC32 per tile, row-major, as the device computes them while pixels arrive."""
    stride = width * 2
    out = []
    for ty in range(0, height, tile):
        rows = min(tile, height - ty)
        for tx in range(0, width, tile):
            cols = min(tile, width - tx)
            crc = 0
            for row in range(rows):
                start = (ty + row) * stride + tx * 2
                crc = zlib.crc32(data[start: start + cols * 2], crc)
            out.append(crc)
    return out


def image_id(data, width, height, tile=20):
    """What state.profile.image.id reports: CRC32 over the tile CRCs, each as four bytes, most significant first."""
    table = b"".join(crc.to_bytes(4, "big") for crc in tile_crcs(data, width, height, tile))
    return "%08x" % zlib.crc32(table)


def changed_tiles(old, new, width, height, tile=20, max_rects=8):
    """Tile-aligned rectangles (x, y, w, h) covering every tile that differs. Rows of changed tiles are merged,
    then stacked where the next row has the same run; more than max_rects rectangles become their bounding box."""
    before, after = tile_crcs(old, width, height, tile), tile_crcs(new, width, height, tile)
    per_row = (width + tile - 1) // tile
    rects, above = [], {}
    for ty in range(0, height, tile):
        rows = min(tile, height - ty)
        row_index = ty // tile
        runs, tx = [], 0
        while tx < width:
            i = row_index * per_row + tx // tile
            if before[i] == after[i]:
                tx += tile
                continue
            x0 = tx
            while tx < width and before[row_index * per_row + tx // tile] != after[row_index * per_row + tx // tile]:
                tx += tile
            runs.append((x0, min(tx, width) - x0))
        row = {}
        for run in runs:
            if run in above:
                i = above[run]
                rx, ry, rw, rh = rects[i]
                rects[i] = (rx, ry, rw, rh + rows)
            else:
                i = len(rects)
                rects.append((run[0], ty, run[1], rows))
            row[run] = i
        above = row
    if len(rects) > max_rects:
        x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
        x1, y1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
        rects = [(x0, y0, x1 - x0, y1 - y0)]
    return rects
