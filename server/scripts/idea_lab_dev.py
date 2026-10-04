"""Start/stop only the explicitly marked, isolated local Idea Lab development server.

No systemd registration, installer, production profile or existing session is used.
Run with the development Python environment on Linux, as the native CLI user.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import sys
import time
from urllib.request import Request, ProxyHandler, build_opener

SERVER = Path(__file__).resolve().parents[1]
MARKER = "agentsdock-isolated-idea-lab-v1"
CLIENT_DIRECTORY = "AgentsDockIdeaLabData"


def reject_symlinks(path: Path):
    """Validate every existing component before following or creating a path."""
    for component in (path, *path.parents):
        if component.is_symlink():
            raise RuntimeError("Idea Lab paths must not contain symlinks")


def reject_linked_descendants(root: Path, codex=None):
    """Keep existing nested server state inside the private development tree."""
    reject_symlinks(root)
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        for name in (*subdirectories, *files):
            path = Path(directory) / name
            if path.is_symlink():
                # Native Codex creates executable aliases in its private arg0
                # scratch directory. Permit only its known aliases pointing to
                # the configured executable; never permit linked state, auth,
                # directories or arbitrary targets.
                parts = path.relative_to(root).parts
                native_alias = (codex and len(parts) == 6 and parts[:4] == ("home", ".codex", "tmp", "arg0")
                    and parts[4].startswith("codex-arg0")
                    and name in ("codex-linux-sandbox", "codex-execve-wrapper", "applypatch", "apply_patch")
                    and path.is_file() and path.resolve() == Path(codex).resolve())
                if not native_alias:
                    raise RuntimeError("Idea Lab paths must not contain symlinks")


def read_json_regular(path: Path):
    reject_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Idea Lab metadata must be a regular, unlinked file")
        text = stream.read(65537)
        if len(text) > 65536:
            raise RuntimeError("Idea Lab metadata is too large")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise RuntimeError("Invalid Idea Lab metadata")
        return value


def write_private(path: Path, value: str):
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Idea Lab metadata must be a regular, unlinked file")
        os.ftruncate(stream.fileno(), 0)
        stream.write(value)


def settings(root: Path):
    path = root / "development.json"
    value = read_json_regular(path)
    if value.get("kind") != MARKER or value.get("root") != str(root):
        raise RuntimeError("This directory is not the isolated Idea Lab instance")
    return value


def owned_pid(root: Path):
    try:
        reject_symlinks(root / "server.pid")
        pid = int((root / "server.pid").read_text())
        environment = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
        expected = ("AGENTSDOCK_IDEA_LAB_DEV_ROOT=" + str(root)).encode()
        if expected in environment:
            return pid
    except (OSError, ValueError):
        pass
    return None


def client_profile_target(value):
    profile = Path(value).absolute()
    if profile.name != "idea-lab-dev-profile.json" or profile.parent.name != CLIENT_DIRECTORY:
        raise RuntimeError("Use idea-lab-dev-profile.json inside the dedicated AgentsDockIdeaLabData directory")
    reject_symlinks(profile)
    if profile.parent.exists() and any(profile.parent.iterdir()):
        if not profile.exists() or read_json_regular(profile).get("kind") != MARKER:
            raise RuntimeError("Refusing a nonempty client directory without its Idea Lab development marker")
    return profile


def prepare(args, root):
    # Reject an existing working app profile before writing a marker, copying
    # authentication, or creating any development state.
    profile = client_profile_target(args.client_profile)
    prior_codex = settings(root).get("codex") if (root / "development.json").exists() else None
    reject_linked_descendants(root, prior_codex)
    # Capture only the existing native login into a separate private CLI home.
    # Never copy conversations, settings, projects, MCP config or real sessions.
    original_auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if root.exists() and any(root.iterdir()) and not (root / "development.json").exists():
        raise RuntimeError("Refusing to use a nonempty unmarked directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (root / "development.json").exists():
        value = settings(root)
        if value["port"] != args.port:
            raise RuntimeError("Existing Idea Lab uses a different port; stop it before changing configuration")
    else:
        if not 15000 <= args.port <= 65535:
            raise RuntimeError("Choose a dedicated development port from 15000 to 65535")
        value = {"kind": MARKER, "root": str(root), "port": args.port,
                 "token": secrets.token_urlsafe(32), "codex": shutil.which("codex")}
        if not value["codex"]:
            raise RuntimeError("Native Codex CLI was not found for this development user")
        write_private(root / "development.json", json.dumps(value))
    if not owned_pid(root):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", value["port"])) == 0:
                raise RuntimeError("The development port is occupied by another process; it was not stopped")
        if not original_auth.is_file():
            raise RuntimeError("Native Codex login is unavailable; authenticate the CLI before using Idea Lab")
        destination = root / "home" / ".codex" / "auth.json"
        reject_symlinks(destination)
        if original_auth.resolve() == destination.resolve():
            raise RuntimeError("Development authentication must remain separate")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "wb") as target:
            info = os.fstat(target.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("Development authentication must be a separate regular file")
            os.ftruncate(target.fileno(), 0)
            target.write(original_auth.read_bytes())
    write_private(profile, json.dumps({"kind": MARKER, "serverUrl": f"http://127.0.0.1:{value['port']}",
                                       "accessToken": value["token"]}))
    print(json.dumps({"status": "prepared", "server_url": f"http://127.0.0.1:{value['port']}",
                      "running": owned_pid(root) is not None}))


def serve(root):
    value = settings(root)
    if owned_pid(root):
        raise RuntimeError("The isolated Idea Lab server is already running")
    reject_linked_descendants(root, value["codex"])
    home = root / "home"
    instance = "idea-lab"
    state = home / ".agentsdock-instances" / instance
    config = home / ".config" / "agents-server-instances" / instance
    install = home / ".local" / "share" / "agents-server-instances" / instance
    workspace = root / "workspace"
    directories = (state, config, install, workspace, root / "tmp", root / "runtime", root / "tmux")
    # Validate the entire setup before the first mkdir. In particular a linked
    # state/config root must never reach the full server's startup recovery.
    for path in (*directories, home / ".codex" / "auth.json", root / "server.pid"):
        reject_symlinks(path)
    for path in directories:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "America/Los_Angeles",
        "TMPDIR": str(root / "tmp"), "TMUX_TMPDIR": str(root / "tmux"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"), "XDG_RUNTIME_DIR": str(root / "runtime"),
        "PYTHONPATH": str(SERVER), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1", "CODEX_BIN": value["codex"], "CODEX_HOME": str(home / ".codex"),
        "AGENTS_SERVER_CONFIG_DIR": str(config), "AGENTS_SERVER_INSTALL_DIR": str(install),
        "AGENTSDOCK_STATE_DIR": str(state), "AGENTS_SERVER_INSTANCE": instance,
        "AGENTSDOCK_AGENT_TOKEN": value["token"], "AGENTSDOCK_AGENT_BIND": "127.0.0.1",
        "AGENTSDOCK_AGENT_PORT": str(value["port"]), "AGENTSDOCK_IDEA_LAB_DEV_ROOT": str(root),
    }
    write_private(root / "server.pid", str(os.getpid()))
    os.chdir(workspace)
    os.execve(sys.executable, [sys.executable, "-m", "uvicorn", "agent_server:app", "--host", "127.0.0.1",
                              "--port", str(value["port"]), "--no-access-log"], env)


def status(root):
    value = settings(root)
    if not owned_pid(root):
        print(json.dumps({"running": False}))
        return 1
    request = Request(f"http://127.0.0.1:{value['port']}/api/research/ideas",
                      headers={"X-AgentsDock-Token": value["token"]})
    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
        response.read()
        print(json.dumps({"running": response.status == 200, "port": value["port"]}))
    return 0


def stop(root):
    settings(root)
    pid = owned_pid(root)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if owned_pid(root) != pid:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Isolated server is still shutting down; no other process was stopped")
    print(json.dumps({"stopped": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "serve", "status", "stop"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", default=17851, type=int)
    parser.add_argument("--client-profile")
    args = parser.parse_args()
    root = args.root.absolute()
    if root.is_symlink() or root == Path.home() or root == Path("/"):
        parser.error("Use a separate, non-symlinked development directory")
    try:
        if args.operation == "prepare":
            if not args.client_profile:
                parser.error("prepare requires --client-profile")
            prepare(args, root)
        elif args.operation == "serve":
            serve(root)
        elif args.operation == "status":
            return status(root)
        else:
            stop(root)
    except (OSError, ValueError, RuntimeError) as error:
        # Do not print raw configuration or tokens.
        print(f"Idea Lab development: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
