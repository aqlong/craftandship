"""Remove the credentials from URLs in text, in one pass and in linear time.

One rule, the same as the web app's `redactForCustomer`
(web/src/routes/builds.ts, URL_WITH_CREDENTIALS_RE and DATABASE_URL_RE):

  The credentials of `scheme://userinfo@host` are everything between "://" and
  the LAST "@" of the authority. That is not only "user:password": a token is
  often the whole userinfo (`https://TOKEN@host/repo.git`), a user name can be
  the secret (`https://TOKEN:x-oauth-basic@host`), and a password may hold "@",
  ")" or other characters. All of it is replaced by a mask, the rest of the URL
  stays, so a log line still says which host was meant.

  The authority ends at the first "/", "?" or "#" or at whitespace. For a
  database or queue URL (postgres, mysql, mongodb, redis, amqp) it ends at the
  end of the word instead (whitespace, a quote or an angle bracket), because a
  password in such a URL can hold "/", "?" or "#" and must not be cut short
  there. The price is that a closing parenthesis, or anything else that ends a
  sentence but is not whitespace, goes into the authority of such a URL; if
  that text holds a later "@" the host goes with the userinfo.

No scheme is needed to decide: "://user:secret@" is removed whatever precedes
it. The scan is linear in the length of the text whatever the text is (error
output of up to a few megabytes passes through here): each "://" is handled
once, the search resumes after what it consumed, and the end of a word and its
last "@" are found once per word.
"""

import re

__all__ = ["MASK", "has_url_credentials", "redact_url_credentials"]

MASK = "***"

_DATABASE_SCHEMES = (
    "postgres", "postgresql", "postgresqls", "mysql", "mongodb", "mongodb+srv",
    "redis", "rediss", "amqp", "amqps",
)
_SCHEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
_AUTHORITY_END = re.compile(r"[/?#\s]")
_WORD_END = re.compile(r"""[\s'"<>]""")


def _is_database_url(text: str, separator: int) -> bool:
    """Whether the characters before the "://" at `separator` end in a database scheme."""
    begin = separator
    while begin > 0 and text[begin - 1] in _SCHEME_CHARS:
        begin -= 1
    run = text[begin:separator].lower()
    for scheme in _DATABASE_SCHEMES:
        if run.endswith(scheme) and (len(run) == len(scheme) or not run[-len(scheme) - 1].isalnum()):
            return True
    return False


def _credential_spans(text: str):
    """Yield (start, end) of the userinfo of every URL in `text`, in order and without overlap."""
    separator = text.find("://")
    word_end = -1           # the end of the word the cached values below belong to
    word_last_at = -1       # the last "@" in the word, from its first URL on
    while separator != -1:
        start = separator + 3
        if _is_database_url(text, separator):
            if start >= word_end:
                found = _WORD_END.search(text, start)
                word_end = found.start() if found else len(text)
                word_last_at = text.rfind("@", start, word_end)
            last_at = word_last_at if word_last_at >= start else -1
        else:
            found = _AUTHORITY_END.search(text, start)
            end = found.start() if found else len(text)
            last_at = text.rfind("@", start, end)
        if last_at > start:                      # a non-empty userinfo
            yield start, last_at
            separator = text.find("://", last_at + 1)
        else:
            separator = text.find("://", start)


def redact_url_credentials(text, mask: str = MASK) -> str:
    """`text` with the userinfo of every URL in it replaced by `mask`.

    A value that is not a string is returned as it is.
    """
    if not isinstance(text, str) or "://" not in text:
        return text
    pieces = []
    position = 0
    for start, end in _credential_spans(text):
        pieces.append(text[position:start])
        pieces.append(mask)
        position = end
    if not pieces:
        return text
    pieces.append(text[position:])
    return "".join(pieces)


def has_url_credentials(text) -> bool:
    """Whether `text` holds a URL with a userinfo (the same test the redaction uses)."""
    if not isinstance(text, str) or "://" not in text:
        return False
    return next(_credential_spans(text), None) is not None
