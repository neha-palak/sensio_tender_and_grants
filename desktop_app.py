import logging
import os
import socket
import sys
import threading
import time
import webbrowser

# In a windowless build (no console on Windows / no Terminal on macOS) PyInstaller
# leaves stdout/stderr as None. Flask/Werkzeug + our own startup prints still write
# there, so a None stream would crash the app. Point them at the null device.
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name)
    if _stream is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
    else:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

logging.getLogger("werkzeug").setLevel(logging.ERROR)

from Website_frontend.server import app, PORT

HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}"


def _open_browser_when_ready():
    for _ in range(120):  # up to ~60s
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((HOST, PORT)) == 0:
                break
        time.sleep(0.5)
    webbrowser.open(URL)


threading.Thread(target=_open_browser_when_ready, daemon=True).start()

app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
