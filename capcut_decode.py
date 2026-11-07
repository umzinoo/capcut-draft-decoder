#!/usr/bin/env python3
"""
CapCut Draft Decoder
====================
Turns CapCut's scrambled draft media back into normal, playable MP4 / M4A files.

CapCut keeps some media inside its project folders in a scrambled form (usually
in  <project>/Resources/combination/ ). Video players refuse to open them.
This script unscrambles them. Your original files are never changed.

    python3 capcut_decode.py                       scan CapCut's projects folder
    python3 capcut_decode.py <file or folder>      decode one file, or all in a folder
    python3 capcut_decode.py <...> -o <folder>     choose where the results go

Needs Python 3.8+ and nothing else.


How CapCut scrambles a file
---------------------------
  * It picks three numbers: a key (one byte), a block size and a length.
  * The file is cut into blocks of `block size` bytes. In every block, the first
    `length` bytes are XOR-ed with the key. The rest of the block is untouched:

        |XXXXXXXXXXXX.......|XXXXXXXXXXXX.......|XXXXXXXXXXXX.......|
         <- length ->
         <--- block size -->

  * It appends a small footer (a 'bdve' box). Inside it, a 'crpt' box stores
    SHA-256(block size + length + key): a fingerprint, not the numbers themselves.

How we undo it
--------------
  1. The key: a real MP4 always starts with the byte 0x00, so the first byte of
     the scrambled file IS the key.
  2. Block size and length: we look inside the file (box headers and the first
     bytes of every video/audio frame) and note which spots are clearly scrambled
     and which are clearly plain. That narrows the numbers down to a small range.
  3. We try each candidate against the fingerprint in the footer. A match means
     the numbers are exactly right, so the result is byte-perfect.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

# (block size, length) pairs seen in real files. Checked first because it's instant.
KNOWN_PROFILES = [
    (1_022_554, 311_610),
    (102_303, 89_230),
    (690_910, 449_204),
    (493_151, 305_707),
    (230_988, 74_375),
]
MAX_BLOCK_SIZE = 5_000_000
MAX_NOTES = 5_000  # plenty to pin the numbers down; more just makes the search slower
MEDIA_EXTENSIONS = {".mp4", ".m4a", ".m4v", ".mov"}

# MP4 box names we expect to see. Used to tell whether a box header is scrambled.
KNOWN_BOXES = {
    b"ftyp", b"wide", b"free", b"skip", b"mdat", b"moov", b"mvhd", b"trak", b"tkhd",
    b"tref", b"edts", b"elst", b"mdia", b"mdhd", b"hdlr", b"minf", b"vmhd", b"smhd",
    b"nmhd", b"gmhd", b"dinf", b"dref", b"stbl", b"stsd", b"stts", b"stss", b"ctts",
    b"cslg", b"stsc", b"stsz", b"stz2", b"stco", b"co64", b"sdtp", b"sgpd", b"sbgp",
    b"udta", b"meta", b"iods", b"mvex", b"uuid", b"pdin", b"load", b"tapt",
}
CONTAINER_BOXES = {b"moov", b"trak", b"tref", b"edts", b"mdia", b"minf", b"dinf", b"stbl", b"mvex"}

VIDEO_CODECS = {b"avc1", b"avc3", b"hvc1", b"hev1"}  # frames start with a 4-byte length
AAC_FIRST_BYTES = {0x00, 0x01, 0x20, 0x21}            # first byte of a mono / stereo AAC frame


class DecodeError(Exception):
    """The file looks scrambled, but we couldn't unscramble it."""


# ---------------------------------------------------------------------------
# The scrambling itself
# ---------------------------------------------------------------------------

def xor_table(key: int) -> bytes:
    """Lookup table for bytes.translate(): turns every byte b into b XOR key."""
    return bytes(b ^ key for b in range(256))


def fingerprint(block_size: int, length: int, key: int) -> bytes:
    """What CapCut stores in the footer: SHA-256 of the three numbers."""
    data = block_size.to_bytes(4, "big") + length.to_bytes(4, "big") + bytes([key])
    return hashlib.sha256(data).digest()


def unscramble(body: bytes, block_size: int, length: int, key: int) -> bytes:
    """XOR the first `length` bytes of every block. (Doing it twice undoes it.)"""
    table = xor_table(key)
    out = bytearray(body)
    for start in range(0, len(out), block_size):
        end = min(start + length, len(out))
        out[start:end] = out[start:end].translate(table)
    return bytes(out)


# ---------------------------------------------------------------------------
# The footer CapCut appends
# ---------------------------------------------------------------------------

def u32(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos:pos + 4], "big")


def read_footer(data: bytes) -> tuple[int, bytes] | None:
    """Return (where the footer starts, the SHA-256 fingerprint), or None if there is no footer."""
    if len(data) < 64:
        return None
    footer_size = u32(data, len(data) - 4)  # the footer ends with its own size
    start = len(data) - footer_size
    if footer_size < 56 or start < 0 or data[start + 4:start + 8] != b"bdve":
        return None

    pos = start + 8
    while pos + 8 <= len(data):
        size, kind = u32(data, pos), data[pos + 4:pos + 8]
        if size < 8:
            break
        if kind == b"crpt" and size >= 48:
            method = u32(data, pos + 8)
            if method != 1:
                raise DecodeError(f"unknown scrambling method {method} (only method 1 is supported)")
            return start, data[pos + 16:pos + 48]
        pos += size
    return None


def has_footer(path: Path) -> bool:
    """Quick check without reading the whole file: does it end with a CapCut footer?"""
    try:
        with open(path, "rb") as f:
            file_size = f.seek(0, os.SEEK_END)
            if file_size < 64:
                return False
            f.seek(-4, os.SEEK_END)
            footer_size = int.from_bytes(f.read(4), "big")
            if not 56 <= footer_size <= file_size:
                return False
            f.seek(-footer_size, os.SEEK_END)
            return f.read(8)[4:] == b"bdve"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Reading the MP4 structure to find "clearly scrambled" / "clearly plain" spots
# ---------------------------------------------------------------------------

def readable_copy(body: bytes, table: bytes, notes: list) -> bytes:
    """
    Copy of the file where every box header (and every small box) is unscrambled,
    whichever state it was in. Lets us read the frame tables even when some boxes
    are scrambled and others aren't. Records each header's state in `notes`.
    """
    copy = bytearray(body)

    def walk(start: int, end: int) -> None:
        pos = start
        while pos + 8 <= end:
            raw = body[pos:pos + 16]
            xored = raw.translate(table)
            if xored[4:8] in KNOWN_BOXES:
                scrambled, header = True, xored
            elif raw[4:8] in KNOWN_BOXES:
                scrambled, header = False, raw
            else:
                return  # unrecognised box: stop walking this level
            notes.append((pos, scrambled))

            size, kind, header_len = u32(header, 0), header[4:8], 8
            if size == 1:  # 64-bit size follows
                size, header_len = int.from_bytes(header[8:16], "big"), 16
            elif size == 0:  # box runs to the end
                size = end - pos
            if size < header_len or pos + size > end:
                return

            copy[pos:pos + header_len] = header[:header_len]
            if kind in CONTAINER_BOXES:
                walk(pos + header_len, pos + size)
            elif kind != b"mdat":  # the media itself is left alone
                box = body[pos:pos + size]
                copy[pos:pos + size] = box.translate(table) if scrambled else box
            pos += size

    walk(0, len(body))
    return bytes(copy)


def iter_boxes(data: bytes, start: int, end: int):
    """Yield (name, content start, end) for each box between start and end."""
    pos = start
    while pos + 8 <= end:
        size, kind, header_len = u32(data, pos), data[pos + 4:pos + 8], 8
        if size == 1:
            size, header_len = int.from_bytes(data[pos + 8:pos + 16], "big"), 16
        elif size == 0:
            size = end - pos
        if size < header_len or pos + size > end:
            return
        yield kind, pos + header_len, pos + size
        pos += size


def child(data: bytes, start: int, end: int, name: bytes) -> tuple[int, int] | None:
    for kind, content, box_end in iter_boxes(data, start, end):
        if kind == name:
            return content, box_end
    return None


def read_entries(data: bytes, pos: int, count: int, width: int, key: int, limit: int) -> list[int]:
    """
    Read `count` numbers from a table. A big table can cross a scrambled/plain edge,
    so the second half may still be in the other state. A number in the wrong state
    comes out huge, so if one is impossibly large we use its flipped version instead.
    """
    flip = int.from_bytes(bytes([key]) * width, "big")
    values = []
    for i in range(count):
        value = int.from_bytes(data[pos + width * i:pos + width * (i + 1)], "big")
        if value > limit and (value ^ flip) <= limit:
            value ^= flip
        values.append(value)
    return values


def frame_positions(data: bytes, stbl: tuple[int, int], key: int) -> list[tuple[int, int]]:
    """Read a track's sample tables and return (position, size) of every frame."""
    stsz = child(data, *stbl, b"stsz")
    stsc = child(data, *stbl, b"stsc")
    stco = child(data, *stbl, b"stco") or child(data, *stbl, b"co64")
    if not (stsz and stsc and stco):
        return []
    file_size = len(data)

    # Frame sizes
    pos, end = stsz
    fixed_size, count = u32(data, pos + 4), u32(data, pos + 8)
    if not fixed_size and pos + 12 + 4 * count > end:
        raise ValueError("bad stsz")
    sizes = [fixed_size] * count if fixed_size else read_entries(data, pos + 12, count, 4, key, file_size)

    # Where each chunk of frames starts
    pos, end = stco
    width = 8 if data[pos - 4:pos] == b"co64" else 4
    count = u32(data, pos + 4)
    if pos + 8 + width * count > end:
        raise ValueError("bad stco")
    chunk_starts = read_entries(data, pos + 8, count, width, key, file_size)

    # How many frames are in each chunk: runs of (first chunk number, frames per chunk)
    pos, end = stsc
    count = u32(data, pos + 4)
    if pos + 8 + 12 * count > end:
        raise ValueError("bad stsc")
    runs = [(u32(data, pos + 8 + 12 * i), u32(data, pos + 12 + 12 * i)) for i in range(count)]

    frames, frame, run = [], 0, 0
    for chunk_number, offset in enumerate(chunk_starts, start=1):
        while run + 1 < len(runs) and runs[run + 1][0] <= chunk_number:
            run += 1
        for _ in range(runs[run][1] if runs else 0):
            if frame >= len(sizes):
                return frames
            frames.append((offset, sizes[frame]))
            offset += sizes[frame]
            frame += 1
    return frames


def tracks(data: bytes, key: int):
    """Yield (handler, codec, frames) for every track: handler is b'vide' or b'soun'."""
    moov = child(data, 0, len(data), b"moov")
    if not moov:
        return
    for kind, start, end in iter_boxes(data, *moov):
        if kind != b"trak":
            continue
        mdia = child(data, start, end, b"mdia")
        hdlr = mdia and child(data, *mdia, b"hdlr")
        minf = mdia and child(data, *mdia, b"minf")
        stbl = minf and child(data, *minf, b"stbl")
        stsd = stbl and child(data, *stbl, b"stsd")
        if not (hdlr and stsd):
            continue
        handler = data[hdlr[0] + 8:hdlr[0] + 12]
        codec = data[stsd[0] + 12:stsd[0] + 16]
        try:
            yield handler, codec, frame_positions(data, stbl, key)
        except ValueError:
            continue  # this table was damaged by the scrambling; the other tracks may still work


def frame_checker(handler: bytes, codec: bytes):
    """A function telling whether a frame's first bytes look right, or None for unknown codecs."""
    if handler == b"vide" and codec in VIDEO_CODECS:
        # a video frame starts with the length of its first piece
        return lambda head, size: 0 < int.from_bytes(head[:4], "big") <= size - 4
    if handler == b"soun" and codec == b"mp4a":
        return lambda head, size: head[0] in AAC_FIRST_BYTES
    return None


def frame_notes(body: bytes, table: bytes, frames: list) -> list:
    """
    For each frame (position, size, checker), note whether its first bytes look right
    scrambled or right as-is. Real frames sit end to end without overlapping, so a
    frame that overlaps another came from a misread table entry and is skipped.
    """
    frames = sorted(frames, key=lambda frame: frame[0])
    notes = []
    for i, (pos, size, looks_right) in enumerate(frames):
        overlaps_previous = i > 0 and frames[i - 1][0] + frames[i - 1][1] > pos
        overlaps_next = i + 1 < len(frames) and pos + size > frames[i + 1][0]
        if overlaps_previous or overlaps_next or size < 5 or pos + size > len(body):
            continue
        head = body[pos:pos + 4]
        as_is, xored = looks_right(head, size), looks_right(head.translate(table), size)
        if as_is != xored:  # only keep the unambiguous ones
            notes.append((pos, xored))
    return notes


# ---------------------------------------------------------------------------
# Finding the block size and length
# ---------------------------------------------------------------------------

def search(notes, key: int, target: bytes, block_sizes) -> tuple[int, int] | None:
    """Try each block size; keep only lengths that agree with every note; check fingerprints."""
    key_byte = bytes([key])
    for block_size in block_sizes:
        lowest, highest = 1, block_size  # possible lengths
        for pos, scrambled in notes:
            offset = pos % block_size  # where this spot falls inside its block
            if scrambled:
                lowest = max(lowest, offset + 1)  # the scrambled part reaches past it
            else:
                highest = min(highest, offset)   # the scrambled part stops before it
            if lowest > highest:
                break
        else:  # no note ruled this block size out
            prefix = block_size.to_bytes(4, "big")
            for length in range(lowest, highest + 1):
                if hashlib.sha256(prefix + length.to_bytes(4, "big") + key_byte).digest() == target:
                    return block_size, length
    return None


def find_numbers(notes, key: int, target: bytes, log=print) -> tuple[int, int]:
    for block_size, length in KNOWN_PROFILES:
        if fingerprint(block_size, length, key) == target:
            return block_size, length

    notes = sorted(set(notes))[:MAX_NOTES]
    plain = [pos for pos, scrambled in notes if not scrambled]
    if not plain:
        raise DecodeError("couldn't find any unscrambled part of the file")

    # Fast: block 0's plain part holds the first plain spot, and block 1 starts
    # at or before the next scrambled spot after it.
    first_plain = plain[0]
    later = [pos for pos, scrambled in notes if scrambled and pos > first_plain]
    if later:
        found = search(notes, key, target, range(first_plain + 1, later[0] + 1))
        if found:
            return found

    # Slow: try every block size.
    log("    trying every block size, this can take a few minutes...")
    found = search(notes, key, target, range(1, MAX_BLOCK_SIZE + 1))
    if found:
        return found
    raise DecodeError("couldn't work out how this file was scrambled")


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def decode_bytes(data: bytes, log=print) -> tuple[bytes, int, int, int]:
    """Unscramble one file's bytes. Returns (result, key, block size, length)."""
    footer = read_footer(data)
    if footer is None:
        raise DecodeError("not a scrambled CapCut file (no CapCut footer)")
    footer_start, target = footer
    body = data[:footer_start]
    key = body[0]
    table = xor_table(key)

    notes = []
    readable = readable_copy(body, table, notes)
    frames = []
    for handler, codec, track_frames in tracks(readable, key):
        looks_right = frame_checker(handler, codec)
        if looks_right:
            frames += [(pos, size, looks_right) for pos, size in track_frames]
    notes += frame_notes(body, table, frames)

    block_size, length = find_numbers(notes, key, target, log)
    return unscramble(body, block_size, length, key), key, block_size, length


def draft_name(path: Path) -> str | None:
    """The CapCut project a file belongs to (the folder right under com.lveditor.draft)."""
    for parent in path.parents:
        if parent.parent.name == "com.lveditor.draft":
            return parent.name
    return None


def default_capcut_folders() -> list[Path]:
    candidates = [Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"]  # macOS
    if os.environ.get("LOCALAPPDATA"):  # Windows
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "CapCut/User Data/Projects/com.lveditor.draft")
    return [folder for folder in candidates if folder.is_dir()]


def default_output_folder() -> Path:
    desktop = Path.home() / "Desktop"
    return (desktop if desktop.is_dir() else Path.home()) / "CapCut Decoded"


def find_scrambled_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(
        path for path in target.rglob("*")
        if path.suffix.lower() in MEDIA_EXTENSIONS and path.is_file() and has_footer(path)
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Make CapCut's scrambled draft videos playable again. Originals are never changed.")
    parser.add_argument("input", nargs="?", type=Path,
                        help="a scrambled file, or a folder to search (default: CapCut's projects folder)")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="folder for the decoded files (default: 'CapCut Decoded' on your Desktop)")
    args = parser.parse_args(argv)

    if args.input:
        if not args.input.exists():
            print(f"Can't find: {args.input}")
            return 1
        targets = [args.input]
    else:
        targets = default_capcut_folders()
        if not targets:
            print("Couldn't find CapCut's projects folder. Pass a file or folder instead, e.g.:")
            print('    python3 capcut_decode.py "path/to/your/file.mp4"')
            return 1

    output = args.output or default_output_folder()
    files = [f for target in targets for f in find_scrambled_files(target)]
    if not files:
        print("No scrambled CapCut files found in: " + ", ".join(str(t) for t in targets))
        return 0

    print(f"Found {len(files)} scrambled file(s). Saving playable copies to: {output}\n")
    failed = 0
    for src in files:
        project = draft_name(src)
        dst = output / (f"{project} - {src.name}" if project else src.name)
        print(f"  {src.name}")
        try:
            result, key, block_size, length = decode_bytes(src.read_bytes())
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(result)
            print(f"    OK  key 0x{key:02X}, block {block_size:,}, length {length:,}  ->  {dst.name}")
        except (DecodeError, OSError) as error:
            failed += 1
            print(f"    FAILED: {error}")

    print(f"\nDone: {len(files) - failed} decoded, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    exit_code = main()
    if len(sys.argv) == 1 and sys.platform == "win32":
        input("\nPress Enter to close...")  # keep the window open when double-clicked on Windows
    sys.exit(exit_code)
