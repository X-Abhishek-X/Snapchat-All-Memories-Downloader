import argparse
import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import exif
import httpx
from pydantic import BaseModel, Field, field_validator
from tqdm.asyncio import tqdm


class Memory(BaseModel):
    date: datetime = Field(alias="Date")
    download_link: str = Field(alias="Download Link")
    location: str = Field(default="", alias="Location")
    latitude: float | None = None
    longitude: float | None = None

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, v):
        if isinstance(v, str):
            # Parse as UTC and make timezone-aware
            dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S UTC")
            return dt.replace(tzinfo=timezone.utc)
        return v

    def model_post_init(self, __context):
        if self.location and not self.latitude:
            if match := re.search(r"([-\d.]+),\s*([-\d.]+)", self.location):
                self.latitude = float(match.group(1))
                self.longitude = float(match.group(2))

    @property
    def local_date(self) -> datetime:
        """Convert UTC date to system's local timezone"""
        # Convert UTC to local timezone automatically
        return self.date.astimezone()

    @property
    def filename(self) -> str:
        """Generate filename using system's local timezone"""
        return self.local_date.strftime("%Y-%m-%d_%H-%M-%S")


class Stats(BaseModel):
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    mb: float = 0


def load_memories(json_path: Path) -> list[Memory]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [Memory(**item) for item in data["Saved Media"]]


def update_memories_json(json_path: Path, downloaded_memories: list[Memory], no_cleanup: bool = False):
    """Remove successfully downloaded memories from the JSON file."""
    if no_cleanup or not downloaded_memories:
        return
    
    # Create backup with timestamp to avoid overwriting previous backups
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = json_path.with_suffix(f".json.backup.{timestamp}")
    shutil.copy2(json_path, backup_path)
    print(f"\nBackup created: {backup_path}")
    
    # Load original data
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Create set of downloaded memory identifiers (using download_link as unique ID)
    downloaded_links = {mem.download_link for mem in downloaded_memories}
    
    # Filter out downloaded memories
    original_count = len(data["Saved Media"])
    data["Saved Media"] = [
        item for item in data["Saved Media"]
        if item.get("Download Link") not in downloaded_links
    ]
    removed_count = original_count - len(data["Saved Media"])
    
    # Save updated data
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"Updated {json_path}: Removed {removed_count} successfully downloaded entries")
    print(f"Remaining entries (failed/skipped): {len(data['Saved Media'])}")


async def get_cdn_url(download_link: str) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            download_link,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.text.strip()


def add_exif_data(image_path: Path, memory: Memory):
    try:
        with open(image_path, "rb") as f:
            img = exif.Image(f)

        # Use local timezone for EXIF data
        dt_str = memory.local_date.strftime("%Y:%m:%d %H:%M:%S")
        img.datetime_original = dt_str
        img.datetime_digitized = dt_str
        img.datetime = dt_str

        if memory.latitude is not None and memory.longitude is not None:
            # Convert decimal degrees to degrees, minutes, seconds
            def decimal_to_dms(decimal):
                degrees = int(abs(decimal))
                minutes_decimal = (abs(decimal) - degrees) * 60
                minutes = int(minutes_decimal)
                seconds = (minutes_decimal - minutes) * 60
                return (degrees, minutes, seconds)
            
            lat_dms = decimal_to_dms(memory.latitude)
            lon_dms = decimal_to_dms(memory.longitude)
            
            img.gps_latitude = lat_dms
            img.gps_latitude_ref = "N" if memory.latitude >= 0 else "S"
            img.gps_longitude = lon_dms
            img.gps_longitude_ref = "E" if memory.longitude >= 0 else "W"

        with open(image_path, "wb") as f:
            f.write(img.get_file())
    except Exception:
        pass


async def download_memory(
    memory: Memory, output_dir: Path, add_exif: bool, semaphore: asyncio.Semaphore, max_retries: int = 3
) -> tuple[bool, int]:
    async with semaphore:
        for attempt in range(max_retries):
            try:
                cdn_url = await get_cdn_url(memory.download_link)
                ext = Path(cdn_url.split("?")[0]).suffix or ".jpg"
                output_path = output_dir / f"{memory.filename}{ext}"

                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    response = await client.get(cdn_url)
                    response.raise_for_status()

                    output_path.write_bytes(response.content)

                    # Use local timezone for file timestamp
                    timestamp = memory.local_date.timestamp()
                    os.utime(output_path, (timestamp, timestamp))

                    if add_exif and ext == ".jpg":
                        add_exif_data(output_path, memory)

                    return True, len(response.content)
            except Exception as e:
                if attempt < max_retries - 1:
                    # Exponential backoff: 1s, 2s, 4s, ...
                    wait_time = 2 ** attempt
                    await asyncio.sleep(wait_time)
                else:
                    print(f"\n❌ Failed [{memory.filename}] after {max_retries} attempt(s): {e}")
                    return False, 0
        # All attempts exhausted
        return False, 0


async def download_all(
    memories: list[Memory],
    output_dir: Path,
    max_concurrent: int,
    add_exif: bool,
    skip_existing: bool,
    max_retries: int,
    json_path: Path,
    no_cleanup: bool,
):
    output_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max_concurrent)
    stats = Stats()
    start_time = time.time()

    # Track memories by status
    downloaded_memories = []
    failed_memories = []
    to_download = []
    
    for memory in memories:
        jpg_path = output_dir / f"{memory.filename}.jpg"
        mp4_path = output_dir / f"{memory.filename}.mp4"
        if skip_existing and (jpg_path.exists() or mp4_path.exists()):
            stats.skipped += 1
        else:
            to_download.append(memory)

    if not to_download:
        print("All files already downloaded!")
        return

    progress_bar = tqdm(
        total=len(to_download),
        desc="Downloading",
        unit="file",
        disable=False,
    )

    async def process_and_update(memory):
        success, bytes_downloaded = await download_memory(
            memory, output_dir, add_exif, semaphore, max_retries
        )
        if success:
            stats.downloaded += 1
            downloaded_memories.append(memory)
        else:
            stats.failed += 1
            failed_memories.append(memory)
        stats.mb += bytes_downloaded / 1024 / 1024

        elapsed = time.time() - start_time
        mb_per_sec = (stats.mb) / elapsed if elapsed > 0 else 0
        progress_bar.set_postfix({"MB/s": f"{mb_per_sec:.2f}"}, refresh=False)
        progress_bar.update(1)

    try:
        # Handle graceful cancellation
        await asyncio.gather(*[process_and_update(m) for m in to_download])
    except asyncio.CancelledError:
        print("\n\n⚠️  Download cancelled by user!")
        raise
    except KeyboardInterrupt:
        print("\n\n⚠️  Download cancelled by user!")
        raise
    finally:
        # Always clean up, even on cancellation
        progress_bar.close()
        elapsed = time.time() - start_time
        mb_total = stats.mb
        mb_per_sec = mb_total / elapsed if elapsed > 0 else 0
        print(
            f"\n{'='*50}\nDownloaded: {stats.downloaded} ({mb_total:.1f} MB @ {mb_per_sec:.2f} MB/s) | Skipped: {stats.skipped} | Failed: {stats.failed}\n{'='*50}"
        )
        
        # Update JSON file to remove successfully downloaded entries
        if downloaded_memories:
            print(f"\nCleaning up JSON file...")
            update_memories_json(json_path, downloaded_memories, no_cleanup)

        # Report failed files so the user knows what to retry
        if failed_memories:
            print(f"\n⚠️  {len(failed_memories)} file(s) failed to download:")
            for mem in failed_memories:
                print(f"   • {mem.filename} — {mem.download_link[:60]}...")


async def main():
    parser = argparse.ArgumentParser(
        description="Download Snapchat memories from data export"
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        default="json/memories_history.json",
        help="Path to memories_history.json (default: json/memories_history.json)",
    )
    parser.add_argument(
        "-o", "--output", default="./downloads", help="Output directory"
    )
    parser.add_argument(
        "-c", "--concurrent", type=int, default=40, help="Max concurrent downloads"
    )
    parser.add_argument(
        "--max-retries", type=int, default=3, help="Max retry attempts for failed downloads"
    )
    parser.add_argument("--no-exif", action="store_true", help="Disable EXIF metadata")
    parser.add_argument(
        "--no-skip-existing", action="store_true", help="Re-download existing files"
    )
    parser.add_argument(
        "--no-cleanup", action="store_true", help="Don't remove downloaded entries from JSON"
    )
    args = parser.parse_args()

    json_path = Path(args.json_file)
    output_dir = Path(args.output)

    print("📸 Snapchat Memories Downloader")
    print("Press Ctrl+C to cancel and clean up JSON file\n")

    # Check if JSON file exists
    if not json_path.exists():
        print(f"❌ Error: Could not find '{json_path}'")
        if args.json_file == "json/memories_history.json":
            print("\n💡 Tip: Make sure you've:")
            print("   1. Extracted the Snapchat ZIP file")
            print("   2. Created a 'json' folder in this directory")
            print("   3. Copied 'memories_history.json' into the 'json' folder")
            print("\nOr specify a custom path: python main.py /path/to/memories_history.json")
        return

    try:
        memories = load_memories(json_path)

        await download_all(
            memories,
            output_dir,
            args.concurrent,
            not args.no_exif,
            not args.no_skip_existing,
            args.max_retries,
            json_path,
            args.no_cleanup,
        )
    except KeyboardInterrupt:
        print("\n\n✅ Gracefully cancelled. JSON file has been cleaned up.")
        return


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n✅ Download cancelled.")
