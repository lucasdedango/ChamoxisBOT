import asyncio
import os
import time
import signal
import subprocess
from pathlib import Path
import aiohttp
import psutil


class Driver:
    """Only commands from the administrator-owned registry may be executed."""
    def __init__(self):
        self.processes = {}

    def matching_process(self, service):
        owned = self.processes.get(service.id)
        if owned and owned.poll() is None:
            return owned
        if service.command:
            expected = service.command
            for process in psutil.process_iter(["cmdline"]):
                try:
                    actual = process.info["cmdline"] or []
                    if len(actual) == len(expected) and actual[1:] == expected[1:] and (
                        actual[0] == expected[0] or Path(actual[0]).resolve() == Path(expected[0]).resolve()
                    ):
                        return process
                except (psutil.Error, OSError):
                    continue
        return None

    async def health(self, service):
        if service.type == "windows_service":
            if os.name != "nt":
                return False, "unsupported_platform"
            try:
                running = psutil.win_service_get(service.windows_name).status() == "running"
            except psutil.Error:
                return False, "service_unavailable"
            if not running:
                return False, "process_stopped"
        elif service.type in {"process", "python"}:
            if not self.matching_process(service):
                return False, "process_stopped"
        if service.health_url:
            headers = {}
            if service.health_key_env:
                headers["X-API-Key"] = os.getenv(service.health_key_env, "")
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=service.timeout)) as client:
                    async with client.get(service.health_url, headers=headers, allow_redirects=False) as response:
                        if response.status != 200:
                            return False, "api_unavailable"
                        if service.health_model:
                            body = await response.json()
                            names = {m.get("name") for m in body.get("models", [])}
                            if service.health_model not in names:
                                return False, "model_unavailable"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return False, "api_unavailable"
        elif service.type == "http":
            return False, "health_url_missing"
        return True, "healthy"

    async def start(self, service):
        if service.type == "http":
            raise RuntimeError("Cannot start a remote HTTP service")
        if service.type == "windows_service":
            if os.name != "nt":
                raise RuntimeError("Windows service controls require Windows")
            process = await asyncio.create_subprocess_exec("sc.exe", "start", service.windows_name,
                                                          stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await process.wait() != 0:
                raise RuntimeError("Windows service start failed")
        elif not self.matching_process(service):
            self.processes[service.id] = psutil.Popen(service.command, cwd=service.cwd,
                                                     stdout=None, stderr=None,
                                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)

    async def stop(self, service):
        if service.type == "windows_service":
            if os.name != "nt":
                raise RuntimeError("Windows service controls require Windows")
            process = await asyncio.create_subprocess_exec("sc.exe", "stop", service.windows_name,
                                                          stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await process.wait() != 0:
                raise RuntimeError("Windows service stop failed")
            return
        process = self.matching_process(service)
        if process:
            if os.name == "nt" and service.type == "python" and self.processes.get(service.id) is process:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=10)
            except (psutil.TimeoutExpired, TimeoutError):
                raise RuntimeError("Process did not stop; refusing to start a duplicate")


class Supervisor:
    def __init__(self, registry, driver=None, clock=time.monotonic, store=None):
        self.registry, self.driver, self.clock = registry, driver or Driver(), clock
        self.store = store
        self.states = {s.id: {"status": "unknown", "attempts": 0, "next_check": 0,
                              "next_restart": 0, "grace_until": clock() + s.startup_delay}
                       for s in registry.services.values()}
        if store:
            for key, state in self.states.items():
                state["attempts"] = store.get("restart_attempts:" + key, 0)
        self.lock = asyncio.Lock()

    def statuses(self):
        return [{"id": key, "name": self.registry.services[key].name,
                 "desired": self.registry.services[key].desired, **state} for key, state in self.states.items()]

    async def tick(self):
        async with self.lock:
            for key, service in self.registry.services.items():
                state, now = self.states[key], self.clock()
                if now < state["next_check"]:
                    continue
                state["next_check"] = now + service.interval
                if service.desired == "stopped":
                    # A disabled service is never treated as a crash and never started.
                    state["status"] = "disabled"
                    continue
                if now < state["grace_until"]:
                    state["status"] = "starting"
                    continue
                if any(self.states[dep]["status"] != "healthy" for dep in service.depends_on):
                    state["status"] = "waiting_dependencies"
                    continue
                ok, reason = await self.driver.health(service)
                state["status"] = reason
                if ok:
                    state.setdefault("healthy_since", now)
                    # Brief recovery must not reset a crash-loop budget.
                    if now - state["healthy_since"] >= max(60, service.interval * 3):
                        state["attempts"] = 0
                    continue
                state.pop("healthy_since", None)
                if service.desired != "running" or not service.restart or service.external_supervisor:
                    continue
                if state["attempts"] >= service.max_attempts:
                    state["status"] = "restart_limit"
                    continue
                if now < state["next_restart"]:
                    continue
                state["attempts"] += 1
                state["next_restart"] = now + service.backoff * 2 ** (state["attempts"] - 1)
                try:
                    # For API hangs, restart the verified configured process/service.
                    if reason == "api_unavailable":
                        await self.driver.stop(service)
                    await self.driver.start(service)
                    state["status"] = "starting"
                    state["grace_until"] = now + service.startup_delay
                except Exception:
                    state["status"] = "restart_failed"

    async def restart(self, key):
        async with self.lock:
            service = self.registry.services[key]
            if service.external_supervisor or service.type == "http" or service.desired != "running":
                raise ValueError("Service is not explicitly managed by this supervisor")
            await self.driver.stop(service)
            await self.driver.start(service)
            state = self.states[key]
            state.update(status="starting", attempts=0, next_check=0,
                         grace_until=self.clock() + service.startup_delay)
            state.pop("healthy_since", None)

    async def run(self):
        while True:
            previous = {key: state["status"] for key, state in self.states.items()}
            await self.tick()
            if self.store:
                from shared.schemas import Event
                for key, state in self.states.items():
                    self.store.set("restart_attempts:" + key, state["attempts"])
                    if previous[key] != state["status"]:
                        self.store.event(Event(source="manager", type="service.changed",
                                               data={"name": self.registry.services[key].name,
                                                     "status": state["status"],
                                                     "message": f"{self.registry.services[key].name}: {state['status']}"}))
            await asyncio.sleep(1)
