""" Core code for BAMOT
"""
import concurrent.futures
import copy
import logging
import queue
import time
from threading import Event
from typing import Dict, Iterable, List, Tuple

import cv2
import numpy as np
from hungarian_algorithm import algorithm as ha
from shapely.geometry import Polygon

from bamot.core.base_types import (CameraParameters, Feature, FeatureMatcher,
                                   ImageId, Landmark, Match, ObjectTrack,
                                   Observation, StereoCamera, StereoImage,
                                   StereoObjectDetection, TrackMatch,
                                   get_camera_parameters_matrix)
from bamot.core.optimization import object_bundle_adjustment
from bamot.util.cv import (back_project, dilate_mask, from_homogeneous_pt,
                           get_convex_hull, get_convex_hull_mask, mask_img,
                           project_landmarks, to_homogeneous_pt, triangulate)

LOGGER = logging.getLogger("CORE:MOT")

MAX_DIST = 25


def max_lm(track):
    if isinstance(track, ObjectTrack):
        if not track.landmarks:
            return None
        return max(np.linalg.norm(lm.pt_3d) for lm in track.landmarks.values())
    else:
        if not track:
            return None
        return max(np.linalg.norm(lm.pt_3d) for lm in track.values())


def _localize_object(
    left_features: List[Feature],
    track_matches: List[Match],
    landmark_mapping: Dict[int, int],
    landmarks: Dict[int, Landmark],
    T_cam_obj: np.ndarray,
    camera_params: CameraParameters,
    num_iterations: int = 2000,
    reprojection_error: float = 1.0,
) -> np.ndarray:
    pts_3d = []
    pts_2d = []

    LOGGER.debug(
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
    if successful:
        LOGGER.debug("Optimization successful! Found %d inliers", len(inliers))
        LOGGER.debug("Running optimization with inliers...")
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
            return optimized_pose
    LOGGER.debug("Optimization failed...")
    return T_cam_obj


def _add_new_landmarks_and_observations(
    landmarks: Dict[int, Landmark],
    track_matches: List[Match],
    landmark_mapping: Dict[int, int],
    stereo_matches: List[Match],
    left_features: List[Feature],
    right_features: List[Feature],
    stereo_cam: StereoCamera,
    T_obj_cam: np.ndarray,
    img_id: int,
) -> Dict[int, Landmark]:
    # add new observations to existing landmarks
    # explicitly add stereo features --> look at visnav
    already_added_features = []
    left_timecam_id = (img_id, 0)
    right_timecam_id = (img_id, 1)
    stereo_match_dict = {}
    for left_feature_idx, right_feature_idx in stereo_matches:
        stereo_match_dict[left_feature_idx] = right_feature_idx

    for features_idx, landmark_idx in track_matches:
        feature = left_features[features_idx]
        pt_obj = landmarks[landmark_mapping[landmark_idx]].pt_3d
        pt_cam = from_homogeneous_pt(
            np.linalg.inv(T_obj_cam) @ to_homogeneous_pt(pt_obj)
        )
        z = pt_cam[2]
        if (
            z < 0.5 or np.linalg.norm(pt_cam) > MAX_DIST
        ):  # don't add landmark matches with great discrepancy
            continue
        # print(landmark_mapping[landmark_idx])
        # print(pt_cam)
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
            descriptor=feature.descriptor, pt_2d=feature_pt, timecam_id=left_timecam_id,
        )
        already_added_features.append(features_idx)
        landmarks[landmark_mapping[landmark_idx]].observations.append(obs)
    LOGGER.debug("Added %d observations", len(already_added_features))

    # add new landmarks
    landmark_id = max(landmarks.keys(), default=-1) + 1
    created_landmarks = 0
    bad_matches = []
    for left_feature_idx, right_feature_idx in stereo_matches:
        # check whether landmark exists already
        if left_feature_idx in already_added_features:
            continue
        # if not, triangulate
        left_feature = left_features[left_feature_idx]
        right_feature = right_features[right_feature_idx]
        left_pt = np.array([left_feature.u, left_feature.v])
        right_pt = np.array([right_feature.u, right_feature.v])
        feature_pt = np.array([left_feature.u, left_feature.v, right_feature.u])
        if not np.allclose(left_feature.v, right_feature.v, atol=1):
            # match doesn't fullfill epipolar constraint
            bad_matches.append((left_feature_idx, right_feature_idx))
            continue
        vec_left = back_project(stereo_cam.left, left_pt)
        vec_right = back_project(stereo_cam.right, right_pt)
        R_left_right = stereo_cam.T_left_right[:3, :3]
        t_left_right = stereo_cam.T_left_right[:3, 3].reshape(3, 1)
        try:
            pt_3d_left_cam = triangulate(
                vec_left, vec_right, R_left_right, t_left_right
            )
            # print("tri:")
            # print(pt_3d_left_cam)
        except np.linalg.LinAlgError as e:
            # LOGGER.error("Encountered error during triangulation: %s", e)
            bad_matches.append((left_feature_idx, right_feature_idx))
            continue
        if pt_3d_left_cam[-1] < 0.5 or np.linalg.norm(pt_3d_left_cam) > MAX_DIST:
            # triangulated point should not be behind camera (or very close) or too far away
            bad_matches.append((left_feature_idx, right_feature_idx))
            continue
        pt_3d_obj = from_homogeneous_pt(
            T_obj_cam @ to_homogeneous_pt(pt_3d_left_cam)
        ).reshape(3, 1)
        # print(landmark_id)
        # print(pt_3d_obj)
        # print("obj:")
        # print(pt_3d_obj)
        # create new landmark
        obs = Observation(
            descriptor=left_feature.descriptor,
            pt_2d=feature_pt,
            timecam_id=left_timecam_id,
        )
        landmark = Landmark(pt_3d_obj, [obs])
        landmarks[landmark_id] = landmark
        landmark_id += 1
        created_landmarks += 1

    for match in bad_matches:
        stereo_matches.remove(match)
    LOGGER.debug("Created %d landmarks", created_landmarks)
    return landmarks


def _get_object_associations(
    detections: List[StereoObjectDetection], object_tracks: Dict[int, ObjectTrack]
) -> List[TrackMatch]:
    graph: Dict[int, Dict[int, float]] = {}
    # TODO: fix
    for i, detection in enumerate(detections):
        # compute IoU for left seg
        poly_detection = Polygon(detection.left.convex_hull)
        graph[i] = {}
        for j, track in object_tracks.items():
            projected_landmarks = project_landmarks(track.landmarks)
            poly_track = Polygon(get_convex_hull(projected_landmarks))
            iou = (poly_detection.intersection(poly_track)) / (
                poly_detection.union(poly_track)
            )
            graph[i][j] = iou

    # get matches from hulgarian algo
    matches = ha.find_matching(graph, matching_type="max", return_type="list")
    track_matches = []
    for match in matches:
        detection_idx = match[0][0]
        track_idx = match[0][1]
        track_match = TrackMatch(track_index=track_idx, detection_index=detection_idx)
        track_matches.append(track_match)
    return track_matches


def _get_median_descriptor(observations: List[Observation], norm: int) -> np.ndarray:
    distances = np.zeros((len(observations), len(observations)))
    for i, obs in enumerate(observations):
        for j in range(i, len(observations)):
            other_obs = observations[j]
            # calculate distance between i and j
            dist = np.linalg.norm(obs.descriptor - other_obs.descriptor, ord=norm)
            # do for all combinations
            distances[i, j] = dist
            distances[j, i] = dist
    best_median = None
    best_idx = 0
    for i, obs in enumerate(observations):
        dist_per_descriptor = distances[i]
        median = np.median(dist_per_descriptor)
        if not best_median or median < best_median:
            best_median = median
            best_idx = i
    return observations[best_idx].descriptor


def _get_features_from_landmarks(
    landmarks: Dict[int, Landmark]
) -> Tuple[List[Feature], Dict[int, int]]:
    # todo: refactor
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


def run(
    images: Iterable[StereoImage],
    detections: Iterable[List[StereoObjectDetection]],
    feature_matcher: FeatureMatcher,
    stereo_cam: StereoCamera,
    slam_data: queue.Queue,
    shared_data: queue.Queue,
    stop_flag: Event,
    next_step: Event,
    continuous: bool,
):
    object_tracks: Dict[int, ObjectTrack] = {}
    LOGGER.info("Starting MOT run")

    for it, (stereo_image, new_detections) in enumerate(zip(images, detections)):
        while not continuous and not next_step.is_set():
            time.sleep(0.05)
        next_step.clear()
        all_poses = slam_data.get()
        current_pose = all_poses[it]
        object_tracks, all_left_features, all_right_features, all_stereo_matches = step(
            new_detections=new_detections,
            stereo_image=stereo_image,
            object_tracks=copy.deepcopy(object_tracks),  # weird behavior w/o deepcopy
            matcher=feature_matcher,
            stereo_cam=stereo_cam,
            img_id=it,
            current_cam_pose=current_pose,
            all_poses=all_poses,
        )
        shared_data.put(
            {
                "object_tracks": copy.deepcopy(object_tracks),
                "stereo_image": stereo_image,
                "all_left_features": all_left_features,
                "all_right_features": all_right_features,
                "all_stereo_matches": all_stereo_matches,
            }
        )
    stop_flag.set()


def step(
    new_detections: List[StereoObjectDetection],
    stereo_image: StereoImage,
    object_tracks: Dict[int, ObjectTrack],
    matcher: FeatureMatcher,
    stereo_cam: StereoCamera,
    all_poses: Dict[ImageId, np.ndarray],
    img_id: ImageId,
    current_cam_pose: np.ndarray,
) -> Tuple[
    Dict[int, ObjectTrack], List[List[Feature]], List[List[Feature]], List[List[Match]]
]:
    img_shape = stereo_image.left.shape
    all_left_features = []
    all_right_features = []
    all_stereo_matches = []
    LOGGER.debug("Running step for image %d", img_id)
    if all(map(lambda x: x.left.track_id is None, new_detections)):
        # no track ids yet
        matches = _get_object_associations(new_detections, object_tracks)
    else:
        # track ids already exist
        matches = [
            TrackMatch(track_index=track_idx, detection_index=detection_idx)
            for detection_idx, track_idx in enumerate(
                map(lambda x: x.left.track_id, new_detections)
            )
        ]
        # if new track ids are present, the tracks need to be added to the object_tracks
        for match in matches:
            if object_tracks.get(match.track_index) is None:
                LOGGER.debug("Added track with index %d", match.track_index)
                object_tracks[match.track_index] = ObjectTrack(
                    landmarks={}, poses={img_id: current_cam_pose},
                )
    # per match, match features
    active_tracks = []
    matched_detections = []
    LOGGER.debug("%d matches with object tracks", len(matches))
    # TODO: run in parallel
    with concurrent.futures.ProcessPoolExecutor() as executor:
        for match in matches:

            detection = new_detections[match.detection_index]
            track = object_tracks[match.track_index]
            active_tracks.append(match.track_index)
            matched_detections.append(match.detection_index)

            track, left_features, right_features, stereo_matches = _process_match(
                track=track,
                detection=detection,
                all_poses=all_poses,
                track_index=match.track_index,
                stereo_cam=stereo_cam,
                img_id=img_id,
                img_shape=img_shape,
                stereo_image=stereo_image,
                matcher=matcher,
                current_cam_pose=current_cam_pose,
            )
            object_tracks[match.track_index] = track
            all_left_features.append(left_features)
            all_right_features.append(right_features)
            all_stereo_matches.append(stereo_matches)
    # stereo_image.left = mask_img(left_obj_mask, stereo_image.left, dilate=20)

    # Set old tracks inactive
    old_tracks = set(object_tracks.keys()).difference(set(active_tracks))
    num_deactivated = 0
    for track_id in old_tracks:
        if object_tracks[track_id].active:
            object_tracks[track_id].active = False
            num_deactivated += 1
    LOGGER.debug("Deactivated %d tracks", num_deactivated)
    # add new tracks
    # new_tracks = set(range(len(new_detections))).difference(set(matched_detections))
    # LOGGER.debug("Adding %d new tracks", len(new_tracks))
    # for detection_id in new_tracks:
    #    detection = new_detections[detection_id]
    #    track_id = max(object_tracks.keys()) + 1
    #    # mask out object from image
    #    left_obj_mask = get_convex_hull_mask(
    #        detection.left.convex_hull, img_shape=img_shape
    #    )
    #    right_obj_mask = get_convex_hull_mask(
    #        detection.right.convex_hull, img_shape=img_shape
    #    )
    #    left_obj = mask_img(left_obj_mask, stereo_image.left, dilate=True)
    #    right_obj = mask_img(right_obj_mask, stereo_image.right, dilate=True)

    #    # detect features per new detection
    #    left_features = matcher.detect_features(left_obj, left_obj_mask)
    #    right_features = matcher.detect_features(right_obj, right_obj_mask)
    #    detection.left.features = left_features
    #    detection.right.features = right_features
    #    # match stereo features
    #    stereo_matches = matcher.match_features(left_features, right_features)
    #    # match left features with track features
    #    # initial pose is camera pose
    #    current_pose = current_cam_pose
    #    poses = {img_id: current_pose}
    #    # initial landmarks are triangulated stereo matches
    #    landmarks = _add_new_landmarks_and_observations(
    #        landmarks={},
    #        track_matches=[],
    #        stereo_matches=stereo_matches,
    #        left_features=left_features,
    #        right_features=right_features,
    #        stereo_cam=stereo_cam,
    #        T_obj_cam=np.identity(4),
    #        img_id=img_id,
    #    )
    #    obj_track = ObjectTrack(landmarks=landmarks, poses=poses,)
    #    object_tracks[track_id] = obj_track

    LOGGER.debug("Finished step")
    LOGGER.debug("=" * 90)
    return object_tracks, all_left_features, all_right_features, all_stereo_matches


# TODO: improve right mask for detections (i.e. by matching convex hull points from left image --> how to match true contour?)
# Better feature matcher?
# Better triangulation?
# why is superpoint not working?
# TODO: display landmark matches
# for loop (for match in matches) in separate threads/processes
def _process_match(
    track,
    detection,
    all_poses,
    track_index,
    stereo_cam,
    img_id,
    img_shape,
    stereo_image,
    matcher,
    current_cam_pose,
):
    track.active = True
    # mask out object from image
    left_obj_mask = get_convex_hull_mask(
        np.array(detection.left.convex_hull), img_shape=img_shape
    )
    right_obj_mask = get_convex_hull_mask(
        np.array(detection.right.convex_hull), img_shape=img_shape
    )
    left_features = matcher.detect_features(stereo_image.left, left_obj_mask)
    LOGGER.debug("Detected %d features on left object", len(left_features))
    # TODO: why does using left_obj_mask with no dilation work best here?
    right_features = matcher.detect_features(
        stereo_image.right, dilate_mask(left_obj_mask, num_pixels=0)
    )
    LOGGER.debug("Detected %d features on right object", len(right_features))
    detection.left.features = left_features
    detection.right.features = right_features
    # match stereo features
    stereo_matches = matcher.match_features(left_features, right_features)
    LOGGER.debug("%d stereo matches", len(stereo_matches))
    # match left features with track features
    features, lm_mapping = _get_features_from_landmarks(track.landmarks)
    track_matches = matcher.match_features(left_features, features)
    LOGGER.debug("%d track matches", len(track_matches))
    # localize object
    T_world_obj1 = track.poses[max(track.poses.keys())]
    # add motion if at least two poses are present
    if len(track.poses) >= 2:
        T_world_obj0 = track.poses[max(track.poses.keys()) - 1]
        T_obj0_obj1 = np.linalg.inv(T_world_obj0) @ T_world_obj1
        # print(track.poses)
        # print(T_world_obj0)
        # print(T_world_obj1)
        # print("Relative transform: ", T_obj0_obj1)
        T_world_obj = T_world_obj1 @ T_obj0_obj1  # constant motion assumption
        # print(T_world_obj)
    else:
        T_world_obj = T_world_obj1
    T_world_cam = current_cam_pose
    T_obj_cam = np.linalg.inv(T_world_obj) @ T_world_cam
    if len(track_matches) >= 5 and track_index != -1:
        T_cam_obj = _localize_object(
            left_features=left_features,
            track_matches=track_matches,
            landmark_mapping=lm_mapping,
            landmarks=copy.deepcopy(track.landmarks),
            T_cam_obj=np.linalg.inv(T_obj_cam),
            camera_params=stereo_cam.left,
        )
        T_obj_cam = np.linalg.inv(T_cam_obj)
    T_world_obj = T_world_cam @ np.linalg.inv(T_obj_cam)
    track.poses[img_id] = T_world_obj
    # add new landmark observations from track matches
    # add new landmarks from stereo matches
    track.landmarks = _add_new_landmarks_and_observations(
        landmarks=copy.deepcopy(track.landmarks),
        track_matches=track_matches,
        landmark_mapping=lm_mapping,
        stereo_matches=stereo_matches,
        left_features=left_features,
        right_features=right_features,
        stereo_cam=stereo_cam,
        img_id=img_id,
        T_obj_cam=T_obj_cam,
    )
    # BA optimizes landmark positions w.r.t. object and object position over time
    # -> SLAM optimizes motion of camera
    # cameras maps a timecam_id (i.e. frame + left/right) to a camera pose and camera parameters
    if len(track.poses) > 3:
        LOGGER.debug("Running BA")
        track = object_bundle_adjustment(
            object_track=copy.deepcopy(track),
            all_poses=all_poses,
            stereo_cam=stereo_cam,
        )
    # remove outlier landmarks
    if True and track_index != -1 and track.landmarks:
        landmarks_to_remove = []
        points = []
        for landmark in track.landmarks.values():
            points.append(landmark.pt_3d)
        points = np.array(points)
        cluster_center = np.mean(points, axis=0)
        stddev = np.std(points, axis=0)
        for lid, lm in track.landmarks.items():
            if np.linalg.norm(lm.pt_3d - cluster_center) > np.linalg.norm(3 * stddev):
                landmarks_to_remove.append(lid)
        LOGGER.debug("Removing %d outlier landmarks", len(landmarks_to_remove))
        for lid in landmarks_to_remove:
            track.landmarks.pop(lid)
        if len(track.landmarks) < 25:
            track.active = False
    return track, left_features, right_features, stereo_matches
