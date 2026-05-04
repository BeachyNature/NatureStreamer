import os
import sys
import cv2
import time
import struct
import socket
import threading
import numpy as np

from PyQt6.QtCore import Qt, QEvent, QObject, QPointF, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPixmap, QImage, QAction
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QHBoxLayout, QVBoxLayout,
    QPushButton, QComboBox, QLineEdit, QSizePolicy,
)

import yaml
import logging
import logging.config

with open("logger.yaml") as f:
    logging.config.dictConfig(yaml.safe_load(f))
logger = logging.getLogger("streamer")

from __version__ import version
from common_utils.wrapper import pprint, read_yaml

# ── Constants ──────────────────────────────────────────────────────────────────

send_lock = threading.Lock()

KEY_MAP = {
    Qt.Key.Key_Space:     "space",
    Qt.Key.Key_Return:    "return",
    Qt.Key.Key_Enter:     "enter",
    Qt.Key.Key_Escape:    "escape",
    Qt.Key.Key_Tab:       "tab",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Left:      "left",
    Qt.Key.Key_Right:     "right",
    Qt.Key.Key_Up:        "up",
    Qt.Key.Key_Down:      "down",
}

MOUSE_ACTIONS = {
    QEvent.Type.MouseButtonPress:   "click",
    QEvent.Type.MouseButtonRelease: "release",
    QEvent.Type.Leave:              "leave",
}

MOUSE_BUTTONS = {
    Qt.MouseButton.LeftButton:  "left",
    Qt.MouseButton.RightButton: "right",
}

MAX_FRAME_BYTES = 10 * 1024 * 1024
CAM_LABEL       = "📷 Camera"

# ── Networking ─────────────────────────────────────────────────────────────────

def connect(host: str, port: int, retries: int = 5, delay: float = 0.1) -> socket.socket:
    for attempt in range(retries):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((host, port))
            pprint(f"Connected to {host}:{port}")
            return s
        except (ConnectionRefusedError, OSError) as e:
            pprint(f"Attempt {attempt + 1}/{retries} failed: {e}")
            s.close()
            time.sleep(delay)
    raise ConnectionRefusedError(f"Could not connect to {host}:{port} after {retries} attempts")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed mid-read")
        buf += chunk
    return buf

# ── Mouse tracker ──────────────────────────────────────────────────────────────

class MouseTracker(QObject):
    actionCalled = pyqtSignal(str, str, QPointF)

    def __init__(self, widget: QLabel) -> None:
        super().__init__(widget)
        self._last_button: str | None = None
        self._widget = widget
        widget.setMouseTracking(True)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if not self._widget.running:
            return super().eventFilter(obj, event)

        atype = MOUSE_ACTIONS.get(event.type())
        if not atype:
            return super().eventFilter(obj, event)

        if atype == "leave":
            if self._last_button:
                self.actionCalled.emit("leave", self._last_button, QPointF())
            return super().eventFilter(obj, event)

        btype = MOUSE_BUTTONS.get(event.button())
        if not btype:
            return super().eventFilter(obj, event)

        self._last_button = btype
        self.actionCalled.emit(atype, btype, event.position())
        return super().eventFilter(obj, event)

# ── Video label ────────────────────────────────────────────────────────────────

class VideoLabel(QLabel):
    """Displays the stream and maps input events back to capture coordinates."""

    def __init__(self, placeholder: str = "", parent=None) -> None:
        super().__init__(placeholder, parent)
        self.capture_w: int = 1
        self.capture_h: int = 1
        self.running:   bool = False
        self.control_conn: socket.socket | None = None

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setScaledContents(False)

    def set_frame(self, pixmap: QPixmap) -> None:
        self.setPixmap(pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def key_press(self, key: str) -> None:
        self._send(f"key:{key}\n")

    @pyqtSlot(str, str, QPointF)
    def send_action(self, atype: str, btype: str, pos: QPointF) -> None:
        if atype == "leave":
            self._send(f"mouse:leave:{btype}\n")
            return

        lbl_w, lbl_h = self.width(), self.height()
        src_ar = self.capture_w / self.capture_h

        if lbl_w / lbl_h > src_ar:
            px_h, px_w = lbl_h, int(lbl_h * src_ar)
        else:
            px_w, px_h = lbl_w, int(lbl_w / src_ar)

        ox, oy = (lbl_w - px_w) // 2, (lbl_h - px_h) // 2
        cx, cy = pos.x(), pos.y()

        if not (ox <= cx < ox + px_w and oy <= cy < oy + px_h):
            return

        sx = int((cx - ox) / px_w * self.capture_w)
        sy = int((cy - oy) / px_h * self.capture_h)
        self._send(f"mouse:{atype}:{btype}:{sx},{sy}\n")

    def _send(self, msg: str) -> None:
        if not self.control_conn:
            return
        try:
            with send_lock:
                self.control_conn.sendall(msg.encode())
        except (BrokenPipeError, ConnectionResetError):
            pprint("Send failed — connection lost")

# ── Main window ────────────────────────────────────────────────────────────────

class StreamerApp(QMainWindow):

    def __init__(self, socket_dict: dict = {}) -> None:
        super().__init__()
        self.socket_dict  = socket_dict
        self.video_conn:   socket.socket | None = None
        self.control_conn: socket.socket | None = None

        # Maps view_combo index → command string sent to server ("1", "2", "cam", …)
        self._view_cmds: list[str] = []
        self._init_ui()

    # ── UI construction ────────────────────────────────────────────────────────

    def _init_ui(self) -> None:
        self.setWindowTitle(f"Nature Station v{version}")
        self.resize(1920, 1080)
        self._build_menu()

        self.ip_combo   = QComboBox(); self.ip_combo.addItems(self.socket_dict); self.ip_combo.setFixedWidth(250)
        self.port_input = QLineEdit("3000"); self.port_input.setFixedWidth(100)

        self.view_combo = QComboBox()
        self.view_combo.setFixedWidth(250)
        self.view_combo.setEnabled(False)
        self.view_combo.activated.connect(self._on_view_changed)

        self.conn_btn = QPushButton("Connect")
        self.conn_btn.setAutoDefault(False)
        self.conn_btn.clicked.connect(self._on_connect)

        self.info_lbl   = QLabel("Not connected.")
        self.stream_lbl = VideoLabel("Click Connect to View Stream")
        self.stream_lbl.setMinimumSize(640, 400)
        self.stream_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        tracker = MouseTracker(self.stream_lbl)
        tracker.actionCalled.connect(self.stream_lbl.send_action)

        toolbar = QHBoxLayout()
        for w in (self.ip_combo, self.port_input, self.view_combo, self.info_lbl):
            toolbar.addWidget(w)
        toolbar.addStretch()

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(toolbar)
        root.addWidget(self.stream_lbl, stretch=1)
        root.addWidget(self.conn_btn)

        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

    def _build_menu(self) -> None:
        settings_act = QAction("Settings",  self); settings_act.triggered.connect(self._on_settings)
        keybinds_act = QAction("Key Binds", self); keybinds_act.triggered.connect(self._on_keybinds)
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(settings_act)
        file_menu.addAction(keybinds_act)
        self.stream_menu = self.menuBar().addMenu("Streams")

    # ── Menu stubs ─────────────────────────────────────────────────────────────

    def _on_settings(self) -> None: pprint("TODO: settings window")
    def _on_keybinds(self) -> None: pprint("TODO: keybinds window")

    # ── View combo ─────────────────────────────────────────────────────────────

    def _populate_views(self, num_displays: int, has_camera: bool) -> None:
        """Fill the view combo and build the parallel command list."""
        self._view_cmds = []
        self.view_combo.clear()

        for i in range(num_displays + 1):
            label = f"Display {i}"
            self.view_combo.addItem(label)
            self._view_cmds.append(str(i))

        if has_camera:
            self.view_combo.addItem(CAM_LABEL)
            self._view_cmds.append("cam")
        self.view_combo.setEnabled(True)

    def _on_view_changed(self, combo_index: int) -> None:
        if combo_index < 0 or combo_index >= len(self._view_cmds):
            return
        cmd = self._view_cmds[combo_index]
        with send_lock:
            self.control_conn.sendall(f"view:{cmd}\n".encode())
        pprint(f"Sent view:{cmd}")

    # ── Connection lifecycle ───────────────────────────────────────────────────

    def _on_connect(self) -> None:
        if self.stream_lbl.running:
            self._disconnect()
            return

        port_text = self.port_input.text().strip()
        if not port_text.isdigit():
            self.info_lbl.setText("Invalid port.")
            return

        host = self.ip_combo.currentText()
        ip   = self.socket_dict.get(host)
        if not ip:
            pprint(f"No IP for {host}")
            return

        self.info_lbl.setText("Connecting…")
        try:
            port = int(port_text)
            self.video_conn   = connect(ip, port)
            self.control_conn = connect(ip, port + 1)
        except ConnectionRefusedError:
            self.info_lbl.setText(f"Could not connect to {host} ({ip})")
            return

        self.info_lbl.setText(f"Connected to {host} ({ip})")
        self.conn_btn.setText("Disconnect")
        self.stream_lbl.running = True

        self.view_combo.setCurrentIndex(0)
        self.stream_menu.addAction(QAction(host, self))
        threading.Thread(target=self._control_loop, daemon=True).start()
        threading.Thread(target=self._video_loop,   daemon=True).start()

    def _disconnect(self) -> None:
        if not self.stream_lbl.running:
            return
        self.stream_lbl.running = False
        self.view_combo.clear()
        self.view_combo.setEnabled(False)
        self._view_cmds = []
        for conn in (self.video_conn, self.control_conn):
            try: conn.close()
            except Exception: pass
        self.conn_btn.setText("Connect")
        self.info_lbl.setText("Disconnected.")
        self.stream_lbl.setText("Click Connect to View Stream")

    # ── Background threads ─────────────────────────────────────────────────────

    def _control_loop(self) -> None:
        buf = ""
        while self.stream_lbl.running:
            try:
                chunk = self.control_conn.recv(1024).decode()
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    msg, buf = buf.split("\n", 1)
                    self._handle_control(msg.strip())
            except (ConnectionError, OSError):
                pprint("Control connection lost")
                break

    def _handle_control(self, msg: str) -> None:
        if msg.startswith("res:"):
            w, h = map(int, msg[4:].split(","))
            self.stream_lbl.capture_w, self.stream_lbl.capture_h = w, h
            pprint(f"Resolution → {w}x{h}")

    def _video_loop(self) -> None:
        try:
            # Extended handshake: width, height, num_displays, has_camera
            w, h, num_displays, has_camera = struct.unpack(">IIII", recv_exact(self.video_conn, 16))

            self.stream_lbl.capture_w    = w
            self.stream_lbl.capture_h    = h
            self.stream_lbl.control_conn = self.control_conn
            self._populate_views(num_displays, bool(has_camera))
            pprint(f"Handshake: {w}x{h}, {num_displays} display(s), camera={'yes' if has_camera else 'no'}")

            while self.stream_lbl.running:
                size = struct.unpack(">I", recv_exact(self.video_conn, 4))[0]
                if size == 0:
                    continue
                if size > MAX_FRAME_BYTES:
                    pprint(f"Frame too large ({size}B) — closing")
                    break

                frame = cv2.imdecode(
                    np.frombuffer(recv_exact(self.video_conn, size), np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if frame is None:
                    continue

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                pixmap = QPixmap.fromImage(
                    QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
                )
                self.stream_lbl.set_frame(pixmap)

        except (ConnectionError, struct.error) as e:
            self.info_lbl.setText(f"Stream error: {e}")

    # ── Qt overrides ───────────────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        if event.isAutoRepeat():
            return
        key = KEY_MAP.get(event.key()) or event.text()
        if key and self.stream_lbl.control_conn:
            self.stream_lbl.key_press(key)
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._disconnect()
        super().closeEvent(event)

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    addr_path   = os.path.join(os.getcwd(), "config", "address.yaml")
    socket_dict = read_yaml(addr_path)
    win = StreamerApp(socket_dict=socket_dict)
    win.show()
    sys.exit(app.exec())