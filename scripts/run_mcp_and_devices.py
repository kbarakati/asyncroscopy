#!/usr/bin/env python
"""
Interactive CLI to start a Tango DB, register and run specified Tango devices,
and then start the MCP server.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import importlib
import contextlib
from typing import Callable
from pathlib import Path

from tango import Database, DbDevInfo, DeviceProxy
from tango.server import device_property

# Add the parent directory to Python path to allow asyncroscopy imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asyncroscopy.mcp.mcp_server import MCPServer

class ManagedProcess:
    def __init__(self, name: str, process: subprocess.Popen[str]):
        self.name = name
        self.process = process
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        stop_process(self)

def log_stderr(msg: str) -> None:
    """Log to stderr to avoid corrupting MCP stdout JSON-RPC."""
    print(msg, file=sys.stderr, flush=True)

def find_free_port(host: str = "127.0.0.1") -> tuple[int, socket.socket]:
    """Find a free port and return (port, socket)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    return int(sock.getsockname()[1]), sock

def make_env(tango_host: str) -> dict[str, str]:
    env = os.environ.copy()
    env["TANGO_HOST"] = tango_host
    env["PYTHONUNBUFFERED"] = "1"
    return env

def retry_until_success[T](func: Callable[[], T], timeout: float, error_msg: str) -> T:
    start = time.monotonic()
    last_error = None
    while time.monotonic() - start < timeout:
        try:
            return func()
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise TimeoutError(f"{error_msg} Last error: {last_error}")

def wait_for_process_output(
    proc: subprocess.Popen[str],
    expected_text: str,
    timeout: float,
    process_name: str,
) -> None:
    start = time.monotonic()
    seen_lines = []

    while time.monotonic() - start < timeout:
        if proc.poll() is not None:
            output = "\n".join(seen_lines)
            raise RuntimeError(
                f"{process_name} exited early with code {proc.returncode}.\n"
                f"Observed output:\n{output}"
            )

        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            line = line.rstrip("\n")
            seen_lines.append(line)
            log_stderr(f"[{process_name}] {line}")
            if expected_text in line:
                return
        else:
            time.sleep(0.05)

    output = "\n".join(seen_lines)
    raise TimeoutError(
        f"Timed out waiting for '{expected_text}' from {process_name}.\n"
        f"Observed output:\n{output}"
    )

def wait_for_device_ready(device_name: str, timeout: float = 10.0) -> None:
    def check():
        dev = DeviceProxy(device_name)
        dev.ping()
    retry_until_success(check, timeout, f"Timed out waiting for device '{device_name}' readiness.")

def connect_database(host: str, port: int, timeout: float = 10.0) -> Database:
    def check():
        db = Database(host, port)
        db.get_db_host()
        return db
    return retry_until_success(check, timeout, f"Timed out connecting to Tango DB at {host}:{port}.")

def stop_process(managed: ManagedProcess, timeout: float = 5.0) -> None:
    proc = managed.process
    if proc.poll() is not None:
        return

    log_stderr(f"[shutdown] terminating {managed.name} (pid={proc.pid})")
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log_stderr(f"[shutdown] killing {managed.name} (pid={proc.pid})")
        proc.kill()
        proc.wait(timeout=timeout)

def start_background_process(name: str, args: list[str], env: dict[str, str], expected_text: str, timeout: float, cwd: Path | None = None) -> ManagedProcess:
    log_stderr(f"[startup] Starting {name}...")
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    managed = ManagedProcess(name=name, process=proc)
    try:
        wait_for_process_output(proc, expected_text, timeout, name)
        return managed
    except Exception:
        stop_process(managed)
        raise

def get_class_from_name(class_name: str):
    """Dynamically find a Tango Device class in the asyncroscopy package."""
    module_paths_to_try = [
        f"asyncroscopy.{class_name}",
        f"asyncroscopy.hardware.{class_name}",
        f"asyncroscopy.detectors.{class_name}"
    ]
    
    for mod_path in module_paths_to_try:
        try:
            module = importlib.import_module(mod_path)
            if hasattr(module, class_name):
                return getattr(module, class_name)
        except ImportError:
            continue
            
    raise ValueError(f"Could not find class {class_name} in asyncroscopy")

def add_device(db: Database, server: str, classname: str, device: str):
    info = DbDevInfo()
    info.server = server
    info._class = classname
    info.name = device
    db.add_device(info)
    print(f"Registered '{device}' (Server: {server}, Class: {classname})")

def get_required_subdevices(class_name: str) -> list[dict[str, str]]:
    """Parses the class device properties to find sub-devices."""
    cls = get_class_from_name(class_name)
    sub_devices = []
    for attr_name in dir(cls):
        if attr_name.endswith("_device_address"):
            prop = getattr(cls, attr_name)
            if isinstance(prop, device_property):
                prefix = attr_name.split("_device_address")[0]
                sub_devices.append({
                    "class": prefix.upper(),
                    "attr_name": attr_name,
                    "prefix": prefix.lower()
                })
    return sub_devices

def cleanup_old_servers_for_class(class_name: str) -> None:
    """
    Cleanup of Tango servers related to a device class.
    Only runs if TANGO_HOST is already in the environment.
    """
    if "TANGO_HOST" not in os.environ:
        log_stderr(f"[startup] No TANGO_HOST set; skipping stale-server cleanup (no old DB to query)")
        return

    try:
        db = Database()
        related_classes = {class_name}

        try:
            for sub in get_required_subdevices(class_name):
                related_classes.add(sub["class"])
        except Exception as exc:
            log_stderr(f"[startup] Could not inspect related device classes for {class_name}: {exc}")

        for related_class in sorted(related_classes):
            servers = list(db.get_server_list(f"{related_class}/*"))
            log_stderr(f"[startup] Existing {related_class} servers: {servers}")

            for server in servers:
                try:
                    dserver_name = f"dserver/{server}"
                    log_stderr(f"[startup] Killing stale server via {dserver_name}")
                    dserver = DeviceProxy(dserver_name)
                    dserver.command_inout("Kill")
                except Exception as exc:
                    log_stderr(f"[startup] Failed to kill {server}: {exc}")
    except Exception as exc:
        log_stderr(f"[startup] Skipping stale-server cleanup: {exc}")

def main():
    host = "127.0.0.1"
    python_bin = sys.executable
    port_socket: socket.socket | None = None

    try:
        class_name = input("Enter the name of the main class to register (e.g., 'ThermoMicroscope'): ")

        cleanup_old_servers_for_class(class_name)

        port, port_socket = find_free_port(host)
        tango_host = f"{host}:{port}"

        print(f"[config] TANGO_HOST={tango_host}")
        os.environ["TANGO_HOST"] = tango_host

        env = make_env(tango_host)

        with contextlib.ExitStack() as stack:
            db_dir_obj = stack.enter_context(tempfile.TemporaryDirectory(prefix="tango-db-run-"))
            db_path = Path(db_dir_obj)

            # Start Tango DB
            db_proc = start_background_process(
                name="tango-db",
                args=[python_bin, "-m", "tango.databaseds.database", "2"],
                env=env,
                expected_text="Ready to accept request",
                timeout=30.0,
                cwd=db_path
            )
            stack.enter_context(db_proc)

            # Tango DB is now running and bound to the port; we can release the port-finder socket
            if port_socket is not None:
                port_socket.close()
                port_socket = None

            db = connect_database(host, port)
            device_name = f"test/{class_name.lower()}/1"
            server_name = f"{class_name}/{class_name.lower()}_instance"

            # Setup main device
            add_device(db, server_name, class_name, device_name)

            # Setup and Start Sub-devices
            sub_devices = get_required_subdevices(class_name)
            for sub in sub_devices:
                sub_classname = sub["class"]
                sub_device = f"test/{sub['prefix']}/1"
                sub_server = f"{sub_classname}/{sub['prefix']}_instance"

                # Register the sub-device and link it to the main device
                add_device(db, sub_server, sub_classname, sub_device)
                db.put_device_property(device_name, {sub['attr_name']: [sub_device]})
                print(f"  property:   {sub['attr_name']} = {sub_device}")

                # Start sub-device server
                cls = get_class_from_name(sub_classname)
                proc = start_background_process(
                    name=f"device-{cls.__module__.split('.')[-1]}",
                    args=[python_bin, "-m", cls.__module__, f"{sub['prefix']}_instance"],
                    env=env,
                    expected_text="Ready to accept request",
                    timeout=30.0
                )
                stack.enter_context(proc)
                wait_for_device_ready(sub_device, timeout=10.0)
                log_stderr(f"[startup] {sub_classname} device is fully accessible")

            # Start Main Device
            main_cls = get_class_from_name(class_name)
            main_proc = start_background_process(
                name=f"device-{main_cls.__module__.split('.')[-1]}",
                args=[python_bin, "-m", main_cls.__module__, f"{class_name.lower()}_instance"],
                env=env,
                expected_text="Ready to accept request",
                timeout=30.0
            )
            stack.enter_context(main_proc)
            
            wait_for_device_ready(device_name, timeout=10.0)
            log_stderr(f"[startup] Main {class_name} device is fully accessible")
            
            # Start MCPServer
            log_stderr("[startup] Initializing MCP Server...")
            server = MCPServer(
                name=f"MCPServer_{class_name}",
                tango_host=host,
                tango_port=port,
                blocked_classes=["DataBase", "DServer"],
                verbose=False,
            )

            mcp_host = input("Enter MCP server host (default: 127.0.0.1): ").strip() or "127.0.0.1"
            mcp_port_input = input("Enter MCP server port (default: 8000): ").strip()
            mcp_port = int(mcp_port_input) if mcp_port_input else 8000

            log_stderr(f"[startup] Starting MCP Server at {mcp_host}:{mcp_port}. Exported devices: {server.list_devices()}")
            server.start_http(host=mcp_host, port=mcp_port)
        
    except KeyboardInterrupt:
        log_stderr("\n[shutdown] KeyboardInterrupt received. Shutting down...")
    except Exception as exc:
        log_stderr(f"\n[error] Fatal error: {exc}")
        sys.exit(1)
    finally:
        if port_socket is not None:
            port_socket.close()

if __name__ == "__main__":
    main()
