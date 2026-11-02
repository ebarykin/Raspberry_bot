"""HUD парктроника: cairo на teleop BGRx.

Геометрия (FOV, высота, кольца, коридор) — из cameras[].parktronic.
Палитра, штрихи, уголки и заливка — здесь.
"""

from __future__ import annotations

import math
from typing import Any

from ...config import ParktronicOverlayConfig

PARK_ELEMENT_NAME = "park0"

_CYAN = (0.30, 0.92, 0.88)
_AMBER = (0.98, 0.72, 0.22)
_CHROME = (0.82, 0.90, 0.92)
_TAU = 6.283185307179586
_RING_DASH = (7.0, 5.5)


def _y_max_m(params: ParktronicOverlayConfig) -> float:
    far = params.rings_m[-1]
    hw = params.rail_half_width_m
    return math.sqrt(far * far - hw * hw)


def draw_parktronic_guides(
    cr: Any,
    width: int,
    height: int,
    params: ParktronicOverlayConfig | None = None,
) -> None:
    """Янтарные рельсы коридора, пунктирные изоляции и перекрестье."""
    spec = params if params is not None else ParktronicOverlayConfig()
    w = float(width)
    h = float(height)
    cx, cy = w * 0.50, h * 0.50
    fx = (w / 2.0) / math.tan(math.radians(spec.hfov_deg / 2.0))
    fy = (h / 2.0) / math.tan(math.radians(spec.vfov_deg / 2.0))
    pitch = math.radians(spec.pitch_deg)
    s, c = math.sin(pitch), math.cos(pitch)
    hair = max(0.95, h / 380.0)
    y_min = spec.y_min_m
    y_max = _y_max_m(spec)
    hw = spec.rail_half_width_m
    rings = spec.rings_m
    fill_style = spec.fill_style

    def project(x: float, y: float) -> tuple[float, float] | None:
        yc = -y * s + spec.height_m * c
        zc = y * c + spec.height_m * s
        if zc <= 1e-4:
            return None
        return cx + fx * x / zc, cy + fy * yc / zc

    def in_frame(u: float, v: float, pad: float = 36.0) -> bool:
        return -pad <= u <= w + pad and -pad <= v <= h + pad

    def set_rgb(rgb: tuple[float, float, float], alpha: float, lw: float | None = None) -> None:
        cr.set_source_rgba(rgb[0], rgb[1], rgb[2], alpha)
        if lw is not None:
            cr.set_line_width(lw)

    def stroke_poly(
        pts: list[tuple[float, float]],
        rgb: tuple[float, float, float],
        alpha: float,
        lw: float,
        dash: tuple[float, float] | None = None,
    ) -> None:
        vis = [(u, v) for u, v in pts if in_frame(u, v)]
        if len(vis) < 2:
            return
        if dash is not None:
            cr.set_dash(list(dash))
        set_rgb(rgb, alpha, lw)
        cr.move_to(vis[0][0], vis[0][1])
        for u, v in vis[1:]:
            cr.line_to(u, v)
        cr.stroke()
        if dash is not None:
            cr.set_dash([])

    cr.set_line_cap(1)  # CAIRO_LINE_CAP_ROUND
    cr.set_line_join(1)
    cr.select_font_face("Sans", 0, 0)

    arm, inset = 16.0, 8.0
    set_rgb(_CHROME, 0.55, hair)
    for x, y, sx, sy in (
        (inset, inset, 1, 1),
        (w - inset, inset, -1, 1),
        (inset, h - inset, 1, -1),
        (w - inset, h - inset, -1, -1),
    ):
        cr.move_to(x + sx * arm, y)
        cr.line_to(x, y)
        cr.line_to(x, y + sy * arm)
        cr.stroke()

    gap, ar, rad = 3.0, 9.0, 3.0
    set_rgb(_CHROME, 0.70, hair)
    cr.move_to(cx - ar, cy)
    cr.line_to(cx - gap, cy)
    cr.stroke()
    cr.move_to(cx + gap, cy)
    cr.line_to(cx + ar, cy)
    cr.stroke()
    cr.move_to(cx, cy - ar)
    cr.line_to(cx, cy - gap)
    cr.stroke()
    cr.move_to(cx, cy + gap)
    cr.line_to(cx, cy + ar)
    cr.stroke()
    cr.arc(cx, cy, rad, 0.0, _TAU)
    cr.stroke()

    n_rail = 48
    rails: list[list[tuple[float, float]]] = []
    for sign in (-1.0, 1.0):
        pts = []
        for i in range(n_rail):
            y = y_min + (y_max - y_min) * i / (n_rail - 1)
            p = project(sign * hw, y)
            if p is not None:
                pts.append(p)
        rails.append(pts)

    def fill_ground(pts: list[tuple[float, float]], alpha: float) -> None:
        proj = [project(x, y) for x, y in pts]
        if any(p is None for p in proj):
            return
        set_rgb(_AMBER, alpha)
        cr.move_to(proj[0][0], proj[0][1])
        for u, v in proj[1:]:
            cr.line_to(u, v)
        cr.close_path()
        cr.fill()

    def y_on_far_ring(x: float) -> float:
        return math.sqrt(max(0.0, rings[-1] ** 2 - x * x))

    if fill_style == "uniform" and len(rails[0]) >= 2 and len(rails[1]) >= 2:
        set_rgb(_AMBER, 0.14)
        cr.move_to(rails[0][0][0], rails[0][0][1])
        for u, v in rails[0][1:]:
            cr.line_to(u, v)
        for u, v in reversed(rails[1]):
            cr.line_to(u, v)
        cr.close_path()
        cr.fill()
    elif fill_style == "fade":
        n = 22
        for i in range(n - 1):
            t0 = i / (n - 1)
            t1 = (i + 1) / (n - 1)
            y0 = y_min + t0 * (y_max - y_min)
            y1 = y_min + t1 * (y_max - y_min)
            fill_ground(
                ((-hw, y0), (hw, y0), (hw, y1), (-hw, y1)),
                0.20 * (1.0 - t0) ** 1.4,
            )
    elif fill_style == "edge":
        falloff = 0.20
        n = 8
        for i in range(n):
            t0 = i / n
            t1 = (i + 1) / n
            alpha = 0.13 * (1.0 - t0)
            for sign in (-1.0, 1.0):
                x_outer = sign * (hw - falloff * t0)
                x_inner = sign * (hw - falloff * t1)
                y_outer = y_on_far_ring(x_outer)
                y_inner = y_on_far_ring(x_inner)
                fill_ground(
                    (
                        (x_outer, y_min),
                        (x_inner, y_min),
                        (x_inner, y_inner),
                        (x_outer, y_outer),
                    ),
                    alpha,
                )
    elif fill_style == "band":
        y_1m = math.sqrt(max(0.0, rings[0] ** 2 - hw * hw))
        n = 10
        span = y_max - y_1m
        for i in range(n - 1):
            t0 = i / (n - 1)
            t1 = (i + 1) / (n - 1)
            fill_ground(
                (
                    (-hw, y_1m + t0 * span),
                    (hw, y_1m + t0 * span),
                    (hw, y_1m + t1 * span),
                    (-hw, y_1m + t1 * span),
                ),
                0.16 * (1.0 - 0.5 * t0),
            )

    for pts in rails:
        stroke_poly(pts, _AMBER, 0.35, hair * 4.4)
        stroke_poly(pts, _AMBER, 1.0, hair * 2.15)

    cr.set_font_size(max(10.0, h / 34.0))
    n_ring = 64
    ang_span = math.pi * 0.42
    for rm in rings:
        pts = []
        for i in range(n_ring):
            ang = -ang_span + 2.0 * ang_span * i / (n_ring - 1)
            x = rm * math.sin(ang)
            y = rm * math.cos(ang)
            if y < y_min:
                continue
            p = project(x, y)
            if p is not None:
                pts.append(p)
        stroke_poly(pts, _CYAN, 0.14, hair * 2.2, _RING_DASH)
        stroke_poly(pts, _CYAN, 0.86, hair, _RING_DASH)
        vis = [(u, v) for u, v in pts if 8.0 < u < w - 52.0 and 12.0 < v < h - 8.0]
        if not vis:
            continue
        u, v = max(vis, key=lambda p: p[0])
        label = f"{rm:g} m"
        tx, ty = min(w - 50.0, u + 6.0), v + 4.0
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.55)
        cr.move_to(tx + 1.0, ty + 1.0)
        cr.show_text(label)
        set_rgb(_CYAN, 0.95)
        cr.move_to(tx, ty)
        cr.show_text(label)
