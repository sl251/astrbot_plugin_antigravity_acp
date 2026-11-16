"""Recognize bounded standalone CLI notices across streaming chunks."""

import re
from enum import Enum

from .errors import classified_error, remote_error


class UpstreamState(str, Enum):
    UNKNOWN = "UNKNOWN"
    ACCOUNT_QUOTA_EXHAUSTED = "ACCOUNT_QUOTA_EXHAUSTED"
    RATE_LIMIT_OR_CAPACITY = "RATE_LIMIT_OR_CAPACITY"
    TRANSIENT_RETRY = "TRANSIENT_RETRY"
    AUTH_FATAL = "AUTH_FATAL"
    AGENT_FATAL = "AGENT_FATAL"


# Heuristic marker for content-triggered 429 investigation (docs/429 doc P1):
# a generic RESOURCE_EXHAUSTED wording without structured quota details.
# This classifies the CLI *summary* text, not the raw HTTP body; truncation at
# max_diagnostic_bytes may drop trailing structured fields (known limitation).
_DETAIL_FREE_PATTERN = re.compile(
    r"resource has been exhausted \(e\.g\. check quota\)", re.IGNORECASE
)
_STRUCTURED_DETAIL_PATTERN = re.compile(
    r"retryinfo|quotafailure|errorinfo|retrydelay|details[\"'\\s]*[:=\\[]",
    re.IGNORECASE,
)


def detail_free_exhausted(text: str) -> bool:
    """True when the notice looks like a detail-free capacity exhaustion."""
    if not text:
        return False
    if _STRUCTURED_DETAIL_PATTERN.search(text):
        return False
    return bool(_DETAIL_FREE_PATTERN.search(text))


def classify_notice(text: str) -> UpstreamState:
    """Only call on bounded CLI diagnostic envelopes, never answer prose."""
    text = text.lower()
    if re.search(
        r"(?:daily|monthly|account|subscription|billing)[\s\S]{0,80}"
        r"(?:quota|limit|credits?)[\s\S]{0,40}(?:exceeded|exhausted|reached|depleted)\b|"
        r"\bquota[\s_-]+(?:exceeded|exhausted)\b|\binsufficient credits\b", text
    ):
        return UpstreamState.ACCOUNT_QUOTA_EXHAUSTED
    if re.search(r"\b401\b|\bunauthenticated\b|\binvalid (?:credentials?|token)\b|"
                 r"\bauthentication required\b|\btoken (?:has )?expired\b", text):
        return UpstreamState.AUTH_FATAL
    if re.search(r"\b429\b|\bresource_exhausted\b|\btoo many requests\b", text):
        return UpstreamState.RATE_LIMIT_OR_CAPACITY
    if re.search(r"\b503\b|\bunavailable\b|\bno capacity\b", text):
        return UpstreamState.TRANSIENT_RETRY
    if "agent execution terminated due to error." in text:
        return UpstreamState.AGENT_FATAL
    return UpstreamState.UNKNOWN


class RetryNoticeGuard:
    """Discard complete CLI retry and stream-interrupt notices before streaming."""

    # Empirical captures (CLI 1.5.6-1.5.8) show CLI status notices arrive on
    # stdout agent_message_chunk (stderr stays empty), often preceded by a leading
    # newline, and API error dumps may wrap '"https?://...' onto a continuation line.
    heads = ("⚠ API error (attempt ", "⚠️ API error (attempt ")
    interrupt_heads = (
        "⚠ The stream was interrupted. Please continue",
        "⚠️ The stream was interrupted. Please continue",
    )
    header = re.compile(r"^⚠\ufe0f? API error \(attempt [0-9]+\):")
    interrupt_header = re.compile(
        r"^⚠\ufe0f? The stream was interrupted\. Please continue"
    )
    # Match wrapped URL continuation lines by line shape rather than chunk boundary;
    # blank lines, new ⚠ headers, terminal markers, non-URL lines, 2 lines, or 4096B stop it.
    continuation_prefixes = ('"http://', '"https://')
    continuation_header = re.compile(r'^"https?://')
    max_continuation_lines = 2
    max_diagnostic_bytes = 4096

    def __init__(self):
        self.pending = ""
        self.leading_blanks = ""
        self.emitted = False
        self.line_start = True
        self.dropping = False
        self.continuing = False
        self._can_continue = False
        self.continuation_lines = 0
        self.diagnostic = ""
        self._notice_diagnostic = ""
        self.error_code = ""
        self.state = UpstreamState.UNKNOWN
        self.fast_fail_code = ""
        self.rate_limit_notices = 0
        self._rate_counted = False
        self._attempt = ""
        self._last_rate_attempt = None
        # Content-429 candidate observability (docs/429 doc P1): sticky across
        # notices within one turn, recalculated as diagnostic text grows.
        self.detail_free = False
        self.detail_free_seen = False

    @property
    def attempt_number(self) -> str | None:
        return self._attempt or None

    def _possible(self, text):
        for head in self.heads:
            if head.startswith(text):
                return True
            if text.startswith(head):
                tail = text[len(head):]
                if re.fullmatch(r"[0-9]+\)?", tail):
                    return True
        return any(head.startswith(text) for head in self.interrupt_heads)

    def _possible_continuation(self, text):
        return bool(text) and any(
            prefix.startswith(text) for prefix in self.continuation_prefixes
        )

    def _append_diagnostic(self, text, *, separator=""):
        # Aggregate header + continuation lines up to 4096B before classifying via
        # remote_error(..., retry_notice=True); notice text is never emitted.
        if separator and len(self._notice_diagnostic) < self.max_diagnostic_bytes:
            self._notice_diagnostic += separator
        notice_room = max(0, self.max_diagnostic_bytes - len(self._notice_diagnostic))
        notice_added = text[:notice_room]
        line_room = max(0, self.max_diagnostic_bytes - len(self.diagnostic))
        line_added = text[:min(notice_room, line_room)]
        if line_added:
            self.diagnostic += line_added
        if notice_added or not self.error_code:
            self._notice_diagnostic += notice_added
            self.error_code = remote_error(
                self._notice_diagnostic, retry_notice=True
            ).code
            self.state = classify_notice(self._notice_diagnostic)
            self.detail_free = (
                self.state == UpstreamState.RATE_LIMIT_OR_CAPACITY
                and detail_free_exhausted(self._notice_diagnostic)
            )
            if self.detail_free:
                self.detail_free_seen = True
            if self.state == UpstreamState.ACCOUNT_QUOTA_EXHAUSTED:
                self.fast_fail_code = "QUOTA"
            elif self.state == UpstreamState.AUTH_FATAL:
                self.fast_fail_code = "AUTH"
            elif self.state == UpstreamState.RATE_LIMIT_OR_CAPACITY:
                if not self._rate_counted and self._attempt != self._last_rate_attempt:
                    self.rate_limit_notices += 1
                    self._rate_counted = True
                    self._last_rate_attempt = self._attempt
                if self.rate_limit_notices >= 2:
                    self.fast_fail_code = "RATE_LIMIT"
            elif self.state == UpstreamState.TRANSIENT_RETRY:
                self.rate_limit_notices = 0

    def _end_continuation(self):
        self.continuing = False
        self.continuation_lines = 0
        self._notice_diagnostic = ""

    def _start_notice(self, candidate, *, can_continue):
        self._end_continuation()
        self._rate_counted = False
        self.detail_free = False
        match = re.match(r"^⚠\ufe0f? API error \(attempt ([0-9]+)\):", candidate)
        self._attempt = match[1] if match else ""
        self.diagnostic = ""
        self._can_continue = can_continue
        self._append_diagnostic(candidate)

    def _finish_dropped_line(self):
        self.dropping = False
        self.diagnostic = ""
        self.line_start = True
        if self.continuing:
            if (
                self.continuation_lines >= self.max_continuation_lines
                or len(self._notice_diagnostic) >= self.max_diagnostic_bytes
            ):
                self._end_continuation()
        elif (
            self._can_continue
            and len(self._notice_diagnostic) < self.max_diagnostic_bytes
        ):
            self.continuing = True
        else:
            self._end_continuation()
        self._can_continue = False

    def _drop_line(self, newline):
        if newline:
            self._finish_dropped_line()
        else:
            self.dropping = True

    def _emit(self, output, chunk):
        if chunk.strip():
            self.rate_limit_notices = 0
        if self.leading_blanks:
            output.append(self.leading_blanks)
            self.leading_blanks = ""
        output.append(chunk)
        self.emitted = True

    def feed(self, text, *, final=False):
        text = self.pending + text
        self.pending = ""
        output = []
        while text:
            line, newline, rest = text.partition("\n")
            if self.dropping:
                self._append_diagnostic(line.rstrip("\r"))
                if newline:
                    self._finish_dropped_line()
                text = rest
                continue
            if self.line_start and self.continuing:
                trimmed = line.rstrip("\r")
                if self.continuation_header.match(trimmed):
                    self.continuation_lines += 1
                    self._append_diagnostic(trimmed, separator="\n")
                    self._drop_line(newline)
                    text = rest
                    continue
                if (
                    not newline
                    and not final
                    and len(line) <= self.max_diagnostic_bytes
                    and self._possible_continuation(line)
                ):
                    self.pending = line
                    break
                self._end_continuation()
            if self.line_start:
                # CLI prefixes real alerts with a leading "\n"; hold pre-content empty
                # lines transiently so standalone alerts emit zero bytes, while flushing
                # them intact before any normal text or at final=True.
                if (
                    not self.emitted
                    and line in ("", "\r")
                    and newline
                    and len(self.leading_blanks) + len(line) + len(newline)
                    <= self.max_diagnostic_bytes
                ):
                    self.leading_blanks += line + newline
                    self.line_start = True
                    text = rest
                    continue
                candidate = line.lstrip()
                can_continue = bool(self.header.match(candidate))
                if can_continue or self.interrupt_header.match(candidate):
                    self.leading_blanks = ""
                    self._start_notice(
                        candidate.rstrip("\r"), can_continue=can_continue
                    )
                    self._drop_line(newline)
                    text = rest
                    continue
                if (
                    not newline
                    and not final
                    and len(line) <= self.max_diagnostic_bytes
                    and self._possible(candidate)
                ):
                    self.pending = line
                    break
            self._emit(output, line + newline)
            self.line_start = bool(newline)
            text = rest
        if final:
            if self.leading_blanks:
                output.append(self.leading_blanks)
                self.leading_blanks = ""
                self.emitted = True
            self._end_continuation()
            self._can_continue = False
            self.dropping = False
            self.diagnostic = ""
        return "".join(output)


class TerminalNoticeGuard:
    messages = {
        "there was a network issue connecting to the server, please try again": "NETWORK",
        "our servers are experiencing high traffic right now, please try again in a minute": "RATE_LIMIT",
    }

    def __init__(self):
        self.pending = ""
        self.line_start = True

    def _verdict(self, raw, final):
        if len(raw) > 4096:
            return "emit"
        text = re.sub(r"\s+", " ", raw.replace("\ufe0f", "")).strip().lower()
        marked = text.startswith("⚠")
        if marked:
            text = text[1:].lstrip()
            if "error:".startswith(text) and not final:
                return "hold"
            if text.startswith("error:"):
                text = text[6:].lstrip()
        for head, code in self.messages.items():
            if text in {head, head + "."}:
                if final or (marked and text.endswith(".")):
                    raise classified_error(code)
                return "hold"
            if not final and (head + ".").startswith(text):
                return "hold"
        return "emit"

    def feed(self, text, *, final=False):
        output = []
        for piece in text.splitlines(keepends=True):
            candidate = self.pending + piece
            self.pending = ""
            if self.line_start and self._verdict(candidate, False) == "hold":
                self.pending = candidate
            else:
                output.append(candidate)
                self.line_start = candidate.endswith(("\n", "\r"))
        if final and self.pending:
            self._verdict(self.pending, True)
            output.append(self.pending)
            self.line_start = self.pending.endswith(("\n", "\r"))
            self.pending = ""
        return "".join(output)
