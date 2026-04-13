import os
import sys
import cv2
import time
import struct
import socket
import threading
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QMainWindow, QPushButton, QWidget, QLabel, 
    QPushButton, QComboBox, QLineEdit, QVBoxLayout, QSizePolicy
)

# Local Imports
from __version__ import version
from wrapper import pprint, read_yaml

VALID_SCAN_CODES = set(range(1, 84))
send_lock = threading.Lock()

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

def connect_to_server(host: str, port: int, retries: int = 5, delay: float = 1.0) -> socket.socket:
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

def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from the socket, or raise if connection closes."""

    data = b''
    while len(data) < n:
        try:
            packet = sock.recv(n - len(data))
            if not packet:
                raise ConnectionError("Connection closed before all bytes were received.")
            data += packet
        except (BlockingIOError, socket.timeout):
            continue
    return data


class VideoLabel(QLabel):
    """QLabel that maps clicks back to capture coordinates and sends them to the server."""

    def __init__(self, parent=None) -> None:
        super(VideoLabel, self).__init__(parent)

        self.capture_w = 1
        self.capture_h = 1
        self.running = False
        self.control_conn  = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        time.sleep(0.005)

        # Send the key press event to the server
        with send_lock:
            self.control_conn.sendall(msg.encode('utf-8'))
            pprint(f"Sent key press: {key}")

    def mousePressEvent(self, event) -> None:
        """ Actions on how to handle mouse clicks """

        if event.button() != Qt.MouseButton.LeftButton or not self.running:
            return super().mousePressEvent(event)

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

        click_x = event.position().x()
        click_y = event.position().y()

        pprint(f"Label: ({lbl_w}x{lbl_h}) | Pixmap: ({px_w}x{px_h}) | Offset: ({offset_x},{offset_y}) | Click: ({click_x},{click_y})")
        if not (offset_x <= click_x < offset_x + px_w and
                offset_y <= click_y < offset_y + px_h):
            pprint("Clicked outside image area")
            return super().mousePressEvent(event)

        rel_x    = (click_x - offset_x) / px_w
        rel_y    = (click_y - offset_y) / px_h
        screen_x = int(rel_x * self.capture_w)
        screen_y = int(rel_y * self.capture_h)

        try: # Send the clicked location ----------
            msg = f"click:{screen_x},{screen_y}\n"
            with send_lock:
                self.control_conn.sendall(msg.encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError):
            pprint("Failed to send click — connection lost.")
        super().mousePressEvent(event)

class StreamerApp(QMainWindow):
    """ Initial Window to connect to the server and select the display to stream """

    control_conn = None
    video_conn = None

    def __init__(self, socket_dict:dict={}) -> None:
        super(StreamerApp, self).__init__()

        # Get the addresses --------
        self._pressed_keys = set()
        self.socket_dict = socket_dict
        self.init_ui()
    
    def init_ui(self):
        """ Initialize the UI components of the connection window """

        central_widget = QWidget()
        central_widget.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(central_widget)

        hosts = list(self.socket_dict.keys())
        self.ip_combo = QComboBox()
        self.ip_combo.addItems(hosts)
        self.ip_combo.setFixedWidth(250)

        self.port_input = QLineEdit("3000")
        self.port_input.setPlaceholderText("Port")
        self.port_input.setFixedWidth(100)

        # TODO: Change to combobox based on how many displays are avalaible
        self.view_combo = QComboBox()
        self.view_combo.currentIndexChanged.connect(self.view_event)
        self.view_combo.setFixedWidth(250)

        self.conn_btn = QPushButton("Connect")
        self.conn_btn.clicked.connect(self.connect)

        self.info_lbl = QLabel("Not Connected.")
        self.stream_lbl = VideoLabel("Click Connect to View Stream")
        self.stream_lbl.setMinimumSize(640, 400)
        self.stream_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding
        )

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
        self.resize(1920, 1080)
        self.setWindowTitle(f"Nature Station v{version}")

    def disconnect(self):
        """ Stop the stream """

        if not self.stream_lbl.running:
            pprint("No active streams avaliable.")
            return

        self.view_combo.clear()
        self.info_lbl.setText("Stream ended.")
        self.stream_lbl.setText("Click Connect To View")
        self.conn_btn.setText("Connect")
        self.stream_lbl.running = False
        
        # Close TCP Connections
        self.video_conn.close()
        self.control_conn.close()

    def connect(self):
        """ Connect to the server and start the video stream """

        if self.stream_lbl.running:
            """ Close the connection """

            pprint("Closing connection!")
            self.disconnect()
            self.stream_lbl.running = False
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
            if not self.stream_lbl.running:
                break

            try: # Recieve an incoming control change from server
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
            capture_w = struct.unpack('>I', recv_exact(self.video_conn, 4))[0]
            capture_h = struct.unpack('>I', recv_exact(self.video_conn, 4))[0]
            num_displays = struct.unpack('>I', recv_exact(self.video_conn, 4))[0]

            # Store values about display --------------------
            views = [f"View {i}" for i in range(num_displays)]
            self.view_combo.addItems(views)

            # Wire up the label's connection once we know cap dimensions
            self.stream_lbl.control_conn  = self.control_conn
            self.stream_lbl.capture_w = capture_w
            self.stream_lbl.capture_h = capture_h

            # Enable key functions ---------------
            # kb_hook = keyboard.on_release(lambda e: key_press(e.scan_code, param=self.control_conn))
            pprint(f"Server screen dimensions: ({capture_w}x{capture_h}) Total Displays: {num_displays}")

            while self.stream_lbl.running:
                if not self.stream_lbl.running:
                    pprint("Closing Video Stream...")
                    break
        
                raw_size = recv_exact(self.video_conn, 4)
                frame_size = struct.unpack('>I', raw_size)[0]

                if frame_size == 0:
                    pprint("Received zero-length frame. Skipping.")
                    continue

                if frame_size > 10 * 1024 * 1024:
                    pprint(f"Implausible frame size: {frame_size} bytes. Closing.")
                    break

                frame_data = recv_exact(self.video_conn, frame_size)
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
        finally:
            # keyboard.unhook(kb_hook)
            self.disconnect()

    def closeEvent(self, a0):
        """ Action to do before closing """

        self.disconnect()
        return super().closeEvent(a0)

    def keyPressEvent(self, event) -> None:
        if event.isAutoRepeat():
            return

        key = event.key()
        if key in self._pressed_keys:  # Already down, skip
            return

        self._pressed_keys.add(key)
        text = event.text()
        if text and self.stream_lbl.control_conn:
            self.stream_lbl.key_press(text)

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.isAutoRepeat():
            return

        self._pressed_keys.discard(event.key())  # Mark key as released
        super().keyReleaseEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    curr_dir = os.getcwd()
    addr_pth = os.path.join(curr_dir, "address.yaml")
    socket_dict = read_yaml(addr_pth)
    win = StreamerApp(socket_dict=socket_dict)
    win.show()
    sys.exit(app.exec())
