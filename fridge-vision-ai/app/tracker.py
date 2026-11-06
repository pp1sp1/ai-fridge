"""Simple centroid Y-axis motion tracker for fridge door sequences.

Detections are matched across consecutive frames using centroid distance
and IoU. Each resulting track is classified along the Y axis:

* movement aligned with ``vector_direction``  -> ``INSERTED``
* movement opposed to ``vector_direction``    -> ``REMOVED``
* track shorter than 2 frames, or the absolute Y displacement below the
  motion threshold (a fraction of the frame height) -> ``AMBIGUOUS``

The tracker also renders the annotated best frame (boxes, labels and
motion vector arrows) used by the integration's image entity.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import logging
from typing import Sequence

import cv2
import numpy as np

LOGGER = logging.getLogger(__name__)

ACTION_INSERTED = "INSERTED"
ACTION_REMOVED = "REMOVED"
ACTION_AMBIGUOUS = "AMBIGUOUS"

VECTOR_TOP_TO_BOTTOM = "top_to_bottom"
VECTOR_BOTTOM_TO_TOP = "bottom_to_top"

# Matching / classification tuning constants.
IOU_MATCH_THRESHOLD = 0.2
CENTROID_DISTANCE_RATIO = 0.15
MAX_TRACK_GAP_FRAMES = 1
DEFAULT_MOTION_THRESHOLD = 0.05

# BGR colors used for annotation (INSERTED / REMOVED / AMBIGUOUS).
_COLOR_INSERTED = (60, 190, 75)
_COLOR_REMOVED = (60, 60, 225)
_COLOR_AMBIGUOUS = (40, 150, 240)
_ACTION_COLORS: dict[str, tuple[int, int, int]] = {
    ACTION_INSERTED: _COLOR_INSERTED,
    ACTION_REMOVED: _COLOR_REMOVED,
    ACTION_AMBIGUOUS: _COLOR_AMBIGUOUS,
}

BBox = tuple[float, float, float, float]
Centroid = tuple[float, float]


def clamp_point(point: Centroid, width: int, height: int) -> Centroid:
    """Clamp a point into the frame."""
    x = min(max(point[0], 0.0), float(max(0, width - 1)))
    y = min(max(point[1], 0.0), float(max(0, height - 1)))
    return (x, y)


def clamp_bbox(bbox: BBox, width: int, height: int) -> tuple[int, int, int, int]:
    """Clamp a box into the frame and round to integer pixels."""
    x1 = int(round(min(max(bbox[0], 0.0), float(max(0, width - 2)))))
    y1 = int(round(min(max(bbox[1], 0.0), float(max(0, height - 2)))))
    x2 = int(round(min(max(bbox[2], x1 + 1.0), float(width))))
    y2 = int(round(min(max(bbox[3], y1 + 1.0), float(height))))
    return (x1, y1, x2, y2)


@dataclass(slots=True)
class Detection:
    """A single detection in a single frame."""

    frame_index: int
    bbox: BBox
    prompt: str
    confidence: float

    @property
    def centroid(self) -> Centroid:
        """Center point of the bounding box."""
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


@dataclass(slots=True)
class Track:
    """A motion track connecting detections across frames."""

    track_id: int
    prompt: str
    detections: dict[int, Detection] = field(default_factory=dict)
    best_confidence: float = 0.0

    @property
    def first_frame(self) -> int:
        """First frame index of the track."""
        return min(self.detections)

    @property
    def last_frame(self) -> int:
        """Last frame index of the track."""
        return max(self.detections)

    @property
    def track_length_frames(self) -> int:
        """Number of frames the object was seen in."""
        return len(self.detections)

    @property
    def start_centroid(self) -> Centroid:
        """Centroid in the first frame."""
        return self.detections[self.first_frame].centroid

    @property
    def end_centroid(self) -> Centroid:
        """Centroid in the last frame."""
        return self.detections[self.last_frame].centroid


@dataclass(slots=True, frozen=True)
class TrackResult:
    """Classification result for one track, ready for the API response."""

    track_id: int
    ai_prompt: str
    action: str
    confidence: float
    track_length_frames: int
    delta_y: float
    best_frame_index: int
    bbox: BBox
    start_centroid: Centroid
    end_centroid: Centroid


def iou(box_a: BBox, box_b: BBox) -> float:
    """Compute the intersection-over-union of two boxes."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1, inter_y1 = max(ax1, bx1), max(ay1, by1)
    inter_x2, inter_y2 = min(ax2, bx2), min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    if inter_area <= 0.0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter_area
    return inter_area / union if union > 0.0 else 0.0


def centroid_distance(a: Centroid, b: Centroid) -> float:
    """Euclidean distance between two centroids."""
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


class CentroidTracker:
    """Greedy IoU + centroid matching across consecutive frames."""

    def __init__(
        self,
        iou_threshold: float = IOU_MATCH_THRESHOLD,
        distance_ratio: float = CENTROID_DISTANCE_RATIO,
        max_gap_frames: int = MAX_TRACK_GAP_FRAMES,
    ) -> None:
        self._iou_threshold = iou_threshold
        self._distance_ratio = distance_ratio
        self._max_gap_frames = max_gap_frames
        self._tracks: list[Track] = []
        self._next_track_id = 1

    @property
    def tracks(self) -> list[Track]:
        """All tracks created during the sequence."""
        return list(self._tracks)

    def update(
        self,
        frame_index: int,
        detections: Sequence[Detection],
        frame_width: int,
        frame_height: int,
    ) -> None:
        """Match detections to active tracks; unmatched ones open new tracks."""
        diag = float(np.hypot(frame_width, frame_height))
        max_distance = self._distance_ratio * diag

        active = [
            track
            for track in self._tracks
            if 0 < frame_index - track.last_frame <= self._max_gap_frames
        ]

        pairs: list[tuple[float, Track, Detection]] = []
        for track in active:
            last_det = track.detections[track.last_frame]
            for det in detections:
                dist = centroid_distance(last_det.centroid, det.centroid)
                if dist <= max_distance or iou(last_det.bbox, det.bbox) >= self._iou_threshold:
                    pairs.append((dist, track, det))
        pairs.sort(key=lambda item: item[0])

        used_tracks: set[int] = set()
        used_dets: set[int] = set()
        for _, track, det in pairs:
            if id(track) in used_tracks or id(det) in used_dets:
                continue
            used_tracks.add(id(track))
            used_dets.add(id(det))
            self._attach(track, det)

        for det in detections:
            if id(det) not in used_dets:
                track = Track(track_id=self._next_track_id, prompt=det.prompt)
                self._next_track_id += 1
                self._tracks.append(track)
                self._attach(track, det)

    def _attach(self, track: Track, det: Detection) -> None:
        track.detections[det.frame_index] = det
        track.best_confidence = max(track.best_confidence, det.confidence)


def classify_track(
    track: Track,
    vector_direction: str,
    motion_threshold_ratio: float,
    frame_height: int,
) -> tuple[str, float]:
    """Classify a track along the Y axis. Returns ``(action, delta_y)``."""
    if track.track_length_frames < 2:
        return ACTION_AMBIGUOUS, 0.0
    delta_y = track.end_centroid[1] - track.start_centroid[1]
    if abs(delta_y) < motion_threshold_ratio * float(frame_height):
        return ACTION_AMBIGUOUS, delta_y
    insert_sign = 1.0 if vector_direction == VECTOR_TOP_TO_BOTTOM else -1.0
    if delta_y * insert_sign > 0.0:
        return ACTION_INSERTED, delta_y
    return ACTION_REMOVED, delta_y


def analyze_tracks(
    tracks: Sequence[Track],
    vector_direction: str,
    motion_threshold_ratio: float,
    frame_height: int,
) -> tuple[list[TrackResult], int]:
    """Classify all tracks and pick the frame in which most tracks appear."""
    if not tracks:
        return [], 0
    ordered = sorted(tracks, key=lambda track: (track.first_frame, track.track_id))
    frame_hits: Counter[int] = Counter()
    for track in ordered:
        frame_hits.update(track.detections.keys())
    best_frame_index = min(frame_hits, key=lambda idx: (-frame_hits[idx], idx))

    results: list[TrackResult] = []
    for track in ordered:
        action, delta_y = classify_track(
            track, vector_direction, motion_threshold_ratio, frame_height
        )
        det_at_best = track.detections.get(best_frame_index)
        if det_at_best is None:
            det_at_best = max(
                track.detections.values(),
                key=lambda det: (det.confidence, det.frame_index),
            )
        results.append(
            TrackResult(
                track_id=track.track_id,
                ai_prompt=track.prompt,
                action=action,
                confidence=round(track.best_confidence, 4),
                track_length_frames=track.track_length_frames,
                delta_y=round(delta_y, 2),
                best_frame_index=det_at_best.frame_index,
                bbox=det_at_best.bbox,
                start_centroid=track.start_centroid,
                end_centroid=track.end_centroid,
            )
        )
    return results, best_frame_index


def _draw_label(canvas: np.ndarray, text: str, x: int, y: int, color: tuple[int, int, int]) -> None:
    """Draw a text label with a filled background for readability."""
    (text_w, text_h), baseline = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
    )
    top = max(y - text_h - baseline, 0)
    left = max(x, 0)
    cv2.rectangle(
        canvas,
        (left, top),
        (left + text_w + 6, top + text_h + baseline + 2),
        color,
        -1,
    )
    cv2.putText(
        canvas,
        text,
        (left + 3, top + text_h + 1),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (24, 24, 24),
        1,
        cv2.LINE_AA,
    )


def annotate_frame(frame: np.ndarray, results: Sequence[TrackResult]) -> np.ndarray:
    """Draw boxes, labels and motion vector arrows onto a copy of the frame."""
    height, width = frame.shape[:2]
    canvas = frame.copy()
    for result in results:
        color = _ACTION_COLORS.get(result.action, _COLOR_AMBIGUOUS)
        x1, y1, x2, y2 = clamp_bbox(result.bbox, width, height)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)
        _draw_label(
            canvas,
            f"{result.action} {result.ai_prompt} {result.confidence:.2f}",
            x1,
            y1,
            color,
        )
        start = clamp_point(result.start_centroid, width, height)
        end = clamp_point(result.end_centroid, width, height)
        start_px = (int(round(start[0])), int(round(start[1])))
        end_px = (int(round(end[0])), int(round(end[1])))
        if start_px != end_px:
            cv2.arrowedLine(
                canvas,
                start_px,
                end_px,
                color,
                2,
                tipLength=0.25,
            )
    return canvas


def encode_jpeg(frame: np.ndarray, quality: int = 90) -> bytes:
    """Encode a BGR frame as JPEG bytes."""
    ok, buffer = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    if not ok:
        raise ValueError("Could not encode frame to JPEG")
    return buffer.tobytes()


def decode_jpeg(data: bytes) -> np.ndarray | None:
    """Decode JPEG bytes into a BGR frame, or None when not decodable."""
    array = np.frombuffer(data, dtype=np.uint8)
    if array.size == 0:
        return None
    return cv2.imdecode(array, cv2.IMREAD_COLOR)
