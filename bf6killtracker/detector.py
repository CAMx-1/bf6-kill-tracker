"""Per-frame detection of the Battlefield 6 kill notification.

When you get a kill, BF6 shows a skull icon just below and left of the
crosshair, next to a points counter ("100", "205", ...):

* the new icon "pops" in: a translucent square with a growing X is drawn for
  ~0.3 s in the rightmost slot, then the skull fades in inside it;
* further kills while icons are still visible add skulls in the rightmost slot
  and push older ones one slot to the left (a multi-kill from one shot can add
  several at once);
* headshot kills use a red helmeted skull; each icon fades out on its own a
  few seconds later.

For every frame this module measures:

* ``slot_scores`` - how well each icon slot matches a skull template;
* ``pop_score``   - how strongly the rightmost slot looks like the pop square;
* a binary mask of the points counter, used to tell real new kills from
  icons that merely re-appear after being hidden (smoke, flashes, scoreboard).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
TEMPLATE_FILES = ("skull.png", "skull_headshot.png")
# look-alike icons that are NOT kills (damage assist: small skull over a bone)
NEGATIVE_FILES = ("assist.png",)
_BLURS = (1, 9, 17)  # horizontal motion-blur variants of each template
POP_PAD = 5  # template border around the pop square


@dataclass
class HudLayout:
    """HUD geometry measured on a 2560x1440 recording at the default HUD scale.

    Recordings at other 16:9 resolutions (1080p, 4K, ...) are rescaled to
    ``ref_size`` before cropping, so these numbers apply unchanged.
    """

    ref_size: Tuple[int, int] = (2560, 1440)
    slot0_x: int = 955        # left edge of the rightmost skull slot
    slot_pitch: int = 48      # distance between slots; slot k is at slot0_x - k * pitch
    num_slots: int = 5
    icon_y: int = 918         # top edge of the skull icons
    icon_size: Tuple[int, int] = (40, 48)
    tol: int = 7              # search tolerance around each slot (pixels)
    pop_box: Tuple[int, int, int, int] = (948, 922, 50, 50)    # pop square (x, y, w, h)
    score_box: Tuple[int, int, int, int] = (1005, 916, 140, 50)  # points counter (x, y, w, h)

    def slot_xs(self) -> List[int]:
        return [self.slot0_x - k * self.slot_pitch for k in range(self.num_slots)]

    def roi(self) -> Tuple[int, int, int, int]:
        """Smallest crop covering all slot search windows, the pop box and the points box."""
        tw, th = self.icon_size
        px, py, pw, ph = self.pop_box
        bx, by, bw, bh = self.score_box
        x0 = min(min(self.slot_xs()), px - POP_PAD, bx) - self.tol
        y0 = min(self.icon_y, py - POP_PAD, by) - self.tol
        x1 = max(self.slot0_x + tw, px + pw + POP_PAD, bx + bw) + self.tol
        y1 = max(self.icon_y + th, py + ph + POP_PAD, by + bh) + self.tol
        return x0, y0, x1 - x0, y1 - y0

    def shifted(self, dx: int, dy: int) -> "HudLayout":
        def box(b):
            return (b[0] + dx, b[1] + dy, b[2], b[3])

        return HudLayout(ref_size=self.ref_size, slot0_x=self.slot0_x + dx, slot_pitch=self.slot_pitch,
                         num_slots=self.num_slots, icon_y=self.icon_y + dy, icon_size=self.icon_size,
                         tol=self.tol, pop_box=box(self.pop_box), score_box=box(self.score_box))

    @classmethod
    def for_video(cls, width: int, height: int) -> "HudLayout":
        """Default layout for a recording of this size.

        Everything is scaled to 1440 lines. Wider-than-16:9 recordings (ultrawide)
        keep their aspect ratio; the kill notification stays centred on the crosshair.
        """
        base = cls()
        if not width or not height or abs(width / height - 16 / 9) < 0.01:
            return base
        rw = int(round(1440 * width / height / 2)) * 2
        out = HudLayout(**{**base.__dict__, "ref_size": (rw, 1440)})
        return out.shifted((rw - 2560) // 2, 0)

    def scaled(self, s: float) -> "HudLayout":
        """Layout for a non-default in-game HUD scale (scales around screen centre)."""
        cx, cy = self.ref_size[0] / 2, self.ref_size[1] / 2

        def fx(v):
            return int(round(cx + (v - cx) * s))

        def fy(v):
            return int(round(cy + (v - cy) * s))

        def box(b):
            return (fx(b[0]), fy(b[1]), int(round(b[2] * s)), int(round(b[3] * s)))

        return HudLayout(
            ref_size=self.ref_size, slot0_x=fx(self.slot0_x), slot_pitch=int(round(self.slot_pitch * s)),
            num_slots=self.num_slots, icon_y=fy(self.icon_y),
            icon_size=(int(round(self.icon_size[0] * s)), int(round(self.icon_size[1] * s))),
            tol=self.tol, pop_box=box(self.pop_box), score_box=box(self.score_box),
        )


@dataclass
class FrameObservation:
    slot_scores: np.ndarray                     # (num_slots,) best skull-template score per slot
    pop_score: float = 0.0                      # pop-square score of the rightmost slot
    score_mask: Optional[np.ndarray] = field(default=None, repr=False)  # bright pixels of the points box
    score_mask_fill: float = 0.0                # fraction of bright pixels in the points box


def _grad(gray: np.ndarray) -> np.ndarray:
    g = cv2.GaussianBlur(gray.astype(np.float32), (3, 3), 0)
    return cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))


class SkullDetector:
    def __init__(self, layout: Optional[HudLayout] = None, template_paths: Optional[Sequence[str]] = None,
                 min_contrast: float = 45.0):
        self.layout = layout or HudLayout()
        self.min_contrast = min_contrast
        L = self.layout
        paths = template_paths or [os.path.join(TEMPLATE_DIR, f) for f in TEMPLATE_FILES]
        self.templates: List[np.ndarray] = []
        self.body_masks: List[np.ndarray] = []  # icon pixels (vs eye holes / background) per template
        for p in paths:
            img = cv2.imread(p, cv2.IMREAD_COLOR)
            if img is None:
                raise FileNotFoundError(f"Template not found: {p}")
            if img.shape[1::-1] != tuple(L.icon_size):
                img = cv2.resize(img, L.icon_size, interpolation=cv2.INTER_AREA)
            red = img[:, :, 2]  # white and red skulls are both bright in the red channel
            body = red > (int(red.min()) + int(red.max())) // 2
            for k in _BLURS:
                self.body_masks.append(body)
                if k == 1:
                    self.templates.append(red)
                else:
                    kernel = np.full((1, k), 1.0 / k, np.float32)
                    self.templates.append(cv2.filter2D(red, -1, kernel))
        self.negatives: List[np.ndarray] = []
        for f in NEGATIVE_FILES:
            img = cv2.imread(os.path.join(TEMPLATE_DIR, f), cv2.IMREAD_COLOR)
            if img is None:
                raise FileNotFoundError(f"Template not found: {f}")
            if img.shape[1::-1] != tuple(L.icon_size):
                img = cv2.resize(img, L.icon_size, interpolation=cv2.INTER_AREA)
            self.negatives.append(img[:, :, 2])
        # pop square: matched on gradient magnitude so it works on light and dark backgrounds
        _, _, pw, ph = L.pop_box
        sq = np.zeros((ph + 10, pw + 10), np.float32)
        sq[5:5 + ph, 5:5 + pw] = 120
        self.pop_template = _grad(sq)
        self.roi = L.roi()

    def observe(self, roi_bgr: np.ndarray) -> FrameObservation:
        L = self.layout
        rx, ry, _, _ = self.roi
        tw, th = L.icon_size
        red = roi_bgr[:, :, 2]
        y0 = L.icon_y - L.tol - ry
        y1 = L.icon_y + th + L.tol - ry
        scores = np.zeros(L.num_slots, np.float32)
        for k, sx in enumerate(L.slot_xs()):
            x0 = sx - L.tol - rx
            x1 = sx + tw + L.tol - rx
            patch = red[y0:y1, x0:x1]
            best, best_loc, best_t = -1.0, (0, 0), 0
            for ti, tpl in enumerate(self.templates):
                _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(patch, tpl, cv2.TM_CCOEFF_NORMED))
                if mx > best:
                    best, best_loc, best_t = mx, loc, ti
            # a look-alike non-kill icon matches its own template better
            for neg in self.negatives:
                if float(cv2.matchTemplate(patch, neg, cv2.TM_CCOEFF_NORMED).max()) > best:
                    best = min(best, 0.3)
                    break
            # correlation ignores contrast: a real icon must stand out from its surroundings
            # in at least one colour channel (white on yellow grass shows up in blue,
            # red headshot skulls in red), and its own pixels must not be yellow/green
            cx, cy = x0 + best_loc[0], y0 + best_loc[1]
            icon = roi_bgr[cy:cy + th, cx:cx + tw].astype(np.float32)
            body = self.body_masks[best_t]
            if icon.shape[:2] == body.shape:
                inside, outside = icon[body], icon[~body]
                contrast = float((inside.mean(axis=0) - outside.mean(axis=0)).max())
                b, g, r = inside.mean(axis=0)
                if contrast < self.min_contrast or (g - b > 60 and b < 140 and r > 150):
                    best = min(best, 0.3)
            scores[k] = best

        px, py, pw, ph = L.pop_box
        m = L.tol + POP_PAD
        pop_patch = cv2.cvtColor(roi_bgr[py - m - ry:py + ph + m - ry, px - m - rx:px + pw + m - rx], cv2.COLOR_BGR2GRAY)
        pop = float(cv2.matchTemplate(_grad(pop_patch), self.pop_template, cv2.TM_CCOEFF_NORMED).max())

        bx, by, bw, bh = L.score_box
        gray = cv2.cvtColor(roi_bgr[by - ry:by - ry + bh, bx - rx:bx - rx + bw], cv2.COLOR_BGR2GRAY)
        mask = gray > 200
        return FrameObservation(scores, pop, mask, float(mask.mean()))
