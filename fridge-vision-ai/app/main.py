"""FastAPI service for the Fridge Vision AI add-on."""

from __future__ import annotations

import base64
import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .model import YOLOWorldEngine
from .tracker import (
    Detection,
    Track,
    compute_centroid,
    evaluate_action,
    match_tracks,
    MOTION_THRESHOLD_PX,
)

app = FastAPI(title="Fridge Vision AI")

engine = YOLOWorldEngine()

MAX_CENTROID_DISTANCE = 60.0
MIN_IOU = 0.3


class SetClassesRequest(BaseModel):
    classes: list[str] = Field(..., min_length=1)


class AnalyzeSequenceRequest(BaseModel):
    frames: list[str] = Field(..., min_length=1)
    vector_direction: str = Field("top_to_bottom")
    confidence_threshold: float = Field(0.25, ge=0.0, le=1.0)


class DetectionOut(BaseModel):
    ai_prompt: str
    action: str  # "INSERTED" | "REMOVED" | "AMBIGUOUS"
    confidence: float
    track_length_frames: int


class AnalyzeSequenceResponse(BaseModel):
    detections: list[DetectionOut]
    annotated_image_base64: str


def _decode_frame(b64: str) -> np.ndarray:
    """Decode a base64 JPEG string (with or without a data-URL prefix)."""
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    raw = base64.b64decode(b64)
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Failed to decode frame")
    return image


def _encode_image(image: np.ndarray) -> str:
    """Encode an image as a base64 JPEG string."""
    ok, buffer = cv2.imencode(
        ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
    )
    if not ok:
        raise RuntimeError("Failed to encode annotated image")
    return base64.b64encode(buffer).decode("ascii")


def _draw_annotations(
    image: np.ndarray,
    tracks: List[Track],
    actions: dict,
    vector_direction: str,
) -> np.ndarray:
    """Draw bounding boxes, labels and motion arrows onto a copy of the image."""
    annotated = image.copy()
    height, width = annotated.shape[:2]
    scale = max(1.0, min(width, height) / 500.0)

    action_colors = {
        "INSERTED": (0, 200, 0),    # green
        "REMOVED": (0, 0, 200),     # red
        "AMBIGUOUS": (0, 200, 200), # yellow
    }

    for track in tracks:
        if not track.detections:
            continue
        last = track.detections[-1]
        x1, y1, x2, y2 = (int(v) for v in last.bbox)

        eval_result = actions.get(id(track))
        color = (128, 128, 128)
        label = f"{track.label} {last.confidence:.2f}"
        if eval_result is not None:
            action = eval_result["action"]
            color = action_colors.get(action, (128, 128, 128))
            label = (
                f"{track.label} [{action}] {eval_result['confidence']:.2f}"
            )

        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            annotated,
            label,
            (x1, max(0, y1 - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55 * scale,
            color,
            2,
            cv2.LINE_AA,
        )

        # Motion arrow from the first to the last centroid of the track.
        if len(track.detections) >= 2:
            first_c = track.detections[0].centroid
            last_c = track.detections[-1].centroid
            p1 = (int(first_c[0]), int(first_c[1]))
            p2 = (int(last_c[0]), int(last_c[1]))
            cv2.arrowedLine(annotated, p1, p2, color, 2, cv2.LINE_AA)
            cv2.circle(annotated, p1, 5, (255, 255, 0), -1)
            cv2.circle(annotated, p2, 5, color, -1)

    return annotated


@app.on_event("startup")
def _startup() -> None:
    engine.load()


@app.post("/api/set_classes", response_model=dict)
async def set_classes(request: SetClassesRequest) -> dict:
    """Re-parametrize the YOLO-World CLIP text encoder with new prompts."""
    try:
        engine.set_classes(request.classes)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "classes": engine.classes()}


@app.post("/api/analyze_sequence", response_model=AnalyzeSequenceResponse)
async def analyze_sequence(request: AnalyzeSequenceRequest) -> AnalyzeSequenceResponse:
    """Analyze a sequence of frames and classify insert/remove motion."""
    if request.vector_direction not in (
        "top_to_bottom",
        "bottom_to_top",
    ):
        raise HTTPException(
            status_code=400,
            detail="vector_direction must be 'top_to_bottom' or 'bottom_to_top'",
        )

    frames: list[np.ndarray] = []
    for b64 in request.frames:
        try:
            frames.append(_decode_frame(b64))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Run YOLO-World inference on every frame.
    per_frame_detections: list[list[Detection]] = []
    for idx, frame in enumerate(frames):
        try:
            results = engine.predict(frame, conf=request.confidence_threshold)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        detections: list[Detection] = []
        for result in results:
            boxes = result.boxes
            if boxes is None:
                continue
            for box in boxes:
                cls_id = int(box.cls[0])
                label = result.names[cls_id]
                confidence = float(box.conf[0])
                x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
                centroid = compute_centroid((x1, y1, x2, y2))
                detections.append(
                    Detection(
                        label=label,
                        confidence=confidence,
                        bbox=(x1, y1, x2, y2),
                        centroid=centroid,
                        frame_index=idx,
                    )
                )
        per_frame_detections.append(detections)

    # Track objects across consecutive frames.
    tracks: List[Track] = []
    active: List[Track] = []
    for detections in per_frame_detections:
        matched_tracks, matched_detections, unmatched = match_tracks(
            active, detections, MAX_CENTROID_DISTANCE, MIN_IOU
        )
        matched_set = set(matched_tracks)

        # Extend matched tracks with their new detections.
        for ti, di in zip(matched_tracks, matched_detections):
            active[ti].detections.append(detections[di])

        # Tracks not matched this frame are completed.
        still_active: List[Track] = []
        for ti, track in enumerate(active):
            if ti in matched_set:
                still_active.append(track)
            else:
                tracks.append(track)

        # Unmatched detections start new tracks.
        for di in unmatched:
            still_active.append(
                Track(label=detections[di].label, detections=[detections[di]])
            )
        active = still_active

    # Tracks still open at the end of the sequence are completed now.
    tracks.extend(active)

    # Evaluate every completed track.
    actions: dict = {}
    for track in tracks:
        result = evaluate_action(track, request.vector_direction, MOTION_THRESHOLD_PX)
        if result is not None:
            actions[id(track)] = result

    # Annotate the last frame of the sequence.
    best_frame = frames[len(frames) - 1]
    annotated = _draw_annotations(
        best_frame, tracks, actions, request.vector_direction
    )

    detection_out = [
        DetectionOut(
            ai_prompt=result["ai_prompt"],
            action=result["action"],
            confidence=result["confidence"],
            track_length_frames=result["track_length_frames"],
        )
        for result in actions.values()
    ]

    return AnalyzeSequenceResponse(
        detections=detection_out,
        annotated_image_base64=_encode_image(annotated),
    )
