"""Start actual application entry points with isolated data and no credentials."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
import httpx


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EntrypointTests(unittest.TestCase):
    def test_both_apps_start_and_refuse_a_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / "services.json"
            services.write_text('{"services": []}', encoding="utf-8")
            env = os.environ.copy()
            env.update({"DISCORD_TOKEN": "", "MANAGER_API_KEY": "dummy-manager-key", "MANAGER_ADMIN_API_KEY": "dummy-admin-key",
                        "PLEX_MODULE_API_KEY": "dummy-plex-key", "MANAGER_ENV_FILE": str(root / "absent-manager.env"),
                        "PLEX_ENV_FILE": str(root / "absent-plex.env"), "SERVICES_CONFIG": str(services),
                        "MANAGER_DATA_DIR": str(root / "manager"), "PLEX_DATA_DIR": str(root / "plex"),
                        "PLEX_MOVIES_PATHS": tmp, "PLEX_SERIES_PATHS": tmp,
                        "DISABLE_TORRENT_DOWNLOAD": "true", "MANAGER_PORT": str(free_port()), "PLEX_MODULE_PORT": str(free_port())})
            env["MANAGER_URL"] = "http://127.0.0.1:" + env["MANAGER_PORT"]
            processes = []
            try:
                for module, port, key in [("manager.main", env["MANAGER_PORT"], "dummy-manager-key"),
                                          ("modules.plex.main", env["PLEX_MODULE_PORT"], "dummy-plex-key")]:
                    process = subprocess.Popen([sys.executable, "-m", module], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    processes.append(process)
                    ready = False
                    with httpx.Client(trust_env=False, timeout=.5) as client:
                        for _ in range(100):
                            if process.poll() is not None:
                                self.fail(f"{module} exited unexpectedly with {process.returncode}")
                            try:
                                response = client.get(f"http://127.0.0.1:{port}/health", headers={"X-API-Key": key})
                                if response.status_code == 200:
                                    ready = True
                                    self.assertEqual(response.json()["status"], "ok")
                                    break
                            except httpx.HTTPError:
                                pass
                            time.sleep(.05)
                    self.assertTrue(ready, module)
                    duplicate = subprocess.run([sys.executable, "-m", module], env=env,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    self.assertNotEqual(duplicate.returncode, 0)
                    self.assertIsNone(process.poll())
            finally:
                for process in reversed(processes):
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
