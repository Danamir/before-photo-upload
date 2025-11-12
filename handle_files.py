"""
Image file handler with EXIF data parsing and intelligent renaming.

Renames images based on capture datetime from:
  1. EXIF data (if available)
  2. Filename parsing (if datetime found in name)
  3. File creation time (fallback)

Handles duplicate filenames by appending a counter suffix.

Usage:
  handle_files.py [--date-format <format>] [--dry-run] [-v] [-h] DIRECTORY
  handle_files.py -h | --help

Arguments:
  DIRECTORY             Path to directory containing images

Options:
  --date-format <format>  Date format for renaming [default: %Y%m%d_%H%M%S]
  -d --dry-run            Show what would be renamed without making changes
  -v --verbose            Display verbose output including skipped files
  -h --help               Show this help message and exit

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
    
    def __init__(self, date_format='%Y%m%d_%H%M%S', verbose=False):
        """
        Initialize handler.
        
        Args:
            date_format: Format string for output datetime
            verbose: If True, display verbose output including skipped files
        """
        self.date_format = date_format
        self.verbose = verbose
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
    
    def process_directory(self, directory, dry_run=False):
        """
        Process all images in directory and plan/perform renames.
        
        Args:
            directory: Path to directory
            dry_run: If True, only show what would be renamed
            
        Returns:
            List of (old_name, new_name, status) tuples
        """
        if not os.path.isdir(directory):
            print(f"Error: Directory '{directory}' not found.")
            return []
        
        results = []
        files_in_dir = os.listdir(directory)
        image_files = [f for f in files_in_dir if f.lower().endswith(self.IMAGE_EXTENSIONS)]
        
        if not image_files:
            print(f"No image files found in {directory}")
            return results
        
        print(f"Found {len(image_files)} image(s) to process.\n")
        
        for filename in sorted(image_files):
            filepath = os.path.join(directory, filename)
            
            try:
                new_filename = self.generate_new_filename(filepath, filename)
                
                if new_filename == filename:
                    status = "NO_CHANGE"
                    if self.verbose:
                        print(f"SKIP: {filename}")
                else:
                    new_filepath = os.path.join(directory, new_filename)
                    
                    if not dry_run:
                        # Check if target already exists (shouldn't happen with our logic, but safety check)
                        if os.path.exists(new_filepath) and new_filepath != filepath:
                            status = "ERROR_EXISTS"
                            print(f"ERROR: {filename} -> {new_filename} (target already exists)")
                        else:
                            os.rename(filepath, new_filepath)
                            status = "RENAMED"
                            print(f"RENAME: {filename}")
                            print(f"     -> {new_filename}")
                    else:
                        status = "DRY_RUN"
                        print(f"[DRY-RUN] {filename}")
                        print(f"       -> {new_filename}")
                
                results.append((filename, new_filename, status))
            
            except Exception as e:
                status = "ERROR"
                print(f"ERROR: {filename} - {str(e)}")
                results.append((filename, filename, status))
        
        return results
    
    def print_summary(self, results):
        """
        Print summary of operations.
        
        Args:
            results: List of (old_name, new_name, status) tuples
        """
        status_counts = defaultdict(int)
        for _, _, status in results:
            status_counts[status] += 1
        
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        
        for status in ["RENAMED", "DRY_RUN", "NO_CHANGE", "ERROR", "ERROR_EXISTS"]:
            if status in status_counts:
                print(f"{status}: {status_counts[status]}")
        
        print("=" * 60)


def main():
    """Main entry point."""
    args = docopt(__doc__)
    
    directory = args['DIRECTORY']
    date_format = args['--date-format']
    dry_run = args['--dry-run']
    verbose = args['--verbose']
    
    handler = ImageFileHandler(date_format=date_format, verbose=verbose)
    
    if dry_run:
        print(f"DRY-RUN MODE: No files will be modified.\n")
    
    results = handler.process_directory(directory, dry_run=dry_run)
    handler.print_summary(results)


if __name__ == "__main__":
    main()
