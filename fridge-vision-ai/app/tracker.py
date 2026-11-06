"""Centroid Y-axis motion tracker for fridge inventory frame sequences."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Detection:
    """A single object detection in a single frame."""

    label: str
    confidence: float
    bbox: Tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels
    centroid: Tuple[float, float]
    frame_index: int


# Motion classification threshold (pixels). |delta_y| below this is AMBIGUOUS.
MOTION_THRESHOLD_PX = 15.0


@dataclass
class Track:
    """A single object tracked across consecutive frames."""

    label: str
    detections: List[Detection] = field(default_factory=list)


def compute_centroid(
    bbox: Tuple[float, float, float, float],
) -> Tuple[float, float]:
    """Return the (x, y) centroid of a bounding box."""
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def bbox_iou(
    bbox_a: Tuple[float, float, float, float],
    bbox_b: Tuple[float, float, float, float],
) -> float:
    """Intersection-over-union of two bounding boxes."""
    ax1, ay1, ax2, ay2 = bbox_a
    bx1, by1, bx2, by2 = bbox_b

    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)

    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h

    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter_area

    if union <= 0.0:
        return 0.0
    return inter_area / union


def centroid_distance(
    a: Tuple[float, float], b: Tuple[float, float]
) -> float:
    """Euclidean distance between two centroids."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def match_tracks(
    tracks: List[Track],
    detections: List[Detection],
    max_centroid_distance: float = 60.0,
    min_iou: float = 0.3,
) -> Tuple[List[int], List[int], List[int]]:
    """Greedily match detections to existing tracks.

    Only detections and tracks with the same label are matched. A match
    requires the centroid distance to be below ``max_centroid_distance`` and
    the IoU to be at least ``min_iou``.

    Returns:
        (matched_track_indices, matched_detection_indices, unmatched_detection_indices)
    """
    used_tracks: set[int] = set()
    used_detections: set[int] = set()
    matched_tracks: List[int] = []
    matched_detections: List[int] = []

    candidates: List[Tuple[float, int, int]] = []
    for ti, track in enumerate(tracks):
        if not track.detections:
            continue
        last_detection = track.detections[-1]
        for di, detection in enumerate(detections):
            if track.label != detection.label:
                continue
            distance = centroid_distance(
                last_detection.centroid, detection.centroid
            )
            if distance > max_centroid_distance:
                continue
            iou_value = bbox_iou(last_detection.bbox, detection.bbox)
            if iou_value < min_iou:
                continue
            score = iou_value - (distance / max_centroid_distance)
            candidates.append((score, ti, di))

    # Best score first so each track/detection is used at most once.
    candidates.sort(reverse=True, key=lambda candidate: candidate[0])

    for _, ti, di in candidates:
        if ti in used_tracks or di in used_detections:
            continue
        used_tracks.add(ti)
        used_detections.add(di)
        matched_tracks.append(ti)
        matched_detections.append(di)

    unmatched_detections = [
        di for di in range(len(detections)) if di not in used_detections
    ]
    return matched_tracks, matched_detections, unmatched_detections


def evaluate_action(
    track: Track,
    vector_direction: str,
    motion_threshold_px: float = MOTION_THRESHOLD_PX,
) -> Optional[dict]:
    """Evaluate a completed track into an inventory action.

    Returns None when the track is too short to evaluate.
    """
    if len(track.detections) < 2:
        return None

    first = track.detections[0]
    last = track.detections[-1]
    delta_y = last.centroid[1] - first.centroid[1]
    magnitude = abs(delta_y)

    if magnitude < motion_threshold_px:
        action = "AMBIGUOUS"
    elif vector_direction == "top_to_bottom":
        # Object moved downwards: a hand inserting an item moves top -> bottom.
        action = "INSERTED" if delta_y > 0 else "REMOVED"
    else:  # bottom_to_top
        action = "REMOVED" if delta_y > 0 else "INSERTED"

    average_confidence = sum(
        detection.confidence for detection in track.detections
    ) / len(track.detections)

    return {
        "ai_prompt": track.label,
        "action": action,
        "confidence": round(average_confidence, 3),
        "track_length_frames": len(track.detections),
        "delta_y": round(delta_y, 2),
    }


