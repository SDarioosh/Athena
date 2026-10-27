"""Zoom display names to people (Zoom bot plan, lane 2B).

A meeting session holds one track per Zoom participant (``Store.tracks``). This module
turns a track's display names into the speaker name written on its lines:

1. ``clean_display_name`` drops what Zoom adds to a name: pronouns, "| Company",
   " - Company", "@ Company", device suffixes ("Kath's iPhone"), emoji and stray
   punctuation, and fixes ALL-CAPS or all-lowercase names. A name that is only a device,
   a room or a generic label ("iPhone", "Zoom user", "Conference Room") cleans to "".
2. ``speaker_for_track`` matches the cleaned names against the owner's Zoom names
   (``CONVO_ZOOM_OWNER_NAMES``), then against people names and aliases, exactly (any
   case). An unknown name becomes a new person. Every display name the track used is
   kept as an alias, so the next meeting finds the same person and a participant who
   renamed mid-meeting stays one person.

Matching is deliberately strict: "Kath Dlamini" does not match a person called "Kath",
because a wrong merge mixes two people's facts while a new person is fixed with one
``rename_speaker_everywhere`` (which also keeps the Zoom name as an alias for next time).

Imported lazily by the meeting pipeline, never at server startup.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import Store

# Label for a track with no usable name; the existing unnamed-speaker flow skips it and
# ``name_speaker`` can rename it. ``speakers_for_tracks`` numbers them "Speaker N".
PLACEHOLDER = "Unknown"

_DEVICE = (r"iphone|ipad|ipod|android|galaxy|pixel|macbook|imac|mac|chromebook|laptop|"
           r"desktop|pc|tablet|phone|mobile|cell|computer|surface|windows|linux|ubuntu|"
           r"huawei|xiaomi|oneplus|motorola|moto|nokia|samsung|apple|google|lenovo|dell|hp")
_MODEL = (r"pro|air|max|mini|plus|ultra|fold|flip|note|lite|se|xps|latitude|inspiron|"
          r"optiplex|precision|vostro|thinkpad|ideapad|elitebook|zenbook|[a-z]?\d+[a-z]*")
# Device words that are also names (QA BUG-09): "Mac" is a common short name and "Dell" a
# given name and surname, so alone they are a person; "Mac Pro", "Dell XPS 13", "Kath's
# Mac" are devices. "Apple" and "Google" stay devices: a Zoom name that is only the
# company is a shared or test account far more often than a person called that. iPhone,
# iPad, Galaxy, Pixel, Android, MacBook, Chromebook, laptop, desktop have no use as a
# name at all.
_NAME_LIKE_DEVICE = {"mac", "dell"}
_PRONOUN = r"(?:he|him|his|she|her|hers|they|them|theirs|ze|zir|hir|xe|xem|any|all)"

_PRONOUNS_RE = re.compile(rf"\b{_PRONOUN}(?:\s*/\s*{_PRONOUN})+\b", re.IGNORECASE)
_BRACKETS_RE = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}|<[^<>]*>")
_SEPARATOR_RE = re.compile(r"\s*[|•·]\s*|\s+[-–—]+\s+|\s+//\s*|\s+@\s*|\s*@\s+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# "Kath's iPhone", "James' Galaxy S21": the name before the device
_POSSESSIVE_DEVICE_RE = re.compile(rf"\s*'s?\s+(?:my\s+)?(?:{_DEVICE})\b.*$", re.IGNORECASE)
# "iPhone de Kath", "iPad von Jan"
_DEVICE_OF_RE = re.compile(rf"^(?:{_DEVICE})(?:\s+(?:{_MODEL}))*\s+(?:de|di|da|von|van|of)\s+",
                           re.IGNORECASE)
# "Kath iPhone", "Kath Galaxy S21" (only unambiguous device words; not "Mac" or "Pixel")
_TRAILING_DEVICE_RE = re.compile(
    rf"\s+(?:iphone|ipad|ipod|android|galaxy|macbook|imac|chromebook|laptop|desktop|pc|"
    rf"tablet)(?:\s+(?:{_MODEL}))*\s*$", re.IGNORECASE)
_ONLY_DEVICE_RE = re.compile(
    rf"(?:(?:my|the|new|old)\s+)?(?:(?:{_DEVICE})\s*)+(?:\s*(?:{_MODEL}))*", re.IGNORECASE)
_HOSTNAME_RE = re.compile(r"(?:desktop|laptop|pc|win|macbook(?:-pro|-air)?|ipad|iphone)"
                          r"[-_][a-z0-9-]+", re.IGNORECASE)
_ROOM_RE = re.compile(r"\b(?:conference|meeting|board|huddle|zoom|focus|team)\s*rooms?\b|"
                      r"^rooms?\b|\brooms?\s*[a-z]?\d|\b(?:boardroom|auditorium)\b",
                      re.IGNORECASE)
# Rooms and conference hardware (QA BUG-01): a track that can hold several people, never
# one person. Agrees with lane 2A's ``meeting_tracks.is_shared_room`` on the clear cases.
# Product words with no use as a name match anywhere in the name ("Polycom Trio 8800").
_HARDWARE_RE = re.compile(r"\b(?:polycom|crestron|logitech|yealink|webex|speakerphone|meetup|"
                          r"zoom\s*rooms?|dial-?in)\b", re.IGNORECASE)
# Words that are also names, or surnames ("Hall", "Cisco", "Jabra", "Rally"), only as the
# whole name: alone when no one is called that, else with a room or product word or a
# number. "Main Hall", "Hall 2", "Neat Bar", "Meeting Owl 3" are rooms; "Jordan Hall",
# "Cisco Ramirez", "Neat", "Owl" are names.
_NUM = r"(?:[a-z]?\d+[a-z0-9]*|[a-z])"  # "4", "4b", "a20", "b"
_ROOM_ONLY_RE = re.compile(
    rf"(?:(?:main|front)\s+)?lobby(?:\s+{_NUM})?|"
    rf"(?:main|conference|town|lecture|great|dining|assembly|event|meeting|concert|sports|"
    rf"exhibition|banquet|function|study|music|community|church|village|entrance)\s+hall"
    rf"(?:\s+{_NUM})?|hall\s+{_NUM}|"
    rf"huddle(?:\s+(?:room|space|area|pod))?(?:\s+{_NUM})?|"
    rf"conf(?:erence)?\.?(?:\s*(?:rm|room|call|line|bridge|phone)\.?)?(?:\s+{_NUM})?|"
    rf"rm\.?\s*{_NUM}|"
    rf"cisco(?:\s+(?:webex|room|kit|desk|board|codec|bar|pro|plus|mini|navigator|touch|"
    rf"{_NUM}))*|"
    rf"jabra(?:\s+(?:speak|panacast|evolve|link|{_NUM}))*|"
    rf"(?:logitech\s+)?rally(?:\s+(?:bar|plus|mini|camera|board|{_NUM}))*|"
    rf"neat\s+(?:bar|board|pad|frame|center)(?:\s+(?:pro|{_NUM}))*|"
    rf"(?:meeting\s+owl|owl\s+(?:pro|labs|bar))(?:\s+(?:pro|{_NUM}))*|"
    rf"poly\s+(?:studio|trio|sync|voyager|g\d+|x\d+)(?:\s+{_NUM})*|"
    rf"surface\s+hub(?:\s+{_NUM})*|"
    rf"(?:(?:samsung|galaxy)\s+)*sm-[a-z]?\d+[a-z0-9]*")  # Samsung model "SM-G991B"
_GENERIC = {"zoom user", "zoom", "user", "guest", "host", "co-host", "cohost", "participant",
            "attendee", "panelist", "anonymous", "unknown", "admin", "test", "me", "owner",
            "speaker", "audio", "phone user", "call-in user", "dial-in user"}
_PLACEHOLDER_RE = re.compile(r"(?:speaker\s*\d+|unknown|call-?in\s*user\s*\d*|"
                             r"zoom\s*user\s*\d*|guest\s*\d*|user\s*\d*|\+?[\d\s().-]{5,})",
                             re.IGNORECASE)
_TITLE_RE = re.compile(r"^(?:dr|prof|mr|mrs|ms|mx|sir|dame)\.?\s+(?=\S)", re.IGNORECASE)
_CREDENTIALS_RE = re.compile(r"\s*,?\s+\b(?:phd|ph\.d\.?|md|m\.d\.?|mba|msc|bsc|cpa|pmp|"
                             r"esq|rn|dds)\.?(?:\s*,?\s*(?:phd|md|mba|msc|cpa|pmp))*$",
                             re.IGNORECASE)
_PARTICLES = {"van", "von", "der", "den", "de", "da", "di", "du", "del", "della", "la", "le",
              "dos", "das", "do", "bin", "al", "ter", "ten", "y"}
_KEEP_PUNCT = set(" '.-,|•·@()[]{}<>/&_+–—")
_EDGE_PUNCT = " -–—_.,;:'/&|@+"


def looks_like_device(name: str) -> bool:
    """Whether a display name names a device, a room or a generic label instead of a
    person: "iPhone", "Galaxy S21", "DESKTOP-4KQ2", "Conference Room 2", "Zoom user".

    Used for cleaning only. Lane 2A's shared-room detector (``meeting_tracks.py``) decides
    which tracks get diarized; the two should become one helper once both have landed.
    """
    n = re.sub(r"\s+", " ", _strip_symbols(name)).strip(_EDGE_PUNCT).lower()
    if not n:
        return True
    return bool(n in _GENERIC or _PLACEHOLDER_RE.fullmatch(n)
                or (_ONLY_DEVICE_RE.fullmatch(n) and n not in _NAME_LIKE_DEVICE)
                or _HOSTNAME_RE.fullmatch(n) or _ROOM_RE.search(n) or looks_like_room(n))


def looks_like_room(name: str) -> bool:
    """Whether a display name is a room or conference hardware ("Huddle 4", "Conf Rm A",
    "Polycom", "Logitech Rally", "Lobby", "SM-G991B"): a track several people can share.
    """
    n = re.sub(r"\s+", " ", _strip_symbols(name)).strip(_EDGE_PUNCT).lower()
    return bool(n and (_ROOM_RE.search(n) or _HARDWARE_RE.search(n)
                       or _ROOM_ONLY_RE.fullmatch(n)))


def clean_display_name(raw: str) -> str:
    """A Zoom display name as a person's name, or "" when it names no person.

    "Kath Dlamini (she/her) | Acme" -> "Kath Dlamini"; "KATH'S IPHONE" -> "Kath";
    "priya raman" -> "Priya Raman"; "iPhone" and "Zoom user" -> "". Hyphenated names
    ("Smith-Jones") and mixed case ("McDonald", "van der Berg", "O'Neil") are kept.
    """
    s = _strip_symbols(raw or "")
    s = re.sub(r"\s+", " ", s).strip()
    if _EMAIL_RE.fullmatch(s):  # "kath.dlamini@acme.example" -> "kath dlamini"
        s = re.sub(r"[._]+", " ", s.split("@", 1)[0].split("+", 1)[0])
    parts = [p for p in _SEPARATOR_RE.split(_drop_brackets(s)) if p.strip(_EDGE_PUNCT)]
    if any(looks_like_room(p) for p in parts):  # "Boardroom | Acme": a room, no one
        return ""
    # the first part that is a name: "she/her | Kath", "iPhone | Kath" -> "Kath" (QA BUG-10)
    return next((c for c in map(_clean_part, parts) if c), "")


def _clean_part(s: str) -> str:
    """One part of a display name between separators, as a person's name, or ""."""
    s = _PRONOUNS_RE.sub(" ", s)
    if looks_like_device(s):  # "My iPhone": not "My" once the device is cut off
        return ""
    s = _DEVICE_OF_RE.sub("", s)
    s = _POSSESSIVE_DEVICE_RE.sub("", s)
    s = _TRAILING_DEVICE_RE.sub("", s)
    s = _CREDENTIALS_RE.sub("", _TITLE_RE.sub("", s.strip()))
    if " " not in s.strip() and re.search(r"[a-z][._][a-z]", s):  # "kath_dlamini"
        s = re.sub(r"[._]+", " ", s)
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip(" -–—_,;:'/&|@+")
    s = s.rstrip(".") if not re.search(r"\b[^\W\d_]{1,2}\.$", s) else s
    s = s.lstrip(".").strip()
    s = re.sub(r"\s*,\s*$", "", s)
    s = _fix_case(s)
    if not re.search(r"[^\W\d_]", s) or looks_like_device(s):
        return ""
    return s[:100].strip()


def speaker_for_track(store: Store, track_info: Mapping[str, Any],
                      owner_names: Sequence[str]) -> tuple[str, bool]:
    """The speaker name to write on a meeting track's lines, and whether it is the owner.

    ``track_info`` is one entry of ``Store.tracks``: ``name`` (latest display name) and
    ``names`` (every display name the track used, in first-use order). The track is the
    owner when any of its names, raw or cleaned, matches a name in ``owner_names`` (even
    one that cleans to nothing, such as "Me"); else
    the first of its names (latest first, cleaned before raw) that is a person's name or
    alias picks that person; else a new person is made from the latest cleaned name. All
    display names of the track, raw and cleaned, become aliases of that person. A track
    with no usable name gets ``track_info["placeholder"]`` (default "Unknown") and makes
    no person. Lane 2A calls this through ``meeting_tracks.track_speaker``.
    """
    raws = _track_names(track_info)
    usable = [(r, c) for r, c in ((r, clean_display_name(r)) for r in raws) if c]
    aliases = list(dict.fromkeys(x for r, c in usable for x in (" ".join(r.split()), c)))
    if _is_owner_track(raws, owner_names):
        # raw names too: an owner Zoom name that cleans to nothing ("Me", "iPhone")
        # still names the owner (QA BUG-08); such a name makes no person or alias
        candidates = [store.owner_name() or "", *(clean_display_name(o) for o in owner_names),
                      *(c for _, c in usable[-1:])]
        got = store.link_person(candidates, aliases, is_owner=True)
        return (got[1] if got else " ".join(raws[-1].split())), True
    if not usable:
        return str(track_info.get("placeholder") or PLACEHOLDER), False
    latest_first = usable[::-1]
    got = store.link_person([*(c for _, c in latest_first),
                             *(" ".join(r.split()) for r, _ in latest_first)], aliases)
    if got is None:
        return str(track_info.get("placeholder") or PLACEHOLDER), False
    owner = store.owner_person_id(owner_names)
    return got[1], owner is not None and got[0] == owner


def speakers_for_tracks(store: Store, tracks: Iterable[Mapping[str, Any]],
                        owner_names: Sequence[str]) -> dict[str, tuple[str, bool]]:
    """``speaker_for_track`` for every track of a meeting, keyed by track id. Tracks with
    no usable name are told apart as "Speaker 1", "Speaker 2", ... in track order, so
    ``name_speaker`` can name each one."""
    out: dict[str, tuple[str, bool]] = {}
    unnamed = 0
    for t in tracks:
        names = _track_names(t)
        if not any(clean_display_name(n) for n in names) and not _is_owner_track(
                names, owner_names):
            unnamed += 1
            out[t["track"]] = (f"Speaker {unnamed}", False)
        else:
            out[t["track"]] = speaker_for_track(store, t, owner_names)
    return out


def _is_owner_track(raws: Sequence[str], owner_names: Sequence[str]) -> bool:
    """Whether any display name of a track, raw or cleaned, is one of ``owner_names``
    (raw or cleaned, any case)."""
    keys = {k for o in owner_names for k in (clean_display_name(o).lower(),
                                             " ".join(o.split()).lower()) if k}
    return any(k in keys for r in raws for k in (clean_display_name(r).lower(),
                                                 " ".join(r.split()).lower()) if k)


def _track_names(track_info: Mapping[str, Any]) -> list[str]:
    """The track's display names in first-use order, the latest (``name``) last."""
    names = [n for n in (track_info.get("names") or []) if n and str(n).strip()]
    latest = track_info.get("name")
    if latest and str(latest).strip():
        names = [n for n in names if n != latest] + [latest]
    return [str(n) for n in names]


def _strip_symbols(s: str) -> str:
    """NFKC (fancy and full-width letters to plain), curly apostrophes to ', and emoji,
    symbols and invisible characters to spaces."""
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("‘", "'").replace("`", "'")
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        keep_mark = cat in ("Mn", "Mc") and not 0xFE00 <= ord(ch) <= 0xFE0F
        if cat[0] in "LN" or ch in _KEEP_PUNCT or keep_mark:
            out.append(ch)
        elif cat in ("Cf", "Mn", "Me"):  # zero-width joiners, variation selectors
            continue
        else:
            out.append(" ")
    return "".join(out)


def _drop_brackets(s: str) -> str:
    """Drop bracketed tags ("(she/her)", "[Acme]", "(iPad)"); a name that is only a
    bracket ("(Kath)") keeps its content."""
    for _ in range(3):  # nested brackets
        inner = _BRACKETS_RE.findall(s)
        dropped = _BRACKETS_RE.sub(" ", s)
        if not dropped.strip(_EDGE_PUNCT + "()[]{}<>"):
            return " ".join(i[1:-1] for i in inner if not looks_like_device(i[1:-1])
                            and not _PRONOUNS_RE.fullmatch(i[1:-1].strip()))
        if dropped == s:
            break
        s = dropped
    return re.sub(r"[()\[\]{}<>]", " ", s)


def _fix_case(name: str) -> str:
    """Title-case a name typed in ALL CAPS or all lowercase; leave mixed case alone."""
    # cased letters only: CJK, Arabic and Hebrew have no case ("王 DANA" -> "王 Dana")
    letters = [ch for ch in name if ch.isupper() or ch.islower()]
    if len(letters) < 2 or not (all(c.isupper() for c in letters)
                                or all(c.islower() for c in letters)):
        return name
    upper = all(c.isupper() for c in letters)
    words = name.split(" ")
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if 0 < i < len(words) - 1 and lw in _PARTICLES:
            out.append(lw)
        elif upper and len(lw.strip(".")) <= 2 and not re.search(r"[aeiouy]", lw):
            out.append(w)  # initials: "JD", "TJ"
        else:
            out.append(_cap_word(lw))
    return " ".join(out)


def _cap_word(w: str) -> str:
    """"o'neil" -> "O'Neil", "smith-jones" -> "Smith-Jones", "mcdonald" -> "McDonald"."""
    parts = re.split(r"([-'])", w)
    out = []
    for p in parts:
        if p.startswith("mc") and len(p) > 2:
            out.append("Mc" + p[2:].capitalize())
        else:
            out.append(p.capitalize())
    return "".join(out)
