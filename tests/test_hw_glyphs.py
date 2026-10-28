"""SD19 glyph source (tools/hw_glyphs.py): hash-join indexer on fake archives, punctuation harvest, lockfile.

Everything here is offline: the stub glyph pack is drawn at test time in SD19's raster format (128x128, white
background, black ink, binary) and the "archives" are tmp zips. No dataset bytes, no network.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import random
import socket
import string
import zipfile
from functools import lru_cache

import pytest
from PIL import Image, ImageDraw, ImageFont

from tools import hw_glyphs

STUB_CHARS = string.digits + string.ascii_letters
SD19_SIZE = (128, 128)


def stub_raster(char: str, rng: random.Random):
    """One SD19-format raster of `char`: 128x128, white background, black binary ink, jittered size/offset/stroke."""
    font = ImageFont.load_default(size=rng.randint(52, 70))
    stroke = rng.randint(0, 1)  # 2 closes the gap under an i/j dot at these sizes
    img = Image.new("L", SD19_SIZE, 255)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = d.textbbox((0, 0), char, font=font, stroke_width=stroke)
    x = (SD19_SIZE[0] - (x1 - x0)) // 2 - x0 + rng.randint(-6, 6)
    y = (SD19_SIZE[1] - (y1 - y0)) // 2 - y0 + rng.randint(-6, 6)
    d.text((x, y), char, font=font, fill=0, stroke_width=stroke, stroke_fill=0)
    return img.point(lambda v: 0 if v < 128 else 255)


@lru_cache(maxsize=None)
def _pack(n_writers: int, seed: int, samples: int) -> dict:
    out = {}
    for i in range(n_writers):
        writer = f"f{i:04d}_{i % 100:02d}"
        rng = random.Random(f"stub:{seed}:{writer}")
        out[writer] = {c: tuple(stub_raster(c, rng) for _ in range(samples)) for c in STUB_CHARS}
    return out


def stub_pack(n_writers: int = 4, seed: int = 7, samples: int = 1, drop: dict[str, str] | None = None) -> dict:
    """writer -> char -> [rasters]. `drop` removes characters from writers ({writer: "chars"}, "*" = everyone)."""
    pack = {w: {c: list(v) for c, v in cs.items()} for w, cs in _pack(n_writers, seed, samples).items()}
    for writer, chars in (drop or {}).items():
        for w in pack if writer == "*" else [writer]:
            for c in chars:
                pack[w].pop(c, None)
    return pack


class StubGlyphs(hw_glyphs.RasterGlyphs):
    """GlyphSource over a stub pack (the injection point the generator tests use)."""

    def __init__(self, pack: dict) -> None:
        super().__init__()
        self.pack = pack

    def writers(self) -> list[str]:
        return sorted(self.pack)

    def _chars(self, writer: str) -> set[str]:
        return {c for c, v in self.pack[writer].items() if v}

    def _samples(self, writer: str, char: str) -> list:
        return self.pack[writer].get(char, [])


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)


# ------------------------------------------------------------------ stub format + protocol

def test_stub_rasters_match_sd19_format():
    g = stub_raster("a", random.Random(1))
    assert g.size == SD19_SIZE and g.mode == "L"
    assert set(g.tobytes()) == {0, 255}  # binary: black ink on white
    assert g.getpixel((0, 0)) == 255


def test_stub_source_satisfies_the_protocol():
    src = StubGlyphs(stub_pack())
    assert isinstance(src, hw_glyphs.GlyphSource)
    w = src.writers()[0]
    assert set(STUB_CHARS) | {".", ":", ","} <= src.coverage(w)
    g = src.get(w, "a", random.Random(0))
    assert g.size == SD19_SIZE and g.mode == "L"


# ------------------------------------------------------------------ punctuation harvest

def test_tittle_splits_from_i_and_j_but_not_from_stems():
    rng = random.Random(3)
    for c in "ij":
        dot = hw_glyphs.split_tittle(stub_raster(c, rng))
        assert dot is not None, c
        body = hw_glyphs.ink_mask(stub_raster(c, random.Random(3))).getbbox()
        assert dot.height < hw_glyphs.MAX_TITTLE_FRAC * (body[3] - body[1])
        assert hw_glyphs.ink_pixels(hw_glyphs.ink_mask(dot)) >= hw_glyphs.MIN_TITTLE_INK
    for c in "l1I":
        assert hw_glyphs.split_tittle(stub_raster(c, rng)) is None, c
    assert hw_glyphs.split_tittle(Image.new("L", SD19_SIZE, 255)) is None


def test_descender_tail_and_composites():
    rng = random.Random(5)
    dot = hw_glyphs.split_tittle(stub_raster("i", rng))
    tail = hw_glyphs.descender_tail(stub_raster("j", rng))
    assert tail is not None and tail.height >= 2
    colon = hw_glyphs.compose_colon(dot, dot)
    mask = hw_glyphs.ink_mask(colon)
    rows = [any(mask.crop((0, r, mask.width, r + 1)).tobytes()) for r in range(mask.height)]
    runs = sum(1 for a, b in zip([False, *rows], rows) if b and not a)
    assert runs == 2  # two separate dots stacked
    comma = hw_glyphs.compose_comma(dot, tail)
    assert comma.height > dot.height and hw_glyphs.ink_mask(comma).getbbox() is not None
    period = hw_glyphs.compose_period(dot)
    assert hw_glyphs.ink_pixels(hw_glyphs.ink_mask(period)) == hw_glyphs.ink_pixels(hw_glyphs.ink_mask(dot))


def test_punctuation_coverage_needs_the_writers_own_ink():
    pack = stub_pack(n_writers=3, drop={"f0001_01": "ij", "f0002_02": "jg"})
    src = StubGlyphs(pack)
    assert {".", ":", ","} <= src.coverage("f0000_00")
    assert not ({".", ":", ","} & src.coverage("f0001_01"))  # no i or j: no dot to harvest
    cov = src.coverage("f0002_02")
    assert {".", ":"} <= cov and "," not in cov  # dots from i, but no j/g descender for the comma
    with pytest.raises(KeyError):
        src.get("f0001_01", ".", random.Random(0))
    with pytest.raises(KeyError):
        src.get("f0002_02", ",", random.Random(0))


def test_get_picks_with_replacement_and_is_seeded():
    src = StubGlyphs(stub_pack(samples=3))
    w = src.writers()[0]
    a = [src.get(w, "e", random.Random(9)).tobytes() for _ in range(2)]
    assert a[0] == a[1]
    picks = [src.get(w, "e", r).tobytes() for r in [random.Random(1)] for _ in range(12)]
    assert len(set(picks)) < len(picks)  # repeats happen: picks are with replacement


def test_autocrop_refuses_blank_rasters():
    with pytest.raises(ValueError):
        hw_glyphs.autocrop(Image.new("L", SD19_SIZE, 255))
    crop = hw_glyphs.autocrop(stub_raster("H", random.Random(2)))
    assert crop.size[0] < 128 and crop.size[1] < 128


# ------------------------------------------------------------------ fake archives and the hash join

def _png(img, mode: str) -> bytes:
    buf = io.BytesIO()
    img.convert(mode).save(buf, "PNG")
    return buf.getvalue()


def _field(c: str) -> str:
    return "d" if c.isdigit() else "u" if c.isupper() else "l"


def make_fake_archives(tmp_path, prefix: str = ""):
    """by_write.zip + by_class.zip reproducing SD19's 2nd-edition layouts with stub PNGs.

    by_write PNGs are 1-bit, by_class PNGs 8-bit gray: file bytes differ, decoded pixels agree, so the join
    must work on pixels. Planted cases: one raster filed under two classes (0 and O), one raster under two
    writers, one by_write raster absent from by_class, a duplicate under train_<hex>, and stray members."""
    rng = random.Random(11)
    writers = {"f0000_14": "hsf_0", "f0500_07": "hsf_1", "f2100_03": "hsf_4"}
    chars = "aij1Agx"
    glyphs = {(w, c): stub_raster(c, rng) for w in writers for c in chars}
    write_members, class_members = [], []
    counters: dict[tuple[str, str], int] = {}
    for (w, c), g in glyphs.items():
        f = _field(c)
        n = counters.setdefault((w, f), 0)
        counters[(w, f)] += 1
        wid = w[1:]
        write_members.append((f"{prefix}by_write/{writers[w]}/{w}/{f}{wid}/{f}{wid}_{n:05d}.png", _png(g, "1")))
    for i, ((w, c), g) in enumerate(sorted(glyphs.items())):
        hx = f"{ord(c):02x}"
        class_members.append((f"{prefix}by_class/{hx}/{writers[w]}/{writers[w]}_{i:05d}.png", _png(g, "L")))
        class_members.append((f"{prefix}by_class/{hx}/train_{hx}/train_{hx}_{i:05d}.png", _png(g, "L")))
    # ambiguous class: one raster labeled both "0" and "O" (writer f0000_14, constitution field)
    amb = stub_raster("0", rng)
    write_members.append((f"{prefix}by_write/hsf_0/f0000_14/c0000_14/c0000_14_00000.png", _png(amb, "1")))
    class_members.append((f"{prefix}by_class/30/hsf_0/hsf_0_90000.png", _png(amb, "L")))
    class_members.append((f"{prefix}by_class/4f/hsf_0/hsf_0_90001.png", _png(amb, "L")))
    # ambiguous writer: the same raster under two writers, labeled "Z"
    dup = stub_raster("Z", rng)
    write_members.append((f"{prefix}by_write/hsf_0/f0000_14/u0000_14/u0000_14_00099.png", _png(dup, "1")))
    write_members.append((f"{prefix}by_write/hsf_1/f0500_07/u0500_07/u0500_07_00099.png", _png(dup, "1")))
    class_members.append((f"{prefix}by_class/5a/hsf_0/hsf_0_90002.png", _png(dup, "L")))
    # unlabeled: in by_write only
    write_members.append((f"{prefix}by_write/hsf_4/f2100_03/l2100_03/l2100_03_00099.png",
                          _png(stub_raster("q", rng), "1")))
    # stray members the patterns must not match
    write_members.append((f"{prefix}by_write/hsf_0/f0000_14/README.png", _png(stub_raster("k", rng), "1")))
    class_members.append((f"{prefix}by_class/30/hsf_0.mit", b"not a png"))
    paths = {}
    for name, members in (("by_write", write_members), ("by_class", class_members)):
        paths[name] = tmp_path / f"{name}.zip"
        with zipfile.ZipFile(paths[name], "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{prefix}{name}/", b"")
            for member, data in members:
                z.writestr(member, data)
    return paths, glyphs, writers


@pytest.mark.parametrize("prefix", ["", "sd19/2ndEdition2016/data/"])
def test_index_hash_joins_class_and_writer(tmp_path, no_network, prefix):
    paths, glyphs, writers = make_fake_archives(tmp_path, prefix)
    with zipfile.ZipFile(paths["by_write"]) as zw, zipfile.ZipFile(paths["by_class"]) as zc:
        wbytes = {n: zw.read(n) for n in zw.namelist() if n.endswith(".png")}
        cbytes = {n: zc.read(n) for n in zc.namelist() if n.endswith(".png")}
    assert not set(wbytes.values()) & set(cbytes.values())  # no PNG file is byte-identical across archives

    index = hw_glyphs.build_index(paths["by_write"], paths["by_class"])
    assert sorted(index["writers"]) == sorted(writers)
    for w in writers:
        assert set(index["writers"][w]) == set("aij1Agx")  # Z (two writers), 0/O (two classes), q (unlabeled) dropped
        for c, members in index["writers"][w].items():
            assert len(members) == 1
            member = members[0]
            assert f"/{w}/" in f"/{member}" and member.startswith(prefix)
            with zipfile.ZipFile(paths["by_write"]) as z:
                got = Image.open(io.BytesIO(z.read(member))).convert("L")
            assert got.tobytes() == glyphs[(w, c)].tobytes()  # the class label belongs to these exact pixels
    s = index["stats"]
    assert s["dropped_multi_class"] == 1 and s["dropped_multi_writer"] == 1 and s["unlabeled"] == 1
    assert s["joined"] == 3 * 7 and s["class_conflict_hashes"] == 1
    assert s["by_write_unmatched"] == 1 and s["by_class_unmatched"] == 0


def test_sd19_source_reads_glyphs_through_the_index(tmp_path, no_network):
    paths, glyphs, _writers = make_fake_archives(tmp_path)
    lock = copy.deepcopy(hw_glyphs.load_lock())
    for name in hw_glyphs.ARCHIVES:
        digest, size = hw_glyphs.file_digest(paths[name])
        lock["archives"][name].update(sha256=digest, size=size)
    index = hw_glyphs.write_index(tmp_path, lock)
    assert (tmp_path / hw_glyphs.INDEX_NAME).exists() and index["archives"]["by_write"]["sha256"]
    src = hw_glyphs.SD19Glyphs(tmp_path, lock=lock)
    assert isinstance(src, hw_glyphs.GlyphSource)
    assert src.writers() == sorted(_writers)
    assert src.coverage("f0000_14") == set("aij1Agx") | {".", ":", ","}
    got = src.get("f0500_07", "g", random.Random(0))
    assert got.tobytes() == glyphs[("f0500_07", "g")].tobytes()
    assert hw_glyphs.ink_mask(src.get("f0500_07", ",", random.Random(0))).getbbox() is not None

    # the index must come from the locked archives
    other = copy.deepcopy(lock)
    other["archives"]["by_class"]["sha256"] = "0" * 64
    with pytest.raises(hw_glyphs.Unverified):
        hw_glyphs.SD19Glyphs(tmp_path, lock=other)
    # and by_write.zip must still be the locked bytes
    tampered = copy.deepcopy(lock)
    tampered["archives"]["by_write"]["size"] += 1
    with pytest.raises(hw_glyphs.Unverified):
        hw_glyphs.SD19Glyphs(tmp_path, lock=tampered)


def test_index_refuses_a_layout_it_does_not_recognize(tmp_path):
    for name in ("by_write", "by_class"):
        with zipfile.ZipFile(tmp_path / f"{name}.zip", "w") as z:
            z.writestr(f"{name}/flat/glyph_00000.png", _png(stub_raster("a", random.Random(0)), "L"))
    with pytest.raises(hw_glyphs.LayoutMismatch, match="layout pattern"):
        hw_glyphs.build_index(tmp_path / "by_write.zip", tmp_path / "by_class.zip")


def test_layout_patterns_match_the_published_md5_log_paths():
    # member paths exactly as NIST's by_write_md5.log / by_class_md5.log list them
    w = hw_glyphs.BY_WRITE_PATTERN.search(
        "sd19/2ndEdition2016/data/by_write/hsf_0/f0000_14/c0000_14/c0000_14_00000.png")
    assert w and w["writer"] == "f0000_14" and w["field"] == "c" and w["partition"] == "hsf_0"
    assert not hw_glyphs.BY_WRITE_PATTERN.search("by_write/hsf_0/f0000_14/d0001_14/d0001_14_00000.png")
    for path, char in (("sd19/2ndEdition2016/data/by_class/30/hsf_0/hsf_0_00000.png", "0"),
                       ("sd19/2ndEdition2016/data/by_class/7a/train_7a/train_7a_00001.png", "z")):
        m = hw_glyphs.BY_CLASS_PATTERN.search(path)
        assert m and chr(int(m["hex"], 16)) == char
    assert not hw_glyphs.BY_CLASS_PATTERN.search("sd19/2ndEdition2016/data/by_class/30/hsf_0.mit")


# ------------------------------------------------------------------ MD5 logs and the lockfile

def test_md5_log_verification(tmp_path):
    paths, _g, _w = make_fake_archives(tmp_path)
    lines = []
    with zipfile.ZipFile(paths["by_write"]) as z:
        for info in z.infolist():
            if not info.is_dir():
                lines.append(f"{hashlib.md5(z.read(info)).hexdigest()} sd19/2ndEdition2016/data/{info.filename}")
    log = tmp_path / "by_write_md5.log"
    log.write_text("d41d8cd98f00b204e9800998ecf8427e sd19/\n" + "\n".join(lines) + "\n")
    assert hw_glyphs.verify_md5_log(paths["by_write"], log, "by_write") == len(lines)

    bad = lines[:]
    bad[0] = "0" * 32 + bad[0][32:]
    log.write_text("\n".join(bad) + "\n")
    with pytest.raises(hw_glyphs.Unverified, match="mismatch"):
        hw_glyphs.verify_md5_log(paths["by_write"], log, "by_write")
    log.write_text("\n".join(lines + [f"{'1' * 32} sd19/2ndEdition2016/data/by_write/hsf_9/missing.png"]) + "\n")
    with pytest.raises(hw_glyphs.Unverified, match="absent"):
        hw_glyphs.verify_md5_log(paths["by_write"], log, "by_write")


def test_committed_lockfile_names_the_nist_archives():
    lock = hw_glyphs.load_lock()
    assert set(lock["archives"]) == set(hw_glyphs.ARCHIVES) and lock["doi"] == "10.18434/T4H01C"
    for name, entry in lock["archives"].items():
        assert entry["url"] == f"https://s3.amazonaws.com/nist-srd/SD19/{name}.zip"
        assert entry["md5_log_url"] == f"https://s3.amazonaws.com/nist-srd/SD19/{name}_md5.log"
        assert set(entry) == {"url", "md5_log_url", "sha256", "size"}


def test_null_lock_entries_refuse_to_load(tmp_path):
    lock = copy.deepcopy(hw_glyphs.load_lock())
    for entry in lock["archives"].values():
        entry.update(sha256=None, size=None)
    with pytest.raises(hw_glyphs.Unverified, match="(?i)unverified.*fetch first"):
        hw_glyphs.require_verified(lock)
    with pytest.raises(hw_glyphs.Unverified, match="unverified"):
        hw_glyphs.SD19Glyphs(tmp_path, lock=lock)
    with pytest.raises(hw_glyphs.Unverified):
        hw_glyphs.write_index(tmp_path, lock)
    lock["archives"]["by_write"].update(sha256="a" * 64, size=1)
    with pytest.raises(hw_glyphs.Unverified, match="by_class"):
        hw_glyphs.require_verified(lock)


def test_verify_archive_checks_sha256_and_size(tmp_path):
    f = tmp_path / "by_write.zip"
    f.write_bytes(b"sd19")
    digest = hashlib.sha256(b"sd19").hexdigest()
    assert hw_glyphs.verify_archive(f, {"sha256": None, "size": None}) == (digest, 4)
    assert hw_glyphs.verify_archive(f, {"sha256": digest, "size": 4}) == (digest, 4)
    with pytest.raises(hw_glyphs.Unverified):
        hw_glyphs.verify_archive(f, {"sha256": "0" * 64, "size": 4})


def test_cli_verify_refuses_an_unpinned_lock(tmp_path, capsys, no_network):
    lock = copy.deepcopy(hw_glyphs.load_lock())
    for entry in lock["archives"].values():
        entry.update(sha256=None, size=None)
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(lock))
    assert hw_glyphs.main(["verify", "--cache", str(tmp_path), "--lock", str(path)]) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert hw_glyphs.main(["index", "--cache", str(tmp_path), "--lock", str(path)]) == 2
