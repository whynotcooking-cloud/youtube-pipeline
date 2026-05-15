import argparse, csv, re, shutil, time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
import imagehash
from PIL import Image, ImageStat
from duckduckgo_search import DDGS
from tqdm import tqdm

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
WATERMARK_DIR = "Фото с ватермарками"
DUP_DIR = "Дубли"

@dataclass
class Block:
    index: int
    title: str
    count: int = 8
    queries: list = field(default_factory=list)

def safe_name(text):
    text = re.sub(r'[\\/:*?"<>|]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:90] or "Без названия"

def parse_blocks(path):
    text = path.read_text(encoding="utf-8")
    blocks = []
    current = None
    in_queries = False

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        m = re.match(r"^#+\s*БЛОК\s*(\d+)\s*[—-]\s*(.+)$", s, re.I)
        if m:
            if current:
                blocks.append(current)
            current = Block(int(m.group(1)), safe_name(m.group(2)))
            in_queries = False
            continue

        if current is None:
            continue

        m = re.match(r"^Фото\s*:\s*(\d+)", s, re.I)
        if m:
            current.count = int(m.group(1))
            continue

        if s.lower().startswith("запросы"):
            in_queries = True
            continue

        if in_queries and s.startswith("-"):
            q = s[1:].strip()
            if q:
                current.queries.append(q)

    if current:
        blocks.append(current)

    return blocks

def create_structure(project_name, blocks):
    root = Path.home() / "Desktop" / "YOUTUBE_PROJECTS" / safe_name(project_name)
    archive = root / "Медиафайлы" / "Архивные фотографии"

    for p in [
        root / "Озвучка",
        root / "Итоги",
        root / "Медиафайлы" / "Сгенерированные изображения",
        archive,
    ]:
        p.mkdir(parents=True, exist_ok=True)

    block_dirs = {}
    for b in blocks:
        d = archive / f"Блок {b.index:03d} — {safe_name(b.title)}"
        d.mkdir(parents=True, exist_ok=True)
        (d / WATERMARK_DIR).mkdir(exist_ok=True)
        (d / DUP_DIR).mkdir(exist_ok=True)
        block_dirs[b.index] = d

    return root, archive, block_dirs

def ext_from_url(url):
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in IMAGE_EXTS else ".jpg"

def valid_image(path):
    try:
        with Image.open(path) as im:
            w, h = im.size
            if w < 400 or h < 300:
                return False
            im.verify()
        return True
    except Exception:
        return False

def download_image(url, out):
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or len(r.content) < 10000:
            return False
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(r.content)
        if not valid_image(tmp):
            tmp.unlink(missing_ok=True)
            return False
        tmp.rename(out)
        return True
    except Exception:
        return False

def download_for_block(block, block_dir):
    downloaded = 0
    seen = set()

    with DDGS() as ddgs:
        for query in block.queries:
            if downloaded >= block.count:
                break

            print(f"  Поиск: {query}")

            try:
                results = ddgs.images(
                    query,
                    max_results=20,
                    safesearch="moderate",
                    type_image="photo",
                )
            except Exception as e:
                print("  Ошибка поиска:", e)
                continue

            for item in results:
                if downloaded >= block.count:
                    break

                url = item.get("image")
                if not url or url in seen:
                    continue
                seen.add(url)

                out = block_dir / f"{block.index:03d}_{downloaded+1:03d}{ext_from_url(url)}"

                if download_image(url, out):
                    downloaded += 1
                    print(f"    скачано: {out.name}")
                    time.sleep(0.4)

    return downloaded

def is_image(p):
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS

def collect_images(archive):
    result = []
    for p in archive.rglob("*"):
        if not is_image(p):
            continue
        if DUP_DIR in p.parts:
            continue
        result.append(p)
    return result

def watermark_suspicious(path):
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            w, h = im.size
            zones = [
                (0, int(h*0.75), w, h),
                (0, 0, w, int(h*0.18)),
                (int(w*0.55), int(h*0.55), w, h),
                (0, int(h*0.55), int(w*0.45), h),
            ]
            hits = 0
            for box in zones:
                crop = im.crop(box)
                stat = ImageStat.Stat(crop)
                if stat.stddev[0] > 55 or (stat.mean[0] > 175 and stat.stddev[0] > 30):
                    hits += 1
            return hits >= 2
    except Exception:
        return False

def move_watermarks(archive):
    moved = []
    for p in collect_images(archive):
        if WATERMARK_DIR in p.parts:
            continue
        if watermark_suspicious(p):
            block_dir = next(x for x in p.parents if x.parent == archive)
            target = block_dir / WATERMARK_DIR / p.name
            shutil.move(str(p), str(target))
            moved.append(str(target))
    return moved

def phash(path):
    try:
        with Image.open(path) as im:
            return imagehash.phash(im.convert("RGB"))
    except Exception:
        return None

def resolution_and_size(path):
    try:
        with Image.open(path) as im:
            w, h = im.size
        return w*h, path.stat().st_size
    except Exception:
        return 0, 0

def block_number(path):
    for part in path.parts:
        m = re.match(r"Блок\s+(\d+)", part)
        if m:
            return int(m.group(1))
    return 9999

def move_duplicates(archive, threshold=6):
    imgs = collect_images(archive)
    data = []

    for p in tqdm(imgs, desc="Индексация дублей"):
        h = phash(p)
        if h is not None:
            area, size = resolution_and_size(p)
            data.append({"path": p, "hash": h, "area": area, "size": size, "block": block_number(p)})

    used = set()
    moved = []

    for i, a in enumerate(data):
        if a["path"] in used:
            continue

        group = [a]
        for b in data[i+1:]:
            if b["path"] in used:
                continue
            if a["hash"] - b["hash"] <= threshold:
                group.append(b)

        if len(group) < 2:
            continue

        keeper = sorted(group, key=lambda x: (-x["area"], -x["size"], x["block"], str(x["path"])))[0]

        for item in group:
            used.add(item["path"])
            if item["path"] == keeper["path"]:
                continue

            block_dir = next(x for x in item["path"].parents if x.parent == archive)
            target = block_dir / DUP_DIR / item["path"].name
            shutil.move(str(item["path"]), str(target))
            moved.append({"kept": str(keeper["path"]), "duplicate": str(target)})

    return moved

def write_report(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", default="blocks.txt")
    ap.add_argument("--project", default="Большой террор СССР")
    args = ap.parse_args()

    blocks = parse_blocks(Path(args.blocks))
    root, archive, block_dirs = create_structure(args.project, blocks)

    print("Проект:", root)

    for block in blocks:
        print(f"\nБлок {block.index:03d}: {block.title}")
        n = download_for_block(block, block_dirs[block.index])
        print(f"  Итого скачано: {n}")

    wm = move_watermarks(archive)
    write_report(root / "report_watermarks.csv", [{"file": x} for x in wm])
    print(f"\nWatermark-подозрения перенесены: {len(wm)}")

    dups = move_duplicates(archive)
    write_report(root / "report_duplicates.csv", dups)
    print(f"Дубли перенесены: {len(dups)}")

    print("\nГОТОВО.")
    print("Папка проекта:", root)

if __name__ == "__main__":
    main()
