"""serve/tokenizing.py - the token and text plumbing between the engine and the API.

The byte tokenizer, the streaming detokenizer, the stop-string matcher and the turn-tail holder.
None of them touch a process or a socket: they turn token ids into text and decide when that text
has ended. Re-exported by serve/server.py."""
from __future__ import annotations

import codecs

OPENAI_MAX_STOP = 4                                  # OpenAI's limit on `stop`

TAIL_CHECK_PROMPT = ('Before this turn ends, check your work against the request: did you complete every '
                     'task it asked for? If everything is done, reply with the single word "done" and '
                     'nothing else. If anything is still outstanding, do that work now.')
TAIL_CHECK_MAX = 3              # how many times one turn may be asked, at most
                                  # OpenAI's limit on `stop`


TAIL_CONFIRM_CHARS = 120        # a confirmation is a word or two; longer text is the model working on


TAIL_OUTSTANDING = ("not yet", "still need", "still have", "haven't", "have not", "incomplete",
                    "unfinished", "remaining", "i need", "i'll", "i will", "are outstanding",
                    "is outstanding", "pending", "not done", "next step", "continue working", "to do")


TAIL_CONFIRM = ("done", "complete", "completed", "finished", "everything is done", "all tasks done",
                "all tasks complete", "no outstanding", "nothing outstanding", "nothing left",
                "nothing more", "yes")


class ByteTokenizer:
    """Tiny stand-in tokenizer for tests without the pack: one id per UTF-8 byte, specials as ids >= 256."""
    SPECIALS = ["<|im_start|>", "<|im_end|>", "<|endoftext|>", "<|vision_start|>", "<|image_pad|>", "<|vision_end|>"]

    ALWAYS = ()                                     # specials matched without parse_special (type 4, as <think>)

    @property
    def control_tokens(self):
        return [s for s in self.SPECIALS if s not in self.ALWAYS]

    def encode(self, text, parse_special=False, plain=()):
        out, i = [], 0
        while i < len(text):
            for k, s in enumerate(self.SPECIALS):
                if (parse_special or s in self.ALWAYS) and text.startswith(s, i) and not any(
                        a <= i < b for a, b in plain):
                    out.append(256 + k)
                    i += len(s)
                    break
            else:
                out.extend(text[i].encode("utf-8"))
                i += 1
        return out

    def decode(self, ids, errors="replace"):
        raw = bytearray()
        for t in ids:
            raw += self.SPECIALS[t - 256].encode() if t >= 256 else bytes([t])
        return raw.decode("utf-8", errors=errors)


class Detokenizer:
    """Incremental decode: each token's bytes go through an incremental UTF-8 decoder, which emits the complete
    characters and holds a multi-byte character split across tokens until it is complete (invalid bytes become
    U+FFFD, as a whole decode with errors="replace" makes them).  Constant time per token - the old re-decode of
    every generated id cost 2 ms per token after 8K tokens and 4 ms after 16K (perf-review F-1).  A tokenizer
    without `token_bytes` (the tests' byte tokenizer) keeps the re-decode."""

    def __init__(self, tok):
        self.tok, self.ids, self.sent = tok, [], 0
        self.inc = codecs.getincrementaldecoder("utf-8")(errors="replace") if hasattr(tok, "token_bytes") else None

    def pending(self) -> bool:
        """A character is split across the tokens so far: its first bytes are held."""
        if self.inc is not None:
            return bool(self.inc.getstate()[0])
        return self.tok.decode(self.ids).endswith("\ufffd")

    def push(self, t: int) -> str:
        if self.inc is not None:
            return self.inc.decode(self.tok.token_bytes(t))
        self.ids.append(t)
        text = self.tok.decode(self.ids)
        if text.endswith("�"):
            return ""
        delta, self.sent = text[self.sent:], len(text)
        return delta


def stop_strings(req: dict) -> list[str]:
    """The request's stop strings: OpenAI's `stop` (a string or a list of up to 4) or Anthropic's `stop_sequences`
    (a list).  Empty strings are left out.  A bad value is a ValueError, so a 400 before anything is generated."""
    req = req or {}
    if req.get("stop") is not None:
        stop = req["stop"]
        stop = [stop] if isinstance(stop, str) else stop
        if not isinstance(stop, list) or not all(isinstance(x, str) for x in stop):
            raise ValueError("stop must be a string or a list of strings")
        if len(stop) > OPENAI_MAX_STOP:
            raise ValueError(f"stop takes at most {OPENAI_MAX_STOP} strings")
    elif req.get("stop_sequences") is not None:
        stop = req["stop_sequences"]
        if not isinstance(stop, list) or not all(isinstance(x, str) for x in stop):
            raise ValueError("stop_sequences must be a list of strings")
    else:
        return []
    return [x for x in stop if x]


class StopMatcher:
    """Cuts the answer's text at the first stop string.  Text that could still be the start of a stop string is held
    back (only that tail, never more), so a stop string split across tokens or chunks is still found and never sent."""

    def __init__(self, stops: list[str]):
        self.stops, self.held, self.hit = stops, "", None

    def push(self, text: str) -> str:
        """-> the text that is safe to send now; after a match, self.hit is the stop string and nothing more comes."""
        if self.hit is not None:
            return ""
        buf = self.held + text
        first = None
        for s in self.stops:
            i = buf.find(s)
            if i >= 0 and (first is None or i < first[0]):
                first = (i, s)
        if first is not None:
            self.held, self.hit = "", first[1]
            return buf[:first[0]]
        keep = 0                                      # the longest end of buf that a stop string starts with
        for s in self.stops:
            for k in range(min(len(s) - 1, len(buf)), keep, -1):
                if buf.endswith(s[:k]):
                    keep = k
                    break
        self.held = buf[len(buf) - keep:] if keep else ""
        return buf[:len(buf) - keep]

    def flush(self) -> str:
        held, self.held = self.held, ""
        return held


def tail_normalize(text: str) -> str:
    """A reply as words: lower case, punctuation and runs of space gone."""
    return " ".join((text or "").lower().replace(".", " ").replace(",", " ").replace("!", " ").split())


def tail_is_done(text: str) -> bool:
    """A reply to the turn-tail check: is the work finished?

    A turn is still open when the reply says so (an outstanding-work word), when it is longer than a
    word or two (a model carrying on is not confirming), or when there is no reply at all - ignoring
    the question is not an answer.
    """
    t = tail_normalize(text)
    if not t or len(t) > TAIL_CONFIRM_CHARS:
        return False
    if any(w in t for w in TAIL_OUTSTANDING):
        return False
    return any(w in t for w in TAIL_CONFIRM)


class TailHold:
    """A check round's reply, held until it is known whether the work is finished.

    A confirmation is never shown to the client; everything else is the model doing its remaining work
    and is streamed as soon as it is clear. The same shape as the stop-string matcher in Service.run:
    text is emitted only when it cannot still be a confirmation.
    """

    def __init__(self):
        self.held, self.open = "", True

    def push(self, text: str) -> str:
        if not self.open:
            return text
        self.held += text
        t = tail_normalize(self.held)
        if not t:
            return ""                       # only whitespace so far: nothing to say yet
        if len(t) > TAIL_CONFIRM_CHARS or any(w in t for w in TAIL_OUTSTANDING):
            out, self.held, self.open = self.held, "", False
            return out
        return ""

    def flush(self) -> str:
        out, self.held = self.held, ""
        return out
