"""The public, dependency-light 2D image API in :mod:`xrd.ml`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import xrd.ml as ml
from xrd.root import UnsupportedFeatureError, create


@pytest.fixture
def images(tmp_path: Path) -> Path:
    """A rectangle, a square and two named layers in one small ROOT tree."""
    path = tmp_path / "images.root"
    columns = {
        "pixels": ("f", 6),
        "square": ("h", 4),
        "layers": ("B", 8),
        "target": "f",
    }
    rows = [
        {
            "pixels": [-2, -1, 0, 1, 2, 4],
            "square": [1, 2, 3, 4],
            "layers": list(range(8)),
            "target": 2.5,
        }
    ]
    with create(str(path)) as output:
        output.tree("train", columns).extend(rows)
    return path


def test_a_rectangular_float_image_retains_raw_values_and_minmax_normalizes(
    images: Path,
) -> None:
    image = ml.load_image_2d(
        images,
        branch="pixels",
        shape=(2, 3),
        planes=None,
        plane=None,
        normalization="minmax",
    )

    assert image.raw == ((-2.0, -1.0, 0.0), (1.0, 2.0, 4.0))
    assert image.normalized[0] == pytest.approx((0.0, 1 / 6, 2 / 6))
    assert image.normalized[1] == pytest.approx((3 / 6, 4 / 6, 1.0))
    assert (image.shape, image.height, image.width) == ((2, 3), 2, 3)
    assert (image.raw_min, image.raw_max, image.target) == (-2.0, 4.0, 2.5)
    assert image.metadata["branch"] == "pixels"
    assert image.metadata["normalization"] == "minmax"


def test_a_single_square_image_is_inferred_without_plane_metadata(images: Path) -> None:
    image = ml.load_image_2d(images, branch="square", planes=None, plane=None)
    assert image.raw == ((1, 2), (3, 4))
    assert image.plane is None


def test_named_layers_can_be_selected_by_name_or_python_index(images: Path) -> None:
    options = {"branch": "layers", "shape": (2, 2), "planes": ("left", "right")}
    named = ml.load_image_2d(images, plane="right", **options)
    numbered = ml.load_image_2d(images, plane=-1, **options)

    assert named.raw == numbered.raw == ((4, 5), (6, 7))
    assert named.plane == numbered.plane == "right"


def test_visualize_2d_remains_a_compatible_descriptive_alias(images: Path) -> None:
    options = {"branch": "square", "planes": None, "plane": None}
    assert ml.visualize_2d(images, **options) == ml.load_image_2d(images, **options)


def test_images_can_be_renormalized_and_flattened_without_rereading(images: Path) -> None:
    raw = ml.load_image_2d(
        images,
        branch="pixels",
        shape=(2, 3),
        planes=None,
        plane=None,
        normalization="none",
    )
    maximum = raw.with_normalization("max")
    minmax = raw.with_normalization("minmax")

    assert raw.normalized == raw.raw
    assert maximum.normalized == ((-0.5, -0.25, 0.0), (0.25, 0.5, 1.0))
    assert minmax.normalized[-1][-1] == 1.0
    assert raw.flat(normalized=False) == (-2.0, -1.0, 0.0, 1.0, 2.0, 4.0)
    assert raw.values(normalized=False) is raw.raw
    assert "pixels" in repr(raw) and "-2.0" not in repr(raw)


@pytest.mark.parametrize("normalization", ["maximum", "", "unit"])
def test_unknown_normalizations_are_rejected_before_opening_a_file(normalization: Any) -> None:
    with pytest.raises(ValueError, match="normalization must be"):
        ml.load_image_2d("missing.root", normalization=normalization)


def test_invalid_image_layouts_and_plane_selections_are_explained(images: Path) -> None:
    with pytest.raises(ValueError, match="two positive integers"):
        ml.load_image_2d(images, branch="pixels", shape=(0, 3), planes=None, plane=None)
    with pytest.raises(ValueError, match="plane names must be unique"):
        ml.load_image_2d(images, planes=("xy", "xy"))
    with pytest.raises(ValueError, match="sequence of names"):
        ml.load_image_2d(images, planes="xy")
    with pytest.raises(ValueError, match=r"contains 2 images.*3 plane names"):
        ml.load_image_2d(images, branch="layers", shape=(2, 2))
    with pytest.raises(ValueError, match="plane=None is ambiguous"):
        ml.load_image_2d(
            images, branch="layers", shape=(2, 2), planes=("left", "right"), plane=None
        )
    with pytest.raises(ValueError, match="plane must be one of"):
        ml.load_image_2d(
            images, branch="layers", shape=(2, 2), planes=("left", "right"), plane="middle"
        )
    with pytest.raises(IndexError, match="plane index 2"):
        ml.load_image_2d(
            images, branch="layers", shape=(2, 2), planes=("left", "right"), plane=2
        )
    with pytest.raises(KeyError, match="no 'missing' branch"):
        ml.load_image_2d(images, branch="missing")


def test_numpy_conversion_uses_float32_for_model_ready_values(
    images: Path, monkeypatch: Any
) -> None:
    calls: list[tuple[Any, Any]] = []

    class Numpy:
        @staticmethod
        def asarray(values: Any, dtype: Any = None) -> tuple[Any, Any]:
            calls.append((values, dtype))
            return values, dtype

    monkeypatch.setattr(ml, "_numpy", lambda: Numpy)
    image = ml.load_image_2d(images, branch="square", planes=None, plane=None)

    assert image.to_numpy() == (image.normalized, "float32")
    assert image.to_numpy(normalized=False, dtype="int16") == (image.raw, "int16")
    assert calls == [(image.normalized, "float32"), (image.raw, "int16")]


def test_tensor_conversion_defaults_to_one_float32_channel(
    images: Path, monkeypatch: Any
) -> None:
    class Tensor:
        def __init__(self, values: Any, options: dict[str, Any]) -> None:
            self.values = values
            self.options = options
            self.shape = (len(values), len(values[0]))

        def unsqueeze(self, axis: int) -> Tensor:
            assert axis == 0
            self.shape = (1, *self.shape)
            return self

    class Torch:
        float32 = "float32"

        @staticmethod
        def tensor(values: Any, **options: Any) -> Tensor:
            return Tensor(values, options)

    monkeypatch.setattr(ml, "_torch", lambda: Torch)
    image = ml.load_image_2d(images, branch="square", planes=None, plane=None)

    model_ready = image.to_tensor(device="cpu")
    raw = image.to_tensor(normalized=False, channel=False)
    assert (model_ready.shape, model_ready.options) == (
        (1, 2, 2),
        {"device": "cpu", "dtype": "float32"},
    )
    assert (raw.shape, raw.options) == ((2, 2), {"device": None})


def test_optional_array_and_plotting_dependencies_fail_with_install_help(
    images: Path, monkeypatch: Any
) -> None:
    image = ml.load_image_2d(images, branch="square", planes=None, plane=None)
    monkeypatch.setitem(sys.modules, "numpy", None)
    with pytest.raises(UnsupportedFeatureError, match=r"pip install numpy.*matrices"):
        image.to_numpy()
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", None)
    with pytest.raises(UnsupportedFeatureError, match=r"pyxrootdclient\[plot\].*matrices"):
        image.plot()


def test_plotting_can_embed_or_save_a_complete_comparison(images: Path, tmp_path: Path) -> None:
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg", force=True)
    from matplotlib import pyplot

    image = ml.load_image_2d(images, branch="square", planes=None, plane=None)
    owned, axes = pyplot.subplots(1, 2)
    figure, selected = image.plot(axes=axes, colorbar=False, title="A useful image")
    assert figure is owned
    assert tuple(selected) == tuple(axes)
    assert [axis.get_title() for axis in axes] == ["Raw values", "Normalized (max)"]
    assert figure._suptitle.get_text() == "A useful image"

    destination = image.save(tmp_path / "comparison.png", colorbar=False)
    assert destination == tmp_path / "comparison.png"
    assert destination.read_bytes().startswith(b"\x89PNG")
    pyplot.close(owned)


def test_plotting_rejects_an_axes_collection_of_the_wrong_size(images: Path) -> None:
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg", force=True)
    from matplotlib import pyplot

    image = ml.load_image_2d(images, branch="square", planes=None, plane=None)
    figure, axes = pyplot.subplots(1, 3)
    with pytest.raises(ValueError, match="exactly two"):
        image.plot(axes=axes)
    with pytest.raises(ValueError, match="dpi must be a positive integer"):
        image.save("unused.png", dpi=0)
    pyplot.close(figure)
