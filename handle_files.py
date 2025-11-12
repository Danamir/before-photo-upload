"""
Image file handler with EXIF data parsing, intelligent renaming, and optional conversion.

Renames images based on capture datetime from:
  1. EXIF data (if available)
  2. Filename parsing (if datetime found in name)
  3. File creation time (fallback)

Optionally converts and/or resizes images. Handles duplicate filenames by appending a counter suffix.

Usage:
  handle_files.py [options] [--date-format <format>] [-d] [-v] [-h] DIRECTORY
  handle_files.py -h | --help

Arguments:
  DIRECTORY                    Path to directory containing images

Options:
  --date-format <format>       Date format for renaming [default: %Y%m%d_%H%M%S]
  --convert                    Enable file conversion [default: False]
  --convert-format <format>    Output image format (jpg, png, webp, etc.) [default: jpg]
  --out <folder>               Output folder, created if not existing [default: out]
  --quality <quality>          JPEG/WebP compression quality (1-100) [default: 85]
  --short-side <pixels>        Resize to this short-side dimension, keep aspect ratio
  --long-side <pixels>         Resize to this long-side dimension, keep aspect ratio
  -d --dry-run                 Show what would be renamed/converted without making changes
  -v --verbose                 Display verbose output including skipped files
  -h --help                    Show this help message and exit

Notes:
  - If --short-side or --long-side is set, conversion is automatically enabled
  - If converted file is same format as original but <10% smaller, original is copied instead
  - --short-side and --long-side are mutually exclusive

Date Format Examples:
  %Y%m%d_%H%M%S    20250112_143025
  %Y-%m-%d_%H-%M   2025-01-12_14-30
  %Y/%m/%d_%H%M%S  2025/01/12_143025
"""

import os
from datetime import datetime
from pathlib import Path
from docopt import docopt
from PIL import Image
from PIL.ExifTags import TAGS
from pillow_heif import register_heif_opener
import re
from collections import defaultdict

register_heif_opener()


class ImageFileHandler:
    """
    Handles image file renaming based on datetime from various sources.
    Also supports optional image conversion and resizing.
    """
    
    # Common datetime patterns in filenames
    DATETIME_PATTERNS = [
        # YYYY-MM-DD HH:MM:SS
        r'(\d{4})[_\-\.](\d{2})[_\-\.](\d{2})[_\-\s](\d{2}):(\d{2}):(\d{2})',
        # YYYY-MM-DD_HH-MM-SS (with underscores/dashes)
        r'(\d{4})[_\-\.](\d{2})[_\-\.](\d{2})[_\-](\d{2})[_\-](\d{2})[_\-](\d{2})',
        # YYYY-MM-DD only
        r'(\d{4})[_\-\.](\d{2})[_\-\.](\d{2})',
        # YYYYMMDD_HH-MM-SS or similar
        r'(\d{4})(\d{2})(\d{2})[_\-]?(\d{2})[_\-]?(\d{2})[_\-]?(\d{2})',
        # YYYYMMDD only
        r'(\d{4})(\d{2})(\d{2})',
        # DD-MM-YYYY HH:MM:SS
        r'(\d{2})[_\-\.](\d{2})[_\-\.](\d{4})[_\-\s](\d{2}):(\d{2}):(\d{2})',
    ]
    
    IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.heic', '.heif', '.webp', '.tiff')
    
    def __init__(self, date_format='%Y%m%d_%H%M%S', verbose=False, convert=False, 
                 convert_format='jpg', output_folder='out', quality=85, 
                 short_side=None, long_side=None):
        """
        Initialize handler.
        
        Args:
            date_format: Format string for output datetime
            verbose: If True, display verbose output including skipped files
            convert: If True, convert files
            convert_format: Output image format (jpg, png, webp, etc.)
            output_folder: Output folder for converted files
            quality: Compression quality for JPEG/WebP (1-100)
            short_side: Resize to this short-side dimension (enables conversion)
            long_side: Resize to this long-side dimension (enables conversion)
        """
        self.date_format = date_format
        self.verbose = verbose
        self.convert = convert or short_side is not None or long_side is not None
        self.convert_format = convert_format.lower()
        self.output_folder = output_folder
        self.quality = int(quality)
        self.short_side = int(short_side) if short_side else None
        self.long_side = int(long_side) if long_side else None
        self.rename_map = {}  # Maps old name to new name
        self.duplicates = defaultdict(int)  # Track duplicate new names
    
    def extract_exif_datetime(self, filepath):
        """
        Extract datetime from image EXIF data.
        
        Args:
            filepath: Path to image file
            
        Returns:
            datetime object or None
        """
        try:
            image = Image.open(filepath)
            exif_data = image._getexif()
            
            if exif_data is None:
                return None
            
            # DateTimeOriginal is the capture time (36867)
            # DateTime is the modification time (306)
            # SubSecTimeOriginal is subseconds (37521)
            
            datetime_str = None
            for tag_id, value in exif_data.items():
                tag_name = TAGS.get(tag_id, tag_id)
                
                if tag_name == 'DateTimeOriginal':
                    datetime_str = value
                    break
            
            if not datetime_str:
                # Fallback to DateTime
                for tag_id, value in exif_data.items():
                    tag_name = TAGS.get(tag_id, tag_id)
                    if tag_name == 'DateTime':
                        datetime_str = value
                        break
            
            if datetime_str:
                # EXIF datetime format is typically "YYYY:MM:DD HH:MM:SS"
                return datetime.strptime(datetime_str, '%Y:%m:%d %H:%M:%S')
        
        except Exception as e:
            pass
        
        return None
    
    def parse_datetime_from_filename(self, filename):
        """
        Try to extract datetime from filename.
        
        Args:
            filename: Filename (without path)
            
        Returns:
            datetime object or None
        """
        # Remove extension
        name_without_ext = os.path.splitext(filename)[0]
        
        for pattern in self.DATETIME_PATTERNS:
            match = re.search(pattern, name_without_ext)
            if match:
                groups = match.groups()
                try:
                    # Determine if this is YYYY-MM-DD or DD-MM-YYYY format
                    # If first group > 31, it's likely YYYY
                    first_val = int(groups[0])
                    
                    if first_val > 31:  # YYYY format
                        if len(groups) == 3:
                            # YYYY MM DD - use 12:00:00 as default time
                            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                            return datetime(year, month, day, 12, 0, 0)
                        elif len(groups) == 6:
                            # YYYY MM DD HH MM SS
                            year, month, day, hour, minute, second = map(int, groups)
                            return datetime(year, month, day, hour, minute, second)
                    else:  # DD-MM-YYYY format
                        if len(groups) == 6:
                            # DD MM YYYY HH MM SS
                            day, month, year, hour, minute, second = map(int, groups)
                            return datetime(year, month, day, hour, minute, second)
                
                except (ValueError, IndexError):
                    continue
        
        return None
    
    def get_file_creation_datetime(self, filepath):
        """
        Get file creation/modification datetime.
        
        Args:
            filepath: Path to file
            
        Returns:
            datetime object
        """
        try:
            # On Windows, os.path.getctime() returns creation time
            # On Unix, it returns metadata change time, so we use mtime
            stat_info = os.stat(filepath)
            mtime = stat_info.st_mtime
            return datetime.fromtimestamp(mtime)
        except Exception as e:
            print(f"Error getting file time for {filepath}: {e}")
            return datetime.now()
    
    def get_datetime_for_image(self, filepath, filename):
        """
        Get datetime for image using priority:
        1. EXIF data
        2. Filename parsing
        3. File creation time
        
        Args:
            filepath: Full path to image
            filename: Filename only
            
        Returns:
            datetime object
        """
        # Try EXIF
        dt = self.extract_exif_datetime(filepath)
        if dt:
            return dt
        
        # Try filename
        dt = self.parse_datetime_from_filename(filename)
        if dt:
            return dt
        
        # Use file creation time
        return self.get_file_creation_datetime(filepath)
    
    def generate_new_filename(self, filepath, filename):
        """
        Generate new filename based on datetime.
        
        Args:
            filepath: Full path to image
            filename: Original filename
            
        Returns:
            New filename (with extension)
        """
        ext = os.path.splitext(filename)[1]
        
        # Use converted format if conversion is enabled
        if self.convert:
            ext = '.' + self.convert_format
        
        dt = self.get_datetime_for_image(filepath, filename)
        new_name = dt.strftime(self.date_format) + ext
        
        # Handle duplicates by appending counter
        if new_name in self.duplicates:
            self.duplicates[new_name] += 1
            base, ext = os.path.splitext(new_name)
            new_name = f"{base}_{self.duplicates[new_name]:03d}{ext}"
        else:
            self.duplicates[new_name] = 0
        
        return new_name
    
    def get_resized_dimensions(self, original_width, original_height):
        """
        Calculate resized dimensions based on short/long side constraints.
        
        Args:
            original_width: Original image width
            original_height: Original image height
            
        Returns:
            Tuple of (new_width, new_height) or (original_width, original_height) if no resize
        """
        if not self.short_side and not self.long_side:
            return original_width, original_height
        
        aspect_ratio = original_width / original_height
        
        if self.short_side:
            # Resize based on short side
            if original_width < original_height:
                # Width is short side
                new_width = self.short_side
                new_height = int(new_width / aspect_ratio)
            else:
                # Height is short side
                new_height = self.short_side
                new_width = int(new_height * aspect_ratio)
        else:  # self.long_side
            # Resize based on long side
            if original_width > original_height:
                # Width is long side
                new_width = self.long_side
                new_height = int(new_width / aspect_ratio)
            else:
                # Height is long side
                new_height = self.long_side
                new_width = int(new_height * aspect_ratio)
        
        return new_width, new_height
    
    def convert_image(self, filepath, output_path, original_size):
        """
        Convert and optionally resize an image.
        
        Args:
            filepath: Path to source image
            output_path: Path to output image
            original_size: Size of original file in bytes
            
        Returns:
            Tuple of (success: bool, new_size: int, format_changed: bool, copied: bool, original_dims: tuple, new_dims: tuple)
            copied: True if original was copied instead of converted
            original_dims: Tuple of (width, height) of original
            new_dims: Tuple of (width, height) of converted
        """
        try:
            image = Image.open(filepath)
            
            # Get original dimensions
            original_width, original_height = image.size
            original_dims = (original_width, original_height)
            
            # Calculate new dimensions
            new_width, new_height = self.get_resized_dimensions(original_width, original_height)
            new_dims = (new_width, new_height)
            
            # Resize if needed
            if new_width != original_width or new_height != original_height:
                image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            # Convert format if specified
            save_kwargs = {}
            
            # Normalize format name for PIL
            format_name = self.convert_format.upper()
            if format_name == 'JPG':
                format_name = 'JPEG'
            
            if self.convert_format in ('jpg', 'jpeg'):
                # Convert to RGB if necessary (JPEG doesn't support transparency)
                if image.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', image.size, (255, 255, 255))
                    background.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
                    image = background
                save_kwargs['quality'] = self.quality
            elif self.convert_format == 'webp':
                save_kwargs['quality'] = self.quality
            
            # Check if conversion is worthwhile
            original_format = image.format or os.path.splitext(filepath)[1][1:].lower()
            
            # Save to temporary location first to check size
            temp_path = output_path + '.tmp'
            image.save(temp_path, format=format_name, **save_kwargs)
            new_size = os.path.getsize(temp_path)
            
            # Check if we should copy original instead
            format_changed = original_format.lower() != self.convert_format.lower()
            if not format_changed and original_size > 0:
                size_reduction = (original_size - new_size) / original_size
                if size_reduction < 0.10:  # Less than 10% smaller
                    # Copy original instead
                    os.replace(filepath, output_path)
                    os.remove(temp_path)
                    return True, original_size, False, True, original_dims, original_dims
            
            # Move temp file to final location
            os.replace(temp_path, output_path)
            return True, new_size, format_changed, False, original_dims, new_dims
        
        except Exception as e:
            print(f"  Conversion error: {e}")
            return False, 0, False, False, (0, 0), (0, 0)
    
    def get_file_size_info(self, filepath_or_size):
        """
        Get file size information.
        
        Args:
            filepath_or_size: Path to file or size in bytes
            
        Returns:
            Tuple of (size_bytes, size_string)
        """
        try:
            # If it's a string, treat as filepath
            if isinstance(filepath_or_size, str):
                size_bytes = os.path.getsize(filepath_or_size)
            else:
                # Treat as integer size
                size_bytes = filepath_or_size
            
            if size_bytes < 1024:
                return size_bytes, f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                return size_bytes, f"{size_bytes / 1024:.1f} KB"
            else:
                return size_bytes, f"{size_bytes / (1024 * 1024):.1f} MB"
        except:
            return 0, "unknown"
    
    def process_directory(self, directory, dry_run=False):
        """
        Process all images in directory and plan/perform renames and conversions.
        
        Args:
            directory: Path to directory
            dry_run: If True, only show what would be renamed/converted
            
        Returns:
            List of (old_name, new_name, status, old_size, new_size) tuples
        """
        if not os.path.isdir(directory):
            print(f"Error: Directory '{directory}' not found.")
            return []
        
        # Create output folder if needed
        if self.convert:
            # If output_folder is relative, make it relative to the input directory
            if not os.path.isabs(self.output_folder):
                output_path = os.path.join(directory, self.output_folder)
            else:
                output_path = self.output_folder
            os.makedirs(output_path, exist_ok=True)
        else:
            output_path = directory
        
        results = []
        files_in_dir = os.listdir(directory)
        image_files = [f for f in files_in_dir if f.lower().endswith(self.IMAGE_EXTENSIONS)]
        
        if not image_files:
            print(f"No image files found in {directory}")
            return results
        
        print(f"Found {len(image_files)} image(s) to process.\n")
        
        for filename in sorted(image_files):
            filepath = os.path.join(directory, filename)
            original_size, original_size_str = self.get_file_size_info(filepath)
            
            try:
                new_filename = self.generate_new_filename(filepath, filename)
                
                if self.convert:
                    output_file_path = os.path.join(output_path, new_filename)
                else:
                    output_file_path = os.path.join(directory, new_filename)
                
                if new_filename == filename and not self.convert:
                    status = "NO_CHANGE"
                    if self.verbose:
                        print(f"SKIP: {filename}")
                    results.append((filename, new_filename, status, original_size, original_size))
                else:
                    if not dry_run:
                        # Check if target already exists
                        if os.path.exists(output_file_path) and output_file_path != filepath:
                            status = "ERROR_EXISTS"
                            print(f"ERROR: {filename} -> {new_filename} (target already exists)")
                            results.append((filename, new_filename, status, original_size, original_size))
                        else:
                            if self.convert:
                                success, new_size, format_changed, copied, orig_dims, new_dims = self.convert_image(filepath, output_file_path, original_size)
                                if success:
                                    new_size_str, _ = self.get_file_size_info(output_file_path)
                                    _, new_size_str = self.get_file_size_info(output_file_path)
                                    status = "CONVERTED" if format_changed or not copied else "COPIED"
                                    print(f"{status}: {filename}")
                                    print(f"     -> {new_filename}")
                                    print(f"        {original_size_str} -> {new_size_str}")
                                    if orig_dims != new_dims:
                                        print(f"        {orig_dims[0]}x{orig_dims[1]} -> {new_dims[0]}x{new_dims[1]}")
                                    
                                    results.append((filename, new_filename, status, original_size, new_size))
                                else:
                                    status = "ERROR"
                                    print(f"ERROR: Failed to convert {filename}")
                                    results.append((filename, new_filename, status, original_size, original_size))
                                    continue
                    else:
                        # Dry-run: simulate conversion to get new size
                        new_size = original_size
                        status = "DRY_RUN"
                        new_size_str = original_size_str
                        
                        if self.convert:
                            try:
                                image = Image.open(filepath)
                                original_width, original_height = image.size
                                new_width, new_height = self.get_resized_dimensions(original_width, original_height)
                                
                                if new_width != original_width or new_height != original_height:
                                    image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
                                
                                save_kwargs = {}
                                
                                # Normalize format name for PIL
                                format_name = self.convert_format.upper()
                                if format_name == 'JPG':
                                    format_name = 'JPEG'
                                
                                if self.convert_format in ('jpg', 'jpeg'):
                                    if image.mode in ('RGBA', 'LA', 'P'):
                                        background = Image.new('RGB', image.size, (255, 255, 255))
                                        background.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
                                        image = background
                                    save_kwargs['quality'] = self.quality
                                elif self.convert_format == 'webp':
                                    save_kwargs['quality'] = self.quality
                                
                                temp_path = filepath + '.dryrun_tmp'
                                image.save(temp_path, format=format_name, **save_kwargs)
                                new_size = os.path.getsize(temp_path)
                                _, new_size_str = self.get_file_size_info(temp_path)
                                os.remove(temp_path)
                                
                                # Display resolution info
                                if new_width != original_width or new_height != original_height:
                                    resolution_str = f"\n       {original_width}x{original_height} -> {new_width}x{new_height}"
                                else:
                                    resolution_str = ""
                            except:
                                resolution_str = ""
                                pass
                        
                        print(f"[DRY-RUN] {filename}")
                        print(f"       -> {new_filename}")
                        if self.convert:
                            print(f"       {original_size_str} -> {new_size_str} (.{self.convert_format}){resolution_str}")
                        
                        results.append((filename, new_filename, status, original_size, new_size))
            
            except Exception as e:
                status = "ERROR"
                print(f"ERROR: {filename} - {str(e)}")
                results.append((filename, filename, status, original_size, original_size))
        
        return results
    
    def print_summary(self, results):
        """
        Print summary of operations.
        
        Args:
            results: List of (old_name, new_name, status, old_size, new_size) tuples
        """
        status_counts = defaultdict(int)
        total_original_size = 0
        total_new_size = 0
        
        for _, _, status, old_size, new_size in results:
            status_counts[status] += 1
            if status in ("RENAMED", "CONVERTED", "COPIED", "DRY_RUN"):
                total_original_size += old_size
                total_new_size += new_size
        
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        
        for status in ["RENAMED", "CONVERTED", "COPIED", "DRY_RUN", "NO_CHANGE", "ERROR", "ERROR_EXISTS"]:
            if status in status_counts:
                print(f"{status}: {status_counts[status]}")
        
        # Display size gain if conversion was performed
        if total_original_size > 0 and total_new_size > total_original_size:
            size_saved = total_original_size - total_new_size
            size_saved_pct = (size_saved / total_original_size) * 100
            _, original_str = self.get_file_size_info(total_original_size)
            _, new_str = self.get_file_size_info(total_new_size)
            _, saved_str = self.get_file_size_info(size_saved)
            print(f"\nSize reduction: {original_str} -> {new_str} (saved {saved_str}, {size_saved_pct:.1f}%)")
        elif total_original_size > 0 and total_new_size < total_original_size:
            size_saved = total_original_size - total_new_size
            size_saved_pct = (size_saved / total_original_size) * 100
            _, original_str = self.get_file_size_info(total_original_size)
            _, new_str = self.get_file_size_info(total_new_size)
            _, saved_str = self.get_file_size_info(size_saved)
            print(f"\nSize reduction: {original_str} -> {new_str} (saved {saved_str}, {size_saved_pct:.1f}%)")
        
        print("=" * 60)


def main():
    """Main entry point."""
    args = docopt(__doc__)
    
    directory = args['DIRECTORY']
    date_format = args['--date-format']
    dry_run = args['--dry-run']
    verbose = args['--verbose']
    convert = args['--convert']
    convert_format = args['--convert-format']
    output_folder = args['--out']
    quality = args['--quality']
    short_side = args['--short-side']
    long_side = args['--long-side']
    
    # Validate exclusive options
    if short_side and long_side:
        print("Error: --short-side and --long-side are mutually exclusive")
        return
    
    handler = ImageFileHandler(
        date_format=date_format,
        verbose=verbose,
        convert=convert,
        convert_format=convert_format,
        output_folder=output_folder,
        quality=quality,
        short_side=short_side,
        long_side=long_side
    )
    
    if dry_run:
        print(f"DRY-RUN MODE: No files will be modified.\n")
    
    results = handler.process_directory(directory, dry_run=dry_run)
    handler.print_summary(results)


if __name__ == "__main__":
    main()
