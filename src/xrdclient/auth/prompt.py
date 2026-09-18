"""Asking a person for the credential this machine does not have.

A library that prompts is a library that hangs in a cron job, so the rules
are deliberately narrow. The ladder only comes here when every mechanism was
unusable, only for a mechanism the server actually offered, only when the
terminal is really a terminal, and only for material somebody could plausibly
type - a proxy path or a token, never a Kerberos ticket. Answers are
remembered per ``(mechanism, host)``, so the second connection of a session
is silent, and a refusal is remembered just as firmly as an answer.

The question itself carries three things, because "authentication failed" is
what the C client says and nobody has ever been helped by it: *what* is
missing, *why* the usual place did not have it, and the command that normally
produces it.

    >>> from xrdclient import Config
    >>> Config(prompt=False)                     # never ask, fail instead
    >>> Config(prompter=my_gui_dialog)           # ask somewhere else
"""

from __future__ import annotations

import getpass
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from .._compat import SLOTS
from .._log import get_logger

if TYPE_CHECKING:
    from ..config import Config

__all__ = [
    "Ask",
    "Prompter",
    "answer_for",
    "ask_on_terminal",
    "forget",
    "humanise",
    "interactive",
    "remember",
]

_log = get_logger(__name__)


@dataclass(frozen=True, **SLOTS)
class Ask:
    """One request for credential material a person could supply.

    Prompters receive these and return what was typed, or ``None`` to decline.
    Nothing here is secret - the answer may be, which is what :attr:`secret`
    is for - so an ``Ask`` is safe to log.
    """

    #: Wire name of the mechanism that wants something (``"gsi"``, ``"ztn"``).
    mechanism: str
    #: What is missing, as a noun phrase: ``"an X.509 proxy"``.
    what: str
    #: Why the usual place did not have it: ``"there is no file at /tmp/..."``.
    reason: str
    #: The command that normally produces it: ``"voms-proxy-init -voms lhcb"``.
    hint: str
    #: Label for the input line: ``"path to a proxy file"``.
    prompt: str
    #: The endpoint that asked, when there is one.
    host: str = ""
    #: Whether the answer must not be echoed or logged.
    secret: bool = False

    @property
    def key(self) -> tuple[str, str]:
        """What an answer is remembered under."""
        return (self.mechanism, self.host)

    def explain(self) -> str:
        """The two or three lines a terminal prompter prints above the question."""
        offered = f"{self.mechanism} is offered"
        where = f"{self.host} accepts {self.mechanism}" if self.host else offered
        return (
            f"xrd: {where}, but {self.what} is missing\n"
            f"     why: {self.reason}\n"
            f"     fix: {self.hint}\n"
        )

    def question(self) -> str:
        """The input line itself, ending in the cursor's own space."""
        hidden = ", not echoed" if self.secret else ""
        return f"     {self.prompt} (Enter to skip{hidden}): "


#: What :attr:`xrdclient.Config.prompter` has to be: given an :class:`Ask`, return
#: what the person typed, or ``None`` to decline.
Prompter = Callable[[Ask], "str | None"]


def humanise(seconds: float) -> str:
    """A duration short enough to read inside a prompt: ``4500`` is ``1h 15m``."""
    whole = int(abs(seconds))
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m"
    if whole < 86400:
        return f"{whole // 3600}h {whole % 3600 // 60}m"
    return f"{whole // 86400}d {whole % 86400 // 3600}h"


def _is_terminal(stream: IO[str] | None) -> bool:
    """Whether ``stream`` is a real terminal, forgiving anything that is not a stream."""
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError):  # replaced by a capture object, or closed
        return False


def interactive(config: Config) -> bool:
    """Whether this process may ask a person for credentials.

    ``config.prompt`` decides when it is set - including ``False`` for a
    daemon that must fail rather than block. Left at ``None`` it means "if
    there is somebody there": both the input and the channel the question
    goes out on have to be a terminal.
    """
    if config.prompt is not None:
        return config.prompt
    return _is_terminal(sys.stdin) and _is_terminal(sys.stderr)


def ask_on_terminal(ask: Ask) -> str | None:
    """The default prompter: ask on ``stderr``, read from ``stdin``.

    Never ``stdout``, because ``xrd-fs cat`` pipes it and a question in the
    middle of the bytes would be a corruption. A secret answer goes through
    :func:`getpass.getpass`, so it is not echoed and never reaches the
    terminal's scrollback.
    """
    stream = sys.stderr
    stream.write(ask.explain())
    stream.flush()
    try:
        if ask.secret:
            typed = getpass.getpass(ask.question(), stream=stream)
        else:
            stream.write(ask.question())
            stream.flush()
            typed = sys.stdin.readline()
            if not typed:  # end of input: the terminal never got its newline
                stream.write("\n")
    except EOFError:  # getpass's way of saying the same thing
        stream.write("\n")
        return None
    return typed.strip() or None


_ANSWERS: dict[tuple[str, str], str | None] = {}
_LOCK = threading.RLock()


def remember(ask: Ask, answer: str | None) -> None:
    """Record ``answer`` - including ``None``, a refusal - for this endpoint."""
    with _LOCK:
        _ANSWERS[ask.key] = answer


def forget(ask: Ask | None = None) -> None:
    """Forget one remembered answer, or every one of them.

    Worth calling when a pasted token has served its purpose: the answers
    live in this process only, but they do live for as long as it does.
    """
    with _LOCK:
        if ask is None:
            _ANSWERS.clear()
        else:
            _ANSWERS.pop(ask.key, None)


def answer_for(ask: Ask, config: Config) -> str | None:
    """Put ``ask`` to a person, once per endpoint, and give back what they said.

    The lock is held across the prompt on purpose: two threads racing for one
    terminal produce two interleaved questions and one usable answer, so the
    second thread waits and then finds the first thread's answer waiting.
    """
    with _LOCK:
        if ask.key in _ANSWERS:
            return _ANSWERS[ask.key]
        prompter: Prompter = config.prompter or ask_on_terminal
        _log.debug("asking for %s material for %s", ask.mechanism, ask.host or "the endpoint")
        answer = prompter(ask)
        _ANSWERS[ask.key] = answer
        return answer
