"""What a user means by "a channel": a UC id, an @handle, or a channel URL.

Pure string work, no quota and no database: whether the handle actually exists
is for the caller to find out (application/collecting.resolve_channel_id).
"""
import re

CHANNEL_ID_RE = re.compile(r"(UC[\w-]{22})")
HANDLE_RE = re.compile(r"@([\w.\-]+)")


def parse_channel_ref(ref):
    """-> ("id", "UC...") | ("handle", name without "@") | None.

    A UC id wins wherever it appears, so /channel/UC... URLs need no lookup.
    A bare word that isn't a URL is taken as a handle typed without the "@".
    """
    ref = (ref or "").strip()
    m = CHANNEL_ID_RE.search(ref)
    if m:
        return "id", m.group(1)
    m = HANDLE_RE.search(ref)
    if m:
        return "handle", m.group(1)
    if ref and not ref.startswith("http"):
        return "handle", ref
    return None


def is_channel_id(value) -> bool:
    return bool(value) and CHANNEL_ID_RE.fullmatch(value) is not None
