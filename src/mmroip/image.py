"""
1-bit images in the display profile's wire format, "1bpp-row-msb" with Base64: row-major, MSB = leftmost
pixel, 1 = lit, ceil(width / 8) × height bytes. That is exactly what Pillow's mode "1" tobytes() produces.

pattern() needs nothing; text() and picture() import Pillow when called.
"""

import base64


def image_size(definition):
    """(width, height) of the "image" object in a /definition document."""
    for obj in definition.get("objects", []):
        if obj.get("id") == "image":
            return obj["profile"]["width_px"], obj["profile"]["height_px"]
    raise ValueError("this endpoint has no image object")


def encode(data):
    """Base64 text for an image object's desired state."""
    return base64.b64encode(data).decode()


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
