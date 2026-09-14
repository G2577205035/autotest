"""Shared PDF CJK font selection without application configuration imports."""
import os
from pathlib import Path


def pdf_font_name():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf'),
        Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'msyh.ttc',
        Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'simhei.ttf',
        Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'simsun.ttc',
    ]
    for path in candidates:
        if path.is_file():
            try:
                pdfmetrics.registerFont(TTFont('LiemaCJK', str(path), subfontIndex=0))
                return 'LiemaCJK'
            except Exception:
                continue
    try:
        pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
        return 'STSong-Light'
    except Exception:
        return 'Helvetica'
