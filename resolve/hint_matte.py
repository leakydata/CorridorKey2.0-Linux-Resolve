#!/usr/bin/env python3
"""Automatic person matte for CorridorKey's hint input (footage without a green screen).

Runs in the CorridorKey venv (torch + torchvision from the install step, opencv from requirements.txt). Segments every
frame of a video with torchvision's DeepLabV3-ResNet101 (COCO, "person" class) and writes one grayscale PNG per frame
(white = person). The mattes are rough on purpose: CorridorKey refines the edges and only uses them to decide *which*
subject to key (see "Hint Quality" in the upstream README).

    venv/bin/python resolve/hint_matte.py --input clip.mov --output_dir hint_frames   # prints a JSON summary last
    venv/bin/python resolve/hint_matte.py --download                                  # fetch the model only

The model (about 233 MB, torchvision, BSD-3-Clause; trained on a COCO subset) is downloaded once from
download.pytorch.org into <checkout>/weights/torch/ and is never redistributed with this repository.
"""
import argparse
import json
import os
import subprocess
import sys
import time

CK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSON = 15  # Pascal VOC label index of "person" in the COCO_WITH_VOC_LABELS weights


def load_model(device):
    os.environ.setdefault("TORCH_HOME", os.path.join(CK_DIR, "weights", "torch"))
    import torch
    from torchvision.models.segmentation import DeepLabV3_ResNet101_Weights, deeplabv3_resnet101
    model = deeplabv3_resnet101(weights=DeepLabV3_ResNet101_Weights.COCO_WITH_VOC_LABELS_V1)
    model = model.eval().to(device)
    return model.half() if device == "cuda" else model


def probe_size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input")
    ap.add_argument("--output_dir")
    ap.add_argument("--download", action="store_true", help="only fetch the model weights")
    ap.add_argument("--scale", type=float, default=2 / 3, help="segmentation resolution relative to the video")
    a = ap.parse_args()

    import torch
    device = "cuda" if torch.cuda.is_available() and not a.download else "cpu"
    t0 = time.time()
    model = load_model(device)
    if a.download:
        print(json.dumps({"ok": True, "model": "deeplabv3_resnet101", "seconds": round(time.time() - t0, 1)}))
        return
    if not a.input or not a.output_dir:
        ap.error("--input and --output_dir are required")

    import cv2
    import numpy as np
    os.makedirs(a.output_dir, exist_ok=True)
    w, h = probe_size(a.input)
    sw, sh = max(8, int(w * a.scale) // 8 * 8), max(8, int(h * a.scale) // 8 * 8)
    mean = torch.tensor([0.485, 0.456, 0.406], device=device).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225], device=device).view(1, 3, 1, 1)
    dt = torch.float16 if device == "cuda" else torch.float32

    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", a.input, "-map", "0:v:0", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    coverage, n, t1 = [], 0, time.time()
    while True:
        buf = p.stdout.read(w * h * 3)
        if len(buf) < w * h * 3:
            break
        x = torch.frombuffer(bytearray(buf), dtype=torch.uint8).view(h, w, 3).to(device)
        x = x.permute(2, 0, 1).float().unsqueeze(0) / 255
        x = torch.nn.functional.interpolate(x, size=(sh, sw), mode="bilinear", align_corners=False)
        with torch.no_grad():
            prob = model(((x - mean) / std).to(dt))["out"].float().softmax(1)[:, PERSON:PERSON + 1]
        m = torch.nn.functional.interpolate(prob, size=(h, w), mode="bilinear", align_corners=False)[0, 0].clamp(0, 1)
        m8 = (m.cpu().numpy() * 255).astype(np.uint8)
        cv2.imwrite(os.path.join(a.output_dir, "m_%06d.png" % n), m8)
        coverage.append(float((m8 > 127).mean()))
        n += 1
    p.wait()
    if n == 0:
        sys.exit("no frames decoded from %s" % a.input)
    print(json.dumps({"ok": True, "frames": n, "median_coverage": float(np.median(coverage)),
                      "min_coverage": float(min(coverage)), "seconds": round(time.time() - t1, 1)}))


if __name__ == "__main__":
    main()
