"""Generate fictional building-photo cards. No private media or credentials."""
import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from shared import DEFAULT_DATA_DIR


def create_demo(root: Path) -> Path:
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("Destination is not empty. Choose a new --output directory; existing files are never replaced.")
    root.mkdir(parents=True, exist_ok=True)
    cards = [
        ("1/msg_100.jpg", 1, "INDEX", "#BDD8E0"),
        ("1/msg_101.png", 1, "COURTYARD", "#BDD8E0"),
        ("2/msg_202.jpg", 2, "INDEX", "#E5C6B5"),
        ("_unsorted/batch_001/msg_200.png", 2, "ENTRANCE", "#E5C6B5"),
        ("_unsorted/batch_001/msg_201.png", 2, "SIDE VIEW", "#E5C6B5"),
        ("_unsorted/batch_002/msg_300.png", 3, "ENTRANCE", "#CBC6DF"),
        ("_unsorted/batch_002/msg_301.jpg", 3, "INDEX", "#CBC6DF"),
        ("_unsorted/batch_003/msg_500.png", 4, "UNMATCHED", "#C7D9BE"),
    ]
    font = ImageFont.load_default(size=30)
    small = ImageFont.load_default(size=18)
    for relative, number, label, colour in cards:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (640, 420), colour)
        draw = ImageDraw.Draw(image)
        draw.rectangle((140, 110, 500, 340), fill="#F7F4ED", outline="#263645", width=4)
        for x in (190, 280, 370):
            draw.rectangle((x, 150, x + 45, 200), fill="#92B4CB")
            draw.rectangle((x, 225, x + 45, 275), fill="#92B4CB")
        draw.text((26, 22), f"BUILDING {number} / {label}", fill="#263645", font=font)
        draw.text((26, 370), "Synthetic demo — no real people or locations", fill="#263645", font=small)
        image.save(path)
    (root / "_singles").mkdir(exist_ok=True)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    try:
        print(f"Demo created: {create_demo(args.output)}")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print("Run python visual_review.py to review the fictional albums.")


if __name__ == "__main__":
    main()
