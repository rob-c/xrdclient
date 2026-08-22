"""Show one JARVIS 2D crystal entry as raw and normalized images.

The source may be a local file, a hosted ROOT URL, or a catalogue name:

    $ python3 -m pip install -e '.[plot]'
    $ export XRD_CATALOGUE=https://data.example.org
    $ python3 examples/jarvis_2d_visualize.py jarvis_dft2d_formation_energy
    $ python3 examples/jarvis_2d_visualize.py \
        root://127.0.0.1:21094//jarvis_dft2d_formation_energy.root \
        --entry 12 --output entry.png

Only the selected ROOT entry is read when the source is remote.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import xrd.ml


def arguments() -> argparse.Namespace:
    """Parse the dataset entry and presentation choices."""
    parser = argparse.ArgumentParser(
        description="Inspect one hosted JARVIS crystal projection without downloading the file."
    )
    parser.add_argument("source", help="a ROOT path, URL, or xrd-datasets catalogue name")
    parser.add_argument("--tree", default="train", help="TTree to read (default: train)")
    parser.add_argument("--entry", type=int, default=0, help="entry number (default: 0)")
    parser.add_argument("--plane", choices=("xy", "xz", "yz"), default="xy")
    parser.add_argument(
        "--normalization",
        choices=("max", "minmax", "none"),
        default="max",
        help="floating-point scaling shown on the right (default: max)",
    )
    parser.add_argument("--cmap", default="magma", help="Matplotlib colour map (default: magma)")
    parser.add_argument("--output", type=Path, help="save here instead of opening a window")
    return parser.parse_args()


def main() -> None:
    """Read the selected entry and draw both image representations."""
    options = arguments()
    image = xrd.ml.load_image_2d(
        options.source,
        tree=options.tree,
        entry=options.entry,
        plane=options.plane,
        normalization=options.normalization,
    )
    print(image)
    print(f"id={image.jid or '-'} formula={image.formula or '-'} target={image.target}")
    if options.output:
        saved = image.save(options.output, cmap=options.cmap)
        print(f"saved {saved}")
        return
    _figure, _axes = image.plot(cmap=options.cmap)
    from matplotlib import pyplot

    pyplot.show()


if __name__ == "__main__":
    main()
