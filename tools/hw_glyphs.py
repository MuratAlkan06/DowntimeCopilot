"""NIST Special Database 19 (2nd edition) as the glyph source for tools/hw_fixtures.py.

SD19 ships the same segmented characters in several organizations. by_write.zip groups them by writer but
its field-type directories (digit, upper, lower, constitution) carry no per-glyph class; by_class.zip carries
the class but discards the writer. The index joins the two on a hash of the DECODED pixels (not the PNG file
bytes): class from by_class, writer from by_write. A pixel hash seen under more than one class, or under more
than one writer, is dropped (conservative). The zips are read in place; the 1.6M members are never extracted.

  python -m tools.hw_glyphs fetch  [--cache DIR] [--write-lock]   # NETWORK: download, verify MD5 logs + lock
  python -m tools.hw_glyphs verify [--cache DIR]                    # re-verify the cached archives
  python -m tools.hw_glyphs index  [--cache DIR]                    # build DIR/sd19_index.json from them

Only `fetch` touches the network, and only when run explicitly: never on import, in tests or in CI. The
archives, MD5 logs and index live in the cache dir (default .cache/handwriting/, git-ignored). Until
tools/handwriting_sources.lock.json records each archive's sha256 and size (`fetch --write-lock` at the
first verified fetch), the source refuses to load: unverified bytes never reach a fixture.

The layout patterns below are assumptions about the real archives (cross-checked against the published MD5
logs' member paths); the indexer refuses loudly when nothing matches them. Licensing, provenance and privacy:
docs/handwriting-datasets.md.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import re
import shutil
import string
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Protocol, runtime_checkable

ROOT = Path(__file__).resolve().parent.parent
LOCKFILE = ROOT / "tools" / "handwriting_sources.lock.json"
DEFAULT_CACHE = ROOT / ".cache" / "handwriting"
INDEX_NAME = "sd19_index.json"
INDEX_VERSION = 1
ARCHIVES = ("by_write", "by_class")

# ---- layout assumptions (S3 checks them against the real archives) -------------------------------------
# Any leading prefix is allowed: the MD5 logs list members as sd19/2ndEdition2016/data/by_write/...
# by_write/hsf_<p>/f<wwww>_<tt>/<d|u|l|c><wwww>_<tt>/<d|u|l|c><wwww>_<tt>_<nnnnn>.png ; writer = f<wwww>_<tt>
BY_WRITE_PATTERN = re.compile(
    r"(?:^|/)by_write/(?P<partition>hsf_\d+)/(?P<writer>f(?P<wid>\d{4}_\d{2}))/(?P<field>[dulc])(?P=wid)/"
    r"[^/]+\.png$")
# by_class/<hex ASCII code>/(hsf_<p>|train_<hex>)/<name>.png ; class = chr(int(hex, 16)), one of 62
BY_CLASS_PATTERN = re.compile(r"(?:^|/)by_class/(?P<hex>[0-9a-f]{2})/(?P<group>hsf_\d+|train_[0-9a-f]{2})/[^/]+\.png$")
# NIST MD5 logs: one "<md5> <path>" line per archive member (md5sum style; a leading "*" is tolerated)
MD5_LOG_LINE = re.compile(r"^(?P<md5>[0-9a-fA-F]{32}) [ *]?(?P<path>\S.*?)\s*$")
GLYPH_CLASSES = frozenset(string.digits + string.ascii_letters)

# ---- punctuation harvested from the writer's own ink ---------------------------------------------------
PUNCTUATION = frozenset(".:,")
TITTLE_SOURCES = ("i", "j")       # period = the dot of an i or j; colon = two of them stacked
DESCENDER_SOURCES = ("j", "g")    # comma = a dot plus the clipped descender terminal of a j or g
INK_LEVEL = 128                   # on an SD19 raster (white background, black ink) a pixel < 128 is ink
MIN_TITTLE_INK = 4                # source pixels: smaller "dots" are specks, not tittles
MAX_TITTLE_FRAC = 0.4             # the dot is at most this fraction of the glyph's ink height ...
MIN_BODY_TO_TITTLE = 2.0          # ... and the stem below it at least twice the dot's height
DESCENDER_FRAC = 0.35             # bottom fraction of a j/g ink box kept as the descender terminal
MIN_TAIL_INK = 6
COLON_GAP = 2.2                   # colon: gap between the two dots, in dot heights
COMMA_TAIL = 1.6                  # comma: tail height, in dot heights


class Unverified(RuntimeError):
    """The archives are not pinned in the lockfile, or the cached bytes do not match it."""


class LayoutMismatch(RuntimeError):
    """An archive does not have the layout the patterns above assume."""


# ------------------------------------------------------------------ glyph source protocol

@runtime_checkable
class GlyphSource(Protocol):
    def writers(self) -> list[str]:
        """Writer ids, sorted."""

    def coverage(self, writer: str) -> set[str]:
        """Characters get() can produce for this writer: glyph classes, plus . : , when harvestable."""

    def get(self, writer: str, char: str, rng: random.Random):
        """One glyph instance (PIL 'L' image, white background, black ink). Picks are WITH replacement."""


def ink_mask(img):
    """'L' mask, 255 where the raster has ink."""
    return img.convert("L").point(lambda v: 255 if v < INK_LEVEL else 0)


def ink_pixels(mask) -> int:
    return sum(1 for v in mask.tobytes() if v)


def autocrop(img):
    """Crop a raster to its ink. Raises on a raster with no ink (never ship an invisible glyph)."""
    box = ink_mask(img).getbbox()
    if box is None:
        raise ValueError("glyph raster has no ink")
    return img.convert("L").crop(box)


def split_tittle(img):
    """The dot of an i or j as its own raster, or None when it is not cleanly separate (no blank row between
    dot and stem, a dot too large to be one, or a speck)."""
    mask = ink_mask(img)
    box = mask.getbbox()
    if box is None:
        return None
    x0, y0, x1, y1 = box
    crop = mask.crop(box)
    w, h = crop.size
    data = crop.tobytes()
    inked = [any(data[r * w:(r + 1) * w]) for r in range(h)]
    r = 0
    while r < h and inked[r]:
        r += 1
    dot_h = r
    while r < h and not inked[r]:
        r += 1
    if r >= h or dot_h > MAX_TITTLE_FRAC * h or (h - r) < MIN_BODY_TO_TITTLE * dot_h:
        return None
    dot = crop.crop((0, 0, w, dot_h))
    if ink_pixels(dot) < MIN_TITTLE_INK:
        return None
    dx0, dy0, dx1, dy1 = dot.getbbox()
    return img.convert("L").crop((x0 + dx0, y0 + dy0, x0 + dx1, y0 + dy1))


def descender_tail(img):
    """The clipped bottom terminal of a j or g (the lowest DESCENDER_FRAC of its ink), or None."""
    mask = ink_mask(img)
    box = mask.getbbox()
    if box is None:
        return None
    x0, y0, x1, y1 = box
    cut = y1 - max(2, round(DESCENDER_FRAC * (y1 - y0)))
    tail = mask.crop((x0, cut, x1, y1))
    tb = tail.getbbox()
    if tb is None or ink_pixels(tail) < MIN_TAIL_INK:
        return None
    return img.convert("L").crop((x0 + tb[0], cut + tb[1], x0 + tb[2], cut + tb[3]))


def _canvas(w: int, h: int):
    from PIL import Image

    return Image.new("L", (w, h), 255)


def _stamp(canvas, glyph, xy) -> None:
    """Merge glyph ink onto a white canvas (darker pixel wins)."""
    from PIL import ImageChops

    region = canvas.crop((xy[0], xy[1], xy[0] + glyph.width, xy[1] + glyph.height))
    canvas.paste(ImageChops.darker(region, glyph), xy)


def compose_period(dot):
    out = _canvas(dot.width + 2, dot.height + 2)
    _stamp(out, dot, (1, 1))
    return out


def compose_colon(top, bottom):
    gap = max(2, round(COLON_GAP * max(top.height, bottom.height)))
    w = max(top.width, bottom.width)
    out = _canvas(w + 2, top.height + gap + bottom.height + 2)
    _stamp(out, top, (1 + (w - top.width) // 2, 1))
    _stamp(out, bottom, (1 + (w - bottom.width) // 2, 1 + top.height + gap))
    return out


def compose_comma(dot, tail):
    from PIL import Image

    th = max(2, round(COMMA_TAIL * dot.height))
    tw = max(1, round(tail.width * th / tail.height))
    tail = tail.resize((tw, th), Image.Resampling.LANCZOS)
    w = max(dot.width, tw)
    out = _canvas(w + 2, dot.height + th + 1)
    _stamp(out, dot, (1 + w - dot.width, 1))
    _stamp(out, tail, (1 + w - tw, dot.height))  # overlaps the dot's last row: one connected mark
    return out


class RasterGlyphs:
    """GlyphSource over per-writer SD19-format rasters (128x128, white background, black ink). Subclasses
    provide writers(), _chars(writer) (classes with at least one sample) and _samples(writer, char) (every
    sample, fixed order). Punctuation is harvested from the same writer's ink, never drawn."""

    def __init__(self) -> None:
        self._coverage: dict[str, frozenset[str]] = {}

    def writers(self) -> list[str]:
        raise NotImplementedError

    def _chars(self, writer: str) -> set[str]:
        raise NotImplementedError

    def _samples(self, writer: str, char: str) -> list:
        raise NotImplementedError

    def _harvest(self, writer: str, sources: tuple[str, ...], fn, first_only: bool = False) -> list:
        chars = self._chars(writer)
        found = []
        for c in sources:
            if c not in chars:
                continue
            for g in self._samples(writer, c):
                part = fn(g)
                if part is not None:
                    found.append(part)
                    if first_only:
                        return found
        return found

    def tittles(self, writer: str) -> list:
        return self._harvest(writer, TITTLE_SOURCES, split_tittle)

    def tails(self, writer: str) -> list:
        return self._harvest(writer, DESCENDER_SOURCES, descender_tail)

    def coverage(self, writer: str) -> set[str]:
        if writer not in self._coverage:
            out = set(self._chars(writer)) & GLYPH_CLASSES
            if self._harvest(writer, TITTLE_SOURCES, split_tittle, first_only=True):
                out |= {".", ":"}
                if self._harvest(writer, DESCENDER_SOURCES, descender_tail, first_only=True):
                    out.add(",")
            self._coverage[writer] = frozenset(out)
        return set(self._coverage[writer])

    def get(self, writer: str, char: str, rng: random.Random):
        """One instance of `char` in this writer's hand. Each call picks independently WITH replacement:
        SD19 holds about one sample per letter per writer from the alphabet fields (more for letters in the
        constitution text), so a repeated letter in a value may reuse the same sample."""
        if char in PUNCTUATION:
            dots = self.tittles(writer)
            if not dots:
                raise KeyError(f"writer {writer} has no harvestable i/j dot for {char!r}")
            if char == ".":
                return compose_period(rng.choice(dots))
            if char == ":":
                return compose_colon(rng.choice(dots), rng.choice(dots))
            tails = self.tails(writer)
            if not tails:
                raise KeyError(f"writer {writer} has no harvestable j/g descender for ','")
            return compose_comma(rng.choice(dots), rng.choice(tails))
        samples = self._samples(writer, char)
        if not samples:
            raise KeyError(f"writer {writer} has no sample of {char!r}")
        return rng.choice(samples).copy()


# ------------------------------------------------------------------ lockfile and verification

def load_lock(path: Path | None = None) -> dict:
    return json.loads((path or LOCKFILE).read_text())


def require_verified(lock: dict) -> None:
    """Refuse unless every archive has a recorded sha256 and size (null = never verified)."""
    missing = [n for n in ARCHIVES if not lock["archives"][n].get("sha256") or lock["archives"][n].get("size") is None]
    if missing:
        raise Unverified(f"handwriting sources unverified: sha256/size are null for {', '.join(missing)} in "
                         f"{LOCKFILE.name}. Fetch first: python -m tools.hw_glyphs fetch --write-lock")


def file_digest(path: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def verify_archive(path: Path, entry: dict) -> tuple[str, int]:
    """sha256 and size of `path`; raises Unverified when the lock entry records different ones."""
    digest, size = file_digest(path)
    if entry.get("sha256") and (digest != entry["sha256"] or size != entry.get("size")):
        raise Unverified(f"{path.name}: sha256 {digest} / {size} B does not match the lockfile "
                         f"({entry['sha256']} / {entry.get('size')} B)")
    return digest, size


def _log_key(path: str, root: str) -> str | None:
    i = path.find(f"{root}/")
    while i > 0 and path[i - 1] != "/":
        i = path.find(f"{root}/", i + 1)
    return path[i:] if i >= 0 else None


def parse_md5_log(text: str, root: str) -> dict[str, str]:
    """Member path (from `root/` on) -> md5, for every file line of a NIST MD5 log under `root`."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = MD5_LOG_LINE.match(line)
        if not m or m["path"].endswith("/"):
            continue
        key = _log_key(m["path"], root)
        if key:
            out[key] = m["md5"].lower()
    return out


def verify_md5_log(zip_path: Path, log_path: Path, root: str) -> int:
    """Every file member of the zip under `root/` matches the NIST MD5 log, and every logged file is present.
    Returns the number of members checked."""
    logged = parse_md5_log(log_path.read_text(errors="replace"), root)
    if not logged:
        raise Unverified(f"{log_path.name}: no '{root}/' entries; not an MD5 log for {zip_path.name}")
    seen, bad = set(), []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            key = None if info.is_dir() else _log_key(info.filename, root)
            if key is None:
                continue
            seen.add(key)
            digest = hashlib.md5(z.read(info)).hexdigest()  # noqa: S324 - matching NIST's published MD5s
            if logged.get(key) != digest:
                bad.append(key)
    missing = sorted(set(logged) - seen)
    if bad or missing:
        raise Unverified(f"{zip_path.name} vs {log_path.name}: {len(bad)} md5 mismatch or unlisted "
                         f"(first {bad[:3]}), {len(missing)} logged but absent (first {missing[:3]})")
    return len(seen)


# ------------------------------------------------------------------ index (hash join)

_AMBIGUOUS = object()


def pixel_hash(png: bytes) -> bytes:
    """Hash of the decoded pixels (as 8-bit grayscale, with the size): the same glyph hashes the same whatever
    PNG encoding each archive used."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        g = im.convert("L")
    return hashlib.blake2b(f"{g.width}x{g.height}:".encode() + g.tobytes(), digest_size=16).digest()


def build_index(by_write: Path, by_class: Path, archives: dict | None = None) -> dict:
    """Join by_class (class) with by_write (writer) on decoded-pixel hashes. Returns the index document:
    writers -> class -> sorted by_write member names, plus counts of everything matched, joined and dropped."""
    stats: Counter[str] = Counter({k: 0 for k in (
        "by_class_matched", "by_class_unmatched", "by_write_matched", "by_write_unmatched", "class_conflict_hashes",
        "dropped_multi_class", "dropped_multi_writer", "joined", "unlabeled")})
    classes: dict[bytes, object] = {}
    with zipfile.ZipFile(by_class) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".png"):
                continue
            m = BY_CLASS_PATTERN.search(info.filename)
            char = chr(int(m["hex"], 16)) if m else None
            if char not in GLYPH_CLASSES:
                stats["by_class_unmatched"] += 1
                continue
            stats["by_class_matched"] += 1
            h = pixel_hash(z.read(info))
            prev = classes.get(h)
            if prev is None:
                classes[h] = char
            elif prev is not _AMBIGUOUS and prev != char:
                classes[h] = _AMBIGUOUS
    stats["class_conflict_hashes"] = sum(1 for v in classes.values() if v is _AMBIGUOUS)

    owners: dict[bytes, object] = {}
    with zipfile.ZipFile(by_write) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".png"):
                continue
            m = BY_WRITE_PATTERN.search(info.filename)
            if not m:
                stats["by_write_unmatched"] += 1
                continue
            stats["by_write_matched"] += 1
            h = pixel_hash(z.read(info))
            prev = owners.get(h)
            if prev is None:
                owners[h] = (m["writer"], info.filename)
            elif prev is not _AMBIGUOUS:
                if prev[0] != m["writer"]:
                    owners[h] = _AMBIGUOUS
                else:  # the same pixels twice for one writer: keep one member, chosen independent of zip order
                    owners[h] = (prev[0], min(prev[1], info.filename))
    for name, key in (("by_write", "by_write_matched"), ("by_class", "by_class_matched")):
        if not stats[key]:
            pattern = (BY_WRITE_PATTERN if name == "by_write" else BY_CLASS_PATTERN).pattern
            raise LayoutMismatch(f"{name}: no PNG member matches the layout pattern {pattern} "
                                 f"({stats[name + '_unmatched']} PNG members did not)")

    writers: dict[str, dict[str, list[str]]] = {}
    for h, owner in owners.items():
        if owner is _AMBIGUOUS:
            stats["dropped_multi_writer"] += 1
            continue
        char = classes.get(h)
        if char is None:
            stats["unlabeled"] += 1
            continue
        if char is _AMBIGUOUS:
            stats["dropped_multi_class"] += 1
            continue
        writers.setdefault(owner[0], {}).setdefault(char, []).append(owner[1])
        stats["joined"] += 1
    if not writers:
        raise LayoutMismatch("the by_class x by_write join produced no labeled glyph")
    return {
        "version": INDEX_VERSION,
        "patterns": {"by_write": BY_WRITE_PATTERN.pattern, "by_class": BY_CLASS_PATTERN.pattern},
        "archives": archives or {},
        "stats": dict(sorted(stats.items())),
        "writers": {w: {c: sorted(ms) for c, ms in sorted(cs.items())} for w, cs in sorted(writers.items())},
    }


# ------------------------------------------------------------------ the SD19 source

class SD19Glyphs(RasterGlyphs):
    """Glyphs read from the cached by_write.zip through the hash-join index. Refuses to load unless the lockfile
    pins both archives, the index was built from those exact archives, and by_write.zip still matches."""

    def __init__(self, root: Path = DEFAULT_CACHE, lock: dict | None = None, verify_bytes: bool = True) -> None:
        super().__init__()
        lock = load_lock() if lock is None else lock
        require_verified(lock)
        index_path = root / INDEX_NAME
        if not index_path.exists():
            raise Unverified(f"no glyph index at {index_path}; run: python -m tools.hw_glyphs index --cache {root}")
        index = json.loads(index_path.read_text())
        for name in ARCHIVES:
            if index.get("archives", {}).get(name, {}).get("sha256") != lock["archives"][name]["sha256"]:
                raise Unverified(f"{index_path.name} was not built from the locked {name}.zip; rebuild the index")
        zip_path, _log = _paths(root, "by_write", lock["archives"]["by_write"])
        if verify_bytes:
            verify_archive(zip_path, lock["archives"]["by_write"])
        self._zip = zipfile.ZipFile(zip_path)
        self._index: dict[str, dict[str, list[str]]] = index["writers"]
        self._decoded: dict[tuple[str, str], list] = {}

    def writers(self) -> list[str]:
        return sorted(self._index)

    def _chars(self, writer: str) -> set[str]:
        return {c for c, members in self._index.get(writer, {}).items() if members}

    def _samples(self, writer: str, char: str) -> list:
        from PIL import Image

        key = (writer, char)
        if key not in self._decoded:
            out = []
            for member in self._index.get(writer, {}).get(char, []):
                with Image.open(io.BytesIO(self._zip.read(member))) as im:
                    out.append(im.convert("L"))
            self._decoded[key] = out
        return self._decoded[key]


# ------------------------------------------------------------------ CLI (fetch is the only network path)

def _download(url: str, dest: Path) -> None:
    import urllib.request

    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=120) as r, tmp.open("wb") as f:  # noqa: S310 - fixed https URLs
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.replace(dest)


def _paths(cache: Path, name: str, entry: dict) -> tuple[Path, Path]:
    return cache / entry["url"].rsplit("/", 1)[1], cache / entry["md5_log_url"].rsplit("/", 1)[1]


def verify_cache(cache: Path, lock: dict) -> dict[str, dict]:
    """MD5 logs and lockfile for both archives; returns {name: {sha256, size, members}}."""
    out = {}
    for name in ARCHIVES:
        entry = lock["archives"][name]
        zpath, lpath = _paths(cache, name, entry)
        members = verify_md5_log(zpath, lpath, name)
        digest, size = verify_archive(zpath, entry)
        out[name] = {"sha256": digest, "size": size, "members": members}
    return out


def fetch(cache: Path, lock_path: Path, write_lock: bool, force: bool = False) -> dict[str, dict]:
    lock = load_lock(lock_path)
    cache.mkdir(parents=True, exist_ok=True)
    for name in ARCHIVES:
        entry = lock["archives"][name]
        for url, dest in zip((entry["url"], entry["md5_log_url"]), _paths(cache, name, entry)):
            if force or not dest.exists():
                print(f"fetch {url} -> {dest}", flush=True)
                _download(url, dest)
    result = verify_cache(cache, lock)
    unpinned = [n for n in ARCHIVES if not lock["archives"][n].get("sha256")]
    if unpinned and write_lock:
        for n in unpinned:
            lock["archives"][n]["sha256"], lock["archives"][n]["size"] = result[n]["sha256"], result[n]["size"]
        lock_path.write_text(json.dumps(lock, indent=2) + "\n")
        print(f"pinned {', '.join(unpinned)} in {lock_path}")
    elif unpinned:
        print(f"NOT pinned ({', '.join(unpinned)}): rerun with --write-lock to record sha256/size", file=sys.stderr)
    return result


def write_index(cache: Path, lock: dict) -> dict:
    require_verified(lock)
    archives = {}
    for name in ARCHIVES:
        zpath, _log = _paths(cache, name, lock["archives"][name])
        digest, size = verify_archive(zpath, lock["archives"][name])
        archives[name] = {"sha256": digest, "size": size}
    by_write, _l1 = _paths(cache, "by_write", lock["archives"]["by_write"])
    by_class, _l2 = _paths(cache, "by_class", lock["archives"]["by_class"])
    index = build_index(by_write, by_class, archives)
    tmp = cache / (INDEX_NAME + ".part")
    tmp.write_text(json.dumps(index, sort_keys=True) + "\n")
    tmp.replace(cache / INDEX_NAME)
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("fetch", "verify", "index"))
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--lock", type=Path, default=LOCKFILE)
    ap.add_argument("--write-lock", action="store_true", help="fetch: record sha256/size where the lock has null")
    ap.add_argument("--force", action="store_true", help="fetch: download again even if cached")
    args = ap.parse_args(argv)
    try:
        if args.command == "fetch":
            result = fetch(args.cache, args.lock, args.write_lock, args.force)
        elif args.command == "verify":
            lock = load_lock(args.lock)
            require_verified(lock)
            result = verify_cache(args.cache, lock)
        else:
            index = write_index(args.cache, load_lock(args.lock))
            result = {"writers": len(index["writers"]), **index["stats"]}
    except (Unverified, LayoutMismatch, FileNotFoundError) as e:
        print(f"hw_glyphs: REFUSED: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
