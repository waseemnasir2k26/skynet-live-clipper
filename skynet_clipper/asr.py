"""Audio: per-second loudness from each new segment + rolling faster-whisper transcription."""
from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

from .translit import to_roman
from .util import BELOW_NORMAL, FFMPEG, log

SR = 16000


def segment_pcm(path: str) -> np.ndarray:
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
                        "-f", "s16le", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=BELOW_NORMAL, timeout=120)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def per_second_db(pcm: np.ndarray, t0: float) -> list[tuple[int, float]]:
    """dBFS RMS for each whole second of stream time covered by pcm starting at t0.
    Uses the loudest 250 ms inside each second so short shouts/laughs register."""
    out = []
    first = int(np.ceil(t0))
    off = int(round((first - t0) * SR))
    q = SR // 4
    n_sec = (len(pcm) - off) // SR
    for i in range(n_sec):
        sec = pcm[off + i * SR: off + (i + 1) * SR]
        rms = [float(np.sqrt(np.mean(sec[j:j + q] ** 2)) + 1e-9) for j in range(0, SR, q)]
        out.append((first + i, 20 * np.log10(max(rms))))
    return out


@dataclass
class Word:
    t0: float
    t1: float
    raw: str
    roman: str


def _add_cuda_dlls() -> None:
    sp = os.path.join(os.path.dirname(os.__file__), "site-packages", "nvidia")
    for d in ("cublas", "cudnn", "cuda_nvrtc"):
        p = os.path.join(sp, d, "bin")
        if os.path.isdir(p):
            os.add_dll_directory(p)
            os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]


class Transcriber:
    def __init__(self, model: str, device: str, compute_type: str, language: str, beam: int):
        self.model_name, self.device, self.compute_type = model, device, compute_type
        self.language, self.beam = language, beam
        self.model = None
        self.lock = threading.Lock()

    def load(self) -> None:
        if os.name == "nt" and self.device == "cuda":
            _add_cuda_dlls()
        from faster_whisper import WhisperModel
        try:
            self.model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type,
                                      local_files_only=True)
        except Exception:
            self.model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        log(f"whisper {self.model_name} loaded on {self.device}")

    def transcribe(self, pcm: np.ndarray, t0: float) -> list[Word]:
        with self.lock:
            if self.model is None:
                self.load()
            try:
                segs, _ = self.model.transcribe(pcm, language=self.language, vad_filter=True, word_timestamps=True,
                                                beam_size=self.beam, condition_on_previous_text=False,
                                                no_speech_threshold=0.5, temperature=0.0)
                segs = list(segs)
            except Exception as e:  # CUDA DLL failures surface here -> fall back to CPU int8
                if self.device != "cuda":
                    raise
                log(f"whisper cuda failed ({e}); falling back to cpu int8")
                self.device, self.compute_type, self.model = "cpu", "int8", None
                self.load()
                segs = list(self.model.transcribe(pcm, language=self.language, vad_filter=True,
                                                  word_timestamps=True, beam_size=1,
                                                  condition_on_previous_text=False)[0])
        words: list[Word] = []
        for s in segs:
            for w in (s.words or []):
                raw = w.word.strip()
                if not raw:
                    continue
                words.append(Word(t0 + w.start, t0 + w.end, raw, to_roman(raw)))
        return _drop_repeats(words)


def _drop_repeats(words: list[Word], max_rep: int = 3) -> list[Word]:
    """Whisper hallucination guard: cut runs of the same word repeated more than max_rep times."""
    out: list[Word] = []
    run = 0
    for w in words:
        if out and w.raw == out[-1].raw:
            run += 1
            if run >= max_rep:
                continue
        else:
            run = 0
        out.append(w)
    return out
