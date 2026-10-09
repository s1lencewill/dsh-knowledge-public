"""Library-level capacity: the one place a per-file ceiling is resolved.

The service enforces three capacity dimensions before it will search a library, and all three fail
closed: a library that does not fit is **rejected**, never silently reduced to the part that does. Two
of them are service-wide constants (how many text files, how many bytes in total). The third - how large
a single text file may be - belongs to the library, because the size of one "document" is a property of
the corpus and not of the service:

* a 2,815,653-byte Markdown note is *one document* in a notes wiki, and a 2,000,000-byte ceiling makes
  the whole wiki unsearchable to keep a bound that was chosen for a different corpus;
* the same ceiling applied to a corpus of extracted papers is a bound on an extraction defect, and
  raising it service-wide would remove that signal from every library at once.

So the ceiling is resolved per library, defaulting to the existing 2,000,000 bytes, and **the same
resolved number is the one authority for every code path that has to decide about a file**:

    snapshot   `evidence.snapshot`        may this file be part of the searchable snapshot?
    read       `reader.resolve`/`read_text`  may this file be read at all?
    catalogue  `catalog.verify_text_file` may a listed document be given a read handle?
    handle     `reader.Issuer`            issued only after one of the above already agreed

A caller that computes its own limit, or that falls back to the default when the configured value is
unusable, reintroduces exactly the disagreement this module exists to prevent - a file the catalogue
offers and the reader then refuses, or vice versa. So a configuration that cannot be honoured is a
**refusal**, never a fallback, and the refusal names the library and the value.

`maxFileBytes` is optional: absent means the default, which is what makes the change backward compatible
with every configuration written before it existed.
"""
import os

#: The per-file ceiling a library gets when it declares none. This is the value every library was
#: already subject to, so an existing configuration keeps exactly the behaviour it had.
DEFAULT_MAX_FILE_BYTES = 2_000_000

#: The largest value a library may declare. A declared ceiling is a promise about memory: the snapshot
#: holds one file's bytes, the reader holds another copy, and the catalogue check reads the file a third
#: time. Above this the promise stops being something this service can keep, so the value is refused
#: rather than honoured - which also keeps "unlimited" from being expressible.
CEILING_MAX_FILE_BYTES = 8_000_000

#: The two dimensions that are *not* per library, kept here so the whole capacity rule is stated in one
#: place and the builder, the snapshot and the authorization check cannot drift apart.
MAX_FILES = 1000
MAX_TOTAL_BYTES = 100_000_000

#: The per-library config key. Absent means `DEFAULT_MAX_FILE_BYTES`.
KEY = "maxFileBytes"

CONFIG_INVALID = "LIBRARY_CONFIG_INVALID"


class CapacityConfigError(Exception):
    """A declared capacity is unusable, so the library that declares it is refused.

    Not a `SnapshotCapacityError`: that one says "this library does not fit the limits", and this one
    says "this library's limits cannot be read". They are different failures with different fixes, and
    collapsing them would tell an operator to trim a corpus when the real problem is a typo.
    """

    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(code + ": " + detail)


def audit(entry):
    """`(value, detail)` for one library entry. Never raises, so a caller can decide the refusal.

    `detail` is None for a usable configuration. A value is usable when it is a whole number of bytes,
    at least 1, and no larger than :data:`CEILING_MAX_FILE_BYTES`. Everything else is refused,
    including:

    * a boolean (`true` is an `int` in Python and would otherwise read as 1 byte);
    * a float, even one with an integral value (`4000000.0`), because a byte count is not approximate;
    * a numeric *string* (`"4000000"`), so a quoted number cannot mean one thing here and another in
      the code that reads the same file;
    * a value outside the range, including the infinities and NaN that JSON cannot carry but a
      hand-edited file might.
    """
    if not isinstance(entry, dict):
        return None, "%s: a library entry must be an object" % CONFIG_INVALID
    raw = entry.get(KEY)
    name = entry.get("id")
    if raw is None:
        return DEFAULT_MAX_FILE_BYTES, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, ("%s: library %r declares %s=%r; a whole number of bytes is required"
                      % (CONFIG_INVALID, name, KEY, raw))
    if raw < 1:
        return None, ("%s: library %r declares %s=%d; it must be at least 1 byte"
                      % (CONFIG_INVALID, name, KEY, raw))
    if raw > CEILING_MAX_FILE_BYTES:
        return None, ("%s: library %r declares %s=%d, above the %d-byte ceiling this service will honour"
                      % (CONFIG_INVALID, name, KEY, raw, CEILING_MAX_FILE_BYTES))
    return raw, None


def resolve(entry):
    """The effective per-file ceiling for one library entry, or :class:`CapacityConfigError`."""
    value, detail = audit(entry)
    if detail is not None:
        raise CapacityConfigError(CONFIG_INVALID, detail)
    return value


def valid(value):
    """The same rule for a value that has no library entry around it (the builder's own argument)."""
    return audit({KEY: value, "id": "argument"})


def root_identity(root):
    """A stable identity for a library root, for binding an artefact to the library it was built for.

    Case-folded, normalised and fully resolved, because that is the comparison the visibility rule
    already makes: `G:\\KB` and `g:\\kb\\` are one directory on Windows, and a binding that called them
    different would refuse a catalogue that is in fact the right one. Not a digest: this value is
    written beside the library root it names, in a file that lives in the same operator-owned directory,
    so hiding it would cost diagnosability and buy nothing.
    """
    return os.path.normcase(os.path.normpath(os.path.realpath(str(root))))
