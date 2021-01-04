import argparse
import glob
import subprocess
from pathlib import Path

from bamot.config import CONFIG as config


def main(args):
    scene = str(args.scene).zfill(4)
    recordings_path = Path("./data/recordings/")
    detections_recordings_path = recordings_path / "detections"
    detections_recordings_path.mkdir(exist_ok=True)
    detections_video_path = detections_recordings_path / (scene + ".avi")
    if not detections_video_path.exists():  # or args.new_detections:
        print("Creating detections video...")
        # create detections video
        detections_path = (
            Path(config.EST_DETECTIONS_PATH) / ".." / "slam" / "image_02" / scene
        )
        detection_images = detections_path / "*.png"
        print(detection_images.as_posix())
        if not glob.glob(detection_images.as_posix()):
            raise RuntimeError(
                "No processed images, run preprocessing script for detections first"
            )
        subprocess.run(
            [
                "ffmpeg",
                "-r",
                str(config.FRAME_RATE),
                "-pattern_type",
                "glob",
                "-i",
                detection_images.as_posix(),
                "-vcodec",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                detections_video_path.as_posix(),
            ],
            capture_output=False,
            check=True,
        )
        print("Creating detections video...done")
    else:
        print("Using existing detections video...")
    # create video of recording
    recording_path = Path(args.recording) / "out.avi"
    print("Creating video of recording...")
    subprocess.run(
        [
            "ffmpeg",
            "-r",
            str(config.FRAME_RATE),
            "-pattern_type",
            "glob",
            "-i",
            (Path(args.recording) / "*.png").as_posix(),
            "-vcodec",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            recording_path.as_posix(),
        ],
        capture_output=False,
        check=True,
    )
    print("Creating video of recording...done")
    print(f"Saved at {recording_path.as_posix()}")
    # stack videos
    print("Stacking all videos...")
    output_path = Path(args.recording) / "out_stacked.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-i",
            detections_video_path.as_posix(),
            "-i",
            recording_path.as_posix(),
            "-filter_complex",
            "[1][0]scale2ref=iw:iw*(main_h/main_w)[2nd][ref];[ref][2nd]vstack=inputs=2",
            output_path.as_posix(),
        ],
        capture_output=False,
        check=True,
    )
    print("Stacking all videos...done")
    print(f"Saved at {output_path.as_posix()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="")
    parser.add_argument("recording", type=str)
    parser.add_argument("scene", type=int)
    args = parser.parse_args()
    main(args)