"""
1-bit images in the display profile's wire format, "1bpp-row-msb" with Base64: row-major, MSB = leftmost
pixel, 1 = lit, ceil(width / 8) × height bytes. That is exactly what Pillow's mode "1" tobytes() produces.

pattern() needs nothing; text() and picture() import Pillow when called.

Partial updates send rectangles of an image in the same format: crop() cuts them, changed_rects() finds them,
paste() predicts the result.
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
