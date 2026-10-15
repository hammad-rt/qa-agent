"""Local reCAPTCHA tile classifier (YOLO11x-cls fine-tuned at 224 px, ONNX, CPU). Model:
huggingface.co/1simo/recaptcha-classification-57k-224 saved as models/recaptcha_cls_224.onnx (CAPTCHA_TILE_MODEL
overrides). Returns None whenever it can't answer (no model, no onnxruntime, 4x4 grid, unknown object) so the
caller asks the vision LLM instead."""
from __future__ import annotations

import base64
import io
import os
import re
from pathlib import Path

from .config import ROOT

CLASSES = ["Bicycle", "Bridge", "Bus", "Car", "Chimney", "Crosswalk", "Hydrant", "Motorcycle", "Mountain",
           "Other", "Palm", "Stair", "Tractor", "Traffic Light"]
WORDS = [(r"bicycle", "Bicycle"), (r"bridge", "Bridge"), (r"\bbus", "Bus"), (r"\bcars?\b", "Car"),
         (r"chimney", "Chimney"), (r"crosswalk", "Crosswalk"), (r"hydrant", "Hydrant"),
         (r"motorcycle", "Motorcycle"), (r"mountain|hill", "Mountain"), (r"palm", "Palm"),
         (r"stair", "Stair"), (r"tractor", "Tractor"), (r"traffic light", "Traffic Light")]
_session = None


def target(desc: str) -> str | None:
    d = (desc or "").lower()
    for pat, cls in WORDS:
        if re.search(pat, d):
            return cls
    return None


def model_path() -> Path:
    return Path(os.environ.get("CAPTCHA_TILE_MODEL", str(ROOT / "models" / "recaptcha_cls_224.onnx")))


def _load():
    global _session
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession(str(model_path()), providers=["CPUExecutionProvider"])
    return _session


def _prep(img):
    import numpy as np
    w, h = img.size
    s = 224 / min(w, h)
    img = img.convert("RGB").resize((max(224, round(w * s)), max(224, round(h * s))), 2)   # bilinear
    w, h = img.size
    l, t = (w - 224) // 2, (h - 224) // 2
    a = np.asarray(img.crop((l, t, l + 224, t + 224)), dtype="float32") / 255.0
    return a.transpose(2, 0, 1)


def classify(image_b64: str, tiles: int, desc: str, threshold: float | None = None) -> list[int] | None:
    cls = target(desc)
    if tiles != 9 or not cls or os.environ.get("CAPTCHA_TILE_MODEL_OFF") == "1" or not model_path().exists():
        return None
    try:
        import numpy as np
        from PIL import Image
        sess = _load()
    except Exception:  # noqa: BLE001
        return None
    threshold = threshold if threshold is not None else float(os.environ.get("CAPTCHA_TILE_THRESHOLD", "0.5"))
    img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
    W, H = img.size
    batch = np.stack([_prep(img.crop((c * W // 3, r * H // 3, (c + 1) * W // 3, (r + 1) * H // 3)))
                      for r in range(3) for c in range(3)])
    probs = sess.run(None, {sess.get_inputs()[0].name: batch})[0]
    k = CLASSES.index(cls)
    return [i + 1 for i, p in enumerate(probs) if float(p[k]) >= threshold]
