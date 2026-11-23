"""Heavy lane: send a recorded stream chunk to a PRIVATE Kaggle kernel (2x T4) for whisper + clip rendering.

Offload order after the laptop's full-power window: Kaggle -> Colab -> VPS light profile.
No secrets go into the dataset or kernel: only the chunk, its chat json, and this repo's code (no config secrets
exist; the claude CLI judge is NOT available there, so the deterministic fallback scorer is used and the
laptop/dashboard can re-judge later).

  py -3.11 -m skynet_clipper.kaggle_lane pack  CHUNK.mkv [CHAT.json] [--start-epoch E]  -> C:/clipper-data/kaggle/<slug>/
  py -3.11 -m skynet_clipper.kaggle_lane push  <slug>      (dataset create + kernel push, both private)
  py -3.11 -m skynet_clipper.kaggle_lane pull  <slug>      (status; when COMPLETE, output -> C:/clipper-data/kaggle/<slug>/result)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path("C:/clipper-data/kaggle")
USER = "skynetlabs"
KAGGLE = [sys.executable, "-m", "kaggle"]

KERNEL = r'''
import glob, os, subprocess, sys, tarfile, yaml
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "faster-whisper", "pyyaml", "opencv-python-headless"], check=True)
src = glob.glob("/kaggle/input/**/code.tgz", recursive=True)[0]
os.makedirs("/kaggle/working/app", exist_ok=True)
tarfile.open(src).extractall("/kaggle/working/app")
chunk = [p for p in glob.glob("/kaggle/input/**/*", recursive=True) if p.endswith((".mkv", ".mp4", ".ts"))][0]
chat = (glob.glob("/kaggle/input/**/*.live_chat.json", recursive=True) or [""])[0]
cfg = yaml.safe_load(open("/kaggle/working/app/config.yaml"))
cfg["source"].update(mode="file", file=chunk, chat_file=chat, start_epoch=__EPOCH__, stall_end_seconds=1)
cfg["paths"].update(data_root="/kaggle/working/data", out_root="/kaggle/working/out",
                    render_lock="/kaggle/working/RENDER.lock", stop_file="/kaggle/working/STOP")
cfg["asr"].update(model="large-v3", device="cuda", compute_type="float16")
cfg["judge"]["enabled"] = False
cfg["clips"]["copyright_gate"] = False
cfg["render"]["encoder"] = "libx264"
cfg["safety"]["full_power_until"] = "2099-01-01T00:00:00+00:00"
yaml.safe_dump(cfg, open("/kaggle/working/app/kaggle.yaml", "w"))
subprocess.run([sys.executable, "run.py", "--config", "kaggle.yaml"], cwd="/kaggle/working/app", check=False)
subprocess.run("rm -rf /kaggle/working/data /kaggle/working/app", shell=True)
'''


def pack(chunk: str, chat: str | None, epoch: float) -> Path:
    slug = "clip-" + Path(chunk).stem.lower().replace("_", "-")[:40] + "-" + time.strftime("%m%d%H%M")
    d = BASE / slug
    ds, kn = d / "dataset", d / "kernel"
    ds.mkdir(parents=True, exist_ok=True)
    kn.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ds / "code.tgz", "w:gz") as t:
        for p in ["run.py", "config.yaml", "requirements.txt", "skynet_clipper"]:
            t.add(ROOT / p, arcname=p, filter=lambda ti: None if "__pycache__" in ti.name else ti)
    shutil.copy(chunk, ds / Path(chunk).name)
    if chat:
        shutil.copy(chat, ds / (Path(chat).stem.split(".")[0] + ".live_chat.json"))
    (ds / "dataset-metadata.json").write_text(json.dumps(
        {"title": slug + "-data", "id": f"{USER}/{slug}-data", "licenses": [{"name": "CC0-1.0"}]}, indent=1))
    (kn / "script.py").write_text(KERNEL.replace("__EPOCH__", repr(float(epoch))), encoding="utf-8")
    (kn / "kernel-metadata.json").write_text(json.dumps(
        {"id": f"{USER}/{slug}", "title": slug, "code_file": "script.py", "language": "python",
         "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_internet": True,
         "dataset_sources": [f"{USER}/{slug}-data"], "competition_sources": [], "kernel_sources": []}, indent=1))
    print(f"packed {d}")
    return d


def push(slug: str) -> None:
    d = BASE / slug
    subprocess.run(KAGGLE + ["datasets", "create", "-p", str(d / "dataset"), "--dir-mode", "skip"], check=True)
    # private by default (no --public); wait for the dataset to be ready before the kernel mounts it
    for _ in range(60):
        st = subprocess.run(KAGGLE + ["datasets", "status", f"{USER}/{slug}-data"], stdout=subprocess.PIPE).stdout
        if b"ready" in st.lower():
            break
        time.sleep(10)
    subprocess.run(KAGGLE + ["kernels", "push", "-p", str(d / "kernel"), "--accelerator", "NvidiaTeslaT4"], check=True)


def pull(slug: str) -> None:
    st = subprocess.run(KAGGLE + ["kernels", "status", f"{USER}/{slug}"], stdout=subprocess.PIPE).stdout.decode()
    print(st.strip())
    if "COMPLETE" in st.upper():
        out = BASE / slug / "result"
        out.mkdir(exist_ok=True)
        subprocess.run(KAGGLE + ["kernels", "output", f"{USER}/{slug}", "-p", str(out)], check=True)
        print(f"output -> {out} (copy result/out/clips/* into C:/clipper-data/out/clips to show them on the dashboard)")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "pack":
        args = sys.argv[2:]
        ep = 0.0
        if "--start-epoch" in args:
            i = args.index("--start-epoch")
            ep = float(args[i + 1])
            del args[i:i + 2]
        pack(args[0], args[1] if len(args) > 1 else None, ep)
    elif cmd == "push":
        push(sys.argv[2])
    elif cmd == "pull":
        pull(sys.argv[2])
