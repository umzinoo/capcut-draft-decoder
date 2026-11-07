"""
Round-trip tests: build a fake MP4, scramble it exactly the way CapCut does,
then check the decoder gets the original back byte for byte.

    python3 -m unittest -v
"""

import contextlib
import io
import random
import tempfile
import unittest
from pathlib import Path

import capcut_decode as cd


def box(name: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + name + payload


def full_box(name: bytes, payload: bytes) -> bytes:  # box with a version/flags field
    return box(name, b"\0\0\0\0" + payload)


def u32(n: int) -> bytes:
    return n.to_bytes(4, "big")


def track(handler: bytes, codec: bytes, frames) -> bytes:
    """A trak box with one frame per chunk."""
    stsd = full_box(b"stsd", u32(1) + box(codec, bytes(8)))
    stsz = full_box(b"stsz", u32(0) + u32(len(frames)) + b"".join(u32(size) for _, size in frames))
    stsc = full_box(b"stsc", u32(1) + u32(1) + u32(1) + u32(1))
    stco = full_box(b"stco", u32(len(frames)) + b"".join(u32(pos) for pos, _ in frames))
    hdlr = full_box(b"hdlr", bytes(4) + handler + bytes(13))
    stbl = box(b"stbl", stsd + stsz + stsc + stco)
    return box(b"trak", box(b"mdia", hdlr + box(b"minf", stbl)))


def fake_mp4(rng: random.Random, video_frames: int, audio_per_video: int) -> bytes:
    """ftyp + mdat (interleaved video/audio frames) + moov at the end, like CapCut's files."""
    ftyp = box(b"ftyp", b"qt  " + u32(0x200) + b"qt  ")
    pos = len(ftyp) + 8
    media, video, audio = [], [], []
    for _ in range(max(video_frames, 1)):
        if video_frames:
            size = rng.randint(800, 40_000)
            media.append(u32(size - 4) + bytes([rng.choice([0x65, 0x41])]) + rng.randbytes(size - 5))
            video.append((pos, size))
            pos += size
        for _ in range(audio_per_video):
            size = rng.randint(200, 700)
            media.append(bytes([0x21]) + rng.randbytes(size - 1))
            audio.append((pos, size))
            pos += size
    moov = box(b"moov", (track(b"vide", b"avc1", video) if video else b"") +
               (track(b"soun", b"mp4a", audio) if audio else b""))
    return ftyp + box(b"mdat", b"".join(media)) + moov


def capcut_scramble(original: bytes, block_size: int, length: int, key: int) -> bytes:
    body = cd.unscramble(original, block_size, length, key)
    crpt = box(b"crpt", u32(1) + u32(3) + cd.fingerprint(block_size, length, key))
    return body + box(b"bdve", crpt + box(b"size", u32(8 + len(crpt) + 12)))


class RoundTrip(unittest.TestCase):
    def check(self, original, block_size, length, key):
        scrambled = capcut_scramble(original, block_size, length, key)
        self.assertNotEqual(scrambled[:8], original[:8])
        decoded, found_key, found_block, found_length = cd.decode_bytes(scrambled, log=lambda *_: None)
        self.assertEqual((found_key, found_block, found_length), (key, block_size, length))
        self.assertEqual(decoded, original)

    def test_video_with_audio(self):
        rng = random.Random(1)
        original = fake_mp4(rng, video_frames=300, audio_per_video=2)
        for key in (0xB4, 0x74, 0xAF, 0x01, 0xFF):
            block_size = rng.randint(150_000, 900_000)
            with self.subTest(key=hex(key), block_size=block_size):
                self.check(original, block_size, rng.randint(40_000, block_size - 40_000), key)

    def test_audio_only(self):
        rng = random.Random(2)
        original = fake_mp4(rng, video_frames=0, audio_per_video=1500)
        self.check(original, 230_988 - 1, 74_375 + 3, 0xAF)

    def test_small_plain_gap_needs_slow_search(self):
        rng = random.Random(3)
        original = fake_mp4(rng, video_frames=300, audio_per_video=2)
        self.check(original, 120_000, 117_000, 0x5A)

    def test_known_profile(self):
        rng = random.Random(4)
        original = fake_mp4(rng, video_frames=200, audio_per_video=1)
        self.check(original, 1_022_554, 311_610, 0x33)


class Files(unittest.TestCase):
    def test_plain_file_is_rejected(self):
        with self.assertRaises(cd.DecodeError):
            cd.decode_bytes(fake_mp4(random.Random(5), 10, 1))

    def test_folder_scan_writes_copies_and_leaves_originals(self):
        rng = random.Random(6)
        original = fake_mp4(rng, video_frames=100, audio_per_video=1)
        scrambled = capcut_scramble(original, 400_000, 250_000, 0xB4)
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "com.lveditor.draft" / "my video" / "Resources" / "combination"
            project.mkdir(parents=True)
            (project / "clip_video.mp4").write_bytes(scrambled)
            (project / "normal.mp4").write_bytes(original)  # not scrambled: should be skipped
            out = Path(tmp) / "out"

            with contextlib.redirect_stdout(io.StringIO()):  # keep the test output tidy
                exit_code = cd.main([str(Path(tmp) / "com.lveditor.draft"), "-o", str(out)])
            self.assertEqual(exit_code, 0)

            self.assertEqual((out / "my video - clip_video.mp4").read_bytes(), original)
            self.assertEqual(len(list(out.iterdir())), 1)
            self.assertEqual((project / "clip_video.mp4").read_bytes(), scrambled)


if __name__ == "__main__":
    unittest.main()
