"""The engine: ingest -> signals -> candidates -> LLM judge -> render, with caps and breakers."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import judge as J
from .asr import Transcriber, Word, per_second_db, segment_pcm, SR
from .contract import OutStore, now_iso, atomic_write_json
from .ingest import Ingest
from .layout import Box, detect_face, grab_frame, plan_layout
from .render import measure_loudness, render_clip, render_cover
from .signals import (HourBudget, Peak, RollingZ, SpikeDetector, Timeline, combined_score, keyword_hits,
                      overlaps_any)
from .util import YTDLP, log, set_log

COPYRIGHT = Path.home() / ".claude" / "skills" / "music-copyright-check" / "check_music.py"


def stream_id_from(url: str, fallback: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/live/)([\w-]{6,})", url) or re.search(r"(?:twitch\.tv|kick\.com)/([\w-]+)", url)
    return m.group(1) if m else fallback


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        src = cfg["source"]
        self.sid = stream_id_from(src["url"], "stream")
        self.data = Path(cfg["paths"]["data_root"]) / self.sid
        self.data.mkdir(parents=True, exist_ok=True)
        set_log(self.data / "engine.log")
        self.out = OutStore(cfg["paths"]["out_root"])
        self.ingest = Ingest(cfg, self.data)
        self.tl = Timeline()
        d = cfg["detect"]
        self.z_chat = RollingZ(d["baseline_seconds"], min_samples=60)
        self.z_hype = RollingZ(d["baseline_seconds"], min_samples=60, floor=0.3)
        self.z_audio = RollingZ(d["baseline_seconds"], min_samples=60, floor=1.5)
        self.spike = SpikeDetector(d["combined_trigger"], d["min_gap_s"])
        self.scores: dict[int, Peak] = {}
        self.det_t = -1               # last processed stream second
        self.audio_t = 0.0            # audio processed up to
        self.asr_t = 0.0              # transcript covers up to
        self.words: list[Word] = []
        self.words_lock = threading.Lock()
        self.pcm_chunks: list[tuple[float, np.ndarray]] = []
        self.pcm_lock = threading.Lock()
        self.seg_q: queue.Queue = queue.Queue()
        self.pending: list[dict] = []
        self.pending_lock = threading.RLock()
        self.judging = 0
        self.render_q: queue.Queue = queue.Queue()
        self.cut_ranges: list[tuple[float, float]] = []
        self.chat_count = 0
        self.cands_seen = 0
        self.clips_ready = 0
        self.llm_calls = 0
        self.llm_fail = 0
        self.llm_budget = HourBudget(cfg["judge"]["max_calls_per_hour"], cfg["judge"]["max_calls_per_stream"])
        self.clip_budget = HourBudget(cfg["clips"]["max_per_hour"])
        self.last_cand_t = 0.0
        self.started_wall = time.time()
        self.started_at = now_iso()
        self.stop_flag = threading.Event()
        self.layout = None
        self.light = False
        self.encoder = cfg["render"]["encoder"]
        self.latencies: list[float] = []
        self.last_render_s = 0.0
        self.cmd_offset = 0
        self.switch_url: str | None = None
        self.asr = Transcriber(cfg["asr"]["model"], cfg["asr"]["device"], cfg["asr"]["compute_type"],
                               cfg["asr"]["language"], cfg["asr"]["beam_size"]) if cfg["asr"]["enabled"] else None
        self.stats_path = self.data / "stats.json"

    # ------------------------------------------------------------------ helpers
    def ev(self, typ: str, msg: str, data: dict | None = None) -> None:
        log(f"[{typ}] {msg}")
        self.out.event(typ, msg, data)

    def transcript(self, t0: float, t1: float) -> list[Word]:
        with self.words_lock:
            return [w for w in self.words if w.t1 > t0 and w.t0 < t1]

    def stop_requested(self) -> bool:
        return os.path.exists(self.cfg["paths"]["stop_file"])

    # ------------------------------------------------------------------ workers
    def audio_worker(self) -> None:
        while not self.stop_flag.is_set():
            try:
                path, a, b = self.seg_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                pcm = segment_pcm(path)
                for s, db in per_second_db(pcm, a):
                    self.tl.audio_db[s] = db
                with self.pcm_lock:
                    self.pcm_chunks.append((a, pcm))
                self.audio_t = max(self.audio_t, a + len(pcm) / SR)
            except Exception as e:
                self.ev("error", f"audio failed on {Path(path).name}: {e}")

    def asr_worker(self) -> None:
        chunk = self.cfg["asr"]["chunk_seconds"]
        while not self.stop_flag.is_set():
            with self.pcm_lock:
                have = sum(len(p) for _, p in self.pcm_chunks) / SR
                if have < chunk or not self.pcm_chunks:
                    take = None
                else:
                    take = list(self.pcm_chunks)
                    self.pcm_chunks.clear()
            if take is None:
                if self.asr is None and self.pcm_chunks:
                    with self.pcm_lock:
                        last = self.pcm_chunks[-1]
                        self.asr_t = last[0] + len(last[1]) / SR
                        self.pcm_chunks.clear()
                time.sleep(0.5)
                continue
            t0 = take[0][0]
            pcm = np.concatenate([p for _, p in take])
            t_end = take[-1][0] + len(take[-1][1]) / SR
            if self.asr is None:
                self.asr_t = t_end
                continue
            try:
                st = time.time()
                ws = self.asr.transcribe(pcm, t0)
                with self.words_lock:
                    self.words.extend(ws)
                    self.words.sort(key=lambda w: w.t0)
                    cutoff = t_end - 6 * 3600
                    if self.words and self.words[0].t0 < cutoff:
                        self.words = [w for w in self.words if w.t0 >= cutoff]
                self.asr_t = t_end
                with open(self.data / "transcript.jsonl", "a", encoding="utf-8") as f:
                    for w in ws:
                        f.write(json.dumps({"t0": round(w.t0, 2), "t1": round(w.t1, 2), "w": w.raw, "r": w.roman},
                                           ensure_ascii=False) + "\n")
                log(f"asr {t0:.0f}-{t_end:.0f}s: {len(ws)} words in {time.time() - st:.1f}s")
            except Exception as e:
                self.asr_t = t_end
                self.ev("error", f"asr failed {t0:.0f}-{t_end:.0f}: {e}")

    # ------------------------------------------------------------------ detection
    def detect_step(self) -> None:
        d = self.cfg["detect"]
        upto = int(self.audio_t) - 1
        while self.det_t < upto:
            t = self.det_t + 1
            self.det_t = t
            rate, hype = self.tl.chat_rate(t, d["chat_window_s"])
            db_vals = [self.tl.audio_db.get(s) for s in range(t - 2, t + 1)]
            db_vals = [v for v in db_vals if v is not None]
            adb = float(np.mean(db_vals)) if db_vals else -60.0
            cz, hz, az = self.z_chat.push(rate), self.z_hype.push(hype), self.z_audio.push(adb)
            sc = combined_score(cz, hz, az, d["hype_weight"])
            pk = Peak(t, sc, cz, az)
            self.scores[t] = pk
            self.cur_sig = {"chat_rate": round(rate, 3), "chat_z": round(cz, 2), "audio_db": round(adb, 1),
                            "audio_z": round(az, 2)}
            if t % 2 == 0:
                self.out.signal(t, rate, cz, adb, az)
            if t < d["warmup_seconds"]:
                continue
            done = self.spike.update(pk)
            if done:
                chat_led = max(done.chat_z, 0) >= max(done.audio_z, 0)
                center = done.t - (d["chat_lag_s"] if chat_led else 0)
                done.kind = "chat" if chat_led else "audio"
                self.add_candidate(center, done)
            elif t - self.last_cand_t >= d["quiet_sample_every_s"]:
                lo, hi = max(int(self.last_cand_t + 30), d["warmup_seconds"]), t - d["window_after_s"]
                best = max((self.scores[s] for s in range(lo, hi) if s in self.scores),
                           key=lambda p: p.score, default=None)
                if best:
                    best = Peak(best.t, best.score, best.chat_z, best.audio_z, "quiet")
                    self.add_candidate(best.t, best)
                else:
                    self.last_cand_t = t
        for s in [s for s in self.scores if s < self.det_t - 4000]:
            del self.scores[s]

    def add_candidate(self, center: float, pk: Peak) -> None:
        d = self.cfg["detect"]
        self.last_cand_t = max(self.last_cand_t, pk.t)
        w0, w1 = max(0.0, center - d["window_before_s"]), center + d["window_after_s"]
        with self.pending_lock:
            return self._add_candidate(center, pk, w0, w1)

    def _add_candidate(self, center: float, pk: Peak, w0: float, w1: float) -> None:
        d = self.cfg["detect"]
        for c in self.pending:
            if abs(c["center"] - center) < d["min_gap_s"]:
                if pk.score > c["peak"].score:
                    c.update(center=center, peak=pk, w0=min(c["w0"], w0), w1=max(c["w1"], w1))
                return
        if overlaps_any(center - 10, center + 5, self.cut_ranges, pad=0):
            return
        self.cands_seen += 1
        c = {"center": center, "peak": pk, "w0": w0, "w1": w1, "found_wall": time.time()}
        self.pending.append(c)
        self.ev("candidate", f"{pk.kind} peak at {pk.t:.0f}s score {pk.score:.2f}",
                {"t": pk.t, "center": center, "score": round(pk.score, 2), "chat_z": round(pk.chat_z, 2),
                 "audio_z": round(pk.audio_z, 2), "kind": pk.kind})

    # ------------------------------------------------------------------ judging
    def judge_worker(self) -> None:
        """Own thread so a slow LLM call never stalls ingest/heartbeats."""
        while not self.stop_flag.is_set():
            with self.pending_lock:
                ready = [c for c in self.pending if self.asr_t >= c["w1"] and self.audio_t >= c["w1"]]
                ready.sort(key=lambda c: c["center"])
                c = ready[0] if ready else None
                if c:
                    self.pending.remove(c)
                    self.judging += 1
            if c is None:
                time.sleep(0.5)
                continue
            try:
                self.judge_one(c)
            except Exception as e:
                self.ev("error", f"judge failed: {e}")
            finally:
                self.judging -= 1

    def judge_one(self, c: dict) -> None:
        cfg, pk = self.cfg, c["peak"]
        w0, w1 = c["w0"], c["w1"]
        words = self.transcript(w0, w1)
        lines, cur, cur_t = [], [], None
        for w in words:
            if cur_t is None or w.t0 - cur_t > 4:
                if cur:
                    lines.append(f"[{cur_t:.0f}] " + " ".join(cur))
                cur, cur_t = [], w.t0
            cur.append(w.raw)
        if cur:
            lines.append(f"[{cur_t:.0f}] " + " ".join(cur))
        chat = self.tl.messages(w0, w1 + self.cfg["detect"]["chat_lag_s"])
        roman = " ".join(w.roman for w in words)
        kw = keyword_hits(" ".join(w.raw for w in words) + " " + roman)
        loud = sorted(((self.tl.audio_db.get(s, -99), s) for s in range(int(w0), int(w1))), reverse=True)[:4]
        busy = sorted(((self.tl.chat_rate(s, 5)[0], s) for s in range(int(w0), int(w1 + 15))), reverse=True)[:3]
        sig = (f"trigger={pk.kind}, combined={pk.score:.2f}, chat_z={pk.chat_z:.2f}, audio_z={pk.audio_z:.2f}, "
               f"chat msgs in window={len(chat)}, transcript keyword cues={kw}; loudest seconds "
               f"{', '.join(f'{s}s ({db:.0f} dBFS)' for db, s in sorted(loud, key=lambda x: x[1]))}; "
               f"chat bursts at {', '.join(f'{s}s' for _, s in sorted(busy, key=lambda x: x[1]))}")
        verdict, used_llm = None, False
        now = time.time()
        if cfg["judge"]["enabled"] and self.llm_budget.allow(now):
            self.llm_budget.take(now)
            self.llm_calls += 1
            used_llm = True
            prompt = J.build_prompt(cfg["source"]["streamer"], cfg["source"]["stream_title"], w0, w1, sig, lines, chat)
            txt = J.call_llm(cfg["judge"]["cmd"], prompt, cfg["judge"]["timeout_s"], str(self.data))
            if txt:
                verdict = J.parse_reply(txt, w0, w1, cfg["clips"]["min_len"], cfg["clips"]["max_len"])
            if verdict is None:
                self.llm_fail += 1
        if verdict is None:
            if pk.score < cfg["judge"]["fallback_min_signal"] and used_llm is False and pk.kind == "quiet":
                return
            verdict = J.fallback_judgement(pk.score, kw, w0, w1, c["center"], cfg["clips"]["min_len"],
                                           cfg["clips"]["max_len"], roman)
            verdict["fallback"] = True
        self.ev("judged", f"score {verdict['score']:.0f} [{verdict['start']:.0f}-{verdict['end']:.0f}] {verdict['reason']}",
                {"score": verdict["score"], "start": verdict["start"], "end": verdict["end"], "title": verdict["title"],
                 "llm": used_llm and not verdict.get("fallback"), "trigger": pk.kind})
        if verdict["score"] < cfg["judge"]["min_score"]:
            return
        if overlaps_any(verdict["start"], verdict["end"], self.cut_ranges, pad=-8):
            self.ev("info", f"skip overlap {verdict['start']:.0f}-{verdict['end']:.0f}")
            return
        if not self.clip_budget.allow(verdict["start"]):
            self.ev("info", f"clip budget reached for this hour; skipping score {verdict['score']:.0f}")
            return
        self.clip_budget.take(verdict["start"])
        self.cut_ranges.append((verdict["start"], verdict["end"]))
        self.render_q.put({"c": c, "v": verdict, "words": self.transcript(verdict["start"], verdict["end"]),
                           "kw": kw})

    # ------------------------------------------------------------------ rendering
    def ensure_layout(self) -> None:
        if self.layout is not None:
            return
        ov = self.cfg["render"].get("facecam_override")
        face = Box(*ov) if ov else None
        if face is None:
            segs = self.ingest.index.covering(0, 1e9)
            pick = segs[:: max(1, len(segs) // 12)][:12]
            frames = [f for f in (grab_frame(p, 3.0) for p, _, _ in pick) if f is not None]
            face = detect_face(frames)
        self.layout = plan_layout(face) if self.cfg["render"]["layout"] != "center" else plan_layout(None)
        atomic_write_json(self.data / "layout.json", {k: (v.as_list() if isinstance(v, Box) else v)
                                                       for k, v in self.layout.__dict__.items()})
        self.ev("info", f"layout {self.layout.kind}", {"face": face.as_list() if face else None})

    def render_worker(self) -> None:
        while not self.stop_flag.is_set():
            try:
                job = self.render_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.render_one(job)
            except Exception as e:
                self.ev("error", f"render failed: {e}")

    def take_lock(self) -> bool:
        lock = Path(self.cfg["paths"]["render_lock"])
        if self.last_render_s < self.cfg["render"]["lock_if_longer_than_s"]:
            return False
        for _ in range(600):
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"skynet-live-clipper pid {os.getpid()} {now_iso()}".encode())
                os.close(fd)
                return True
            except FileExistsError:
                if time.time() - lock.stat().st_mtime > 3600:
                    lock.unlink(missing_ok=True)
                time.sleep(1)
        return False

    def render_one(self, job: dict) -> None:
        self.ensure_layout()
        v, c = job["v"], job["c"]
        t0, t1 = v["start"], v["end"]
        segs = self.ingest.index.covering(t0 - 1, t1 + 1)
        if not segs:
            raise RuntimeError(f"no segments for {t0}-{t1}")
        cid = f"{self.sid}_{int(t0):06d}"
        work = self.data / "work" / cid
        st = time.time()
        locked = self.take_lock()
        try:
            r = render_clip(work, segs, t0, t1, self.layout, job["words"], v["title"], self.cfg["source"]["credit"],
                            self.cfg["clips"]["watermark"], self.cfg, self.encoder)
            # cover: loudest second after the hook
            secs = [(self.tl.audio_db.get(s, -99), s) for s in range(int(t0 + 3), int(t1 - 1))]
            best = max(secs)[1] if secs else t0 + 3
            cover = render_cover(work, best - r["base"], 0.0, self.layout, v["title"], self.cfg["source"]["credit"])
        finally:
            if locked:
                Path(self.cfg["paths"]["render_lock"]).unlink(missing_ok=True)
        self.last_render_s = time.time() - st
        lufs = measure_loudness(r["path"])
        dst = self.out.clip_dir(cid)
        shutil.copy(r["path"], dst / "clip.mp4")
        shutil.copy(cover, dst / "cover.jpg")
        pk = c["peak"]
        transcript = " ".join(w.roman for w in job["words"])
        reasons = [v["reason"], f"trigger: {pk.kind} spike (combined {pk.score:.2f})"]
        if pk.chat_z > 1.5:
            reasons.append(f"chat velocity z={pk.chat_z:.1f}")
        if pk.audio_z > 1.5:
            reasons.append(f"audio energy z={pk.audio_z:.1f}")
        if job["kw"]:
            reasons.append(f"{job['kw']} transcript cue words")
        if v.get("fallback"):
            reasons.append("deterministic fallback score (LLM unavailable)")
        meta = {"id": cid, "stream_url": self.cfg["source"]["url"], "streamer": self.cfg["source"]["streamer"],
                "stream_title": self.cfg["source"]["stream_title"], "t_start": t0, "t_end": t1,
                "duration": round(t1 - t0, 2), "created_at": now_iso(), "score": round(float(v["score"]), 1),
                "reasons": reasons,
                "signals": {"chat_z": round(pk.chat_z, 2), "audio_z": round(pk.audio_z, 2),
                            "llm_score": None if v.get("fallback") else round(float(v["score"]), 1)},
                "transcript": transcript, "title": v["title"], "caption": v["caption"],
                "hashtags": v["hashtags"], "status": "ready"}
        self.out.write_meta(cid, meta)
        moment_wall = self.ingest.epoch + t1
        latency = time.time() - moment_wall
        live_moment = moment_wall >= self.started_wall
        if live_moment:
            self.latencies.append(latency)
        atomic_write_json(dst / "render.json", {"encoder": r["encoder"], "render_seconds": round(self.last_render_s, 1),
                                                "lufs": lufs, "layout": self.layout.kind,
                                                "moment_to_clip_s": round(latency, 1) if live_moment else None,
                                                "judge_found_wall": c["found_wall"]})
        self.clips_ready += 1
        self.ev("cut", f"clip {cid} ready ({t1 - t0:.0f}s, score {v['score']:.0f}, {lufs} LUFS, "
                       f"render {self.last_render_s:.0f}s)", {"id": cid, "path": str(dst / "clip.mp4")})
        if self.cfg["clips"].get("copyright_gate") and COPYRIGHT.exists():
            threading.Thread(target=self.copyright_gate, args=(dst,), daemon=True).start()

    def copyright_gate(self, dst: Path) -> None:
        try:
            p = subprocess.run([sys.executable, str(COPYRIGHT), str(dst / "clip.mp4"), "--fast", "--windows", "4"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600,
                               creationflags=0x00004000 if os.name == "nt" else 0)
            out = p.stdout.decode("utf-8", "replace")
            (dst / "copyright.txt").write_text(out, encoding="utf-8")
            m = re.search(r"VERDICT:\s*(.+)", out)
            verdict = m.group(1).strip() if m else f"no verdict (rc={p.returncode})"
            tracks = re.findall(r"•\s*(.+?)\s+\(matched", out)
            mp = dst / "meta.json"
            meta = json.loads(mp.read_text(encoding="utf-8"))
            meta["reasons"] = [r for r in meta["reasons"] if not r.startswith("copyright gate:")] + \
                [f"copyright gate: {verdict}" + (f" ({'; '.join(tracks)})" if tracks else "")]
            self.out.write_meta(meta["id"], meta)
            self.ev("info", f"copyright gate {dst.name}: rc={p.returncode} {verdict}", {"tracks": tracks})
        except Exception as e:
            self.ev("error", f"copyright gate failed {dst.name}: {e}")

    # ------------------------------------------------------------------ supervisor
    def write_state(self) -> None:
        with self.words_lock:
            tail = " ".join(w.roman for w in self.words[-14:])
        live = self.ingest.alive() and (self.ingest.mode == "live" or self.ingest.file_growing())
        self.out.state({"stream_url": self.cfg["source"]["url"], "streamer": self.cfg["source"]["streamer"],
                        "stream_title": self.cfg["source"]["stream_title"], "live": bool(live),
                        "started_at": self.started_at, "captured_seconds": round(self.ingest.index.end, 1),
                        "chat_messages": self.chat_count, "candidates_seen": self.cands_seen,
                        "clips_ready": self.clips_ready, "last_heartbeat": now_iso(),
                        "current_signals": getattr(self, "cur_sig", {"chat_rate": 0, "chat_z": 0, "audio_db": -60,
                                                                     "audio_z": 0}),
                        "last_transcript_line": tail})
        atomic_write_json(self.stats_path, {"llm_calls": self.llm_calls, "llm_failures": self.llm_fail,
                                            "candidates_seen": self.cands_seen, "clips_ready": self.clips_ready,
                                            "chat_messages": self.chat_count, "captured_seconds": self.ingest.index.end,
                                            "moment_to_clip_latencies_s": [round(x, 1) for x in self.latencies],
                                            "light_mode": self.light, "encoder": self.encoder,
                                            "detected_upto_s": self.det_t, "asr_upto_s": round(self.asr_t, 1),
                                            "ingest_restarts": self.ingest.restarts, "updated": now_iso()})

    def still_live(self) -> bool:
        try:
            p = subprocess.run([YTDLP, "--no-warnings", "--print", "live_status", self.cfg["source"]["url"]],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
            st = p.stdout.decode().strip()
            log(f"live_status check: {st!r}")
            return st == "is_live"
        except Exception as e:
            log(f"live_status check failed: {e}")
            return False

    def check_light_mode(self) -> None:
        if self.light:
            return
        until = datetime.fromisoformat(self.cfg["safety"]["full_power_until"])
        if datetime.now(until.tzinfo) >= until:
            lm = self.cfg["safety"]["light_mode"]
            self.light = True
            self.encoder = lm["encoder"]
            self.clip_budget.per_hour = lm["max_per_hour"]
            if self.asr:
                with self.asr.lock:
                    self.asr.model_name, self.asr.device, self.asr.compute_type = lm["asr_model"], lm["asr_device"], "int8"
                    self.asr.model = None
            self.ev("info", "light mode engaged (full-power window over)", lm)

    def run(self) -> str:
        cfg = self.cfg
        if self.stop_requested():
            self.ev("info", "STOP file present at start; not running")
            return "stopped"
        self.ev("info", f"engine start: {cfg['source']['mode']} {cfg['source']['url']}", {"pid": os.getpid()})
        self.ingest.start()
        for fn in (self.audio_worker, self.asr_worker, self.judge_worker, self.render_worker):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
        steps, last_state, last_hb, backoff_i = 0, 0.0, 0.0, 0
        reason = "ended"
        max_wall = cfg["safety"]["max_wall_hours"] * 3600
        try:
            while True:
                steps += 1
                now = time.time()
                if self.stop_requested():
                    reason = "stopped"
                    self.ev("info", "STOP file found; halting cleanly")
                    break
                if steps > cfg["safety"]["max_steps"] or now - self.started_wall > max_wall:
                    reason = "cap"
                    self.ev("info", "step / wall-clock cap reached; halting")
                    break
                for item in self.ingest.poll_segments():
                    self.seg_q.put(item)
                for m in self.ingest.poll_chat():
                    self.chat_count += 1
                    self.tl.add_chat(m.ts - self.ingest.epoch, m.text, m.is_super)
                self.detect_step()
                if not self.ingest.alive() and not self.ingest.disk_stopped:
                    if self.ingest.mode == "file" and not self.ingest.file_growing():
                        pass
                    else:
                        self.ingest.restarts += 1
                        wait = cfg["ingest"]["restart_backoff_s"][min(backoff_i, len(cfg["ingest"]["restart_backoff_s"]) - 1)]
                        backoff_i += 1
                        self.ev("error", f"ingest died; restart #{self.ingest.restarts} in {wait}s")
                        time.sleep(wait)
                        self.ingest.start()
                elif self.ingest.alive() and now - self.ingest.last_growth < 30:
                    backoff_i = 0
                # live mode: no new segment for 3 min -> treat as ended only after restarts fail
                if self.ingest.mode == "file" and not self.ingest.file_growing() and \
                        now - self.ingest.last_growth > 60 and self.ingest.alive():
                    self.ingest.stop()
                    if self.still_live():
                        self.ev("info", "source file stopped growing but stream is still LIVE; switching to own capture")
                        self.switch_url = self.cfg["source"]["url"]
                        reason = "switch"
                        break
                    self.ev("info", "source file stopped growing and stream is not live; finishing pending work")
                if self.ingest.mode == "live" and self.ingest.restarts > 12 and now - self.ingest.last_growth > 600:
                    self.ev("info", "live capture gone for 10 min after 12 restarts; stream ended")
                    self.ingest.stop()
                    self.ingest.disk_stopped = True
                if steps % 30 == 0 and not self.ingest.disk_ok() and not self.ingest.disk_stopped:
                    self.ingest.stop()
                    self.ingest.disk_stopped = True
                    self.ev("error", "disk guard: free space below limit, capture stopped")
                if steps % 60 == 0:
                    keep = self.ingest.index.end - cfg["ingest"]["keep_segments_hours"] * 3600
                    if keep > 0:
                        n = self.ingest.index.prune(keep)
                        if n:
                            log(f"pruned {n} old segments")
                    self.check_light_mode()
                    cmds, self.cmd_offset = self.out.read_commands(self.cmd_offset)
                    for cmd in cmds:
                        if cmd.get("cmd") == "watch" and cmd.get("url"):
                            self.switch_url = cmd["url"]
                            self.ev("info", f"command: watch {cmd['url']}")
                    if self.switch_url:
                        reason = "switch"
                        break
                if now - last_state >= 5:
                    last_state = now
                    self.write_state()
                if now - last_hb >= 60:
                    last_hb = now
                    self.ev("heartbeat", f"t={self.ingest.index.end:.0f}s chat={self.chat_count} cands={self.cands_seen} "
                                         f"clips={self.clips_ready} llm={self.llm_calls} asr={self.asr_t:.0f}s",
                            {"captured_seconds": self.ingest.index.end})
                finished = (not self.ingest.alive()) and self.seg_q.empty() and self.ingest.mode == "file" and \
                    not self.ingest.file_growing()
                if finished and self.det_t >= int(self.audio_t) - 1 and self.asr_t >= self.audio_t - 1 and \
                        not self.pending and not self.judging and self.render_q.empty():
                    if now - self.ingest.last_growth > 120:
                        reason = "ended"
                        self.ev("info", "all captured material processed; stream ended")
                        break
                if finished and self.pending and self.asr_t >= self.audio_t - 1:
                    for c in self.pending:  # flush tail candidates
                        c["w1"] = min(c["w1"], self.audio_t)
                time.sleep(1)
        finally:
            self.ingest.stop()
            # let an in-flight render finish (bounded)
            t_end = time.time() + 600
            while (not self.render_q.empty()) and time.time() < t_end and reason != "stopped":
                time.sleep(2)
            self.stop_flag.set()
            self.write_state()
            self.ev("info", f"engine exit: {reason}")
        return reason
