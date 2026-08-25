"""Render a SKU's main image from a template.

The template is a base image plus positioned layers. Coordinates are stored as
0..1 fractions of the canvas so the browser editor and this renderer agree without
either knowing the other's pixel dimensions -- the editor can show the base image
scaled to fit a panel and still produce coordinates that land correctly on a
4000px original.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# Marketplace minimum is 700x900; anything smaller is rejected on upload.
MIN_WIDTH, MIN_HEIGHT = 700, 900


class TemplateRenderError(RuntimeError):
    pass


@dataclass(slots=True)
class RenderedImage:
    content: bytes
    width: int
    height: int


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _load_font(path: str | None, size: int) -> ImageFont.ImageFont:
    if not path:
        return ImageFont.load_default()
    key = (path, size)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(path, size)
        except OSError as exc:
            raise TemplateRenderError(f"could not load font {path!r}: {exc}") from exc
    return _font_cache[key]


def render(
    base_image_path: str | Path,
    layers: list[dict],
    values: dict[str, Any],
    *,
    quality: int = 92,
) -> RenderedImage:
    """Compose one image.

    ``values`` are the SKU's axis values, interpolated into each text layer's
    ``text`` format string -- so a layer of ``"{w}"`` prints the width.
    """
    try:
        img = Image.open(base_image_path)
    except OSError as exc:
        raise TemplateRenderError(f"could not open base image {base_image_path}: {exc}") from exc

    img = img.convert("RGB")
    width, height = img.size
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise TemplateRenderError(
            f"base image is {width}x{height}; the marketplace requires at least "
            f"{MIN_WIDTH}x{MIN_HEIGHT}"
        )

    draw = ImageDraw.Draw(img)

    for i, layer in enumerate(layers or []):
        kind = layer.get("type", "text")
        if kind != "text":
            raise TemplateRenderError(f"layer {i}: unsupported type {kind!r}")

        template = layer.get("text", "")
        try:
            text = template.format(**values)
        except KeyError as exc:
            raise TemplateRenderError(
                f"layer {i}: text refers to {exc}, which is not an axis of this line"
            ) from exc

        font = _load_font(layer.get("font"), int(layer.get("size", 48)))
        x = float(layer.get("x", 0.5)) * width
        y = float(layer.get("y", 0.5)) * height
        anchor = layer.get("anchor", "mm")
        fill = layer.get("color", "#000000")

        draw.text((x, y), text, font=font, fill=fill, anchor=anchor)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return RenderedImage(content=buf.getvalue(), width=width, height=height)


def preview(base_image_path: str | Path, layers: list[dict], values: dict[str, Any]) -> bytes:
    """Smaller render for the template editor's live preview."""
    out = render(base_image_path, layers, values, quality=80)
    img = Image.open(io.BytesIO(out.content))
    img.thumbnail((800, 800))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()
