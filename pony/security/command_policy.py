"""Fail-closed classification for Host shell commands."""

import os
import re
import shlex
import stat
from pathlib import Path

from pony.security import paths as security_paths
from pony.security.paths import is_sensitive_path

_TWO_CHAR_SHELL_TOKENS = ("&&", "||", "<<", ">>")
_ONE_CHAR_SHELL_TOKENS = frozenset("|;&<>()")
_REDIRECT_TOKENS = {"<", ">", "<<", ">>"}
_CONTROL_KEYWORDS = {
    "if",
    "then",
    "elif",
    "else",
    "fi",
    "while",
    "until",
    "for",
    "do",
    "done",
    "case",
    "esac",
    "!",
    "time",
}
_COMMAND_PREFIX_KEYWORDS = {
    "{",
    "!",
    "time",
    "if",
    "then",
    "elif",
    "else",
    "while",
    "until",
    "do",
}
_ASSIGNMENT_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _line_break_length(raw, index):
    if raw[index : index + 2] == "\r\n":
        return 2
    return int(index < len(raw) and raw[index] in "\r\n")


def _scan_shell_syntax(command):
    raw = str(command or "")
    operators = []
    redirect_operators = []
    has_expansion = False
    quote = ""
    escaped = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == "single":
            if char == "'":
                quote = ""
            index += 1
            continue
        if quote == "double":
            if char == '"':
                quote = ""
            elif char == "\\":
                line_break_length = _line_break_length(raw, index + 1)
                if line_break_length:
                    operators.append("\n")
                    index += line_break_length + 1
                    continue
                escaped = True
            elif char == "`" or char == "$":
                has_expansion = True
            index += 1
            continue
        if char == "\\":
            line_break_length = _line_break_length(raw, index + 1)
            if line_break_length:
                operators.append("\n")
                index += line_break_length + 1
                continue
            escaped = True
            index += 1
            continue
        if char == "'":
            quote = "single"
            index += 1
            continue
        if char == '"':
            quote = "double"
            index += 1
            continue
        line_break_length = _line_break_length(raw, index)
        if line_break_length:
            operators.append("\n")
            index += line_break_length
            continue
        pair = raw[index : index + 2]
        if pair == "$(":
            operators.append(pair)
            has_expansion = True
            index += 2
            continue
        if pair in _TWO_CHAR_SHELL_TOKENS:
            operators.append(pair)
            if pair in _REDIRECT_TOKENS:
                redirect_operators.append(pair)
            index += 2
            continue
        if char in _ONE_CHAR_SHELL_TOKENS:
            operators.append(char)
            if char in _REDIRECT_TOKENS:
                redirect_operators.append(char)
            index += 1
            continue
        if char in "`$*?[{" or (
            char == "~" and (index == 0 or raw[index - 1].isspace())
        ):
            has_expansion = True
        index += 1

    parse_error = bool(quote or escaped)
    argv = []
    if not parse_error:
        try:
            argv = shlex.split(raw, comments=False, posix=True)
        except ValueError:
            parse_error = True
    redirects = []
    if not parse_error and redirect_operators:
        lexer = shlex.shlex(raw, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        grammar_tokens = list(lexer)
        for token_index, token in enumerate(grammar_tokens):
            if token in _REDIRECT_TOKENS:
                target = (
                    grammar_tokens[token_index + 1]
                    if token_index + 1 < len(grammar_tokens)
                    else ""
                )
                redirects.append((token, target))
            elif any(char in "<>" for char in token) and all(
                char in _ONE_CHAR_SHELL_TOKENS for char in token
            ):
                redirects.append((token, ""))
    has_assignment = bool(argv and _ASSIGNMENT_TOKEN_RE.match(argv[0]))
    has_control_keyword = bool(argv and argv[0].casefold() in _CONTROL_KEYWORDS)
    return {
        "parse_error": parse_error,
        "operators": tuple(operators),
        "redirects": tuple(redirects),
        "has_expansion": has_expansion,
        "has_assignment": has_assignment,
        "has_control_keyword": has_control_keyword,
    }


_LS_OPTIONS = {"-1", "-a", "-A", "-d", "-F", "-l"}
_FILE_OPTIONS = {"-b", "--brief"}
_WC_OPTIONS = {"-c", "-l", "-w"}
_GIT_STATUS_OPTIONS = {"--short", "--porcelain", "--porcelain=v1", "--branch"}
_AUTO_HEADS = {"pwd", "ls", "stat", "file", "wc", "git"}
_SHELL_WRAPPERS = {"sh", "bash", "zsh"}
_SHELL_REQUIRED_VALUE_OPTIONS = {
    "--rcfile",
    "--init-file",
}
_ENV_REQUIRED_LONG_OPTIONS = {
    "--unset",
    "--chdir",
    "--path",
    "--argv0",
}
_ENV_SHORT_VALUE_OPTIONS = frozenset("uCPa")
_INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php"}
_PRIVILEGED = {"sudo", "doas", "pkexec"}
_DESTRUCTIVE_HEADS = {
    "shutdown",
    "reboot",
    "mount",
    "umount",
    "chown",
    "chmod",
    "kill",
}


def _shell_wrapper_payload(argv):
    if not argv or Path(argv[0]).name.casefold() not in _SHELL_WRAPPERS:
        return None
    index = 1
    while index < len(argv):
        option = argv[index]
        if option == "--":
            return None
        if option in _SHELL_REQUIRED_VALUE_OPTIONS:
            index += 2
            continue
        if option.startswith("--"):
            index += 1
            continue
        if option.startswith(("-", "+")) and len(option) > 1:
            cluster = option[1:]
            for cluster_index, flag in enumerate(cluster):
                if option[0] == "-" and flag == "c":
                    return argv[index + 1] if index + 1 < len(argv) else None
                if flag in "oO":
                    index += 1
                    if (
                        cluster_index == len(cluster) - 1
                        and index < len(argv)
                        and not argv[index].startswith(("-", "+"))
                    ):
                        index += 1
                    break
            else:
                index += 1
            continue
        return None
    return None


def _env_prefix(argv):
    if not argv or Path(argv[0]).name.casefold() != "env":
        return False, 0
    index = 1
    while index < len(argv):
        option = argv[index]
        if option == "--":
            return False, index + 1
        if option == "-":
            index += 1
            continue
        if option == "--split-string" or option.startswith("--split-string="):
            return True, index
        if option in _ENV_REQUIRED_LONG_OPTIONS:
            index += 2
            continue
        if option.startswith("--"):
            index += 1
            continue
        if option.startswith("-") and len(option) > 1:
            cluster = option[1:]
            consume_next = False
            for cluster_index, flag in enumerate(cluster):
                if flag == "S":
                    return True, index
                if flag in _ENV_SHORT_VALUE_OPTIONS:
                    consume_next = cluster_index == len(cluster) - 1
                    break
            index += 2 if consume_next else 1
            continue
        return False, index
    return False, index


def _assessment(risk_class, decision, reason, argv, execution_mode):
    return {
        "risk_class": risk_class,
        "decision": decision,
        "reason": reason,
        "argv": list(argv),
        "execution_mode": execution_mode,
    }


def _path_operand_reason(workspace_root, raw_path, *, require_regular=False):
    raw = str(raw_path or "")
    if not raw or "\x00" in raw:
        return "unsafe_path"
    try:
        root = Path(workspace_root).resolve(strict=True)
        source = Path(raw)
        candidate = Path(
            os.path.abspath(
                os.fspath(source if source.is_absolute() else root / source)
            )
        )
    except (OSError, RuntimeError, ValueError):
        return "unsafe_path"
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return "outside_path"
    if ".." in source.parts:
        return "unsafe_path"
    if is_sensitive_path(relative.as_posix()):
        return "sensitive_path"
    env_template = security_paths.is_allowed_env_template_leaf(relative.as_posix())
    current = root
    try:
        mode = root.lstat().st_mode
    except OSError:
        return "unsafe_path"
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if index < len(relative.parts) - 1:
                return "unsafe_path"
            return "sensitive_path" if env_template else ""
        except OSError:
            return "unsafe_path"
        if stat.S_ISLNK(mode):
            return "unsafe_path"
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
            return "unsafe_path"
    if env_template and not stat.S_ISREG(mode):
        return "sensitive_path"
    if require_regular and not stat.S_ISREG(mode):
        return "unsafe_path"
    return ""


def _paths_reason(workspace_root, operands, *, require_regular=False):
    for operand in operands:
        reason = _path_operand_reason(
            workspace_root,
            operand,
            require_regular=require_regular,
        )
        if reason:
            return reason
    return ""


def _git_grammar_reason(argv):
    args = tuple(argv[1:])
    if not args:
        return "unknown_git_grammar"
    subcommand, rest = args[0], args[1:]
    if subcommand == "status":
        return (
            ""
            if len(rest) == len(set(rest)) and set(rest) <= _GIT_STATUS_OPTIONS
            else "unknown_git_grammar"
        )
    if subcommand == "rev-parse":
        accepted = {
            ("--show-toplevel",),
            ("--is-inside-work-tree",),
            ("--abbrev-ref", "HEAD"),
            ("HEAD",),
        }
        return "" if rest in accepted else "unknown_git_grammar"
    if subcommand == "branch":
        return (
            "" if rest in {("--show-current",), ("--list",)} else "unknown_git_grammar"
        )
    if subcommand == "worktree":
        return "" if rest == ("list",) else "unknown_git_grammar"
    if subcommand == "ls-files":
        return "" if not rest else "unknown_git_grammar"
    return "unknown_git_grammar"


def _automatic_grammar_reason(argv, workspace_root):
    head, args = argv[0], list(argv[1:])
    if head == "pwd":
        return "" if not args else "unknown_option"
    if head == "ls":
        options = [item for item in args if item.startswith("-")]
        paths = [item for item in args if not item.startswith("-")]
        if any(item not in _LS_OPTIONS for item in options):
            return "unknown_option"
        return _paths_reason(workspace_root, paths)
    if head == "stat":
        if not args:
            return "missing_path"
        if any(item.startswith("-") for item in args):
            return "unknown_option"
        return _paths_reason(workspace_root, args)
    if head == "file":
        if args and args[0] in _FILE_OPTIONS:
            args = args[1:]
        if not args or any(item.startswith("-") for item in args):
            return "unknown_option_or_missing_path"
        return _paths_reason(workspace_root, args)
    if head == "wc":
        if args and args[0] in _WC_OPTIONS:
            args = args[1:]
        if not args or any(item.startswith("-") for item in args):
            return "unknown_option_or_missing_path"
        return _paths_reason(workspace_root, args, require_regular=True)
    if head == "git":
        return _git_grammar_reason(argv)
    return "unknown_command"


def _backtick_end(raw, start):
    escaped = False
    for index in range(start + 1, len(raw)):
        char = raw[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            return index
    return -1


def _dollar_paren_end(raw, start):
    depth = 1
    quote = ""
    escaped = False
    in_backtick = False
    quoted_substitutions = 0
    index = start + 2
    while index < len(raw):
        char = raw[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == "single":
            if char == "'":
                quote = ""
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if in_backtick:
            if char == "`":
                in_backtick = False
            index += 1
            continue
        pair = raw[index : index + 2]
        if char == "`":
            in_backtick = True
            index += 1
            continue
        if quote == "double":
            if char == '"':
                quote = ""
            elif pair == "$(":
                depth += 1
                quoted_substitutions += 1
                index += 2
                continue
            elif char == ")" and quoted_substitutions:
                depth -= 1
                quoted_substitutions -= 1
            index += 1
            continue
        if char == "'":
            quote = "single"
        elif char == '"':
            quote = "double"
        elif pair == "$(":
            depth += 1
            index += 2
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _embedded_shell_payloads(command):
    raw = str(command or "")
    payloads = []
    last_close = raw.rfind(")")
    broad_payload_added = False
    quote = ""
    escaped = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == "single":
            if char == "'":
                quote = ""
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == "'" and not quote:
            quote = "single"
            index += 1
            continue
        if char == '"':
            quote = "" if quote == "double" else "double"
            index += 1
            continue
        if char == "`":
            end = _backtick_end(raw, index)
            if end >= 0:
                payloads.append(raw[index + 1 : end])
                index = end + 1
                continue
        if raw[index : index + 2] == "$(":
            end = _dollar_paren_end(raw, index)
            if end >= 0:
                payloads.append(raw[index + 2 : end])
                if not broad_payload_added and last_close > end:
                    payloads.append(raw[index + 2 : last_close])
                    broad_payload_added = True
                index = end + 1
                continue
        index += 1
    return tuple(payloads)


def _remove_shell_line_continuations(command):
    raw = str(command or "")
    result = []
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote == "single":
            result.append(char)
            if char == "'":
                quote = ""
            index += 1
            continue
        if not quote:
            line_break_length = _line_break_length(raw, index)
            if line_break_length:
                result.append(";")
                index += line_break_length
                continue
        if char == "\\":
            line_break_length = _line_break_length(raw, index + 1)
            if line_break_length:
                index += line_break_length + 1
                continue
            if index + 1 >= len(raw):
                index += 1
                continue
            result.extend((char, raw[index + 1]))
            index += 2
            continue
        result.append(char)
        if char == "'" and not quote:
            quote = "single"
        elif char == '"':
            quote = "" if quote == "double" else "double"
        index += 1
    return "".join(result)


def _grammar_words(command):
    lexer = shlex.shlex(str(command or ""), posix=True, punctuation_chars="|&;<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _literal_word_is_sensitive(word):
    if is_sensitive_path(word):
        return True
    if ":" in word and any(
        is_sensitive_path(candidate) for candidate in word.split(":")[1:] if candidate
    ):
        return True
    if not word.startswith("-") or len(word) <= 2:
        return False
    if security_paths.has_sensitive_path_suffix(word):
        return True
    candidates = [word[2:]]
    if "=" in word:
        candidates.append(word.split("=", 1)[1])
    dot_index = word.find(".", 2)
    if dot_index >= 0:
        candidates.append(word[dot_index:])
    return any(is_sensitive_path(candidate) for candidate in candidates)


def _assignment_value_is_sensitive(token):
    return bool(
        _ASSIGNMENT_TOKEN_RE.match(token)
        and _literal_word_is_sensitive(token.split("=", 1)[1])
    )


def _command_segments(words):
    boundaries = {"&&", "||", ";", "|", "&", "(", ")"}
    segments = [[]]
    for word in words:
        if word in boundaries:
            segments.append([])
        else:
            segments[-1].append(word)
    return segments


def _is_redirect_run(token):
    return bool(
        token
        and any(char in "<>" for char in token)
        and all(char in _ONE_CHAR_SHELL_TOKENS for char in token)
    )


def _literal_sensitive_reason(command, workspace_root):
    pending = [str(command or "")]
    seen = set()
    while pending:
        raw = pending.pop()
        text = _remove_shell_line_continuations(raw)
        if text in seen:
            continue
        seen.add(text)
        pending.extend(_embedded_shell_payloads(text))
        try:
            words = _grammar_words(text)
        except ValueError:
            words = re.split(r"[\s|&;<>]+", text)
        for index, word in enumerate(words):
            if Path(word).name.casefold() != "env":
                continue
            has_split_string, _ = _env_prefix(words[index:])
            if has_split_string:
                return "env_split_string_rejected"
        if any(_literal_word_is_sensitive(word) for word in words):
            return "sensitive_path"
        for segment in _command_segments(words):
            index = 0
            while index < len(segment):
                if segment[index].casefold() in _COMMAND_PREFIX_KEYWORDS:
                    index += 1
                    continue
                if _ASSIGNMENT_TOKEN_RE.match(segment[index]):
                    if _assignment_value_is_sensitive(segment[index]):
                        return "sensitive_path"
                    index += 1
                    continue
                if (
                    segment[index].isdigit()
                    and index + 1 < len(segment)
                    and _is_redirect_run(segment[index + 1])
                ):
                    index += 1
                if index < len(segment) and _is_redirect_run(segment[index]):
                    index += 2
                    continue
                break
            if index < len(segment) and Path(segment[index]).name.casefold() == "env":
                _, env_index = _env_prefix(segment[index:])
                index += env_index
                while index < len(segment) and _ASSIGNMENT_TOKEN_RE.match(
                    segment[index]
                ):
                    if _assignment_value_is_sensitive(segment[index]):
                        return "sensitive_path"
                    index += 1
        for index, token in enumerate(words):
            if Path(token).name.casefold() not in _SHELL_WRAPPERS:
                continue
            payload = _shell_wrapper_payload(words[index:])
            if payload is not None:
                pending.append(payload)
    return ""


def _assess_command(command, workspace_root, executables, _depth=0):
    raw = str(command or "").strip()
    scan = _scan_shell_syntax(raw)
    has_shell_grammar = bool(
        scan["parse_error"]
        or scan["operators"]
        or scan["has_expansion"]
        or scan["has_assignment"]
        or scan["has_control_keyword"]
    )
    literal_reason = _literal_sensitive_reason(raw, workspace_root)
    if literal_reason:
        return _assessment(
            "destructive",
            "reject",
            literal_reason,
            [],
            "shell" if has_shell_grammar else "argv",
        )
    if scan["parse_error"]:
        return _assessment("external_effect", "ask", "shell_parse_error", [], "shell")
    argv = shlex.split(raw, comments=False, posix=True)
    if scan["redirects"]:
        if scan["has_expansion"]:
            return _assessment("destructive", "ask", "dynamic_redirect", [], "shell")
        redirect_reasons = [
            _path_operand_reason(workspace_root, target)
            for _, target in scan["redirects"]
        ]
        if "sensitive_path" in redirect_reasons:
            return _assessment("destructive", "reject", "sensitive_path", [], "shell")
        if any(
            reason in {"outside_path", "unsafe_path"} for reason in redirect_reasons
        ):
            return _assessment("destructive", "ask", "unsafe_redirect", [], "shell")
        return _assessment(
            "workspace_write", "ask", "redirect_requires_approval", [], "shell"
        )
    if has_shell_grammar:
        return _assessment(
            "external_effect",
            "ask",
            "shell_grammar_requires_approval",
            [],
            "shell",
        )
    if not argv:
        return _assessment("external_effect", "ask", "empty_command", [], "shell")
    head = argv[0]
    if "/" in head or "\\" in head:
        return _assessment(
            "external_effect",
            "ask",
            "executable_path_requires_approval",
            argv,
            "argv",
        )
    shell_payload = _shell_wrapper_payload(argv)
    if shell_payload is not None:
        nested = (
            _assess_command(
                shell_payload,
                workspace_root,
                executables,
                _depth=_depth + 1,
            )
            if _depth < 2
            else None
        )
        if nested is not None and nested["decision"] == "reject":
            return _assessment("destructive", "reject", nested["reason"], argv, "argv")
        return _assessment(
            "external_effect",
            "ask",
            "shell_wrapper_requires_approval",
            argv,
            "argv",
        )
    if head in _AUTO_HEADS:
        reason = _automatic_grammar_reason(argv, workspace_root)
        if reason:
            decision = (
                "reject"
                if reason in {"sensitive_path", "unsafe_path", "outside_path"}
                else "ask"
            )
            risk = "destructive" if decision == "reject" else "external_effect"
            return _assessment(risk, decision, reason, argv, "argv")
        if executables is not None and head not in executables:
            return _assessment(
                "read_only",
                "ask",
                "trusted_executable_missing",
                argv,
                "argv",
            )
        return _assessment("read_only", "allow", "proved_read_only", argv, "argv")
    if head in _INTERPRETERS:
        reason = "interpreter_requires_approval"
    elif head in _PRIVILEGED:
        reason = "privileged_command_requires_approval"
    elif head in _DESTRUCTIVE_HEADS:
        return _assessment(
            "destructive",
            "ask",
            "system_command_requires_approval",
            argv,
            "argv",
        )
    else:
        reason = "unknown_command_requires_approval"
    return _assessment("external_effect", "ask", reason, argv, "argv")


def assess_command(command, workspace_root, executables=None):
    return _assess_command(command, workspace_root, executables, _depth=0)
