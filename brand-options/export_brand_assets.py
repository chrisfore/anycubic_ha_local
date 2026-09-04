#!/usr/bin/env python3
"""Export the locked brand icons to Home-Assistant brand-asset names/sizes.

Source (512px): v2-06-ace-forward.png (light), v2-06-ace-forward-dark.png (dark).
Outputs into ../brand/:
  icon.png 256, icon@2x.png 512, dark_icon.png 256, dark_icon@2x.png 512
These get copied into custom_components/anycubic/brand/ when the integration is scaffolded.
"""
import os
from PIL import Image

SRC = "brand-options"
DST = "brand"
os.makedirs(DST, exist_ok=True)

JOBS = [
    ("v2-06-ace-forward.png", "icon.png", 256),
    ("v2-06-ace-forward.png", "icon@2x.png", 512),
    ("v2-06-ace-forward-dark.png", "dark_icon.png", 256),
    ("v2-06-ace-forward-dark.png", "dark_icon@2x.png", 512),
]

for src, dst, size in JOBS:
    img = Image.open(f"{SRC}/{src}").convert("RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    img.save(f"{DST}/{dst}")
    print(f"{dst:20s} {size}x{size}  <- {src}")
