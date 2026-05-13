"""
Pulsar icon generator.
Produces pulsar.ico (multi-resolution) and creates a desktop shortcut.
Run once:  python make_icon.py
"""
import math
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


DIR = Path(__file__).parent


# -- Icon rendering --------------------------------------------------------

def _draw_pulsar(size: int) -> Image.Image:
    img  = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    s  = size / 256          # scale factor from 256-px base

    # Background: rounded dark-navy circle
    pad = max(1, int(size * 0.04))
    draw.ellipse([pad, pad, size - pad - 1, size - pad - 1],
                 fill=(0, 49, 61, 255))          # #00313D Ebury darkest

    # Emission jets — two opposing beams (up / down)
    jet_len = int(cx * 0.88)
    for angle_deg in (270, 90):
        angle = math.radians(angle_deg)
        for r in range(2, jet_len):
            t     = r / jet_len
            alpha = int(230 * (1 - t) ** 1.3)
            w     = max(1, int((6 - t * 5) * s))
            x     = cx + math.cos(angle) * r
            y     = cy + math.sin(angle) * r
            draw.ellipse([x - w, y - w, x + w, y + w],
                         fill=(0, 190, 240, alpha))   # #00BEF0 Ebury cyan

    # Glow halo around core
    halo_r = int(size * 0.18)
    for r in range(halo_r, 0, -1):
        alpha = int(90 * (1 - r / halo_r) ** 1.5)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     fill=(0, 151, 189, alpha))       # #0097BD Ebury medium

    # Glowing core — soft white bloom
    core_r = max(2, int(size * 0.11))
    for r in range(core_r, 0, -1):
        t     = r / core_r
        alpha = int(255 * (1 - t ** 0.55))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     fill=(255, 255, 255, alpha))

    # Brilliant centre point
    c2 = max(1, int(size * 0.032))
    draw.ellipse([cx - c2, cy - c2, cx + c2, cy + c2],
                 fill=(255, 255, 255, 255))

    # Soft outer glow via blur overlay
    if size >= 48:
        glow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        gd   = ImageDraw.Draw(glow)
        gr   = int(size * 0.13)
        gd.ellipse([cx - gr, cy - gr, cx + gr, cy + gr],
                   fill=(0, 190, 240, 120))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=int(size * 0.06)))
        img  = Image.alpha_composite(img, glow)

    return img


def make_ico(path: Path):
    sizes   = [256, 128, 64, 48, 32, 16]
    base    = _draw_pulsar(256)
    images  = []
    for s in sizes:
        img = base.resize((s, s), Image.LANCZOS) if s < 256 else base.copy()
        images.append(img.convert('RGBA'))

    images[0].save(
        path, format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f'  icon  -> {path}')


# -- Desktop shortcut ------------------------------------------------------

def create_shortcut(icon_path: Path):
    pythonw  = DIR / 'env' / 'Scripts' / 'pythonw.exe'
    script   = DIR / 'pulsar.py'
    desktop  = Path(os.path.expandvars(r'%USERPROFILE%\Desktop'))
    lnk_path = desktop / 'Pulsar.lnk'

    ps = f"""
$ws  = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut('{lnk_path}')
$lnk.TargetPath       = '{pythonw}'
$lnk.Arguments        = '"{script}"'
$lnk.WorkingDirectory = '{DIR}'
$lnk.IconLocation     = '{icon_path},0'
$lnk.Description      = 'Pulsar — workday tracker'
$lnk.WindowStyle      = 1
$lnk.Save()
Write-Host "shortcut -> {lnk_path}"
"""
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command', ps],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f'  link  -> {lnk_path}')
    else:
        print('  shortcut failed:', result.stderr.strip())


# -- Entry point -----------------------------------------------------------

def main():
    print('Pulsar setup')
    print('-' * 40)
    ico = DIR / 'pulsar.ico'
    make_ico(ico)
    create_shortcut(ico)
    print('-' * 40)
    print('Done. Double-click "Pulsar" on your desktop to launch.')


if __name__ == '__main__':
    main()
