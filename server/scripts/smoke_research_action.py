"""Exercise the actual research HTTP boundary with isolated synthetic state.

Run on Linux with the server's dependency environment and an explicit, new
--output directory. Starts only this checkout's agent_server:app on an inherited
loopback socket, with Uvicorn lifespan disabled. No provider is called, no live
configuration is loaded, and no existing service is restarted. This verifies
the real backend HTTP path, not native desktop UI or startup-worker behavior.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_REVISION = "cd7bded69fbf65cdcb2e33f2e7c395eef3222115"
QUOTE = "AgentsDock is the client. AgentsServer is the self-hosted backend."
PROTOCOL_IDS = ("literature-cache-v0.5", "records-v0.5", "execution-v0.5")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    data = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o600)
    return sha256(data)


class ServerProcess:
    def __init__(self, sandbox, output):
        self.sandbox = sandbox
        self.output = output
        self.token = secrets.token_urlsafe(32)
        self.process = None
        self.log = None
        self.starts = 0
        self.stopped = []
        self.opener = build_opener(ProxyHandler({}))
        self.home = sandbox / "home"
        self.state = self.home / ".agentsdock-instances" / "research-smoke"
        self.config = self.home / ".config" / "agents-server-instances" / "research-smoke"
        self.install = self.home / ".local" / "share" / "agents-server-instances" / "research-smoke"
        for name in ("home", "xdg-config", "xdg-data", "xdg-cache", "xdg-state", "runtime", "tmp"):
            (sandbox / name).mkdir(mode=0o700)
        for path in (self.state, self.config, self.install):
            path.mkdir(parents=True, mode=0o700)

    def start(self):
        require(self.process is None, "A harness server is already running")
        self.starts += 1
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        # Construct a fresh environment, never copy provider credentials or live
        # server configuration. Paths resolve inside this temporary Linux root.
        environment = {
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
            "TMPDIR": str(self.sandbox / "tmp"),
            "XDG_CONFIG_HOME": str(self.sandbox / "xdg-config"),
            "XDG_DATA_HOME": str(self.sandbox / "xdg-data"),
            "XDG_CACHE_HOME": str(self.sandbox / "xdg-cache"),
            "XDG_STATE_HOME": str(self.sandbox / "xdg-state"),
            "XDG_RUNTIME_DIR": str(self.sandbox / "runtime"),
            "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1",
            "AGENTS_SERVER_CONFIG_DIR": str(self.config),
            "AGENTS_SERVER_INSTALL_DIR": str(self.install),
            "AGENTSDOCK_STATE_DIR": str(self.state),
            "AGENTS_SERVER_INSTANCE": "research-smoke",
            "AGENTSDOCK_AGENT_TOKEN": self.token,
            "AGENTSDOCK_AGENT_BIND": "127.0.0.1",
            "AGENTSDOCK_AGENT_PORT": str(port),
        }
        self.log = (self.output / f"server-{self.starts}.log").open("xb")
        command = [sys.executable, "-m", "uvicorn", "agent_server:app",
                   "--fd", str(listener.fileno()), "--lifespan", "off",
                   "--no-access-log", "--no-proxy-headers", "--log-level", "warning"]
        try:
            self.process = subprocess.Popen(command, cwd=self.sandbox, env=environment,
                                            pass_fds=(listener.fileno(),), stdout=self.log,
                                            stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            listener.close()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            require(self.process.poll() is None,
                    f"Isolated server exited during startup; inspect server-{self.starts}.log")
            try:
                status, _, _ = self.request("GET", "/api/research/protocols/records-v0.5", auth=False)
                if status == 401:
                    return
                raise AssertionError(f"Readiness expected protected route, received HTTP {status}")
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.1)
        raise AssertionError("Isolated server did not become ready within 45 seconds")

    def stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.stopped.append({"launch": self.starts, "exit_code": process.returncode})
        if self.log is not None:
            self.log.close()
            self.log = None

    def request(self, method, path, payload=None, *, auth=True, headers=None):
        request_headers = {"Accept": "application/json"}
        if auth:
            request_headers["X-AgentsDock-Token"] = self.token
        if headers:
            request_headers.update(headers)
        data = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.base_url + path, data=data, headers=request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read()

    def json(self, method, path, payload=None, *, expected=200, **kwargs):
        status, headers, data = self.request(method, path, payload, **kwargs)
        require(status == expected, f"HTTP {method} expected {expected}, received {status}: {data[:400]!r}")
        return json.loads(data), headers


def exercise(output, reference, source_bytes, acceptance_digest, summary):
    with tempfile.TemporaryDirectory(prefix="agentsdock-research-smoke-") as temporary:
        server = ServerProcess(Path(temporary), output)
        try:
            server.start()
            auth_observations = []
            for label, method, path, auth, headers, expected in (
                ("no credentials", "POST", "/api/research/actions", False, {}, 401),
                ("query token only", "POST", "/api/research/actions?token=" + server.token, False, {}, 401),
                ("browser origin", "POST", "/api/research/actions", True, {"Origin": "https://example.invalid"}, 403),
                ("browser fetch metadata", "POST", "/api/research/actions", True, {"Sec-Fetch-Mode": "cors"}, 403),
                ("preflight", "OPTIONS", "/api/research/actions", True, {}, 403),
            ):
                status, _, _ = server.request(method, path, reference, auth=auth, headers=headers)
                require(status == expected, f"{label}: expected {expected}, received {status}")
                require(not (server.state / "research").exists(), "Rejected request initialized research storage")
                auth_observations.append({"case": label, "status": status, "research_storage_absent": True})
            save_json(output / "authorization.json", auth_observations)
            summary["checks"]["authorization_before_research_storage"] = True

            planned, headers = server.json("POST", "/api/research/actions", reference)
            save_json(output / "plan.json", planned)
            require(planned["status"] == "planned" and planned["result"] is None, "Action was not frozen before execution")
            require(planned["spec"]["goal"] == reference["goal"], "Goal/revision did not survive planning")
            require(headers.get("cache-control") == "no-store", "Action response should disable HTTP cache")
            require(planned["spec"]["source"]["digest"] == sha256(source_bytes), "Frozen source hash differs")
            require(planned["spec"]["required_protocols"] == list(PROTOCOL_IDS), "Protocol references changed")
            require("requirements" not in json.dumps(planned["spec"]), "Full protocol text was eagerly included")
            action_path = "/api/research/actions/" + planned["action_id"]
            server.json("POST", action_path + "/run", {"spec_hash": "stale"}, expected=409)
            after_stale, _ = server.json("GET", action_path)
            require(after_stale == planned, "Stale frozen specification changed action state")
            summary["checks"]["frozen_goal_spec_and_stale_rejection"] = True

            completed, _ = server.json("POST", action_path + "/run", {"spec_hash": planned["spec_hash"]})
            save_json(output / "completed.json", completed)
            result = completed["result"]
            require(completed["status"] == "completed", "Reference action did not complete")
            require(result["observation"]["exact_match_found"], "Known reference quote was not found")
            require(result["observation"]["match_count"] == source_bytes.decode().count(QUOTE), "Occurrence count differs from frozen source")
            for span in result["observation"]["spans"]:
                require(source_bytes[span["byte_start"]:span["byte_end"]].decode() == QUOTE, "Reported byte span is not the exact quote")
            require(result["qc"]["passed"] and result["source_support"] == "supported", "Mechanical reference QC did not pass")
            require(result["inference_validity"] == "cannot_determine", "Exact occurrence overclaimed inference validity")
            require(result["observation"]["is_fresh_replicate"] is False, "Deterministic check labeled a fresh replicate")
            require(not result["cache"]["parsed_source_reused"], "New isolated store unexpectedly reused a parse")
            require(all(result["cost"][key] == 0 for key in ("external_requests", "model_tokens", "monetary_cost")), "Reference action reports external work")
            summary["checks"]["exact_observation_qc_limits_and_costs"] = True

            artifact_dir = output / "artifacts"
            artifact_dir.mkdir(mode=0o700)
            artifacts = {"spec": completed["spec_hash"], "result": completed["result_digest"], **result["artifact_digests"]}
            for name, digest in artifacts.items():
                status, headers, body = server.request("GET", "/api/research/artifacts/" + digest)
                require(status == 200 and sha256(body) == digest, f"Artifact {name} digest mismatch")
                require(headers.get("x-content-sha256") == digest, "Artifact digest header differs")
                if name == "source":
                    require(body == source_bytes, "Retrieved source differs from frozen git reference")
                (artifact_dir / f"{name}-{digest}.bin").write_bytes(body)
            save_json(output / "artifact-index.json", artifacts)
            summary["checks"]["all_artifact_digests_over_http"] = True

            protocol, _ = server.json("GET", "/api/research/protocols/literature-cache-v0.5")
            require(protocol["representation"] == "implementation_summary", "Protocol summary must be labeled")
            save_json(output / "selected-protocol.json", protocol)
            summary["checks"]["one_protocol_loaded_on_demand"] = True

            server.stop()
            server.start()
            replayed, _ = server.json("POST", action_path + "/run", {"spec_hash": planned["spec_hash"]})
            retrieved, _ = server.json("GET", action_path)
            require(replayed == completed == retrieved, "Process restart or repeated run changed durable result/events")
            save_json(output / "replayed-after-restart.json", replayed)
            repeat_plan, _ = server.json("POST", "/api/research/actions", reference)
            require(repeat_plan == completed, "Identical idempotency request did not return original completed action")
            changed_request = {**reference, "quote": "A different quote under the same idempotency key."}
            conflict, _ = server.json("POST", "/api/research/actions", changed_request, expected=409)
            save_json(output / "idempotency-conflict.json", conflict)
            require([event["type"] for event in replayed["events"]].count("execution_started") == 1, "Duplicate logical execution recorded")
            summary["checks"]["process_restart_idempotency_and_conflict"] = True

            variants = []
            for name, text, coverage, missing, expected_reuse, support in (
                ("same-source", source_bytes.decode(), "full_text", [], True, "supported"),
                ("changed-source", source_bytes.decode() + "\nSynthetic fixture revision for cache invalidation.\n", "full_text", [], False, "supported"),
                ("coverage-gap", "Only this deliberately incomplete excerpt was supplied.\n", "excerpt", ["remaining README sections"], False, "cannot_determine"),
            ):
                request = copy.deepcopy(reference)
                request["idempotency_key"] = "reference-action-" + name
                request["source"].update(text=text, coverage=coverage, missing_sections=missing)
                save_json(output / (name + "-request.json"), request)
                plan, _ = server.json("POST", "/api/research/actions", request)
                save_json(output / (name + "-plan.json"), plan)
                done, _ = server.json("POST", "/api/research/actions/" + plan["action_id"] + "/run", {"spec_hash": plan["spec_hash"]})
                save_json(output / (name + "-completed.json"), done)
                require(done["status"] == "completed" and done["result"]["qc"]["passed"],
                        f"{name}: missing source support was confused with execution/QC failure")
                require(done["result"]["cache"]["parsed_source_reused"] == expected_reuse, f"{name}: incorrect parse reuse")
                require(done["result"]["source_support"] == support, f"{name}: incorrect support scope")
                require(done["result"]["inference_validity"] == "cannot_determine", f"{name}: inferred beyond exact text")
                require(done["spec"]["goal"] == reference["goal"], f"{name}: goal/revision lost")
                require(done["result"]["observation"]["coverage"] == coverage and done["result"]["observation"]["missing_sections"] == missing,
                        f"{name}: coverage gaps not visible")
                if name == "changed-source":
                    require(done["spec"]["source"]["digest"] != planned["spec"]["source"]["digest"], "Source version not invalidated")
                variants.append({"case": name, "action_id": done["action_id"], "parsed_source_reused": expected_reuse, "source_support": support})
            still_original, _ = server.json("GET", action_path)
            require(still_original == completed, "New source version overwrote original history")
            summary["checks"]["parse_reuse_version_invalidation_visible_gaps_goal_retention"] = True
            summary.update(action_id=completed["action_id"], spec_hash=completed["spec_hash"], result_digest=completed["result_digest"], variants=variants,
                           acceptance_spec_sha256=acceptance_digest, status="passed")
        finally:
            server.stop()
            summary["child_processes"] = server.stopped
            summary["all_started_processes_stopped"] = len(server.stopped) == server.starts
            database = server.state / "research" / "research.sqlite3"
            if database.is_file():
                destination = output / "research.sqlite3"
                # Both child processes have exited; SQLite backup also handles
                # any rollback journal without exporting unrelated server state.
                with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
                    source.backup(target)
                if summary["status"] == "passed":
                    restored_root = Path(temporary) / "restored-research"
                    restored_root.mkdir(mode=0o700)
                    with sqlite3.connect(destination) as backup, sqlite3.connect(restored_root / "research.sqlite3") as restored:
                        backup.backup(restored)
                    # Only the standalone core is imported for this offline
                    # readback; no second server/startup worker is involved.
                    sys.path.insert(0, str(ROOT))
                    try:
                        from research_actions import ResearchActionStore
                        restored_store = ResearchActionStore(restored_root)
                        require(restored_store.get(completed["action_id"]) == completed,
                                "Restored SQLite backup changed the completed action")
                        for digest in artifacts.values():
                            require(sha256(restored_store.artifact(digest)) == digest,
                                    "Restored SQLite backup changed an artifact")
                    finally:
                        sys.path.pop(0)
                    save_json(output / "backup-restore.json", {
                        "completed_action_identical": True,
                        "artifact_digests_verified": artifacts,
                        "method": "SQLite online backup API, restore into new isolated root, core readback",
                    })
                    summary["checks"]["sqlite_backup_restored_with_same_action_and_artifacts"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private local evidence directory; must not already exist")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run in Linux/WSL; this harness requires inherited Unix sockets and isolated Linux temporary directories")
    if not (ROOT / "research_actions.py").is_file() or not (ROOT / "research_action_routes.py").is_file():
        parser.error("Research core/routes are not present yet; finish the source slice before running acceptance")
    if "app.include_router(create_research_action_router(" not in (ROOT / "agent_server.py").read_text():
        parser.error("Research routes are not integrated into agent_server:app yet")
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("--output must be a new directory; previous acceptance evidence will not be overwritten")
    # The Windows checkout can be owned by a different uid in WSL. Trust only
    # this invocation's exact source path; never alter global Git configuration.
    try:
        source_bytes = subprocess.check_output([
            "git", "-c", "safe.directory=" + str(ROOT.parent), "-C", str(ROOT.parent),
            "show", REFERENCE_REVISION + ":README.md",
        ], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        parser.error("Could not read the pinned README git object; confirm source revision and checkout access")
    require(QUOTE in source_bytes.decode("utf-8"), "Frozen reference quote does not occur in the requested git revision")
    output.mkdir(parents=True, mode=0o700)
    (output / "source-frozen.md").write_bytes(source_bytes)
    reference = {
        "idempotency_key": "reference-action-readme-v1",
        "goal": {"id": "agentsdock-reference-action", "revision": 1,
                 "question": "Does the frozen AgentsDock README contain the exact client/backend sentence?",
                 "completion_criterion": "Report exact supplied-text spans, immutable artifacts, coverage and QC; leave scientific and inference validity undetermined."},
        "source": {"uri": f"https://github.com/ZhengyiLuo/AgentsDock/blob/{REFERENCE_REVISION}/README.md",
                   "text": source_bytes.decode("utf-8"), "coverage": "full_text", "missing_sections": []},
        "quote": QUOTE,
    }
    save_json(output / "request.json", reference)
    implementation = {
        name: sha256((ROOT / name).read_bytes())
        for name in ("agent_server.py", "research_actions.py", "research_action_routes.py", "scripts/smoke_research_action.py")
    }
    save_json(output / "implementation-frozen.json", implementation)
    acceptance = {
        "frozen_at": datetime.now(timezone.utc).isoformat(), "reference_git_revision": REFERENCE_REVISION,
        "source_sha256": sha256(source_bytes), "quote": QUOTE,
        "goal": reference["goal"], "method": "exact supplied-text occurrence only",
        "checks": ["no credential/query credential/browser/preflight rejected before research storage",
                   "freeze goal/spec/source before execution; stale spec rejected without mutation",
                   "expected quote spans and all artifact hashes verified over real HTTP",
                   "mechanical QC kept separate from scientific and inference validity",
                   "protocol referenced in spec and one selected summary retrieved on demand",
                   "new Uvicorn process reuses same isolated state without duplicate action/event/cost",
                   "same idempotency key changed payload conflicts; same source new key reuses parser",
                   "changed source invalidates parse without overwriting old history",
                   "missing quote in incomplete excerpt yields cannot_determine and visible gaps",
                   "SQLite backup restored into a fresh root retains identical action and artifact hashes"],
        "boundary": "actual agent_server:app and HTTP authorization, with lifespan off",
        "excluded": ["startup workers", "native desktop UI", "provider integration", "scientific performance evaluation", "external job recovery"],
    }
    acceptance_digest = save_json(output / "acceptance-frozen.json", acceptance)
    summary = {"status": "failed", "checks": {}, "test_surface": "real backend HTTP, isolated synthetic state",
               "native_desktop_ui_exercised": False, "lifespan": "off", "startup_workers_exercised": False,
               "provider_calls": 0, "base_git_revision": REFERENCE_REVISION,
               "implementation_file_sha256": implementation}
    try:
        exercise(output, reference, source_bytes, acceptance_digest, summary)
        require(all(sha256((ROOT / name).read_bytes()) == digest for name, digest in implementation.items()),
                "Implementation changed while acceptance ran; repeat against a stable source snapshot")
    except Exception as error:
        summary["status"] = "failed"
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        save_json(output / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "checks_passed": len(summary["checks"]),
                      "all_started_processes_stopped": summary.get("all_started_processes_stopped", True),
                      "evidence": str(output), **({"error": summary["error"]} if "error" in summary else {})}, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
