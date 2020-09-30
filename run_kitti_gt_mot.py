"""Script for BAMOT with GT data from KITTI and mocked SLAM system.
"""
import argparse
import datetime
import glob
import json
import logging
import multiprocessing as mp
import queue
import subprocess
import threading
import time
import warnings
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

import colorlog
import cv2
import numpy as np
import tqdm

from bamot.config import CONFIG as config
from bamot.config import get_config_dict, update_config
from bamot.core.base_types import (ObjectDetection, StereoImage,
                                   StereoObjectDetection)
from bamot.core.mot import run
from bamot.util.kitti import (get_cameras_from_kitti, get_gt_poses_from_kitti,
                              get_label_data_from_kitti)
from bamot.util.misc import TqdmLoggingHandler
from bamot.util.viewer import run as run_viewer

warnings.filterwarnings(action="ignore")

LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "ERROR": logging.ERROR}
LOGGER = colorlog.getLogger()
HANDLER = TqdmLoggingHandler()
HANDLER.setFormatter(
    colorlog.ColoredFormatter(
        "%(asctime)s | %(log_color)s%(name)s:%(levelname)s%(reset)s: %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "white",
            "SUCCESS:": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        },
    )
)
LOGGER.handlers = [HANDLER]


def _fake_slam(
    slam_data: Union[queue.Queue, mp.Queue], gt_poses: List[np.ndarray], offset: int
):
    all_poses = []
    for i, pose in enumerate(gt_poses):
        all_poses = gt_poses[: i + 1 + offset]
        slam_data.put(all_poses)
        time.sleep(20 / 1000)  # 20 ms or 50 Hz
    LOGGER.debug("Finished adding fake slam data")


def _get_image_shape(kitti_path: str) -> Tuple[int, int]:
    left_img_path = Path(kitti_path) / "image_02" / scene
    left_imgs = sorted(glob.glob(left_img_path.as_posix() + "/*.png"))
    img_shape = cv2.imread(left_imgs[0], cv2.IMREAD_COLOR).astype(np.uint8).shape
    return img_shape


def _get_image_stream(
    kitti_path: str,
    scene: str,
    stop_flag: Union[threading.Event, mp.Event],
    offset: int,
) -> Iterable[Tuple[int, StereoImage]]:
    left_img_path = Path(kitti_path) / "image_02" / scene
    right_img_path = Path(kitti_path) / "image_03" / scene
    left_imgs = sorted(glob.glob(left_img_path.as_posix() + "/*.png"))
    right_imgs = sorted(glob.glob(right_img_path.as_posix() + "/*.png"))
    if not left_imgs or not right_imgs or len(left_imgs) != len(right_imgs):
        raise ValueError(
            f"No or differing amount of images found at {left_img_path.as_posix()} and {right_img_path.as_posix()}"
        )

    LOGGER.debug(
        "Found %d left images and %d right images", len(left_imgs), len(right_imgs)
    )
    LOGGER.debug("Starting at image %d", offset)
    img_id = offset
    for left, right in tqdm.tqdm(
        zip(left_imgs[offset:], right_imgs[offset:]), total=len(left_imgs[offset:]),
    ):
        left_img = cv2.imread(left, cv2.IMREAD_COLOR).astype(np.uint8)
        right_img = cv2.imread(right, cv2.IMREAD_COLOR).astype(np.uint8)
        yield img_id, StereoImage(left_img, right_img)
        img_id += 1
    LOGGER.debug("Setting stop flag")
    stop_flag.set()


def _get_detection_stream(
    obj_detections_path: Path,
    offset: int,
    img_shape: Tuple[int, int],
    object_ids: Optional[List[int]],
) -> Iterable[List[StereoObjectDetection]]:
    detection_files = sorted(glob.glob(obj_detections_path.as_posix() + "/*.json"))
    if not detection_files:
        raise ValueError(f"No detection files found at {obj_detections_path}")
    LOGGER.debug("Found %d detection files", len(detection_files))
    for f in detection_files[offset:]:
        with open(f, "r") as fd:
            json_data = fd.read()
        detections = StereoObjectDetection.schema().loads(json_data, many=True)
        if object_ids:
            detections = list(
                filter(lambda x: x.left.track_id in object_ids, detections)
            )
        yield detections
    LOGGER.debug("Finished yielding object detections")


if __name__ == "__main__":
    parser = argparse.ArgumentParser("BAMOT with GT KITTI data")
    parser.add_argument(
        "-v",
        "--verbosity",
        dest="verbosity",
        help="verbosity of output (default is INFO, surpresses most logs)",
        type=str,
        choices=["DEBUG", "INFO", "ERROR"],
        default="INFO",
    )
    parser.add_argument(
        "-s",
        "--scene",
        dest="scene",
        help="scene to run (default is 0)",
        type=int,
        default=0,
    )
    parser.add_argument(
        "-o",
        "--offset",
        dest="offset",
        help="The image number offset to use (i.e. start at image # <offset> instead of 0)",
        type=int,
        default=0,
    )
    parser.add_argument(
        "-nv",
        "--no-viewer",
        dest="no_viewer",
        help="Disable viewer",
        action="store_true",
    )
    parser.add_argument(
        "-mp",
        "--multiprocessing",
        help="Whether to run viewer in separate process (default separate thread).",
        dest="multiprocessing",
        action="store_true",
    )
    parser.add_argument(
        "-c",
        "--continuous",
        action="store_true",
        dest="continuous",
        help="Whether to run process continuously (default is next step via 'n' keypress).",
    )
    parser.add_argument(
        "-t",
        "--tags",
        type=str,
        nargs="+",
        help="One or several tags for the given run (default=`YEAR_MONTH_DAY-HOUR_MINUTE`)",
        default=[datetime.datetime.strftime(datetime.datetime.now(), "%Y_%m_%d-%H_%M")],
    )
    parser.add_argument(
        "--out",
        dest="out",
        type=str,
        help="""Where to save GT and estimated object trajectories
        (default: `<kitti>/trajectories/<scene>/<features>/<tags>[0]/<tags>[1]/.../<tags>[N]`)""",
    )
    parser.add_argument(
        "-id",
        "--indeces",
        dest="indeces",
        help="Use this to only track specific object ids",
        nargs="+",
    )
    parser.add_argument(
        "-r",
        "--record",
        dest="record",
        help="Record sequence from viewer at given path (ignored if `--no-viewer` is set)",
        type=str,
    )

    parser.add_argument(
        "--cam",
        dest="cam",
        help="Show objects in camera coordinates in viewer (instead of world)",
        action="store_true",
    )
    parser.add_argument(
        "-cfg",
        "--config",
        dest="config",
        help="Use different config file (default: `config.yaml` (if available))",
    )

    args = parser.parse_args()
    scene = str(args.scene).zfill(4)
    kitti_path = Path(config.KITTI_PATH)
    obj_detections_path = Path(config.DETECTIONS_PATH) / scene
    if args.config:
        update_config(args.config)
    LOGGER.setLevel(LOG_LEVELS[args.verbosity])

    LOGGER.info(30 * "+")
    LOGGER.info("STARTING BAMOT with GT KITTI data")
    LOGGER.info("SCENE: %s", scene)
    LOGGER.info("VERBOSITY LEVEL: %s", args.verbosity)
    LOGGER.info("USING MULTI-PROCESS: %s", args.multiprocessing)
    LOGGER.info("CONFIG:\n%s", json.dumps(get_config_dict(), indent=4))
    LOGGER.info(30 * "+")

    if args.multiprocessing:
        queue_class = mp.JoinableQueue
        flag_class = mp.Event
        process_class = mp.Process
    else:
        queue_class = queue.Queue
        flag_class = threading.Event
        process_class = threading.Thread
    shared_data = queue_class()
    returned_data = queue_class()
    slam_data = queue_class()
    stop_flag = flag_class()
    next_step = flag_class()
    next_step.set()
    img_shape = _get_image_shape(kitti_path)
    image_stream = _get_image_stream(kitti_path, scene, stop_flag, offset=args.offset)
    stereo_cam, T02 = get_cameras_from_kitti(kitti_path)
    gt_poses = get_gt_poses_from_kitti(kitti_path, scene)
    label_data = get_label_data_from_kitti(
        kitti_path, scene, poses=gt_poses, offset=args.offset
    )
    detection_stream = _get_detection_stream(
        obj_detections_path,
        img_shape=img_shape,
        offset=args.offset,
        object_ids=[int(idx) for idx in args.indeces] if args.indeces else None,
    )

    slam_process = process_class(
        target=_fake_slam, args=[slam_data, gt_poses, args.offset], name="Fake SLAM"
    )
    mot_process = process_class(
        target=run,
        kwargs={
            "images": image_stream,
            "detections": detection_stream,
            "stereo_cam": stereo_cam,
            "slam_data": slam_data,
            "shared_data": shared_data,
            "stop_flag": stop_flag,
            "next_step": next_step,
            "returned_data": returned_data,
            "continuous": args.continuous,
        },
        name="BAMOT",
    )
    LOGGER.debug("Starting fake SLAM thread")
    slam_process.start()
    LOGGER.debug("Starting MOT thread")
    mot_process.start()
    LOGGER.debug("Starting viewer")
    if args.no_viewer:
        while not stop_flag.is_set():
            shared_data.get(block=True)
            shared_data.task_done()
            next_step.set()
    else:
        run_viewer(
            shared_data=shared_data,
            stop_flag=stop_flag,
            next_step=next_step,
            gt_trajectories=label_data.world_positions,
            save_path=Path(args.record) if args.record else None,
            poses=gt_poses if args.cam else None,
        )
    LOGGER.info("No more frames - terminating processes")
    slam_process.join()
    LOGGER.debug("Joined fake SLAM thread")
    mot_process.join()
    LOGGER.debug("Joined MOT thread")
    estimated_trajectories_world, estimated_trajectories_cam = returned_data.get()
    returned_data.task_done()
    returned_data.join()
    if not args.out:
        out_path = kitti_path / "trajectories" / scene / config.FEATURE_MATCHER
        for tag in args.tags:
            out_path /= tag
    else:
        out_path = Path(args.out)

    # Save trajectories
    out_path.mkdir(exist_ok=True, parents=True)
    out_est_world = out_path / "est_trajectories_world.json"
    out_est_cam = out_path / "est_trajectories_cam.json"
    # estimated
    with open(out_est_world, "w") as fp:
        json.dump(estimated_trajectories_world, fp, indent=4, sort_keys=True)
    with open(out_est_cam, "w") as fp:
        json.dump(estimated_trajectories_cam, fp, indent=4, sort_keys=True)
    LOGGER.info(
        "Saved estimated object track trajectories to %s", out_path,
    )

    # Save config + git hash
    state_file = out_path / "state.json"
    with open(state_file, "w") as fp:
        state = {}
        state["CONFIG"] = get_config_dict()
        state["HASH"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        json.dump(state, fp)
        fp.write(json.dumps(state, indent=4))

    # Cleanly shutdown
    while not shared_data.empty():
        shared_data.get()
        shared_data.task_done()
    shared_data.join()
    LOGGER.info("FINISHED RUNNING KITTI GT MOT")
