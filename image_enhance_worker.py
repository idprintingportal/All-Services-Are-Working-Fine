"""Private photo enhancement worker for passport/4x6/name-date photos.

This deliberately uses conservative, identity-preserving processing. It does not
invent facial details or perform face replacement/restoration.
"""
import base64, io, os
from flask import Flask, jsonify, request
from PIL import Image, ImageOps
import cv2
import numpy as np

app = Flask(__name__)
MAX_BYTES = int(os.getenv("MAX_IMAGE_BYTES", "12000000"))
API_KEY = os.getenv("ENHANCE_API_KEY", "")

def _authorized():
    return bool(API_KEY) and request.headers.get("X-Enhance-Key", "") == API_KEY

def _process(raw: bytes, level: int) -> bytes:
    src = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    bgr = cv2.cvtColor(np.asarray(src), cv2.COLOR_RGB2BGR)
    # Gentle denoise, local contrast and unsharp mask. No facial synthesis.
    strength = max(0.0, min(1.0, level / 100.0))
    if strength:
        bgr = cv2.fastNlMeansDenoisingColored(bgr, None, 2 + int(3 * strength), 2 + int(3 * strength), 7, 21)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.0 + 0.7 * strength, tileGridSize=(8, 8))
        lab = cv2.merge((clahe.apply(l), a, b))
        bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        blur = cv2.GaussianBlur(bgr, (0, 0), 1.0)
        bgr = cv2.addWeighted(bgr, 1.0 + 0.35 * strength, blur, -0.35 * strength, 0)
    out = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    buf = io.BytesIO()
    Image.fromarray(out).save(buf, format="JPEG", quality=96, optimize=True)
    return buf.getvalue()

@app.get("/health")
def health():
    return jsonify(success=True, service="image-enhance", mode="conservative")

@app.post("/enhance")
def enhance():
    if not _authorized():
        return jsonify(success=False, error="Unauthorized"), 401
    raw = request.files.get("image")
    if not raw or not raw.filename:
        return jsonify(success=False, error="image file is required"), 400
    data = raw.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return jsonify(success=False, error="image is too large"), 413
    try:
        level = int(request.form.get("level", "35"))
        result = _process(data, level)
        return jsonify(success=True, imageBase64=base64.b64encode(result).decode("ascii"), mimeType="image/jpeg")
    except Exception:
        app.logger.exception("enhancement failed")
        return jsonify(success=False, error="Image enhancement failed"), 422

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
