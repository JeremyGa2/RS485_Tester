"""Génère assets/app.ico (exécuté au moment du build, nécessite Pillow)."""
import os
from PIL import Image, ImageDraw, ImageFont

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(os.path.join(here, "assets"), exist_ok=True)
S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=52, fill=(37, 99, 235, 255))
# signal carré (trame série)
pts, x, y_hi, y_lo = [], 34, 70, 120
for lvl in (1, 0, 1, 0, 1, 1, 0, 1):
    y = y_hi if lvl else y_lo
    if pts:
        pts.append((x, y))
    else:
        pts.append((x, y))
    x += 24
    pts.append((x, y))
d.line(pts, fill=(255, 255, 255, 255), width=10, joint="curve")
try:
    font = ImageFont.truetype("arialbd.ttf", 78)
except OSError:
    font = ImageFont.load_default()
d.text((S / 2, 188), "485", fill=(255, 255, 255, 255), font=font, anchor="mm")
img.save(os.path.join(here, "assets", "app.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                        (128, 128), (256, 256)])
print("assets/app.ico créé")
