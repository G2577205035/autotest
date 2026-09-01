"""Cross-platform CJK font selection for Matplotlib report charts."""

from __future__ import annotations

import os
from pathlib import Path


def _font_paths() -> tuple[Path, ...]:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return (
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        windows / "msyh.ttc",
        windows / "simhei.ttf",
        windows / "simsun.ttc",
    )


def _register_font(path: Path) -> str:
    from matplotlib import font_manager

    font_manager.fontManager.addfont(str(path))
    return str(font_manager.FontProperties(fname=str(path)).get_name() or "").strip()


def configure_matplotlib_cjk() -> str:
    """Select a real installed CJK font instead of an unavailable family name.

    Assigning ``font.sans-serif`` does not validate that a font exists.  On the
    production Linux image that silently fell back to DejaVu Sans, which drew
    Chinese report labels as square boxes.  Registering the bundled font file
    directly also avoids stale Matplotlib font-cache results.
    """

    from matplotlib import font_manager, rcParams

    selected = ""
    for path in _font_paths():
        if not path.is_file():
            continue
        try:
            selected = _register_font(path)
        except Exception:
            continue
        if selected:
            break

    if not selected:
        for family in (
            "Noto Sans CJK SC",
            "Noto Sans CJK JP",
            "Microsoft YaHei",
            "SimHei",
            "Arial Unicode MS",
            "WenQuanYi Zen Hei",
            "Malgun Gothic",
        ):
            try:
                font_manager.findfont(family, fallback_to_default=False)
            except (ValueError, OSError):
                continue
            selected = family
            break

    rcParams["font.family"] = "sans-serif"
    rcParams["font.sans-serif"] = [selected, "DejaVu Sans"] if selected else ["DejaVu Sans"]
    rcParams["axes.unicode_minus"] = False
    return selected
