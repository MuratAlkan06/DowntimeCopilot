"""tools/demo_video/lib/post.mjs (with lib/timeline.mjs, which it re-exports), the demo video's post-production logic:
the global offset from ffprobe's first-frame PTS, cuts only inside still windows (with 0.75 s to spare) and the
"sped up" label past 2 s, the pre-roll and tail trims, captions in final-cut time, the exact encoder settings and the
mechanical gates. Runs the real modules in node against fake ffprobe output and fake timelines: no ffmpeg, no
Playwright, no network (skipped if node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
POST = Path(__file__).resolve().parent.parent / "tools" / "demo_video" / "lib" / "post.mjs"
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

T0 = 1_800_000_000_000  # context start, wall ms


def run(body: str, **data):
    script = (f"import * as P from {json.dumps(POST.as_uri())};\n"
              + "".join(f"const {k} = {json.dumps(v)};\n" for k, v in data.items())
              + "const out = (x) => console.log(JSON.stringify(x));\n"
              + "const err = (f) => { try { f(); return null; } catch (e) { return e.message; } };\n" + body)
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def still(id_, beat, s, e):
    return {"id": id_, "beat": beat, "start_ms": T0 + s, "end_ms": T0 + e}


def wait(id_, s, e, a=True):
    """A trim wait from s to e (ms after T0) with 1.5 s still windows: after the trigger and once it is done."""
    return {"id": id_, "start_ms": T0 + s, "end_ms": T0 + e,
            "a": [T0 + s, T0 + s + 1500] if a else None, "b": [T0 + e, T0 + e + 1500] if a else None}


def timeline(**over):
    """Pre-roll still 0.5-2.0 s, beat "one" 2.0-30.0 s (a 10 s wait at 4 s, a 3.5 s wait at 18 s), beat "two"
    30.0-40.0 s ending on a hold, then the tail still."""
    t = {
        "context_start_ms": T0,
        "stills": [
            still("preroll:edge", "preroll", 500, 2000), still("one:pause", "one", 2000, 3000),
            still("w1:a", "one", 4000, 5500), still("w1:b", "one", 14000, 15500),
            still("w2:a", "one", 18000, 19500), still("w2:b", "one", 21500, 23000),
            still("two:hold", "two", 37000, 40000), still("tail:edge", "tail", 40005, 41500),
        ],
        "beats": [
            {"id": "one", "max_s": 20, "speedup": True, "start_ms": T0 + 2000, "end_ms": T0 + 30000,
             "markers": {"m": T0 + 16000}, "waits": [wait("w1", 4000, 14000), wait("w2", 18000, 21500)]},
            {"id": "two", "max_s": 10, "speedup": True, "start_ms": T0 + 30000, "end_ms": T0 + 40000,
             "markers": {"close": T0 + 38000}, "waits": []},
        ],
    }
    t.update(over)
    return t


MANIFEST = {
    "opening": {"live": "All data is synthetic. Recorded live against real AWS services.",
                "dry_run": "DRY RUN — development footage, not a deliverable."},
    "dry_run_mark": "DRY RUN",
    "beats": [
        {"id": "one", "captions": [{"text": "First {caption}"}, {"at": "m", "text": "Second", "text_dry_run": "Second (dry)"}]},
        {"id": "two", "captions": [{"text": "Last\nline"}], "notice": {"at": "close", "text": "See RESULTS.md"}},
    ],
}


def test_first_frame_pts_and_duration_come_from_ffprobe_json():
    got = run(
        "out([P.firstFramePts({frames: [{pts_time: '0.360000'}]}),"
        " P.firstFramePts({frames: [{best_effort_timestamp_time: '0.040000'}]}),"
        " err(() => P.firstFramePts({frames: []})), err(() => P.firstFramePts({frames: [{pts_time: 'N/A'}]})),"
        " P.probeDuration({format: {duration: '88.120000'}}), err(() => P.probeDuration({format: {}}))]);")
    assert got[:2] == [0.36, 0.04]
    assert "no video frame" in got[2] and "no usable PTS" in got[3]
    assert got[4] == 88.12 and "no usable duration" in got[5]


def test_the_first_frame_pts_is_the_global_offset_of_every_trim():
    got = run("const segs = P.keepSegments(t, P.planCuts(t));\n"
              "const a = P.mp4Args({output: 'demo.mp4', segments: segs, timeline: t, ptsOffset: 0.36});\n"
              "out([P.videoTime(t.context_start_ms + 2000, t, 0.36), a[a.indexOf('-filter_complex') + 1]]);",
              t=timeline())
    assert got[0] == pytest.approx(2.36)
    assert "trim=start=2.360:end=5.110" in got[1]  # head at 2.0 s, the first cut's in-point at 4.75 s


def test_cuts_run_between_still_window_centres_and_long_ones_are_labelled():
    cuts = run("out(P.planCuts(t, {labels: {one: 'Sped up: packets, {s} s removed'}}));", t=timeline())
    assert [(c["wait"], c["in_ms"] - T0, c["out_ms"] - T0) for c in cuts] == [("w1", 4750, 14750), ("w2", 18750, 22250)]
    assert cuts[0]["removed_s"] == 10 and cuts[0]["label"] == "Sped up: packets, 10 s removed"
    assert cuts[1]["removed_s"] == 3.5 and cuts[1]["label"] == "Sped up: packets, 4 s removed"


def test_a_cut_of_two_seconds_or_less_is_unlabelled_and_a_short_wait_is_kept_whole():
    t = timeline()
    t["beats"][0]["waits"] = [wait("short", 4000, 4500), wait("two_s", 18000, 20000)]
    t["stills"] += [still("short:a", "one", 4000, 5500), still("short:b", "one", 4500, 6000),
                    still("two_s:a", "one", 18000, 19500), still("two_s:b", "one", 20000, 21500)]
    cuts = run("out(P.planCuts(t));", t=t)
    assert [(c["wait"], c["removed_s"], c["label"]) for c in cuts] == [("two_s", 2, None)]


def test_waits_without_windows_and_beats_without_speedup_are_never_cut():
    t = timeline()
    t["beats"][0]["waits"].append(wait("instant", 25000, 25050, a=False))
    assert [c["wait"] for c in run("out(P.planCuts(t));", t=t)] == ["w1", "w2"]
    t["beats"][0]["speedup"] = False
    assert run("out(P.planCuts(t));", t=t) == []


def test_a_cut_point_not_inside_a_still_window_with_tolerance_is_refused():
    t = timeline()
    t["stills"][2] = still("w1:a", "one", 4000, 5000)  # 1.0 s: a centre cut has only 0.5 s on each side
    msg = run("out(err(() => P.planCuts(t)));", t=t)
    assert "not inside a still window" in msg and "w1" in msg


def test_back_to_back_stills_join_into_one_window_for_the_head_and_tail_trims():
    t = timeline()
    assert run("out(P.stillWindows(t).length);", t=t) == 6  # preroll+pause and hold+tail joined
    t["stills"][1] = still("one:pause", "one", 2100, 3000)  # 100 ms gap: something happened in between
    assert "head trim point" in run("out(err(() => P.keepSegments(t, P.planCuts(t))));", t=t)


def test_segments_drop_the_preroll_tail_and_cuts_and_map_final_cut_time():
    got = run("const cuts = P.planCuts(t); const segs = P.keepSegments(t, cuts);\n"
              "const at = (s) => P.outputTime(t.context_start_ms + s * 1000, segs);\n"
              "out({segs: segs.map(([a, b]) => [a - t.context_start_ms, b - t.context_start_ms]), total: P.totalS(segs),"
              " t: [at(1), at(2), at(4.75), at(10), at(14.75), at(30), at(40), at(45)]});", t=timeline())
    assert got["segs"] == [[2000, 4750], [14750, 18750], [22250, 40000]]
    assert got["total"] == pytest.approx(38 - 13.5)
    # before the head -> 0; inside the first cut -> the cut itself; after it, shifted by the 10 s removed
    assert got["t"] == pytest.approx([0, 0, 2.75, 2.75, 2.75, 14.5, 24.5, 24.5])


def test_budget_report_measures_each_beat_after_its_cuts():
    t = timeline()
    t["beats"][1]["max_s"] = 9.5
    rep = run("out(P.budgetReport(t, P.planCuts(t)));", t=t)
    assert [(r["id"], r["post_cut_s"], r["over"]) for r in rep] == [("one", 14.5, False), ("two", 10, True)]


def test_captions_follow_markers_in_final_cut_time_with_labels_and_the_dry_run_mark():
    script = ("const cuts = P.planCuts(t, {labels: {one: 'Sped up: {s} s'}}); const segs = P.keepSegments(t, cuts);\n"
              "out([P.buildAss({manifest: m, mode: 'live', timeline: t, segments: segs, cuts}),"
              " P.buildAss({manifest: m, mode: 'dry_run', timeline: t, segments: segs, cuts})]);")
    live, dry = run(script, t=timeline(), m=MANIFEST)
    assert "PlayResX: 1280\nPlayResY: 800" in live
    ev = [line for line in live.splitlines() if line.startswith("Dialogue:")]
    assert "Dialogue: 0,0:00:00.00,0:00:04.00,Caption,,0,0,0,,First (caption)" in ev  # braces cannot open overrides
    assert "Dialogue: 0,0:00:04.00,0:00:14.50,Caption,,0,0,0,,Second" in ev  # marker at 16 s -> 4.0 s after the cut
    assert "Dialogue: 0,0:00:14.50,0:00:24.50,Caption,,0,0,0,,Last\\Nline" in ev
    assert "Dialogue: 1,0:00:00.00,0:00:14.50,Notice,,0,0,0,,All data is synthetic. Recorded live against real AWS services." in ev
    assert "Dialogue: 1,0:00:22.50,0:00:24.50,Notice,,0,0,0,,See RESULTS.md" in ev
    assert "Dialogue: 2,0:00:02.75,0:00:04.95,Label,,0,0,0,,Sped up: 10 s" in ev
    assert "Dialogue: 2,0:00:06.75,0:00:08.95,Label,,0,0,0,,Sped up: 4 s" in ev
    assert "Watermark,,0,0,0,,DRY RUN" not in live and "DRY RUN" not in live.split("[Events]")[1]
    assert "Notice,,0,0,0,,DRY RUN — development footage, not a deliverable." in dry
    assert "Caption,,0,0,0,,Second (dry)" in dry
    assert "Dialogue: 3,0:00:00.00,0:00:24.50,Watermark,,0,0,0,,DRY RUN" in dry


def test_a_caption_marker_never_reached_is_an_error():
    t = timeline()
    t["beats"][0]["markers"] = {}
    msg = run("const segs = P.keepSegments(t, P.planCuts(t));\n"
              "out(err(() => P.buildAss({manifest: m, mode: 'live', timeline: t, segments: segs, cuts: []})));",
              t=t, m=MANIFEST)
    assert "caption marker m was never reached" in msg


def test_ass_times_are_centiseconds():
    assert run("out([P.assTime(0), P.assTime(3661.237), P.assTime(59.999), P.assTime(-1)]);") == [
        "0:00:00.00", "1:01:01.24", "0:01:00.00", "0:00:00.00"]


def test_mp4_command_has_the_frozen_encoder_settings_and_no_audio():
    args = run("const segs = P.keepSegments(t, P.planCuts(t));\n"
               "out(P.mp4Args({output: 'demo.mp4', segments: segs, timeline: t, ptsOffset: 0}));", t=timeline())
    pairs = {args[i]: args[i + 1] for i in range(len(args) - 1)}
    assert {k: pairs[k] for k in ("-c:v", "-preset", "-crf", "-maxrate", "-bufsize", "-pix_fmt", "-movflags")} == {
        "-c:v": "libx264", "-preset": "slow", "-crf": "23", "-maxrate": "850k", "-bufsize": "1700k",
        "-pix_fmt": "yuv420p", "-movflags": "+faststart"}
    assert "-an" in args and args[-1] == "demo.mp4" and pairs["-i"] == "raw.webm"
    graph = pairs["-filter_complex"]
    assert graph.startswith("[0:v]split=3[s0][s1][s2];") and "concat=n=3:v=1:a=0[cat]" in graph
    assert graph.endswith("[cat]scale=1280:800:flags=lanczos,setsar=1,subtitles=captions.ass[out]")


def test_gif_is_a_window_of_the_final_cut_at_ten_fps_and_800_px():
    got = run("const segs = P.keepSegments(t, P.planCuts(t));\n"
              "const w = P.gifWindow({timeline: t, segments: segs, beat: 'one', from: 'm', seconds: 12});\n"
              "const whole = P.gifWindow({timeline: t, segments: segs, beat: 'two'});\n"
              "out({w, whole, args: P.gifArgs({input: 'demo.mp4', output: 'demo.gif', startS: w.startS, seconds: w.seconds}),"
              " tooLong: err(() => P.gifArgs({input: 'a', output: 'b', startS: 0, seconds: 12.5})),"
              " noMark: err(() => P.gifWindow({timeline: t, segments: segs, beat: 'one', from: 'nope'}))});", t=timeline())
    assert got["w"] == {"startS": 4, "seconds": pytest.approx(10.5)}  # from the marker to the beat's end, under 12 s
    assert got["whole"] == {"startS": 14.5, "seconds": 10}
    args = got["args"]
    graph = args[args.index("-filter_complex") + 1]
    assert graph.startswith("fps=10,scale=800:-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];")
    assert "paletteuse" in graph and args[args.index("-ss") + 1] == "4.000" and args[-1] == "demo.gif"
    assert "over the 12 s cap" in got["tooLong"] and "never reached" in got["noMark"]


def test_mechanical_gates():
    got = run("out([P.checkGates({mp4DurationS: 85, mp4Bytes: 10000000, gifBytes: 5000000}),"
              " P.checkGates({mp4DurationS: 95, mp4Bytes: 1, gifBytes: 1}),"
              " P.checkGates({mp4DurationS: 84.99, mp4Bytes: 1, gifBytes: 1}),"
              " P.checkGates({mp4DurationS: 95.01, mp4Bytes: 10000001, gifBytes: 5000001})]);")
    assert got[0] == [] and got[1] == []
    assert len(got[2]) == 1 and "outside 85-95 s" in got[2][0]
    assert len(got[3]) == 3 and "over 10000000" in got[3][1] and "over 5000000" in got[3][2]


def test_reading_time_is_twenty_characters_a_second_with_a_two_second_floor():
    assert run("out([P.readS('short'), P.readS('x'.repeat(80))]);") == [2, 4]
