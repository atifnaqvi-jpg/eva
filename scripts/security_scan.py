#!/usr/bin/env python3
"""Dependency scanning and SAST as one command that fails loudly (SEC-13, §19.1).

Two halves, both required by the §19.1 bullet "Dependency scanning + SAST in CI":

**Dependencies.** Every runtime requirements file is resolved to its full closure
with `pip install --dry-run --report`, which asks pip what the *image* would
install rather than what happens to be importable on the scanning host — the two
differ precisely on the transitive packages nobody pins. Each resolved
`(name, version)` is then queried against the OSV database in one batched call.
The two JavaScript trees are audited with `npm audit`, run against the lockfile
in a scratch directory so a checkout without `node_modules` still scans.

**SAST.** Python is walked as an AST rather than grepped, because a regex cannot
tell `subprocess.run(cmd, shell=True)` from the string `"shell=True"` in a
comment, and a rule that fires on prose gets baselined until it means nothing.
The JavaScript trees have no stdlib parser on this host, so those rules are
regex over *source only* and are deliberately narrow.

**The baseline is not a silencer.** `scripts/security_baseline.json` holds the
findings this repository has looked at and decided to carry. An entry needs a
written `why`, a `scope`, and a `review_by` date, and the gate fails when:
a finding is not in the baseline, a baseline entry matches no current finding
(so it cannot rot into a permanent permission), an entry has passed its review
date, or an entry carries no reason. Fixing a finding therefore *requires*
deleting its entry, which is the point.

**A gate must be able to fail, and to say so.** The scan asserts floors on what
it scanned (packages, files) because "zero findings" from zero input is the same
green as a clean tree. Exit codes: 0 clean, 1 findings, 2 the scanner itself
could not complete — a network failure must not read as a pass.

Run: `python scripts/security_scan.py [--github]`
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "security_baseline.json"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_ATTEMPTS = 3

# Each entry is a file the deployed images install. `requirements-dev.txt` is
# deliberately absent: it is the CI toolchain, it never enters an image, and
# scanning it would suggest the product ships it.
PYTHON_REQUIREMENTS = (
    "backend/api/requirements.txt",
    "backend/worker/requirements.txt",
    "backend/embed/requirements.txt",
)
NPM_TREES = ("backend/api/widget", "dashboard")

# Floors, so a scan that quietly read nothing cannot report a clean result.
MIN_PACKAGES_PER_REQUIREMENTS = 20
MIN_JS_AUDITED_TREES = 2

# Test fixtures are excluded on purpose: `tests/corpus/` holds hostile input by
# design (injection strings, fuzzed uploads) and a SAST pass over it would report
# the fixtures as defects until someone switched the gate off.
PYTHON_SOURCE_ROOTS = (
    "backend/api/app",
    "backend/api/migrations",
    "backend/worker/worker",
    "backend/embed",
    "scripts",
)
JS_SOURCE_ROOTS = (
    "dashboard/app",
    "dashboard/lib",
    "dashboard/components",
    "backend/api/widget/src",
)
SECRET_SCAN_ROOTS = PYTHON_SOURCE_ROOTS + JS_SOURCE_ROOTS + (".github", "caddy", "compose.yaml")

# Floors for the SAST half, for the same non-vacuity reason.
MIN_PYTHON_FILES = 60
MIN_JS_FILES = 10

SKIP_DIR_NAMES = frozenset(
    {"node_modules", ".venv", "__pycache__", ".next", ".git", "dist", ".pytest_cache"}
)

# Migration files run DDL, and their GRANT/REVOKE strings interpolate a module
# constant. The SQL-interpolation rule is meaningless there and would fire on
# every one of them; the other rules still apply.
SQL_RULE_SKIP_PREFIX = "backend/api/migrations"

CREDENTIAL_SHAPES = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("provider_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")),
    ("age_identity", re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
)

JS_SINK_PATTERNS = (
    ("js-eval", re.compile(r"(?<![\w.])eval\s*\(")),
    ("js-new-function", re.compile(r"\bnew\s+Function\s*\(")),
    ("js-document-write", re.compile(r"\bdocument\s*\.\s*write(?:ln)?\s*\(")),
    ("js-raw-html-prop", re.compile(r"\bdangerouslySetInnerHTML\b")),
)
JS_HTML_ASSIGN = re.compile(r"\.\s*(innerHTML|outerHTML)\s*=\s*([^;]*)", re.S)


@dataclass(frozen=True)
class Finding:
    """One reportable item, with a key stable enough to baseline.

    The key deliberately omits line numbers and resolved versions: they move for
    reasons unrelated to the finding, and a baseline that needs editing on every
    upgrade becomes a file people edit without reading.
    """

    kind: str  # dependency | sast | secret
    key: str
    where: str
    rule: str
    message: str
    scope: str = "shipped"

    def line(self) -> str:
        return f"{self.where}: [{self.rule}] {self.message}"


class ScannerError(RuntimeError):
    """The scanner could not complete; distinct from a finding (exit 2, not 1)."""


# --------------------------------------------------------------------------- #
# Dependency scanning
# --------------------------------------------------------------------------- #


def resolve_python_closure(relative_requirements: str) -> list[tuple[str, str]]:
    """Return the (name, version) closure pip would install for one requirements file."""
    path = REPO_ROOT / relative_requirements
    if not path.is_file():
        raise ScannerError(f"{relative_requirements} does not exist")

    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "report.json"
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--quiet",
            "--ignore-installed",
            "--report",
            str(report_path),
            "-r",
            str(path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600, cwd=REPO_ROOT)
        if completed.returncode != 0:
            raise ScannerError(
                f"pip could not resolve {relative_requirements}: {completed.stderr.strip()[-800:]}"
            )
        payload = json.loads(report_path.read_text())
    return _packages_from_report(payload, relative_requirements)


def _packages_from_report(payload: dict, origin: str) -> list[tuple[str, str]]:
    packages = []
    for item in payload.get("install", []):
        meta = item.get("metadata", {})
        name, version = meta.get("name"), meta.get("version")
        if not name or not version:
            raise ScannerError(f"unparseable pip report entry in {origin}: {meta!r}")
        packages.append((name, version))
    if not packages:
        raise ScannerError(f"pip reported no packages for {origin}")
    return packages


def query_osv(packages: list[tuple[str, str]], transport=None) -> dict[int, list[dict]]:
    """Batch-query OSV; returns {package index: [vuln, ...]}.

    Retries a transient failure, then raises: an unreachable advisory database is
    not a clean bill of health, and reporting one would be the worst possible
    failure mode for this gate.
    """
    if not packages:
        raise ScannerError("refusing to query OSV with no packages")
    transport = transport or _urllib_transport
    body = {
        "queries": [
            {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
            for name, version in packages
        ]
    }
    last: Exception | None = None
    for attempt in range(1, OSV_ATTEMPTS + 1):
        try:
            payload = transport(OSV_BATCH_URL, body)
            results = payload.get("results", [])
            if len(results) != len(packages):
                raise ScannerError(
                    f"OSV returned {len(results)} results for {len(packages)} packages"
                )
            return {index: entry.get("vulns", []) for index, entry in enumerate(results)}
        except ScannerError:
            raise
        except Exception as error:  # noqa: BLE001 - any transport failure is retryable
            last = error
            if attempt < OSV_ATTEMPTS:
                time.sleep(2 * attempt)
    raise ScannerError(f"OSV unreachable after {OSV_ATTEMPTS} attempts: {last}")


def _urllib_transport(url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read())


def scan_python_dependencies(transport=None) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    scanned = {"requirements_files": 0, "python_packages": 0}
    per_file: dict[str, list[tuple[str, str]]] = {}

    for relative in PYTHON_REQUIREMENTS:
        packages = resolve_python_closure(relative)
        if len(packages) < MIN_PACKAGES_PER_REQUIREMENTS:
            raise ScannerError(
                f"{relative} resolved to {len(packages)} packages, floor is "
                f"{MIN_PACKAGES_PER_REQUIREMENTS}; a short closure means the scan is not reading it"
            )
        per_file[relative] = packages
        scanned["requirements_files"] += 1
        scanned["python_packages"] += len(packages)

    # One batched call for the union, then attribute each advisory to the files
    # that actually install the package.
    union: dict[tuple[str, str], None] = {}
    for packages in per_file.values():
        for package in packages:
            union[package] = None
    ordered = list(union)
    vulnerable = query_osv(ordered, transport=transport)

    for index, advisories in vulnerable.items():
        name, version = ordered[index]
        origins = sorted(f for f, pkgs in per_file.items() if (name, version) in pkgs)
        for advisory in advisories:
            advisory_id = advisory.get("id", "unknown")
            severity = (advisory.get("database_specific") or {}).get("severity", "UNKNOWN")
            findings.append(
                Finding(
                    kind="dependency",
                    key=f"dep:pypi/{name.lower()}/{advisory_id}".lower(),
                    where=f"{name}=={version} ({', '.join(origins)})",
                    rule=f"osv/{advisory_id}",
                    message=f"{severity}: {advisory.get('summary', 'no summary')}",
                )
            )
    return findings, scanned


def scan_npm_trees() -> tuple[list[Finding], dict]:
    """Audit both JavaScript trees from their lockfiles.

    Run in a scratch directory holding only `package.json` + `package-lock.json`:
    `npm audit` reads the lockfile, so a checkout without `node_modules` scans the
    same tree the image builds from, and nothing in the workspace is modified.
    """
    findings: list[Finding] = []
    scanned = {"npm_trees": 0, "npm_packages": 0}

    for tree in NPM_TREES:
        source = REPO_ROOT / tree
        for required in ("package.json", "package-lock.json"):
            if not (source / required).is_file():
                raise ScannerError(f"{tree}/{required} is missing; npm audit cannot read a lockfile")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("package.json", "package-lock.json"):
                (Path(tmp) / name).write_bytes((source / name).read_bytes())
            full, package_count = _npm_audit(tmp)
            prod, _ = _npm_audit(tmp, omit_dev=True)

        shipped_keys = {finding.key for finding in prod}
        scanned["npm_trees"] += 1
        scanned["npm_packages"] += package_count

        for finding in full:
            if finding.key not in shipped_keys:
                # Dev-only: it never reaches an image, but it still runs in CI.
                finding = Finding(
                    kind=finding.kind,
                    key=finding.key,
                    where=finding.where,
                    rule=finding.rule,
                    message=finding.message,
                    scope="dev",
                )
            findings.append(finding)

    if scanned["npm_trees"] != MIN_JS_AUDITED_TREES:
        raise ScannerError(f"audited {scanned['npm_trees']} npm trees, expected {MIN_JS_AUDITED_TREES}")
    return findings, scanned


def _npm_audit(directory: str, omit_dev: bool = False) -> tuple[list[Finding], int]:
    """Run `npm audit --json` and return (findings, package count).

    npm exit codes: 0 clean, 1 findings, anything else the tool failing — which
    must not be read as "no vulnerabilities".
    """
    command = ["npm", "audit", "--json"]
    if omit_dev:
        command.append("--omit=dev")
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300, cwd=directory)
    if completed.returncode not in (0, 1):
        raise ScannerError(
            f"npm audit failed in {directory} (exit {completed.returncode}): "
            f"{completed.stderr.strip()[-600:]}"
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise ScannerError(f"npm audit produced no JSON in {directory}: {error}") from error

    findings: list[Finding] = []
    vulnerabilities = payload.get("vulnerabilities", {})
    for name, entry in sorted(vulnerabilities.items()):
        # A `via` string means "vulnerable through this other package"; the
        # advisory itself is reported under that package, so it is not duplicated.
        for via in entry.get("via", []):
            if not isinstance(via, dict):
                continue
            advisory_id = (via.get("url") or "").rsplit("/", 1)[-1] or f"{name}-unknown"
            findings.append(
                Finding(
                    kind="dependency",
                    key=f"dep:npm/{name.lower()}/{advisory_id}".lower(),
                    where=f"{name} {entry.get('range', '?')}",
                    rule=f"npm/{advisory_id}",
                    message=f"{(via.get('severity') or entry.get('severity', 'unknown')).upper()}: "
                    f"{via.get('title', 'no title')}",
                )
            )
    count = ((payload.get("metadata") or {}).get("dependencies") or {}).get("total", 0)
    return findings, count


# --------------------------------------------------------------------------- #
# SAST
# --------------------------------------------------------------------------- #


def _iter_source(roots: tuple[str, ...], suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        if base.is_file():
            files.append(base)
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            files.append(path)
    return files


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def sast_python() -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    files = _iter_source(PYTHON_SOURCE_ROOTS, (".py",))
    for path in files:
        relative = _relative(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError as error:
            raise ScannerError(f"{relative} does not parse, so SAST cannot read it: {error}") from error
        findings.extend(_python_rules(tree, relative, path.read_text(encoding="utf-8")))
    return findings, len(files)


def _dotted(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - unparse is best-effort for a message
        return "<expression>"


def _python_rules(tree: ast.AST, relative: str, source: str) -> list[Finding]:
    out: list[Finding] = []
    allow_sql_rule = not relative.startswith(SQL_RULE_SKIP_PREFIX)

    def evidence(node: ast.AST) -> str:
        return ast.get_source_segment(source, node) or _dotted(node)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        site = evidence(node)

        if name in {"eval", "exec", "compile"}:
            out.append(
                _sast(
                    relative, node.lineno, "py-dynamic-exec",
                    f"{name}() executes a string as code", site,
                )
            )

        if name in {"os.system", "os.popen"}:
            out.append(
                _sast(relative, node.lineno, "py-os-command", f"{name}() runs a shell command", site)
            )

        if name.startswith("subprocess.") or name.startswith("asyncio.create_subprocess_"):
            for keyword in node.keywords:
                if keyword.arg == "shell" and not _is_false(keyword.value):
                    out.append(
                        _sast(
                            relative, node.lineno, "py-shell-true",
                            f"{name}(shell=...) lets a metacharacter reach a shell", site,
                        )
                    )

        if (
            name in {"hashlib.md5", "hashlib.sha1"}
            or (
                name == "hashlib.new"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).lower() in {"md5", "sha1"}
            )
        ):
            out.append(
                _sast(
                    relative, node.lineno, "py-weak-hash",
                    f"{name} is a broken hash for security use", site,
                )
            )

        if name in {"pickle.load", "pickle.loads"}:
            out.append(
                _sast(
                    relative, node.lineno, "py-unsafe-deserialise",
                    f"{name} runs arbitrary code from its input", site,
                )
            )

        for keyword in node.keywords:
            if keyword.arg in {"verify", "verify_ssl"} and _is_false(keyword.value):
                out.append(
                    _sast(
                        relative, node.lineno, "py-tls-verify-off",
                        f"{name} disables certificate verification", site,
                    )
                )

        if allow_sql_rule and name.split(".")[-1] in {"execute", "executemany", "text", "exec_driver_sql"}:
            for argument in node.args:
                if _sql_interpolates(argument):
                    out.append(
                        _sast(
                            relative, node.lineno, "py-sql-interpolation",
                            "SQL is built by interpolation instead of bound parameters", site,
                        )
                    )
    return out


def _sast(relative: str, lineno: int, rule: str, message: str, evidence: str = "") -> Finding:
    """Build a SAST finding whose baseline key identifies *this site*, not just its file.

    Without the fingerprint, baselining one accepted interpolation in a file would
    silently accept the next one a later work package adds to the same file — the
    baseline would grow into a per-file exemption. The digest is over the offending
    source text, so moving the code keeps the exemption and changing it does not.
    """
    digest = hashlib.sha256(evidence.encode()).hexdigest()[:12] if evidence else "0"
    return Finding(
        kind="sast",
        key=f"sast:{rule}:{relative}:{digest}".lower(),
        where=f"{relative}:{lineno}",
        rule=rule,
        message=message,
    )


def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


_SQL_KEYWORD = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|GRANT|REVOKE|CREATE|ALTER)\b", re.I)


_SAFE_CASTS = {"int", "float", "bool"}


def _is_safe_interpolant(node: ast.AST) -> bool:
    """Is this value incapable of carrying SQL syntax into the statement?

    A literal, an attribute, an ALL-CAPS name (the module-constant convention this
    repo uses for `APP_ROLE`/`HNSW_INDEX`), or an `int()`/`float()`/`bool()` cast of
    one. Anything else -- a request field, a local variable, a `join()` -- can, and
    `f"... WHERE x = {user_input}"` is exactly the pattern this rule exists for.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Attribute):
        return True
    if isinstance(node, ast.Name):
        return node.id.isupper()
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SAFE_CASTS:
        # A cast to a numeric type produces a number whatever it was given, so the
        # result cannot carry SQL syntax. This is why `vector({int(dim)})` is fine.
        return True
    return False


def _sql_interpolates(node: ast.AST) -> bool:
    """True when a SQL statement is assembled from a value that could carry SQL."""
    if isinstance(node, ast.JoinedStr):
        literal = "".join(
            value.value for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
        if not _SQL_KEYWORD.search(literal):
            return False
        return any(
            isinstance(value, ast.FormattedValue) and not _is_safe_interpolant(value.value)
            for value in node.values
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return isinstance(node.func.value, ast.Constant) and bool(
            _SQL_KEYWORD.search(str(node.func.value.value))
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return isinstance(node.left, ast.Constant) and bool(
            _SQL_KEYWORD.search(str(node.left.value))
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sql_interpolates(node.left) or _sql_interpolates(node.right)
    return False


def sast_javascript() -> tuple[list[Finding], int]:
    """Regex SAST over JavaScript/TypeScript source.

    Matched against the whole file rather than line by line, because the one
    legitimate `innerHTML` in this tree puts its string on the line *after* the `=`,
    and a per-line rule reads that as a non-literal assignment. Comment-only lines
    are skipped: `app.tsx` and `markdown.ts` discuss `innerHTML` in prose, and a rule
    that fires on documentation is a rule that gets baselined until it means nothing.
    """
    findings: list[Finding] = []
    files = _iter_source(JS_SOURCE_ROOTS, (".ts", ".tsx", ".mjs", ".js", ".jsx"))
    for path in files:
        relative = _relative(path)
        text = path.read_text(encoding="utf-8")

        for rule, pattern in JS_SINK_PATTERNS:
            for match in pattern.finditer(text):
                lineno = _line_of(text, match.start())
                if _is_comment_line(text, lineno):
                    continue
                findings.append(
                    _sast(relative, lineno, rule, f"{pattern.pattern} reaches an HTML/JS sink", match.group(0))
                )

        for match in JS_HTML_ASSIGN.finditer(text):
            lineno = _line_of(text, match.start())
            if _is_comment_line(text, lineno):
                continue
            if not _is_literal_markup(match.group(2)):
                findings.append(
                    _sast(
                        relative, lineno, "js-inner-html",
                        f".{match.group(1)} is assigned non-literal markup", match.group(0),
                    )
                )
    return findings, len(files)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_comment_line(text: str, lineno: int) -> bool:
    lines = text.splitlines()
    if not 1 <= lineno <= len(lines):
        return False
    return lines[lineno - 1].strip().startswith(("//", "*", "/*"))


def _is_literal_markup(rhs: str) -> bool:
    """A quoted string with no interpolation is authored markup, not untrusted data.

    `loader.ts` assigns an inline SVG as a concatenation of single-quoted literal
    lines; nothing in it can carry attacker data, so it is markup by construction.
    A template literal with `${...}`, or an expression that starts with anything but
    a quote, is not literal and is reported.
    """
    rhs = rhs.strip()
    if not rhs or rhs[0] not in "\"'`":
        return False
    return "${" not in rhs


def sast_secrets() -> tuple[list[Finding], int]:
    """Look for live credential shapes in tracked source.

    The matched text is never printed or included in the finding: a scanner that
    echoes the secret it found writes it into a CI log, which is a durable,
    world-readable copy of the very thing it flagged.
    """
    findings: list[Finding] = []
    files = _iter_source(
        SECRET_SCAN_ROOTS,
        (".py", ".ts", ".tsx", ".mjs", ".js", ".yml", ".yaml", ".json", ".toml", ".cfg", ".txt", ".md", ""),
    )
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as error:
            raise ScannerError(f"cannot read {_relative(path)}: {error}") from error
        for rule, pattern in CREDENTIAL_SHAPES:
            for match in pattern.finditer(text):
                findings.append(
                    Finding(
                        kind="secret",
                        key=f"secret:{rule}:{_relative(path)}".lower(),
                        where=f"{_relative(path)}:{text[: match.start()].count(chr(10)) + 1}",
                        rule=rule,
                        message="a value here has the shape of a live credential (not printed)",
                    )
                )
    return findings, len(files)


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #


def load_baseline(path: Path = BASELINE_PATH) -> list[dict]:
    if not path.is_file():
        raise ScannerError(f"baseline {path} is missing; the gate cannot distinguish new from known")
    payload = json.loads(path.read_text())
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ScannerError(f"{path} has no 'entries' list")
    return entries


def baseline_problems(entries: list[dict], today: str) -> list[str]:
    problems = []
    seen = set()
    for entry in entries:
        key = entry.get("key")
        if not key:
            problems.append(f"baseline entry without a key: {entry!r}")
            continue
        if key in seen:
            problems.append(f"baseline entry duplicated: {key}")
        seen.add(key)
        if not (entry.get("why") or "").strip():
            problems.append(f"baseline entry {key} carries no reason")
        review_by = entry.get("review_by") or ""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", review_by):
            problems.append(f"baseline entry {key} needs review_by as YYYY-MM-DD")
        elif review_by < today:
            problems.append(f"baseline entry {key} passed its review date {review_by}; re-review it")
        if entry.get("scope") not in {"shipped", "dev"}:
            problems.append(f"baseline entry {key} needs scope 'shipped' or 'dev'")
    return problems


def apply_baseline(findings: list[Finding], entries: list[dict]) -> tuple[list[Finding], list[str]]:
    known = {entry["key"] for entry in entries if entry.get("key")}
    open_findings = [finding for finding in findings if finding.key not in known]
    current = {finding.key for finding in findings}
    stale = [
        f"baseline entry {entry['key']} matches no current finding; delete it so the "
        "baseline cannot become a permanent permission"
        for entry in entries
        if entry.get("key") and entry["key"] not in current
    ]
    return open_findings, stale


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run(transport=None, today: str | None = None) -> int:
    today = today or time.strftime("%Y-%m-%d")

    dep_findings, dep_scanned = scan_python_dependencies(transport=transport)
    npm_findings, npm_scanned = scan_npm_trees()
    py_sast, py_files = sast_python()
    js_sast, js_files = sast_javascript()
    secret_findings, secret_files = sast_secrets()

    if py_files < MIN_PYTHON_FILES:
        raise ScannerError(f"SAST read {py_files} Python files, floor is {MIN_PYTHON_FILES}")
    if js_files < MIN_JS_FILES:
        raise ScannerError(f"SAST read {js_files} JavaScript files, floor is {MIN_JS_FILES}")

    findings = [*dep_findings, *npm_findings, *py_sast, *js_sast, *secret_findings]
    scanned = {
        **dep_scanned,
        **npm_scanned,
        "python_files": py_files,
        "js_files": js_files,
        "secret_files": secret_files,
    }

    entries = load_baseline()
    problems = baseline_problems(entries, today)
    open_findings, stale = apply_baseline(findings, entries)

    print("security scan scanned: " + ", ".join(f"{k}={v}" for k, v in sorted(scanned.items())))
    print(f"  findings: {len(findings)} total, {len(entries)} baselined, {len(open_findings)} open")

    baselined = {entry["key"]: entry for entry in entries if entry.get("key")}
    for key, entry in baselined.items():
        for finding in [f for f in findings if f.key == key]:
            print(f"  baselined [{entry['scope']}]: {finding.line()}")
            print(f"      why: {entry['why']} (review by {entry['review_by']})")

    failures = list(problems) + list(stale)
    for finding in open_findings:
        failures.append(f"new finding [{finding.scope}]: {finding.line()}")

    if failures:
        print(f"\nsecurity scan FAILED ({len(failures)} problem(s)):", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        _emit_failures(failures)
        return EXIT_FINDINGS

    print("\nsecurity scan: clean (every current finding is baselined and in date)")
    return EXIT_OK


def _emit_failures(failures: list[str]) -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    # Job logs need admin rights on this repository; annotations are the readable
    # channel, which is why the workflow uses them everywhere else too.
    for failure in failures:
        print(f"::error::{failure}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--github", action="store_true", help="emit ::error annotations")
    arguments = parser.parse_args(argv)
    if arguments.github:
        os.environ["GITHUB_ACTIONS"] = "true"
    try:
        return run()
    except ScannerError as error:
        print(f"security scan could not complete: {error}", file=sys.stderr)
        _emit_failures([f"the security scanner failed to run: {error}"])
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
