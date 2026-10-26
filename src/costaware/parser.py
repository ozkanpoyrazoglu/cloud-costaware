"""Extract AWS CLI invocations from a shell command string."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

SEPARATORS = {"|", "||", "&", "&&", ";", ";;", "(", ")", "\n"}
WRAPPERS = {"sudo", "env", "time", "nohup", "exec", "command", "xargs", "watch"}
# A word that can be an AWS CLI service or operation name, used to spot `aws` behind wrappers we don't know.
CLI_WORD = re.compile(r"^[a-z][a-z0-9-]*$")
# Characters after which an unquoted "#" starts a comment.
WORD_BREAKS = set(" \t\n;|&()<>")
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT = re.compile(r"^[0-9]*[<>]+&?$")
SHELLS = {"sh", "bash", "zsh", "dash"}
# One level of nested parentheses is enough for "$(aws ... --query "$(...)")"-style commands.
SUBSTITUTION = re.compile(r"\$\(((?:[^()]|\([^()]*\))*)\)|`([^`]*)`")

# Global AWS CLI options that take a value when they appear before the service name.
GLOBAL_VALUE_OPTIONS = {
    "--region", "--profile", "--output", "--endpoint-url", "--query", "--color",
    "--ca-bundle", "--cli-read-timeout", "--cli-connect-timeout", "--cli-binary-format",
}

# Options that never take a value. Anything starting with --no- is treated as a flag too.
FLAG_OPTIONS = {
    "--debug", "--version", "--dry-run", "--recursive", "--delete", "--force", "--quiet",
    "--only-show-errors", "--exact-timestamps", "--follow-symlinks", "--human-readable",
    "--summarize", "--size-only", "--cli-auto-prompt", "--generate-cli-skeleton",
    "--consistent-read", "--return-consumed-capacity-flag",
}

# High-level `aws s3` commands. Their options take exactly one value, and positionals matter.
S3_HIGH_LEVEL = {"ls", "cp", "mv", "rm", "sync", "mb", "rb", "presign", "website"}


@dataclass
class AwsCall:
    service: str | None
    operation: str | None
    options: dict[str, list[str]] = field(default_factory=dict)
    positionals: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def region(self) -> str | None:
        values = self.options.get("--region")
        return values[0] if values else None

    @property
    def facts(self) -> dict[str, str]:
        """Derived properties that rules can match on with "@name"."""
        facts = {}
        if self.service == "s3" and self.operation in {"cp", "mv", "sync"} and len(self.positionals) >= 2:
            src, dst = (p.startswith("s3://") for p in self.positionals[:2])
            if src and dst:
                facts["direction"] = "s3-to-s3"
            elif src:
                facts["direction"] = "download"
            else:
                facts["direction"] = "upload"
        return facts


class ParseError(ValueError):
    pass


def strip_comments(command: str) -> str:
    """Remove shell comments: an unquoted "#" at the start of a word, up to the end of the line."""
    out, quote, i = [], None, 0
    while i < len(command):
        char = command[i]
        if quote:
            if char == "\\" and quote == '"' and i + 1 < len(command):
                out.append(command[i:i + 2])
                i += 2
                continue
            if char == quote:
                quote = None
        elif char == "\\" and i + 1 < len(command):
            out.append(command[i:i + 2])
            i += 2
            continue
        elif char in "'\"":
            quote = char
        elif char == "#" and (i == 0 or command[i - 1] in WORD_BREAKS):
            end = command.find("\n", i)
            i = len(command) if end == -1 else end
            continue
        out.append(char)
        i += 1
    return "".join(out)


def tokenize(command: str) -> list[str]:
    if "$'" in command:
        # ANSI-C quoting ($'it\'s') has escapes neither shlex nor strip_comments model; fail closed.
        raise ParseError("$'...' quoting is not supported")
    command = strip_comments(command.replace("\\\n", " "))
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:  # unbalanced quotes and the like
        raise ParseError(str(exc)) from exc


def split_segments(tokens: list[str]) -> list[list[str]]:
    segments, current = [], []
    for token in tokens:
        if token in SEPARATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_prefix(segment: list[str]) -> list[str]:
    """Drop env assignments and wrapper commands such as sudo/env/xargs."""
    i = 0
    while i < len(segment):
        token = segment[i]
        if ENV_ASSIGNMENT.match(token):
            i += 1
        elif token in WRAPPERS:
            i += 1
            while i < len(segment) and segment[i].startswith("-"):
                i += 1
        else:
            break
    return segment[i:]


def _strip_redirects(args: list[str]) -> list[str]:
    out, skip_next = [], False
    for token in args:
        if skip_next:
            skip_next = False
        elif REDIRECT.match(token):
            # `2>/dev/null` tokenizes as "2", ">", "/dev/null".
            if out and out[-1].isdigit():
                out.pop()
            skip_next = not token.endswith("&")
        elif token.startswith(">&") or token.startswith("&>"):
            continue
        else:
            out.append(token)
    return out


def _is_flag(option: str) -> bool:
    return option in FLAG_OPTIONS or option.startswith("--no-")


def _parse_args(args: list[str], single_value: bool) -> tuple[dict[str, list[str]], list[str]]:
    options: dict[str, list[str]] = {}
    positionals: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--"):
            name, eq, value = token.partition("=")
            values = options.setdefault(name, [])
            i += 1
            if eq:
                values.append(value)
            elif not _is_flag(name):
                while i < len(args) and not args[i].startswith("--"):
                    values.append(args[i])
                    i += 1
                    if single_value:
                        break
        else:
            positionals.append(token)
            i += 1
    return options, positionals


def _is_aws(token: str) -> bool:
    return token.rsplit("/", 1)[-1] == "aws"


def _find_wrapped_aws(segment: list[str]) -> int | None:
    """Index of an `aws <service> <operation>` behind a wrapper with arguments (`sudo -u x`, `timeout 60`,
    `aws-vault exec p --`...). Errs on the side of finding one: `echo aws ec2 run-instances` counts too."""
    for i, token in enumerate(segment[1:], start=1):
        rest = segment[i + 1:i + 3]
        if _is_aws(token) and len(rest) == 2 and all(CLI_WORD.match(t) for t in rest):
            return i
    return None


def parse_aws_segment(segment: list[str]) -> AwsCall | None:
    segment = _strip_prefix(segment)
    if segment and not _is_aws(segment[0]):
        start = _find_wrapped_aws(segment)
        segment = segment[start:] if start is not None else []
    if not segment:
        return None
    args = _strip_redirects(segment[1:])
    raw = shlex.join(["aws", *args])

    global_options: dict[str, list[str]] = {}
    i = 0
    while i < len(args) and args[i].startswith("--"):
        name, eq, value = args[i].partition("=")
        i += 1
        if eq:
            global_options[name] = [value]
        elif name in GLOBAL_VALUE_OPTIONS and i < len(args):
            global_options[name] = [args[i]]
            i += 1
        else:
            global_options[name] = []

    service = args[i] if i < len(args) else None
    operation = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else None
    rest = args[i + 2:] if operation else args[i + 1:]

    options, positionals = _parse_args(rest, single_value=service == "s3" and operation in S3_HIGH_LEVEL)
    for name, values in global_options.items():
        options.setdefault(name, values)
    return AwsCall(service, operation, options, positionals, raw)


def find_aws_calls(command: str) -> list[AwsCall]:
    """Return every `aws ...` invocation in a shell command. Raises ParseError if unparsable."""
    calls = []
    # Pull out $(...) and `...` first: shlex can't handle quotes nested inside them.
    substitutions = [m.group(1) or m.group(2) or "" for m in SUBSTITUTION.finditer(command)]
    command = SUBSTITUTION.sub("SUBSTITUTION", command)
    for segment in split_segments(tokenize(command)):
        call = parse_aws_segment(segment)
        if call is not None:
            calls.append(call)
        for i, token in enumerate(segment[1:], start=1):
            if token == "-c" and segment[i - 1].rsplit("/", 1)[-1] in SHELLS and i + 1 < len(segment):
                calls.extend(find_aws_calls(segment[i + 1]))
    for inner in substitutions:
        calls.extend(find_aws_calls(inner))
    return calls


def to_cli_name(name: str) -> str:
    """Convert an API operation name (RunInstances, DescribeDBInstances) to CLI form."""
    if not any(c.isupper() for c in name):
        return name
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    return name.lower()


PYTHON = re.compile(r"^python[0-9.]*$")
# <<EOF / <<-'EOF' / <<"EOF" ... EOF. Group 4 is the body.
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)


@dataclass
class PythonSource:
    kind: str  # "inline" (-c or heredoc) or "file"
    value: str  # the code, or the script path


@dataclass
class Heredoc:
    body: str
    executed: bool  # fed to a shell (`bash <<EOF`, `cat <<EOF | sh`, `ssh host <<EOF`), not just data


# Programs that run a heredoc's body as shell commands.
SHELL_READERS = SHELLS | {"ssh", "eval", "source"}


def split_heredocs(command: str) -> tuple[str, list[PythonSource], list[Heredoc]]:
    """Pull every heredoc body out of the command.

    Returns the command without the bodies, so quotes or Python syntax in a body can't confuse the shell
    parser, plus the bodies fed to a Python interpreter and the other bodies.
    """
    sources: list[PythonSource] = []
    heredocs: list[Heredoc] = []

    def replace(match: re.Match) -> str:
        # The reader can come before the heredoc (`python3 - <<EOF`) or after it (`cat <<EOF | python3`).
        line_start = command.rfind("\n", 0, match.start()) + 1
        line = command[line_start:match.start()] + " " + match.group(3)
        words = {w.rsplit("/", 1)[-1] for w in re.split(r"[\s;|&()<>]+", line)}
        if any(PYTHON.match(w) for w in words):
            sources.append(PythonSource("inline", match.group(4)))
        else:
            heredocs.append(Heredoc(match.group(4), executed=bool(words & SHELL_READERS)))
        return match.group(3)

    return HEREDOC.sub(replace, command), sources, heredocs


# Environment assignments that can't change what an `aws` call executes or which credentials it reads.
SAFE_AWS_ENV = re.compile(r"^(AWS_PROFILE|AWS_REGION|AWS_DEFAULT_REGION|AWS_PAGER|AWS_DEFAULT_OUTPUT)=")


def is_plain_aws(command: str) -> bool:
    """True when the command does nothing but run `aws` CLI calls: no substitutions, heredocs, redirects,
    wrappers (sudo, env...), other programs, or environment variables beyond region/profile/output."""
    if SUBSTITUTION.search(command) or "<<" in command:
        return False
    try:
        segments = split_segments(tokenize(command))
    except ParseError:
        return False
    for segment in segments:
        while segment and SAFE_AWS_ENV.match(segment[0]):
            segment = segment[1:]
        if not segment or segment[0] != "aws" or any(c in t for t in segment for c in "<>"):
            return False
    return bool(segments)


def find_python_sources(command: str) -> list[PythonSource]:
    """Return Python code run by a shell command: `python -c ...`, `python script.py`, heredocs."""
    command, sources, _ = split_heredocs(command)
    command = SUBSTITUTION.sub("SUBSTITUTION", command)
    try:
        segments = split_segments(tokenize(command))
    except ParseError:
        return sources
    for segment in segments:
        segment = _strip_prefix(segment)
        if not segment or not PYTHON.match(segment[0].rsplit("/", 1)[-1]):
            continue
        args = iter(segment[1:])
        for token in args:
            if token == "-c":
                code = next(args, None)
                if code is not None:
                    sources.append(PythonSource("inline", code))
                break
            if token == "-m":
                break  # running a module: nothing to read
            if not token.startswith("-"):
                sources.append(PythonSource("file", token))
                break
    return sources
