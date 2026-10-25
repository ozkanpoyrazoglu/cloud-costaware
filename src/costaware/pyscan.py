"""Find AWS API calls in Python source without running it.

Two styles are recognized: boto3 clients (`boto3.client("ec2").create_nat_gateway(...)`), and the AWS MCP Server
sandbox's `await call_boto3(service_name="ec2", operation_name="CreateNatGateway", params={...})`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .parser import ParseError

# Client methods that are not API calls.
LOCAL_METHODS = {"can_paginate", "close", "get_waiter", "generate_presigned_post", "generate_presigned_url"}

# boto3's managed S3 transfer helpers, mapped to the API call they make.
S3_TRANSFER = {
    "upload_file": "put_object", "upload_fileobj": "put_object",
    "download_file": "get_object", "download_fileobj": "get_object",
    "copy": "copy_object",
}

# The AWS MCP Server sandbox's generic API function, and its module of higher-level helpers.
SANDBOX_CALL = "call_boto3"
SANDBOX_MODULE = "aws_mcp"
SANDBOX_FREE_HELPERS = {"search_functions"}

# Method names that look like a costly AWS call when the receiver can't be resolved to a client.
SUSPICIOUS_PREFIXES = (
    "create_", "run_", "start_", "put_", "purchase_", "allocate_", "request_", "restore_", "copy_",
    "invoke", "execute_", "scan", "update_", "modify_", "register_", "import_", "publish", "send_",
    "resume_", "replicate_",
)


@dataclass
class PyCall:
    """One boto3 call. `service`/`operation` are None when the call could not be resolved."""

    service: str | None
    operation: str | None
    params: dict = field(default_factory=dict)
    region: str | None = None
    line: int = 0
    source: str = ""
    note: str = ""
    dynamic_params: bool = False


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _dotted(node: ast.AST) -> str | None:
    """`self.ec2` -> "self.ec2", `ec2` -> "ec2"; None for anything more complex."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _client_spec(node: ast.AST) -> tuple[str, str, str | None] | None:
    """Recognize `<anything>.client("svc", region_name=...)` / `.resource(...)`: (kind, service, region)."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("client", "resource")):
        return None
    service = _literal(node.args[0]) if node.args else None
    region = _literal(node.args[1]) if len(node.args) > 1 else None
    for kw in node.keywords:
        if kw.arg == "service_name":
            service = _literal(kw.value)
        elif kw.arg == "region_name":
            region = _literal(kw.value)
    if not isinstance(service, str):
        service = "?"  # a client whose service is computed at runtime
    return node.func.attr, service, region if isinstance(region, str) else None


class _Scanner(ast.NodeVisitor):
    def __init__(self, source: str, aws_context: bool):
        self.source = source
        self.aws_context = aws_context
        self.clients: dict[str, tuple[str, str, str | None]] = {}
        self.calls: list[PyCall] = []

    def _snippet(self, node: ast.AST) -> str:
        text = ast.get_source_segment(self.source, node) or ""
        text = " ".join(text.split())
        return text if len(text) <= 120 else text[:117] + "..."

    def _remember(self, targets, value):
        spec = _client_spec(value)
        if not spec:
            return
        for target in targets:
            name = _dotted(target)
            if name:
                self.clients[name] = spec

    def visit_Assign(self, node: ast.Assign):
        self._remember(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value is not None:
            self._remember([node.target], node.value)
        self.generic_visit(node)

    def visit_withitem(self, node: ast.withitem):
        if node.optional_vars is not None:
            self._remember([node.optional_vars], node.context_expr)
        self.generic_visit(node)

    def _add(self, node, service, operation, **extra):
        self.calls.append(PyCall(service, operation, line=node.lineno, source=self._snippet(node), **extra))

    def _sandbox_call(self, node: ast.Call):
        """`call_boto3(service_name=..., operation_name=..., region_name=..., params={...})`."""
        args = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        for name, value in zip(("service_name", "operation_name", "region_name", "params"), node.args):
            args.setdefault(name, value)
        service = _literal(args["service_name"]) if "service_name" in args else None
        operation = _literal(args["operation_name"]) if "operation_name" in args else None
        region = _literal(args["region_name"]) if "region_name" in args else None
        if not (isinstance(service, str) and isinstance(operation, str)):
            self._add(node, service if isinstance(service, str) else None,
                      operation if isinstance(operation, str) else None,
                      note="The service or operation is computed at runtime.")
            return
        params, dynamic = {}, any(kw.arg is None for kw in node.keywords)
        node_params = args.get("params")
        if isinstance(node_params, ast.Dict):
            for key, value in zip(node_params.keys, node_params.values):
                key, literal = (_literal(key) if key is not None else None), _literal(value)
                if not isinstance(key, str) or (literal is None and not isinstance(value, ast.Constant)):
                    dynamic = True
                elif literal is not None:
                    params[key] = literal
        elif node_params is not None and not (isinstance(node_params, ast.Constant) and node_params.value is None):
            dynamic = True  # params built elsewhere
        self._add(node, service, operation, params=params, region=region if isinstance(region, str) else None,
                  dynamic_params=dynamic)

    def visit_Call(self, node: ast.Call):
        self.generic_visit(node)
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name == SANDBOX_CALL:
            self._sandbox_call(node)
            return
        if isinstance(func, ast.Attribute) and _dotted(func.value) == SANDBOX_MODULE:
            if func.attr not in SANDBOX_FREE_HELPERS:
                self._add(node, None, func.attr,
                          note=f"{SANDBOX_MODULE}.{func.attr} is a higher-level AWS MCP helper; its API calls are not visible.")
            return
        if isinstance(func, ast.Name) and func.id == "getattr" and node.args:
            if _dotted(node.args[0]) in self.clients:
                self._add(node, self.clients[_dotted(node.args[0])][1], None,
                          note="Operation name is computed at runtime (getattr).")
            return
        if not isinstance(func, ast.Attribute):
            return
        receiver = func.value
        spec = _client_spec(receiver) or self.clients.get(_dotted(receiver) or "")
        method = func.attr
        if spec is None:
            if self.aws_context and method.startswith(SUSPICIOUS_PREFIXES):
                self._add(node, None, method, note="Looks like an AWS call, but the client could not be resolved.")
            return
        kind, service, region = spec
        if kind == "resource":
            # s3.Bucket("b") / ec2.Instance("i-1") only build objects; reads are free; anything else is flagged.
            if not (method[:1].isupper() or method.startswith(("get_", "list_", "describe_"))):
                self._add(node, service, method, region=region,
                          note="boto3 resource API is not analyzed; review this call.")
            return
        if method in LOCAL_METHODS:
            return
        if method == "get_paginator":
            operation = _literal(node.args[0]) if node.args else None
            self._add(node, service, operation if isinstance(operation, str) else None, region=region,
                      note="" if isinstance(operation, str) else "Paginated operation is computed at runtime.")
            return
        if service == "s3":
            method = S3_TRANSFER.get(method, method)
        params, dynamic = {}, False
        for kw in node.keywords:
            value = _literal(kw.value) if kw.arg else None
            if kw.arg is None or (value is None and not isinstance(kw.value, ast.Constant)):
                dynamic = True  # **kwargs or a value only known at runtime
            elif value is not None:
                params[kw.arg] = value
        self._add(node, service, method, params=params, region=region, dynamic_params=dynamic)


def find_boto3_calls(source: str, aws_context: bool = False) -> list[PyCall]:
    """Return the AWS calls in `source`. Raises ParseError on a syntax error.

    With `aws_context` (the code is known to run against AWS, e.g. an AWS MCP script tool), calls such as
    `x.create_foo()` on receivers that can't be resolved to a client are reported as unresolved.
    """
    try:
        # The AWS MCP sandbox runs scripts with top-level `await`.
        tree = compile(source, "<script>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except SyntaxError as exc:
        raise ParseError(f"invalid Python: {exc.msg} (line {exc.lineno})") from exc
    aws_context = aws_context or any(word in source for word in ("boto3", "botocore", SANDBOX_MODULE))
    scanner = _Scanner(source, aws_context)
    scanner.visit(tree)  # first pass collects clients, so calls above the assignment (in functions) resolve
    scanner.calls = []
    scanner.visit(tree)
    return sorted(scanner.calls, key=lambda c: c.line)
