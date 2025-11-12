# Photo Upload Tools

A Python toolkit for managing and organizing image files, featuring intelligent duplicate detection and automated file renaming based on capture datetime.

## Features

### 1. **Duplicate Detection** (`find_duplicates.py`)
- **Perceptual hashing** using pHash to find visually similar images
- **BK-tree** (Burkhard-Keller tree) for efficient nearest neighbor search
- Detects duplicates even if images have been resized, recompressed, or slightly modified
- Configurable similarity threshold (0-64+ Hamming distance)
- Persistent index with compression for fast subsequent searches
- Support for multiple formats: PNG, JPG, JPEG, GIF, BMP, HEIC, HEIF, WebP, TIFF

### 2. **Intelligent File Handling** (`handle_files.py`)
- Extracts datetime from **EXIF metadata** (capture time)
- Falls back to **filename parsing** if EXIF unavailable
- Uses **file creation time** as last resort
- Customizable date format for output filenames
- Automatic filename duplicate handling with counter suffixes
- Dry-run mode to preview changes before applying
- Resize on short-side or long-side dimensions
- Convert to selected format, only when necessary
- Copy EXIF data to converted files
- Support for multiple formats: PNG, JPG, JPEG, GIF, BMP, HEIC, HEIF, WebP, TIFF

## Installation

### Prerequisites
- Python 3.7+
- pip package manager

### Setup

```bash
pip install -r requirements.txt
```

**Dependencies:**
- `imagehash` - Perceptual image hashing
- `pillow` - Python Imaging Library
- `pillow-heif` - HEIC/HEIF image support
- `docopt` - Command-line interface creation

## Usage

### Finding Duplicate Images

#### Basic usage (find all duplicate groups)
```bash
python find_duplicates.py path/to/images
```

#### Find duplicates of a specific image
```bash
python find_duplicates.py path/to/images path/to/image.jpg
```

#### Options
```
Arguments:
  DIRECTORY             Path to directory containing images
  IMAGE                 Optional: Path to specific image to find duplicates for

Options:
  -t --threshold <threshold>  Maximum Hamming distance [default: 5]
  -h --help               Show this help message and exit
```

#### Threshold Guide
- **0**: Exact match only
- **1-5**: Very similar (resized, slight compression)
- **6-10**: Similar content, moderate changes
- **11-15**: Recognizably similar
- **16+**: Increasingly different

### Handle Input Images

**Note:** Renaming is now optional and controlled by the `--rename` flag. Without this flag, images will keep their original filenames (useful when only converting formats or resizing).

#### Options
```
Arguments:
  DIRECTORY                    Path to directory containing images

Options:
  --date-format <format>       Date format for renaming [default: %Y%m%d_%H%M%S]
  --rename                     Enable file renaming based on date info [default: False]
  --convert                    Enable file conversion [default: False]
  --convert-format <format>    Output image format (jpg, png, webp, etc.) [default: jpg]
  --out <folder>               Output folder, created if not existing [default: out]
  --quality <quality>          JPEG/WebP compression quality (1-100) [default: 85]
  --short-side <pixels>        Resize to this short-side dimension, keep aspect ratio
  --long-side <pixels>         Resize to this long-side dimension, keep aspect ratio
  --pool-size <size>           Number of parallel workers for processing [default: 5]
  -d --dry-run                 Show what would be renamed/converted without making changes
  -v --verbose                 Display verbose output including skipped files
  -h --help                    Show this help message and exit
```

#### Date Format Examples
- `%Y%m%d_%H%M%S` → `20250112_143025`
- `%Y-%m-%d_%H-%M` → `2025-01-12_14-30`
- `%Y/%m/%d_%H%M%S` → `2025/01/12_143025`

See [Python strftime documentation](https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes) for more format codes.

## How It Works

### Duplicate Detection Algorithm

The `find_duplicates.py` tool uses a two-stage approach:

1. **Perceptual Hash Generation**
   - Converts each image to grayscale
   - Resizes to 8×8 pixels
   - Computes discrete cosine transform (DCT)
   - Generates a 64-bit hash representing the image content

2. **BK-Tree Search**
   - Organizes hashes in a metric tree structure
   - Uses the triangle inequality to prune search space
   - Finds all hashes within a specified Hamming distance threshold
   - Much faster than brute-force comparison for large collections

3. **Persistent Index**
   - Caches hashes and file metadata in a compressed `.image_index.zip` file
   - Tracks file modification times to update only changed/new images
   - Dramatically speeds up repeated searches

### File Renaming Strategy

The `handle_files.py` tool uses this priority order to find capture datetime:

1. **EXIF DateTimeOriginal** - The original capture time from camera metadata
2. **EXIF DateTime** - Fallback EXIF datetime
3. **Filename Parsing** - Extracts datetime if present in filename (supports multiple patterns)
4. **File Modification Time** - Last resort, uses OS file timestamp

Duplicate filenames are handled by appending a counter suffix (e.g., `photo_001.jpg`, `photo_002.jpg`).

## Examples

### Example 1: Find all duplicates in a photo collection
```bash
python find_duplicates.py C:\Photos\Vacation
```

Output:
```
Building/updating index...
Processed 247 new/updated images
BK-tree size: 203 unique hashes

Finding duplicates...

Found 3 groups of duplicates:

Group 1 (2 images):
  - vacation_01.jpg (distance: 0)
  - vacation_02.jpg (distance: 3)

Group 2 (4 images):
  - IMG_001.jpg (distance: 0)
  - IMG_001_edited.jpg (distance: 2)
  - copy_IMG_001.jpg (distance: 1)
  - IMG_001_backup.jpg (distance: 4)

Group 3 (2 images):
  - beach_sunset.jpg (distance: 0)
  - beach_sunset_resized.jpg (distance: 5)
```

### Example 2: Preview renaming with custom format
```bash
python handle_files.py --dry-run --date-format "%Y-%m-%d %H-%M-%S" C:\Photos\ToSort
```

Output:
```
Found 12 image(s) to process.

[DRY-RUN] photo1.jpg
       -> 2025-01-12 14-30-25.jpg
[DRY-RUN] IMG_12345.jpg
       -> 2025-01-15 09-45-10.jpg
[DRY-RUN] vacation.png
       -> 2025-01-10 16-22-33.png

============================================================
SUMMARY
============================================================
DRY_RUN: 12
============================================================
```

### Example 3: Apply the renaming
```bash
python handle_files.py C:\Photos\ToSort
```

## Performance Notes

- **Duplicate Detection**: 
  - First run: Slow (processes all images)
  - Subsequent runs: Fast (only processes new/modified images)
  - Index is cached in `.image_index.zip` in the target directory

- **BK-Tree Efficiency**: 
  - Searching through 10,000+ images is nearly as fast as searching through 100
  - Memory efficient due to lazy evaluation of distances

- **File Renaming**:
  - Linear time complexity (O(n) where n = number of images)
  - EXIF parsing is the slowest step; images without EXIF are processed faster

## Supported Image Formats

Both tools support:
- PNG (`.png`)
- JPEG (`.jpg`, `.jpeg`)
- GIF (`.gif`)
- Bitmap (`.bmp`)
- HEIC/HEIF (`.heic`, `.heif`) - Apple formats
- WebP (`.webp`) - Web format
- TIFF (`.tiff`) - Tag Image File Format

## License

This project is licensed under the **GNU General Public License v3.0** (GPL-3.0). 

See the [LICENCE](LICENCE) file for full license details.

## Contributing

Feel free to extend these tools with:
- Additional hash functions (aHash, dHash, wHash)
- Database backend for larger collections
- Web UI for batch operations
- Integration with photo organizing services

---

**Created**: November 2025
**Last Updated**: November 12, 2025
