# Eden2JKSV

Convert Nintendo Switch save data between **JKSV** and **Eden** formats.

## Features

- Convert between JKSV and Eden save formats
- Automatic Title ID resolution via SwitchBrew database
- Supports both folder and zip file input
- 24-hour local caching to minimize network requests

## Requirements

- Python 3.8+
- No external dependencies

## Usage

```bash
python convert.py --input <format> --output <format> --source <path> [options]
```

### Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--input` | Yes | Source format: `jksv` or `eden` |
| `--output` | Yes | Target format: `jksv` or `eden` |
| `--source` | Yes | Path to source save data (folder or zip file) |
| `--username` | No | Username for output (default: `User`) |
| `--title-id` | No | Title ID override (16 hex chars, no `0x` prefix) |
| `--game-name` | No | Game name for Title ID lookup |
| `--strict` | No | Fail instead of using placeholder when Title ID is not found |
| `--refresh-db` | No | Force refresh the Title ID database from SwitchBrew |

### Examples

Convert a JKSV folder to Eden format:

```bash
python convert.py --input jksv --output eden --source ./saves \
    --game-name "The Legend of Zelda: Breath of the Wild"
```

Convert using explicit Title ID:

```bash
python convert.py --input jksv --output eden --source ./backup.zip \
    --username MyPlayer --title-id 01008F6D12349000
```

Convert an Eden zip to JKSV format:

```bash
python convert.py --input eden --output jksv --source ./eden_saves --username MyPlayer
```

Force refresh the Title ID database:

```bash
python convert.py --input jksv --output eden --source ./saves \
    --game-name "Super Mario Odyssey" --refresh-db
```

## Output Structure

Converted files follow the naming convention:

```
[<Format>] <Game Title> - <Date>.zip
```

Examples:
- `[Eden] Zelda BotW - 2024-01-15_10-30-00.zip`
- `[JKSV] Super Mario Odyssey - 2024-06-20 14_00.zip`

Output files are saved to `output/<format>/` in the project directory.

## Title ID Resolution (JKSV → Eden)

When converting from JKSV to Eden, a Title ID is required. The resolution chain is:

1. `--title-id` command-line argument (always takes priority)
2. Binary scan of save files for embedded Title ID
3. `--game-name` command-line argument (SwitchBrew database lookup)
4. Fail with error message

Note: Folder names are not used for Title ID lookup in JKSV format, as they are arbitrary and unreliable.

## Title ID Database

The script fetches the game title list from [SwitchBrew](https://switchbrew.org/w/index.php?title=Title_list/Games&action=raw) and caches it locally.

- Cache location: `cache/title_id_cache.json`
- Cache TTL: 24 hours
- Force refresh: `--refresh-db`

## File Structure

```
Eden2JKSV/
├── convert.py          # Main conversion script
├── cache/
│   └── title_id_cache.json  # Local Title ID cache
└── output/              # Generated conversion output
    ├── eden/
    └── jksv/
```
