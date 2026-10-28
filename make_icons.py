# -*- coding: utf-8 -*-
"""生成 FanControl 应用图标：蓝色圆角底 + 三叶片风扇"""
import math, os
from PIL import Image, ImageDraw

OUT = r"C:\Users\akira\WorkBuddy\2026-10-02-09-42-57\fancontrol-pkg\fancontrol"
SS = 4


def draw_icon(size):
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 底：蓝色圆角方
    pad = S * 0.025
    radius = S * 0.215
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=radius,
                        fill=(55, 138, 221, 255))

    cx = cy = S / 2.0
    ring_r = S * 0.335
    ring_w = max(2, int(S * 0.026))

    # 中心到叶片的距离
    hub_r = S * 0.072
    blade_inner = hub_r * 1.15
    blade_outer = ring_r * 0.90

    # 三片叶片：每片是一个"水滴状"四边形（从中心向外张开的弧形）
    blade_deg = 46          # 叶片张开角度
    for i in range(3):
        base = -90 + i * 120  # 叶片中心朝向
        pts = []
        # 从内到外，左边缘
        for t in (0.0, 0.35, 0.7, 1.0):
            r = blade_inner + (blade_outer - blade_inner) * t
            spread = blade_deg * (1 - t * 0.15) / 2.0
            a = math.radians(base - spread)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        # 外缘弧
        for t in (1.0, 0.8, 0.55, 0.3, 0.0):
            r = blade_outer * (0.80 + 0.20 * math.sin(math.pi * t))
            spread = blade_deg * (1 - t * 0.15) / 2.0
            a = math.radians(base + spread)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        # 右边缘回内
        for t in (1.0, 0.7, 0.35, 0.0):
            r = blade_inner + (blade_outer - blade_inner) * t
            spread = blade_deg * (1 - t * 0.15) / 2.0
            a = math.radians(base + spread)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        d.polygon(pts, fill=(255, 255, 255, 255))

    # 外圈环
    d.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
              outline=(255, 255, 255, 110), width=ring_w)

    # 中心轴
    d.ellipse([cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r],
              fill=(255, 255, 255, 255))
    d.ellipse([cx - hub_r * 0.42, cy - hub_r * 0.42,
               cx + hub_r * 0.42, cy + hub_r * 0.42],
              fill=(45, 120, 196, 255))

    return img.resize((size, size), Image.LANCZOS)


def main():
    for name, size in [
        (os.path.join(OUT, "ICON.PNG"), 128),
        (os.path.join(OUT, "ICON_256.PNG"), 256),
        (os.path.join(OUT, "app", "ui", "images", "icon_64.png"), 64),
        (os.path.join(OUT, "app", "ui", "images", "icon_256.png"), 256),
    ]:
        os.makedirs(os.path.dirname(name), exist_ok=True)
        draw_icon(size).save(name, "PNG")
        print("saved", name, size)


if __name__ == "__main__":
    main()
