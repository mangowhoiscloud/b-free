"""Static detector for FR-2. Walks an app repository, applies the rule table, returns a report.

Deterministic: files are visited in sorted order, findings are sorted, IDs are content hashes,
and the report carries no clock value. Paths in the output are relative to the scanned root.
Nothing here imports an LLM client (NFR-5); ``미분류`` items are only listed with evidence.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from .rules import (
    AWS_SERVICE_NAMES,
    CLIENT_FACTORIES,
    CODE_CLASSES,
    CONFIG,
    ENV_DSN_NAME,
    ENV_ENDPOINT_NAME,
    GO,
    JAVA,
    JS,
    KMS_CALLS,
    KMS_UNIQUE_CALLS,
    NON_CLOUD_HOST,
    PYTHON,
    RULES,
    RULES_BY_ID,
    RULES_VERSION,
    SCRIPT,
    SM_CALLS,
    SM_UNIQUE_CALLS,
    SPEC_HOST,
    TERRAFORM,
    TERRAFORM_BLOCK,
    TERRAFORM_EXACT,
    TERRAFORM_PREFIX,
    TIER_CODE,
    Rule,
    Tier,
)

SCHEMA_VERSION = "inventory/v1"
MAX_FILE_BYTES = 1024 * 1024
MAX_EVIDENCE = 200

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".terraform", ".next", "coverage", ".idea", ".vscode",
})
# Lock files repeat every dependency name; the manifest next to them is enough.
SKIP_FILES = frozenset({
    "uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum",
    "Cargo.lock", "composer.lock", "Gemfile.lock", ".terraform.lock.hcl",
})
EXT_CLASS: dict[str, str] = {
    ".py": PYTHON,
    ".js": JS, ".mjs": JS, ".cjs": JS, ".jsx": JS, ".ts": JS, ".tsx": JS, ".mts": JS, ".cts": JS,
    ".go": GO,
    ".java": JAVA, ".kt": JAVA, ".kts": JAVA,
    ".sh": SCRIPT, ".bash": SCRIPT, ".sql": SCRIPT,
    ".tf": TERRAFORM, ".tfvars": TERRAFORM, ".hcl": TERRAFORM,
    ".yaml": CONFIG, ".yml": CONFIG, ".json": CONFIG, ".toml": CONFIG, ".ini": CONFIG, ".cfg": CONFIG,
    ".conf": CONFIG, ".properties": CONFIG, ".env": CONFIG,
}
COMMENT_PREFIXES: dict[str, tuple[str, ...]] = {
    PYTHON: ("#",),
    JS: ("//", "/*", "*"),
    GO: ("//", "/*", "*"),
    JAVA: ("//", "/*", "*"),
    SCRIPT: ("#", "--"),
    TERRAFORM: ("#", "//", "/*", "*"),
    CONFIG: ("#", ";"),
}

_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL userinfo password: postgresql://user:<secret>@host
    (re.compile(r"(://[^/:@\s'\"]*:)[^@\s'\"]+@"), r"\1***@"),
    # key = value / key: value / "key": "value", where the key contains a secret word anywhere:
    # DB_PASSWORD=, aws_secret_access_key =, "password": "...", api_key:, SESSION_TOKEN= (audit B18)
    (re.compile(r"(?i)([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|credential)"
                r"[A-Za-z0-9_]*)(['\"]?\s*[=:]\s*['\"]?)(?![%${<*])[^\s'\",;)}]+"), r"\1\2***"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), r"\1****************"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*"), "-----BEGIN PRIVATE KEY----- ***"),
)


class Finding(BaseModel):
    id: str
    rule_id: str
    tier: str
    tier_code: str
    service: str
    kind: str
    file: str
    line: int
    evidence: str
    reason: str
    hardcoded: bool
    confidence: Literal["rule"] = "rule"


class InventoryReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    rules_version: str = RULES_VERSION
    root: str
    input_digest: str = Field(description="sha256 over (relative path, file sha256) of every scanned file")
    files_scanned: int
    files_skipped: int
    skipped: list[dict[str, str]] = Field(default_factory=list, description="candidate files not read, with reason")
    summary: dict[str, int]
    findings: list[Finding]
    unclassified: list[Finding]
    test_summary: dict[str, int] = Field(default_factory=dict, description="tier counts of test-only files")
    test_findings: list[Finding] = Field(
        default_factory=list, description="findings in test code (tests/, test_*.py, *_test.go, *.spec.ts ...); "
        "reported apart from the migration summary, never dropped")


def mask(text: str) -> str:
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return text


def _evidence(line: str) -> str:
    out = mask(line.strip())
    return out if len(out) <= MAX_EVIDENCE else out[: MAX_EVIDENCE - 1] + "…"


def file_class(path: PurePosixPath) -> str | None:
    name = path.name
    if name in SKIP_FILES:
        return None
    if name.startswith("Dockerfile") or name.endswith(".dockerfile") or name == "Containerfile":
        return CONFIG
    if name == ".env" or name.startswith(".env."):
        return CONFIG
    return EXT_CLASS.get(path.suffix.lower())


class _Collector:
    def __init__(self, relpath: str, fclass: str, lines: list[str]) -> None:
        self.relpath = relpath
        self.fclass = fclass
        self.lines = lines
        self.items: dict[str, Finding] = {}

    def add(self, rule: Rule, lineno: int, match_text: str, service: str | None = None) -> None:
        text = self.lines[lineno - 1] if 0 < lineno <= len(self.lines) else ""
        digest = hashlib.sha256(f"{rule.rule_id}\0{self.relpath}\0{lineno}\0{match_text}".encode()).hexdigest()
        fid = digest[:16]
        if fid in self.items:
            return
        hardcoded = rule.literal and self.fclass in CODE_CLASSES
        tier, reason = rule.tier, rule.reason
        if hardcoded and rule.kind == "dsn_literal":
            # A DSN literal in code cannot change per environment without editing the code (audit 37).
            tier = Tier.CODE_CHANGE
            reason = (rule.reason + " The literal sits in code, so moving it needs a code change"
                      " (read it from configuration).")
        self.items[fid] = Finding(
            id=fid,
            rule_id=rule.rule_id,
            tier=tier.value,
            tier_code=TIER_CODE[tier],
            service=service if rule.service == "*" else rule.service,
            kind=rule.kind,
            file=self.relpath,
            line=lineno,
            evidence=_evidence(text),
            reason=reason,
            hardcoded=hardcoded,
        )


def _str_const(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _PythonVisitor(ast.NodeVisitor):
    """Finds boto3/aioboto3/botocore clients, their calls and env-provided DSNs/endpoints."""

    def __init__(self, out: _Collector) -> None:
        self.out = out
        self.bound: dict[str, str] = {}  # unparsed receiver -> AWS service name

    def _client_service(self, call: ast.AST) -> tuple[str, ast.Call] | None:
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            return None
        if call.func.attr not in CLIENT_FACTORIES:
            return None
        service = _str_const(call.args[0]) if call.args else None
        if service is None:
            for kw in call.keywords:
                if kw.arg == "service_name":
                    service = _str_const(kw.value)
        if service is None:
            return None
        receiver = ast.unparse(call.func.value).lower()
        if service not in AWS_SERVICE_NAMES and "boto" not in receiver and "session" not in receiver:
            return None
        return service, call

    def _record_client(self, service: str, call: ast.Call) -> None:
        line = call.lineno
        if service == "secretsmanager":
            self.out.add(RULES_BY_ID["PY-SM-CLIENT"], line, service)
        elif service == "kms":
            self.out.add(RULES_BY_ID["PY-KMS-CLIENT"], line, service)
        elif service == "s3":
            endpoint = next((kw.value for kw in call.keywords if kw.arg == "endpoint_url"), None)
            if endpoint is None or (isinstance(endpoint, ast.Constant) and endpoint.value is None):
                self.out.add(RULES_BY_ID["PY-S3-ENDPOINT-DEFAULT"], line, service)
            elif _str_const(endpoint) is not None:
                self.out.add(RULES_BY_ID["PY-S3-ENDPOINT-LITERAL"], line, service)
            else:
                self.out.add(RULES_BY_ID["PY-S3-ENDPOINT-CONFIG"], line, service)
        else:
            self.out.add(RULES_BY_ID["PY-AWS-OTHER-CLIENT"], line, service, service=f"aws:{service}")

    def _bind(self, target: ast.AST, value: ast.AST) -> None:
        found = self._client_service(value)
        if found is None and isinstance(value, ast.Await):
            found = self._client_service(value.value)
        if found is not None:
            self.bound[ast.unparse(target)] = found[0]

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._bind(node.target, node.value)
        self.generic_visit(node)

    def _with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._bind(item.optional_vars, item.context_expr)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self._with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._with(node)

    def _env_name(self, name: str, lineno: int) -> None:
        if ENV_DSN_NAME.match(name):
            self.out.add(RULES_BY_ID["PY-ENV-DSN"], lineno, name)
        elif ENV_ENDPOINT_NAME.match(name):
            self.out.add(RULES_BY_ID["PY-ENV-ENDPOINT"], lineno, name)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        name = _str_const(node.slice)
        if name is not None:
            self._env_name(name, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        found = self._client_service(node)
        if found is not None:
            self._record_client(*found)
        # Env-variable names passed to any call (os.getenv, environ.get, project helpers).
        for arg in node.args:
            name = _str_const(arg)
            if name is not None:
                self._env_name(name, node.lineno)
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            receiver = node.func.value
            service: str | None = None
            nested = self._client_service(receiver)
            if nested is not None:
                service = nested[0]
            else:
                service = self.bound.get(ast.unparse(receiver))
            if (service == "secretsmanager" and attr in SM_CALLS) or (service is None and attr in SM_UNIQUE_CALLS):
                self.out.add(RULES_BY_ID["PY-SM-CALL"], node.lineno, attr)
            elif (service == "kms" and attr in KMS_CALLS) or (service is None and attr in KMS_UNIQUE_CALLS):
                self.out.add(RULES_BY_ID["PY-KMS-CALL"], node.lineno, attr)
        self.generic_visit(node)


_PY_FALLBACK = re.compile(r"\.(?:client|resource|create_client)\(\s*(?:service_name\s*=\s*)?['\"]([a-z0-9-]+)['\"]")


def _python_pass(out: _Collector, source: str) -> None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # Unparsable file: fall back to a regex for client creation only.
        for lineno, line in enumerate(out.lines, start=1):
            for m in _PY_FALLBACK.finditer(line):
                service = m.group(1)
                if service == "secretsmanager":
                    out.add(RULES_BY_ID["PY-SM-CLIENT"], lineno, m.group(0))
                elif service == "kms":
                    out.add(RULES_BY_ID["PY-KMS-CLIENT"], lineno, m.group(0))
                elif service == "s3":
                    out.add(RULES_BY_ID["PY-S3-ENDPOINT-DEFAULT"], lineno, m.group(0))
                elif service in AWS_SERVICE_NAMES:
                    out.add(RULES_BY_ID["PY-AWS-OTHER-CLIENT"], lineno, m.group(0), service=f"aws:{service}")
        return
    _PythonVisitor(out).visit(tree)


def _terraform_pass(out: _Collector) -> None:
    for lineno, line in enumerate(out.lines, start=1):
        m = TERRAFORM_BLOCK.match(line)
        if not m:
            continue
        rtype = m.group(2)
        rule_id = TERRAFORM_EXACT.get(rtype)
        if rule_id is None:
            rule_id = next((rid for prefix, rid in TERRAFORM_PREFIX if rtype.startswith(prefix)), "TF-AWS-OTHER")
        out.add(RULES_BY_ID[rule_id], lineno, f"{m.group(1)}.{rtype}.{m.group(3)}", service=f"aws:{rtype}")


def _url_host_is_cloud_candidate(host: str) -> bool:
    host = host.lower()
    if "amazonaws.com" in host:
        return False  # GEN-AWS-ENDPOINT covers it
    return not (NON_CLOUD_HOST.match(host) or SPEC_HOST.match(host))


def _strip_trailing_hash_comment(line: str) -> str:
    """Drop a trailing `` # comment`` outside quotes (YAML, .env, shell). ``a#b`` inside a value stays."""
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and i > 0 and line[i - 1] in " \t":
            return line[:i]
    return line


def _line_pass(out: _Collector) -> None:
    comment = COMMENT_PREFIXES.get(out.fclass, ())
    hash_comments = out.fclass in (CONFIG, SCRIPT)
    regex_rules = [r for r in RULES if r.pattern is not None and out.fclass in r.applies_to]
    for lineno, line in enumerate(out.lines, start=1):
        stripped = line.lstrip()
        if not stripped or (comment and stripped.startswith(comment)):
            continue
        if hash_comments:
            line = _strip_trailing_hash_comment(line)
        for rule in regex_rules:
            assert rule.pattern is not None
            for m in rule.pattern.finditer(line):
                text = m.group(0)
                service: str | None = None
                if rule.rule_id == "GEN-URL-LITERAL":
                    if not _url_host_is_cloud_candidate(m.group(1)):
                        continue
                    service = f"host:{m.group(1).lower()}"
                elif rule.rule_id == "GEN-AWS-ENDPOINT":
                    host = re.sub(r"^https?://", "", text).split("/")[0].lower()
                    service = f"aws-endpoint:{host}"
                elif rule.rule_id == "GEN-DSN-URL" and re.search(r"[{$%]", text):
                    continue  # templated: GEN-DSN-TEMPLATE
                elif rule.service == "*" and m.groups():
                    service = f"aws:{m.group(1).lower()}"
                out.add(rule, lineno, text, service=service)


def _walk(root: Path) -> Iterator[tuple[Path, PurePosixPath]]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            yield path, PurePosixPath(path.relative_to(root).as_posix())


_TEST_DIRS = frozenset({"tests", "test", "__tests__", "testdata", "e2e"})
_RULE_ORDER = {r.rule_id: i for i, r in enumerate(RULES)}


def is_test_path(rel: PurePosixPath) -> bool:
    """Test code is not migration surface. It is listed apart, not dropped."""
    name = rel.name
    return (any(p in _TEST_DIRS for p in rel.parts[:-1]) or name == "conftest.py" or name.startswith("test_")
            or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.js", ".spec.ts", ".spec.js")))


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """One dependency per (file, line, tier, service): keep the earliest rule in the table.

    Used for the summary counts only; the finding lists keep every matching rule as evidence."""
    best: dict[tuple[str, int, str, str], Finding] = {}
    for f in findings:
        key = (f.file, f.line, f.tier, f.service)
        cur = best.get(key)
        if cur is None or _RULE_ORDER.get(f.rule_id, 1 << 30) < _RULE_ORDER.get(cur.rule_id, 1 << 30):
            best[key] = f
    return list(best.values())


def scan(root: Path, label: str | None = None) -> InventoryReport:
    """Scan ``root`` and return the FR-2 inventory. ``label`` names the root in the output."""
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"inventory root is not a directory: {root.name or root}")
    root = root.resolve()
    findings: list[Finding] = []
    scanned = 0
    skipped_files: list[dict[str, str]] = []  # audit 37: every skipped candidate file is listed with a reason
    digest = hashlib.sha256()
    for path, rel in _walk(root):
        fclass = file_class(rel)
        if fclass is None:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped_files.append({"file": str(rel), "reason": f"larger than {MAX_FILE_BYTES} bytes"})
                continue
            raw = path.read_bytes()
        except OSError as exc:
            skipped_files.append({"file": str(rel), "reason": f"unreadable: {type(exc).__name__}"})
            continue
        if b"\0" in raw[:8192]:
            skipped_files.append({"file": str(rel), "reason": "binary (NUL byte)"})
            continue
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped_files.append({"file": str(rel), "reason": "not UTF-8"})
            continue
        scanned += 1
        digest.update(f"{rel}\0{hashlib.sha256(raw).hexdigest()}\n".encode())
        out = _Collector(str(rel), fclass, source.splitlines())
        if fclass == PYTHON:
            _python_pass(out, source)
        elif fclass == TERRAFORM:
            _terraform_pass(out)
        _line_pass(out)
        findings.extend(out.items.values())

    findings.sort(key=lambda f: (f.file, f.line, f.rule_id, f.id))
    tests = [f for f in findings if is_test_path(PurePosixPath(f.file))]
    findings = [f for f in findings if not is_test_path(PurePosixPath(f.file))]
    classified = [f for f in findings if f.tier != Tier.UNCLASSIFIED.value]
    unclassified = [f for f in findings if f.tier == Tier.UNCLASSIFIED.value]
    summary = {t.value: 0 for t in Tier}
    for f in _dedupe(findings):
        summary[f.tier] += 1
    test_summary = {t.value: 0 for t in Tier}
    for f in _dedupe(tests):
        test_summary[f.tier] += 1
    return InventoryReport(
        root=label if label is not None else root.name,
        input_digest=digest.hexdigest(),
        files_scanned=scanned,
        files_skipped=len(skipped_files),
        skipped=sorted(skipped_files, key=lambda s: s["file"]),
        summary=summary,
        findings=classified,
        unclassified=unclassified,
        test_summary=test_summary,
        test_findings=tests,
    )


def rules_table() -> list[dict[str, object]]:
    """The rule table as plain data (for the README and an API listing)."""
    return [
        {
            "rule_id": r.rule_id,
            "tier": r.tier.value,
            "tier_code": TIER_CODE[r.tier],
            "service": r.service,
            "kind": r.kind,
            "applies_to": sorted(r.applies_to),
            "literal": r.literal,
            "reason": r.reason,
        }
        for r in RULES
    ]
