""" Core code for BAMOT
"""
import copy
import logging
import queue
import time
import uuid
from collections import defaultdict
from itertools import zip_longest
from threading import Event
from typing import Dict, Iterable, List, Set, Tuple

import cv2
import g2o
import numpy as np
import pathos
from bamot.config import CONFIG as config
from bamot.core.base_types import (CameraParameters, Feature, FeatureMatcher,
                                   ImageId, Landmark, Location, Match,
                                   ObjectTrack, Observation, StereoCamera,
                                   StereoImage, StereoObjectDetection, TrackId,
                                   TrackMatch, get_camera_parameters_matrix)
from bamot.core.optimization import object_bundle_adjustment
from bamot.util.cv import (TriangulationError, from_homogeneous,
                           get_center_of_landmarks, get_feature_matcher,
                           get_masks_from_landmarks, is_in_view,
                           to_homogeneous, triangulate_stereo_match)
from bamot.util.misc import get_mad, timer
from scipy.optimize import linear_sum_assignment

LOGGER = logging.getLogger("CORE:MOT")


def get_rotation_of_track(track: ObjectTrack, T_world_cam: np.ndarray) -> float:
    if len(track.poses) < 2:
        return 0
    dir_vector_world = get_direction_vector(
        track, config.SLIDING_WINDOW_DIR_VEC
    ).reshape(3, 1)
    dir_vector_cam = (
        g2o.Isometry3d(T_world_cam).inverse().R.reshape(3, 3) @ dir_vector_world
    )
    # take plane-axes (x, z)
    dir_vector = dir_vector_cam[[0, 2]]
    # normalize
    dir_vector = dir_vector / np.linalg.norm(dir_vector)
    # compute angle between x axis and dir vector
    angle = np.arccos(np.dot(dir_vector.T, np.array([1, 0]).reshape(2, 1)))
    if dir_vector[1] > 0:
        angle = -angle
    if not np.isfinite(angle):
        angle = 0
    return angle


def _get_track_logger(track_id: str):

    readable_track_id = (track_id[:3] + "..") if len(track_id) > 5 else track_id
    return logging.getLogger(f"CORE:MOT:Track {readable_track_id}")


def _add_constant_motion_to_track(
    track: ObjectTrack,
    img_id: ImageId,
    T_world_cam: np.ndarray,
    track_id: TrackId,
    stereo_cam: StereoCamera,
    img_shape: Tuple[int, int],
):
    if not track.poses:
        return track
    track_logger = _get_track_logger(str(track_id))
    T_world_obj = _estimate_next_pose(track, track_logger)
    if track.landmarks:
        track.poses[img_id] = T_world_obj
        track.pcl_centers[img_id] = get_center_of_landmarks(track.landmarks.values())
        track.locations[img_id] = from_homogeneous(
            track.poses[img_id] @ to_homogeneous(track.pcl_centers[img_id])
        )
        track.rot_angle[img_id] = get_rotation_of_track(track, T_world_cam)
        T_cam_obj = np.linalg.inv(T_world_cam) @ T_world_obj
        # track behind camera/ego
        pcl_center_cam = from_homogeneous(
            T_cam_obj @ to_homogeneous(track.pcl_centers[img_id])
        )
        if not track.in_view:
            track.masks = (None, None)
        else:
            track.masks = get_masks_from_landmarks(
                track.landmarks, T_cam_obj, stereo_cam, img_shape
            )
        if len(track.poses) > 1 and pcl_center_cam[-1] < 0:
            track_logger.debug("Track is behind camera (z: %f)", pcl_center_cam[-1])
            track.active = False
    return track


def _remove_outlier_landmarks(
    landmarks, current_landmarks, cls, track_logger, T_cam_obj
):
    landmarks_to_remove = []
    points = np.array(current_landmarks)

    cluster_median_center = np.median(points, axis=0)
    dist_from_cam = np.linalg.norm(
        from_homogeneous(T_cam_obj @ to_homogeneous(cluster_median_center))
    )
    dist_factor = 1 + max(0, dist_from_cam - 15) / 30
    for lid, lm in landmarks.items():
        if not config.USING_MEDIAN_CLUSTER:
            cluster_radius = (
                config.CLUSTER_RADIUS_CAR if cls == "car" else config.CLUSTER_RADIUS_PED
            )
            if np.linalg.norm(lm.pt_3d - cluster_median_center) > (
                dist_factor * cluster_radius
            ):
                landmarks_to_remove.append(lid)
        else:
            if np.linalg.norm(
                lm.pt_3d - cluster_median_center
            ) > config.MAD_SCALE_FACTOR * get_mad(points):
                landmarks_to_remove.append(lid)

    track_logger.debug("Removing %d outlier landmarks", len(landmarks_to_remove))
    for lid in landmarks_to_remove:
        landmarks.pop(lid)
    return cluster_median_center, dist_from_cam


def get_median_translation(object_track):
    translations = []
    frames = list(object_track.poses.keys())
    for i in range(len(frames[-2 * config.SLIDING_WINDOW_BA :]) - 1):
        img_id_0 = frames[i]
        img_id_1 = frames[i + 1]
        location0 = object_track.locations[img_id_0]
        location1 = object_track.locations[img_id_1]
        translations.append(np.linalg.norm(location0 - location1))
    if not translations:
        return None
    LOGGER.debug("Translations:\n%s", translations)
    return np.median(translations)


def _get_max_dist(
    obj_cls,
    badly_tracked_frames,
    cam,
    median_translation=None,
    dist_from_cam=None,
    num_poses=0,
    track_logger=LOGGER,
):
    max_speed = config.MAX_SPEED_CAR if obj_cls == "car" else config.MAX_SPEED_PED
    min_speed = (
        max_speed / 10
    )  # allow for some motion even if previous estimates had none
    if median_translation is not None and (num_poses - badly_tracked_frames) >= 5:
        track_logger.debug("Using max speed based on median translation")
        max_speed = min(max_speed, 4 * median_translation * config.FRAME_RATE)
        max_translation = max(
            min_speed / config.FRAME_RATE, max_speed / config.FRAME_RATE
        )
    else:
        track_logger.debug("Using max speed of object type")
        max_translation = max_speed / config.FRAME_RATE
    track_logger.debug("Max translation: %f", max_translation)
    cam_baseline = cam.T_left_right[0, 3]
    dist_factor = (
        1
        if dist_from_cam is None
        else max(1, (1 * dist_from_cam) / (2 * 20 * cam_baseline))
    )
    track_logger.debug("Dist factor: %f", dist_factor)
    track_logger.debug("Badly tracked frames: %d", badly_tracked_frames)
    return (
        min(
            config.MAX_MAX_DIST_MULTIPLIER,
            (0.75 * badly_tracked_frames + 1) * dist_factor,
        )
        * max_translation
    )


def _is_valid_motion(
    Tr_rel,
    obj_cls,
    badly_tracked_frames,
    cam,
    median_translation=None,
    dist_from_cam=None,
    track_logger=LOGGER,
    num_poses=0,
):
    curr_translation = np.linalg.norm(Tr_rel[:3, 3])
    max_dist = _get_max_dist(
        obj_cls=obj_cls,
        badly_tracked_frames=badly_tracked_frames,
        cam=cam,
        dist_from_cam=dist_from_cam,
        median_translation=median_translation,
        num_poses=num_poses,
        track_logger=track_logger,
    )
    track_logger.debug("Current translation: %.2f", float(curr_translation))
    track_logger.debug("Max. allowed translation: %.2f", max_dist)
    return curr_translation < max_dist


def _localize_object(
    left_features: List[Feature],
    track_matches: List[Match],
    landmark_mapping: Dict[int, int],
    landmarks: Dict[int, Landmark],
    T_cam_obj: np.ndarray,
    camera_params: CameraParameters,
    logger: logging.Logger = LOGGER,
    num_iterations: int = 400,
    reprojection_error: float = 2.0,
) -> Tuple[np.ndarray, bool, float]:
    pts_3d = []
    pts_2d = []

    if len(track_matches) < 4:
        logger.debug("Too few matches (%d) for PnP (minimum 4)", len(track_matches))
        return T_cam_obj, False, 0
    logger.debug(
        "Localizing object based on %d point correspondences", len(track_matches)
    )
    # build pt arrays
    for features_idx, landmark_idx in track_matches:
        pt_3d = landmarks[landmark_mapping[landmark_idx]].pt_3d
        feature = left_features[features_idx]
        pt_2d = np.array([feature.u, feature.v])
        pts_3d.append(pt_3d)
        pts_2d.append(pt_2d)
    pts_3d = np.array(pts_3d).reshape(-1, 3)
    pts_2d = np.array(pts_2d).reshape(-1, 2)
    rot = T_cam_obj[:3, :3]
    # use previous pose + constant motion as initial guess
    trans = T_cam_obj[:3, 3]
    # solvePnPRansac estimates object pose, not camera pose
    successful, rvec, tvec, inliers = cv2.solvePnPRansac(
        objectPoints=pts_3d,
        imagePoints=pts_2d,
        cameraMatrix=get_camera_parameters_matrix(camera_params),
        distCoeffs=None,
        rvec=cv2.Rodrigues(rot)[0],
        tvec=trans.astype(float),
        useExtrinsicGuess=True,
        iterationsCount=num_iterations,
        reprojectionError=reprojection_error,
    )
    num_inliers = len(inliers) if inliers is not None else 0
    inlier_ratio = num_inliers / len(track_matches)
    logger.debug("Inlier ratio for PnP: %.2f", inlier_ratio)
    if successful and inlier_ratio > 0.25:
        logger.debug("Optimization successful! Found %d inliers", len(inliers))
        logger.debug("Running optimization with inliers...")
        successful, rvec, tvec = cv2.solvePnP(
            objectPoints=np.array([mp for i, mp in enumerate(pts_3d) if i in inliers]),
            imagePoints=np.array([ip for i, ip in enumerate(pts_2d) if i in inliers]),
            cameraMatrix=get_camera_parameters_matrix(camera_params),
            distCoeffs=None,
            rvec=rvec,
            tvec=tvec,
            useExtrinsicGuess=True,
        )
        if successful:
            LOGGER.debug("Inlier optimization successful!")
            rot, _ = cv2.Rodrigues(rvec)
            optimized_pose = np.identity(4)
            optimized_pose[:3, :3] = rot
            optimized_pose[:3, 3] = tvec
            LOGGER.debug("Optimized pose from \n%s\nto\n%s", T_cam_obj, optimized_pose)
            return optimized_pose, True, inlier_ratio
    logger.debug("Optimization failed...")
    return T_cam_obj, False, 0


def _add_new_landmarks_and_observations(
    landmarks: Dict[int, Landmark],
    track_matches: List[Match],
    landmark_mapping: Dict[int, int],
    stereo_matches: List[Match],
    left_features: List[Feature],
    right_features: List[Feature],
    stereo_cam: StereoCamera,
    T_cam_obj: np.ndarray,
    img_id: int,
    logger: logging.Logger,
) -> Dict[int, Landmark]:
    already_added_features = []
    stereo_match_dict = {}
    for left_feature_idx, right_feature_idx in stereo_matches:
        stereo_match_dict[left_feature_idx] = right_feature_idx

    current_landmarks = []

    # add new observations to existing landmarks
    for features_idx, landmark_idx in track_matches:
        feature = left_features[features_idx]
        pt_obj = landmarks[landmark_mapping[landmark_idx]].pt_3d
        pt_cam = from_homogeneous(T_cam_obj @ to_homogeneous(pt_obj))
        z = pt_cam[2]
        if (
            z < 0.5 or np.linalg.norm(pt_cam) > config.MAX_DIST
        ):  # don't add landmarks that are very behind camera/very close or far away
            continue
        # stereo observation
        if stereo_match_dict.get(features_idx) is not None:
            right_feature = right_features[stereo_match_dict[features_idx]]
            # check epipolar constraint
            if np.allclose(feature.v, right_feature.v, atol=1):
                feature_pt = np.array([feature.u, feature.v, right_feature.u])
            else:
                feature_pt = np.array([feature.u, feature.v])

        # mono observation
        else:
            feature_pt = np.array([feature.u, feature.v])
        obs = Observation(
            descriptor=feature.descriptor, pt_2d=feature_pt, img_id=img_id
        )
        current_landmarks.append(pt_obj)
        already_added_features.append(features_idx)
        landmarks[landmark_mapping[landmark_idx]].observations.append(obs)
    logger.debug("Added %d observations", len(already_added_features))

    # add new landmarks
    created_landmarks = 0
    bad_matches = []
    for left_feature_idx, right_feature_idx in stereo_matches:
        # check whether landmark exists already
        if left_feature_idx in already_added_features:
            continue
        left_feature = left_features[left_feature_idx]
        right_feature = right_features[right_feature_idx]
        try:
            pt_3d_obj = triangulate_stereo_match(
                left_feature=left_feature,
                right_feature=right_feature,
                stereo_cam=stereo_cam,
                T_ref_cam=np.linalg.inv(T_cam_obj),
            )
        except TriangulationError:
            bad_matches.append((left_feature_idx, right_feature_idx))
            continue

        feature_pt = np.array([left_feature.u, left_feature.v, right_feature.u])
        landmark_id = uuid.uuid1().int
        # create new landmark
        obs = Observation(
            descriptor=left_feature.descriptor, pt_2d=feature_pt, img_id=img_id
        )
        current_landmarks.append(pt_3d_obj)
        landmark = Landmark(pt_3d_obj, [obs])
        landmarks[landmark_id] = landmark
        created_landmarks += 1

    for match in bad_matches:
        stereo_matches.remove(match)
    logger.debug("Created %d landmarks", created_landmarks)
    return landmarks, current_landmarks


def _get_median_descriptor(
    observations: List[Observation], norm: int, smallest_dist_to_rest: bool = True
) -> np.ndarray:
    if len(observations) > config.SLIDING_WINDOW_DESCRIPTORS:
        rng = np.random.default_rng()
        subset = rng.choice(
            observations, size=config.SLIDING_WINDOW_DESCRIPTORS, replace=False
        )
    else:
        subset = observations
    if not smallest_dist_to_rest:
        med_desc = np.median(
            np.array([obs.descriptor for obs in subset]).reshape(len(subset), -1),
            axis=0,
        ).astype(np.uint8)
        return med_desc
    # subset = observations[-config.SLIDING_WINDOW_DESCRIPTORS :]
    distances = np.zeros((len(subset), len(subset)))
    for i, obs in enumerate(subset):
        for j in range(i, len(subset)):
            other_obs = subset[j]
            # calculate distance between i and j
            dist = np.linalg.norm(obs.descriptor - other_obs.descriptor, ord=norm)
            # do for all combinations
            distances[i, j] = dist
            distances[j, i] = dist
    best_median = None
    best_idx = 0
    for i, obs in enumerate(subset):
        dist_per_descriptor = distances[i]
        median = np.median(dist_per_descriptor)
        if not best_median or median < best_median:
            best_median = median
            best_idx = i
    return subset[best_idx].descriptor


def _get_features_from_landmarks(
    landmarks: Dict[int, Landmark]
) -> Tuple[List[Feature], Dict[int, int]]:
    features = []
    landmark_mapping = {}
    idx = 0
    for lid, landmark in landmarks.items():
        obs = landmark.observations
        descriptor = _get_median_descriptor(obs, norm=2)
        features.append(Feature(u=0.0, v=0.0, descriptor=descriptor))
        landmark_mapping[idx] = lid
        idx += 1
    return features, landmark_mapping


@timer
def run(
    images: Iterable[StereoImage],
    detections: Iterable[List[StereoObjectDetection]],
    stereo_cam: StereoCamera,
    slam_data: queue.Queue,
    shared_data: queue.Queue,
    writer_data_2d: queue.Queue,
    writer_data_3d: queue.Queue,
    writer_obb_data: queue.Queue,
    returned_data: queue.Queue,
    stop_flag: Event,
    next_step: Event,
    continuous_until_img_id: int,
    img_shape: Tuple[int, int],
):
    active_object_tracks: Dict[int, ObjectTrack] = {}
    all_object_tracks: Dict[int, ObjectTrack] = {}
    ba_slots: Tuple[set] = tuple(set() for _ in range(config.BA_EVERY_N_STEPS))
    LOGGER.info("Starting MOT run")

    def _process_match(
        track: ObjectTrack,
        detection: StereoObjectDetection,
        all_poses: Dict[ImageId, np.ndarray],
        track_id: TrackId,
        stereo_cam: StereoCamera,
        img_id: ImageId,
        stereo_image: StereoImage,
        current_cam_pose: np.ndarray,
        run_ba: bool,
        cached_pnp_poses: Dict[TrackId, np.ndarray],
    ):
        track.active = True
        track_logger = _get_track_logger(str(track_id))
        track_logger.debug("Image: %d", img_id)
        feature_matcher = get_feature_matcher()
        left_features, right_features = _extract_features(
            detection, stereo_image, img_id, track_id
        )
        # match stereo features
        if detection.stereo_matches is None:
            stereo_matches = feature_matcher.match_features(
                left_features, right_features
            )
        else:
            stereo_matches = detection.stereo_matches
        track_logger.debug("%d stereo matches", len(stereo_matches))
        # match left features with track features
        features, lm_mapping = _get_features_from_landmarks(track.landmarks)
        track_matches = feature_matcher.match_features(left_features, features)
        track_logger.debug("%d track matches", len(track_matches))
        # localize object
        T_world_obj = _estimate_next_pose(track, track_logger)
        T_world_cam = current_cam_pose
        T_cam_obj = np.linalg.inv(T_world_cam) @ T_world_obj
        enough_track_matches = len(track_matches) >= 5
        successful = True
        valid_motion = True
        median_translation = get_median_translation(track)
        if enough_track_matches:
            if cached_pnp_poses.get(track_id) is not None:
                track_logger.debug("Getting cached PnP pose")
                T_cam_obj_pnp = cached_pnp_poses.get(track_id)
                successful = True
            else:
                T_cam_obj_pnp, successful, _ = _localize_object(
                    left_features=left_features,
                    track_matches=track_matches,
                    landmark_mapping=lm_mapping,
                    landmarks=copy.deepcopy(track.landmarks),
                    T_cam_obj=T_cam_obj.copy(),
                    camera_params=stereo_cam.left,
                    logger=track_logger,
                )

            if successful:
                if len(track.poses) >= 2:
                    T_world_obj_prev = track.poses[list(track.poses.keys())[-2]]
                    T_world_obj_pnp = T_world_cam @ T_cam_obj_pnp
                    T_rel = np.linalg.inv(T_world_obj_prev) @ T_world_obj_pnp
                    valid_motion = _is_valid_motion(
                        T_rel,
                        track.cls,
                        track.badly_tracked_frames,
                        cam=stereo_cam,
                        dist_from_cam=track.dist_from_cam,
                        median_translation=median_translation,
                        track_logger=track_logger,
                        num_poses=len(track.poses),
                    )
                    track_logger.debug("Median translation: %.2f", median_translation)
                    if valid_motion:
                        track_logger.debug("PnP estimate is valid motion")
                        T_cam_obj = T_cam_obj_pnp
        if not (
            (enough_track_matches or len(track.poses) == 1)
            and successful
            and valid_motion
        ):
            track_logger.debug(
                "Enough matches: %s (%d)", enough_track_matches, len(track_matches)
            )
            if enough_track_matches:
                track_logger.debug("PnP successful: %s", successful)
                if successful:
                    track_logger.debug("Valid motion: %s", valid_motion)
        track.badly_tracked_frames = 0

        T_world_obj = T_world_cam @ T_cam_obj
        # add new landmark observations from track matches
        # add new landmarks from stereo matches
        track.landmarks, current_landmarks = _add_new_landmarks_and_observations(
            landmarks=copy.deepcopy(track.landmarks),
            track_matches=track_matches,
            landmark_mapping=lm_mapping,
            stereo_matches=stereo_matches,
            left_features=left_features,
            right_features=right_features,
            stereo_cam=stereo_cam,
            img_id=img_id,
            T_cam_obj=T_cam_obj,
            logger=track_logger,
        )
        # remove outlier landmarks
        if current_landmarks:
            current_landmark_median, dist_from_cam = _remove_outlier_landmarks(
                track.landmarks, current_landmarks, track.cls, track_logger, T_cam_obj
            )
            track.dist_from_cam = dist_from_cam
        else:
            current_landmark_median = get_center_of_landmarks(
                track.landmarks.values(), reduction="median"
            )
        # BA optimizes landmark positions w.r.t. object and object position over time
        # -> SLAM optimizes motion of camera
        # cameras maps a timecam_id (i.e. frame + left/right) to a camera pose and camera parameters
        if len(track.poses) > 3 and track.landmarks and run_ba:
            track_logger.debug("Running BA")
            track = object_bundle_adjustment(
                object_track=copy.deepcopy(track),
                all_poses=all_poses,
                stereo_cam=stereo_cam,
                median_translation=median_translation,
            )
        if track.landmarks:
            track.poses[img_id] = T_world_obj
        if (
            len(track.poses) == 1
            and img_id == list(track.poses.keys())[0]
            and current_landmarks
        ):
            # re-calculate object frame to be close to object
            T_world_obj = track.poses[img_id]
            T_world_obj_old = T_world_obj.copy()
            median_cluster_world = from_homogeneous(
                T_world_obj_old @ to_homogeneous(current_landmark_median)
            )
            T_world_obj = np.identity(4)
            T_world_obj[:3, 3] += median_cluster_world.reshape(3,)
            current_landmark_median = np.array([0, 0, 0]).reshape(3, 1)
            for lmid in track.landmarks:
                pt_3d_obj = track.landmarks[lmid].pt_3d
                pt_3d_world = T_world_obj_old @ to_homogeneous(pt_3d_obj)
                pt_3d_obj_new = np.linalg.inv(T_world_obj) @ (pt_3d_world)
                track.landmarks[lmid].pt_3d = from_homogeneous(pt_3d_obj_new)

        # not setting or setting min_landmarks to 0 disables robust initialization
        min_landmarks = (
            config.MIN_LANDMARKS_CAR if track.cls == "car" else config.MIN_LANDMARKS_PED
        )
        # robust init
        if (
            len(track.poses) == 1
            and min_landmarks
            and len(track.landmarks) < min_landmarks
        ):
            track_logger.debug(
                "Track doesn't have enough landmarks (%d) for init (min: %d)",
                len(track.landmarks),
                min_landmarks,
            )
            track.active = False
        # track far away
        if track.dist_from_cam > config.MAX_DIST:
            track_logger.debug(
                "Track too far away: %f (max: %f)", track.dist_from_cam, config.MAX_DIST
            )
            track.active = False

        if track.landmarks:
            track.poses[img_id] = T_world_obj
            track.pcl_centers[img_id] = current_landmark_median
            track.locations[img_id] = from_homogeneous(
                track.poses[img_id]
                @ to_homogeneous(
                    np.mean(
                        [
                            current_landmark_median,
                            get_center_of_landmarks(
                                track.landmarks.values(), reduction="median"
                            ),
                        ],
                        axis=0,
                    )
                )
            )
            track.rot_angle[img_id] = get_rotation_of_track(track, T_world_cam)
            # track behind camera/ego
            pcl_center_cam = from_homogeneous(
                T_cam_obj @ to_homogeneous(track.pcl_centers[img_id])
            )
            if len(track.poses) > 1 and pcl_center_cam[-1] < 0:
                track_logger.debug("Track is behind camera (z: %f)", pcl_center_cam[-1])
                track.active = False
        return track, left_features, right_features, stereo_matches

    point_cloud_sizes = {}
    track_id_mapping = {}
    for (img_id, stereo_image), new_detections in zip_longest(
        images, detections, fillvalue=[]
    ):
        all_track_ids = set(all_object_tracks).union(set(active_object_tracks))
        if config.TRACK_POINT_CLOUD_SIZES:
            for track_id, obj in active_object_tracks.items():
                point_cloud_size = len(obj.landmarks)
                if point_cloud_sizes.get(track_id):
                    point_cloud_sizes[track_id].append(point_cloud_size)
                else:
                    point_cloud_sizes[track_id] = [point_cloud_size]
        if stop_flag.is_set():
            break
        if img_id > continuous_until_img_id and continuous_until_img_id != -1:
            while not next_step.is_set():
                time.sleep(0.05)
        next_step.clear()
        all_poses = slam_data.get()
        slam_data.task_done()
        current_pose = all_poses[img_id]
        active_track_ids = list(active_object_tracks)

        # clear slots
        slot_sizes = {}
        for idx, slot in enumerate(ba_slots):
            slot.clear()
            slot_sizes[idx] = 0
        # add track_ids to ba slots
        for track_id in active_track_ids:
            slot_idx, _ = sorted(list(slot_sizes.items()), key=lambda x: x[1],)[0]
            ba_slots[slot_idx].add(track_id)
            slot_sizes[slot_idx] += 1
        tracks_to_run_ba = ba_slots[img_id % config.BA_EVERY_N_STEPS]
        LOGGER.debug("BA slots: %s", ba_slots)
        try:
            (
                active_object_tracks,
                all_left_features,
                all_right_features,
                all_stereo_matches,
                old_tracks,
            ) = step(
                new_detections=new_detections,
                stereo_image=stereo_image,
                object_tracks=copy.deepcopy(active_object_tracks),
                process_match=_process_match,
                stereo_cam=stereo_cam,
                img_id=img_id,
                current_cam_pose=current_pose,
                all_poses=all_poses,
                tracks_to_run_ba=tracks_to_run_ba,
                all_track_ids=all_track_ids,
                track_id_mapping=track_id_mapping,
                img_shape=img_shape,
            )
        except Exception as exc:  # ignore: broad-except
            LOGGER.exception("Unexpected error: %s", exc)
            break
        for track_id in old_tracks:
            # only store tracks that weren't immediately deemed false positives
            LOGGER.debug("Deleting %d", track_id)
            track = active_object_tracks[track_id]
            if len(track.poses) > 1:
                all_object_tracks[track_id] = copy.deepcopy(track)
            del active_object_tracks[track_id]
            inverse_track_mapping = {v: k for k, v in track_id_mapping.items()}
            source_track_id = inverse_track_mapping.get(track_id)
            if source_track_id is not None:
                del track_id_mapping[source_track_id]

        shared_data.put(
            {
                "object_tracks": copy.deepcopy(active_object_tracks),
                "stereo_image": stereo_image,
                "all_left_features": all_left_features,
                "all_right_features": all_right_features,
                "all_stereo_matches": all_stereo_matches,
                "img_id": img_id,
                "current_cam_pose": current_pose,
            }
        )
        if config.SAVE_UPDATED_2D_TRACK:
            track_copy = copy.deepcopy(
                {
                    track_id: track
                    for track_id, track in active_object_tracks.items()
                    if track.masks[0] is not None and track.landmarks
                }
            )
            writer_data_2d.put(
                {
                    "track_ids": [track_id for track_id in track_copy],
                    "img_id": img_id,
                    "object_classes": [obj.cls for obj in track_copy.values()],
                    "masks": [obj.masks[0] for obj in track_copy.values()],
                }
            )
        if config.SAVE_3D_TRACK:
            track_copy = copy.deepcopy(
                {
                    track_id: track
                    for track_id, track in active_object_tracks.items()
                    if track.masks[0] is not None and track.landmarks
                }
            )
            writer_data_3d.put(
                {"T_world_cam": current_pose, "tracks": track_copy, "img_id": img_id,}
            )
        if config.SAVE_OBB_DATA:
            track_copy = copy.deepcopy(
                {
                    track_id: track
                    for track_id, track in active_object_tracks.items()
                    if track.masks[0] is not None and track.landmarks
                }
            )
            writer_obb_data.put(
                {"tracks": track_copy, "img_id": img_id, "T_world_cam": current_pose}
            )

    stop_flag.set()
    shared_data.put({})
    writer_data_2d.put({})
    writer_data_3d.put({})
    writer_obb_data.put({})
    all_object_tracks.update(active_object_tracks)
    if config.FINAL_FULL_BA:
        for track_id, track in all_object_tracks.items():
            median_translation = get_median_translation(track)
            track = object_bundle_adjustment(
                track,
                all_poses,
                stereo_cam,
                median_translation,
                max_iterations=20,
                full_ba=True,
            )
            all_object_tracks[track_id] = track

    returned_data.put(
        dict(
            trajectories=_compute_estimated_trajectories(all_object_tracks, all_poses),
            point_cloud_sizes=point_cloud_sizes,
            track_id_to_class_mapping={
                track_id: track.cls for track_id, track in all_object_tracks.items()
            },
        ),
    )


def get_direction_vector(track, num_frames):
    available_poses = list(track.poses)  # sorted in order of entry by default
    num_frames = min(num_frames, len(available_poses))
    T_world_obj1 = g2o.Isometry3d(track.poses[available_poses[-1]])
    LOGGER.debug("Previous pose:\n%s", T_world_obj1.matrix())
    T_world_obj0 = g2o.Isometry3d(track.poses[available_poses[-num_frames]])
    return (T_world_obj1.translation() - T_world_obj0.translation()) / (num_frames)


def _estimate_next_pose(track: ObjectTrack, track_logger=LOGGER) -> np.ndarray:
    available_poses = list(track.poses)  # sorted in order of entry by default
    if len(available_poses) >= 2:
        num_frames = min(int(config.SLIDING_WINDOW_BA), len(available_poses))
        T_world_obj1 = g2o.Isometry3d(track.poses[available_poses[-1]])
        track_logger.debug("Previous pose:\n%s", T_world_obj1.matrix())
        T_world_obj0 = g2o.Isometry3d(track.poses[available_poses[-num_frames]])
        rel_translation = (T_world_obj1.translation() - T_world_obj0.translation()) / (
            num_frames
        )
        track_logger.debug("Relative translation:\n%s", rel_translation)
        T_world_new = g2o.Isometry3d(
            T_world_obj1.rotation(), T_world_obj1.translation() + 1.0 * rel_translation,
        )
        track_logger.debug("Estimated new pose:\n%s", T_world_new.matrix())
        return T_world_new.matrix()
    return track.poses[available_poses[-1]]


def _extract_features(stereo_detection, stereo_image, img_id, track_id):
    feature_matcher = get_feature_matcher()
    if not stereo_detection.left.features or config.FORCE_NEW_DETECTIONS:
        left_features = feature_matcher.detect_features(
            stereo_image.left, stereo_detection.left.mask, img_id, track_id, "left"
        )
        stereo_detection.left.features = left_features
    else:
        left_features = stereo_detection.left.features
    if not stereo_detection.right.features or config.FORCE_NEW_DETECTIONS:
        right_features = feature_matcher.detect_features(
            stereo_image.right, stereo_detection.right.mask, img_id, track_id, "right",
        )
        stereo_detection.right.features = right_features
    else:
        right_features = stereo_detection.right.features
    return left_features, right_features


def _get_center_of_stereo_pointcloud(
    stereo_detection: StereoObjectDetection,
    stereo_image: StereoImage,
    img_id: ImageId,
    track_id: TrackId,
    stereo_cam: StereoCamera,
    T_world_cam: np.ndarray,
    reduction: str = "median",
):
    feature_matcher = get_feature_matcher()
    left_features, right_features = _extract_features(
        stereo_detection, stereo_image, img_id, track_id
    )

    stereo_matches = feature_matcher.match_features(left_features, right_features,)
    stereo_detection.stereo_matches = stereo_matches
    pcl = []
    for left_feature_idx, right_feature_idx in stereo_matches:
        left_feature = left_features[left_feature_idx]
        right_feature = right_features[right_feature_idx]
        try:
            pt_world = triangulate_stereo_match(
                left_feature, right_feature, stereo_cam, T_world_cam
            )
            pcl.append(pt_world)
        except TriangulationError:
            pass
    if not pcl:  # no stereo matches
        return None
    if reduction == "mean":
        return np.mean(pcl, axis=0)
    if reduction == "median":
        return np.median(pcl, axis=0)
    raise RuntimeError(f"Unknown reduction: {reduction}")


def _improve_association(
    detections,
    tracks,
    T_world_cam,
    stereo_cam,
    stereo_image,
    img_id,
    all_track_ids,
    track_id_mapping,
):
    cost_matrix = np.zeros((len(detections), len(tracks)))
    feature_matcher = get_feature_matcher()
    all_pnp_poses = defaultdict(dict)
    matches = []
    tracks_not_in_view = set()
    medians = {}
    median_translations = {}
    LOGGER.debug("%d detection(s) in image %d", len(detections), img_id)
    # first, do pnp-based matching
    for i, detection in enumerate(detections):
        for j, (track_id, track) in enumerate(tracks.items()):
            LOGGER.debug(
                "Checking track %d against detection %d (tid: %d)",
                track_id,
                i,
                detection.left.track_id,
            )
            left_features, right_features = _extract_features(
                detection, stereo_image, img_id, track_id
            )
            median = medians.get(
                i,
                _get_center_of_stereo_pointcloud(
                    detection, stereo_image, img_id, track_id, stereo_cam, T_world_cam
                ),
            )
            medians[i] = median
            median_translation = median_translations.get(
                track_id, get_median_translation(track)
            )
            median_translations[track_id] = median_translation
            if track.cls != detection.left.cls:
                LOGGER.debug("Wrong class!")
                # wrong class
                continue
            if median is None:
                LOGGER.debug("No stereo matches!")
                continue
            T_world_obj = _estimate_next_pose(track)
            T_cam_obj = np.linalg.inv(T_world_cam) @ T_world_obj
            if not is_in_view(
                track.landmarks,
                T_cam_obj,
                stereo_cam.left,
                min_landmarks=1,  # int(0.2 * len(track.landmarks)),
            ):
                LOGGER.debug("Track %d not in view, can't match", track_id)
                track.in_view = False
                tracks_not_in_view.add(track_id)
                tracks_not_in_view.add(track_id_mapping.get(track_id, track_id))
                continue
            track.in_view = True
            features, lm_mapping = _get_features_from_landmarks(track.landmarks)
            track_matches = feature_matcher.match_features(left_features, features)
            T_cam_obj_pnp, pnp_success, inlier_ratio = _localize_object(
                left_features,
                track_matches,
                lm_mapping,
                track.landmarks,
                T_cam_obj,
                camera_params=stereo_cam.left,
            )
            LOGGER.debug("Pnp successfull: %s", pnp_success)
            LOGGER.debug("Inlier ratio: %f", inlier_ratio)
            # check whether pnp pose estimate is valid
            num_inliers = inlier_ratio * len(track_matches)
            last_img_id = list(track.poses)[-1]
            T_world_obj_prev = track.poses[last_img_id]
            T_world_obj_pnp = T_world_cam @ T_cam_obj_pnp
            T_rel = np.linalg.inv(T_world_obj_prev) @ T_world_obj_pnp
            if pnp_success and _is_valid_motion(
                T_rel,
                obj_cls=track.cls,
                badly_tracked_frames=track.badly_tracked_frames,
                cam=stereo_cam,
                dist_from_cam=track.dist_from_cam,
                num_poses=len(track.poses),
                median_translation=median_translation,
            ):
                score = num_inliers / min(len(features), len(track.landmarks))
                cost_matrix[i][j] = score
                all_pnp_poses[track_id][i] = T_cam_obj_pnp.copy()

    first_indices, second_indices = linear_sum_assignment(cost_matrix, maximize=True)
    matched_tracks = set()
    matched_detections = set()
    pnp_poses = {}

    for row_idx, col_idx in zip(first_indices, second_indices):
        track_id = list(tracks.keys())[col_idx]
        detection_id = row_idx

        inlier_ratio = cost_matrix[row_idx, col_idx].sum()
        track = tracks[track_id]

        if inlier_ratio > 0.0:
            track_ids_match = track_id == detections[detection_id].left.track_id
            LOGGER.debug(
                "Matched detection %d to track %d with inlier ratio of %f. Track ids match: %s",
                detection_id,
                track_id,
                inlier_ratio,
                track_ids_match,
            )
            if not track_ids_match:
                track_id_mapping[detections[detection_id].left.track_id] = track_id
            matches.append(TrackMatch(track_id=track_id, detection_id=detection_id,))
            matched_tracks.add(track_id)
            matched_detections.add(detection_id)
            pnp_poses[track_id] = all_pnp_poses[track_id][detection_id]

    unmatched_detections = set(range(len(detections))).difference(matched_detections)
    unmatched_tracks = set(tracks).difference(matched_tracks)
    LOGGER.debug("%d valid track match(es) in total", len(matched_tracks))
    LOGGER.debug(
        "%d unmatched detection(s) after 3D + appearance association: %s",
        len(unmatched_detections),
        unmatched_detections,
    )
    LOGGER.debug(
        "%d unmatched track(s) after 3D + appearance association: %s ",
        len(unmatched_tracks),
        unmatched_tracks,
    )
    # then do corroborated 2d matching
    if config.TRUST_2D != "no":
        LOGGER.debug("Corroborating unmatched associations from tracker")
        for detection_id in unmatched_detections:
            detection = detections[detection_id]
            detection_track_id = detections[detection_id].left.track_id
            if detection_track_id in track_id_mapping:
                track_id = track_id_mapping[detection_track_id]
            else:
                track_id = detection_track_id

            LOGGER.debug("Checking detection %d w/ track id %d", detection_id, track_id)

            if track_id in matched_tracks:
                # already matched --> disregard 2d tracker
                LOGGER.debug("Track already matched")
                track_id_mapping[track_id] = uuid.uuid1().int
                track_id = track_id_mapping[track_id]
                matched_detections.add(detection_id)
                matches.append(TrackMatch(track_id=track_id, detection_id=detection_id))
                continue

            if track_id in tracks_not_in_view:
                LOGGER.debug("Track not in view, matching makes no sense")
                track_id_mapping[track_id] = uuid.uuid1().int
                track_id = track_id_mapping[track_id]
                matched_detections.add(detection_id)
                matches.append(TrackMatch(track_id=track_id, detection_id=detection_id))
                continue

            if track_id not in all_track_ids.union(matched_tracks):
                # track id is new --> create new track
                LOGGER.debug("Track is new!")
                matched_detections.add(detection_id)
                matches.append(TrackMatch(track_id=track_id, detection_id=detection_id))

            elif track_id not in tracks:
                # track id wasn't matched yet & its not new & its not in the current tracks --> old track
                # create new track with new id & update track mapping
                LOGGER.debug("Track is old, creating new track")
                track_id_mapping[track_id] = uuid.uuid1().int
                track_id = track_id_mapping[track_id]
                matched_detections.add(detection_id)
                matches.append(TrackMatch(track_id=track_id, detection_id=detection_id))
            else:
                # track isn't new or old & wasn't matched yet --> check whether distance is valid
                if track.cls != detections[detection_id].left.cls:
                    LOGGER.debug("Wrong class!")
                    continue
                median = medians[detection_id]
                if median is None:
                    # if no points can be matched, detection has no useful info, discard
                    LOGGER.debug("No stereo matches, trusting tracker")
                    continue
                median_translation = median_translations[track_id]

                last_img_id = list(track.locations)[-1]
                prev_location = track.locations[last_img_id]
                dist = np.linalg.norm(median - prev_location)
                max_dist = _get_max_dist(
                    obj_cls=track.cls,
                    badly_tracked_frames=track.badly_tracked_frames,
                    dist_from_cam=track.dist_from_cam,
                    median_translation=median_translation,
                    num_poses=len(track.poses),
                    cam=stereo_cam,
                )
                LOGGER.debug("Dist/max. dist: %f/%f", dist, max_dist)
                valid_motion = np.isfinite(dist) and dist < max_dist
                if valid_motion:
                    LOGGER.debug("2D association makes sense in 3D")
                    matches.append(
                        TrackMatch(track_id=track_id, detection_id=detection_id)
                    )
                    matched_detections.add(detection_id)
                    matched_tracks.add(track_id)
                else:
                    LOGGER.debug("2D association does not make sense in 3D")
        unmatched_detections = set(range(len(detections))).difference(
            matched_detections
        )
        unmatched_tracks = set(tracks).difference(matched_tracks)
        LOGGER.debug(
            "%d unmatched track(s) after 2D association: %s ",
            len(unmatched_tracks),
            unmatched_tracks,
        )
        LOGGER.debug(
            "%d unmatched detections(s) after 2D association: %s ",
            len(unmatched_detections),
            unmatched_detections,
        )

    LOGGER.debug("Associating using only 3D info")
    cost_matrix = np.zeros((len(detections), len(unmatched_tracks)))
    for detection_id in unmatched_detections:
        for j, track_id in enumerate(unmatched_tracks):
            LOGGER.debug(
                "Checking track %d against detection %d (tid: %d)",
                track_id,
                detection_id,
                detections[detection_id].left.track_id,
            )
            if track_id in tracks_not_in_view:
                LOGGER.debug("Track not in view, matching makes no sense")
                continue

            if track.cls != detections[detection_id].left.cls:
                LOGGER.debug("Wrong class!")
                continue

            track = tracks[track_id]
            median = medians[detection_id]
            if median is None:
                LOGGER.debug("No median!")
                continue

            last_img_id = list(track.locations)[-1]
            prev_location = track.locations[last_img_id]
            dist = np.linalg.norm(median - prev_location)
            median_translation = median_translations[track_id]
            max_dist = _get_max_dist(
                obj_cls=track.cls,
                badly_tracked_frames=track.badly_tracked_frames,
                median_translation=median_translation,
                num_poses=len(track.poses),
                dist_from_cam=track.dist_from_cam,
                cam=stereo_cam,
            )
            LOGGER.debug("Dist/max. dist: %f/%f", dist, max_dist)
            if not np.isfinite(dist) or dist > max_dist:
                # invalid distance
                LOGGER.debug("Invalid distance!")
                continue
            cost_matrix[detection_id][j] = 1 / dist

    first_indices, second_indices = linear_sum_assignment(cost_matrix, maximize=True)
    for row_idx, col_idx in zip(first_indices, second_indices):
        detection_id = row_idx
        track_id = list(unmatched_tracks)[col_idx]

        inv_dist = cost_matrix[row_idx, col_idx].sum()
        if inv_dist:  # initialized to 0
            track = tracks[track_id]

            track_ids_match = track_id == detections[detection_id].left.track_id
            LOGGER.debug(
                "Matched detection %d to track %d with dist of %f. Track ids match: %s",
                detection_id,
                track_id,
                1 / inv_dist,
                track_ids_match,
            )
            if not track_ids_match:
                track_id_mapping[detections[detection_id].left.track_id] = track_id
            matches.append(TrackMatch(track_id=track_id, detection_id=detection_id,))
            matched_tracks.add(track_id)
            matched_detections.add(detection_id)
    unmatched_detections = set(range(len(detections))).difference(matched_detections)
    unmatched_tracks = set(tracks).difference(matched_tracks)
    LOGGER.debug("%d valid track match(es) in total", len(matched_tracks))
    LOGGER.debug(
        "%d unmatched detection(s) after further 3D association: %s",
        len(unmatched_detections),
        unmatched_detections,
    )
    LOGGER.debug(
        "%d unmatched track(s) after further 3D association: %s ",
        len(unmatched_tracks),
        unmatched_tracks,
    )

    if config.TRUST_2D == "no":
        # create new tracks from remaining detections
        track_id = max(all_track_ids, default=0) + 1
        for detection_id in unmatched_detections:
            track_id += 1
            detection = detections[detection_id]
            LOGGER.debug("Creating new track with id %d", track_id)
            matched_detections.add(detection_id)
            matches.append(TrackMatch(track_id=track_id, detection_id=detection_id))

    return matches, unmatched_tracks, pnp_poses


@timer
def step(
    new_detections: List[StereoObjectDetection],
    stereo_image: StereoImage,
    object_tracks: Dict[int, ObjectTrack],
    process_match: FeatureMatcher,
    stereo_cam: StereoCamera,
    all_poses: Dict[ImageId, np.ndarray],
    img_id: ImageId,
    current_cam_pose: np.ndarray,
    tracks_to_run_ba: List[TrackId],
    all_track_ids: Set[TrackId],
    track_id_mapping: Dict[TrackId, TrackId],
    img_shape: Tuple[int, int],
) -> Tuple[
    Dict[int, ObjectTrack], List[List[Feature]], List[List[Feature]], List[List[Match]]
]:
    all_left_features = []
    all_right_features = []
    all_stereo_matches = []
    LOGGER.debug("-----------------------------------------")
    LOGGER.debug("Running step for image %d", img_id)
    LOGGER.debug("Current ego pose:\n%s", current_cam_pose)
    LOGGER.debug("Current track ids: %s", list(object_tracks.keys()))
    if not config.TRUST_2D == "yes":
        matches, unmatched_tracks, cached_pnp_poses = _improve_association(
            detections=new_detections,
            tracks=object_tracks,
            T_world_cam=current_cam_pose,
            stereo_cam=stereo_cam,
            stereo_image=stereo_image,
            img_id=img_id,
            all_track_ids=all_track_ids,
            track_id_mapping=track_id_mapping,
        )
    else:
        matches = [
            TrackMatch(track_id=track_idx, detection_id=detection_idx)
            for detection_idx, track_idx in enumerate(
                map(lambda x: x.left.track_id, new_detections)
            )
        ]
        unmatched_tracks = set(object_tracks.keys()).difference(
            set([x.left.track_id for x in new_detections])
        )
        cached_pnp_poses = {}

    for match in matches:
        # if new track ids are present, the tracks need to be added to the object_tracks
        if object_tracks.get(match.track_id) is None:
            LOGGER.debug("Added track with ID %d", match.track_id)
            object_tracks[match.track_id] = ObjectTrack(
                cls=new_detections[match.detection_id].left.cls,
                masks=(
                    new_detections[match.detection_id].left.mask,
                    new_detections[match.detection_id].right.mask,
                ),
                poses={img_id: current_cam_pose},
            )
        else:
            object_tracks[match.track_id].masks = (
                new_detections[match.detection_id].left.mask,
                new_detections[match.detection_id].right.mask,
            )
    # per match, match features
    LOGGER.debug("%d matches with object tracks", len(matches))

    # TODO: currently disabled bc slower than single threaded and single process
    with pathos.threading.ThreadPool(nodes=len(matches) if False else 1) as executor:
        matched_futures_to_track_id = {}
        unmatched_futures_to_track_id = {}
        for track_id in unmatched_tracks:
            track = object_tracks[track_id]
            track.badly_tracked_frames += 1
            track_logger = _get_track_logger(str(track_id))
            track_logger.debug(
                "Increased badly tracked frames to %d", track.badly_tracked_frames
            )
            unmatched_futures_to_track_id[
                executor.apipe(
                    _add_constant_motion_to_track,
                    track=track,
                    img_id=img_id,
                    T_world_cam=current_cam_pose,
                    track_id=track_id,
                    stereo_cam=stereo_cam,
                    img_shape=img_shape,
                )
            ] = track_id
        for match in matches:
            detection = new_detections[match.detection_id]
            track = object_tracks[match.track_id]
            run_ba = match.track_id in tracks_to_run_ba
            matched_futures_to_track_id[
                executor.apipe(
                    process_match,
                    track=track,
                    detection=detection,
                    all_poses=all_poses,
                    track_id=match.track_id,
                    stereo_cam=stereo_cam,
                    img_id=img_id,
                    stereo_image=stereo_image,
                    current_cam_pose=current_cam_pose,
                    run_ba=run_ba,
                    cached_pnp_poses=cached_pnp_poses,
                )
            ] = match.track_id

        for future, track_id in unmatched_futures_to_track_id.items():
            track = future.get()
            object_tracks[track_id] = track

        for future, track_id in matched_futures_to_track_id.items():
            track, left_features, right_features, stereo_matches = future.get()
            object_tracks[track_id] = track
            all_left_features.append(left_features)
            all_right_features.append(right_features)
            all_stereo_matches.append(stereo_matches)

    # Set old tracks inactive
    old_tracks = set()
    num_deactivated = 0
    for track_id, track in object_tracks.items():
        if (
            not track.active
            or track.badly_tracked_frames > config.KEEP_TRACK_FOR_N_FRAMES_AFTER_LOST
            or track.badly_tracked_frames > (0.75 * len(track.poses))
        ):
            num_deactivated += 1
            old_tracks.add(track_id)

    LOGGER.debug("Deactivated %d tracks", num_deactivated)
    LOGGER.debug("Finished step %d", img_id)
    LOGGER.debug("=" * 90)
    return (
        object_tracks,
        all_left_features,
        all_right_features,
        all_stereo_matches,
        old_tracks,
    )


def _compute_estimated_trajectories(
    object_tracks: Dict[TrackId, ObjectTrack], all_poses: Dict[ImageId, np.ndarray]
) -> Tuple[Dict[TrackId, Dict[ImageId, Location]]]:
    offline_trajectories_world = {}
    offline_trajectories_cam = {}
    online_trajectories_world = {}
    online_trajectories_cam = {}
    for track_id, track in object_tracks.items():
        offline_trajectory_world = {}
        offline_trajectory_cam = {}
        online_trajectory_world = {}
        online_trajectory_cam = {}
        for img_id, pose_world_obj in track.poses.items():
            Tr_world_cam = all_poses[img_id]
            object_center = track.pcl_centers.get(img_id)
            if object_center is None:
                continue
            object_center_world_offline = pose_world_obj @ to_homogeneous(object_center)
            object_center_cam_offline = (
                np.linalg.inv(Tr_world_cam) @ object_center_world_offline
            )
            offline_trajectory_world[int(img_id)] = tuple(
                from_homogeneous(object_center_world_offline).tolist()
            )
            offline_trajectory_cam[int(img_id)] = tuple(
                from_homogeneous(object_center_cam_offline).tolist()
            )
        for img_id, object_center_world_online in track.locations.items():
            Tr_world_cam = all_poses[img_id]
            object_center_cam_online = np.linalg.inv(Tr_world_cam) @ to_homogeneous(
                object_center_world_online
            )
            online_trajectory_world[int(img_id)] = tuple(
                object_center_world_online.tolist()
            )
            online_trajectory_cam[int(img_id)] = tuple(
                from_homogeneous(object_center_cam_online).tolist()
            )
        offline_trajectories_world[int(track_id)] = offline_trajectory_world
        offline_trajectories_cam[int(track_id)] = offline_trajectory_cam
        online_trajectories_world[int(track_id)] = online_trajectory_world
        online_trajectories_cam[int(track_id)] = online_trajectory_cam
    return (
        (offline_trajectories_world, offline_trajectories_cam),
        (online_trajectories_world, online_trajectories_cam),
    )
