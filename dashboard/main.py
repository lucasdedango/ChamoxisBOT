import os
import secrets
import threading
import webbrowser
from pathlib import Path
import uvicorn
from filelock import FileLock
from dashboard.server import create_app


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    data = root / "data/dashboard"
    data.mkdir(parents=True, exist_ok=True)
    with FileLock(str(data / "dashboard.lock"), timeout=0):
        token = secrets.token_urlsafe(32)
        url = "http://127.0.0.1:8765/#login=" + token
        if os.getenv("DASHBOARD_NO_BROWSER") != "true":
            timer = threading.Timer(1.5, lambda: webbrowser.open(url))
            timer.daemon = True
            timer.start()
        uvicorn.run(create_app(root, launch_token=token), host="127.0.0.1", port=8765, access_log=False)


if __name__ == "__main__":
    main()
