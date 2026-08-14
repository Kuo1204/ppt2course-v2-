"""Dev-only tool: draws the bundled placeholder 2D avatar with PyMuPDF's
vector shape API and rasterizes each state to a transparent PNG under
``src/ppt2course/assets/avatar/default/``.

This is a flat-icon-style placeholder, not real character art — it exists so
the Avatar feature has *something* to render out of the box. Run it again
any time the placeholder design changes; it is not invoked at request time
(the server just reads the PNG files it produces).

    python scripts/generate_default_avatar.py
"""

import os

import pymupdf

OUT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "src", "ppt2course", "assets", "avatar", "default"
)

# Canvas in points; rendered at 2x zoom below for a crisper raster. Portrait
# bust framing (head + shoulders, cropped at the bottom of the canvas) so it
# reads clearly even scaled down to the "small" corner-overlay size.
W, H = 400, 480
ZOOM = 2

SKIN = (0.98, 0.82, 0.66)
BODY = (0.23, 0.51, 0.96)  # matches the frontend's --accent blue
BODY_DARK = (0.15, 0.37, 0.78)
EYE = (0.15, 0.15, 0.18)
MOUTH = (0.55, 0.18, 0.2)
OUTLINE = (0.10, 0.12, 0.16)

HEAD_CX, HEAD_CY, HEAD_R = 200, 190, 92
EYE_DX, EYE_DY, EYE_R = 34, -8, 8


def _base_shape(page: "pymupdf.Page") -> "pymupdf.Shape":
    shape = page.new_shape()

    # Shoulders/torso, cropped by the page's bottom edge.
    torso = pymupdf.Rect(HEAD_CX - 150, 330, HEAD_CX + 150, H + 40)
    shape.draw_rect(torso)
    shape.finish(color=OUTLINE, fill=BODY, width=3, closePath=True)

    # Neck.
    neck = pymupdf.Rect(HEAD_CX - 28, 255, HEAD_CX + 28, 300)
    shape.draw_rect(neck)
    shape.finish(color=OUTLINE, fill=SKIN, width=2, closePath=True)

    # Head.
    shape.draw_circle((HEAD_CX, HEAD_CY), HEAD_R)
    shape.finish(color=OUTLINE, fill=SKIN, width=3)

    # Eyes.
    for sign in (-1, 1):
        shape.draw_circle((HEAD_CX + sign * EYE_DX, HEAD_CY + EYE_DY), EYE_R)
        shape.finish(color=None, fill=EYE)

    return shape


def _draw_mouth(shape: "pymupdf.Shape", state: str) -> None:
    cx, cy = HEAD_CX, HEAD_CY + 46
    if state == "talk_open":
        r = pymupdf.Rect(cx - 26, cy - 16, cx + 26, cy + 16)
        shape.draw_oval(r)
        shape.finish(color=OUTLINE, fill=MOUTH, width=2)
    elif state == "talk_close":
        r = pymupdf.Rect(cx - 24, cy - 5, cx + 24, cy + 5)
        shape.draw_oval(r)
        shape.finish(color=OUTLINE, fill=MOUTH, width=2)
    else:
        # Idle: a gentle closed-mouth smile (a shallow arc).
        p1 = pymupdf.Point(cx - 26, cy - 4)
        p2 = pymupdf.Point(cx, cy + 12)
        p3 = pymupdf.Point(cx + 26, cy - 4)
        shape.draw_bezier(p1, p1, p2, p2)
        shape.draw_bezier(p2, p2, p3, p3)
        shape.finish(color=OUTLINE, width=3, fill=None, closePath=False)


def _draw_arm_point(shape: "pymupdf.Shape") -> None:
    # Extended arm + finger, angled up-right as if gesturing at on-screen
    # content beside the avatar.
    shoulder = pymupdf.Point(HEAD_CX + 130, 360)
    hand = pymupdf.Point(HEAD_CX + 250, 250)
    shape.draw_line(shoulder, hand)
    shape.finish(color=BODY_DARK, width=34, closePath=False, lineCap=1)
    shape.draw_circle((hand.x, hand.y), 20)
    shape.finish(color=OUTLINE, fill=SKIN, width=2)


def _draw_arm_wave(shape: "pymupdf.Shape") -> None:
    # Raised arm beside the head, hand roughly level with the eyes.
    shoulder = pymupdf.Point(HEAD_CX - 130, 360)
    elbow = pymupdf.Point(HEAD_CX - 175, 260)
    hand = pymupdf.Point(HEAD_CX - 140, 170)
    shape.draw_line(shoulder, elbow)
    shape.finish(color=BODY_DARK, width=34, closePath=False, lineCap=1)
    shape.draw_line(elbow, hand)
    shape.finish(color=BODY_DARK, width=30, closePath=False, lineCap=1)
    shape.draw_circle((hand.x, hand.y), 22)
    shape.finish(color=OUTLINE, fill=SKIN, width=2)


def render_state(state: str) -> "pymupdf.Pixmap":
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    shape = _base_shape(page)
    if state == "point":
        _draw_arm_point(shape)
    elif state == "wave":
        _draw_arm_wave(shape)
    shape.commit()

    # Mouth drawn in its own shape, on top, for every state.
    mouth_shape = page.new_shape()
    _draw_mouth(mouth_shape, state)
    mouth_shape.commit()

    pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=True)
    return pix


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for state in ("idle", "talk_open", "talk_close", "point", "wave"):
        pix = render_state(state)
        out_path = os.path.join(OUT_DIR, f"{state}.png")
        pix.save(out_path)
        print(f"wrote {out_path} ({pix.width}x{pix.height})")


if __name__ == "__main__":
    main()
