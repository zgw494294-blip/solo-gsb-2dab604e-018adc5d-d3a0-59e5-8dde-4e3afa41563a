"""Parser for a subset of the DNS master-file format (RFC 1035 §5.1).

The accepted subset is deliberately small:

* Only ``A``, ``CNAME`` and ``NS`` records; the ``IN`` class and a
  non-negative decimal TTL may precede the type, in either order, and both
  are optional. TTLs (including ``$TTL``) are validated but never stored.
* Semicolons start comments that run to the end of the physical line.
* Parentheses group a record across physical lines; nesting is not allowed.
* The initial origin is the zone apex. ``$ORIGIN`` changes it; ``@`` stands
  for the current origin and record names, CNAME/NS targets and the
  ``$ORIGIN`` argument without a trailing dot are relative to it.
* A logical record whose first character is blank reuses the expanded name
  of the previous record; changing the origin does not change that name.

Quoted strings, escapes, any other directive and any other record type are
rejected. Every syntax error reports the 1-based line number of the first
physical line of the offending logical record.
"""
from __future__ import annotations

import re

from .resolver import ZoneError, normalize_name
from .zoneupdate import Record, normalize_value

SUPPORTED_TYPES = ("A", "CNAME", "NS")

_TTL_RE = re.compile(r"[0-9]+")


class MasterFileError(ValueError):
    """Master-file syntax error located at a specific logical record."""

    def __init__(self, line: int, message: str):
        self.line = line
        super().__init__(f"line {line}: {message}")


def _relative_to(name: str, origin: str) -> str:
    """Expand a possibly relative name against the (absolute) origin."""
    if name == "@":
        return origin
    if name.endswith("."):
        return name
    return name + "." + origin


def _scan_line(raw: str, depth: int, line_no: int) -> tuple[list[str], int]:
    """Tokenize one physical line, tracking parenthesis depth.

    Comments (``;`` to end of line) are dropped first, so quotes or
    backslashes inside a comment are harmless. Parentheses act solely as
    whitespace delimiters and never become part of a token.
    """
    code = raw.split(";", 1)[0]
    if '"' in code:
        raise MasterFileError(line_no, "quoted strings are not supported")
    if "\\" in code:
        raise MasterFileError(line_no, "escape sequences are not supported")
    tokens: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    for ch in code:
        if ch in " \t":
            flush()
        elif ch == "(":
            if depth >= 1:
                raise MasterFileError(line_no, "nested parentheses are not allowed")
            depth += 1
            flush()
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise MasterFileError(line_no, "unmatched ')'")
            flush()
        else:
            buf.append(ch)
    flush()
    return tokens, depth


def _validate_ttl(token: str, line: int) -> None:
    # TTL is only validated, never stored; accept any non-negative decimal.
    if _TTL_RE.fullmatch(token) is None:
        raise MasterFileError(
            line,
            f"invalid TTL {token!r}: expected a non-negative decimal integer",
        )


def _directive(tokens: list[str], origin: str, line: int) -> str:
    """Handle a ``$ORIGIN`` / ``$TTL`` directive; return the new origin."""
    directive = tokens[0].upper()
    if directive == "$ORIGIN":
        if len(tokens) != 2:
            raise MasterFileError(
                line, "$ORIGIN expects exactly one origin argument"
            )
        try:
            return normalize_name(_relative_to(tokens[1], origin))
        except ZoneError as exc:
            raise MasterFileError(line, str(exc)) from exc
    if directive == "$TTL":
        if len(tokens) != 2:
            raise MasterFileError(line, "$TTL expects exactly one TTL argument")
        _validate_ttl(tokens[1], line)
        return origin
    raise MasterFileError(
        line, f"unsupported directive {tokens[0]!r}; only $ORIGIN and $TTL"
    )


def _record(
    tokens: list[str],
    leading_blank: bool,
    origin: str,
    last_name: "str | None",
    line: int,
) -> Record:
    """Parse the tokens of one logical record into a normalized Record."""
    idx = 0
    if not leading_blank:
        try:
            name_token = tokens[0]
        except IndexError:
            raise MasterFileError(line, "empty record") from None
        name = normalize_name(
            _relative_to(name_token, origin), allow_wildcard=True
        )
        idx = 1
    else:
        # A leading blank reuses the previous record's already-expanded name.
        if last_name is None:
            raise MasterFileError(
                line, "record with leading whitespace has no previous name to reuse"
            )
        name = last_name

    # Optional IN class and non-negative decimal TTL, in any order, each at
    # most once. Anything else is the record type and must come next.
    seen_ttl = False
    seen_class = False
    while idx < len(tokens):
        token = tokens[idx]
        upper = token.upper()
        if upper == "IN" and not seen_class:
            seen_class = True
            idx += 1
            continue
        if _TTL_RE.fullmatch(token) and not seen_ttl:
            _validate_ttl(token, line)
            seen_ttl = True
            idx += 1
            continue
        break

    if idx >= len(tokens):
        raise MasterFileError(line, "record is missing a type")
    rtype = tokens[idx].upper()
    if rtype not in SUPPORTED_TYPES:
        raise MasterFileError(
            line, f"unsupported record type {tokens[idx]!r}; only A, CNAME and NS"
        )
    idx += 1
    rest = tokens[idx:]
    if len(rest) != 1:
        raise MasterFileError(
            line,
            f"{rtype} record expects exactly one value, got {len(rest)}",
        )
    raw_value = rest[0]
    if rtype == "A":
        value = normalize_value("A", raw_value)
    else:
        # CNAME/NS targets with no trailing dot are relative to the origin.
        target = _relative_to(raw_value, origin)
        value = normalize_value(rtype, target)
    return name, rtype, value


def parse_master_file(zone_name: str, text: str) -> set[Record]:
    """Parse ``text`` into normalized ``(name, type, value)`` records.

    Only master-file syntax and per-record fields are checked here; the
    returned set still has to pass the whole-zone validation of
    :func:`app.resolver.build_zone`, exactly like ``POST /zones``.
    """
    origin = normalize_name(zone_name)
    last_name: str | None = None
    records: set[Record] = set()
    lines = text.splitlines()
    total = len(lines)
    i = 0

    while i < total:
        # Blank lines and comment-only lines never start a logical record
        # (depth is always 0 between records); comments preserve the name.
        if not lines[i].split(";", 1)[0].strip():
            i += 1
            continue

        start_line = i + 1
        leading_blank = lines[i][0] in " \t"
        tokens, depth = _scan_line(lines[i], 0, start_line)
        i += 1
        while depth > 0:
            if i >= total:
                raise MasterFileError(
                    start_line, "unclosed parenthesis: expected ')'"
                )
            more, depth = _scan_line(lines[i], depth, i + 1)
            tokens.extend(more)
            i += 1

        if not tokens:
            # Parentheses containing only whitespace/comments.
            continue

        # Directives must begin in the first column; whitespace before a
        # "$..." line leaves it in the record stream where it is rejected.
        if not leading_blank and tokens[0].startswith("$"):
            origin = _directive(tokens, origin, start_line)
            continue

        try:
            name, rtype, value = _record(
                tokens, leading_blank, origin, last_name, start_line
            )
        except MasterFileError:
            raise
        except ZoneError as exc:
            raise MasterFileError(start_line, str(exc)) from exc
        last_name = name
        records.add((name, rtype, value))

    return records
