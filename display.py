"""
Thin abstraction over the physical e-ink panel.
Set DISPLAY_DRIVER in config.py to match your hardware.
"""
from __future__ import annotations

import logging
import os
from PIL import Image, ImageOps

import config

log = logging.getLogger(__name__)


class PreviewDriver:
    WIDTH  = getattr(config, "DISPLAY_WIDTH",  360)
    HEIGHT = getattr(config, "DISPLAY_HEIGHT", 240)

    def show(self, img: Image.Image) -> None:
        path = os.getenv("AKTIE_PREVIEW_PATH", "preview.png")
        img.convert("L").save(path)
        log.info("Preview saved → %s", path)

    def clear(self) -> None:
        pass

    def sleep(self) -> None:
        pass


class Epd3in52Driver:
    """Waveshare 3.52" — 360×240, same display as the original script."""
    WIDTH  = 360
    HEIGHT = 240

    def __init__(self):
        import sys
        sys.path.insert(0, config.WAVESHARE_LIB)
        from waveshare_epd import epd3in52
        self.epd = epd3in52.EPD()
        self.epd.init()
        self.epd.send_command(0x50)
        self.epd.send_data(0x17)

    def show(self, img: Image.Image) -> None:
        bw = img.convert("1").resize((self.WIDTH, self.HEIGHT))
        self.epd.display(self.epd.getbuffer(bw))
        self.epd.lut_GC()
        self.epd.refresh()

    def clear(self) -> None:
        self.epd.Clear()

    def sleep(self) -> None:
        self.epd.sleep()


class Epd4in26Driver:
    """
    Waveshare 4.26" — 800x480. Uses standard 1-bit mode with dithering.

    init_4GRAY/display_4Gray was tried for real greyscale but a hardware
    diagnostic showed it only produces 2 distinct optical states on this
    panel (not 4), with inconsistent tier-to-shade mapping — so greys
    rendered as solid black instead of an actual grey. Plain 1-bit +
    Floyd-Steinberg dithering (PIL's default) handles large fill areas
    (e.g. the chart) as a clean halftone grey; renderer.py avoids using
    grey for thin text/lines since dithering makes those look washed out.
    """
    WIDTH  = 800
    HEIGHT = 480

    def __init__(self):
        import sys
        sys.path.insert(0, config.WAVESHARE_LIB)
        from waveshare_epd import epd4in26
        self.epd = epd4in26.EPD()
        self.epd.init_Fast()
        self.epd.Clear()

    def show(self, img: Image.Image) -> None:
        bw = img.convert("1").resize((self.WIDTH, self.HEIGHT))
        self.epd.display(self.epd.getbuffer(bw))

    def clear(self) -> None:
        self.epd.Clear()

    def sleep(self) -> None:
        self.epd.sleep()


class Epd2in13V3Driver:
    """Waveshare 2.13" V3 — 250×122."""
    WIDTH  = 250
    HEIGHT = 122

    def __init__(self):
        import sys
        sys.path.insert(0, config.WAVESHARE_LIB)
        from waveshare_epd import epd2in13_V3
        self.epd = epd2in13_V3.EPD()
        self.epd.init()
        self.epd.Clear(0xFF)

    def show(self, img: Image.Image) -> None:
        bw = img.convert("1").resize((self.WIDTH, self.HEIGHT))
        self.epd.display(self.epd.getbuffer(bw))

    def clear(self) -> None:
        self.epd.Clear(0xFF)

    def sleep(self) -> None:
        self.epd.sleep()


_DRIVERS = {
    "preview":    PreviewDriver,
    "epd4in26":   Epd4in26Driver,
    "epd3in52":   Epd3in52Driver,
    "epd2in13v3": Epd2in13V3Driver,
}


def get_driver():
    name = config.DISPLAY_DRIVER
    cls  = _DRIVERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown DISPLAY_DRIVER {name!r}. Choose from: {', '.join(_DRIVERS)}")
    drv = cls()
    log.info("Display: %s (%dx%d)", name, drv.WIDTH, drv.HEIGHT)
    # Propagate dimensions to renderer module
    import renderer
    renderer.W = drv.WIDTH
    renderer.H = drv.HEIGHT
    return drv
