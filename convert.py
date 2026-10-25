#!/usr/bin/env python3
"""
Switch Save Converter - Convert between JKSV and Eden save formats.

Supports converting Nintendo Switch save data between:
  - JKSV format (folder per game, zip files named by user and date)
  - Eden format (zip files named by game and date, containing title ID folder)

Title ID resolution when converting JKSV -> Eden:
  1. --title-id argument
  2. Scan save files for embedded Title ID in binary
  3. --game-name argument
  4. Game title from source (SwitchBrew database lookup)
  5. Placeholder fallback (or fail with --strict)

Usage:
    python convert.py --input <format> --output <format> --source <path> \
                      [--username <name>] [--title-id <id>] \
                      [--game-name <name>] [--strict]

Arguments:
    --input       Source format: "jksv" or "eden"
    --output      Target format: "jksv" or "eden"
    --source      Path to the source save data (folder or zip file)
    --username    Optional username for output (default: "User")
    --title-id    Optional title ID override (16 hex chars, no 0x prefix)
    --game-name   Optional game name for Title ID lookup from SwitchBrew
    --strict      Fail instead of using a placeholder when Title ID is not found
    --refresh-db  Force refresh the Title ID database from SwitchBrew

Examples:
    python convert.py --input jksv --output eden --source ./saves
    python convert.py --input jksv --output eden --source ./backup.zip \
                    --username MyPlayer --title-id 01008F6D12349000
    python convert.py --input jksv --output eden --source ./saves \
                    --game-name "The Legend of Zelda: Breath of the Wild"
    python convert.py --input jksv --output eden --source ./saves --strict
    python convert.py --input eden --output jksv --source ./eden_saves
"""

import argparse
import html
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional


# ---------------------------------------------------------------------------
# Title ID resolution
# ---------------------------------------------------------------------------

class TitleIdMapper:
    """Maps Switch game names to title IDs (Program IDs).

    Fetches data from SwitchBrew's official title list and caches it locally.
    """

    SWITCHBREW_URL = (
        "https://switchbrew.org/w/index.php?"
        "title=Title_list/Games&action=raw"
    )

    CACHE_DIR = Path(__file__).parent / "cache"
    CACHE_FILE = CACHE_DIR / "title_id_cache.json"
    CACHE_TTL = 86400  # 24 hours

    def __init__(self) -> None:
        self._id_to_name: Dict[str, str] = {}
        self._name_to_id: Dict[str, str] = {}
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_cache()

    # --- cache ---

    def _load_cache(self) -> None:
        if not self.CACHE_FILE.exists():
            return
        try:
            ts_file = self.CACHE_DIR / "_cache_ts"
            if ts_file.exists():
                age = time.time() - float(ts_file.read_text().strip())
                if age > self.CACHE_TTL:
                    self.CACHE_FILE.unlink(missing_ok=True)
                    return
            with open(self.CACHE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for tid, name in data.items():
                self._id_to_name[tid] = name
                self._name_to_id[name.lower()] = tid
        except (json.JSONDecodeError, ValueError, OSError):
            self.CACHE_FILE.unlink(missing_ok=True)

    def _save_cache(self) -> None:
        try:
            with open(self.CACHE_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._id_to_name, fh, ensure_ascii=False, indent=2)
            (self.CACHE_DIR / "_cache_ts").write_text(str(time.time()))
        except OSError:
            pass

    # --- fetch ---

    def refresh(self) -> bool:
        """Fetch fresh title list from SwitchBrew and update cache."""
        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(
                self.SWITCHBREW_URL,
                headers={"User-Agent": "SwitchSaveConverter/1.0"},
            )
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                html_text = resp.read().decode("utf-8")
        except Exception as exc:
            print(f"[TitleIdMapper] Failed to fetch from SwitchBrew: {exc}")
            return False

        self._parse_switchbrew_table(html_text)
        self._save_cache()
        return True

    def _parse_switchbrew_table(self, raw: str) -> None:
        """Parse the raw wiki markup table from SwitchBrew's title list page.

        Format example:
          {| class="wikitable sortable"
          ! ProgramId || Description || Region || ...
          |-
          | 0100000000010000 || Super Mario Odyssey || CHN EUR JPN || ...
        """
        lines = raw.split("\n")
        in_table = False

        for line in lines:
            stripped = line.strip()

            # Detect table start/end
            if stripped.startswith("{|"):
                in_table = True
                continue
            if stripped == "|}":
                in_table = False
                continue

            # Skip non-table content
            if not in_table:
                continue

            # Detect table header row
            if stripped.startswith("!"):
                continue

            # Detect row separator
            if stripped.startswith("|-"):
                continue

            # Parse data row: starts with |
            if stripped.startswith("|"):
                content = stripped.lstrip("|").strip()
                cells = [c.strip() for c in content.split("||")]
                if len(cells) < 2:
                    continue

                tid_raw = cells[0].strip()
                title = cells[1].strip()

                # Validate title ID: 16 hex characters (Program ID)
                tid = tid_raw.upper()
                if len(tid) == 16 and re.fullmatch(
                    r"[0-9A-F]{16}", tid
                ):
                    # Clean up title
                    title = re.sub(r"\s*\(.*?\)", "", title).strip()
                    title = re.sub(r"\s*\[.*?\]", "", title).strip()
                    title = re.sub(r"[^\x00-\x7F]+", "", title).strip()
                    title = title.replace("\u2122", "").replace(
                        "\u00ae", ""
                    )
                    if title:
                        self._id_to_name[tid] = title
                        self._name_to_id[title.lower()] = tid

    # --- lookup ---

    def lookup_by_name(self, name: str) -> Optional[str]:
        """Find title ID by game name (case-insensitive, accent-insensitive)."""
        if not name:
            return None
        name_clean = name.lower().strip()
        # Direct lookup
        if name_clean in self._name_to_id:
            return self._name_to_id[name_clean]
        # Partial match
        for game_name, tid in self._name_to_id.items():
            if name_clean in game_name or game_name in name_clean:
                return tid
        return None

    def lookup_by_id(self, tid: str) -> Optional[str]:
        """Find game name by title ID."""
        tid = tid.strip().upper().removeprefix("0X")
        return self._id_to_name.get(tid)


def _normalize_tid(tid: str) -> str:
    """Normalize a title ID: strip 0x prefix, uppercase, pad to 16 chars."""
    tid = tid.strip().upper().removeprefix("0X")
    tid = re.sub(r"[^0-9A-F]", "", tid)
    return tid.zfill(16)[:16]


def _extract_tid_from_binary(data: bytes) -> Optional[str]:
    """Scan binary data for a 16-character hex title ID (Program ID)."""
    # Nintendo Title IDs are typically 16 hex chars (e.g. 01008F6D12349000)
    # They always start with 0100 for retail games
    pattern = re.compile(rb"0[0-9A-Fa-f]{15}")
    for match in pattern.finditer(data):
        candidate = match.group(0).decode("ascii", errors="ignore").upper()
        if candidate and re.fullmatch(r"0[0-9A-F]{15}", candidate):
            return candidate
    return None


def _scan_save_files_for_tid(
    save_files: List[Tuple[Path, str]],
) -> Optional[str]:
    """Scan extracted save files for a title ID in their binary content."""
    # Priority: scan files that are more likely to contain metadata
    priority = {"main", "save", "header", "meta", "info"}
    sorted_files = sorted(
        save_files,
        key=lambda f: (
            0
            if f[1].lower() in priority
            else 1,
        ),
    )
    for src_path, rel_path in sorted_files:
        try:
            if src_path.stat().st_size > 50 * 1024 * 1024:
                continue
            with open(src_path, "rb") as fh:
                chunk = fh.read()
            tid = _extract_tid_from_binary(chunk)
            if tid:
                return tid
        except (OSError, IOError):
            continue
    return None


def resolve_title_id(
    game_title: str,
    save_files: List[Tuple[Path, str]],
    mapper: TitleIdMapper,
    manual_tid: Optional[str] = None,
    game_name_lookup: Optional[str] = None,
    required: bool = False,
    allow_auto_lookup: bool = True,
) -> str:
    """Resolve a title ID using a chain of fallback strategies.

    Args:
        game_title:         The game title from the save data itself.
        save_files:         List of (path, rel_path) tuples to scan.
        mapper:             TitleIdMapper instance.
        manual_tid:         Optional --title-id override.
        game_name_lookup:   Optional --game-name override for DB lookup.
        required:           If True, fail when title ID cannot be found
                            (instead of using a placeholder).
        allow_auto_lookup:  If False, skip the automatic lookup by game_title.
                            Only --game-name or --title-id will be used.
                            Useful for JKSV → Eden where folder names are
                            arbitrary and not reliable for DB lookup.

    Returns a 16-char uppercase hex title ID string.
    """
    # 1. Manual override
    if manual_tid:
        tid = _normalize_tid(manual_tid)
        if len(tid) == 16:
            print(f"  Using provided title ID: {tid}")
            return tid
        print(f"  Warning: Invalid title ID '{manual_tid}', ignoring.")

    # 2. Scan save data files for embedded title ID
    print("  Scanning save files for embedded Title ID...")
    tid = _scan_save_files_for_tid(save_files)
    if tid:
        print(f"  Found embedded Title ID: {tid}")
        return tid
    print("  No embedded Title ID found in save files.")

    # 3. Look up by game name in SwitchBrew database
    # Only auto-lookup if allowed (disabled for JKSV without --game-name)
    lookup_name = None
    if game_name_lookup:
        lookup_name = game_name_lookup
        print(f"  Looking up via --game-name: {lookup_name}")
    elif allow_auto_lookup and game_title:
        lookup_name = game_title
        print(f"  Auto-lookup by folder name: {lookup_name}")

    if lookup_name:
        tid = mapper.lookup_by_name(lookup_name)
        if tid:
            found_name = mapper.lookup_by_id(tid)
            print(f"  Found: {found_name} ({tid})")
            return tid
        if not game_name_lookup:
            print(
                f"  '{lookup_name}' not found in SwitchBrew database. "
                "Use --game-name with the official game title."
            )

    # 4. Fail or fallback
    display_name = game_name_lookup if game_name_lookup else game_title
    if required:
        print(
            f"Error: Could not find a title ID for '{display_name}'. "
            "Provide a valid --title-id or a matching --game-name."
        )
        sys.exit(1)

    print(
        f"  Warning: Could not find title ID for '{display_name}'. "
        "Using placeholder (Eden may reject the import)."
    )
    return "0000000000000000"


# ---------------------------------------------------------------------------
# Save data extraction
# ---------------------------------------------------------------------------

class SaveData:
    """Holds extracted save data from any source format."""

    def __init__(self):
        self.game_title: str = "Unknown Game"
        self.user_id: str = ""
        self.date_dt: datetime = datetime.now()
        self.date_str: str = ""
        self.username: str = "User"
        self.files: List[Tuple[Path, str]] = []
        self.source_format: str = ""
        self._temp_dir: Optional[Path] = None

    def cleanup(self) -> None:
        """Remove temporary extraction directory if it exists."""
        if self._temp_dir and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir)
            self._temp_dir = None


class JKSVReader:
    """Read save data from JKSV format.

    JKSV folder structure:
        Game Title/
            User1 - 2024-01-15_10-30-00.zip
            User2 - 2024-01-15_11-00-00.zip

    Inside each zip: save files directly (no subfolder).
    """

    DATE_FORMAT = "%Y-%m-%d_%H-%M-%S"

    @staticmethod
    def _is_jksv_zip(filepath: Path) -> bool:
        """Check if filename matches JKSV naming convention."""
        stem = filepath.stem
        parts = stem.rsplit(" - ", 1)
        if len(parts) != 2:
            return False
        username, date_part = parts
        if not username.strip():
            return False
        try:
            datetime.strptime(date_part, JKSVReader.DATE_FORMAT)
            return True
        except ValueError:
            return False

    @classmethod
    def read_from_folder(cls, folder: Path) -> List[SaveData]:
        """Read all save sets from a JKSV folder."""
        game_titles = [d for d in folder.iterdir() if d.is_dir()]
        saves = []

        for game_dir in game_titles:
            zip_files = sorted(
                [
                    f
                    for f in game_dir.iterdir()
                    if f.is_file() and f.suffix == ".zip"
                ],
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )

            for zip_path in zip_files:
                if not cls._is_jksv_zip(zip_path):
                    continue

                stem = zip_path.stem
                parts = stem.rsplit(" - ", 1)
                username = parts[0].strip()
                date_part = parts[1]
                date_dt = datetime.strptime(date_part, cls.DATE_FORMAT)

                save = SaveData()
                save.game_title = game_dir.name
                save.username = username
                save.date_dt = date_dt
                save.date_str = date_part
                save.source_format = "jksv"

                temp_dir = folder / "_temp_extract"
                temp_dir.mkdir(exist_ok=True)
                save._temp_dir = temp_dir

                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(temp_dir)

                save.files = cls._collect_files(temp_dir)
                saves.append(save)

        return saves

    @classmethod
    def read_from_single_zip(
        cls, zip_path: Path, username: str, game_name: Optional[str] = None
    ) -> SaveData:
        """Read a single JKSV zip file."""
        save = SaveData()
        save.username = username
        save.date_dt = datetime.now()
        save.source_format = "jksv"

        # Always try to extract date from JKSV filename pattern
        stem = zip_path.stem
        parts = stem.rsplit(" - ", 1)
        if len(parts) == 2:
            test_user = parts[0].strip()
            try:
                datetime.strptime(parts[1], cls.DATE_FORMAT)
                save.username = test_user if not username else username
                save.date_str = parts[1]
                # Re-parse with extracted date
                save.date_dt = datetime.strptime(parts[1], cls.DATE_FORMAT)
            except ValueError:
                pass

        # Determine game title: use --game-name, or parent folder name, or zip stem
        if game_name:
            save.game_title = game_name
        elif save.date_str:
            save.game_title = parts[0].strip()
        else:
            parent = zip_path.parent.name
            if parent.lower() not in (".", "jksv", "saves", "backup"):
                save.game_title = parent
            else:
                save.game_title = stem

        temp_dir = zip_path.parent / "_temp_extract_jksv"
        temp_dir.mkdir(exist_ok=True)
        save._temp_dir = temp_dir

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)

        save.files = cls._collect_files(temp_dir)
        return save

    @staticmethod
    def _collect_files(
        base_dir: Path,
    ) -> List[Tuple[Path, str]]:
        """Collect all files with their relative paths, excluding system files."""
        result = []
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [
                d
                for d in dirs
                if d not in ("__MACOSX", ".DS_Store")
                and not d.startswith("_temp_extract")
            ]
            for fname in files:
                if fname in ("__MACOSX", ".DS_Store"):
                    continue
                src = Path(root) / fname
                rel = src.relative_to(base_dir)
                result.append((src, str(rel)))
        return result


class EdenReader:
    """Read save data from Eden format.

    Eden folder structure:
        Game Name Save Data - 2024-01-15 10_30.zip

    Inside each zip: TitleID/... (save files directly in the TitleID folder).
    """

    DATE_FORMAT = "%Y-%m-%d %H_%M"

    @staticmethod
    def _is_eden_zip(filepath: Path) -> bool:
        """Check if filename matches Eden naming convention."""
        stem = filepath.stem
        if "save data" in stem.lower():
            return True
        if stem.startswith("[Eden]"):
            return True
        return False

    @classmethod
    def read_from_folder(cls, folder: Path) -> List[SaveData]:
        """Read all save sets from an Eden folder."""
        zip_files = sorted(
            [
                f
                for f in folder.iterdir()
                if f.is_file() and f.suffix == ".zip"
            ],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )

        saves = []
        for zip_path in zip_files:
            if not cls._is_eden_zip(zip_path):
                continue

            save = cls._parse_zip(zip_path)
            if save:
                saves.append(save)

        return saves

    @classmethod
    def read_from_single_zip(cls, zip_path: Path) -> SaveData:
        """Read a single Eden zip file."""
        return cls._parse_zip(zip_path)

    @classmethod
    def _parse_zip(cls, zip_path: Path) -> Optional[SaveData]:
        """Parse an Eden zip file and return a SaveData object."""
        stem = zip_path.stem
        parts = stem.rsplit(" - ", 1)

        if len(parts) != 2:
            return None

        game_raw = (
            parts[0]
            .replace("save data", "")
            .replace("Save Data", "")
            .strip()
        )
        # Remove [Eden] prefix if present
        game_raw = re.sub(r"^\[Eden\]\s*", "", game_raw).strip()
        date_part = parts[1]

        date_dt = datetime.now()
        try:
            date_dt = datetime.strptime(date_part, cls.DATE_FORMAT)
        except ValueError:
            pass

        save = SaveData()
        save.game_title = game_raw if game_raw else "Unknown Game"
        save.date_dt = date_dt
        save.date_str = date_part
        save.username = "User"
        save.source_format = "eden"

        temp_dir = zip_path.parent / "_temp_extract_eden"
        temp_dir.mkdir(exist_ok=True)
        save._temp_dir = temp_dir

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)

        sub_folders = [d for d in temp_dir.iterdir() if d.is_dir()]
        if not sub_folders:
            save.cleanup()
            return None

        id_folder = sub_folders[0].resolve()
        save.user_id = sub_folders[0].name

        save.files = []
        for root, dirs, files in os.walk(str(id_folder)):
            dirs[:] = [
                d
                for d in dirs
                if d not in ("__MACOSX", ".DS_Store")
                and not d.startswith("_temp_extract")
            ]
            for fname in files:
                if fname in ("__MACOSX", ".DS_Store"):
                    continue
                src = Path(root) / fname
                rel = Path(src).relative_to(id_folder)
                # Remove intermediate "folder" subdirectory if present
                rel_parts = [p for p in rel.parts if p != "folder"]
                arcname = "/".join(rel_parts)
                save.files.append((src, arcname))

        return save


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

class JKSVWriter:
    """Write save data to JKSV format.

    Output structure:
        output_folder/[Format] [Game Title] - [Date_Time].zip
    """

    DATE_FORMAT = "%Y-%m-%d %H_%M"

    @classmethod
    def write(cls, save: SaveData, output_folder: Path) -> Path:
        """Write save data as a JKSV zip file."""
        game_title = sanitize_filename(save.game_title)
        # Remove [Eden] prefix if present to avoid [JKSV] [Eden] ...
        game_title = re.sub(r"^\[Eden\]\s*", "", game_title)
        date_str = save.date_str
        if not date_str:
            date_str = save.date_dt.strftime(cls.DATE_FORMAT)
        zip_name = f"[JKSV] {game_title} - {date_str}.zip"
        zip_path = output_folder / zip_name

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src_file, rel_path in save.files:
                if "__MACOSX" in rel_path or ".DS_Store" in rel_path:
                    continue
                zf.write(src_file, arcname=rel_path)

        return zip_path


class EdenWriter:
    """Write save data to Eden format.

    Output structure:
        output_folder/[Format] [Game Title] - [Date_Time].zip
        Inside: TitleID/... (save files directly in the TitleID folder)
    """

    DATE_FORMAT = "%Y-%m-%d %H_%M"

    @classmethod
    def write(cls, save: SaveData, output_folder: Path) -> Path:
        """Write save data as an Eden zip file.

        Each zip contains: TitleID/filename (files directly in the TitleID folder).
        """
        game_title = sanitize_filename(save.game_title)
        # Remove [JKSV] prefix if present to avoid [Eden] [JKSV] ...
        game_title = re.sub(r"^\[JKSV\]\s*", "", game_title)
        date_str = save.date_str
        if not date_str:
            date_str = save.date_dt.strftime(cls.DATE_FORMAT)
        zip_name = f"[Eden] {game_title} - {date_str}.zip"
        zip_path = output_folder / zip_name

        tid = save.user_id.strip().upper()
        if tid.startswith("0X"):
            tid = tid[2:]

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src_file, rel_path in save.files:
                if "__MACOSX" in rel_path or ".DS_Store" in rel_path:
                    continue
                # Clean the path: remove backslashes, ensure no empty parts
                clean = rel_path.replace("\\", "/")
                parts = [p for p in clean.split("/") if p]
                # Ensure the path starts with the TitleID
                if not parts or parts[0].upper() != tid:
                    parts = [tid] + parts
                arcname = "/".join(parts)
                zf.write(src_file, arcname=arcname)

        return zip_path


# ---------------------------------------------------------------------------
# Conversion logic
# ---------------------------------------------------------------------------

def sanitize_filename(text: str) -> str:
    """Sanitize text for safe use in filenames and folder names."""
    return text.replace(":", "_").replace("\\", "/").replace("*", "")


def convert_jksv_to_eden(
    save: SaveData,
    username: str,
    output_folder: Path,
    title_id_override: Optional[str],
    mapper: TitleIdMapper,
    game_name_lookup: Optional[str] = None,
    required: bool = False,
) -> Path:
    """Convert JKSV save data to Eden format.

    Eden requires a title ID folder inside the zip.
    Resolution chain:
      1. --title-id argument
      2. Scan save files for embedded Title ID in binary
      3. --game-name argument (or --strict)
      4. Placeholder fallback (or fail with --strict)

    When --game-name is provided, it overrides the game title used
    in the output filename.

    Note: Auto-lookup by folder name is disabled for JKSV because
    folder names are arbitrary and not reliable for DB lookup.
    """
    # Use --game-name for the output filename if provided
    if game_name_lookup:
        save.game_title = game_name_lookup

    save.user_id = resolve_title_id(
        save.game_title,
        save.files,
        mapper,
        manual_tid=title_id_override,
        game_name_lookup=game_name_lookup,
        required=required,
        allow_auto_lookup=False,
    )
    print(f"  Title ID: {save.user_id}")
    return EdenWriter.write(save, output_folder)


def convert_eden_to_jksv(
    save: SaveData,
    username: str,
    output_folder: Path,
) -> Path:
    """Convert Eden save data to JKSV format.

    Applies the given username and strips the 0x prefix from the title ID.
    """
    save.username = username

    tid = save.user_id
    if tid.startswith("0x") or tid.startswith("0X"):
        tid = tid[2:]
    save.user_id = tid

    return JKSVWriter.write(save, output_folder)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Convert Nintendo Switch save data between JKSV and Eden formats."
        ),
        epilog=(
            "Examples:\n"
            "  %(prog)s --input jksv --output eden --source ./saves\n"
            "  %(prog)s --input jksv --output eden --source ./backup.zip "
            "--username MyPlayer --title-id 01008F6D12349000\n"
            '  %(prog)s --input jksv --output eden --source ./saves '
            '--game-name "The Legend of Zelda: Breath of the Wild"\n'
            "  %(prog)s --input eden --output jksv --source ./eden_saves\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        required=True,
        choices=["jksv", "eden"],
        dest="input_format",
        help="Source format (jksv or eden)",
    )
    parser.add_argument(
        "--output",
        required=True,
        choices=["jksv", "eden"],
        dest="output_format",
        help="Target format (jksv or eden)",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to source save data (folder or zip file)",
    )
    parser.add_argument(
        "--username",
        default="User",
        help="Username for output files (default: User)",
    )
    parser.add_argument(
        "--title-id",
        default=None,
        dest="title_id",
        help=(
            "Title ID override (16 hex chars, e.g. 01008F6D12349000). "
            "Used when converting JKSV -> Eden if title ID cannot be "
            "found automatically."
        ),
    )
    parser.add_argument(
        "--game-name",
        default=None,
        dest="game_name",
        help=(
            "Exact game name to use when looking up the Title ID in "
            "the SwitchBrew database. Required for JKSV -> Eden "
            "conversions unless --title-id is provided. "
            "Use the official game title (e.g. "
            "'The Legend of Zelda: Breath of the Wild')."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Currently has no effect (JKSV -> Eden is always strict). "
            "Kept for future compatibility."
        ),
    )
    parser.add_argument(
        "--refresh-db",
        action="store_true",
        help="Force refresh the title ID database from SwitchBrew.",
    )
    return parser.parse_args()


def run(
    input_format: str,
    output_format: str,
    source_path: str,
    username: str,
    title_id_override: Optional[str] = None,
    game_name_lookup: Optional[str] = None,
    strict: bool = False,
) -> None:
    """Run the conversion.

    Args:
        input_format:      "jksv" or "eden"
        output_format:     "jksv" or "eden"
        source_path:       Path to source save data
        username:          Username for output (default: "User")
        title_id_override: Manual title ID override for JKSV -> Eden
        game_name_lookup:  Game name to use for Title ID lookup
        strict:            Fail instead of using placeholder when
                           title ID cannot be resolved
    """
    if input_format == output_format:
        print("Error: Source and target formats are the same.")
        sys.exit(1)

    source = Path(source_path)
    if not source.exists():
        print(f"Error: Source path does not exist: {source_path}")
        sys.exit(1)

    # Initialize title ID mapper
    mapper = TitleIdMapper()
    if input_format == "jksv" and output_format == "eden":
        if title_id_override:
            print(f"Using title ID override: {title_id_override}")
        elif game_name_lookup:
            print(f"Looking up title ID for: {game_name_lookup}")
        else:
            print(
                "JKSV -> Eden conversion requires a Title ID. "
                "Provide --game-name with the official game title or "
                "--title-id with a 16-char hex ID."
            )
        mapper.refresh()

    # Create output directory
    script_dir = Path(__file__).parent.resolve()
    output_dir = script_dir / "output" / output_format
    output_dir.mkdir(parents=True, exist_ok=True)

    # Read source data
    saves: List[SaveData] = []

    if input_format == "jksv":
        if source.is_file():
            save = JKSVReader.read_from_single_zip(
                source, username, game_name_lookup
            )
            saves.append(save)
        elif source.is_dir():
            saves = JKSVReader.read_from_folder(source)
            if not saves:
                print("Error: No valid JKSV save files found in folder.")
                sys.exit(1)
        else:
            print(f"Error: Invalid JKSV source: {source_path}")
            sys.exit(1)

    elif input_format == "eden":
        if source.is_file():
            save = EdenReader.read_from_single_zip(source)
            if save:
                saves.append(save)
            else:
                print(f"Error: Invalid Eden save file: {source_path}")
                sys.exit(1)
        elif source.is_dir():
            saves = EdenReader.read_from_folder(source)
            if not saves:
                print(
                    "Error: No valid Eden save files found in folder."
                )
                sys.exit(1)
        else:
            print(f"Error: Invalid Eden source: {source_path}")
            sys.exit(1)

    # Convert each save
    for save in saves:
        print(f"Converting: {save.game_title}")

        if input_format == "jksv" and output_format == "eden":
            # For JKSV -> Eden, title ID resolution is always required
            # because folder names are arbitrary and not reliable
            output_path = convert_jksv_to_eden(
                save,
                username,
                output_dir,
                title_id_override,
                mapper,
                game_name_lookup=game_name_lookup,
                required=True,
            )
        elif input_format == "eden" and output_format == "jksv":
            output_path = convert_eden_to_jksv(save, username, output_dir)
        else:
            print(
                f"Error: Unsupported conversion: "
                f"{input_format} -> {output_format}"
            )
            sys.exit(1)

        print(f"  Output: {output_path}")
        save.cleanup()

    print("Conversion complete.")


if __name__ == "__main__":
    args = parse_args()
    run(
        args.input_format,
        args.output_format,
        args.source,
        args.username,
        args.title_id,
        args.game_name,
        args.strict,
    )
