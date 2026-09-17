#!/usr/bin/env python3
"""Generate the app icon set (tray.png, tray.ico, macOS .icns).

Usage:  python scripts/make_icon.py
Output: src/static/img/tray.png, tray.ico, and (macOS only) tray.icns.

A white eighth-note on the app's brand blue; drawn with primitives so it
needs no font files and renders legibly down to 16px tray size.
"""
import os
import subprocess
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit('Pillow is required: pip install pillow')

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
IMG_DIR = os.path.join(ROOT, 'src', 'static', 'img')
BLUE = (52, 152, 219, 255)
WHITE = (255, 255, 255, 255)


def draw_note(size=256):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256.0
    # Rounded-square background.
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(56 * s), fill=BLUE)
    # Note head (tilted ellipse).
    head = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(head)
    hd.ellipse([52 * s, 168 * s, 128 * s, 226 * s], fill=WHITE)
    head = head.rotate(20, resample=Image.BICUBIC, center=(90 * s, 197 * s))
    img.alpha_composite(head)
    # Stem.
    d.rectangle([118 * s, 48 * s, 134 * s, 190 * s], fill=WHITE)
    # Flag.
    d.polygon([(126 * s, 48 * s), (196 * s, 74 * s), (196 * s, 104 * s),
               (126 * s, 78 * s)], fill=WHITE)
    return img


def main():
    os.makedirs(IMG_DIR, exist_ok=True)
    master = draw_note(256)
    master.save(os.path.join(IMG_DIR, 'tray.png'))
    master.save(os.path.join(IMG_DIR, 'tray.ico'),
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    print('wrote tray.png + tray.ico')
    if sys.platform == 'darwin':
        iconset = os.path.join(IMG_DIR, 'tray.iconset')
        os.makedirs(iconset, exist_ok=True)
        for name, px in [('16x16', 16), ('16x16@2x', 32), ('32x32', 32),
                         ('32x32@2x', 64), ('128x128', 128),
                         ('128x128@2x', 256), ('256x256', 256),
                         ('256x256@2x', 512), ('512x512', 512),
                         ('512x512@2x', 1024)]:
            master.resize((px, px), Image.LANCZOS).save(
                os.path.join(iconset, f'icon_{name}.png'))
        subprocess.run(['iconutil', '-c', 'icns', iconset, '-o',
                        os.path.join(IMG_DIR, 'tray.icns')], check=True)
        print('wrote tray.icns')


if __name__ == '__main__':
    main()
