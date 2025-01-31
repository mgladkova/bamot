"""Contains data structures used throughout bamot.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import numpy as np

ObjectId = int
FeatureId = int
TrackId = int
ImageId = int  # image number/index
CamId = int  # either 0 (left) or 1 (right)
TimeCamId = Tuple[ImageId, CamId]
Location = Tuple[float, float, float]

FeatureTrack = Dict[ObjectId, FeatureId]


@dataclass
class StereoImage:
    left: np.ndarray
    right: np.ndarray
    img_width: int
    img_height: int


# @dataclass_json
@dataclass
class Feature:
    u: float
    v: float
    descriptor: np.ndarray


@dataclass
class ObjectDetection:
    mask: np.ndarray
    cls: str
    track_id: Optional[TrackId] = None
    features: Optional[List[Feature]] = None


@dataclass
class CameraParameters:
    fx: float
    fy: float
    cx: float
    cy: float


def get_camera_parameters_matrix(params: CameraParameters):
    return np.array([[params.fx, 0, params.cx], [0, params.fy, params.cy], [0, 0, 1]])


class Camera(NamedTuple):
    project: Callable[[np.ndarray], np.ndarray]
    back_project: Callable[[np.ndarray], np.ndarray]
    parameters: Optional[CameraParameters] = None


Match = Tuple[FeatureId, FeatureId]


class FeatureMatcher(NamedTuple):
    name: str
    detect_features: Callable[[np.ndarray, Optional[np.ndarray]], List[Feature]]
    match_features: Callable[[List[Feature], List[Feature]], List[Match]]


@dataclass
class StereoCamera:
    left: CameraParameters
    right: CameraParameters
    T_left_right: np.ndarray


@dataclass
class Observation:
    descriptor: np.ndarray
    pt_2d: np.ndarray  # feature coordinates -- u, v, and if stereo u_r
    img_id: ImageId


@dataclass
class Landmark:
    pt_3d: np.ndarray  # w.r.t. object
    observations: List[Observation]


@dataclass
class ObjectTrack:
    cls: str
    masks: Tuple[np.ndarray, np.ndarray]
    landmarks: Dict[int, Landmark] = field(
        default_factory=dict
    )  # need to be referenced in constant time by id, w.r.t. obj
    poses: Dict[ImageId, np.ndarray] = field(
        default_factory=dict
    )  # changing poses over time w.r.t. world
    locations: Dict[ImageId, np.ndarray] = field(
        default_factory=dict
    )  # "online" locations (only used for eval) w.r.t. world
    pcl_centers: Dict[ImageId, np.ndarray] = field(
        default_factory=dict
    )  # w.r.t. object
    active: bool = True
    in_view: bool = True
    badly_tracked_frames: int = 0
    dist_from_cam: float = 0
    rot_angle: Dict[int, float] = field(default_factory=dict)  # in camera coordinates


@dataclass
class StereoObjectDetection:
    left: ObjectDetection
    right: ObjectDetection
    stereo_matches: Optional[List[Match]] = None


@dataclass
class TrackMatch:
    track_id: int
    detection_id: int
