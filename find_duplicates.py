"""
Image duplicate detection using perceptual hashing and BK-tree
for efficient nearest neighbor search.

Usage:
  find_duplicates.py [-t <threshold>] [--pool-size <size>] [-h] DIRECTORY
  find_duplicates.py [-t <threshold>] [--pool-size <size>] [-h] DIRECTORY IMAGE
  find_duplicates.py -h | --help

Arguments:
  DIRECTORY             Path to directory containing images
  IMAGE                 Optional: Path to specific image to find duplicates for

Options:
  -t --threshold <threshold>  Maximum Hamming distance [default: 5]
  --pool-size <size>      Number of parallel workers for hashing [default: 5]
  -h --help               Show this help message and exit

Threshold Guide:
  0:        Exact match only
  1-5:      Very similar (resized, slight compression)
  6-10:     Similar content, moderate changes
  11-15:    Recognizably similar
  16+:      Increasingly different
"""

import imagehash
from PIL import Image
from pillow_heif import register_heif_opener
import os
from collections import defaultdict
from docopt import docopt
import pickle
import time
import zipfile
import io
from multiprocessing import Pool

register_heif_opener()


def process_image_worker(filepath, hash_func_name='phash'):
    """
    Worker function for parallel image processing.

    Args:
        filepath: Path to image file
        hash_func_name: Name of hash function to use

    Returns:
        Tuple of (filepath, hash_hex, mtime, success)
    """
    try:
        mtime = os.path.getmtime(filepath)

        # Get hash function by name
        if hash_func_name == 'phash':
            hash_func = imagehash.phash
        elif hash_func_name == 'ahash':
            hash_func = imagehash.average_hash
        elif hash_func_name == 'dhash':
            hash_func = imagehash.dhash
        elif hash_func_name == 'whash':
            hash_func = imagehash.whash
        else:
            hash_func = imagehash.phash  # Default fallback

        with Image.open(filepath) as img:
            img_hash = hash_func(img)

        # Serialize same way as save_index does
        hash_hex = img_hash.hash.tobytes().hex()

        return (filepath, hash_hex, mtime, True)
    except Exception as e:
        return (filepath, None, None, False)


class BKTree:
    """
    BK-tree (Burkhard-Keller tree) for efficient similarity search.
    Works with any discrete metric space (like Hamming distance).
    """
    
    def __init__(self, distance_func):
        """
        Args:
            distance_func: Function that takes two items and returns distance
        """
        self.distance_func = distance_func
        self.root = None
        self.size = 0
    
    def add(self, item):
        """Add an item to the tree"""
        if self.root is None:
            self.root = (item, {})
            self.size = 1
            return
        
        current = self.root
        while True:
            parent_item, children = current
            distance = self.distance_func(item, parent_item)
            
            if distance == 0:
                # Exact duplicate already in tree
                return
            
            if distance in children:
                current = children[distance]
            else:
                children[distance] = (item, {})
                self.size += 1
                return
    
    def search(self, item, threshold):
        """
        Find all items within threshold distance of the query item.
        
        Args:
            item: Query item
            threshold: Maximum distance for matches
            
        Returns:
            List of (item, distance) tuples
        """
        if self.root is None:
            return []
        
        results = []
        candidates = [self.root]
        
        while candidates:
            current_item, children = candidates.pop()
            distance = self.distance_func(item, current_item)
            
            if distance <= threshold:
                results.append((current_item, distance))
            
            # BK-tree property: only explore branches within threshold range
            for d in range(distance - threshold, distance + threshold + 1):
                if d in children:
                    candidates.append(children[d])
        
        return results


class ImageHashIndex:
    """
    Index for fast image duplicate detection using pHash and BK-tree.
    """
    
    def __init__(self, hash_func=None, index_file=None, pool_size=5):
        """
        Args:
            hash_func: Hash function (default: imagehash.phash)
            index_file: Path to save/load index (optional)
            pool_size: Number of parallel workers for image processing
        """
        self.hash_func = hash_func or imagehash.phash
        self.bktree = BKTree(distance_func=lambda h1, h2: h1 - h2)
        self.hash_to_files = defaultdict(list)
        self.file_mtimes = {}  # Track file modification times
        self.index_file = index_file
        self.pool_size = int(pool_size)

        # Map hash function to string name for multiprocessing
        self.hash_func_name = 'phash'  # default
        if hash_func == imagehash.average_hash:
            self.hash_func_name = 'ahash'
        elif hash_func == imagehash.dhash:
            self.hash_func_name = 'dhash'
        elif hash_func == imagehash.whash:
            self.hash_func_name = 'whash'
    
    def _find_existing_hash(self, img_hash):
        """
        Find an existing hash object in hash_to_files that equals img_hash.
        This is needed because ImageHash objects with same value are different objects.

        Args:
            img_hash: ImageHash object to search for

        Returns:
            Existing ImageHash object if found, otherwise img_hash
        """
        for existing_hash in self.hash_to_files.keys():
            if existing_hash == img_hash:
                return existing_hash
        return img_hash

    def add_image(self, filepath):
        """
        Add an image to the index.
        
        Args:
            filepath: Path to image file
            
        Returns:
            True if added/updated, False if skipped
        """
        try:
            mtime = os.path.getmtime(filepath)
            
            # Skip if already indexed and file hasn't changed
            if filepath in self.file_mtimes and self.file_mtimes[filepath] == mtime:
                return False
            
            with Image.open(filepath) as img:
                temp_hash = self.hash_func(img)

            # Serialize and deserialize to ensure consistent hash format
            # This matches what happens in parallel processing and save/load
            import numpy as np
            hash_hex = temp_hash.hash.tobytes().hex()
            hash_bytes = bytes.fromhex(hash_hex)
            hash_array = np.frombuffer(hash_bytes, dtype=np.uint8).reshape(temp_hash.hash.shape)
            img_hash = imagehash.ImageHash(hash_array)
            
            # Remove old entry if file was modified
            if filepath in self.file_mtimes:
                for old_hash in list(self.hash_to_files.keys()):
                    if filepath in self.hash_to_files[old_hash]:
                        self.hash_to_files[old_hash].remove(filepath)
                        if not self.hash_to_files[old_hash]:
                            del self.hash_to_files[old_hash]
            
            # Add to BK-tree (may skip if hash already exists, which is fine)
            self.bktree.add(img_hash)

            # Find existing hash object to use as key (important for hash equality)
            hash_key = self._find_existing_hash(img_hash)

            # Always map hash to file (even if hash already exists in tree)
            # Multiple files can have the same hash (crops, resizes, etc.)
            if filepath not in self.hash_to_files[hash_key]:
                self.hash_to_files[hash_key].append(filepath)
            self.file_mtimes[filepath] = mtime
            
            return True
        except Exception as e:
            print(f"Error processing {filepath}: {e}")
            return False
    
    def add_directory(self, directory, extensions=('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.heic', '.heif', '.webp', '.tiff')):
        """
        Add all images from a directory using parallel processing.

        Args:
            directory: Directory path
            extensions: Tuple of valid file extensions

        Returns:
            Number of images added/updated
        """
        count = 0

        # Use parallel processing if pool_size > 1
        if self.pool_size > 1:
            # Get all image files that need processing
            files_to_process = []
            for filename in os.listdir(directory):
                if filename.lower().endswith(extensions):
                    filepath = os.path.join(directory, filename)
                    try:
                        mtime = os.path.getmtime(filepath)
                        # Only process if file is new or modified
                        if filepath not in self.file_mtimes or self.file_mtimes[filepath] != mtime:
                            files_to_process.append(filepath)
                    except OSError:
                        continue

            if files_to_process:
                print(f"Processing {len(files_to_process)} new/updated images with {self.pool_size} workers...")

                # Use parallel processing
                with Pool(self.pool_size) as pool:
                    # Create arguments for starmap: (filepath, hash_func_name)
                    args = [(filepath, self.hash_func_name) for filepath in files_to_process]

                    # Process images in parallel
                    results = pool.starmap(process_image_worker, args)

                    # Process results sequentially (BK-tree is not thread-safe)
                    import numpy as np
                    for filepath, hash_hex, mtime, success in results:
                        if success:
                            # Reconstruct hash object same way as load_index does
                            hash_bytes = bytes.fromhex(hash_hex)
                            # phash produces 8x8 array of uint8
                            hash_array = np.frombuffer(hash_bytes, dtype=np.uint8).reshape((8, 8))
                            img_hash = imagehash.ImageHash(hash_array)

                            # Remove old entry if file was modified
                            if filepath in self.file_mtimes:
                                for old_hash in list(self.hash_to_files.keys()):
                                    if filepath in self.hash_to_files[old_hash]:
                                        self.hash_to_files[old_hash].remove(filepath)
                                        if not self.hash_to_files[old_hash]:
                                            del self.hash_to_files[old_hash]

                            # Add to BK-tree (may skip if hash already exists, which is fine)
                            self.bktree.add(img_hash)

                            # Find existing hash object to use as key (important for hash equality)
                            hash_key = self._find_existing_hash(img_hash)

                            # Always map hash to file (even if hash already exists in tree)
                            # Multiple files can have the same hash (crops, resizes, etc.)
                            if filepath not in self.hash_to_files[hash_key]:
                                self.hash_to_files[hash_key].append(filepath)
                            self.file_mtimes[filepath] = mtime
                            count += 1

                            if count % 100 == 0:
                                print(f"Processed {count} new/updated images...")
                        else:
                            print(f"Error processing {filepath}")
        else:
            # Use sequential processing (original code)
            for filename in os.listdir(directory):
                if filename.lower().endswith(extensions):
                    filepath = os.path.join(directory, filename)
                    if self.add_image(filepath):
                        count += 1
                        if count % 100 == 0:
                            print(f"Processed {count} new/updated images...")
        
        # Remove deleted files from index
        deleted_count = self._remove_deleted_files()
        if deleted_count > 0:
            print(f"Removed {deleted_count} deleted files from index")
        
        return count
    
    def _remove_deleted_files(self):
        """Remove files from index that no longer exist on disk"""
        deleted_count = 0
        deleted_files = []
        
        for filepath in list(self.file_mtimes.keys()):
            if not os.path.exists(filepath):
                deleted_files.append(filepath)
                deleted_count += 1
        
        for filepath in deleted_files:
            del self.file_mtimes[filepath]
            # Remove from hash_to_files
            for img_hash in list(self.hash_to_files.keys()):
                if filepath in self.hash_to_files[img_hash]:
                    self.hash_to_files[img_hash].remove(filepath)
                    if not self.hash_to_files[img_hash]:
                        del self.hash_to_files[img_hash]
        
        # Rebuild BK-tree if files were deleted
        if deleted_count > 0:
            self.bktree = BKTree(distance_func=lambda h1, h2: h1 - h2)
            for img_hash in self.hash_to_files.keys():
                self.bktree.add(img_hash)
        
        return deleted_count
    
    def find_duplicates(self, filepath, threshold=5):
        """
        Find all images similar to the given image.
        
        Args:
            filepath: Path to query image
            threshold: Maximum Hamming distance (0-64, lower = more strict)
            
        Returns:
            List of (filepath, distance) tuples
        """
        try:
            with Image.open(filepath) as img:
                query_hash = self.hash_func(img)
            
            # Search BK-tree
            similar_hashes = self.bktree.search(query_hash, threshold)
            
            # Convert hashes to file paths
            results = []
            query_basename = os.path.basename(filepath)
            for img_hash, distance in similar_hashes:
                for file in self.hash_to_files[img_hash]:
                    if os.path.basename(file) != query_basename:  # Exclude the query image itself
                        results.append((file, distance))
            
            return sorted(results, key=lambda x: x[1])
        except Exception as e:
            print(f"Error searching for {filepath}: {e}")
            return []
    
    def find_all_duplicate_groups(self, threshold=5):
        """
        Find all groups of duplicate images in the index.
        
        Args:
            threshold: Maximum Hamming distance
            
        Returns:
            List of groups, where each group is a list of (filepath, hash) tuples
        """
        processed_hashes = set()
        groups = []
        
        for img_hash in self.hash_to_files.keys():
            if img_hash in processed_hashes:
                continue
            
            # Find all similar hashes
            similar_hashes = self.bktree.search(img_hash, threshold)
            
            # Create a group if:
            # 1. Multiple hashes are similar (len(similar_hashes) > 1), OR
            # 2. Single hash maps to multiple files (exact duplicates with same hash)
            total_files = sum(len(self.hash_to_files[h]) for h, _ in similar_hashes)

            if len(similar_hashes) > 1 or total_files > 1:
                group = []
                for similar_hash, distance in similar_hashes:
                    processed_hashes.add(similar_hash)
                    for filepath in self.hash_to_files[similar_hash]:
                        group.append((filepath, similar_hash, distance))
                
                groups.append(group)
        
        return groups
    
    def save_index(self):
        """Save index to file (compressed with zip)"""
        if not self.index_file:
            return False
        
        try:
            # Convert hashes to hex strings for pickling
            hash_to_files_serializable = {
                h.hash.tobytes().hex(): files for h, files in self.hash_to_files.items()
            }
            
            data = {
                'hash_to_files': hash_to_files_serializable,
                'file_mtimes': self.file_mtimes
            }
            
            # Pickle data and compress with zip
            pickle_data = pickle.dumps(data)
            
            with zipfile.ZipFile(self.index_file, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('index.pkl', pickle_data)
            
            print(f"Index saved to {self.index_file}")
            return True
        except Exception as e:
            print(f"Error saving index: {e}")
            return False
    
    def load_index(self):
        """Load index from file (decompressed from zip)"""
        if not self.index_file or not os.path.exists(self.index_file):
            return False
        
        try:
            # Decompress and unpickle
            with zipfile.ZipFile(self.index_file, 'r') as zf:
                pickle_data = zf.read('index.pkl')
            
            data = pickle.loads(pickle_data)
            
            # Restore file mtimes
            self.file_mtimes = data['file_mtimes']
            
            # Rebuild BK-tree and hash_to_files from stored data
            hash_to_files_serializable = data['hash_to_files']
            self.hash_to_files = defaultdict(list)
            
            for hex_str, files in hash_to_files_serializable.items():
                # Recreate hash object from hex bytes
                import numpy as np
                hash_bytes = bytes.fromhex(hex_str)
                # phash produces 8x8 array of uint8
                hash_array = np.frombuffer(hash_bytes, dtype=np.uint8).reshape((8, 8))
                img_hash = imagehash.ImageHash(hash_array)
                self.hash_to_files[img_hash] = files
                # Add to BK-tree
                self.bktree.add(img_hash)
            
            print(f"Index loaded from {os.path.basename(self.index_file)}")
            return True
        except ValueError as e:
            # Handle shape mismatch or other value errors - likely old format
            if "reshape" in str(e) or "shape" in str(e).lower():
                print(f"Index format incompatible (old version), will rebuild from scratch")
                # Clear file mtimes to force full rebuild
                self.file_mtimes = {}
                self.hash_to_files = defaultdict(list)
                # Remove old index file
                try:
                    os.remove(self.index_file)
                except:
                    pass
                return False
            else:
                print(f"Error loading index: {e}")
                return False
        except (zipfile.BadZipFile, pickle.UnpicklingError, EOFError, KeyError) as e:
            print(f"Index file corrupted, will rebuild: {e}")
            # Remove corrupted index file
            try:
                os.remove(self.index_file)
            except:
                pass
            return False
        except Exception as e:
            print(f"Error loading index: {e}")
            return False


# Example usage
if __name__ == "__main__":
    args = docopt(__doc__)
    
    directory = args['DIRECTORY']
    image = args['IMAGE']
    threshold = int(args['--threshold'])
    pool_size = int(args['--pool-size'])

    # Create index with persistence
    index_file = os.path.join(directory, '.image_index.zip')
    index = ImageHashIndex(index_file=index_file, pool_size=pool_size)
    
    # Load existing index if available
    index_loaded = index.load_index()
    
    if os.path.exists(directory):
        print("Building/updating index...")
        count = index.add_directory(directory)
        if count > 0 or (index_loaded and index.bktree.size == 0):
            print(f"Processed {count} new/updated images")
            print(f"BK-tree size: {index.bktree.size} unique hashes")

            # Save index
            index.save_index()
        elif index_loaded:
            print("Index is up to date")
            print(f"BK-tree size: {index.bktree.size} unique hashes")
        
        # Always run duplicate detection after building/loading index
        if image:
            # Search for duplicates of a specific image
            if os.path.exists(image):
                print(f"\n\nSearching for duplicates of {os.path.basename(image)}:")

                # Find duplicates within threshold
                duplicates = index.find_duplicates(image, threshold=threshold)
                if duplicates:
                    print(f"\nFound {len(duplicates)} duplicate(s) within threshold {threshold}:")
                    for filepath, distance in duplicates:
                        print(f"  - {os.path.basename(filepath)} (distance: {distance})")
                else:
                    print(f"\nNo duplicates found within threshold {threshold}.")

                # Find 10 closest non-duplicate images (exclude those already in duplicates)
                print(f"\n10 closest non-duplicate images:")
                all_similar = index.find_duplicates(image, threshold=64)  # Max possible distance for 8x8 hash
                # Filter out duplicates (distance > threshold)
                non_duplicates = [item for item in all_similar if item[1] > threshold]
                closest_10 = non_duplicates[:10]

                if closest_10:
                    for filepath, distance in closest_10:
                        print(f"  - {os.path.basename(filepath)} (distance: {distance})")
                else:
                    print("  No other similar images found.")
            else:
                print(f"Image file '{image}' not found.")
        else:
            # Find all duplicate groups
            print("\nFinding duplicates...")
            duplicate_groups = index.find_all_duplicate_groups(threshold=threshold)
            
            print(f"\nFound {len(duplicate_groups)} groups of duplicates:")
            for i, group in enumerate(duplicate_groups, 1):
                print(f"\nGroup {i} ({len(group)} images):")
                for filepath, img_hash, distance in group:
                    print(f"  - {os.path.basename(filepath)} (distance: {distance})")
    else:
        print(f"Directory '{directory}' not found.")
