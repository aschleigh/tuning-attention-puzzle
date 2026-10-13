"""Hand-tracked live-camera jigsaw, single process, two hands.

Pieces are cells of the live camera image. Drag any piece with a pinch and drop it
on the lattice; it snaps to the nearest free cell. Any piece mates with any other.
A piece is silent until you first pick it up, and gets clearer as its group grows.

This script owns the webcam, runs MediaPipe itself, and sends OSC to UDP 7500:
the repo tracker's /cv/hand/... addresses, plus a /puzzle/... namespace carrying
the state of the puzzle itself (see OSC_SCHEMA below).

Do NOT run `track-demo` at the same time, and do not leave another patch with a
receiver on 7500 open — one receiver per port.

Run from the repo root:
    uv run python puzzle_tracked.py

Keys: O = record open-hand calibration, C = record pinched calibration,
      R = resend every OSC value (use after opening or reopening a Max patch).
"""
import glob, math, os, random, re, statistics, sys, time
from collections import deque

import cv2
import mediapipe as mp
import pygame
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision
from pythonosc.udp_client import SimpleUDPClient

# ---- settings to tune --------------------------------------------------
SWAP_LABELS = True         # MediaPipe's Left/Right came out swapped for you; set False if hands are crossed over
CAL = {"left": [0.90, 0.15], "right": [0.90, 0.15]}  # [open, pinched] ratio per hand; press O / C in the app to record
GRAB, RELEASE = 0.75, 0.55 # on the rescaled 0..1 pinch
REWIND = 0.15              # seconds to rewind the piece on release
CAM_INDEX = 0
USE_MOUSE = False          # True = test the puzzle with the mouse (acts as the right hand)
COLS, ROWS = 4, 3          # the camera picture is cut into COLS x ROWS pieces
BOARD_H = 480              # pieces shrink as you add rows
CH = BOARD_H // ROWS       # piece width is computed from the camera's aspect ratio
TAB = CH // 5
W, H = 1400, 800
STATUS_H = 70              # space kept free at the bottom for text

# ---- audio -------------------------------------------------------------
LOCAL_AUDIO = True         # True: pygame plays the clips here (works with nothing else running)
                           # False: no sound here at all; Max plays, driven by the OSC below
SOUND_DIR = "sounds"       # sounds/piece_0/stage_00.wav ... stage_09.wav (made by generate_clips.py)
GAIN = 0.6                 # per-piece volume; lower it if the loops add up too loud
FADE = 0.4                 # seconds for a piece to fade in; try ~1.0 with HOLD_ONLY
HOLD_ONLY = False          # False: a piece stays in the mix once you've picked it up
                           # True: a piece sounds only while a hand is actually holding it
STAGES_FALLBACK = 10       # how many clarity stages to report when no clips are loaded here

# ---- OSC ---------------------------------------------------------------
SEND_OSC = True
OSC_ADDR = ("127.0.0.1", 7500)   # ("192.168.1.42", 7500) to reach Max on another machine
OSC_HZ = 20                      # rate cap for continuous values; events are sent immediately
RESEND_EVERY = 4.0               # seconds; resend everything so a late-opened patch catches up
AMP_EPS = 0.01                   # don't resend a volume that moved less than this
# ------------------------------------------------------------------------

OSC_SCHEMA = """
  /cv/hand/<side>/present      0 | 1
  /cv/hand/<side>/index_tip    x y z          (x un-mirrored, to match the repo tracker)
  /cv/hand/<side>/thumb_tip    x y z
  /cv/hand/<side>/wrist        x y z
  /cv/hand/<side>/pinch        raw            (thumb-index distance, image units)
  /cv/hand/<side>/size         s              (wrist-to-knuckle distance: bigger = nearer)

  /puzzle/hello      npieces cols rows nstages      (on start, and on every resend)
  /puzzle/grab       i hand                         (hand: 0 = left, 1 = right)
  /puzzle/drop       i gx gy clustersize
  /puzzle/stage      i stage                        (0 = murky .. nstages-1 = clear)
  /puzzle/amp        i amp                          (0..1, already faded)
  /puzzle/clarity    i clarity                      (0..1, smoothed, matches the blur)
  /puzzle/cell       i gx gy                        (-1 -1 while a hand holds it)
  /puzzle/cluster    largest ngroups
  /puzzle/progress   0..1
  /puzzle/solved     1                              (once, when every piece is one group)
"""

NAMES = ("left", "right")
HAND_ID = {"left": 0, "right": 1}
COLORS = {"left": (110, 170, 255), "right": (255, 255, 255)}

models = glob.glob("models/*hand*")
if not models:
    sys.exit(f"No hand model found in models/ (contents: {os.listdir('models') if os.path.isdir('models') else 'no models dir'}). "
             "Run `uv run fetch-models` from the repo root.")
landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
    base_options=mp_python.BaseOptions(model_asset_path=models[0]),
    running_mode=vision.RunningMode.VIDEO, num_hands=2))


class Out:
    """OSC sender that only repeats itself when something actually changed."""

    def __init__(self, addr, enabled=True):
        self.c = SimpleUDPClient(*addr) if enabled else None
        self.addr = addr
        self.last = {}

    def event(self, address, *vals):
        """Always sent: grabs, drops, one-off announcements."""
        if self.c:
            self.c.send_message(address, list(vals))

    def set(self, address, key, *vals, eps=0.0):
        """Sent only when it differs from the last value under this address+key."""
        if not self.c:
            return
        k = (address, key)
        old = self.last.get(k)
        if old is not None and len(old) == len(vals) and all(
                abs(n - o) <= eps if isinstance(n, float) else n == o
                for n, o in zip(old, vals)):
            return
        self.last[k] = vals
        self.c.send_message(address, ([key] if key is not None else []) + list(vals))

    def resend(self):
        """Forget history, so the next pass transmits the whole state again."""
        self.last.clear()


out = Out(OSC_ADDR, SEND_OSC)

hands = {n: {"x": 0.5, "y": 0.5, "pinch": 0.0, "present": 0, "ratio": 0.0,
             "grabbing": False, "held": None, "trail": deque(maxlen=90),
             "rs": deque(maxlen=15)} for n in NAMES}
T0, last_ts = time.time(), 0


def other(n):
    return "left" if n == "right" else "right"


def physical(label):
    side = label.lower()
    return other(side) if SWAP_LABELS else side


def track(frame_bgr):
    """frame_bgr is already mirrored (selfie view)."""
    global last_ts
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    ts = max(int((time.time() - T0) * 1000), last_ts + 1)
    last_ts = ts
    res = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
    seen = set()
    for lms, hd in zip(res.hand_landmarks, res.handedness):
        name = physical(hd[0].category_name)
        if name in seen:                 # both hands got the same label: give this one the other slot
            name = other(name)
            if name in seen:
                continue
        seen.add(name)
        h = hands[name]
        t, i, w, m = lms[4], lms[8], lms[0], lms[9]
        raw = math.hypot(t.x - i.x, t.y - i.y)
        size = math.hypot(w.x - m.x, w.y - m.y) or 1e-6
        h["ratio"] = raw / size
        h["rs"].append(h["ratio"])
        o, cl = CAL[name]
        c = max(0.0, min(1.0, (o - h["ratio"]) / (o - cl)))
        h["pinch"] = 0.6 * h["pinch"] + 0.4 * c
        h["x"], h["y"] = (t.x + i.x) / 2, (t.y + i.y) / 2   # midpoint of the pinch
        h["present"] = 1
        # x un-mirrored, to match the repo tracker's convention
        out.event(f"/cv/hand/{name}/index_tip", 1 - i.x, i.y, i.z)
        out.event(f"/cv/hand/{name}/thumb_tip", 1 - t.x, t.y, t.z)
        out.event(f"/cv/hand/{name}/wrist", 1 - w.x, w.y, w.z)
        out.event(f"/cv/hand/{name}/pinch", raw)
        out.event(f"/cv/hand/{name}/size", size)
    for n in NAMES:
        if n not in seen:
            hands[n]["present"] = 0
        out.set(f"/cv/hand/{n}/present", None, hands[n]["present"])


# ---- camera (opened first so the pieces can match its aspect ratio) -----
cap = cv2.VideoCapture(CAM_INDEX)
cam_ok = cap.isOpened()
aspect = 16 / 9
if cam_ok:
    ok, f0 = cap.read()
    if ok:
        aspect = f0.shape[1] / f0.shape[0]
CW = round(CH * ROWS * aspect / COLS)      # same shape as the camera, so the picture isn't stretched

pygame.init()
if LOCAL_AUDIO:
    try:
        pygame.mixer.init()
    except pygame.error:
        print("no audio device")
screen = pygame.display.set_mode((W, H))
font = pygame.font.SysFont(None, 26)


# ---- piece shape: every piece is identical (tab right + bottom, blank left + top),
# ---- so any piece mates with any other piece ---------------------------
def make_mask():
    s = pygame.Surface((CW + 2 * TAB, CH + 2 * TAB), pygame.SRCALPHA)
    pygame.draw.rect(s, (255, 255, 255, 255), (TAB, TAB, CW, CH))
    R = int(TAB * 0.6)
    off = TAB - R
    sides = {"top": (TAB + CW // 2, TAB, 0, -1, -1), "bottom": (TAB + CW // 2, TAB + CH, 0, 1, 1),
             "left": (TAB, TAB + CH // 2, -1, 0, -1), "right": (TAB + CW, TAB + CH // 2, 1, 0, 1)}
    for x, y, dx, dy, k in sides.values():          # k=+1 tab, k=-1 blank
        col = (255, 255, 255, 255) if k == 1 else (0, 0, 0, 0)
        pygame.draw.circle(s, col, (x + dx * off * k, y + dy * off * k), R)
    return s


MASK = make_mask()
pts = pygame.mask.from_surface(MASK).outline()
OUTLINE = pygame.Surface(MASK.get_size(), pygame.SRCALPHA)
pygame.draw.lines(OUTLINE, (0, 0, 0, 255), True, pts, 6)          # dark border
pygame.draw.lines(OUTLINE, (255, 255, 255, 255), True, pts, 2)    # light core
padded = pygame.Surface((COLS * CW + 2 * TAB, ROWS * CH + 2 * TAB))
padded.fill((60, 60, 60))


def grab_frame():
    global cam_ok
    if not cap.isOpened():
        return
    ok, f = cap.read()
    if not ok:
        cam_ok = False
        return
    cam_ok = True
    f = cv2.flip(f, 1)                                   # selfie view
    if not USE_MOUSE:
        track(f)
    small = cv2.resize(f, (COLS * CW, ROWS * CH))
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
    padded.blit(pygame.image.frombuffer(rgb.tobytes(), (COLS * CW, ROWS * CH), "RGB"), (TAB, TAB))


def piece_image(c, r, clar=1.0):
    img = padded.subsurface((c * CW, r * CH, CW + 2 * TAB, CH + 2 * TAB)).copy()
    if clar < 0.98:                      # alone = blurry, connected = sharp
        f = 0.10 + 0.90 * clar
        w, h = img.get_size()
        img = pygame.transform.smoothscale(
            pygame.transform.smoothscale(img, (max(2, int(w * f)), max(2, int(h * f)))), (w, h))
    img = img.convert_alpha()
    img.blit(MASK, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    img.blit(OUTLINE, (0, 0))
    return img


# ---- lattice: the only places a piece can rest ---------------------------
GCOLS = (W - 2 * TAB) // CW
GROWS = (H - STATUS_H - 2 * TAB) // CH
GX = (W - GCOLS * CW) // 2
GY = (H - STATUS_H - GROWS * CH) // 2
grid = {}                                   # (gx, gy) -> piece resting there


def cell_origin(cell):
    return GX + cell[0] * CW, GY + cell[1] * CH


def put(p, cell):
    p["cell"] = cell
    grid[cell] = p
    x, y = cell_origin(cell)
    p["rect"].topleft = (x - TAB, y - TAB)


def nearest_free(px, py):
    best, bd = None, float("inf")
    for gx in range(GCOLS):
        for gy in range(GROWS):
            if (gx, gy) in grid:
                continue
            x, y = cell_origin((gx, gy))
            d = (x + CW / 2 - px) ** 2 + (y + CH / 2 - py) ** 2
            if d < bd:
                best, bd = (gx, gy), d
    return best


DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def cluster_sizes():
    """(cell -> size of its connected group, number of groups). Edge-adjacent pieces."""
    seen, size, groups = set(), {}, 0
    for cell in grid:
        if cell in seen:
            continue
        groups += 1
        comp, stack = [], [cell]
        seen.add(cell)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for dx, dy in DIRS:
                nb = (cur[0] + dx, cur[1] + dy)
                if nb in grid and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        for cell_ in comp:
            size[cell_] = len(comp)
    return size, groups


audio_ok = LOCAL_AUDIO and pygame.mixer.get_init() is not None


def load_ladder(i):
    files = sorted(glob.glob(os.path.join(SOUND_DIR, f"piece_{i}", "stage_*.wav")))
    if not files:
        files = glob.glob(os.path.join(SOUND_DIR, f"piece_{i}.wav"))   # old single-file layout
    return [pygame.mixer.Sound(f) for f in files]


ladders = [l for l in (load_ladder(i) for i in range(COLS * ROWS)) if l] if audio_ok else []
if audio_ok:
    pygame.mixer.set_num_channels(COLS * ROWS + 2)
if ladders:
    lengths = [l[0].get_length() for l in ladders]
    print(f"{len(ladders)} sound ladders loaded, loop lengths {min(lengths):.1f}-{max(lengths):.1f} s")
else:
    print("Max drives the sound; nothing played here" if not LOCAL_AUDIO else "no sounds found in sounds/")
T_START = time.time() + 1.0                 # all loops start together; equal lengths then stay locked
pieces = []
for r in range(ROWS):
    for c in range(COLS):
        i = r * COLS + c
        lad = ladders[i % len(ladders)] if ladders else []
        pieces.append({"i": i, "c": c, "r": r, "ladder": lad, "stage": 0, "clar": 0.0,
                       "cell": None, "next": T_START, "amp": 0.0, "live": False,
                       "chan": pygame.mixer.Channel(i) if lad else None,
                       "rect": pygame.Rect(0, 0, CW + 2 * TAB, CH + 2 * TAB)})
NSTAGES = len(ladders[0]) if ladders else STAGES_FALLBACK
if GCOLS * GROWS < len(pieces):
    sys.exit(f"Lattice has {GCOLS * GROWS} cells for {len(pieces)} pieces; lower COLS/ROWS or raise W/H.")
random.seed(7)
start_cells = [(gx, gy) for gx in range(GCOLS) for gy in range(GROWS)]
random.shuffle(start_cells)
for p, cell in zip(pieces, start_cells):
    put(p, cell)

solved_sent = False
print(f"OSC -> {OSC_ADDR[0]}:{OSC_ADDR[1]}" if SEND_OSC else "OSC off")
print(OSC_SCHEMA)
out.event("/puzzle/hello", len(pieces), COLS, ROWS, NSTAGES)


def step(name, cx, cy, pinch):
    """Grab / drag / release for one hand."""
    h = hands[name]
    h["trail"].append((time.time(), cx, cy))
    if len(h["trail"]) > 3:
        t1, x1, y1 = h["trail"][-1]
        t0_, x0, y0 = h["trail"][-4]
        v = math.hypot(x1 - x0, y1 - y0) / max(1e-3, t1 - t0_) / W
        out.set("/puzzle/speed", HAND_ID[name], round(min(1.0, v), 3), eps=0.02)
    if not h["grabbing"] and pinch > GRAB:
        h["grabbing"] = True
        taken = [o["held"] for o in hands.values() if o["held"]]
        cands = [p for p in pieces if all(p is not t for t in taken)
                 and p["rect"].inflate(-2 * TAB + 24, -2 * TAB + 24).collidepoint(cx, cy)]
        p = min(cands, key=lambda p: (p["rect"].centerx - cx) ** 2 + (p["rect"].centery - cy) ** 2, default=None)
        h["held"] = p
        if p:
            p["live"] = True                 # this piece joins the mix
            grid.pop(p["cell"], None)        # pulled out of its spot
            p["cell"] = None
            out.event("/puzzle/grab", p["i"], HAND_ID[name])
    elif h["grabbing"] and pinch < RELEASE:
        h["grabbing"] = False
        p = h["held"]
        if p:
            t0 = time.time() - REWIND            # undo the drift while fingers opened
            px, py = next(((x, y) for t, x, y in reversed(h["trail"]) if t <= t0), (cx, cy))
            put(p, nearest_free(px, py))
            h["held"] = None
            cell = p["cell"]
            n = cluster_sizes()[0].get(cell, 1) if cell else 1
            out.event("/puzzle/drop", p["i"], cell[0] if cell else -1, cell[1] if cell else -1, n)
    elif h["held"]:
        h["held"]["rect"].center = (cx, cy)


# ---- main loop ----------------------------------------------------------
clock = pygame.time.Clock()
next_osc, next_resend = 0.0, time.time() + RESEND_EVERY
while True:
    for e in pygame.event.get():
        if e.type == pygame.QUIT:
            cap.release()
            pygame.quit()
            raise SystemExit
        elif e.type == pygame.KEYDOWN and e.key == pygame.K_r:
            out.resend()                               # a patch just opened: say everything again
            out.event("/puzzle/hello", len(pieces), COLS, ROWS, NSTAGES)
        elif e.type == pygame.KEYDOWN and e.key in (pygame.K_o, pygame.K_c):
            idx = 0 if e.key == pygame.K_o else 1      # O = fingers open, C = fingers pinched
            for n in NAMES:
                if hands[n]["present"] and hands[n]["rs"]:
                    v = statistics.median(hands[n]["rs"])
                    if idx == 0 and v > CAL[n][1] + 0.1 or idx == 1 and v < CAL[n][0] - 0.1:
                        CAL[n][idx] = v
    grab_frame()

    cursors = {}
    for n in NAMES:
        h = hands[n]
        if USE_MOUSE:
            active = n == "right"
            cx, cy = pygame.mouse.get_pos()
            pinch = 1.0 if (active and pygame.mouse.get_pressed()[0]) else 0.0
        else:
            active = bool(h["present"])
            cx, cy = h["x"] * W, h["y"] * H
            pinch = h["pinch"] if active else 0.0
        cursors[n] = (cx, cy, active)
        if active or h["grabbing"]:
            step(n, cx, cy, pinch)

    held_now = [h["held"] for h in hands.values() if h["held"]]
    sizes, ngroups = cluster_sizes()
    for p in pieces:                                  # clarity grows with the size of the connected group
        n = sizes.get(p["cell"], 1) if p["cell"] is not None else 1
        target = (n - 1) / max(1, len(pieces) - 1)    # 0 = alone, 1 = whole puzzle connected
        p["clar"] += (target - p["clar"]) * 0.2
        p["stage"] = round(target * (NSTAGES - 1))
    now = time.time()
    dt = (clock.get_time() / 1000) or 1 / 30
    for p in pieces:                                  # each piece loops at its own clip length;
        if HOLD_ONLY:                                 # stage changes land on that piece's loop boundary
            goal = GAIN if any(p is x for x in held_now) else 0.0
        else:                                         # silent until the first time you pick it up
            goal = GAIN if p["live"] else 0.0
        p["amp"] += max(-dt / FADE, min(dt / FADE, goal - p["amp"]))
        if not p["chan"]:
            continue
        if now >= p["next"]:
            snd = p["ladder"][p["stage"]]
            L = snd.get_length()
            p["next"] = p["next"] + L if p["next"] + L > now else now + L
            p["chan"].play(snd)
        p["chan"].set_volume(p["amp"])                # set every frame, so a fresh loop can't reset it

    if now >= next_resend:                            # periodic full refresh for late-joining patches
        next_resend = now + RESEND_EVERY
        out.resend()
        out.event("/puzzle/hello", len(pieces), COLS, ROWS, NSTAGES)
    if now >= next_osc:                               # continuous values, rate-capped
        next_osc = now + 1 / OSC_HZ
        for p in pieces:
            out.set("/puzzle/stage", p["i"], p["stage"])
            out.set("/puzzle/amp", p["i"], round(p["amp"], 3), eps=AMP_EPS)
            out.set("/puzzle/clarity", p["i"], round(p["clar"], 3), eps=0.01)
            cell = p["cell"] if p["cell"] is not None else (-1, -1)
            out.set("/puzzle/cell", p["i"], cell[0], cell[1])
        largest = max(sizes.values(), default=0)
        out.set("/puzzle/cluster", None, largest, ngroups)
        out.set("/puzzle/progress", None, round((largest - 1) / max(1, len(pieces) - 1), 3), eps=0.005)
        if largest == len(pieces) and not solved_sent:
            solved_sent = True
            out.event("/puzzle/solved", 1)

    screen.fill((25, 25, 25))
    for gx in range(GCOLS):                          # faint lattice
        for gy in range(GROWS):
            x, y = cell_origin((gx, gy))
            pygame.draw.rect(screen, (48, 48, 48), (x, y, CW, CH), 1)
    for p in held_now:                               # where the dragged piece would land
        tc = nearest_free(*p["rect"].center)
        if tc:
            x, y = cell_origin(tc)
            pygame.draw.rect(screen, (80, 190, 110), (x, y, CW, CH), 3)
    for p in sorted(pieces, key=lambda p: any(p is x for x in held_now)):
        screen.blit(piece_image(p["c"], p["r"], p["clar"]), p["rect"].topleft)
    for n in NAMES:
        cx, cy, active = cursors[n]
        if active:
            col = (90, 220, 120) if hands[n]["grabbing"] else COLORS[n]
            pygame.draw.circle(screen, col, (int(cx), int(cy)), 12, 3)
    info = "   ".join(f"{n[0].upper()}: ratio {hands[n]['ratio']:.2f} pinch {hands[n]['pinch']:.2f} "
                      f"{'on' if hands[n]['present'] else 'off'}" for n in NAMES)
    info += (f"   cluster {max(sizes.values(), default=0)}/{len(pieces)}"
             f"   sounding {sum(1 for p in pieces if p['amp'] > 0.01)}/{len(pieces)}"
             f"   audio {'local' if audio_ok else 'Max'}")
    info += f"   OSC {OSC_ADDR[0]}:{OSC_ADDR[1]}" if SEND_OSC else "   OSC off"
    info += f"   camera {'ok' if cam_ok else 'NOT AVAILABLE'}"
    screen.blit(font.render(info, True, (255, 255, 255)), (10, H - 30))
    cal = "   ".join(f"{n[0].upper()}: open {CAL[n][0]:.2f} pinched {CAL[n][1]:.2f}" for n in NAMES)
    screen.blit(font.render(f"Hands open + O, pinched + C.   R = resend OSC.   {cal}", True, (180, 180, 180)), (10, H - 56))
    pygame.display.flip()
    clock.tick(30)