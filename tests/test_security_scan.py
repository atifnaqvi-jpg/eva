"""SEC-13: dependency scanning and SAST are a working gate, not a workflow file.

The point of these tests is the same one `test_ci_workflow.py` makes about the
browser matrix: a scanner that runs, finds nothing, and exits 0 is indistinguishable
from a scanner that read nothing. So every rule is exercised on source that *must*
trip it, every rule is exercised on source that must not, and the gate's own
decision logic (new finding / stale entry / expired review) is driven directly.

No test here needs the network: the OSV transport and the dependency resolution are
seams, and the baseline logic takes `today` as an argument so an expiry can be
tested without waiting for it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "security_scan.py"
BASELINE = ROOT / "scripts" / "security_baseline.json"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _load():
    """Import the script as a module.

    Registered in `sys.modules` first: `@dataclass` resolves annotations through
    `sys.modules[cls.__module__]` and raises AttributeError on a module that was
    never registered, which is a confusing way to fail at collection time.
    """
    name = "eva_security_scan"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


scan = _load()


def _python_rules(source: str, relative: str = "backend/api/app/x.py") -> list:
    import ast

    return scan._python_rules(ast.parse(source), relative, source)


def _rules(findings: list) -> set[str]:
    return {finding.rule for finding in findings}


# --------------------------------------------------------------------------- #
# The SAST rules fire on the thing they claim to catch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source,rule",
    [
        pytest.param("eval(user_input)\n", "py-dynamic-exec", id="eval"),
        pytest.param("exec(payload)\n", "py-dynamic-exec", id="exec"),
        pytest.param("compile(text, '<s>', 'exec')\n", "py-dynamic-exec", id="compile"),
        pytest.param("import os\nos.system(cmd)\n", "py-os-command", id="os-system"),
        pytest.param("import os\nos.popen(cmd)\n", "py-os-command", id="os-popen"),
        pytest.param(
            "import subprocess\nsubprocess.run(cmd, shell=True)\n", "py-shell-true", id="shell-true"
        ),
        pytest.param(
            "import subprocess\nsubprocess.run(cmd)\n", None, id="shell-default-is-fine"
        ),
        pytest.param(
            "import subprocess\nsubprocess.run(cmd, shell=False)\n", None, id="shell-false-is-fine"
        ),
        pytest.param("import hashlib\nhashlib.md5(b'x')\n", "py-weak-hash", id="md5"),
        pytest.param("import hashlib\nhashlib.sha1(b'x')\n", "py-weak-hash", id="sha1"),
        pytest.param("import hashlib\nhashlib.new('md5')\n", "py-weak-hash", id="new-md5"),
        pytest.param(
            "import hashlib\nhashlib.sha256(b'x')\n", None, id="sha256-is-fine"
        ),
        pytest.param("import pickle\npickle.loads(blob)\n", "py-unsafe-deserialise", id="pickle"),
        pytest.param(
            "import httpx\nhttpx.get(url, verify=False)\n", "py-tls-verify-off", id="verify-false"
        ),
        pytest.param(
            "import httpx\nhttpx.get(url, verify=True)\n", None, id="verify-true-is-fine"
        ),
        pytest.param(
            'session.execute(text(f"SELECT * FROM t WHERE a = {value}"))\n',
            "py-sql-interpolation",
            id="sql-fstring",
        ),
        pytest.param(
            'session.execute("SELECT * FROM t WHERE a = %s" % value)\n',
            "py-sql-interpolation",
            id="sql-percent",
        ),
        pytest.param(
            'session.execute("SELECT * FROM t WHERE a = {}".format(value))\n',
            "py-sql-interpolation",
            id="sql-format",
        ),
        # Bound parameters are the fix, and must be silent -- otherwise the rule
        # would report the code that already does the right thing.
        pytest.param(
            'session.execute(text("SELECT * FROM t WHERE a = :a"), {"a": value})\n',
            None,
            id="sql-bound-param-is-fine",
        ),
        # A constant interpolated into DDL is not attacker data, and the migration
        # case below relies on it.
        pytest.param(
            'session.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))\n',
            None,
            id="sql-module-constant-is-fine",
        ),
    ],
)
def test_python_rule_matches_only_what_it_should(source: str, rule: str | None) -> None:
    rules = _rules(_python_rules(source))
    if rule is None:
        assert rules == set(), f"expected no findings, got {rules}"
    else:
        assert rule in rules, f"expected {rule}, got {rules}"


def test_the_sql_rule_is_skipped_for_migrations_but_not_for_app_code() -> None:
    """Migrations interpolate GRANT/REVOKE identifiers by design; app code does not.

    Without this skip the rule reports every migration (they are all
    `op.execute(f"GRANT ... TO {APP_ROLE}")`) and gets switched off.
    """
    source = 'session.execute(text(f"SELECT * FROM t WHERE a = {value}"))\n'
    assert _rules(_python_rules(source, "backend/api/migrations/versions/0001_x.py")) == set()
    assert "py-sql-interpolation" in _rules(_python_rules(source, "backend/api/app/x.py"))


@pytest.mark.parametrize(
    "source,rule",
    [
        pytest.param("eval(code)\n", "js-eval", id="eval"),
        pytest.param("const f = new Function('a', body)\n", "js-new-function", id="new-function"),
        pytest.param("document.write(html)\n", "js-document-write", id="document-write"),
        pytest.param("<div dangerouslySetInnerHTML={{__html: x}} />\n", "js-raw-html-prop", id="raw-html-prop"),
        pytest.param("node.innerHTML = userContent\n", "js-inner-html", id="inner-html-expression"),
        pytest.param("node.innerHTML = `<b>${name}</b>`\n", "js-inner-html", id="inner-html-template"),
        pytest.param('node.innerHTML = "<svg><path d=\\"M0\\"/></svg>"\n', None, id="inner-html-literal-is-fine"),
        pytest.param("// this file never uses innerHTML\n", None, id="comment-prose-is-fine"),
    ],
)
def test_javascript_rule_matches_only_what_it_should(
    source: str, rule: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The JS rules are regex over source, so they are driven through the real reader.

    Patching the file reader rather than calling a helper keeps this test honest: it
    exercises `sast_javascript`'s own comment-skipping and offset-to-line arithmetic.
    """
    target = tmp_path / "probe.ts"
    target.write_text(source)

    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "JS_SOURCE_ROOTS", (".",))
    monkeypatch.setattr(scan, "_relative", lambda path: path.name)

    findings, files = scan.sast_javascript()
    assert files == 1
    if rule is None:
        assert findings == [], [f.line() for f in findings]
    else:
        assert rule in _rules(findings), [f.line() for f in findings]


def test_a_template_literal_with_interpolation_is_not_literal_markup() -> None:
    """The distinction the `innerHTML` rule turns on.

    `loader.ts` has a genuine multi-line literal SVG; the whole rule is worthless if
    it either flags that or accepts `` `...${x}` ``.
    """
    assert scan._is_literal_markup("'<svg></svg>'") is True
    assert scan._is_literal_markup("\"<b>x</b>\"") is True
    assert scan._is_literal_markup("`<b>${name}</b>`") is False
    assert scan._is_literal_markup("userContent") is False


# --------------------------------------------------------------------------- #
# Credential shapes
# --------------------------------------------------------------------------- #


def test_a_live_looking_credential_is_reported_without_being_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The finding must not quote the value: a scanner that echoes a secret writes it
    into a CI log, which is a durable, readable copy of the thing it flagged."""
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    (tmp_path / "leak.py").write_text(f'AWS = "{secret}"\n')

    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "SECRET_SCAN_ROOTS", (".",))
    monkeypatch.setattr(scan, "_relative", lambda path: path.name)

    findings, files = scan.sast_secrets()
    assert files == 1
    assert [f.rule for f in findings] == ["aws_access_key"]
    assert secret not in findings[0].line()
    assert secret not in capsys.readouterr().out


def test_the_secret_scan_finds_nothing_in_this_repository() -> None:
    """A real assertion about the tree, not a rule test: no file here carries a live
    credential shape. `test_secret_scan_roots_cover_the_shipped_code` keeps it honest."""
    findings, files = scan.sast_secrets()
    assert files > 100
    assert [f.line() for f in findings] == []


def test_the_secret_scan_roots_cover_the_shipped_code() -> None:
    """If the roots shrank, the previous test would pass by reading nothing."""
    for required in (
        "backend/api/app",
        "backend/api/migrations",
        "backend/worker/worker",
        "backend/embed",
        "dashboard/app",
        "backend/api/widget/src",
        ".github",
    ):
        assert required in scan.SECRET_SCAN_ROOTS, f"{required} is not scanned for credentials"


# --------------------------------------------------------------------------- #
# Dependency scanning
# --------------------------------------------------------------------------- #


def test_every_shipped_requirements_file_is_scanned() -> None:
    """The three images install three files; a fourth added later must be listed."""
    on_disk = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "backend").rglob("requirements.txt")
    )
    assert on_disk, "no requirements files found at all"
    assert sorted(scan.PYTHON_REQUIREMENTS) == on_disk, (
        "a requirements file exists that the scanner does not read: "
        f"{set(on_disk) - set(scan.PYTHON_REQUIREMENTS)}"
    )
    assert "requirements-dev.txt" not in scan.PYTHON_REQUIREMENTS


def test_both_javascript_trees_are_audited() -> None:
    assert sorted(scan.NPM_TREES) == ["backend/api/widget", "dashboard"]
    for tree in scan.NPM_TREES:
        assert (ROOT / tree / "package-lock.json").is_file(), f"{tree} has no lockfile"
        assert (ROOT / tree / "package.json").is_file(), f"{tree} has no manifest"


def test_an_advisory_is_attributed_to_the_file_that_installs_it() -> None:
    """A finding must say which requirements file pulls the package in.

    Driven with stub resolution + a stub transport, so it tests the attribution
    logic rather than the network or today's advisory database.
    """

    def fake_resolve(relative: str):
        base = [(f"pkg{i}", "1.0") for i in range(scan.MIN_PACKAGES_PER_REQUIREMENTS)]
        if relative.endswith("worker/requirements.txt"):
            base.append(("vulnerable", "2.0"))
        return base

    def fake_transport(url: str, body: dict) -> dict:
        results = []
        for query in body["queries"]:
            if query["package"]["name"] == "vulnerable":
                results.append(
                    {"vulns": [{"id": "GHSA-test", "summary": "s", "database_specific": {"severity": "HIGH"}}]}
                )
            else:
                results.append({})
        return {"results": results}

    original = scan.resolve_python_closure
    scan.resolve_python_closure = fake_resolve
    try:
        findings, scanned = scan.scan_python_dependencies(transport=fake_transport)
    finally:
        scan.resolve_python_closure = original

    assert scanned["requirements_files"] == 3
    assert [f.key for f in findings] == ["dep:pypi/vulnerable/ghsa-test"]
    assert "worker" in findings[0].where
    assert "api/requirements" not in findings[0].where


def test_an_unparseable_pip_report_is_an_error_not_an_empty_scan() -> None:
    with pytest.raises(scan.ScannerError):
        scan._packages_from_report({"install": [{"metadata": {}}]}, "x/requirements.txt")
    with pytest.raises(scan.ScannerError):
        scan._packages_from_report({"install": []}, "x/requirements.txt")


def test_osv_is_never_called_with_an_empty_package_list() -> None:
    """An empty query returning no results would read as a clean dependency tree."""
    with pytest.raises(scan.ScannerError):
        scan.query_osv([], transport=lambda url, body: {"results": []})


def test_a_short_result_set_from_osv_is_an_error() -> None:
    """A partial answer silently drops packages from the scan."""
    packages = [("a", "1"), ("b", "2")]
    with pytest.raises(scan.ScannerError):
        scan.query_osv(packages, transport=lambda url, body: {"results": [{}]})


def test_osv_failure_is_an_error_not_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worst failure mode for this gate is "advisory database unreachable" being
    reported as "no vulnerabilities", so a transport failure must raise."""
    attempts = {"n": 0}

    def boom(url: str, body: dict) -> dict:
        attempts["n"] += 1
        raise OSError("network down")

    monkeypatch.setattr(scan.time, "sleep", lambda seconds: None)
    with pytest.raises(scan.ScannerError) as raised:
        scan.query_osv([("a", "1")], transport=boom)
    assert attempts["n"] == scan.OSV_ATTEMPTS
    assert "unreachable" in str(raised.value)


# --------------------------------------------------------------------------- #
# The gate's decision logic
# --------------------------------------------------------------------------- #


def _finding(key: str = "k", rule: str = "r") -> "scan.Finding":
    return scan.Finding(kind="sast", key=key, where="f.py:1", rule=rule, message="m")


def _entry(key: str, **overrides) -> dict:
    entry = {"key": key, "scope": "shipped", "why": "because", "review_by": "2030-01-01"}
    entry.update(overrides)
    return entry


def test_a_new_finding_is_open() -> None:
    open_findings, stale = scan.apply_baseline([_finding("new")], [])
    assert [f.key for f in open_findings] == ["new"]
    assert stale == []


def test_a_baselined_finding_is_not_open() -> None:
    open_findings, stale = scan.apply_baseline([_finding("known")], [_entry("known")])
    assert open_findings == []
    assert stale == []


def test_a_baseline_entry_that_matches_nothing_is_stale() -> None:
    """Otherwise a fixed problem leaves a permanent hole behind it."""
    open_findings, stale = scan.apply_baseline([], [_entry("gone")])
    assert open_findings == []
    assert len(stale) == 1 and "matches no current finding" in stale[0]


def test_baseline_entries_need_a_reason_a_scope_and_a_date() -> None:
    problems = scan.baseline_problems(
        [
            {"key": "no-why", "scope": "shipped", "review_by": "2030-01-01"},
            {"key": "no-scope", "why": "x", "review_by": "2030-01-01"},
            {"key": "no-date", "why": "x", "scope": "shipped"},
            {"key": "bad-date", "why": "x", "scope": "shipped", "review_by": "next year"},
            {"key": "dup", "why": "x", "scope": "shipped", "review_by": "2030-01-01"},
            {"key": "dup", "why": "x", "scope": "shipped", "review_by": "2030-01-01"},
        ],
        today="2026-11-17",
    )
    joined = "\n".join(problems)
    assert "no-why carries no reason" in joined
    assert "no-scope needs scope" in joined
    assert "no-date needs review_by" in joined
    assert "bad-date needs review_by" in joined
    assert "duplicated: dup" in joined


def test_an_expired_review_date_fails_the_gate() -> None:
    """The mechanism that stops the baseline becoming a permanent permission."""
    entries = [_entry("old", review_by="2026-11-16")]
    assert scan.baseline_problems(entries, today="2026-11-17") != []
    assert scan.baseline_problems(entries, today="2026-11-16") == []


def test_the_shipped_baseline_is_in_date_and_complete() -> None:
    today = "2026-11-17"
    entries = scan.load_baseline()
    assert entries, "the baseline is empty; every finding would then be 'new'"
    assert scan.baseline_problems(entries, today=today) == []
    assert all(e["key"] == e["key"].lower() for e in entries), "keys must be lowercase to match findings"


def test_a_missing_baseline_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(scan.ScannerError):
        scan.load_baseline(tmp_path / "absent.json")


# --------------------------------------------------------------------------- #
# The baseline cannot hide a second problem in an already-baselined file
# --------------------------------------------------------------------------- #


def test_baselining_one_sql_site_does_not_baseline_the_next_one_in_that_file() -> None:
    """The fingerprint in the SAST key is the whole reason this works.

    Without it the key would be `sast:py-sql-interpolation:<file>`, so accepting one
    reviewed interpolation would silently accept any later one added to the same
    file — a per-file exemption wearing a per-finding costume.
    """
    first = 'session.execute(text(f"SELECT a FROM t WHERE x = {one}"))\n'
    second = 'session.execute(text(f"SELECT b FROM t WHERE y = {two}"))\n'
    relative = "backend/api/app/botconfig/router.py"

    first_keys = {f.key for f in _python_rules(first, relative)}
    second_keys = {f.key for f in _python_rules(second, relative)}
    assert first_keys and second_keys
    assert first_keys.isdisjoint(second_keys)

    open_findings, stale = scan.apply_baseline(_python_rules(second, relative), [_entry(next(iter(first_keys)))])
    assert len(open_findings) == 1, "a different statement in a baselined file must still be open"
    assert stale


def test_the_current_baseline_covers_exactly_the_findings_it_claims() -> None:
    """Every baseline key must name a rule that still exists.

    A typo'd or retired key would look like a stale entry at scan time, but the
    clearer failure is here: the key's rule must be one the scanner emits.
    """
    known_rules = set()
    source = SCRIPT.read_text()
    for rule in (
        "py-dynamic-exec",
        "py-os-command",
        "py-shell-true",
        "py-weak-hash",
        "py-unsafe-deserialise",
        "py-tls-verify-off",
        "py-sql-interpolation",
        "js-eval",
        "js-new-function",
        "js-document-write",
        "js-raw-html-prop",
        "js-inner-html",
        "aws_access_key",
        "github_token",
        "provider_api_key",
        "age_identity",
        "private_key_block",
    ):
        assert rule in source, f"rule {rule} is not implemented"
        known_rules.add(rule)

    for entry in scan.load_baseline():
        key = entry["key"]
        if key.startswith("dep:"):
            continue
        rule = key.split(":")[1]
        assert rule in known_rules, f"baseline entry {key} names an unknown rule"


# --------------------------------------------------------------------------- #
# CI wiring
# --------------------------------------------------------------------------- #


def test_ci_runs_the_security_scan_as_a_blocking_job() -> None:
    """§19.1 asks for the scan *in CI*; a scanner nothing invokes is a local tool."""
    workflow = WORKFLOW.read_text()
    assert "security_scan.py" in workflow
    assert "security:" in workflow, "there is no `security` job"

    job = workflow.split("security:", 1)[1]
    job = job.split("\nruns-on:", 1)[0] if "\nruns-on:" in job else job
    assert "security_scan.py" in job


def test_the_security_job_is_not_allowed_to_fail_quietly() -> None:
    """A `continue-on-error` on the scan step would make the gate advisory."""
    workflow = WORKFLOW.read_text()
    start = workflow.index("security:")
    tail = workflow[start:]
    next_job = tail.find("\n  widget-browser:")
    job = tail[: next_job if next_job != -1 else len(tail)]
    assert "continue-on-error" not in job


def test_ci_installs_the_scanner_requirements_before_running_it() -> None:
    workflow = WORKFLOW.read_text()
    start = workflow.index("security:")
    job = workflow[start:]
    install = job.find("pip install")
    run = job.find("security_scan.py")
    assert install != -1, "the security job installs nothing; the scanner needs pip to resolve"
    assert install < run, "the scan runs before its dependencies are installed"


def test_the_scanner_needs_no_installed_project_dependencies() -> None:
    """The CI job installs only `requirements-dev.txt`, never the app.

    That is deliberate: a scanner that imports app code cannot run before the app is
    installable, and a dependency problem would then mask a security problem. This
    asserts the scanner only imports the standard library.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    stdlib = set(sys.stdlib_module_names)
    third_party = {name for name in imported if name not in stdlib and name != "__future__"}
    assert third_party == set(), f"the scanner imports non-stdlib modules: {third_party}"


def test_the_scan_runs_from_a_checkout_without_node_modules(tmp_path: Path) -> None:
    """The CI checkout has no `node_modules`; `npm audit` must still read the locks.

    Driven on a copy of just the manifests and lockfiles, which is the state the job
    starts in.
    """
    for tree in scan.NPM_TREES:
        target = tmp_path / tree
        target.mkdir(parents=True)
        for name in ("package.json", "package-lock.json"):
            (target / name).write_bytes((ROOT / tree / name).read_bytes())

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
        findings, scanned = scan.scan_npm_trees()
    finally:
        monkeypatch.undo()

    assert scanned["npm_trees"] == 2
    assert all(f.kind == "dependency" for f in findings)


@pytest.mark.parametrize("job", ["test", "security"])
def test_the_widget_and_dashboard_lockfiles_are_present_for_the_audit(job: str) -> None:
    """`npm audit` reads a lockfile, so a checkout without one cannot be scanned."""
    for tree in ("backend/api/widget", "dashboard"):
        assert (ROOT / tree / "package-lock.json").is_file()


def test_the_scanner_exits_2_when_it_cannot_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 2, not 1 and not 0: "the scanner broke" is a different fact from "found
    something", and CI must not render it as a pass or as a security finding."""
    monkeypatch.setattr(scan, "run", lambda: (_ for _ in ()).throw(scan.ScannerError("osv is down")))
    assert scan.main([]) == scan.EXIT_ERROR


def test_the_scanner_exits_1_on_an_open_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate's whole purpose. Driven through `run`'s decision path with the scans
    stubbed out, so it tests the exit code rather than the network."""
    monkeypatch.setattr(scan, "scan_python_dependencies", lambda transport=None: ([], {"a": 1}))
    monkeypatch.setattr(scan, "scan_npm_trees", lambda: ([], {"npm_trees": 2}))
    monkeypatch.setattr(scan, "sast_python", lambda: ([_finding("unknown-site")], 100))
    monkeypatch.setattr(scan, "sast_javascript", lambda: ([], 20))
    monkeypatch.setattr(scan, "sast_secrets", lambda: ([], 200))
    monkeypatch.setattr(scan, "load_baseline", lambda *a, **k: [])

    assert scan.run() == scan.EXIT_FINDINGS


def test_the_scanner_exits_0_when_every_finding_is_baselined(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "scan_python_dependencies", lambda transport=None: ([], {"a": 1}))
    monkeypatch.setattr(scan, "scan_npm_trees", lambda: ([], {"npm_trees": 2}))
    monkeypatch.setattr(scan, "sast_python", lambda: ([_finding("known-site")], 100))
    monkeypatch.setattr(scan, "sast_javascript", lambda: ([], 20))
    monkeypatch.setattr(scan, "sast_secrets", lambda: ([], 200))
    monkeypatch.setattr(scan, "load_baseline", lambda *a, **k: [_entry("known-site")])

    assert scan.run() == scan.EXIT_OK


def test_the_scan_refuses_to_report_a_clean_result_for_an_empty_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floors that stop "found nothing" meaning "read nothing"."""
    monkeypatch.setattr(scan, "scan_python_dependencies", lambda transport=None: ([], {}))
    monkeypatch.setattr(scan, "scan_npm_trees", lambda: ([], {"npm_trees": 2}))
    monkeypatch.setattr(scan, "sast_python", lambda: ([], 3))
    monkeypatch.setattr(scan, "sast_javascript", lambda: ([], 20))
    monkeypatch.setattr(scan, "sast_secrets", lambda: ([], 200))
    monkeypatch.setattr(scan, "load_baseline", lambda *a, **k: [])

    with pytest.raises(scan.ScannerError) as raised:
        scan.run()
    assert "floor" in str(raised.value)


def test_the_scan_refuses_a_short_javascript_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "scan_python_dependencies", lambda transport=None: ([], {}))
    monkeypatch.setattr(scan, "scan_npm_trees", lambda: ([], {"npm_trees": 2}))
    monkeypatch.setattr(scan, "sast_python", lambda: ([], 200))
    monkeypatch.setattr(scan, "sast_javascript", lambda: ([], 1))
    monkeypatch.setattr(scan, "sast_secrets", lambda: ([], 200))
    monkeypatch.setattr(scan, "load_baseline", lambda *a, **k: [])

    with pytest.raises(scan.ScannerError) as raised:
        scan.run()
    assert "JavaScript" in str(raised.value)
