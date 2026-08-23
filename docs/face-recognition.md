# Face Recognition Guide

Face recognition identifies **which enrolled person** a detected face belongs
to. It builds on face *detection* (see [ai-detection.md](ai-detection.md) - the
`YOLO11 · Face` models): detection finds *where* faces are, recognition decides
*who* they are, and only for people an admin has explicitly enrolled.

> Recognition is **admin-only** and **off by default**. Nothing runs until an
> admin downloads an embedding model, enables recognition, and enrols at least
> one person. Face embeddings are biometric data - enrolment and identity
> management are restricted to admins and every change is written to the audit
> log.

---

## How it works

1. A **face-detection** model finds faces in the frame (Stage 1).
2. Each face is cropped and passed to an **embedding model**, which turns it into
   a 512-number vector (a "faceprint").
3. The vector is compared by cosine similarity against the vectors of enrolled
   people. The closest match above the **match threshold** wins; anything below
   is **unknown**.

Embeddings are only ever compared within a single embedding model: each stored
vector records the `model_id` it was produced with, so changing models never
matches against incompatible vectors.

---

## Setup

### 1. Download an embedding model

From the face-recognition settings, download one of the bundled models:

| Model | Size | Notes |
| --- | --- | --- |
| **ArcFace R100** | ~249 MB | High accuracy. Recommended for most hosts. |
| **ArcFace R100 · INT8** | ~63 MB | ~4× smaller/faster, slightly lower accuracy. Good for low-power CPU hosts. |

Both output 512-d embeddings from a 112×112 aligned face crop and share the same
`model_id`, so switching precision does not invalidate existing enrolments.

### 2. Enable recognition and tune

- **Enable** face recognition (requires a downloaded model).
- **Match threshold** - cosine-similarity acceptance. Higher = stricter (fewer
  false matches, more "unknown"); start around `0.5` and tune.
- **Alert on unknown** - treat a detected face that matches nobody as an
  alertable "stranger".
- **Minimum face size** - ignore faces smaller than N pixels on their shorter
  side before embedding (tiny/distant faces embed poorly).
- **Retention** - days to keep recognised-identity data on events (`0` = keep
  indefinitely).

### 3. Enrol people

Create a person and enrol one or more face images for them. Multiple varied
images per person (different angles/lighting) improve recognition. Enrolments
take effect immediately.

---

## Model licensing / attribution

The bundled embedding models are **ArcFace ResNet100** from the
[ONNX Model Zoo](https://github.com/onnx/models), distributed under the
**Apache License 2.0**, which permits commercial use.

```
ArcFace ResNet100 (arcfaceresnet100-8 / arcfaceresnet100-11-int8)
Source: ONNX Model Zoo - https://github.com/onnx/models
License: Apache License 2.0
```

The model files are downloaded on demand from the ONNX Model Zoo release; they
are not redistributed inside this repository. You may also supply your own
ArcFace-style ONNX embedding model (112×112 input, L2-normalisable output) by
placing it in `models/` and selecting it as the model path.

---

## Privacy notes

- Enrolment, viewing identities, and deletion are **admin-only**.
- Deleting a person removes **all** of their stored face embeddings.
- Recognised-identity data on events is retained per the **retention** setting.
- Face embeddings are biometric data; operating this feature may carry legal
  obligations (e.g. GDPR/BIPA-style rules) depending on your jurisdiction.
