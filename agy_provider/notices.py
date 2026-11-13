"""Recognize bounded standalone CLI notices across streaming chunks."""

import re

from .errors import classified_error, remote_error


class RetryNoticeGuard:
    """Discard complete CLI retry lines before they reach any streaming consumer."""

    heads = ("⚠ API error (attempt ", "⚠️ API error (attempt ")
    header = re.compile(r"^⚠\ufe0f? API error \(attempt [0-9]+\):")

    def __init__(self):
        self.pending = ""
        self.line_start = True
        self.dropping = False
        self.diagnostic = ""
        self.error_code = ""

    def _possible(self, text):
        for head in self.heads:
            if head.startswith(text):
                return True
            if text.startswith(head):
                tail = text[len(head):]
                if re.fullmatch(r"[0-9]+\)?", tail):
                    return True
        return False

    def _discard(self, text):
        # Retain only a bounded diagnostic for classification, never for output.
        added = text[:max(0, 4096 - len(self.diagnostic))]
        if added or not self.error_code:
            self.diagnostic += added
            self.error_code = remote_error(self.diagnostic).code

    def feed(self, text, *, final=False):
        text = self.pending + text
        self.pending = ""
        output = []
        while text:
            line, newline, rest = text.partition("\n")
            if self.dropping:
                self._discard(line)
                if newline:
                    self.dropping = False
                    self.diagnostic = ""
                    self.line_start = True
                text = rest
                continue
            if self.line_start:
                candidate = line.lstrip()
                if self.header.match(candidate):
                    self._discard(candidate)
                    self.dropping = not bool(newline)
                    if newline:
                        self.diagnostic = ""
                    text = rest
                    continue
                if not newline and not final and len(line) <= 4096 and self._possible(candidate):
                    self.pending = line
                    break
            output.append(line + newline)
            self.line_start = bool(newline)
            text = rest
        if final:
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
