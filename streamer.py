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
    QApplication,
    QMainWindow,
    QHBoxLayout,
    QPushButton,
    QComboBox,
    QVBoxLayout,
    QSizePolicy,
    QLineEdit,
    QWidget,
    QLabel,
)

# Logging Setup -----
import yaml
import logging
import logging.config

# Load the YAML configuration
with open('logger.yaml', 'r') as file:
    config = yaml.safe_load(file)
logging.config.dictConfig(config)

# Create a logger
logger = logging.getLogger('streamer')
logger.debug('This is a debug message')
logger.info('This is an info message')

# Local Imports
from __version__ import version
from common_utils.wrapper import pprint, read_yaml

send_lock = threading.Lock()
KEY_MAP = {
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_Return: "return",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Escape: "escape",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
}

def fit_rect(src_w, src_h, dst_w, dst_h):
    """ Calculate the largest centered rectangle of aspect ratio that fits within the destination """

    src_ar = src_w / src_h
    dst_ar = dst_w / dst_h

    if dst_ar > src_ar:
        h = dst_h; w = int(h * src_ar)
    else:
        w = dst_w; h = int(w / src_ar)
    x = (dst_w - w) // 2; y = (dst_h - h) // 2
    return x, y, w, h

def connect_to_server(host: str, port: int, retries:int=5, delay:float=0.1) -> socket.socket:
    """Establish a TCP connection to the server, with retries."""

    for attempt in range(retries):
        try:
            tcp_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_conn.connect((host, port))
            print(f"Connected to server at {host}:{port}")
            return tcp_conn
        except (ConnectionRefusedError, OSError) as e:
            pprint(f"Connection attempt {attempt + 1}/{retries} failed: {e}")
            tcp_conn.close()
            time.sleep(delay)

    error = f"Could not connect to {host}:{port} after {retries} attempts."
    raise ConnectionRefusedError(error)


class MouseTracker(QObject):

    actionCalled = pyqtSignal(str, str, QPointF)

    def __init__(self, widget):
        super(MouseTracker, self).__init__(widget)

        self.last_action = None
        self.last_button = None

        # Avaliable Mouse Actions --------
        self.mouse_actions = {
            QEvent.Type.MouseButtonPress: "click",
            QEvent.Type.MouseButtonRelease: "release",
            QEvent.Type.Wheel: "wheel",
            QEvent.Type.Leave: "leave",
        }
        self.mouse_buttons = {
            Qt.MouseButton.LeftButton: "left",
            Qt.MouseButton.RightButton: "right",
        }

        # Setup the video label ------
        self.widget = widget
        self.widget.setMouseTracking(True)
        self.widget.installEventFilter(self)

    def eventFilter(self, o, e):
        if not self.widget.running:
            return super().eventFilter(o, e)

        atype = self.mouse_actions.get(e.type())
        if not atype:
            return super().eventFilter(o, e)

        if atype == "leave":
            if self.last_button:
                self.actionCalled.emit("leave", self.last_button, QPointF())
            return super().eventFilter(o, e)

        btype = self.mouse_buttons.get(e.button())
        if not btype:
            return super().eventFilter(o, e)
        self.actionCalled.emit(atype, btype, e.position())

        # Store last known values ------
        self.last_action = atype
        self.last_button = btype
        self.last_loc = e.position()
        return super().eventFilter(o, e)

class VideoLabel(QLabel):
    """QLabel that maps clicks back to capture coordinates and sends them to the server."""

    def __init__(self, parent=None) -> None:
        super(VideoLabel, self).__init__(parent)

        self.capture_w = 1
        self.capture_h = 1
        self.running = False
        self.control_conn  = None

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setScaledContents(False)

    def set_frame(self, pixmap: QPixmap) -> None:
        """ Scale the pixmap to the size of the label """

        # Scale pixmap to fit label while preserving aspect ratio
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.setPixmap(scaled)

    def key_press(self, key):
        """ Send key press event to the server """

        msg = f"key:{key}\n"
        self.send_instruct(msg)

    @pyqtSlot(str, str, QPointF)
    def send_action(self, atype:str, btype:str, coords:QPointF) -> None:
        """ Actions on how to handle mouse clicks """

        # Do not have to send position on release
        if atype == "leave":
            msg = f"mouse:{atype}:{btype}\n"
            self.send_instruct(msg)
            return

        lbl_w = self.width()
        lbl_h = self.height()

        # Recalculate live from current label size — never use stale _scaled_w/h
        src_ar = self.capture_w / self.capture_h
        lbl_ar = lbl_w / lbl_h

        if lbl_ar > src_ar:
            # Label is wider than image — letterbox left/right
            px_h = lbl_h
            px_w = int(lbl_h * src_ar)
        else:
            # Label is taller than image — letterbox top/bottom
            px_w = lbl_w
            px_h = int(lbl_w / src_ar)

        offset_x = (lbl_w - px_w) // 2
        offset_y = (lbl_h - px_h) // 2
        click_x = coords.x()
        click_y = coords.y()

        # Check if the click is valid or not
        if not (offset_x <= click_x < offset_x + px_w and
                offset_y <= click_y < offset_y + px_h):
            return

        rel_x    = (click_x - offset_x) / px_w
        rel_y    = (click_y - offset_y) / px_h
        screen_x = int(rel_x * self.capture_w)
        screen_y = int(rel_y * self.capture_h)

        # Send the clicked location ----------
        msg = f"mouse:{atype}:{btype}:{screen_x},{screen_y}\n"
        self.send_instruct(msg)

    def send_instruct(self, msg) -> None:
        """ Send instructions over the network """

        try:
            with send_lock:
                self.control_conn.sendall(msg.encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError):
            pprint("Failed to send action — connection lost.")

class StreamerApp(QMainWindow):
    """ Initial Window to connect to the server and select the display to stream """

    control_conn = None
    video_conn = None

    def __init__(self, socket_dict:dict={}) -> None:
        super(StreamerApp, self).__init__()

        # Get the addresses --------
        self.socket_dict = socket_dict
        self.init_ui()
    
    def init_ui(self):
        """ Initialize the UI components of the connection window """

        central_widget = QWidget()
        central_widget.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(central_widget)

        self.create_menu()
        self.port_input = QLineEdit("3000")
        self.port_input.setPlaceholderText("Port")
        self.port_input.setFixedWidth(100)

        hosts = list(self.socket_dict.keys())
        self.ip_combo = QComboBox()
        self.ip_combo.addItems(hosts)
        self.ip_combo.setFixedWidth(250)

        self.view_combo = QComboBox()
        self.view_combo.activated.connect(self.view_event)
        self.view_combo.setFixedWidth(250)

        self.conn_btn = QPushButton("Connect")
        self.conn_btn.clicked.connect(self.connect)
        self.conn_btn.setAutoDefault(False)

        self.info_lbl = QLabel("Not Connected.")
        self.stream_lbl = VideoLabel("Click Connect to View Stream")
        self.stream_lbl.setMinimumSize(640, 400)
        self.stream_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding
        )

        # Setup the mouse tracker ------------
        tracker = MouseTracker(self.stream_lbl)
        tracker.actionCalled.connect(self.stream_lbl.send_action)

        hlay = QHBoxLayout()
        hlay.addWidget(self.ip_combo)
        hlay.addWidget(self.port_input)
        hlay.addWidget(self.view_combo)
        hlay.addStretch()

        vlay = QVBoxLayout()
        vlay.setContentsMargins(2, 2, 2, 2)
        vlay.addLayout(hlay)
        vlay.addWidget(self.info_lbl)
        vlay.addStretch()

        mlay = QVBoxLayout()
        mlay.setContentsMargins(0, 0, 0, 0)
        mlay.setSpacing(2)
        mlay.addLayout(vlay)
        mlay.addWidget(self.stream_lbl, stretch=1)
        mlay.addWidget(self.conn_btn)
        self.centralWidget().setLayout(mlay)
        self.setWindowTitle(f"Nature Station v{version}")
        self.resize(1920, 1080)

    def create_menu(self) -> None:
        """ Extented Menu Options """

        settings = QAction("Settings", self)
        key_binds = QAction("Key Binds", self)
        settings.triggered.connect(self.launch_settings)
        key_binds.triggered.connect(self.launch_binds)
        
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        file_menu.addAction(settings)
        file_menu.addAction(key_binds)

    def launch_settings(self) -> None:
        """ Display the settings window """

        # TODO: Build the settings window to add new sockets
        pprint("Launch the settings window")

    def launch_binds(self) -> None:
        """ Display the keybinds window """

        # TODO: Build the keybinds window to set custom keybinds
        pprint("Launch the keybinds window")

    def disconnect(self):
        """ Operations to complete at disconnect of stream """

        if not self.stream_lbl.running:
            pprint("No active streams avaliable.")
            return

        self.stream_lbl.running = False
        self.view_combo.clear()
        self.video_conn.close()
        self.control_conn.close()
        self.conn_btn.setText("Connect")
        self.info_lbl.setText("Stream ended.")
        self.stream_lbl.setText("Click Connect To View")

    def connect(self):
        """ Connect to the server and start the video stream """

        if self.stream_lbl.running:
            pprint("Closing connection!")
            self.disconnect()
            return
    
        # Attempt tp connect to server using the selected IP address
        self.info_lbl.setText("Connecting...")
        port = self.port_input.text().strip()
        if not port.isdigit():
            self.info_lbl.setText("Invalid port number.")
            return

        # Check the ip combo ----------------
        host = self.ip_combo.currentText()
        ip_addr = self.socket_dict.get(host)
        if not ip_addr:
            pprint(f"IP address for {host} not found.")
            return

        try:
            video_port, control_port = int(port), int(port) + 1
            self.video_conn = connect_to_server(ip_addr, video_port)
            self.control_conn = connect_to_server(ip_addr, control_port)
        except Exception as e:
            self.info_lbl.setText(f"Failed to connect to {host} ({ip_addr})")
            return

        # Start the video stream -----------
        self.info_lbl.setText(f"Connected to {host} ({ip_addr}).")
        self.conn_btn.setText("Disconnect")

        # Toggle streaming status -----
        self.stream_lbl.running = not self.stream_lbl.running
        QApplication.processEvents()

        # Separate thread just for control messages
        self.ctrl_thread = threading.Thread(target=self.control_listener, daemon=True)
        self.ctrl_thread.start()

        # Frame thread unchanged
        self.frame_thread = threading.Thread(target=self.display_video)
        self.frame_thread.start()

    def control_listener(self) -> None:
        """Dedicated thread — only reads control messages, never blocks on frames."""
    
        buffer = ""
        while self.stream_lbl.running:
            try:
                # Recieve an incoming control change from server
                chunk = self.control_conn.recv(1024).decode('utf-8')
                if not chunk:
                    break

                # Process complete newline-terminated messages
                buffer += chunk
                while '\n' in buffer:
                    msg, buffer = buffer.split('\n', 1)
                    self.handle_control(msg.strip())

            except (ConnectionError, OSError):
                pprint("Lost connection... Exiting stream.")
                break

    def recv_exact(self,  n: int) -> bytes:
        """
        Read exactly n bytes from the socket, or raise if connection closes

        Args: n (int) - Amount of bytes to process in stream
        Return: (bytes)
        """

        data = b''
        while len(data) < n:
            try:
                packet = self.video_conn.recv(n - len(data))
                if not packet:
                    raise ConnectionError("Connection closed before all bytes were received.")
                data += packet
            except (BlockingIOError, socket.timeout):
                pprint("Unable to extract bytes...")
                return b''
        return data

    def handle_control(self, msg: str) -> None:
        """Handle a single control message."""

        if msg.startswith("res:"):
            _, dims = msg.split("res:")
            new_w, new_h = map(int, dims.split(","))
            self.stream_lbl.capture_w = new_w
            self.stream_lbl.capture_h = new_h
            pprint(f"Resolution updated: {new_w}x{new_h}")

        elif msg.startswith("displays:"):
            _, count = msg.split("displays:")
            pprint(f"Display count updated: {count}")

    def view_event(self, index) -> None:
        """ Send the message to change to a specific view """

        msg = f"view:{index}\n"
        with send_lock:
            self.control_conn.sendall(msg.encode('utf-8'))
            pprint(f"Sent view: (View {index})")

    def display_video(self) -> None:
        """Receive video frames from the server and display them."""

        try:
            # Read handshake once before the loop
            capture_w, capture_h, num_displays = struct.unpack('>III', self.recv_exact(12))

            # Store values about display --------------------
            views = [f"View {i}" for i in range(num_displays)]
            self.view_combo.addItems(views)

            # Wire up the label's connection once we know cap dimensions
            self.stream_lbl.control_conn  = self.control_conn
            self.stream_lbl.capture_w = capture_w
            self.stream_lbl.capture_h = capture_h

            # Enable key functions ---------------
            pprint(f"Server screen dimensions: ({capture_w}x{capture_h}) Total Displays: {num_displays}")
            while self.stream_lbl.running:
                if not self.stream_lbl.running:
                    pprint("Closing Video Stream...")
                    break
        
                frame_size = struct.unpack('>I', self.recv_exact(4))[0]
                if frame_size == 0:
                    pprint("Received zero-length frame. Skipping.")
                    continue

                if frame_size > 10 * 1024 * 1024:
                    pprint(f"Implausible frame size: {frame_size} bytes. Closing.")
                    break

                frame_data = self.recv_exact(frame_size)
                frame_array = np.frombuffer(frame_data, dtype=np.uint8)
                frame = cv2.imdecode(frame_array, cv2.IMREAD_COLOR)

                if frame is None:
                    pprint("Failed to decode frame. Skipping.")
                    continue
        
                src_h, src_w = frame.shape[:2]
                dst_w, dst_h = src_w // 2, src_h // 2
                x, y, w, h = fit_rect(src_w, src_h, dst_w, dst_h)

                # Convert frame to RGB format
                image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = image.shape
                bytes_per_line = ch * w
                qt_image = QImage(image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                pixmap = QPixmap.fromImage(qt_image)
                self.stream_lbl.set_frame(pixmap)

        except ConnectionError as e:
            self.info_lbl.setText(f"Connection error: {e}")
        except struct.error as e:
            self.info_lbl.setText(f"Failed to unpack frame size: {e}")

    def closeEvent(self, a0):
        """ Action to do before closing """

        self.disconnect()
        return super().closeEvent(a0)

    def keyPressEvent(self, event) -> None:
        """ Get the keyboard events to send over network """

        if event.isAutoRepeat():
            pprint("Sticky keys detected! Canceling action.")
            return

        key = event.key()
        text = KEY_MAP.get(key)
        if not text:
            text = event.text()

        # Send the key input to server ----------
        if text and self.stream_lbl.control_conn:
            self.stream_lbl.key_press(text)
        super().keyPressEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Get the address yaml file -----------
    curr_dir = os.getcwd()
    addr_pth = os.path.join(curr_dir, "config", "address.yaml")
    socket_dict = read_yaml(addr_pth)
    win = StreamerApp(socket_dict=socket_dict)
    win.show()
    sys.exit(app.exec())
