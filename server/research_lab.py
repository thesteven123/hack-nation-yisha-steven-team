"""Service-owned bounded research campaigns with pluggable local adapters.

No external execution occurs in this module. Its transaction/replay guarantee
applies only to bounded deterministic local calculations, not external jobs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
import statistics
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from research_actions import ResearchActionStore
from research_hypotheses import HypothesisError, freeze_set
from research_branch_controller import BranchController

SCHEMA_VERSION = 1
MAX_DETAIL_BYTES = 3 * 1024 * 1024
PROTOCOLS = ["planning-v0.5", "records-v0.5", "execution-v0.5", "analysis-review-v0.5"]
PROTOCOL_REQUIREMENTS = {
    "planning-v0.5": ["Freeze question, input, method, QC, interpretation and resource rules before execution", "Do not change scientific criteria in response to observed output"],
    "records-v0.5": ["Preserve immutable input and output references", "Separate observation, source support and inference validity"],
    "execution-v0.5": ["Reserve identity and quota before work", "This executor performs bounded local computation only; no external dispatch or independent replicate"],
    "analysis-review-v0.5": ["Failed execution is not negative evidence", "QC-invalid output is excluded from support", "Retain uncertainty and stop without requiring a positive finding"],
    "literature-cache-v0.5": ["Inspect exact supplied source spans and disclose gaps", "An absent quote does not establish scientific absence", "Cached input is not an independent observation"],
}


class LabError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")
        return encoded
    except (ValueError, TypeError, UnicodeError) as exc:
        raise LabError("invalid_request", "A finite JSON value is required") from exc


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def _environment():
    root = Path(__file__).resolve().parent
    hashes = {}
    for name in ("research_lab.py", "research_actions.py", "research_hypotheses.py", "research_branches.py", "research_branch_controller.py", "uv.lock", "pyproject.toml"):
        path = root / name
        if not path.is_file():
            hashes[name] = None
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            raise LabError("integrity_error", "A pinned execution file exceeds its size bound")
        hashes[name] = _digest(path.read_bytes())
    return {"python": sys.version, "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag, "files_sha256": hashes,
            "scope": "Source/lock fingerprints plus actual Python runtime; no credential or environment-variable capture",
            "missing_files": [name for name, value in hashes.items() if value is None]}


# Fingerprint the code that this process loaded. A file replaced on disk must
# never be reported as the version of still-running old Python functions.
LOADED_EXECUTION_ENVIRONMENT = _environment()


def _loaded_environment():
    if _environment() != LOADED_EXECUTION_ENVIRONMENT:
        raise LabError("environment_changed", "Execution source, lockfile, or Python changed after import; restart the development service before freezing or executing a plan")
    return copy.deepcopy(LOADED_EXECUTION_ENVIRONMENT)


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise LabError("invalid_request", "Unexpected or missing request fields")
    return value


def _text(value, limit=8000, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise LabError("invalid_request", "A nonempty text field is required")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise LabError("invalid_request", "Text must be valid UTF-8") from exc
    if size > limit:
        raise LabError("invalid_request", "Text exceeds its byte limit")
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise LabError("invalid_request", f"Integer must be between {minimum} and {maximum}")
    return value


def _number(value, *, minimum=-1e12):
    if type(value) not in (int, float) or not minimum <= value <= 1e12 or not math.isfinite(value):
        raise LabError("invalid_request", "Numeric values must be finite and within the declared range")
    return value


def _gap_markers(provenance):
    values = []
    if isinstance(provenance, dict):
        for entry in (provenance, provenance.get("original_provenance")):
            if not isinstance(entry, dict):
                continue
            markers = entry.get("gap_markers", [])
            if not isinstance(markers, list) or len(markers) > 8:
                raise LabError("invalid_request", "At most eight explicit omission markers are supported")
            for value in [entry.get("gap_marker"), *markers]:
                if value is None:
                    continue
                value = _text(value, 256)
                if value not in values:
                    values.append(value)
        if len(values) > 8:
            raise LabError("invalid_request", "At most eight distinct omission markers are supported")
    return values


def _source_observation(text, quote, markers):
    raw = ResearchActionStore._observe(text, quote)
    raw["packet_match_count"] = raw["match_count"]
    raw["rejected_gap_matches"] = 0
    if not markers or not any(marker in text for marker in markers):
        return raw
    # Mark omitted ranges once, then check each KMP match in O(1). Building a
    # separate prefix table per segment would be quadratic for adversarial gaps.
    blocked = bytearray(len(text))
    for marker in markers:
        start = 0
        while (start := text.find(marker, start)) >= 0:
            blocked[start:start + len(marker)] = b"\x01" * len(marker)
            start += len(marker)
    totals = [0]
    for value in blocked:
        totals.append(totals[-1] + value)
    prefix = [0] * len(quote)
    matched = 0
    for i in range(1, len(quote)):
        while matched and quote[i] != quote[matched]:
            matched = prefix[matched - 1]
        if quote[i] == quote[matched]:
            matched += 1
        prefix[i] = matched
    matched, count, spans = 0, 0, []
    for end, character in enumerate(text, 1):
        while matched and character != quote[matched]:
            matched = prefix[matched - 1]
        if character == quote[matched]:
            matched += 1
        if matched == len(quote):
            start = end - len(quote)
            if totals[end] == totals[start]:
                count += 1
                if len(spans) < 100:
                    spans.append({"char_start": start, "char_end": end, "byte_start": len(text[:start].encode("utf-8")),
                                  "byte_end": len(text[:end].encode("utf-8")), "line_start": 1 + text.count("\n", 0, start),
                                  "line_end": 1 + text.count("\n", 0, end - 1)})
            matched = prefix[matched - 1]
    raw.update(exact_match_found=count > 0, match_count=count, spans=spans, spans_truncated=count > len(spans),
               rejected_gap_matches=raw["packet_match_count"] - count)
    return raw


class DomainAdapter(Protocol):
    """Adapters supply science-specific rules; the store owns identity and state.

    Implementations must be bounded, deterministic, side-effect-free local code.
    An external adapter requires a different executor with durable submission
    and reconciliation; it cannot opt into this transaction executor.
    """
    manifest: dict

    def validate(self, inputs: dict) -> dict: ...
    def summary(self, inputs: dict) -> dict: ...
    def candidates(self, inputs: dict, previous: list, next_method: str | None) -> list[dict]: ...
    def execute(self, inputs: dict, spec: dict) -> dict: ...
    def quality(self, observation: dict, spec: dict) -> dict: ...
    def analyze(self, observation: dict, qc: dict, spec: dict) -> dict: ...
    def review(self, analysis: dict, qc: dict, observation: dict, spec: dict, previous: list) -> dict: ...


def _manifest(identifier, title, methods, limitations, input_schema):
    return {"id": identifier, "version": identifier + "/1", "title": title,
            "task_family": identifier, "methods": methods, "input_schema": input_schema,
            "observation_schema": {"type": "object", "required": ["kind", "data", "coverage"]},
            "applicability": ["Explicitly supplied fixed inputs", "Bounded local deterministic analysis"],
            "limitations": limitations, "required_protocols": list(PROTOCOLS),
            "execution": "local_deterministic", "fresh_replicates": False}


def _candidate(method, title, parameters, learning, qc, interpretation):
    return {"method": method, "title": title, "parameters": parameters,
            "expected_learning": learning, "qc_rules": qc, "interpretation_rules": interpretation}


def _qc(checks):
    return {"passed": all(c[1] for c in checks),
            "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in checks]}


def _review(next_action, reason, method=None, uncertainties=(), alternatives=()):
    return {"next_action": next_action, "reason": reason, "next_method": method,
            "uncertainties": list(uncertainties), "alternatives": list(alternatives), "input_refs": []}


class SourceEvidenceAdapter:
    manifest = _manifest("source_evidence", "Supplied source evidence", ["exact_quote", "source_context"],
        ["Checks supplied text only; does not search the network", "Quote occurrence does not validate a scientific claim",
         "Context keywords are inspection aids, not semantic or inference judgments", "Coverage gaps remain unresolved"],
        {"type": "object", "required": ["quote", "sources"], "max_sources": 5, "max_total_text_bytes": 153600})
    manifest["required_protocols"].append("literature-cache-v0.5")

    def validate(self, inputs):
        _fields(inputs, {"quote", "sources"})
        quote = _text(inputs["quote"], 16384)
        values = inputs["sources"]
        if not isinstance(values, list) or not 1 <= len(values) <= 5:
            raise LabError("invalid_request", "Provide one to five supplied sources")
        sources = []
        total = 0
        for item in values:
            _fields(item, {"id", "title", "uri", "text", "coverage", "missing_sections"}, {"provenance"})
            if item["coverage"] not in ("full_text", "excerpt"):
                raise LabError("invalid_request", "Coverage must be full_text or excerpt")
            missing = item["missing_sections"]
            if not isinstance(missing, list) or len(missing) > 30:
                raise LabError("invalid_request", "Missing sections must be a bounded list")
            missing = [_text(x, 256) for x in missing]
            if item["coverage"] == "full_text" and missing:
                raise LabError("invalid_request", "Full text cannot declare missing sections")
            source = {"id": _text(item["id"], 128), "title": _text(item["title"], 1000),
                      "uri": _text(item["uri"], 4096), "text": _text(item["text"], 153600, empty=True),
                      "coverage": item["coverage"], "missing_sections": missing}
            total += len(source["text"].encode("utf-8"))
            if "provenance" in item:
                if not isinstance(item["provenance"], dict) or len(_json(item["provenance"]).encode("utf-8")) > 12000:
                    raise LabError("invalid_request", "Source provenance must be a bounded JSON object")
                source["provenance"] = copy.deepcopy(item["provenance"])
                _gap_markers(source["provenance"])
            sources.append(source)
        if total > 153600 or len({s["id"] for s in sources}) != len(sources):
            raise LabError("invalid_request", "Sources require unique IDs and at most 150 KiB total text")
        return {"quote": quote, "sources": sources}

    def summary(self, inputs):
        return {"quote": inputs["quote"], "sources": [{k: s[k] for k in ("id", "title", "uri", "coverage", "missing_sections")}
                                                       for s in inputs["sources"]]}

    def candidates(self, inputs, previous, next_method):
        tested = {r["plan"]["selected_action_id"]: r for r in previous}
        tested_sources = {r["observation"]["data"].get("source_id") for r in tested.values()
                          if r["observation"]["kind"] == "exact_quote"}
        if next_method == "source_context" and previous:
            source_ids = [previous[-1]["observation"]["data"]["source_id"]]
            method = "source_context"
        else:
            source_ids = [s["id"] for s in inputs["sources"] if s["id"] not in tested_sources]
            method = "exact_quote"
        return [_candidate(method, ("Inspect surrounding context: " if method == "source_context" else "Check exact quote: ") + s["title"],
            {"source_id": s["id"], "context_characters": 250},
            "Locate exact supplied evidence and expose missing context; assess no scientific implication",
            ["Input content digest unchanged", "UTF-8 source parsed completely", "Returned spans match the frozen quote"],
            ["A match supports only exact quote occurrence", "No match is unresolved, never refutation", "Inference validity remains cannot_determine"])
            for s in inputs["sources"] if s["id"] in source_ids]

    def execute(self, inputs, spec):
        source = next(s for s in inputs["sources"] if s["id"] == spec["parameters"]["source_id"])
        data = _source_observation(source["text"], inputs["quote"], _gap_markers(source.get("provenance")))
        data.update(source_id=source["id"], source_hash=_digest(source["text"].encode("utf-8")), quote=inputs["quote"])
        if spec["method"] == "source_context":
            contexts = []
            for span in data["spans"][:5]:
                start, end = max(0, span["char_start"] - 250), min(len(source["text"]), span["char_end"] + 250)
                contexts.append({"char_start": start, "char_end": end, "text": source["text"][start:end]})
            data["contexts"] = contexts
            text = " ".join(c["text"] for c in contexts).lower()
            data["inspection_terms"] = [term for term in ("only", "however", "assuming", "limited", "not", "may") if term in text]
        return {"kind": spec["method"], "data": data,
                "coverage": {"kind": source["coverage"], "missing_sections": source["missing_sections"],
                             "source_characters": len(source["text"]), "context_occurrences_returned": min(5, data["match_count"]) if spec["method"] == "source_context" else 0}}

    def quality(self, observation, spec):
        return _qc([("bounded_parser", True, "All supplied UTF-8 text scanned; returned locations bounded to 100"),
                    ("span_integrity", all(s["char_end"] > s["char_start"] for s in observation["data"]["spans"]),
                     "Exact matching uses the frozen quote; no-match is a valid observation")])

    def analyze(self, observation, qc, spec):
        count = observation["data"]["match_count"]
        return {"claim": f"The exact quote occurs {count} time(s) in supplied source {observation['data']['source_id']}.",
                "source_support": "supported" if count and qc["passed"] else "cannot_determine",
                "inference_validity": "cannot_determine",
                "limitations": list(self.manifest["limitations"]) + observation["coverage"]["missing_sections"], "input_refs": []}

    def review(self, analysis, qc, observation, spec, previous):
        if not qc["passed"]:
            return _review("needs_input", "Quality checks failed; the result is excluded from support", uncertainties=["Input or method repair required"])
        if spec["method"] == "source_context":
            return _review("stop", "Quote and bounded surrounding context have been inspected", uncertainties=analysis["limitations"],
                           alternatives=["Request additional literature through the Idea/Literature workspace", "Ask a domain expert to evaluate the inference"])
        if observation["data"]["exact_match_found"]:
            return _review("continue", "The quote was found; inspect surrounding qualifications before any interpretation", "source_context",
                           uncertainties=["Scientific meaning and inference remain unassessed"], alternatives=["Stop with occurrence-only evidence"])
        return _review("continue", "No match in this supplied source; check the next supplied source if available", "exact_quote",
                       uncertainties=["Missing accessible text does not establish absence"], alternatives=["Obtain more source material", "Stop unresolved"])


class PairedNumericAdapter:
    manifest = _manifest("paired_numeric", "Paired numerical analysis", ["paired_summary", "leave_one_out", "extreme_sensitivity"],
        ["Descriptive analysis of supplied pairs; no causal identification", "Normal approximation is not a calibrated interval without sampling assumptions",
         "Sensitivity uses the same fixed dataset, not independent replication", "No hidden test set or population representativeness is assumed"],
        {"type": "object", "required": ["baseline", "treatment", "unit", "minimum_effect"], "max_pairs": 10000})

    def validate(self, inputs):
        _fields(inputs, {"baseline", "treatment", "unit", "minimum_effect"})
        a, b = inputs["baseline"], inputs["treatment"]
        if not isinstance(a, list) or not isinstance(b, list) or not 2 <= len(a) <= 10000 or len(a) != len(b):
            raise LabError("invalid_request", "Provide two equal arrays of 2–10,000 paired values")
        return {"baseline": [_number(x) for x in a], "treatment": [_number(x) for x in b],
                "unit": _text(inputs["unit"], 128), "minimum_effect": _number(inputs["minimum_effect"], minimum=0)}

    def summary(self, inputs):
        return {"pairs": len(inputs["baseline"]), "unit": inputs["unit"], "minimum_effect": inputs["minimum_effect"]}

    def candidates(self, inputs, previous, next_method):
        methods = [next_method] if next_method else ["paired_summary", "extreme_sensitivity"]
        return [_candidate(method, {"paired_summary": "Estimate paired differences", "leave_one_out": "Check leave-one-pair-out stability",
                                   "extreme_sensitivity": "Check extreme-pair sensitivity"}[method],
                {"minimum_effect": inputs["minimum_effect"], "uncertainty_multiplier": 1.96},
                "Compare a descriptive effect against the frozen minimum and expose dependence on observations",
                ["Equal finite paired arrays", "At least four pairs for support eligibility", "No independent replicate claimed"],
                ["Threshold frozen before seeing output", "Summary lower bound above threshold permits descriptive support only",
                 "Sensitivity is exploratory on the same data", "No causal or population claim without additional assumptions"])
                for method in methods]

    def execute(self, inputs, spec):
        differences = [y - x for x, y in zip(inputs["baseline"], inputs["treatment"])]
        n = len(differences)
        total = math.fsum(differences)
        mean = total / n
        se = statistics.stdev(differences) / math.sqrt(n)
        low, high = mean - 1.96 * se, mean + 1.96 * se
        data = {"pairs": n, "mean_difference": mean, "standard_error": se, "descriptive_interval": [low, high],
                "minimum_effect": spec["parameters"]["minimum_effect"], "unit": inputs["unit"],
                "positive_pairs": sum(x > 0 for x in differences), "zero_pairs": sum(x == 0 for x in differences),
                "descriptive_fact": f"Observed mean treatment-minus-baseline: {mean:.8g} {inputs['unit']} from {n} supplied pairs",
                "criterion_result": "lower_bound_exceeds_threshold" if low > inputs["minimum_effect"] else "upper_bound_below_threshold" if high < inputs["minimum_effect"] else "interval_overlaps_threshold"}
        if spec["method"] == "leave_one_out":
            values = [(total - x) / (n - 1) for x in differences]
            data.update(sensitivity_range=[min(values), max(values)],
                        all_above_threshold=all(x > inputs["minimum_effect"] for x in values), omissions=n)
        elif spec["method"] == "extreme_sensitivity":
            index = max(range(n), key=lambda i: abs(differences[i] - mean))
            revised = (total - differences[index]) / (n - 1)
            data.update(omitted_pair_index=index, omitted_difference=differences[index], sensitivity_mean=revised,
                        threshold_conclusion_changed=(mean > inputs["minimum_effect"]) != (revised > inputs["minimum_effect"]))
        return {"kind": spec["method"], "data": data, "coverage": {"kind": "supplied_pairs", "pairs_analyzed": n,
                "independent_sampling_verified": False, "missing_sections": ["Sampling/assignment mechanism", "Independent replication"]}}

    def quality(self, observation, spec):
        return _qc([("finite_summary", all(math.isfinite(x) for x in observation["data"]["descriptive_interval"]), "All aggregates must be finite"),
                    ("minimum_pairs", observation["data"]["pairs"] >= 4, "At least four pairs are required for support eligibility; this is not a power calculation")])

    def analyze(self, observation, qc, spec):
        d = observation["data"]
        if not qc["passed"]:
            label = "cannot_determine"
        elif d["descriptive_interval"][0] > d["minimum_effect"]:
            label = "supported"
        elif d["descriptive_interval"][1] < d["minimum_effect"]:
            label = "contradicted"
        else:
            label = "cannot_determine"
        if spec["method"] != "paired_summary":
            label = "cannot_determine"  # Sensitivity is not fresh confirmatory evidence.
        return {"claim": f"The mean treatment-minus-baseline in the supplied paired dataset exceeds {d['minimum_effect']:.8g} {d['unit']} (tested under the frozen descriptive interval rule).",
                "source_support": label, "inference_validity": "cannot_determine", "limitations": list(self.manifest["limitations"]), "input_refs": []}

    def review(self, analysis, qc, observation, spec, previous):
        if not qc["passed"]:
            return _review("needs_input", "Completed computation failed support-eligibility QC; no support claim accepted", uncertainties=["Supply at least four valid pairs"])
        if spec["method"] != "paired_summary":
            return _review("stop", "The selected sensitivity analysis is complete; independent evidence is still required for confirmation",
                           uncertainties=analysis["limitations"], alternatives=["Collect independently obtained data", "Revise the hypothesis or stop unresolved"])
        d = observation["data"]
        if d["descriptive_interval"][0] > d["minimum_effect"]:
            return _review("continue", "The descriptive lower bound exceeds the threshold; test whether each leave-one-pair-out mean retains that direction",
                           "leave_one_out", analysis["limitations"], ["Inspect extreme-pair sensitivity", "Stop with bounded descriptive findings"])
        return _review("continue", "The threshold is not clearly exceeded; inspect dependence on the most extreme pair before revising the hypothesis",
                       "extreme_sensitivity", analysis["limitations"], ["Collect more independently obtained pairs", "Stop unresolved"])


DEFAULT_ADAPTERS = {a.manifest["id"]: a for a in (SourceEvidenceAdapter(), PairedNumericAdapter())}


class ResearchLabStore(BranchController):
    def __init__(self, root: Path, *, adapters=None):
        self.root = Path(root).absolute()
        self.path = self.root / "lab.sqlite3"
        self.adapters = dict(DEFAULT_ADAPTERS if adapters is None else adapters)
        for key, adapter in self.adapters.items():
            if key != adapter.manifest["id"] or adapter.manifest["execution"] != "local_deterministic":
                raise LabError("unsupported_adapter", "This executor admits bounded local deterministic adapters only")
        try:
            self._safe_path()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._db(write=True, initialize=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise LabError("unsupported_schema", "Unsupported Research Lab schema")
                if version == 0:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise LabError("unsupported_schema", "Unversioned nonempty Research Lab store")
                    for sql in (
                        "CREATE TABLE campaigns(id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
                        "CREATE TABLE artifacts(digest TEXT PRIMARY KEY,data BLOB NOT NULL)",
                        "CREATE TABLE artifact_links(campaign_id TEXT NOT NULL REFERENCES campaigns(id),digest TEXT NOT NULL REFERENCES artifacts(digest),PRIMARY KEY(campaign_id,digest))",
                        "CREATE TABLE versions(campaign_id TEXT NOT NULL REFERENCES campaigns(id),revision INTEGER NOT NULL,at TEXT NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(campaign_id,revision))",
                        "CREATE TABLE mutations(scope TEXT NOT NULL,key TEXT NOT NULL,request_hash TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(scope,key))",
                        "CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,campaign_id TEXT NOT NULL REFERENCES campaigns(id),revision INTEGER NOT NULL,type TEXT NOT NULL,at TEXT NOT NULL,data TEXT NOT NULL)",
                    ):
                        db.execute(sql)
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.path.chmod(0o600)
        except OSError as exc:
            raise LabError("storage_error", "Could not initialize private Research Lab state") from exc

    def _safe_path(self):
        if self.root.is_symlink() or self.path.is_symlink():
            raise LabError("storage_error", "Research Lab state cannot use symlinks")

    @contextmanager
    def _db(self, *, write=False, initialize=False):
        db = None
        try:
            self._safe_path()
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if not initialize and db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise LabError("unsupported_schema", "Unsupported Research Lab schema")
            yield db
            db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise LabError("storage_error", "Research Lab storage failed; uncommitted changes were rolled back") from exc
        finally:
            if db is not None:
                db.close()

    def capabilities(self):
        return {"schema_version": SCHEMA_VERSION, "adapters": [copy.deepcopy(a.manifest) for a in self.adapters.values()],
                "execution": {"local_deterministic": True, "external_jobs": False, "arbitrary_code": False,
                              "network_search": False, "independent_replicates": False, "automatic_model_planning": False,
                              "recovery": "Atomic rollback or replay of committed local computation; no external submission",
                              "literature_entry": "/api/research/ideas", "max_actions": 12, "max_rounds": 6}}

    @staticmethod
    def _load(db, campaign_id, *, include_deleted=False):
        row = db.execute("SELECT payload FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise LabError("not_found", "Research campaign was not found")
        item = json.loads(row[0])
        if item.get("deleted_at") and not include_deleted:
            raise LabError("deleted", "This research is in Trash. Restore it before opening or continuing it.")
        return item

    def _require_idle_models(self, campaign_id):
        # Admission and publication hold the Lab write lock before accessing
        # model jobs. The trash transaction uses the same Lab -> jobs order.
        path = self.root.parent / "model-jobs" / "model-jobs.sqlite3"
        if not path.exists():
            return
        if path.is_symlink() or path.parent.is_symlink():
            raise LabError("storage_error", "Cannot verify the native role state.")
        jobs = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            if jobs.execute("SELECT 1 FROM jobs WHERE campaign_id=? AND json_extract(payload,'$.status')='running' LIMIT 1", (campaign_id,)).fetchone():
                raise LabError("busy", "Stop the active native role before deleting this research.")
        finally:
            jobs.close()

    def set_deleted(self, campaign_id, request, deleted):
        _fields(request, {"expected_revision", "idempotency_key"})
        _integer(request["expected_revision"], 1, 2147483647)
        operation = "deleted" if deleted else "restored"
        with self._db(write=True) as db:
            item = self._load(db, campaign_id, include_deleted=True)
            replay, fingerprint = self._replay(db, campaign_id, request["idempotency_key"], {"operation": operation, **request})
            if replay is not None:
                return self.project(replay)
            if item["revision"] != request["expected_revision"]:
                raise LabError("revision_conflict", "The campaign changed; reload before deleting or restoring it")
            self._require_idle_models(campaign_id)
            if bool(item.get("deleted_at")) != deleted:
                item["revision"] += 1
                item["deleted_at"] = _now() if deleted else None
                if item.get("branch_set"):
                    item["branch_set"]["authority_epoch"] += 1
                self._save(db, item, operation)
            return self._remember(db, campaign_id, request["idempotency_key"], fingerprint, item)

    @staticmethod
    def _read_artifact(db, digest):
        row = db.execute("SELECT data FROM artifacts WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise LabError("not_found", "Research artifact was not found")
        data = bytes(row[0])
        if _digest(data) != digest:
            raise LabError("integrity_error", "Research artifact hash does not match its content")
        return json.loads(data)

    def _artifact(self, db, campaign_id, value):
        data = _json(value).encode("utf-8")
        digest = _digest(data)
        db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?)", (digest, data))
        self._read_artifact(db, digest)
        db.execute("INSERT OR IGNORE INTO artifact_links VALUES (?,?)", (campaign_id, digest))
        return digest

    @staticmethod
    def _replay(db, scope, key, request):
        _text(key, 128)
        fingerprint = _digest(_json(request).encode("utf-8"))
        row = db.execute("SELECT request_hash,response FROM mutations WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row is not None:
            if row[0] != fingerprint:
                raise LabError("idempotency_conflict", "This idempotency key belongs to a different request")
            return json.loads(row[1]), fingerprint
        return None, fingerprint

    def _save(self, db, item, event, data=None):
        item["updated_at"] = _now()
        item["budget"]["remaining_actions"] = item["budget"]["max_actions"] - item["budget"]["used_actions"] - item["budget"]["reserved_actions"]
        entry = {"id": _id("event"), "type": event, "at": item["updated_at"], "revision": item["revision"], "data": data or {}}
        cursor = db.execute("INSERT INTO events(id,campaign_id,revision,type,at,data) VALUES (?,?,?,?,?,?)",
            (entry["id"], item["id"], item["revision"], event, entry["at"], _json(entry["data"])))
        entry["seq"] = cursor.lastrowid
        entry["artifact"] = self._artifact(db, item["id"], entry)
        item["events"].append(entry)
        self._compact_records(item)
        for record in item["rounds"]:
            if "artifact" not in record:
                record["artifact"] = self._artifact(db, item["id"], record)
        payload = _json(item)
        db.execute("UPDATE campaigns SET payload=? WHERE id=?", (payload, item["id"]))
        db.execute("INSERT INTO versions VALUES (?,?,?,?,?)", (item["id"], item["revision"], item["updated_at"], event, payload))
        return item

    def _remember(self, db, scope, key, fingerprint, item):
        db.execute("INSERT INTO mutations VALUES (?,?,?,?)", (scope, key, fingerprint, _json(item)))
        return self.project(item)

    @staticmethod
    def _compact_records(item):
        """Drop repeated task packets, never their immutable scientific refs."""
        for plan in [item.get("current_plan"), *[b.get("current_plan") for b in (item.get("branch_set") or {}).get("branches", [])], *[r["plan"] for r in item.get("rounds", [])]]:
            if plan:
                for spec in plan["candidates"]:
                    spec.pop("task_packet", None)
        item["total_decisions"] = max(item.get("total_decisions", 0), len(item.get("decisions", [])))
        item["decisions"] = item.get("decisions", [])[-3:]
        item["has_more_decisions"] = item["total_decisions"] > len(item["decisions"])
        item["has_more_events"] = len(item.get("events", [])) > 10 or item.get("has_more_events", False)
        item["events"] = item.get("events", [])[-10:]

    @classmethod
    def project(cls, value):
        """Bound native envelopes, including unchanged legacy persisted replies."""
        item = copy.deepcopy(value)
        for branch in (item.get("branch_set") or {}).get("branches", []):
            branch.setdefault("gate", {"allowed": False, "blockers": [{"code": "scope_not_checked"}], "dependency_refs": []})
            branch.setdefault("current_scope", None)
            branch.setdefault("scope_status", "unknown")
            for question in branch["questions"]:
                question.setdefault("needs_answer", bool(question["required"]))
            if branch.get("latest_result"):
                branch["latest_result"].setdefault("current", False)
        cls._compact_records(item)
        item.setdefault("omitted_rounds", [])
        item["total_rounds"] = max(item.get("total_rounds", 0), len(item["rounds"]) + len(item["omitted_rounds"]))
        for record in item["rounds"]:
            if "artifact" not in record:
                record["artifact"] = _digest(_json(record).encode("utf-8"))
        # Leave exact text intact. Older whole records move behind explicit refs.
        for field in ("events", "decisions", "rounds"):
            while len(_json(item).encode("utf-8")) > MAX_DETAIL_BYTES and len(item[field]) > 1:
                older = item[field].pop(0)
                if field == "rounds":
                    item["omitted_rounds"].append({"index": older["index"], "run_id": older["run"]["id"], "artifact": older["artifact"]})
                elif field == "events":
                    item["has_more_events"] = True
        item["has_more_decisions"] = item["total_decisions"] > len(item["decisions"])
        item["has_more_rounds"] = bool(item["omitted_rounds"])
        if len(_json(item).encode("utf-8")) > MAX_DETAIL_BYTES:
            raise LabError("response_too_large", "This adapter's current record exceeds the bounded detail contract; no partial scientific text was returned")
        return item

    def _adapter(self, item):
        adapter = self.adapters.get(item["adapter"]["id"])
        if adapter is None or adapter.manifest["version"] != item["adapter"]["version"]:
            raise LabError("unsupported_adapter", "The frozen adapter version is unavailable; do not replay with a new method")
        return adapter

    def _plan(self, db, item, next_method=None):
        if item.get("branch_set"):
            item["current_plan"] = None
            item.update(status="planned", stop_reason=None)
            return
        hypothesis_set = item.get("hypothesis_set")
        if "hypothesis_set" in item:
            item["hypothesis_set_status"] = ("not_specified" if hypothesis_set is None else
                "current_context" if hypothesis_set["input_artifact"] == item["input_artifact"]
                and hypothesis_set["brief_revision"] == item["brief"]["revision"] else "needs_revalidation")
        if item["budget"]["used_actions"] >= item["budget"]["max_actions"] or item.get("_global_round_count", len(item["rounds"])) >= item["budget"]["max_rounds"]:
            item.update(current_plan=None, status="completed", stop_reason="budget_exhausted")
            item["review"] = _review("stop", "The frozen resource ceiling prevents further execution", uncertainties=["Changed goals or inputs remain unevaluated"])
            return
        adapter = self._adapter(item)
        inputs = self._read_artifact(db, item["input_artifact"])
        previous = [r for r in item["rounds"] if r["run"]["input_artifact"] == item["input_artifact"]
                    and r["plan"]["goal_revision"] == item["brief"]["revision"]]
        candidates = adapter.candidates(inputs, previous, next_method)
        if not candidates:
            item.update(current_plan=None, status="needs_input", stop_reason="supplied_inputs_exhausted")
            item["review"] = _review("needs_input", "No unchecked supplied source remains; provide new material or stop unresolved", uncertainties=["Network retrieval is available through the separate Literature workspace"])
            return
        frozen = []
        for candidate in candidates:
            spec = {**candidate, "id": _id("action"), "question": item["brief"]["goal"], "goal_revision": item["brief"]["revision"],
                    "input_artifact": item["input_artifact"], "adapter_version": adapter.manifest["version"],
                    "cost": {"actions": 1, "external_requests": 0, "model_tokens": 0}, "protocol_refs": adapter.manifest["required_protocols"],
                    "stop_rules": ["One bounded local calculation", "No network, model, arbitrary code, or independent replicate"]}
            post_outcome = (bool(item["rounds"]) and not previous) or any(
                any(a.get("research_mode") == "post_outcome_exploratory" for a in r["plan"]["candidates"]
                    if a["id"] == r["plan"]["selected_action_id"]) for r in previous)
            spec["research_mode"] = "post_outcome_exploratory" if post_outcome else "predeclared_local_analysis"
            spec["execution_environment"] = _loaded_environment()
            if item.get("hypothesis_set_artifact"):
                spec.update(hypothesis_set_artifact=item["hypothesis_set_artifact"],
                            hypothesis_set_status=item["hypothesis_set_status"])
            spec["task_packet"] = {"task_id": spec["id"], "role": "local_research_worker", "brief_revision": item["brief"]["revision"],
                "snapshot_revision": item["revision"], "goal": {"objective": item["brief"]["goal"], "success_criterion": item["brief"]["success_criteria"]},
                "task": {"method": spec["method"], "contribution": spec["expected_learning"]},
                "relevant_state": {"decisions": copy.deepcopy(item["decisions"][-3:]), "previous_review": copy.deepcopy(item["review"]),
                                   "previous_run_ids": [r["run"]["id"] for r in previous]},
                "evidence": {"input_artifact": item["input_artifact"], "input_summary": copy.deepcopy(item["input_summary"])},
                "constraints": {"scope": item["brief"]["constraints"], "authorized_actions": item["brief"]["authorized_actions"],
                                "remaining_actions": item["budget"]["max_actions"] - item["budget"]["used_actions"],
                                "remaining_rounds": item["budget"]["max_rounds"] - item.get("_global_round_count", len(item["rounds"]))},
                "output": {"required": ["Observation", "QC", "AnalysisClaim", "Review", "actual_usage"],
                           "completion_criterion": "Finish the frozen local method and report QC/limits; do not assert the broad goal is solved"},
                "stop_or_ask": spec["stop_rules"] + ["Failed QC", "Missing required supplied input"],
                "protocol_sections": [{"id": key, "version": "0.5", "requirements": PROTOCOL_REQUIREMENTS[key]} for key in spec["protocol_refs"]],
                "input_refs": [item["input_artifact"]]}
            if item.get("hypothesis_set_artifact"):
                spec["task_packet"]["relevant_state"]["hypothesis_set"] = {
                    "id": hypothesis_set["id"], "revision": hypothesis_set["revision"],
                    "artifact": item["hypothesis_set_artifact"], "context_status": item["hypothesis_set_status"],
                    "post_outcome": hypothesis_set["post_outcome"], "scientific_status": "not_validated",
                    "candidates": [{"id": h["id"], "statement": h["statement"]} for h in hypothesis_set["candidates"]]}
                spec["task_packet"]["input_refs"].append(item["hypothesis_set_artifact"])
            spec["spec_artifact"] = self._artifact(db, item["id"], spec)
            spec.pop("task_packet")
            frozen.append(spec)
        item["current_plan"] = {"id": _id("plan"), "round": item.get("_global_round_count", len(item["rounds"])) + 1, "goal_revision": item["brief"]["revision"],
            "input_artifact": item["input_artifact"], "candidates": frozen, "selected_action_id": frozen[0]["id"], "selection_origin": "policy",
            "selection_reason": item["review"]["reason"] if item["review"] else "Bounded initial policy; a person may select another frozen candidate",
            "comparison_criteria": ["Relevance to current question", "Expected information from fixed inputs", "One action unit per candidate", "Coverage and inference limitations remain visible"],
            "frozen_at": _now()}
        item.update(status="planned", stop_reason=None)

    def create(self, request, *, resolve_origin=None):
        _fields(request, {"idempotency_key", "entry", "brief", "adapter_id", "inputs", "budget"}, {"origin", "hypothesis_set"})
        with self._db(write=True) as db:
            replay, fingerprint = self._replay(db, "create", request["idempotency_key"], request)
            if replay is not None:
                # Creation receipts remain immutable; a retry must not reopen
                # a research item that the person has since moved to Trash.
                self._load(db, replay["id"])
                return self.project(replay)
            resolved = request
            if request.get("origin") is not None:
                if resolve_origin is None:
                    raise LabError("invalid_request", "Idea origin requires authoritative service verification")
                resolved = resolve_origin(copy.deepcopy(request))
                _fields(resolved, {"idempotency_key", "entry", "brief", "adapter_id", "inputs", "budget", "origin"}, {"hypothesis_set"})
            item = self._create(db, resolved)
            return self._remember(db, "create", request["idempotency_key"], fingerprint, item)

    def _create(self, db, request):
        if request["entry"] not in ("goal", "hypothesis") or not isinstance(request["adapter_id"], str) or request["adapter_id"] not in self.adapters:
            raise LabError("invalid_request", "Choose a supported entry and adapter")
        brief = _fields(request["brief"], {"goal", "hypothesis", "success_criteria", "constraints"})
        brief = {k: _text(v, empty=k in ("hypothesis", "constraints")) for k, v in brief.items()}
        if request["entry"] == "hypothesis" and not brief["hypothesis"].strip() and request.get("hypothesis_set") is None:
            raise LabError("invalid_request", "Hypothesis entry requires a stated hypothesis")
        budget = _fields(request["budget"], {"max_actions", "max_rounds"})
        budget = {"max_actions": _integer(budget["max_actions"], 1, 12), "max_rounds": _integer(budget["max_rounds"], 1, 6)}
        adapter = self.adapters[request["adapter_id"]]
        inputs = adapter.validate(request["inputs"])
        origin = request.get("origin")
        if origin is not None:
            _fields(origin, {"kind", "session_id", "generation_id", "selected_ids", "decision_revision"})
            if origin["kind"] != "idea" or not isinstance(origin["selected_ids"], list) or not 1 <= len(origin["selected_ids"]) <= 3:
                raise LabError("invalid_request", "Invalid selected Idea provenance")
            origin = {"kind": "idea", "session_id": _text(origin["session_id"], 128), "generation_id": _text(origin["generation_id"], 128),
                      "selected_ids": [_text(x, 128) for x in origin["selected_ids"]],
                      "decision_revision": _integer(origin["decision_revision"], 1, 2147483647)}
        elif adapter.manifest["id"] == "source_evidence":
            for source in inputs["sources"]:
                if "provenance" in source:
                    reported = source["provenance"]
                    markers = _gap_markers(reported)
                    source["provenance"] = {"kind": "user_supplied", "verification": "not_source_verified", "client_metadata": reported}
                    if markers:
                        source["provenance"].update(gap_marker=markers[0], gap_markers=markers)
        campaign_id = _id("campaign")
        db.execute("INSERT INTO campaigns VALUES (?,?)", (campaign_id, "{}"))
        digest = self._artifact(db, campaign_id, inputs)
        brief.update(revision=1, authorized_actions=list(adapter.manifest["methods"]),
                     stop_rules=["Declared action/round ceiling", "Failed QC or missing required inputs", "Human stop"])
        item = {"schema_version": 1, "id": campaign_id, "revision": 1, "created_at": _now(), "updated_at": _now(),
            "entry": request["entry"], "status": "planned", "brief": brief, "origin": origin,
            "adapter": copy.deepcopy(adapter.manifest), "input_artifact": digest, "input_summary": adapter.summary(inputs),
            "hypotheses": [{"id": "stated", "statement": brief["hypothesis"], "status": "untested", "origin": "human",
                            "weakening_condition": "Evaluate only against the adapter's frozen interpretation rules; unrestricted statement remains unvalidated"}] if brief["hypothesis"] else [],
            "budget": {**budget, "used_actions": 0, "reserved_actions": 0, "remaining_actions": budget["max_actions"],
                       "model_tokens": 0, "external_requests": 0, "monetary_cost": 0, "wall_ms": 0},
            "current_plan": None, "rounds": [], "decisions": [], "total_decisions": 0, "claims": [], "comparisons": [], "review": None,
            "hypothesis_set": None, "hypothesis_set_artifact": None, "hypothesis_set_revision": 0,
            "stop_reason": None, "events": [], "has_more_events": False}
        item["hypotheses"].append({"id": "open", "statement": "An unmodeled explanation or missing premise may account for the observations", "status": "open",
                                   "origin": "policy", "weakening_condition": "Requires additional discriminating evidence; not eliminated by these local calculations"})
        if "hypothesis_set" in request:
            self._freeze_hypotheses(db, item, request["hypothesis_set"])
        self._plan(db, item)
        self._save(db, item, "campaign_created", {"authorization_scope": "Bounded local adapter only", "input_artifact": digest})
        return item

    def _freeze_hypotheses(self, db, item, value):
        """Persist a new explicit declaration without rewriting older sets."""
        previous = item.get("hypothesis_set_artifact")
        revision = item.get("hypothesis_set_revision", 0) + 1
        try:
            record = freeze_set(value, campaign_id=item["id"], revision=revision,
                brief_revision=item["brief"]["revision"], introduced_at=_now(),
                input_artifact=item["input_artifact"], inputs=self._read_artifact(db, item["input_artifact"]),
                rounds=item["rounds"], previous_artifact=previous)
        except HypothesisError as exc:
            raise LabError("invalid_request", str(exc)) from None
        item.update(hypothesis_set=record, hypothesis_set_revision=revision,
                    hypothesis_set_artifact=self._artifact(db, item["id"], record) if record else None)

    def get(self, campaign_id):
        with self._db() as db:
            return self.project(self._load(db, campaign_id))

    def list(self):
        with self._db() as db:
            rows = db.execute("SELECT payload FROM campaigns WHERE json_extract(payload,'$.deleted_at') IS NULL ORDER BY json_extract(payload,'$.updated_at') DESC LIMIT 51").fetchall()
            items = []
            for row in rows[:50]:
                item = json.loads(row[0])
                item.update(current_plan=None, rounds=[], decisions=[], claims=[], comparisons=[], events=[])
                items.append(item)
            return {"items": items, "has_more": len(rows) > 50}

    def history(self, campaign_id):
        with self._db() as db:
            self._load(db, campaign_id)
            rows = db.execute("SELECT * FROM versions WHERE campaign_id=? ORDER BY revision DESC LIMIT 51", (campaign_id,)).fetchall()
            return {"items": [{"revision": r["revision"], "at": r["at"], "event": r["event"],
                               "brief": json.loads(r["payload"])["brief"], "status": json.loads(r["payload"])["status"]} for r in rows[:50]], "has_more": len(rows) > 50}

    def artifact(self, campaign_id, digest):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise LabError("invalid_request", "An exact SHA-256 artifact reference is required")
        with self._db() as db:
            item = self._load(db, campaign_id)
            if not db.execute("SELECT 1 FROM artifact_links WHERE campaign_id=? AND digest=?", (campaign_id, digest)).fetchone():
                # Old raw snapshots remain untouched. Their bounded round view
                # is a deterministic, scoped virtual artifact until a later
                # explicit mutation persists the same digest.
                self._compact_records(item)
                for record in item["rounds"]:
                    content = {k: v for k, v in record.items() if k != "artifact"}
                    if _digest(_json(content).encode("utf-8")) == digest:
                        return {"sha256": digest, "media_type": "application/json", "content": content}
                raise LabError("not_found", "Artifact does not belong to this campaign")
            return {"sha256": digest, "media_type": "application/json", "content": self._read_artifact(db, digest)}

    def _mutate(self, campaign_id, request, operation, callback, required=(), optional=()):
        _fields(request, {"expected_revision", "idempotency_key", *required}, optional)
        _integer(request["expected_revision"], 1, 2147483647)
        with self._db(write=True) as db:
            item = self._load(db, campaign_id)
            replay, fingerprint = self._replay(db, campaign_id, request["idempotency_key"], {"operation": operation, **request})
            if replay is not None:
                return self.project(replay)
            if item["revision"] != request["expected_revision"]:
                raise LabError("revision_conflict", "The campaign changed; reload before deciding")
            item["revision"] += 1
            data = callback(db, item)
            self._save(db, item, operation, data)
            return self._remember(db, campaign_id, request["idempotency_key"], fingerprint, item)

    def decide(self, campaign_id, request):
        def apply(db, item):
            kind = request["kind"]
            if item.get("branch_set"):
                import research_branches as branches
                if kind == "select":
                    raise LabError("branch_required", "Select the candidate inside its explicit branch")
                if kind in {"defer", "stop"}:
                    item["branch_set"] = branches.control_root(item["branch_set"], "pause" if kind == "defer" else "stop")
                elif kind == "revise":
                    item["branch_set"]["authority_epoch"] += 1
            feedback = _text(request["feedback"], empty=True)
            if kind not in ("select", "revise", "defer", "stop"):
                raise LabError("invalid_request", "Unknown human decision")
            if "hypothesis_set" in request and kind != "revise":
                raise LabError("invalid_request", "A hypothesis set changes only through an explicit brief revision")
            record = {"id": _id("decision"), "kind": kind, "feedback": feedback, "at": _now(), "actor": "native_user",
                      "brief_revision": item["brief"]["revision"], "campaign_revision": request["expected_revision"],
                      "plan_id": item["current_plan"]["id"] if item["current_plan"] else None,
                      "input_artifact": item["input_artifact"], "selected_action_id": None,
                      "scope": "Research preference within frozen local authorization; not scientific validation or extra budget"}
            # The actual new decision must already be in the current snapshot
            # when a revision freezes its worker packet.
            item["decisions"].append(record)
            item["total_decisions"] = item.get("total_decisions", len(item["decisions"]) - 1) + 1
            if kind == "select":
                if item["status"] != "planned" or not item["current_plan"]:
                    raise LabError("invalid_state", "Select an action in a current frozen plan")
                chosen = _text(request.get("selected_action_id"), 128)
                if chosen not in {a["id"] for a in item["current_plan"]["candidates"]}:
                    raise LabError("invalid_request", "The selected action is not in the current candidate set")
                item["current_plan"].update(selected_action_id=chosen, selection_origin="human", selection_reason=feedback or "Explicit human selection")
                record["selected_action_id"] = chosen
                item["current_plan"].update(decision_id=record["id"], decision_revision=item["revision"],
                                            decision_artifact=self._artifact(db, item["id"], record))
            elif kind == "revise":
                changes = {key: _text(request[key], empty=key in ("hypothesis", "constraints"))
                           for key in ("goal", "hypothesis", "success_criteria", "constraints") if key in request}
                if not changes and "hypothesis_set" not in request:
                    raise LabError("invalid_request", "A brief revision requires at least one explicit brief field")
                proposed_set = request.get("hypothesis_set") if "hypothesis_set" in request else item.get("hypothesis_set")
                if item["entry"] == "hypothesis" and changes.get("hypothesis", item["brief"]["hypothesis"]).strip() == "" and proposed_set is None:
                    raise LabError("invalid_request", "Hypothesis entry requires a stated hypothesis")
                record.update(changes=changes, post_outcome=bool(item["rounds"]),
                              previous_brief={key: item["brief"][key] for key in changes})
                item["brief"].update(**changes, revision=item["brief"]["revision"] + 1)
                if "hypothesis_set" in request:
                    record["previous_hypothesis_set_artifact"] = item.get("hypothesis_set_artifact")
                    self._freeze_hypotheses(db, item, request["hypothesis_set"])
                    record["hypothesis_set_artifact"] = item.get("hypothesis_set_artifact")
                    record["hypothesis_set_revision"] = item["hypothesis_set_revision"]
                if "hypothesis" in changes:
                    item["hypotheses"] = [h for h in item["hypotheses"] if h["id"] != "stated"]
                    if changes["hypothesis"]:
                        item["hypotheses"].insert(0, {"id": "stated", "statement": changes["hypothesis"], "status": "untested",
                            "origin": "human_post_outcome" if item["rounds"] else "human", "weakening_condition": "Evaluate only against frozen applicable rules; independent confirmation is unavailable"})
                for claim in item["claims"]:
                    claim["status"] = "needs_revalidation"
                item["review"] = None
                self._plan(db, item)
            elif kind == "defer":
                item.update(status="needs_input", stop_reason="human_deferred")
            else:
                item.update(status="stopped", stop_reason="human_stop")
                item["review"] = _review("stop", "The person stopped this campaign", uncertainties=["Unresolved questions remain unresolved"])
            return record
        return self._mutate(campaign_id, request, "human_decision", apply, {"kind", "feedback"}, {"selected_action_id", "goal", "hypothesis", "success_criteria", "constraints", "hypothesis_set"})

    def run(self, campaign_id, request):
        def execute(db, item):
            if item.get("branch_set"):
                raise LabError("branch_required", "Select an explicit branch before new execution")
            return self._execute_action(db, item, request)
        return self._mutate(campaign_id, request, "action_executed", execute)

    def _execute_action(self, db, item, request):
        if item["status"] != "planned" or not item["current_plan"]:
            raise LabError("invalid_state", "Only a current frozen plan can execute")
        budget = item["budget"]
        if budget["used_actions"] + budget["reserved_actions"] >= budget["max_actions"] or item.get("_global_round_count", len(item["rounds"])) >= budget["max_rounds"]:
            raise LabError("budget_exhausted", "The frozen local action or round limit is exhausted")
        plan = copy.deepcopy(item["current_plan"])
        summary = next(a for a in plan["candidates"] if a["id"] == plan["selected_action_id"])
        frozen = self._read_artifact(db, summary["spec_artifact"])
        if ({k: v for k, v in frozen.items() if k != "task_packet"} !=
                {k: v for k, v in summary.items() if k not in ("spec_artifact", "task_packet")} or
                summary["goal_revision"] != item["brief"]["revision"] or summary["input_artifact"] != item["input_artifact"]):
            raise LabError("integrity_error", "Frozen action references changed")
        if frozen.get("hypothesis_set_artifact") != item.get("hypothesis_set_artifact"):
            raise LabError("integrity_error", "The frozen hypothesis set reference changed")
        if item.get("hypothesis_set_artifact") and self._read_artifact(db, item["hypothesis_set_artifact"]) != item.get("hypothesis_set"):
            raise LabError("integrity_error", "The current hypothesis set differs from its immutable artifact")
        spec = {**frozen, "spec_artifact": summary["spec_artifact"]}
        if spec.get("execution_environment") != _loaded_environment():
            raise LabError("environment_changed", "Execution source, lockfile, or Python changed; explicitly revise the goal to freeze a current plan")
        adapter = self._adapter(item)
        inputs = self._read_artifact(db, item["input_artifact"])
        run_id, attempt_id = _id("run"), _id("attempt")
        started_at, clock = _now(), time.perf_counter()
        budget["reserved_actions"] += 1
        # Scientific rules stay at their pre-outcome digest. Freeze the
        # actual dispatch context separately after the human selection.
        packet = copy.deepcopy(frozen["task_packet"])
        if item.get("_branch_dispatch_scope"):
            packet["branch_scope"] = copy.deepcopy(item["_branch_dispatch_scope"])
        packet["snapshot_revision"] = item["revision"]
        packet["relevant_state"]["decisions"] = copy.deepcopy(item["decisions"][-3:])
        selected_decision = next((d for d in reversed(item["decisions"]) if d["kind"] == "select"
                                  and d["plan_id"] == plan["id"] and d["selected_action_id"] == spec["id"]), None)
        if plan.get("decision_artifact"):
            selected_decision = self._read_artifact(db, plan["decision_artifact"])
            if selected_decision["id"] not in {d["id"] for d in packet["relevant_state"]["decisions"]}:
                packet["relevant_state"]["decisions"].append(selected_decision)
        packet["actual_selection"] = {"plan_id": plan["id"], "action_id": spec["id"], "origin": plan["selection_origin"],
            "reason": plan["selection_reason"], "decision_id": selected_decision["id"] if selected_decision else None,
            "decision_revision": selected_decision["campaign_revision"] + 1 if selected_decision else None,
            "decision_artifact": plan.get("decision_artifact")}
        dispatch_digest = self._artifact(db, item["id"], {"schema_version": 1, "run_id": run_id, "attempt_id": attempt_id,
            "scientific_spec_artifact": spec["spec_artifact"], "task_packet": packet})
        spec["task_packet"] = packet
        spec["dispatch_artifact"] = dispatch_digest
        # Identity and reservation exist in the same transaction BEFORE local
        # execution. There is no external effect to reconcile on rollback.
        self._artifact(db, item["id"], {"run_id": run_id, "attempt_id": attempt_id, "generation": 1,
                                      "spec_artifact": spec["spec_artifact"], "dispatch_artifact": dispatch_digest,
                                      "idempotency_key": request["idempotency_key"], "state": "reserved"})
        failed, stage_errors = False, []
        try:
            observation = adapter.execute(copy.deepcopy(inputs), copy.deepcopy(spec))
        except Exception:
            failed = True
            stage_errors.append({"stage": "execution", "code": "adapter_execution_failed"})
            observation = {"kind": "execution_failure", "data": {}, "coverage": {"kind": "incomplete"}}
        if failed:
            qc = _qc([("execution_complete", False, "The bounded adapter failed; no scientific negative evidence was produced")])
            analysis = {"claim": "Execution incomplete; no scientific conclusion is available", "source_support": "cannot_determine", "inference_validity": "cannot_determine", "limitations": ["Adapter execution failure"], "input_refs": []}
            review = _review("needs_input", "Execution failed; preserve the attempt and inspect the adapter before a new action", uncertainties=["No valid observation"])
        else:
            # A later role failure cannot erase a completed machine
            # observation. Keep each stage's status and earlier artifacts.
            try:
                qc = adapter.quality(copy.deepcopy(observation), copy.deepcopy(spec))
            except Exception:
                stage_errors.append({"stage": "qc", "code": "quality_evaluation_failed"})
                qc = _qc([("quality_evaluation", False, "Observation retained; QC evaluation failed and support eligibility is unresolved")])
            qc["checks"].insert(0, {"name": "frozen_input_hash", "passed": True, "detail": item["input_artifact"]})
            try:
                analysis = adapter.analyze(copy.deepcopy(observation), copy.deepcopy(qc), copy.deepcopy(spec))
            except Exception:
                stage_errors.append({"stage": "analysis", "code": "analysis_failed"})
                analysis = {"claim": "The machine observation is retained, but analysis did not complete", "source_support": "cannot_determine",
                            "inference_validity": "cannot_determine", "limitations": ["Analysis failed; no interpretation was accepted"], "input_refs": []}
            if spec.get("research_mode") == "post_outcome_exploratory":
                analysis["limitations"].append("The goal or inputs were revised after earlier observations; this analysis is exploratory and not independent confirmation")
            if stage_errors:
                review = _review("needs_input", "A post-execution stage failed; retain the observation and inspect the affected stage",
                                 uncertainties=[error["code"] for error in stage_errors])
            else:
                try:
                    # Review gets a separate bounded record context, never
                    # mutable raw inputs or authority to alter frozen rules.
                    review = adapter.review(copy.deepcopy(analysis), copy.deepcopy(qc), copy.deepcopy(observation), copy.deepcopy(spec), copy.deepcopy(item["rounds"]))
                except Exception:
                    stage_errors.append({"stage": "review", "code": "review_failed"})
                    review = _review("needs_input", "Review failed; completed observation, QC and analysis are preserved", uncertainties=["No completed review"])
        elapsed = round((time.perf_counter() - clock) * 1000, 3)
        observation_digest = self._artifact(db, item["id"], {"observation": observation, "qc": qc})
        analysis["input_refs"] = [spec["spec_artifact"], observation_digest, dispatch_digest]
        if spec.get("hypothesis_set_artifact"):
            analysis["input_refs"].append(spec["hypothesis_set_artifact"])
        analysis_digest = self._artifact(db, item["id"], analysis)
        review["input_refs"] = [spec["spec_artifact"], observation_digest, analysis_digest, dispatch_digest]
        if spec.get("hypothesis_set_artifact"):
            review["input_refs"].append(spec["hypothesis_set_artifact"])
        self._artifact(db, item["id"], review)
        run = {"id": run_id, "attempt_id": attempt_id, "generation": 1, "status": "failed" if failed else "completed",
               "stage_errors": stage_errors,
               "dispatch_artifact": dispatch_digest,
               "spec_artifact": spec["spec_artifact"], "observation_artifact": observation_digest, "input_artifact": item["input_artifact"],
               "replicate_id": None, "is_independent_replicate": False, "started_at": started_at, "completed_at": _now(),
               "runtime": {"python": sys.version.split()[0], "adapter_version": adapter.manifest["version"],
                           "execution_environment": copy.deepcopy(spec["execution_environment"])},
               "usage": {"actions": 1, "external_requests": 0, "model_tokens": 0, "monetary_cost": 0, "wall_ms": elapsed}}
        if spec.get("hypothesis_set_artifact"):
            run["hypothesis_set_artifact"] = spec["hypothesis_set_artifact"]
        budget["reserved_actions"] -= 1
        budget["used_actions"] += 1
        budget["wall_ms"] = round(budget["wall_ms"] + elapsed, 3)
        if item["rounds"]:
            prior = item["rounds"][-1]
            same_inputs = prior["run"]["input_artifact"] == item["input_artifact"]
            same_goal = prior["plan"]["goal_revision"] == plan["goal_revision"]
            comparison = {"id": _id("comparison"), "run_ids": [prior["run"]["id"], run_id],
                "rule": "Inspect paired run records under the frozen plan criteria; never pool shared inputs as independent replications",
                "frozen_criteria": copy.deepcopy(plan["comparison_criteria"]),
                "comparability": "changed_inputs_not_like_for_like" if not same_inputs else "changed_goal_not_like_for_like" if not same_goal else "same_inputs_different_analysis",
                "eligible_for_pooled_confirmation": False,
                "source_support": {"before": prior["analysis"]["source_support"], "after": analysis["source_support"]},
                "inference_validity": {"before": prior["analysis"]["inference_validity"], "after": analysis["inference_validity"]},
                "propositions": [prior["analysis"]["claim"], analysis["claim"]],
                "changed_observation_fields": [key for key in sorted(set(prior["observation"]["data"]) | set(observation["data"]))
                                               if prior["observation"]["data"].get(key) != observation["data"].get(key)],
                "limitations": ["These runs are not independent replicates", "Support labels apply to each precise proposition, not an arbitrary research hypothesis"],
                "unresolved": list(dict.fromkeys(analysis["limitations"] + review["uncertainties"])),
                "input_refs": [prior["run"]["observation_artifact"], observation_digest, spec["spec_artifact"]]}
            comparison["artifact"] = self._artifact(db, item["id"], comparison)
            item.setdefault("comparisons", []).append(comparison)
        item["rounds"].append({"index": len(item["rounds"]) + 1, "plan": plan, "run": run,
                               "observation": observation, "qc": qc, "analysis": analysis, "review": review})
        item["claims"].append({"id": _id("claim"), "text": analysis["claim"], "status": "current",
            "source_support": analysis["source_support"], "inference_validity": analysis["inference_validity"], "conditions": analysis["limitations"],
            "input_artifact": item["input_artifact"], "run_id": run_id})
        item["review"] = review
        if review["next_action"] == "continue" and (budget["used_actions"] >= budget["max_actions"] or item.get("_global_round_count", len(item["rounds"])) >= budget["max_rounds"]):
            item.update(status="completed", stop_reason="budget_exhausted")
            item["review"] = {**review, "next_action": "stop", "next_method": None, "reason": "The resource ceiling was reached; remaining uncertainty is not resolved"}
        elif review["next_action"] == "continue":
            item.update(status="awaiting_next", stop_reason=None)
        elif review["next_action"] == "needs_input":
            item.update(status="needs_input", stop_reason="quality_or_inputs")
        else:
            item.update(status="completed", stop_reason="bounded_analysis_complete")
        return {"run_id": run_id, "observation_artifact": observation_digest, "qc_passed": qc["passed"], "next_action": item["review"]["next_action"]}

    def advance(self, campaign_id, request):
        def apply(db, item):
            if item.get("branch_set"):
                if item["branch_set"]["root_control"] != "paused":
                    raise LabError("branch_required", "Plan the next action inside its explicit branch")
                import research_branches as branches
                item["branch_set"] = branches.control_root(item["branch_set"], "resume")
                item.update(status="planned", stop_reason=None)
                return {"root_control": "active", "authority_epoch": item["branch_set"]["authority_epoch"]}
            if item["status"] == "needs_input" and item["stop_reason"] == "human_deferred":
                if item["current_plan"] and not any(r["plan"]["id"] == item["current_plan"]["id"] for r in item["rounds"]):
                    item.update(status="planned", stop_reason=None)
                    return {"resumed_frozen_plan": item["current_plan"]["id"]}
                if item["review"] and item["review"]["next_action"] == "continue":
                    item["status"] = "awaiting_next"
            if item["status"] != "awaiting_next" or not item["review"] or item["review"]["next_action"] != "continue":
                raise LabError("invalid_state", "There is no review-approved next action; revise inputs or the goal explicitly")
            self._plan(db, item, item["review"]["next_method"])
            return {"review_driven": True, "plan_id": item["current_plan"]["id"] if item["current_plan"] else None}
        return self._mutate(campaign_id, request, "next_plan_frozen", apply)

    def correct_inputs(self, campaign_id, request):
        def apply(db, item):
            reason = _text(request["reason"])
            adapter = self._adapter(item)
            inputs = adapter.validate(request["inputs"])
            old = item["input_artifact"]
            if item["adapter"]["id"] == "source_evidence":
                old_sources = {s["id"]: s for s in self._read_artifact(db, old)["sources"]}
                for source in inputs["sources"]:
                    previous = old_sources.get(source["id"])
                    comparable = {k: v for k, v in source.items() if k != "provenance"}
                    if previous and comparable == {k: v for k, v in previous.items() if k != "provenance"}:
                        # Unchanged text retains only the authoritative stored
                        # lineage, never renderer-supplied replacement metadata.
                        source.pop("provenance", None)
                        if "provenance" in previous:
                            source["provenance"] = copy.deepcopy(previous["provenance"])
                    else:
                        markers = _gap_markers((previous or {}).get("provenance")) + _gap_markers(source.get("provenance"))
                        source["provenance"] = {"kind": "user_corrected", "verification": "not_source_verified",
                            "derived_from": {"input_artifact": old, "source_id": source["id"],
                                             "source_hash": _digest(previous["text"].encode("utf-8")) if previous else None}}
                        if markers:
                            source["provenance"]["gap_marker"] = markers[0]
                            source["provenance"]["gap_markers"] = list(dict.fromkeys(markers))
                            _gap_markers(source["provenance"])
            new = self._artifact(db, item["id"], inputs)
            if new == old:
                raise LabError("invalid_request", "A correction must change the input content")
            affected = []
            for claim in item["claims"]:
                if claim["input_artifact"] == old:
                    claim["status"] = "needs_revalidation"
                    affected.append(claim["id"])
            item.update(input_artifact=new, input_summary=adapter.summary(inputs), review=None)
            item["brief"]["revision"] += 1
            self._plan(db, item)
            return {"reason": reason, "old_input_artifact": old, "new_input_artifact": new, "affected_claims": affected,
                    "historical_observations_preserved": True}
        return self._mutate(campaign_id, request, "inputs_corrected", apply, {"inputs", "reason"})
