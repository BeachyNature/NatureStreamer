import argparse
import contextlib
import queue
import socket
import struct
import threading
import time

import cv2
import mss
import numpy as np
from pynput.keyboard import Controller as KeyControl, Key
from pynput.mouse import Button, Controller as MouseControl

import yaml
import logging
import logging.config

with open("logger.yaml") as f:
    logging.config.dictConfig(yaml.safe_load(f))
logger = logging.getLogger("server")

from common_utils.wrapper import pprint

# ── Input devices ──────────────────────────────────────────────────────────────

_keyboard = KeyControl()
_mouse    = MouseControl()

SPECIAL_KEYS = {
    "return": Key.enter, "backspace": Key.backspace, "tab":    Key.tab,
    "escape": Key.esc,   "space":     Key.space,     "delete": Key.delete,
    "left":   Key.left,  "right":     Key.right,     "up":     Key.up,
    "down":   Key.down,  "home":      Key.home,       "end":    Key.end,
    "page up": Key.page_up, "page down": Key.page_down,
    "shift": Key.shift, "ctrl": Key.ctrl, "alt": Key.alt,
    **{f"f{n}": getattr(Key, f"f{n}") for n in range(1, 6)},
}
MOUSE_BUTTONS = {"left": Button.left, "right": Button.right}

# ── Video sources ──────────────────────────────────────────────────────────────

class ScreenSource:
    def __init__(self):
        self._sct     = mss.mss()
        self._monitors = self._sct.monitors
        self.monitor  = self._monitors[0]

    @property
    def size(self) -> tuple[int, int]:
        return self.monitor["width"], self.monitor["height"]

    @property
    def num_displays(self) -> int:
        return len(self._monitors) - 1

    def switch(self, index: int) -> tuple[int, int] | None:
        """Switch to display index (1-based). Returns new size or None on bad index."""

        self.monitor = self._monitors[index]
        pprint(f"Switched to display {index}")
        return self.size

    def read_frame(self) -> np.ndarray | None:
        """ Read the supporting frames to come from source """

        img = np.asarray(self._sct.grab(self.monitor))[:, :, :3]
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def __enter__(self): return self
    def __exit__(self, *_): self._sct.close()


class CameraSource:
    def __init__(self, index: int = 0):
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera {index}")
        pprint(f"Camera {index} opened")

    @property
    def size(self) -> tuple[int, int]:
        return (int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

    @property
    def num_displays(self) -> int:
        return 1

    def read_frame(self) -> np.ndarray | None:
        ok, frame = self._cap.read()
        return frame if ok else None

    def __enter__(self): return self
    def __exit__(self, *_): self._cap.release()


def probe_camera(index: int = 0) -> bool:
    """Return True if a camera is available at index without holding it open."""

    cap = cv2.VideoCapture(index)
    ok  = cap.isOpened()
    cap.release()
    return ok

# ── Shared stream state ────────────────────────────────────────────────────────

class StreamState:
    """Mutable source pointer shared between the video loop and control handler."""

    def __init__(self, initial: ScreenSource | CameraSource, cam_index: int | None) -> None:
        self._lock      = threading.Lock()
        self._source    = initial
        self._screen    = initial
        self._cam_index = cam_index 

    @property
    def source(self):
        with self._lock:
            return self._source

    def switch_display(self, index: int, ctrl_sock: socket.socket) -> None:
        """ Switch between all of the given displays """

        with self._lock:
            if not isinstance(self._source, ScreenSource):
                self._source = self._screen
            size = self._source.switch(index)

        if size:
            ctrl_sock.sendall(f"res:{size[0]},{size[1]}\n".encode())
            pprint(f"View → display {index} ({size[0]}x{size[1]})")

    def switch_camera(self, ctrl_sock: socket.socket) -> None:
        """ Switch which camera is being viewed """

        if self._cam_index is None:
            pprint("No camera available — ignoring view:cam")
            return

        with self._lock:
            if isinstance(self._source, CameraSource):
                return

            # Launch the new camera source
            cam = CameraSource(self._cam_index)
            self._source = cam

        w, h = cam.size
        ctrl_sock.sendall(f"res:{w},{h}\n".encode())
        pprint(f"View → camera ({w}x{h})")

# ── Event handlers ─────────────────────────────────────────────────────────────

def handle_key(msg: str) -> None:
    """ Handle the actions that are sent from the keyboard """

    for k in msg.split("key:")[1:]:
        key_bind = key.lower().strip()
        key = SPECIAL_KEYS.get( key_bind,  key_bind)

        try:
            _keyboard.tap(key)
        except Exception as e:
            pprint(f"Key error: {e}")


def handle_mouse(msg: str, state: StreamState) -> None:
    """ Handle the actions that are sent from the mouse """

    parts = msg.split(":")
    if len(parts) < 3:
        return

    _, atype, btype, *rest = parts
    button = MOUSE_BUTTONS.get(btype)
    if not button:
        return

    # Only take aciton when a screen is avaliable
    src = state.source
    if isinstance(src, ScreenSource):
        monitor = src.monitor
        if atype == "leave":
            _mouse.release(button)
            return

        if not rest:
            return

        x, y = map(int, rest[0].strip().split(","))
        _mouse.position = (monitor["left"] + x, monitor["top"] + y)
        (_mouse.press if atype == "click" else _mouse.release)(button)

# ── Core loops ─────────────────────────────────────────────────────────────────

def control_loop(ctrl_sock: socket.socket, state: StreamState) -> None:
    """"""
    dispatch = {
        "key:":   lambda msg: handle_key(msg),
        "mouse:": lambda msg: handle_mouse(msg, state),
        "view:":  lambda msg: state.switch_camera(ctrl_sock)
                              if msg == "view:cam" else
                              state.switch_display(int(msg[5:]), ctrl_sock),
    }

    buf = ""
    while True:
        try:
            ctrl_sock.settimeout(1)
            chunk = ctrl_sock.recv(1024).decode()
            if not chunk:
                break
            buf += chunk

            # Read the incoming message
            while "\n" in buf:
                msg, buf = buf.split("\n", 1)
                msg = msg.strip()
                if not msg:
                    continue

                # Get the defined function
                handler = next((
                    fn for prefix, fn in dispatch.items()
                    if msg.startswith(prefix)), None)
                
                # Process the request ------
                if handler:
                    handler(msg)
        except socket.timeout:
            continue
        except (BrokenPipeError, ConnectionResetError, OSError):
            break


def video_loop(video_sock: socket.socket, ctrl_sock: socket.socket,
               state: StreamState, has_camera: bool) -> None:
    threading.Thread(target=control_loop, args=(ctrl_sock, state), daemon=True).start()

    w, h = state.source.size
    num_displays = state.source.num_displays if isinstance(state.source, ScreenSource) \
                   else ScreenSource().num_displays

    # Handshake: width, height, num_displays, has_camera
    video_sock.sendall(struct.pack(">IIII", w, h, num_displays, int(has_camera)))
    pprint(f"Handshake: {w}x{h}, {num_displays} display(s), camera={'yes' if has_camera else 'no'}")

    while True:
        frame = state.source.read_frame()
        if frame is None:
            time.sleep(0.05)
            continue
        ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not ok:
            continue
        try:
            video_sock.sendall(struct.pack(">I", len(data)) + data.tobytes())
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

# ── Server bootstrap ───────────────────────────────────────────────────────────

def make_server(port: int, name: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen()
    pprint(f"{name} listening on :{port}")
    return s


def run(cam_index: int | None) -> None:
    has_camera = cam_index is not None and probe_camera(cam_index)

    while True:
        video_srv = make_server(3000, "Video")
        ctrl_srv  = make_server(3001, "Control")
        try:
            video_sock, addr = video_srv.accept()
            ctrl_sock,  _    = ctrl_srv.accept()
            pprint(f"Client connected: {addr}")
        finally:
            video_srv.close()
            ctrl_srv.close()

        # Always start on display 1 regardless of --cam flag
        screen = ScreenSource()
        state  = StreamState(screen, cam_index if has_camera else None)
        with contextlib.suppress(Exception):
            video_loop(video_sock, ctrl_sock, state, has_camera)

        pprint("Client disconnected — restarting…")
        with contextlib.suppress(Exception):
            video_sock.close()
            ctrl_sock.close()
        time.sleep(1)

# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Screen/camera streaming server")
    parser.add_argument(
        "--cam", nargs="?", const=0, default=None, type=int,
        help="Expose camera for streaming (default device 0)",
    )
    args = parser.parse_args()

    pprint("Listening for new connection...")
    thread = threading.Thread(target=run, args=(args.cam,), daemon=True)
    thread.start()
    thread.join()

if __name__ == "__main__":
    main()
