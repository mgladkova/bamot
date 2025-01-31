"""Tests for preprocessing functions
"""
# pylint: disable=redefined-outer-name
# pylint: disable=invalid-name
# pylint: disable=missing-function-docstring
import numpy as np
import pytest

from bamot.core.preprocessing import (ObjectDetection3D,
                                      compute_extreme_points, from_homogeneous,
                                      get_convex_hull_mask,
                                      get_feature_detections,
                                      get_object_detections, preprocess_frame,
                                      project_bounding_box, to_homogeneous)


@pytest.fixture
def object_center():
    return [1, 2, 3]


@pytest.fixture
def object_dimensions():
    return [4, 5, 6]


@pytest.fixture
def object_detection(object_center, object_dimensions):
    x, y, z = object_center
    w, h, l = object_dimensions
    return ObjectDetection3D(x=x, y=y, z=z, width=w, height=h, length=l)


@pytest.fixture
def extreme_pts(object_center, object_dimensions):
    center_vec = np.array(object_center).reshape(3, 1)
    dim_vec = np.array(object_dimensions).reshape(3, 1)
    extreme_pts = []
    for x in (-1, 1):
        for y in (-1, 1):
            for z in (-1, 1):
                signs = np.array([x, y, z]).reshape(3, 1)
                pt = center_vec + 0.5 * signs * dim_vec
                extreme_pts.append(pt)
    return np.array(extreme_pts).reshape(-1, 3)


@pytest.fixture
def vector():
    return np.array([[1, 2, 3]]).reshape((3, 1))


@pytest.fixture
def homogeneous_vector():
    return np.array([[1, 2, 3, 1]]).reshape((4, 1))


@pytest.fixture
def detected_object():
    return {"x": 1, "y": 2, "z": 3, "width": 4, "height": 5, "length": 6}


@pytest.fixture
def detect_objects_fn(detected_object):
    def detect_objects(stereo_images):
        return [detected_object, detected_object]

    return detect_objects


@pytest.fixture
def feature():
    return {"u": 100, "v": 200, "descriptor": np.random.rand(10)}


@pytest.fixture
def detect_features_fn(feature):
    def detect_features(stereo_images):
        return [feature, feature]

    return detect_features


def test_to_homogeneous(vector, homogeneous_vector):
    assert np.all(to_homogeneous(vector) == homogeneous_vector)


def test_from_homogeneous(homogeneous_vector, vector):
    assert np.all(from_homogeneous(homogeneous_vector) == vector)


def test_compute_extreme_points(object_detection, extreme_pts):
    computed_points = compute_extreme_points(object_detection)
    assert computed_points.shape == extreme_pts.shape
    for pt in computed_points:
        assert pt in extreme_pts


def test_get_convex_hull_mask():
    img_size = (100, 100)
    # point region is in the middle of the image
    points_region = tuple(x / 2 for x in img_size)
    num_points = 100
    # generate some random points
    points_norm = np.random.rand(num_points * 2).reshape(num_points, 2)
    # scale and transform points to middle of image
    points = map(
        lambda p_n: [(1 + p_n[0]) * points_region[0], (1 + p_n[1]) * points_region[1]],
        points_norm,
    )
    hull_mask = get_convex_hull_mask(np.array(list(points)), img_size)
    # assert all points are inside mask
    for point in points:
        assert hull_mask[point] == 1

    # assert mask approx. takes up a quarter of the image
    non_zero_percentage = np.count_nonzero(hull_mask) / (img_size[0] * img_size[1])
    assert np.isclose(non_zero_percentage, 0.25, rtol=2e-1)


def test_project_bounding_box():
    pass


def test_get_feature_detections(feature, detect_features_fn):
    img = np.zeros((100, 100))
    features = get_feature_detections(img, detect_features_fn)
    for f in features:
        assert f.u == feature["u"]
        assert f.v == feature["v"]
        assert np.all(f.descriptor == feature["descriptor"])


def test_get_object_detections(detected_object, detect_objects_fn):
    img = np.zeros((100, 100))
    objs = get_object_detections((img, img), detect_objects_fn)
    for obj in objs:
        assert obj.x == detected_object["x"]
        assert obj.y == detected_object["y"]
        assert obj.z == detected_object["z"]
        assert obj.width == detected_object["width"]
        assert obj.height == detected_object["height"]
        assert obj.length == detected_object["length"]


def test_preprocess_frame():
    pass
