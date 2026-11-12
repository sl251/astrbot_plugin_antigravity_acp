"""Recognize bounded standalone CLI notices across streaming chunks."""

import re

from .errors import classified_error


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
