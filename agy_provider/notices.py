"""Recognize bounded standalone CLI notices across streaming chunks."""

import re

from .errors import classified_error, remote_error


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

    def _end_continuation(self):
        self.continuing = False
        self.continuation_lines = 0
        self._notice_diagnostic = ""

    def _start_notice(self, candidate, *, can_continue):
        self._end_continuation()
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
