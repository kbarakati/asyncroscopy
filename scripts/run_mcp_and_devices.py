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
import pkgutil
from pathlib import Path

from tango import Database, DbDevInfo
from tango.server import device_property

# Add the parent directory to Python path to allow asyncroscopy imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncroscopy
from asyncroscopy.mcp.mcp_server import MCPServer

class ManagedProcess:
    def __init__(self, name: str, process: subprocess.Popen[str]):
        self.name = name
        self.process = process

def log_stderr(msg: str) -> None:
    """Log to stderr to avoid corrupting MCP stdout JSON-RPC."""
    print(msg, file=sys.stderr, flush=True)

def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])

def make_env(tango_host: str) -> dict[str, str]:
    env = os.environ.copy()
    env["TANGO_HOST"] = tango_host
    env["PYTHONUNBUFFERED"] = "1"
    return env

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
    import tango
    start = time.monotonic()
    last_error = None

    while time.monotonic() - start < timeout:
        try:
            dev = tango.DeviceProxy(device_name)
            dev.ping()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)

    raise TimeoutError(
        f"Timed out waiting for device '{device_name}' readiness. "
        f"Last error: {last_error}"
    )

def start_tango_db(python_bin: str, tango_host: str, work_dir: Path, timeout: float) -> ManagedProcess:
    log_stderr("[startup] Starting Tango DB...")
    env = make_env(tango_host)
    proc = subprocess.Popen(
        [python_bin, "-m", "tango.databaseds.database", "2"],
        cwd=work_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    managed = ManagedProcess(name="tango-db", process=proc)
    wait_for_process_output(proc, "Ready to accept request", timeout, managed.name)
    return managed

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

def start_device_server(python_bin: str, tango_host: str, module_path: str, instance: str, timeout: float) -> ManagedProcess:
    log_stderr(f"[startup] Starting device server {module_path}...")
    env = make_env(tango_host)
    proc = subprocess.Popen(
        [python_bin, "-m", module_path, instance],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    managed = ManagedProcess(name=f"device-{module_path.split('.')[-1]}", process=proc)
    wait_for_process_output(proc, "Ready to accept request", timeout, managed.name)
    return managed

def get_class_from_name(class_name: str):
    """Dynamically find a Tango Device class in the asyncroscopy package."""
    
    # Places to look for the class as a module
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

def add_device_and_sub_device(db: Database, server: str, classname: str, device: str):
    add_device(db, server, classname, device)
    
    cls = get_class_from_name(classname)
    
    servers_to_start = []
    
    # Introspect properties ending in _device_address
    for attr_name in dir(cls):
        if attr_name.endswith("_device_address"):
            prop = getattr(cls, attr_name)
            if isinstance(prop, device_property):
                # Extract prefix for class name. e.g. "scan" -> "SCAN"
                prefix = attr_name.split("_device_address")[0]
                sub_classname = prefix.upper()
                sub_device = f"test/{prefix.lower()}/1"
                sub_server = f"{sub_classname}/{prefix.lower()}_instance"
                
                # Register the sub-device
                add_device(db, sub_server, sub_classname, sub_device)
                
                # Configure the property on the parent device
                db.put_device_property(device, {attr_name: [sub_device]})
                print(f"  property:   {attr_name} = {sub_device}")
                
                servers_to_start.append({
                    "class": sub_classname,
                    "device": sub_device,
                    "instance": f"{prefix.lower()}_instance"
                })
    return servers_to_start

def main():
    host = "127.0.0.1"
    port = find_free_port(host)
    tango_host = f"{host}:{port}"
    python_bin = sys.executable
    
    print(f"[config] TANGO_HOST={tango_host}")
    os.environ["TANGO_HOST"] = tango_host
    
    managed_procs = []
    db_dir_obj = tempfile.TemporaryDirectory(prefix="tango-db-run-")
    db_path = Path(db_dir_obj.name)

    try:
        # Start Tango DB
        db_proc = start_tango_db(python_bin, tango_host, db_path, timeout=30.0)
        managed_procs.append(db_proc)
        
        db = Database()
        class_name = input("Enter the name of the main class to register (e.g., 'ThermoMicroscope'): ")
        device_name = f"test/{class_name.lower()}/1"
        server_name = f"{class_name}/{class_name.lower()}_instance"
        
        sub_servers = add_device_and_sub_device(db, server_name, class_name, device_name)
        
        # Start the dynamically found sub-devices
        for sub in sub_servers:
            # Find which module contains the class to know how to start it
            cls = get_class_from_name(sub["class"])
            module_file = sys.modules[cls.__module__].__file__
            module_path = cls.__module__
            
            proc = start_device_server(python_bin, tango_host, module_path, sub["instance"], timeout=30.0)
            managed_procs.append(proc)
            wait_for_device_ready(sub["device"], timeout=10.0)
            log_stderr(f"[startup] {sub['class']} device is fully accessible")

        # Start Main Device
        main_cls = get_class_from_name(class_name)
        main_module_path = main_cls.__module__
        main_proc = start_device_server(python_bin, tango_host, main_module_path, f"{class_name.lower()}_instance", timeout=30.0)
        managed_procs.append(main_proc)
        
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
        for proc in reversed(managed_procs):
            stop_process(proc)
        db_dir_obj.cleanup()

if __name__ == "__main__":
    main()
