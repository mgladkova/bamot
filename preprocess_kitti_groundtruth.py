import argparse
import glob
from functools import partial
from pathlib import Path
from typing import List

import cv2
import numpy as np

from bamot.core.base_types import (Camera, ObjectDetection, StereoCamera,
                                   StereoImage)
from bamot.core.preprocessing import preprocess_frame


def _get_screen_size():
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    width, height = root.winfo_screenwidth(), root.winfo_screenheight()
    return width, height


def _project(pt_3d_cam: np.ndarray, intrinsics: np.ndarray) -> np.ndarray:
    pt_3d_cam = pt_3d_cam[:4] / pt_3d_cam[3]
    pt_2d_hom = intrinsics[:3, :3] @ pt_3d_cam[:3]
    return pt_2d_hom


def _back_project(pt_2d: np.ndarray, intrinsics: np.ndarray) -> np.ndarray:
    pt_2d = pt_2d.reshape(2, 1)
    fx = intrinsics[0, 0]
    fy = intrinsics[1, 1]
    cx = intrinsics[0, 2]
    cy = intrinsics[1, 2]
    u, v = map(float, pt_2d)
    mx = (u - cx) / fx
    my = (v - cy) / fy
    length = np.sqrt(mx ** 2 + my ** 2 + 1)
    return (np.array([mx, my, 1]) / length).reshape(3, 1)


def get_cameras_from_kitti(calib_file: Path) -> StereoCamera:
    with open(calib_file.as_posix(), "r") as fd:
        for line in fd:
            cols = line.split(" ")
            name = cols[0]
            if "R_02" in name:
                R02 = np.array(list(map(float, cols[1:]))).reshape(3, 3)
            elif "T_02" in name:
                t02 = np.array(list(map(float, cols[1:]))).reshape(3)
            elif "R_03" in name:
                R03 = np.array(list(map(float, cols[1:]))).reshape(3, 3)
            elif "T_03" in name:
                t03 = np.array(list(map(float, cols[1:]))).reshape(3)
            elif "P_rect_02" in name:
                intrinsics_02 = np.array(list(map(float, cols[1:]))).reshape(3, 4)
            elif "P_rect_03" in name:
                intrinsics_03 = np.array(list(map(float, cols[1:]))).reshape(3, 4)
    T02 = np.identity(4)
    T02[:3, :3] = R02
    T02[:3, 3] = t02
    T03 = np.identity(4)
    T03[:3, :3] = R03
    T03[:3, 3] = t03
    T23 = np.linalg.inv(T02) @ T03
    left_cam = Camera(
        project=partial(_project, intrinsics=intrinsics_02),
        back_project=partial(_back_project, intrinsics=intrinsics_02),
    )
    right_cam = Camera(
        project=partial(_project, intrinsics=intrinsics_03),
        back_project=partial(_back_project, intrinsics=intrinsics_03),
    )
    return StereoCamera(left_cam, right_cam, T23)


def get_gt_obj_segmentations_from_kitti(instance_file: str) -> List[ObjectDetection]:
    img = np.array(cv2.imread(instance_file, cv2.IMREAD_ANYDEPTH))
    obj_ids = np.unique(img)
    obj_detections = []
    for obj_id in obj_ids:
        if obj_id in [0, 10000]:
            continue
        track_id = obj_id % 1000
        obj_mask = img == obj_id
        convex_hull = cv2.convexHull(np.argwhere(obj_mask), returnPoints=True).reshape(
            -1, 2
        )
        obj_mask = ObjectDetection(
            convex_hull=tuple(convex_hull.tolist()), track_id=track_id
        )
        obj_detections.append(obj_mask)
    return obj_detections


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-s",
        help="the scene to preprocess, default is 1",
        choices=range(0, 20),
        type=int,
        default=0,
    )
    parser.add_argument(
        "-d",
        help="the path to the training data of kitti's tracking dataset"
        + " (default is `./data/KITTI/tracking/training`",
        type=str,
    )
    parser.add_argument(
        "-o",
        help="where to save the masked data (default is `./data/KITTI/tracking/training/preprocessed`)",
        type=str,
    )
    parser.add_argument(
        "--no-save",
        help="flag to disable saving of preprocessed data",
        action="store_true",
    )
    parser.add_argument(
        "--no-view",
        help="flag to disable viewing the preprocessed data while it's being generated (quit execution by hitting `q`)",
        action="store_true",
    )
    args = parser.parse_args()
    scene = str(args.s).zfill(4)
    if not args.d:
        base_path = Path(__file__).parent / "data" / "KITTI" / "tracking" / "training"
    else:
        base_path = Path(args.d)
    if not args.no_save:
        if not args.o:
            save_path = base_path / "preprocessed"
        else:
            save_path = Path(args.o)
        save_path_slam = save_path / "slam"
        save_path_slam_left = save_path_slam / "image_02" / scene
        save_path_slam_right = save_path_slam / "image_03" / scene
        save_path_mot = save_path / "mot" / scene
        save_path_slam_left.mkdir(parents=True, exist_ok=True)
        save_path_slam_right.mkdir(parents=True, exist_ok=True)
        save_path_mot.mkdir(parents=True, exist_ok=True)

    instance_file = base_path / "instances" / scene
    calib_file = base_path / "calib_cam_to_cam.txt"
    left_img_path = base_path / "image_02" / scene
    right_img_path = base_path / "image_03" / scene
    left_imgs = sorted(glob.glob(left_img_path.as_posix() + "/*.png"))
    right_imgs = sorted(glob.glob(right_img_path.as_posix() + "/*.png"))
    instances = sorted(glob.glob(instance_file.as_posix() + "/*.png"))
    stereo_cam = get_cameras_from_kitti(calib_file)
    if not args.no_view:
        width, height = _get_screen_size()
        cv2.namedWindow("Preprocessed", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Preprocessed", (width // 2, height // 2))
    for idx, (l, r, instance_file) in enumerate(zip(left_imgs, right_imgs, instances)):
        left_img = cv2.imread(l, cv2.IMREAD_COLOR)
        right_img = cv2.imread(r, cv2.IMREAD_COLOR)
        object_detections = get_gt_obj_segmentations_from_kitti(instance_file)
        masked_stereo_image_slam = preprocess_frame(
            StereoImage(left_img, right_img),
            stereo_cam,
            object_detections=object_detections,
        )
        left_mot_mask = masked_stereo_image_slam.left == 0
        right_mot_mask = masked_stereo_image_slam.right == 0
        left_img_mot = np.zeros(left_img.shape, dtype=np.uint8)
        left_img_mot[left_mot_mask] = left_img[left_mot_mask]
        right_img_mot = np.zeros(right_img.shape, dtype=np.uint8)
        right_img_mot[right_mot_mask] = right_img[right_mot_mask]
        masked_stereo_image_mot = StereoImage(left_img_mot, right_img_mot)
        result_slam = np.hstack(
            [masked_stereo_image_slam.left, masked_stereo_image_slam.right]
        )
        result_mot = np.hstack(
            [masked_stereo_image_mot.left, masked_stereo_image_mot.right]
        )
        result = np.vstack([result_slam, result_mot])
        if not args.no_view:
            cv2.imshow("Preprocessed", result)
            if cv2.waitKey(1) == ord("q"):
                cv2.destroyAllWindows()
                break
        if not args.no_save:
            img_name = l.split("/")[-1]
            img_id = img_name.split(".")[0]
            slam_left_path = save_path_slam_left / img_name
            slam_right_path = save_path_slam_right / img_name
            cv2.imwrite(slam_left_path.as_posix(), masked_stereo_image_slam.left)
            cv2.imwrite(slam_right_path.as_posix(), masked_stereo_image_slam.right)
            obj_det_json = ObjectDetection.schema().dumps(
                object_detections, many=True, indent=4
            )
            obj_det_path = (save_path_mot / img_id).as_posix() + ".json"
            with open(obj_det_path, "w") as fd:
                fd.write(obj_det_json)
