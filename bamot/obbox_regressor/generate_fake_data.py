import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import tqdm
from bamot.config import CONFIG as cfg
from bamot.util.kitti import (LabelDataRow, get_gt_poses_from_kitti,
                              get_label_data_from_kitti)


def main(base_dir):
    for scene in tqdm.trange(21):
        out_fname = base_dir / (str(scene).zfill(4) + ".csv")
        pcl_dir = base_dir / str(scene).zfill(4)
        pcl_dir.mkdir(exist_ok=True, parents=True)
        scenes = []
        track_ids = []
        img_ids = []
        num_poses = []
        num_other_tracks = []
        fnames = []
        gt_poses = get_gt_poses_from_kitti(kitti_path=cfg.KITTI_PATH, scene=scene)
        label_data = get_label_data_from_kitti(
            kitti_path=cfg.KITTI_PATH, scene=scene, poses=gt_poses
        )
        for track_id, track_data in tqdm.tqdm(
            label_data.items(), position=1, total=len(label_data)
        ):
            for img_id, row_data in track_data.items():
                pcl = _generate_point_cloud(row_data)
                pcl_fname = pcl_dir / (
                    str(track_id).zfill(3) + str(img_id).zfill(4) + ".npy"
                )

                np.save(pcl_fname, pcl)
                scenes.append(scene)
                track_ids.append(track_id)
                img_ids.append(img_id)
                num_poses.append(max(track_data.keys()) - min(track_data.keys()))
                num_other_tracks.append(
                    len(label_data)
                )  # not correct, but not needed for fake data
                fnames.append(pcl_fname)
        df = pd.DataFrame(
            dict(
                track_id=track_ids,
                pointcloud_fname=fnames,
                num_poses=num_poses,
                num_other_tracks=num_other_tracks,
                img_id=img_ids,
            )
        )
        df.to_csv(out_fname, index=False)


def _generate_point_cloud(row: LabelDataRow, num_points: int = 500) -> np.ndarray:
    rng = np.random.default_rng(42)
    height, width, length = row.dim_3d
    rot_cam = row.rot_3d
    x_corners = [
        length / 2,
        length / 2,
        -length / 2,
        -length / 2,
        length / 2,
        length / 2,
        -length / 2,
        -length / 2,
    ]
    y_corners = [0, 0, 0, 0, -height, -height, -height, -height]
    z_corners = [
        width / 2,
        -width / 2,
        -width / 2,
        width / 2,
        width / 2,
        -width / 2,
        -width / 2,
        width / 2,
    ]
    corners = np.array([x_corners, y_corners, z_corners])
    corners = rot_cam @ corners

    max_x = corners[0].max() * 1.2
    max_y = corners[1].max() * 1.2
    max_z = corners[2].max() * 1.2

    min_x = corners[0].min() * 1.2
    min_y = corners[1].min() * 1.2
    min_z = corners[2].min() * 1.2

    # reduce points for pedestrians
    if row.object_class == "pedestrian":
        num_points //= 4

    # reduce points if occluded
    if row.occ_lvl:
        num_points //= row.occ_lvl

    # reduce points if truncated
    if row.trunc_lvl:
        num_points //= row.trunc_lvl

    x_coord = rng.uniform(min_x, max_x, num_points)
    y_coord = rng.uniform(min_y, max_y, num_points)
    z_coord = rng.uniform(min_z, max_z, num_points)
    points = np.array([x_coord, y_coord, z_coord])
    return points


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate fake pointcloud data as PoC of regressor model"
    )
    parser.add_argument(
        "-o",
        "--base-dir",
        help="where to store output data (default: `./data/fake`)",
        default="./data/fake",
    )

    args = parser.parse_args()
    main(base_dir=Path(args.base_dir))
