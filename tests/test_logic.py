import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skynet_clipper.contract import OutStore, validate_meta  # noqa: E402
from skynet_clipper.judge import fix_bounds, parse_reply  # noqa: E402
from skynet_clipper.signals import (HourBudget, Peak, RollingZ, SpikeDetector, Timeline, combined_score,  # noqa: E402
                                    dedupe, hype_score, parse_chat_line)
from skynet_clipper.translit import to_roman  # noqa: E402


def test_rolling_z_flags_spike_not_noise():
    z = RollingZ(300, min_samples=30)
    for i in range(200):
        z.push(1.0 + (i % 3) * 0.1)
    assert abs(z.z(1.1)) < 1.0
    assert z.z(6.0) > 4.0


def test_rolling_z_warmup_is_zero():
    z = RollingZ(300, min_samples=30)
    for _ in range(10):
        z.push(1.0)
    assert z.z(100.0) == 0.0


def test_spike_detector_one_peak_per_excursion_and_gap():
    sd = SpikeDetector(threshold=2.0, min_gap=30)
    seq = [0, 1, 2.5, 4.0, 3.0, 1.0, 0, 2.2, 1.0]  # second excursion at t=7 is within the gap
    out = [sd.update(Peak(t, s)) for t, s in enumerate(seq)]
    peaks = [p for p in out if p]
    assert len(peaks) == 1 and peaks[0].t == 3 and peaks[0].score == 4.0


def test_combined_score_rewards_agreement():
    only_chat = combined_score(3, 0, 0)
    both = combined_score(3, 0, 3)
    assert both > only_chat > 0
    assert combined_score(-2, -1, -3) == 0


def test_dedupe_keeps_best_of_overlaps():
    c = [{"t_start": 0, "t_end": 30, "score": 50}, {"t_start": 20, "t_end": 50, "score": 80},
         {"t_start": 200, "t_end": 230, "score": 40}]
    out = dedupe(c, min_gap=30)
    assert [x["score"] for x in out] == [80, 40]


def test_hour_budget():
    b = HourBudget(2, total=3)
    assert b.allow(0); b.take(0)
    assert b.allow(10); b.take(10)
    assert not b.allow(20)
    assert b.allow(3700); b.take(3700)
    assert not b.allow(9000)  # stream total cap


def test_hype_score():
    assert hype_score("hahaha 😂😂") > hype_score("hello")
    assert hype_score(":_BakchodianLOL:") > 0


def test_chat_parse_and_timeline():
    line = {"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
        "message": {"runs": [{"text": "lol "}, {"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}}]},
        "authorName": {"simpleText": "a"}, "timestampUsec": "1795458700000000"}}}}]}}
    msgs = parse_chat_line(line)
    assert len(msgs) == 1 and msgs[0].text == "lol 😂" and msgs[0].ts == 1795458700.0
    tl = Timeline()
    tl.add_chat(5.2, msgs[0].text)
    rate, hype = tl.chat_rate(9, 10)
    assert rate == 0.1 and hype > 0


def test_judge_parse_and_bounds():
    txt = 'noise {"score": 81, "reason": "x", "start": 100, "end": 190, "title": "T", "caption": "c", "hashtags": ["gta", "#carry"]}'
    v = parse_reply(txt, 90, 170, 20, 55)
    assert v["score"] == 81 and v["start"] == 100 and v["end"] == 155
    assert v["hashtags"] == ["#gta", "#carry"]
    s, e = fix_bounds(100, 105, 90, 170, 20, 55)
    assert e - s >= 20 and s >= 90 and e <= 170
    assert parse_reply("no json here", 0, 10, 20, 55) is None


def test_meta_contract(tmp_path):
    meta = {"id": "x", "stream_url": "u", "streamer": "s", "stream_title": "t", "t_start": 1.0, "t_end": 30.0,
            "duration": 29.0, "created_at": "now", "score": 70.0, "reasons": ["r"],
            "signals": {"chat_z": 1.0, "audio_z": 2.0, "llm_score": 70.0}, "transcript": "", "title": "T",
            "caption": "c", "hashtags": ["#a"], "status": "ready"}
    assert validate_meta(meta) == []
    bad = dict(meta)
    del bad["signals"]
    assert validate_meta(bad)
    st = OutStore(str(tmp_path))
    st.write_meta("x", meta)
    assert json.loads((tmp_path / "clips" / "x" / "meta.json").read_text(encoding="utf-8"))["id"] == "x"
    st.event("info", "hello")
    st.signal(1, 0.5, 1.0, -20, 0.3)
    rec = json.loads((tmp_path / "signals.jsonl").read_text().splitlines()[0])
    assert set(rec) == {"ts", "t", "chat_rate", "chat_z", "audio_db", "audio_z"}


def test_translit_readable():
    assert to_roman("भाई क्या कर रहा है") == "bhai kya kar raha hai"
    assert to_roman("जल्दी भागो") == "jaldi bhago"
    assert to_roman("समझता") == "samajhta"


def test_judge_unsafe_flag():
    v = parse_reply('{"score": 70, "start": 0, "end": 30, "title": "t", "caption": "c", "hashtags": [], "safe": false}',
                    0, 60, 20, 55)
    assert v["safe"] is False
    v = parse_reply('{"score": 70, "start": 0, "end": 30, "title": "t", "caption": "c", "hashtags": []}', 0, 60, 20, 55)
    assert v["safe"] is True


def test_caption_censor():
    from skynet_clipper.render import clean_word
    assert clean_word("bhai") == "BHAI"
    assert clean_word("madarchod").startswith("M*")
