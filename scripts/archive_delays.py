from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_INDEX_URL = "https://pkp-archive.2137.workers.dev/"
DEFAULT_ARCHIVE_DIR = "archive"
DEFAULT_REPO_INDEX_PATH = "index.json"
USER_AGENT = "opoznienia-archive/1.0"


@dataclass(frozen=True)
class SourceRecord:
    canonical_source: str
    source_url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive missing PKP delay source files from the public index endpoint."
    )
    parser.add_argument("--index-url", default=DEFAULT_INDEX_URL)
    parser.add_argument("--root-dir", default=".")
    parser.add_argument("--archive-dir", default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--repo-index-path", default=DEFAULT_REPO_INDEX_PATH)
    parser.add_argument("--raw-base-url")
    parser.add_argument("--github-repository")
    parser.add_argument("--github-ref-name")
    parser.add_argument("--limit-dates", type=int)
    parser.add_argument("--limit-files-per-date", type=int)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def canonicalize_source_url(source_url: str) -> str:
    parts = urlsplit(source_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def open_with_retries(request: Request, timeout: int, retries: int, retry_delay: float):
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise
            last_error = error
        except (TimeoutError, URLError, OSError) as error:
            last_error = error

        if attempt < retries:
            time.sleep(retry_delay * attempt)

    if last_error is None:
        raise RuntimeError("Request failed without raising a tracked exception")
    raise last_error


def fetch_json(url: str, timeout: int, retries: int, retry_delay: float) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with open_with_retries(request, timeout=timeout, retries=retries, retry_delay=retry_delay) as response:
        return json.load(response)


def download_file(
    url: str,
    destination: Path,
    timeout: int,
    retries: int,
    retry_delay: float,
) -> tuple[int, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with open_with_retries(request, timeout=timeout, retries=retries, retry_delay=retry_delay) as response:
        payload = response.read()

    destination.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return len(payload), digest


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        return {"items": []}

    with manifest_path.open("r", encoding="utf-8") as file_handle:
        manifest = json.load(file_handle)

    if not isinstance(manifest, dict):
        raise ValueError(f"Manifest at {manifest_path} is not a JSON object")

    manifest.setdefault("items", [])
    return manifest


def write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def run_git_command(root_dir: Path, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root_dir), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    output = completed.stdout.strip()
    return output or None


def parse_github_repository(remote_url: str) -> str | None:
    normalized = remote_url.strip()
    if normalized.endswith(".git"):
        normalized = normalized[:-4]

    if normalized.startswith("git@github.com:"):
        return normalized.split(":", maxsplit=1)[1]

    prefix = "https://github.com/"
    if normalized.startswith(prefix):
        return normalized[len(prefix):]

    return None


def discover_github_repository(root_dir: Path) -> str | None:
    remote_url = run_git_command(root_dir, ["config", "--get", "remote.origin.url"])
    if remote_url is None:
        return None
    return parse_github_repository(remote_url)


def discover_git_ref_name(root_dir: Path) -> str | None:
    return run_git_command(root_dir, ["branch", "--show-current"])


def resolve_raw_base_url(args: argparse.Namespace, root_dir: Path) -> str:
    if args.raw_base_url:
        return args.raw_base_url.rstrip("/")

    repository = (
        args.github_repository
        or os.environ.get("GITHUB_REPOSITORY")
        or discover_github_repository(root_dir)
    )
    ref_name = (
        args.github_ref_name
        or os.environ.get("GITHUB_REF_NAME")
        or discover_git_ref_name(root_dir)
    )

    if not repository or not ref_name:
        raise ValueError(
            "Unable to determine GitHub repository raw URL. Set --raw-base-url or provide repository/ref information."
        )

    return f"https://raw.githubusercontent.com/{repository}/{ref_name}"


def date_sort_key(date_key: str) -> tuple[int, ...]:
    return tuple(int(part) for part in date_key.split("-"))


def build_repo_index(archive_root: Path, raw_base_url: str) -> dict[str, Any]:
    files: dict[str, list[str]] = {}

    for manifest_path in sorted(archive_root.rglob("manifest.json")):
        manifest = load_manifest(manifest_path)
        date_key = manifest.get("date")
        if not isinstance(date_key, str):
            year, month, day = manifest_path.parent.parts[-3:]
            date_key = f"{int(year)}-{int(month)}-{int(day)}"

        urls: list[str] = []
        for item in manifest.get("items", []):
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            if not isinstance(filename, str):
                continue
            relative_path = (manifest_path.parent / filename).relative_to(archive_root.parent).as_posix()
            urls.append(f"{raw_base_url}/{relative_path}")

        if urls:
            files[date_key] = urls

    ordered_files = {
        date_key: files[date_key]
        for date_key in sorted(files, key=date_sort_key, reverse=True)
    }
    return {"error": True, "files": ordered_files}


def write_repo_index(index_path: Path, payload: dict[str, Any]) -> None:
    index_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def list_existing_archive_files(day_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in day_dir.glob("source-*.json")
        if path.is_file()
    )


def merge_legacy_files_into_manifest(manifest: dict[str, Any], day_dir: Path) -> None:
    items = manifest.get("items", [])
    known_filenames = {item.get("filename") for item in items if isinstance(item, dict)}
    for archive_file in list_existing_archive_files(day_dir):
        if archive_file.name in known_filenames:
            continue
        items.append(
            {
                "filename": archive_file.name,
                "canonical_source": None,
                "source_url": None,
                "downloaded_at": None,
                "sha256": None,
                "size_bytes": archive_file.stat().st_size,
            }
        )
    manifest["items"] = items


def next_archive_filename(manifest: dict[str, Any]) -> str:
    next_index = len(manifest.get("items", [])) + 1
    return f"source-{next_index:03d}.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def date_key_parts(date_key: str) -> tuple[str, str, str]:
    year, month, day = date_key.split("-")
    return year, month.zfill(2), day.zfill(2)


def archive_date(
    archive_root: Path,
    date_key: str,
    source_urls: list[str],
    timeout: int,
    retries: int,
    retry_delay: float,
    dry_run: bool,
    limit_files_per_date: int | None,
) -> tuple[int, int]:
    year, month, day = date_key_parts(date_key)
    day_dir = archive_root / year / month / day
    manifest_path = day_dir / "manifest.json"
    day_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(manifest_path)
    merge_legacy_files_into_manifest(manifest, day_dir)

    known_sources = {
        item["canonical_source"]
        for item in manifest["items"]
        if isinstance(item, dict) and item.get("canonical_source")
    }

    pending_urls: list[SourceRecord] = []
    for source_url in source_urls:
        record = SourceRecord(
            canonical_source=canonicalize_source_url(source_url),
            source_url=source_url,
        )
        if record.canonical_source in known_sources:
            continue
        pending_urls.append(record)

    if limit_files_per_date is not None:
        pending_urls = pending_urls[:limit_files_per_date]

    downloaded = 0
    for record in pending_urls:
        filename = next_archive_filename(manifest)
        destination = day_dir / filename
        if not dry_run:
            size_bytes, sha256 = download_file(
                record.source_url,
                destination,
                timeout,
                retries,
                retry_delay,
            )
        else:
            size_bytes, sha256 = 0, "dry-run"

        manifest["items"].append(
            {
                "filename": filename,
                "canonical_source": record.canonical_source,
                "source_url": record.source_url,
                "downloaded_at": utc_now_iso(),
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        )
        known_sources.add(record.canonical_source)
        downloaded += 1
        manifest["date"] = date_key
        manifest["source_count"] = len(source_urls)
        manifest["archived_count"] = len(manifest["items"])
        manifest["updated_at"] = utc_now_iso()
        if not dry_run:
            write_manifest(manifest_path, manifest)

    manifest["date"] = date_key
    manifest["source_count"] = len(source_urls)
    manifest["archived_count"] = len(manifest["items"])
    manifest["updated_at"] = utc_now_iso()

    if not dry_run:
        write_manifest(manifest_path, manifest)

    return downloaded, len(source_urls)


def main() -> int:
    args = parse_args()
    root_dir = Path(args.root_dir).resolve()
    archive_root = root_dir / args.archive_dir
    archive_root.mkdir(parents=True, exist_ok=True)

    payload = fetch_json(args.index_url, args.timeout, args.retries, args.retry_delay)
    files_by_date = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files_by_date, dict):
        raise ValueError("Endpoint response does not contain a 'files' object")

    date_items = sorted(files_by_date.items(), key=lambda item: date_sort_key(item[0]))
    if args.limit_dates is not None:
        date_items = date_items[: args.limit_dates]

    total_downloaded = 0
    total_sources = 0
    processed_dates = 0

    for date_key, urls in date_items:
        if not isinstance(urls, list):
            continue
        source_urls = [url for url in urls if isinstance(url, str)]
        downloaded, source_count = archive_date(
            archive_root=archive_root,
            date_key=date_key,
            source_urls=source_urls,
            timeout=args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
            dry_run=args.dry_run,
            limit_files_per_date=args.limit_files_per_date,
        )
        total_downloaded += downloaded
        total_sources += source_count
        processed_dates += 1
        print(f"{date_key}: downloaded {downloaded}, indexed {source_count}")

    if not args.dry_run:
        raw_base_url = resolve_raw_base_url(args, root_dir)
        repo_index = build_repo_index(archive_root, raw_base_url)
        write_repo_index(root_dir / args.repo_index_path, repo_index)

    print(
        f"Processed {processed_dates} dates, indexed {total_sources} sources, downloaded {total_downloaded} new files."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
