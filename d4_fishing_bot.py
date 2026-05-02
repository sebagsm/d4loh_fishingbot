#!/usr/bin/env python3
"""
diablo4 loh fishing bot

auto-casts, detects bite prompt via multi-template matching, reels in.
on timeout (mob from water), automatically attacks with right mosue click x4.


requirements:
    pip install mss opencv-python numpy pynput pyautogui

usage:
 change by your binds

CAST_KEY  = "`"
REEL_KEY  = "1"

    1. stand your character at the fishing spot
    2. fish manually a few times and capture the bite indicator at different
       animation frames save them as:
           bite_template_0.png
           bite_template_1.png
           bite_template_2.png
    3. run this script
    4. press F5 to start / pause the bot
    5. press F6 to quit

tested location screenshots: map1.png and map2.png

greetings to KUNSH
"""

import time
import random
import csv
import threading
from datetime import datetime
from pathlib import Path
from enum import Enum, auto

import mss
import cv2
import numpy as np
import pyautogui
from pynput import keyboard


# ─── Config ───────────────────────────────────────────────────────────────────

# Key bindings
CAST_KEY  = "`"
REEL_KEY  = "1"

# Screen region to watch for the bite prompt (x, y, width, height).
# A tight ROI is faster and reduces false positives.
# Set to None to scan the full primary monitor.
# Example: {"left": 750, "top": 350, "width": 400, "height": 250}
WATCH_REGION = None

# Template images — screenshot the bite indicator at several animation frames
# and save them next to this script. Any match triggers the reel.
TEMPLATE_PATHS = [
    Path("bite_template_0.png"),
    Path("bite_template_1.png"),
    Path("bite_template_2.png"),
    Path("bite_template_3.png"),
]

# Detection sensitivity — lower = more sensitive (more false positive risk)
TEMPLATE_THRESH = 0.68

# Color fallback HSV range (used when no templates are found)
# Targets the orange-gold bite glow in VoH
COLOR_HSV_LOWER = np.array([15, 180, 180])
COLOR_HSV_UPPER = np.array([35, 255, 255])
COLOR_THRESH    = 30

# ── Timing ────────────────────────────────────────────────────────────────────

CAST_DELAY_MIN  = 0.8    # random pre-cast pause (simulates human)
CAST_DELAY_MAX  = 1.4

REACT_DELAY_MIN = 0.05   # random delay between detecting bite and pressing key
REACT_DELAY_MAX = 0.25

TIMEOUT_CAST    = 20.0   # give up on a cast after this many seconds

# After reeling, ignore all detections for this long.
# Prevents double-firing on consecutive animation frames of the same bite.
REEL_LOCKOUT    = 1.0

# ── Combat (mob from water) ───────────────────────────────────────────────────

# How many right-clicks to fire when a mob appears
COMBAT_CLICKS       = 4

# Delay between each right-click attack
COMBAT_CLICK_DELAY_MIN = 0.3
COMBAT_CLICK_DELAY_MAX = 0.6

# How long to wait after the last attack before recasting
# (gives the mob time to die and loot to appear)
COMBAT_SETTLE_TIME  = 2.5

# ── Anti-AFK ──────────────────────────────────────────────────────────────────

ANTI_AFK_INTERVAL_MIN = 180   # min seconds between anti-afk actions
ANTI_AFK_INTERVAL_MAX = 300   # max seconds between anti-afk actions
ANTI_AFK_WIGGLE_PX    = 8     # max mouse pixel offset per wiggle

# ── Debug ─────────────────────────────────────────────────────────────────────

# Set to True to save a screenshot every time a bite is detected.
# Use the saved frames to build your bite_template_N.png set.
DEBUG_SAVE_FRAMES = False
DEBUG_FRAMES_DIR  = Path("debug_frames")

# Stats output
STATS_FILE = Path(f"fishing_session_{datetime.now():%Y%m%d_%H%M%S}.csv")


# ─── State machine ────────────────────────────────────────────────────────────

class State(Enum):
    IDLE    = auto()   # about to cast
    WAITING = auto()   # line is in the water, watching for bite
    REELING = auto()   # placeholder for future reel animation wait
    COMBAT  = auto()   # mob appeared, attacking


# ─── Anti-AFK ─────────────────────────────────────────────────────────────────

class AntiAFK:
    """
    Runs in a background thread.
    Every 3-5 minutes performs a subtle random mouse wiggle to prevent
    the game's AFK logout timer from triggering.
    Starts and stops automatically with the bot.
    """

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[anti-afk] Started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        print("[anti-afk] Stopped")

    def _loop(self):
        while not self._stop_event.is_set():
            interval = random.uniform(ANTI_AFK_INTERVAL_MIN, ANTI_AFK_INTERVAL_MAX)
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                if self._stop_event.is_set():
                    return
                time.sleep(1)
            if not self._stop_event.is_set():
                self._wiggle()

    def _wiggle(self):
        ox  = random.randint(-ANTI_AFK_WIGGLE_PX, ANTI_AFK_WIGGLE_PX)
        oy  = random.randint(-ANTI_AFK_WIGGLE_PX, ANTI_AFK_WIGGLE_PX)
        dur = random.uniform(0.1, 0.3)
        pyautogui.moveRel(ox, oy, duration=dur)
        time.sleep(random.uniform(0.1, 0.2))
        pyautogui.moveRel(-ox, -oy, duration=dur)
        print(f"[anti-afk] Wiggle ({ox:+d}, {oy:+d})")


# ─── Fishing Bot ──────────────────────────────────────────────────────────────

class FishingBot:

    def __init__(self):
        self.state          = State.IDLE
        self.running        = False
        self.stats          = {
            "casts":          0,
            "catches":        0,
            "timeouts":       0,
            "mobs_killed":    0,
            "double_blocked": 0,
        }
        self.templates      = self._load_templates()
        self._sct           = mss.mss()
        self._cast_at       = 0.0
        self._last_reel_at  = 0.0
        self._session_log: list[dict] = []
        self._anti_afk      = AntiAFK()

        pyautogui.FAILSAFE = True   # move mouse to corner to emergency-stop

        if DEBUG_SAVE_FRAMES:
            DEBUG_FRAMES_DIR.mkdir(exist_ok=True)
            print(f"[debug] Frame saving ON -> {DEBUG_FRAMES_DIR}/")

    # ── Template loading ─────────────────────────────────────────────────────

    def _load_templates(self) -> list[np.ndarray]:
        templates = []
        for p in TEMPLATE_PATHS:
            if p.exists():
                tmpl = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if tmpl is not None:
                    templates.append(tmpl)
                    print(f"[init] Template loaded: {p}  {tmpl.shape}")
        if templates:
            print(f"[init] {len(templates)} template(s) ready")
        else:
            print("[init] No templates found — using color fallback")
            print(f"[init] Tip: save bite screenshots as {TEMPLATE_PATHS[0]}, etc.")
        return templates

    # ── Screen capture ───────────────────────────────────────────────────────

    def _grab_frame(self) -> np.ndarray:
        region = WATCH_REGION or self._sct.monitors[1]
        shot   = self._sct.grab(region)
        frame  = np.array(shot)
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    # ── Bite detection ───────────────────────────────────────────────────────

    def _detect_bite(self, frame: np.ndarray) -> bool:
        # Lockout window — ignore frames right after reeling to prevent
        # double-firing on consecutive animation frames of the same bite
        if (time.monotonic() - self._last_reel_at) < REEL_LOCKOUT:
            self.stats["double_blocked"] += 1
            return False

        if self.templates:
            return self._template_detect(frame)
        return self._color_detect(frame)

    def _template_detect(self, frame: np.ndarray) -> bool:
        """Check all loaded templates — any match above threshold counts."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        for tmpl in self.templates:
            result = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val >= TEMPLATE_THRESH:
                return True
        return False

    def _color_detect(self, frame: np.ndarray) -> bool:
        """
        Fallback: detect the orange-gold bite glow by HSV color range.
        Tune COLOR_HSV_LOWER / UPPER in Config if needed.
        """
        hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, COLOR_HSV_LOWER, COLOR_HSV_UPPER)
        return int(mask.sum() / 255) >= COLOR_THRESH

    def _maybe_save_debug_frame(self, frame: np.ndarray):
        if not DEBUG_SAVE_FRAMES:
            return
        name = DEBUG_FRAMES_DIR / f"bite_{datetime.now():%H%M%S_%f}.png"
        cv2.imwrite(str(name), frame)
        print(f"[debug] Saved frame -> {name}")

    # ── Combat ───────────────────────────────────────────────────────────────

    def _kill_mob(self):
        """
        A timeout while waiting for a bite means a mob climbed out of the water.
        Fire right-click COMBAT_CLICKS times to kill it, then wait for it to die.
        """
        print(f"  [combat] Mob detected — attacking with {COMBAT_CLICKS}x right-click...")
        for i in range(COMBAT_CLICKS):
            pyautogui.click(button="right")
            delay = random.uniform(COMBAT_CLICK_DELAY_MIN, COMBAT_CLICK_DELAY_MAX)
            time.sleep(delay)
            print(f"  [combat] Click {i + 1}/{COMBAT_CLICKS}")

        print(f"  [combat] Waiting {COMBAT_SETTLE_TIME}s for mob to die...")
        time.sleep(COMBAT_SETTLE_TIME)
        self.stats["mobs_killed"] += 1
        self._log("mob_killed", 0)
        print(f"  [combat] Done — resuming fishing (mobs killed: {self.stats['mobs_killed']})")

    # ── Input helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _press(key: str):
        pyautogui.press(key)

    @staticmethod
    def _jitter(lo: float, hi: float):
        time.sleep(random.uniform(lo, hi))

    # ── Main loop ────────────────────────────────────────────────────────────

    def tick(self):
        now = time.monotonic()

        if self.state == State.IDLE:
            self._jitter(CAST_DELAY_MIN, CAST_DELAY_MAX)
            self._press(CAST_KEY)
            self.stats["casts"] += 1
            self._cast_at = time.monotonic()
            self.state    = State.WAITING
            print(f"  [cast #{self.stats['casts']}] Waiting for bite...")

        elif self.state == State.WAITING:
            frame = self._grab_frame()

            if self._detect_bite(frame):
                self._maybe_save_debug_frame(frame)
                self._jitter(REACT_DELAY_MIN, REACT_DELAY_MAX)
                react_ms = int((time.monotonic() - now) * 1000 + REACT_DELAY_MIN * 1000)
                self._press(REEL_KEY)
                self._last_reel_at = time.monotonic()
                self.stats["catches"] += 1
                self.state = State.IDLE
                self._log("catch", react_ms)
                catch_rate = self.stats["catches"] / self.stats["casts"] * 100
                print(f"  [catch] Reeled in! React: {react_ms}ms | "
                      f"Catches: {self.stats['catches']}/{self.stats['casts']} "
                      f"({catch_rate:.0f}%)")

            elif (time.monotonic() - self._cast_at) > TIMEOUT_CAST:
                self.stats["timeouts"] += 1
                self._log("timeout", 0)
                # Transition to combat — mob likely climbed out
                self.state = State.COMBAT

        elif self.state == State.COMBAT:
            self._kill_mob()
            self.state = State.IDLE   # recast after combat

        elif self.state == State.REELING:
            self.state = State.IDLE

    # ── Session logging ──────────────────────────────────────────────────────

    def _log(self, event: str, react_ms: int):
        self._session_log.append({
            "timestamp":   datetime.now().isoformat(),
            "event":       event,
            "casts":       self.stats["casts"],
            "catches":     self.stats["catches"],
            "mobs_killed": self.stats["mobs_killed"],
            "react_ms":    react_ms,
        })

    def _save_stats(self):
        if not self._session_log:
            return
        with open(STATS_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self._session_log[0].keys())
            writer.writeheader()
            writer.writerows(self._session_log)
        total   = self.stats["casts"]
        catches = self.stats["catches"]
        rate    = (catches / total * 100) if total else 0
        print(f"[bot] Session saved -> {STATS_FILE}")
        print(f"[bot] Final: {catches}/{total} catches ({rate:.1f}%) | "
              f"Timeouts: {self.stats['timeouts']} | "
              f"Mobs killed: {self.stats['mobs_killed']} | "
              f"Double-blocked: {self.stats['double_blocked']}")

    # ── Start / stop ─────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return
        self.running = True
        self.state   = State.IDLE
        self._anti_afk.start()
        print("[bot] Running — F5 to pause, F6 to quit")
        while self.running:
            self.tick()

    def stop(self):
        self.running = False
        self.state   = State.IDLE
        self._anti_afk.stop()
        self._save_stats()
        print("[bot] Stopped")


# ─── Hotkey controller ────────────────────────────────────────────────────────

def main():
    bot        = FishingBot()
    bot_thread: threading.Thread | None = None

    def on_press(key):
        nonlocal bot_thread
        try:
            if key == keyboard.Key.f5:
                if bot.running:
                    print("[hotkey] F5 — pausing")
                    bot.stop()
                else:
                    print("[hotkey] F5 — starting")
                    bot_thread = threading.Thread(target=bot.start, daemon=True)
                    bot_thread.start()

            elif key == keyboard.Key.f6:
                print("[hotkey] F6 — quitting")
                bot.stop()
                return False

        except Exception as exc:
            print(f"[hotkey error] {exc}")

    print("=" * 52)
    print("  Diablo 4 LoH — Fishing Bot")
    print("  F5 = start / pause    F6 = quit")
    print("  Move mouse to screen corner = emergency stop")
    print("=" * 52)
    tmpl_count = sum(1 for p in TEMPLATE_PATHS if p.exists())
    print(f"  Templates   : {tmpl_count} / {len(TEMPLATE_PATHS)} found", end="")
    print("  OK" if tmpl_count > 0 else "  MISSING (color fallback active)")
    print(f"  Threshold   : {TEMPLATE_THRESH}")
    print(f"  Reel lockout: {REEL_LOCKOUT}s  (double-fire prevention)")
    print(f"  Combat      : {COMBAT_CLICKS}x right-click, {COMBAT_SETTLE_TIME}s settle")
    print(f"  Watch region: {WATCH_REGION or 'full screen'}")
    print(f"  Anti-AFK    : every {ANTI_AFK_INTERVAL_MIN}–{ANTI_AFK_INTERVAL_MAX}s")
    print(f"  Debug frames: {'ON -> ' + str(DEBUG_FRAMES_DIR) + '/' if DEBUG_SAVE_FRAMES else 'off'}")
    print()

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


if __name__ == "__main__":
    main()
