"""Stand-in codes for people and model files in the support bundle, so
student identities don't leave the site with it.

Every user and admin gets a short code like "user-3f9a2c", and every
model filename one like "file-81bc04.stl" (filenames often carry a
student's name - "Ocarina of Time ANDRE PHAN.stl"). The codes are an HMAC
with a secret that only exists on this Pi (data/bundle_pseudonym_key), so:

- the same person or file gets the same code in every bundle, which keeps
  bundles from different days comparable;
- the codes can't be turned back into names off site, even by guessing
  likely names, without that secret;
- an admin on site can look a code up (routers/admin.py's support bundle
  codes page).

Free text - feedback, rejection notes, failure reasons, log details, the
service journal - is scrubbed of every known name and filename too. That
part is best effort: a name someone types that isn't an account (a
classmate's, say) can't be recognised.
"""

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote, quote_plus

from sqlmodel import Session, select

from db import DATA_DIR
from models import Admin, Feedback, Job, JobEvent, User

_KEY_PATH = DATA_DIR / "bundle_pseudonym_key"
# Name parts shorter than this aren't scrubbed on their own - "Al" or "Jo"
# would also match ordinary words. Full names always are.
_MIN_NAME_PART = 3


def _key() -> bytes:
    """The site's secret, created on first use. Kept with the live data
    (not in the database, which is itself copied into the bundle)."""
    try:
        return bytes.fromhex(_KEY_PATH.read_text().strip())
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key.hex())
        return key


def _code(key: bytes, kind: str, value: str) -> str:
    return hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:6]


def person_code(kind: str, name: str, key: bytes | None = None) -> str:
    """"user-3f9a2c" / "admin-9b01d4" for an account name."""
    return f"{kind}-{_code(key or _key(), kind, name)}"


def file_code(filename: str, key: bytes | None = None) -> str:
    """"file-81bc04.stl" for a model filename - keeps the extension, which
    says something useful (what format was uploaded) and nothing personal."""
    return f"file-{_code(key or _key(), 'file', filename)}{Path(filename).suffix.lower()}"


def _with_url_forms(mapping: dict[str, str]) -> dict[str, str]:
    """The mapping plus each key's URL-encoded spellings, to the same value."""
    out = dict(mapping)
    for key, value in mapping.items():
        for form in (quote(key), quote_plus(key)):
            out.setdefault(form, value)
    return out


class Scrubber:
    """Replaces every known name and filename in a piece of text. Built
    once per bundle from everything the database knows: current accounts,
    every job's filename, and the names in log/feedback actors and
    review records, which also covers accounts deleted since."""

    def __init__(self, session: Session):
        self.key = _key()
        self.people: dict[tuple[str, str], str] = {}  # (kind, name) -> code
        for user in session.exec(select(User)).all():
            self._add_person("user", user.name)
        for admin in session.exec(select(Admin)).all():
            self._add_person("admin", admin.username)
        actors = [e.actor for e in session.exec(select(JobEvent)).all()]
        actors += [fb.actor for fb in session.exec(select(Feedback)).all()]
        for actor in actors:
            kind, _, name = actor.partition(":")
            if kind in ("user", "admin") and name and not name.startswith("#"):
                self._add_person(kind, name)
        jobs = session.exec(select(Job)).all()
        for job in jobs:
            if job.reviewed_by_name:
                self._add_person("admin", job.reviewed_by_name)

        filenames = {job.original_filename for job in jobs if job.original_filename}
        filenames |= {fb.job_filename for fb in session.exec(select(Feedback)).all() if fb.job_filename}
        self.files = {name: file_code(name, self.key) for name in filenames}
        # Text to scrub also holds URL-encoded forms - the journal logs
        # request URLs, and a filter like ?user=Andre%20Phan carries a name.
        self._file_variants = _with_url_forms(self.files)

        # Whole names first, then each part of a multi-word name on its
        # own (a first name alone in a note), longest first so "Ali Arman"
        # wins over "Ali". A part shared by two people becomes "[name]".
        names: dict[str, str] = {}
        parts: dict[str, set[str]] = {}
        for (kind, name), code in self.people.items():
            names[name.lower()] = code
            for part in name.split():
                if len(part) >= _MIN_NAME_PART:
                    parts.setdefault(part.lower(), set()).add(code)
        for part, codes in parts.items():
            names.setdefault(part, codes.pop() if len(codes) == 1 else "[name]")
        self._names = {k.lower(): v for k, v in _with_url_forms(names).items()}
        self._name_re = self._alternation(self._names, word=True)
        self._file_re = self._alternation(self._file_variants, word=False)

    def _add_person(self, kind: str, name: str) -> None:
        self.people.setdefault((kind, name), person_code(kind, name, self.key))

    @staticmethod
    def _alternation(values, word: bool):
        if not values:
            return None
        body = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
        if word:
            return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)
        return re.compile(body)

    def person(self, kind: str, name: str) -> str:
        return self.people.get((kind, name)) or person_code(kind, name, self.key)

    def actor(self, actor: str) -> str:
        """"user:Andre Phan" -> "user:user-3f9a2c"; "system" unchanged."""
        kind, sep, name = actor.partition(":")
        if kind in ("user", "admin") and sep and name and not name.startswith("#"):
            return f"{kind}:{self.person(kind, name)}"
        return actor

    def filename(self, name: str | None) -> str | None:
        if not name:
            return name
        return self.files.get(name) or file_code(name, self.key)

    def text(self, text: str | None) -> str | None:
        if not text:
            return text
        if self._file_re:
            text = self._file_re.sub(lambda m: self._file_variants[m.group(0)], text)
        if self._name_re:
            text = self._name_re.sub(lambda m: self._names[m.group(0).lower()], text)
        return text
