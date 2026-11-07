# CapCut Draft Decoder

**Make CapCut's scrambled draft videos playable again.**

Found an `.mp4` or `.m4a` inside a CapCut project folder that QuickTime, VLC or Windows Media Player won't open? CapCut stores some draft media in a scrambled form. This tool turns them back into normal files.

- **One Python file.** Nothing to install besides Python.
- **Your originals are never changed.** It writes new copies.
- **Byte-perfect.** CapCut hides a fingerprint inside each file, and every result is checked against it.
- **Works on macOS and Windows.**

---

## Quick start

**1. Get Python 3** (version 3.8 or newer). Check if you already have it by opening Terminal (macOS) or Command Prompt (Windows) and typing:

```
python3 --version
```

If that doesn't work, download it from [python.org](https://www.python.org/downloads/).

**2. Download** [`capcut_decode.py`](capcut_decode.py) (click it, then the download button), or clone this repo.

**3. Run it:**

```
python3 capcut_decode.py
```

That's it. It finds CapCut's projects folder by itself and puts playable copies in a folder called **CapCut Decoded** on your Desktop.

> **Windows:** type `python` or `py` instead of `python3`. You can also just double-click `capcut_decode.py`.

---

## More ways to use it

```
python3 capcut_decode.py "path/to/file.mp4"         # decode one file
python3 capcut_decode.py "path/to/a/folder"         # decode every scrambled file inside a folder
python3 capcut_decode.py "path/..." -o "save/here"  # choose where the results go
```

**Tip:** instead of typing a path, drag the file or folder from Finder / Explorer into the Terminal window.

Example output:

```
Found 3 scrambled file(s). Saving playable copies to: /Users/you/Desktop/CapCut Decoded

  FFA165A6-793B-47C2-8496-3848D1323A2E_video.mp4
    OK  key 0xB4, block 690,910, length 449,204  ->  bread - FFA165A6-793B-47C2-8496-3848D1323A2E_video.mp4
  ...

Done: 3 decoded, 0 failed.
```

Each copy is named after its CapCut project (`bread` above), then the original file name.

## Where CapCut keeps these files

| System  | Projects folder |
|---------|-----------------|
| macOS   | `~/Movies/CapCut/User Data/Projects/com.lveditor.draft/` |
| Windows | `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\` |

Inside each project, scrambled media usually sits in `Resources/combination/`. Files that aren't scrambled (like the `.alpha.mp4` ones) are skipped automatically.

---

## How it works

CapCut picks three numbers for every file: a **key** (one byte), a **block size**, and a **length**. It cuts the file into blocks, and in each block it scrambles the first `length` bytes by XOR-ing them with the key. The rest of the block is left alone:

```
|XXXXXXXXXXXX.......|XXXXXXXXXXXX.......|XXXXXXXXXXXX.......|
 <- length ->
 <--- block size -->
                         X = scrambled    . = untouched
```

Then it adds a small footer to the end of the file (a `bdve` box). The footer doesn't contain the numbers. It contains a **SHA-256 fingerprint** of them, so you can check a guess but can't read the answer.

To undo it, the decoder:

1. **Finds the key.** Every MP4 starts with the byte `0x00`, so the first byte of a scrambled file *is* the key.
2. **Narrows down the block size and length.** It reads the file's structure and looks at the start of every video and audio frame, noting which spots are clearly scrambled and which are clearly plain. Only a small range of numbers fits all of those clues.
3. **Checks each candidate against the fingerprint.** When one matches, the numbers are exactly right, and unscrambling is a single pass over the file.

Most files take a second or two. Unusual ones fall back to trying every block size, which can take a few minutes.

The code is commented section by section if you want to follow along: [`capcut_decode.py`](capcut_decode.py).

## Running the tests

```
python3 -m unittest -v
```

The tests build fake MP4 files, scramble them exactly the way CapCut does, and check the decoder gets the originals back byte for byte. No real CapCut files are needed.

## Troubleshooting

| Message | What it means |
|---------|---------------|
| `not a scrambled CapCut file` | The file is already normal. Try opening it directly. |
| `couldn't find any unscrambled part of the file` / `couldn't work out how this file was scrambled` | This file is scrambled in a way the decoder can't solve yet. Please [open an issue](../../issues) with the printed message (no need to upload your video). |
| `Couldn't find CapCut's projects folder` | Pass the folder or file yourself: `python3 capcut_decode.py "path/to/folder"`. |

You can always open the project in CapCut and use **Export** to get a normal video.

## Credits

The idea of checking guesses against the footer's SHA-256 fingerprint comes from [CC-Decrypter](https://github.com/xorica27/CC-Decrypter) by xorica27. This project is a separate, smaller implementation with no dependencies. It also finds the numbers faster, and it handles files whose parts are a mix of scrambled and plain (common in audio files).

## Disclaimer

This project isn't affiliated with or endorsed by CapCut or ByteDance. It's meant for recovering media from your own projects.

## License

[MIT](LICENSE)
