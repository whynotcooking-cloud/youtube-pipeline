import argparse
import csv
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import imagehash
import requests
import yt_dlp
from duckduckgo_search import DDGS
from PIL import Image, ImageStat
from tqdm import tqdm

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
WATERMARK_DIR = "Фото с ватермарками"
DUP_DIR = "Дубли"

PHOTO_RU_PER_QUERY = 2
PHOTO_EN_PER_QUERY = 1

VIDEO_PER_BLOCK = 2
VIDEO_MAX_DURATION = 180
VIDEO_MAX_SIZE_MB = 300


@dataclass
class Block:
    index: int
    title: str
    count: int = 999
    queries: list[str] = field(default_factory=list)


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

        if s.startswith("-"):
            q = s[1:].strip()
            if q:
                current.queries.append(q)
            continue

        m = re.match(r"^\d+[\.\)]\s*(.+)$", s)
        if m:
            q = m.group(1).strip()
            if q:
                current.queries.append(q)

    if current:
        blocks.append(current)

    if not blocks:
        raise RuntimeError("Не нашёл блоки в blocks.txt")

    return blocks


def create_structure(project_name, blocks):
    root = Path.home() / "Desktop" / "YOUTUBE_PROJECTS" / safe_name(project_name)

    photo_archive = root / "Медиафайлы" / "Архивные фотографии"
    video_archive = root / "Медиафайлы" / "Архивные видео"

    base_dirs = [
        root / "Озвучка",
        root / "Итоги",
        root / "Медиафайлы" / "Сгенерированные изображения",
        photo_archive,
        video_archive,
    ]

    for d in base_dirs:
        d.mkdir(parents=True, exist_ok=True)

    photo_block_dirs = {}
    video_block_dirs = {}

    for b in blocks:
        photo_dir = photo_archive / f"Блок {b.index:03d} — {safe_name(b.title)}"
        video_dir = video_archive / f"Блок {b.index:03d} — {safe_name(b.title)}"

        photo_dir.mkdir(parents=True, exist_ok=True)
        video_dir.mkdir(parents=True, exist_ok=True)

        (photo_dir / WATERMARK_DIR).mkdir(exist_ok=True)
        (photo_dir / DUP_DIR).mkdir(exist_ok=True)

        photo_block_dirs[b.index] = photo_dir
        video_block_dirs[b.index] = video_dir

    return root, photo_archive, video_archive, photo_block_dirs, video_block_dirs


def split_query_pair(query):
    if " / " in query:
        ru, en = query.split(" / ", 1)
        return ru.strip(), en.strip()
    return query.strip(), ""


def ext_from_url(url):
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in IMAGE_EXTS else ".jpg"


def is_valid_image(path):
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
        r = requests.get(
            url,
            timeout=25,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        if r.status_code != 200 or len(r.content) < 12000:
            return False

        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(r.content)

        if not is_valid_image(tmp):
            tmp.unlink(missing_ok=True)
            return False

        tmp.rename(out)
        return True

    except Exception:
        return False


def download_from_query(ddgs, query, block, block_dir, start_index, target_count, seen):
    if not query:
        return 0

    downloaded = 0
    print(f"  Фото-поиск: {query} | цель: {target_count}")

    try:
        results = ddgs.images(
            query,
            max_results=50,
            safesearch="moderate",
            type_image="photo",
        )
    except Exception as e:
        print("    ошибка поиска:", e)
        return 0

    for item in results:
        if downloaded >= target_count:
            break

        url = item.get("image")
        if not url or url in seen:
            continue

        seen.add(url)

        out = block_dir / f"{block.index:03d}_{start_index + downloaded:03d}{ext_from_url(url)}"

        if download_image(url, out):
            downloaded += 1
            print(f"    скачано: {out.name}")
            time.sleep(0.35)

    return downloaded


def download_photos_for_block(block, block_dir):
    total = 0
    seen = set()

    with DDGS() as ddgs:
        for query in block.queries:
            ru, en = split_query_pair(query)

            got_ru = download_from_query(
                ddgs, ru, block, block_dir,
                total + 1,
                PHOTO_RU_PER_QUERY,
                seen,
            )
            total += got_ru

            got_en = download_from_query(
                ddgs, en, block, block_dir,
                total + 1,
                PHOTO_EN_PER_QUERY,
                seen,
            )
            total += got_en

    return total


def make_video_queries(block):
    queries = []

    for q in block.queries[:8]:
        ru, en = split_query_pair(q)

        if en:
            queries.append(en + " archive footage")
            queries.append(en + " documentary footage")
            queries.append(en + " historical footage")

        if ru:
            queries.append(ru + " архивная хроника")
            queries.append(ru + " документальная хроника")

    clean = []
    seen = set()

    for q in queries:
        key = q.lower().strip()
        if key and key not in seen:
            clean.append(q.strip())
            seen.add(key)

    return clean[:12]


def download_videos_for_block(block, video_dir):
    downloaded = 0
    report = []
    queries = make_video_queries(block)

    archive_file = video_dir / "_downloaded_archive.txt"

    for query in queries:
        if downloaded >= VIDEO_PER_BLOCK:
            break

        print(f"  Видео-поиск: {query}")

        ydl_search_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "ignoreerrors": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_search_opts) as ydl:
                info = ydl.extract_info(f"ytsearch10:{query}", download=False)
        except Exception as e:
            print("    ошибка video search:", e)
            continue

        entries = info.get("entries", []) if info else []

        for entry in entries:
            if downloaded >= VIDEO_PER_BLOCK:
                break

            if not entry:
                continue

            duration = entry.get("duration") or 999999
            url = entry.get("webpage_url") or entry.get("url")

            if not url:
                continue

            if duration > VIDEO_MAX_DURATION:
                continue

            outtmpl = str(video_dir / f"{block.index:03d}_video_{downloaded + 1:03d}.%(ext)s")

            ydl_download_opts = {
                "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "outtmpl": outtmpl,
                "merge_output_format": "mp4",
                "max_filesize": VIDEO_MAX_SIZE_MB * 1024 * 1024,
                "download_archive": str(archive_file),
                "ignoreerrors": True,
                "quiet": False,
                "no_warnings": False,
                "noplaylist": True,
            }

            try:
                with yt_dlp.YoutubeDL(ydl_download_opts) as ydl:
                    ydl.download([url])

                downloaded += 1

                report.append({
                    "block": block.index,
                    "query": query,
                    "title": entry.get("title", ""),
                    "duration": duration,
                    "url": entry.get("webpage_url", ""),
                    "source": entry.get("extractor", ""),
                    "folder": str(video_dir),
                })

                print(f"    видео скачано: {downloaded}/{VIDEO_PER_BLOCK}")

            except Exception as e:
                print("    ошибка video download:", e)

    return report


def is_image(p):
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS


def collect_images(photo_archive):
    images = []

    for p in photo_archive.rglob("*"):
        if not is_image(p):
            continue

        if DUP_DIR in p.parts:
            continue

        images.append(p)

    return images


def watermark_suspicious(path):
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            w, h = im.size

            zones = [
                (0, int(h * 0.75), w, h),
                (0, 0, w, int(h * 0.18)),
                (int(w * 0.55), int(h * 0.55), w, h),
                (0, int(h * 0.55), int(w * 0.45), h),
            ]

            hits = 0

            for box in zones:
                crop = im.crop(box)
                stat = ImageStat.Stat(crop)
                mean = stat.mean[0]
                std = stat.stddev[0]

                if std > 58 or (mean > 175 and std > 30):
                    hits += 1

            return hits >= 2

    except Exception:
        return False


def find_block_dir(path, archive):
    for parent in path.parents:
        if parent.parent == archive and parent.name.startswith("Блок"):
            return parent
    return None


def move_watermarks(photo_archive):
    moved = []

    for p in collect_images(photo_archive):
        if WATERMARK_DIR in p.parts:
            continue

        if watermark_suspicious(p):
            block_dir = find_block_dir(p, photo_archive)
            if not block_dir:
                continue

            target = block_dir / WATERMARK_DIR / p.name

            if target.exists():
                target = target.with_name(target.stem + "_copy" + target.suffix)

            shutil.move(str(p), str(target))
            moved.append({"file": str(target)})

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
        return w * h, path.stat().st_size
    except Exception:
        return 0, 0


def block_number(path):
    for part in path.parts:
        m = re.match(r"Блок\s+(\d+)", part)
        if m:
            return int(m.group(1))
    return 9999


def move_duplicates(photo_archive, threshold=6):
    imgs = collect_images(photo_archive)
    data = []

    for p in tqdm(imgs, desc="Индексация дублей"):
        h = phash(p)
        if h is None:
            continue

        area, size = resolution_and_size(p)

        data.append({
            "path": p,
            "hash": h,
            "area": area,
            "size": size,
            "block": block_number(p),
        })

    used = set()
    moved = []

    for i, a in enumerate(data):
        if a["path"] in used:
            continue

        group = [a]

        for b in data[i + 1:]:
            if b["path"] in used:
                continue

            if a["hash"] - b["hash"] <= threshold:
                group.append(b)

        if len(group) < 2:
            continue

        keeper = sorted(
            group,
            key=lambda x: (-x["area"], -x["size"], x["block"], str(x["path"]))
        )[0]

        for item in group:
            used.add(item["path"])

            if item["path"] == keeper["path"]:
                continue

            block_dir = find_block_dir(item["path"], photo_archive)
            if not block_dir:
                continue

            target = block_dir / DUP_DIR / item["path"].name

            if target.exists():
                target = target.with_name(target.stem + "_copy" + target.suffix)

            shutil.move(str(item["path"]), str(target))

            moved.append({
                "kept": str(keeper["path"]),
                "duplicate": str(target),
            })

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
    ap.add_argument("--project", default="Большой террор СССР v3")
    args = ap.parse_args()

    blocks = parse_blocks(Path(args.blocks))

    root, photo_archive, video_archive, photo_block_dirs, video_block_dirs = create_structure(
        args.project,
        blocks,
    )

    print("\nПРОЕКТ:", root)

    video_rows_all = []

    for block in blocks:
        print(f"\n========== Блок {block.index:03d}: {block.title} ==========")

        photo_count = download_photos_for_block(block, photo_block_dirs[block.index])
        print(f"  Итого фото скачано: {photo_count}")

        video_rows = download_videos_for_block(block, video_block_dirs[block.index])
        video_rows_all.extend(video_rows)
        print(f"  Итого видео скачано: {len(video_rows)}")

    wm = move_watermarks(photo_archive)
    write_report(root / "report_watermarks.csv", wm)
    print(f"\nWatermark-подозрения перенесены: {len(wm)}")

    dups = move_duplicates(photo_archive)
    write_report(root / "report_duplicates.csv", dups)
    print(f"Дубли перенесены: {len(dups)}")

    write_report(root / "report_videos.csv", video_rows_all)

    print("\nГОТОВО.")
    print("Папка проекта:", root)


if __name__ == "__main__":
    main()
