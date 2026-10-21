"""Structure-preserving UTF-8 byte truncation helpers (head and tail)."""

from __future__ import annotations

MAX_WINDOW_BYTES = 4096


def _open_fence_marker(line: str) -> str | None:
    stripped = line.lstrip(" \t")
    if stripped.startswith("```"):
        ticks = len(stripped) - len(stripped.lstrip("`"))
        rest = stripped[ticks:].rstrip("\r\n")
        if "`" in rest:
            return None
        return "```"
    if stripped.startswith("~~~"):
        tildes = len(stripped) - len(stripped.lstrip("~"))
        rest = stripped[tildes:].rstrip("\r\n")
        if rest and rest.endswith("~~~"):
            return None
        return "~~~"
    return None


def _is_closing_fence(line: str, marker: str) -> bool:
    stripped = line.lstrip(" \t")
    if not stripped.startswith(marker):
        return False
    rest = stripped.lstrip(marker[0])
    return rest.strip(" \t\r\n") == ""


def _scan_lines(text: str, initial_fence: str | None = None, first_line_partial: bool = False):
    """Return per-line metadata and final fence state for text."""
    lines = text.splitlines(keepends=True)
    records = []
    char_pos = 0
    byte_pos = 0
    in_fence = initial_fence
    open_info = (0, 0, 0, initial_fence) if initial_fence is not None else None

    for idx, line in enumerate(lines):
        line_bytes = len(line.encode("utf-8"))
        end_char = char_pos + len(line)
        end_byte = byte_pos + line_bytes
        is_partial_first = idx == 0 and first_line_partial

        fence_before = in_fence
        marker = None
        opened = False
        closed = False
        close_ticks_byte = byte_pos

        if not is_partial_first:
            if fence_before is None:
                open_marker = _open_fence_marker(line)
                if open_marker is not None:
                    in_fence = open_marker
                    marker = open_marker
                    opened = True
                    open_info = (char_pos, byte_pos, end_byte, open_marker)
            elif _is_closing_fence(line, fence_before):
                marker = fence_before
                in_fence = None
                closed = True
                open_info = None
                lead_len = len(line) - len(line.lstrip(" \t"))
                close_ticks_byte = byte_pos + len(line[:lead_len].encode("utf-8"))

        is_heading = (
            not is_partial_first
            and fence_before is None
            and line.lstrip(" \t").startswith("#")
        )
        is_blank = (
            not is_partial_first
            and fence_before is None
            and line.strip() == ""
        )
        has_newline = line.endswith(("\n", "\r"))

        records.append(
            {
                "start_char": char_pos,
                "end_char": end_char,
                "start_byte": byte_pos,
                "end_byte": end_byte,
                "fence_before": fence_before,
                "fence_after": in_fence,
                "opened": opened,
                "closed": closed,
                "marker": marker,
                "close_ticks_byte": close_ticks_byte,
                "is_heading": is_heading,
                "is_blank": is_blank,
                "has_newline": has_newline,
            }
        )
        char_pos = end_char
        byte_pos = end_byte

    return records, in_fence, open_info


def head(text: str, max_bytes: int) -> str:
    """Return at most max_bytes UTF-8 bytes from the start of text."""
    if max_bytes <= 0 or not text:
        return ""
    raw_all = text.encode("utf-8")
    if len(raw_all) <= max_bytes:
        return text

    raw = raw_all[:max_bytes].decode("utf-8", errors="ignore")
    if not raw:
        return ""
    raw_bytes_len = len(raw.encode("utf-8"))
    window_bytes = min(MAX_WINDOW_BYTES, raw_bytes_len // 4)
    min_byte = raw_bytes_len - window_bytes

    records, _, _ = _scan_lines(raw)

    headings: list[int] = []
    blanks: list[int] = []
    line_ends: list[int] = []

    for rec in records:
        sb = rec["start_byte"]
        eb = rec["end_byte"]
        if rec["is_heading"] and 0 < sb and min_byte <= sb <= raw_bytes_len:
            headings.append(rec["start_char"])
        if rec["is_blank"]:
            if 0 < sb and min_byte <= sb <= raw_bytes_len:
                blanks.append(rec["start_char"])
            elif 0 < eb and min_byte <= eb <= raw_bytes_len:
                blanks.append(rec["end_char"])
        if rec["has_newline"] and 0 < eb and min_byte <= eb <= raw_bytes_len:
            line_ends.append(rec["end_char"])

    if headings:
        cut_char = headings[-1]
    elif blanks:
        cut_char = blanks[-1]
    elif line_ends:
        cut_char = line_ends[-1]
    else:
        cut_char = len(raw)

    piece = raw[:cut_char]
    _, in_fence, open_info = _scan_lines(piece)

    if in_fence is None or open_info is None:
        return piece

    open_char, open_byte, open_line_end_byte, marker = open_info
    if open_byte > 0 and open_byte >= min_byte:
        return raw[:open_char]

    suffix = f"\n{marker}"
    suffix_bytes = len(suffix.encode("utf-8"))
    budget = max_bytes - suffix_bytes

    if budget < open_byte + 3 or (open_char > 0 and budget < open_line_end_byte):
        return raw[:open_char]

    if len(piece.encode("utf-8")) > budget:
        valid_ends = [
            rec["end_char"]
            for rec in records
            if rec["has_newline"]
            and max(min_byte, open_line_end_byte) <= rec["end_byte"] <= budget
        ]
        if valid_ends:
            piece = raw[: valid_ends[-1]]
        else:
            piece = piece.encode("utf-8")[:budget].decode("utf-8", errors="ignore")

    if len(piece.encode("utf-8")) < open_byte + 3:
        return raw[:open_char]

    return piece + suffix


def _tail_initial_state(text: str, raw: str) -> tuple[str | None, bool]:
    prefix_len = len(text) - len(raw)
    if prefix_len <= 0:
        return None, False

    prefix = text[:prefix_len]
    if prefix.endswith(("\n", "\r")):
        _, in_fence, _ = _scan_lines(prefix)
        return in_fence, False

    last_nl = max(prefix.rfind("\n"), prefix.rfind("\r"))
    before_line = prefix[: last_nl + 1] if last_nl != -1 else ""
    _, fence_before_split_line, _ = _scan_lines(before_line)

    raw_first_line = raw.splitlines(keepends=True)[0] if raw else ""
    full_split_line = prefix[last_nl + 1 :] + raw_first_line

    if fence_before_split_line is None:
        full_open = _open_fence_marker(full_split_line)
        if full_open is None:
            return None, True
        if _open_fence_marker(raw_first_line) == full_open:
            return None, False
        return full_open, True

    if _is_closing_fence(full_split_line, fence_before_split_line):
        if _is_closing_fence(raw_first_line, fence_before_split_line):
            return fence_before_split_line, False
        return None, True

    return fence_before_split_line, True


def tail(text: str, max_bytes: int) -> str:
    """Return at most max_bytes UTF-8 bytes from the end of text."""
    if max_bytes <= 0 or not text:
        return ""
    raw_all = text.encode("utf-8")
    if len(raw_all) <= max_bytes:
        return text

    raw = raw_all[-max_bytes:].decode("utf-8", errors="ignore")
    if not raw:
        return ""
    raw_bytes_len = len(raw.encode("utf-8"))
    window_bytes = min(MAX_WINDOW_BYTES, raw_bytes_len // 4)

    initial_fence, first_line_partial = _tail_initial_state(text, raw)
    records, _, _ = _scan_lines(raw, initial_fence=initial_fence, first_line_partial=first_line_partial)

    headings: list[int] = []
    blanks: list[int] = []
    line_ends: list[int] = []

    for rec in records:
        sb = rec["start_byte"]
        eb = rec["end_byte"]
        if rec["is_heading"] and sb <= window_bytes and sb < raw_bytes_len:
            headings.append(rec["start_char"])
        if rec["is_blank"]:
            if eb <= window_bytes and eb < raw_bytes_len:
                blanks.append(rec["end_char"])
            elif sb <= window_bytes and sb < raw_bytes_len:
                blanks.append(rec["start_char"])
        if rec["has_newline"] and 0 < eb <= window_bytes and eb < raw_bytes_len:
            line_ends.append(rec["end_char"])

    if headings:
        start_char = headings[0]
    elif blanks:
        start_char = blanks[0]
    elif line_ends:
        start_char = line_ends[0]
    else:
        start_char = 0

    fence_at_start = initial_fence
    start_rec_idx = 0
    for idx, rec in enumerate(records):
        if rec["start_char"] == start_char:
            fence_at_start = rec["fence_before"]
            start_rec_idx = idx
            break
        if rec["end_char"] == start_char:
            fence_at_start = rec["fence_after"]
            start_rec_idx = idx + 1
            break

    piece = raw[start_char:]
    if fence_at_start is None:
        return piece

    close_rec = None
    for idx in range(start_rec_idx, len(records)):
        rec = records[idx]
        if rec["closed"] and rec["fence_before"] == fence_at_start:
            close_rec = rec
            break

    prefix_fence = f"{fence_at_start}\n"
    prefix_bytes = len(prefix_fence.encode("utf-8"))
    budget = max_bytes - prefix_bytes

    if close_rec is None:
        if budget <= 0:
            return ""
        if len(piece.encode("utf-8")) > budget:
            min_start_byte = raw_bytes_len - budget
            valid_starts = [
                rec["end_char"]
                for rec in records
                if rec["has_newline"] and min_start_byte <= rec["end_byte"] <= window_bytes
            ]
            if valid_starts:
                piece = raw[valid_starts[0] :]
            else:
                piece = piece.encode("utf-8")[-budget:].decode("utf-8", errors="ignore")
        return prefix_fence + piece

    close_end_char = close_rec["end_char"]
    close_end_byte = close_rec["end_byte"]
    close_ticks_byte = close_rec["close_ticks_byte"]

    if close_end_byte <= window_bytes and close_end_byte < raw_bytes_len:
        return raw[close_end_char:]

    if budget < 3:
        return raw[close_end_char:] if close_end_char < len(raw) else ""

    if len(piece.encode("utf-8")) > budget:
        min_start_byte = raw_bytes_len - budget
        valid_starts = [
            rec["end_char"]
            for rec in records
            if rec["has_newline"]
            and min_start_byte <= rec["end_byte"] <= min(window_bytes, close_ticks_byte)
        ]
        if valid_starts:
            piece = raw[valid_starts[0] :]
        else:
            piece = piece.encode("utf-8")[-budget:].decode("utf-8", errors="ignore")

    new_start_byte = raw_bytes_len - len(piece.encode("utf-8"))
    if new_start_byte >= close_ticks_byte:
        return raw[close_end_char:] if close_end_char < len(raw) else ""

    return prefix_fence + piece
