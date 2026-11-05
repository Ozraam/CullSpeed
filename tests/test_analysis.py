import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from tamis import analysis, bursts, peaking  # noqa: E402
from tamis.imaging import gray_array  # noqa: E402
from tamis.models import Photo  # noqa: E402
from tamis.thumbcache import ThumbnailCache  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def noise(seed=1, size=(400, 600)):
    return np.random.default_rng(seed).integers(0, 255, size, dtype=np.uint8)


def blurred(gray, passes=12):
    image = gray.astype(np.float32)
    for _ in range(passes):
        image = (image + np.roll(image, 1, 0) + np.roll(image, -1, 0) + np.roll(image, 1, 1) + np.roll(image, -1, 1)) / 5
    return image.astype(np.uint8)


# -- sharpness -----------------------------------------------------------------

def test_sharpness_ranks_a_sharp_image_above_its_blurred_copy():
    gray = noise()
    assert analysis.sharpness(gray) > analysis.sharpness(blurred(gray)) * 5


def test_sharpness_looks_at_the_sharpest_region():
    background = blurred(noise())
    subject = background.copy()
    subject[100:200, 200:300] = noise(2)[100:200, 200:300]
    assert analysis.sharpness(subject) > analysis.sharpness(background) * 5


def test_sharpness_copes_with_tiny_images():
    assert analysis.sharpness(np.zeros((2, 2), dtype=np.uint8)) == 0.0
    assert analysis.sharpness(np.zeros((3, 3), dtype=np.uint8)) == 0.0


# -- perceptual hash -----------------------------------------------------------

def test_hash_is_close_for_similar_images_and_far_for_others():
    gradient = np.tile(np.linspace(0, 255, 600, dtype=np.uint8), (400, 1))
    similar = np.clip(gradient.astype(int) + 3, 0, 255).astype(np.uint8)
    other = blurred(noise(3), 40)
    distance = lambda a, b: (analysis.difference_hash(a) ^ analysis.difference_hash(b)).bit_count()  # noqa: E731
    assert distance(gradient, similar) <= 4
    assert distance(gradient, other) > bursts.MAX_HASH_DISTANCE // 2


def test_hash_copes_with_small_images():
    assert isinstance(analysis.difference_hash(np.zeros((4, 4), dtype=np.uint8)), int)


# -- bursts --------------------------------------------------------------------

def photo(name, seconds, image_hash=None, sharpness=None):
    return Photo(name, capture_time=seconds, image_hash=image_hash, sharpness=sharpness)


def test_groups_follow_the_gap_in_capture_order():
    photos = [photo("c", 11), photo("a", 10), photo("b", 10.5), photo("far", 100), photo("x", 200), photo("y", 201.9)]
    assert [[p.path for p in run] for run in bursts.groups(photos)] == [["a", "b", "c"], ["x", "y"]]


def test_groups_ignore_photos_without_a_date():
    assert bursts.groups([photo("a", None), photo("b", None)]) == []


def test_a_different_picture_ends_a_burst():
    photos = [photo("a", 1, 0), photo("b", 2, 0b111), photo("c", 3, 2**64 - 1), photo("d", 4, 2**64 - 1)]
    assert [[p.path for p in run] for run in bursts.groups(photos)] == [["a", "b"], ["c", "d"]]


def test_assign_marks_the_sharpest_frame_once_two_are_scored():
    photos = [photo("a", 1, sharpness=5.0), photo("b", 2), photo("alone", 50)]
    bursts.assign(photos)
    assert photos[0].burst.sharpest is False and photos[2].burst is None
    photos[1].sharpness = 9.0
    bursts.assign(photos)
    assert (photos[1].burst.sharpest, photos[1].burst.position, photos[1].burst.size) == (True, 1, 2)
    assert photos[0].burst.sharpest is False


# -- the engine ----------------------------------------------------------------

def save_jpeg(path):
    image = QImage(320, 200, QImage.Format.Format_RGB32)
    image.fill(QColor(90, 120, 150))
    assert image.save(str(path))


def run(engine):
    for _ in range(500):
        if not engine._pending:
            return
        QTest.qWait(10)
    pytest.fail("analysis did not finish")


def test_analyzer_finds_and_remembers_across_sessions(qapp, tmp_path):
    folder, cache = tmp_path / "shoot", tmp_path / "cache"
    folder.mkdir()
    save_jpeg(folder / "a.jpg")
    target = Photo(str(folder / "a.jpg"))
    engine = analysis.PhotoAnalyzer(str(cache))
    engine.open_folder(str(folder))
    found, finished = {}, []
    engine.analyzed.connect(lambda path, results: found.update(results))
    engine.finished.connect(lambda: finished.append(True))
    engine.request([target], [target])
    run(engine)
    assert found["capture_time"] and found["sharpness"] is not None and found["image_hash"] is not None
    assert finished == [True]
    engine.shutdown()

    again = analysis.PhotoAnalyzer(str(cache))
    again.open_folder(str(folder))
    fresh = Photo(str(folder / "a.jpg"))
    again.apply_known([fresh])
    assert fresh.capture_time == found["capture_time"] and fresh.sharpness == found["sharpness"]
    again.request([fresh], [fresh])
    assert not again._pending  # nothing to do twice

    os.rename(folder / "a.jpg", folder / "a2.jpg")  # another name: looked at again; a move keeps name and date
    moved = Photo(str(folder / "a2.jpg"))
    again.apply_known([moved])
    assert moved.capture_time is None
    again.shutdown()


def test_analyzer_survives_a_bad_photo_and_does_not_try_it_twice(qapp, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    target = Photo(str(bad))
    engine = analysis.PhotoAnalyzer(str(tmp_path / "cache"))
    engine.open_folder(str(tmp_path))
    engine.request([], [target])
    run(engine)
    engine.request([], [target])
    assert not engine._pending
    engine.shutdown()


def test_analyzer_ignores_results_from_before_a_restart(qapp, tmp_path):
    save_jpeg(tmp_path / "a.jpg")
    engine = analysis.PhotoAnalyzer()
    engine.open_folder(str(tmp_path))
    found = []
    engine.analyzed.connect(lambda path, results: found.append(path))
    engine._on_done(engine._generation - 1, (str(tmp_path / "a.jpg"), "capture"), "key", {"capture_time": 1.0})
    assert found == []
    engine.shutdown()


# -- focus peaking -------------------------------------------------------------

def test_peaking_marks_sharp_edges_only():
    sharp = np.zeros((100, 100), dtype=np.uint8)
    sharp[:, 50:] = 255
    soft = np.tile(np.linspace(0, 255, 100).astype(np.uint8), (100, 1))
    assert peaking.peak_mask(sharp).any()
    assert not peaking.peak_mask(soft).any()


def test_overlay_is_transparent_except_on_the_edges(qapp):
    image = QImage(60, 40, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    for y in range(40):
        for x in range(30, 60):
            image.setPixelColor(x, y, QColor(255, 255, 255))
    overlay = peaking.overlay_image(image)
    assert overlay.size() == image.size()
    assert overlay.pixelColor(5, 5).alpha() == 0
    assert overlay.pixelColor(30, 20).alpha() == 255


def test_gray_array_handles_row_padding(qapp):
    image = QImage(7, 3, QImage.Format.Format_RGB32)  # not a multiple of 4 wide
    image.fill(QColor(255, 255, 255))
    gray = gray_array(image)
    assert gray.shape == (3, 7) and gray.min() == 255


# -- thumbnails on disk --------------------------------------------------------

def thumb(alpha=False):
    image = QImage(32, 16, QImage.Format.Format_ARGB32 if alpha else QImage.Format.Format_RGB32)
    image.fill(QColor(10, 200, 30, 90 if alpha else 255))
    return image


def test_thumbnail_cache_is_keyed_by_file_state_and_size(qapp, tmp_path):
    cache = ThumbnailCache(str(tmp_path / "c"))
    assert cache.load("/a.jpg", (1, 2), 240) is None
    cache.save("/a.jpg", (1, 2), 240, thumb())
    assert cache.load("/a.jpg", (1, 2), 240).size() == thumb().size()
    assert cache.load("/a.jpg", (1, 3), 240) is None  # changed by another app
    assert cache.load("/a.jpg", (9, 2), 240) is None
    assert cache.load("/b.jpg", (1, 2), 240) is None  # moved
    assert cache.load("/a.jpg", (1, 2), 480) is None  # another screen


def test_thumbnail_cache_keeps_transparency(qapp, tmp_path):
    cache = ThumbnailCache(str(tmp_path / "c"))
    cache.save("/a.png", (1, 2), 240, thumb(alpha=True))
    assert cache.load("/a.png", (1, 2), 240).hasAlphaChannel()


def test_thumbnail_cache_ignores_a_damaged_file(qapp, tmp_path):
    cache = ThumbnailCache(str(tmp_path / "c"))
    cache.save("/a.jpg", (1, 2), 240, thumb())
    with open(cache._path("/a.jpg", (1, 2), 240), "wb") as file:
        file.write(b"garbage")
    assert cache.load("/a.jpg", (1, 2), 240) is None


def test_thumbnail_cache_survives_a_folder_it_cannot_write(qapp, tmp_path):
    (tmp_path / "file").write_bytes(b"x")
    cache = ThumbnailCache(str(tmp_path / "file" / "c"))
    cache.save("/a.jpg", (1, 2), 240, thumb())
    assert cache.load("/a.jpg", (1, 2), 240) is None


def test_thumbnail_cache_trims_the_least_recently_used(qapp, tmp_path):
    cache = ThumbnailCache(str(tmp_path / "c"))
    for i in range(4):
        cache.save(f"/{i}.jpg", (1, 2), 240, thumb())
        os.utime(cache._path(f"/{i}.jpg", (1, 2), 240), (1000 + i, 1000 + i))
    os.utime(cache._path("/0.jpg", (1, 2), 240), (2000, 2000))  # used lately
    one = os.path.getsize(cache._path("/1.jpg", (1, 2), 240))
    cache.limit = one * 2
    cache.trim()
    assert cache.load("/0.jpg", (1, 2), 240) is not None
    assert cache.load("/3.jpg", (1, 2), 240) is not None
    assert cache.load("/1.jpg", (1, 2), 240) is None
    assert cache.load("/2.jpg", (1, 2), 240) is None
