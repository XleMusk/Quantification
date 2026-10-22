"""Portable local workbench launcher. Workers are opt-in; Ctrl+C stops children."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


PROJECT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT / "webapp" / "frontend"
REQUIRED = ("fastapi", "uvicorn", "pandas", "numpy", "pyarrow", "scipy", "python_multipart")


def default_trading_home():
    if os.name == "nt":
        return Path("D:/QuantificationData/trading")
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "quantification" / "trading"


def environment(args):
    env = os.environ.copy()
    data_dir = args.data_dir or env.get("QUANT_TRADING_HOME") or default_trading_home()
    storage = args.web_storage or env.get("QUANT_WEB_STORAGE") or PROJECT / "webapp" / "storage"
    env.update(
        QUANT_TRADING_HOME=str(Path(data_dir).expanduser().resolve()),
        QUANT_WEB_STORAGE=str(Path(storage).expanduser().resolve()),
        QUANT_API_PORT=str(args.api_port),
        QUANT_UI_PORT=str(args.ui_port),
        QUANT_UI_ORIGIN=f"http://127.0.0.1:{args.ui_port}",
        PYTHONUNBUFFERED="1",
        PYTHONUTF8="1",
        PYTHONPATH=os.pathsep.join(filter(None, (str(PROJECT), env.get("PYTHONPATH")))),
    )
    return env


def check(env):
    missing = [name for name in REQUIRED if importlib.util.find_spec(name) is None]
    node = shutil.which("node")
    vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    return {
        "ready": not missing and bool(node) and vite.is_file(),
        "python": sys.executable,
        "missing_python_modules": missing,
        "node": node,
        "frontend_installed": vite.is_file(),
        "trading_home": env["QUANT_TRADING_HOME"],
        "web_storage": env["QUANT_WEB_STORAGE"],
        "historical_snapshot_present": (Path(env["QUANT_TRADING_HOME"]) / "datasets" /
                                        "hs300_20231229_pilot" / "manifest.json").is_file(),
        "live_trading": False,
    }


def require_ports(api_port, ui_port):
    if api_port == ui_port:
        raise ValueError("API and UI require different ports")
    for port in (api_port, ui_port):
        if not 1 <= port <= 65535:
            raise ValueError("Ports must be between 1 and 65535")
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise ValueError(f"Port {port} is occupied; choose another port") from error


def spawn(command, cwd, env):
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
    }
    return subprocess.Popen(command, cwd=cwd, env=env, **options)


def wait_for_http(process, url, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Service exited with code {process.returncode}: {url}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(.1)
    raise RuntimeError(f"Service did not become ready: {url}")


def stop_children(children):
    for process in reversed(children):
        if process.poll() is not None:
            continue
        try:
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                process.wait(timeout=5)
                continue
            except OSError:
                if process.poll() is None:
                    process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            # One child exiting during escalation must not leave the others up.
            print(f"Cleanup of PID {process.pid}: {error}", file=sys.stderr)


def start(args, env):
    require_ports(args.api_port, args.ui_port)
    if args.seed_demo:
        subprocess.run([
            sys.executable, "-c",
            "from webapp.backend.app import data, store; store.initialize(); "
            "print('Synthetic demo dataset:', data.seed_demo()['id'])",
        ], cwd=PROJECT, env=env, check=True)

    children = []
    try:
        api = spawn([sys.executable, "-m", "uvicorn", "webapp.backend.app.main:app",
                     "--host", "127.0.0.1", "--port", str(args.api_port)], PROJECT, env)
        children.append(api)
        wait_for_http(api, f"http://127.0.0.1:{args.api_port}/api/health")
        ui = spawn([shutil.which("node"), str(FRONTEND / "node_modules/vite/bin/vite.js")], FRONTEND, env)
        children.append(ui)
        wait_for_http(ui, env["QUANT_UI_ORIGIN"])
        for enabled, module in ((args.factor_worker, "webapp.workers.factor_worker"),
                                (args.trading_worker, "trading.worker")):
            if enabled:
                children.append(spawn([sys.executable, "-m", module], PROJECT, env))
        print(f"Workbench: {env['QUANT_UI_ORIGIN']}", flush=True)
        print(f"API: http://127.0.0.1:{args.api_port}/docs", flush=True)
        print(f"Factor worker: {args.factor_worker}; paper/replay worker: {args.trading_worker}", flush=True)
        print("Press Ctrl+C to stop this launcher's services. Live submission remains disabled.", flush=True)
        while True:
            for process in children:
                if process.poll() is not None:
                    raise RuntimeError(f"Service PID {process.pid} exited with code {process.returncode}")
            time.sleep(.25)
    finally:
        stop_children(children)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "start"))
    parser.add_argument("--data-dir", type=Path, help="Override QUANT_TRADING_HOME")
    parser.add_argument("--web-storage", type=Path, help="Override QUANT_WEB_STORAGE")
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--ui-port", type=int, default=5175)
    parser.add_argument("--factor-worker", action="store_true", help="Opt in to queued factor calculations")
    parser.add_argument("--trading-worker", action="store_true", help="Opt in to paper/replay worker and EOD data refresh")
    parser.add_argument("--seed-demo", action="store_true", help="Register explicitly synthetic teaching data; no market download")
    args = parser.parse_args(argv)
    env = environment(args)
    status = check(env)
    if args.command == "check":
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["ready"] else 2
    if not status["ready"]:
        print(json.dumps(status, ensure_ascii=False, indent=2), file=sys.stderr)
        print("Install backend/requirements.lock.txt in this Python environment, then run npm ci in webapp/frontend.",
              file=sys.stderr)
        return 2
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    old_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        start(args, env)
    except KeyboardInterrupt:
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_term)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
