import subprocess
import sys
import os
from flask import Flask, render_template, request, send_file, jsonify
from flask_socketio import SocketIO, emit
import mercantile
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import zipfile
import random
import shutil
import re
import time
import json
from shapely.geometry import Polygon, box
from shapely.ops import unary_union
import threading
from PIL import Image
from collections import deque
import logging
import hashlib

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Base directory for caching tiles, absolute path relative to script location
BASE_DIR = Path(__file__).parent.parent  # Root of map-tile-downloader
CACHE_DIR = BASE_DIR / 'tile-cache'
DOWNLOADS_DIR = BASE_DIR / 'downloads'
CACHE_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder='../templates')
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=60, ping_interval=25)

# Load map sources from config file
CONFIG_DIR = Path('config')
MAP_SOURCES_FILE = CONFIG_DIR / 'map_sources.json'
MAP_SOURCES = {}
if MAP_SOURCES_FILE.exists():
    with open(MAP_SOURCES_FILE, 'r') as f:
        MAP_SOURCES = json.load(f)
else:
    print("Warning: map_sources.json not found. No map sources available.")
    sys.exit(1)

# Performance tuning constants
MAX_WORKERS = 20  # Increased from 5 for better parallelism
CONNECTION_POOL_SIZE = 25  # HTTP connection pool
BATCH_SIZE = 100  # Process more tiles at once
RATE_LIMIT_DETECTION_THRESHOLD = 5  # Number of 429s before throttling
RATE_LIMIT_BACKOFF_SECONDS = 10  # Wait time when rate limited

# Known error image hashes (MD5) - tiles matching these will abort the download
ERROR_IMAGE_HASHES = {
    '8F4F0F59FF1D5E6A55FF1AF91D65FC2E',  # Known error image
    # Add more error image hashes here as they're discovered
}

# Session-specific state management
class DownloadSession:
    """Manages state for a single download session."""
    def __init__(self, session_id):
        self.session_id = session_id
        self.cancel_event = threading.Event()  # Start cleared (not cancelled)
        self.active = True  # Track if session is still active
        self.stats_lock = threading.Lock()
        self.stats = {
            'total': 0,
            'completed': 0,
            'skipped': 0,
            'failed': 0,
            'rate_limited': 0
        }
        self.rate_limit_counter = 0
        self.currently_throttled = False
        self.error_image_detected = False
        self.error_message = None

    def is_cancelled(self):
        """Check if download has been cancelled."""
        return self.cancel_event.is_set()  # Set = cancelled (standard semantics)

    def cancel(self, error_message=None):
        """Cancel this download session."""
        self.cancel_event.set()  # Set the event to signal cancellation
        if error_message:
            self.error_message = error_message
        logging.info(f"Session {self.session_id} cancelled")
    
    def update_stats(self, stat_type, count=1):
        with self.stats_lock:
            self.stats[stat_type] += count

# Global session storage
sessions = {}
sessions_lock = threading.Lock()

def get_session(session_id):
    """Get or create a download session."""
    with sessions_lock:
        if session_id not in sessions:
            sessions[session_id] = DownloadSession(session_id)
        return sessions[session_id]

def cleanup_session(session_id):
    """Mark session as inactive (but don't delete immediately)."""
    with sessions_lock:
        if session_id in sessions:
            sessions[session_id].active = False
            logging.info(f"Session {session_id} marked inactive")

def create_requests_session():
    """Create a requests session with connection pooling and retry logic."""
    session = requests.Session()
    
    # Configure retry strategy for transient failures
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    
    adapter = HTTPAdapter(
        pool_connections=CONNECTION_POOL_SIZE,
        pool_maxsize=CONNECTION_POOL_SIZE,
        max_retries=retry_strategy
    )
    
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({'User-Agent': 'MapTileDownloader/1.0'})
    
    return session

def sanitize_style_name(style_name):
    """Convert map style name to a filesystem-safe directory name."""
    style_name = re.sub(r'\s+', '-', style_name)
    style_name = re.sub(r'[^a-zA-Z0-9-_]', '', style_name)
    return style_name

def get_style_cache_dir(style_name, convert_to_8bit=False):
    """Get the cache directory path for a given map style name."""
    sanitized_name = sanitize_style_name(style_name)
    
    # Use subdirectories: 8bit/{style} or normal/{style}
    if convert_to_8bit:
        return CACHE_DIR / '8bit' / sanitized_name
    else:
        return CACHE_DIR / 'normal' / sanitized_name

def convert_image_to_8bit(tile_path):
    """Convert image to 8-bit palette mode if needed."""
    img = None
    try:
        img = Image.open(tile_path)
        if img.mode != 'P':
            img = img.quantize(colors=256)
        img.save(tile_path, optimize=True)
    except Exception as e:
        logging.error(f"Error converting tile to 8-bit: {e}")
    finally:
        if img:
            img.close()

def calculate_file_hash(file_path):
    """Calculate MD5 hash of a file."""
    md5_hash = hashlib.md5()
    with open(file_path, 'rb') as f:
        # Read file in chunks to handle large files efficiently
        for byte_block in iter(lambda: f.read(4096), b""):
            md5_hash.update(byte_block)
    return md5_hash.hexdigest().upper()

def check_for_error_image(file_path, session_obj):
    """Check if downloaded tile matches a known error image hash.
    Returns True if error image detected, False otherwise."""
    try:
        file_hash = calculate_file_hash(file_path)
        if file_hash in ERROR_IMAGE_HASHES:
            error_msg = f"Error image detected (hash: {file_hash}). Map provider returned an error image instead of valid tile data."
            logging.error(error_msg)
            session_obj.cancel(error_msg)
            session_obj.error_image_detected = True
            return True
    except Exception as e:
        logging.error(f"Error checking tile hash: {e}")
    return False

def download_tile(tile, map_style, style_cache_dir, convert_to_8bit, session_obj, http_session, normal_cache_dir=None):
    """Download a single tile with retry logic and rate limit handling."""
    if session_obj.is_cancelled():
        return {'status': 'cancelled', 'tile': tile}
    
    tile_dir = style_cache_dir / str(tile.z) / str(tile.x)
    tile_path = tile_dir / f"{tile.y}.png"
    
    # If converting to 8bit, also set up normal cache path
    if convert_to_8bit and normal_cache_dir:
        normal_tile_dir = normal_cache_dir / str(tile.z) / str(tile.x)
        normal_tile_path = normal_tile_dir / f"{tile.y}.png"
    else:
        normal_tile_path = None
    
    # Check cache first (check normal cache if converting to 8bit)
    # Use try-except instead of check-then-act to avoid TOCTOU race
    cache_check_path = normal_tile_path if convert_to_8bit and normal_tile_path else tile_path

    try:
        if cache_check_path.exists():
            # If we need 8bit version but don't have it yet, convert from normal
            if convert_to_8bit and cache_check_path == normal_tile_path:
                try:
                    tile_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cache_check_path, tile_path)
                    convert_image_to_8bit(tile_path)
                except FileNotFoundError:
                    logging.debug(f"Cache file disappeared during copy: {cache_check_path}")
                    # Fall through to download
                except Exception as e:
                    logging.warning(f"Cache conversion failed: {e}, will re-download")
                    # Fall through to download
                else:
                    session_obj.update_stats('skipped')
                    return {'status': 'skipped', 'tile': tile}
            else:
                # Direct cache hit
                session_obj.update_stats('skipped')
                return {'status': 'skipped', 'tile': tile}
    except Exception as e:
        logging.debug(f"Cache check failed: {e}, proceeding to download")
        # Fall through to download
    
    # Build URL
    subdomain = random.choice(['a', 'b', 'c']) if '{s}' in map_style else ''
    url = (map_style
           .replace('{s}', subdomain)
           .replace('{z}', str(tile.z))
           .replace('{x}', str(tile.x))
           .replace('{y}', str(tile.y)))
    
    # Attempt download with retries
    max_retries = 3
    for attempt in range(max_retries):
        if session_obj.is_cancelled():
            return {'status': 'cancelled', 'tile': tile}
        
        # If we're being rate limited, wait
        if session_obj.currently_throttled:
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
        
        try:
            response = http_session.get(url, timeout=15)

            if response.status_code == 200:
                # Validate response is actually an image
                content_type = response.headers.get('Content-Type', '')
                if not content_type.startswith('image/'):
                    logging.warning(f"Invalid content type '{content_type}' for tile {tile} (expected image/*)")
                    return {'status': 'failed', 'tile': tile}
                # Always save to normal cache first
                if convert_to_8bit and normal_tile_path:
                    normal_tile_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        with open(normal_tile_path, 'wb') as f:
                            f.write(response.content)

                        # Check for error image before processing
                        if check_for_error_image(normal_tile_path, session_obj):
                            # Delete the error image and return error status
                            try:
                                os.remove(normal_tile_path)
                            except OSError as e:
                                logging.warning(f"Failed to remove error image {normal_tile_path}: {e}")
                            return {'status': 'error_image', 'tile': tile}
                    except IOError as e:
                        logging.error(f"Failed to write tile {tile}: {e}")
                        return {'status': 'failed', 'tile': tile}
                    
                    # Convert in-memory to avoid double I/O
                    tile_dir.mkdir(parents=True, exist_ok=True)
                    img = None
                    try:
                        img = Image.open(normal_tile_path)
                        if img.mode != 'P':
                            img = img.quantize(colors=256)
                        img.save(tile_path, optimize=True)
                    except Exception as e:
                        logging.error(f"Error converting tile to 8-bit: {e}")
                        # Fallback to copy if conversion fails
                        shutil.copy2(normal_tile_path, tile_path)
                    finally:
                        if img:
                            img.close()
                else:
                    # Just save normally
                    tile_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        with open(tile_path, 'wb') as f:
                            f.write(response.content)

                        # Check for error image
                        if check_for_error_image(tile_path, session_obj):
                            # Delete the error image and return error status
                            try:
                                os.remove(tile_path)
                            except OSError as e:
                                logging.warning(f"Failed to remove error image {tile_path}: {e}")
                            return {'status': 'error_image', 'tile': tile}
                    except IOError as e:
                        logging.error(f"Failed to write tile {tile}: {e}")
                        return {'status': 'failed', 'tile': tile}
                
                session_obj.update_stats('completed')
                # Removed socket emission for downloaded tiles - reduces overhead dramatically
                
                # Reset rate limit counter on success
                session_obj.rate_limit_counter = 0
                session_obj.currently_throttled = False
                
                return {'status': 'downloaded', 'tile': tile}
            
            elif response.status_code == 429:
                # Rate limited
                session_obj.update_stats('rate_limited')
                session_obj.rate_limit_counter += 1
                
                if session_obj.rate_limit_counter >= RATE_LIMIT_DETECTION_THRESHOLD:
                    session_obj.currently_throttled = True
                    logging.warning(f"Rate limit detected for session {session_obj.session_id}, throttling downloads")
                    socketio.start_background_task(
                        emit_tile_event,
                        'rate_limit_warning',
                        {'message': 'Rate limit detected, slowing down requests'},
                        session_obj.session_id
                    )
                
                time.sleep(2 ** attempt)  # Exponential backoff
            
            elif response.status_code == 404:
                # Tile doesn't exist at this location
                return {'status': 'not_found', 'tile': tile}
            
            else:
                # Other error, retry with backoff
                time.sleep(2 ** attempt)
        
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                logging.error(f"Failed to download tile {tile.z}/{tile.x}/{tile.y}: {e}")
    
    # All retries failed
    session_obj.update_stats('failed')
    # Removed socket emission for failed tiles - reduces overhead
    return {'status': 'failed', 'tile': tile}

def emit_tile_event(event_name, data, session_id):
    """Safely emit tile events with proper Flask context."""
    with app.app_context():
        socketio.emit(event_name, data, to=session_id)

def emit_progress_update(session_obj):
    """Emit progress statistics to the client."""
    with session_obj.stats_lock:
        stats = session_obj.stats.copy()
    
    with app.app_context():
        socketio.emit('download_progress', stats, to=session_obj.session_id)

def get_world_tiles():
    """Generate list of tiles for zoom levels 0 to 7 for entire world."""
    tiles = []
    for z in range(8):
        for x in range(2**z):
            for y in range(2**z):
                tiles.append(mercantile.Tile(x, y, z))
    return tiles

def get_tiles_for_polygons(polygons_data, min_zoom, max_zoom):
    """Generate list of tiles that intersect with the given polygons."""
    polygons = [Polygon([(lng, lat) for lat, lng in poly]) for poly in polygons_data]
    overall_polygon = unary_union(polygons)
    west, south, east, north = overall_polygon.bounds
    all_tiles = []
    
    for z in range(min_zoom, max_zoom + 1):
        tiles = mercantile.tiles(west, south, east, north, zooms=[z])
        for tile in tiles:
            tile_bbox = mercantile.bounds(tile)
            tile_box = box(tile_bbox.west, tile_bbox.south, tile_bbox.east, tile_bbox.north)
            if any(tile_box.intersects(poly) for poly in polygons):
                all_tiles.append(tile)
    
    all_tiles.sort(key=lambda tile: (tile.z, -tile.x, tile.y))
    return all_tiles

def download_tiles_optimized(tiles, map_style, style_cache_dir, convert_to_8bit, session_obj, normal_cache_dir=None):
    """Download tiles using optimized threading with connection pooling."""
    session_obj.update_stats('total', len(tiles))
    
    with app.app_context():
        socketio.emit('download_started', {'total_tiles': len(tiles)}, to=session_obj.session_id)
    
    # Create thread-local sessions for connection pooling
    thread_local = threading.local()
    
    def get_thread_session():
        if not hasattr(thread_local, 'session'):
            thread_local.session = create_requests_session()
        return thread_local.session
    
    # Process tiles in batches with increased parallelism
    retry_queue = deque()
    progress_counter = 0
    progress_lock = threading.Lock()  # Lock for thread-safe progress counter
    progress_update_interval = 50  # Update progress every N tiles

    all_futures = []  # Track all futures for potential cancellation
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit initial batch
            futures = {}
            for tile in tiles:
                if session_obj.is_cancelled():
                    # Cancel all pending futures before breaking
                    for future in all_futures:
                        future.cancel()
                    logging.info(f"Cancelled {len(all_futures)} pending futures")
                    break
                future = executor.submit(
                    download_tile,
                    tile,
                    map_style,
                    style_cache_dir,
                    convert_to_8bit,
                    session_obj,
                    get_thread_session(),
                    normal_cache_dir
                )
                futures[future] = tile
                all_futures.append(future)

            # Process results and handle retries
            for future in as_completed(futures):
                if session_obj.is_cancelled():
                    # Cancel remaining futures
                    for f in all_futures:
                        f.cancel()
                    break

                result = future.result()

                # Check for error image detection
                if result['status'] == 'error_image':
                    logging.error(f"Error image detected at tile {result['tile'].z}/{result['tile'].x}/{result['tile'].y}")
                    # Cancel all remaining downloads
                    session_obj.cancel(session_obj.error_message)
                    for f in all_futures:
                        f.cancel()
                    break

                # Queue failed downloads for retry
                if result['status'] == 'failed' and len(retry_queue) < 1000:  # Limit retry queue size
                    retry_queue.append(result['tile'])

                # Periodic progress updates with thread-safe counter
                should_emit_progress = False
                with progress_lock:
                    progress_counter += 1
                    if progress_counter % progress_update_interval == 0:
                        should_emit_progress = True

                if should_emit_progress:
                    emit_progress_update(session_obj)
    finally:
        logging.debug(f"ThreadPoolExecutor cleanup complete")
        
        # Process retry queue if not cancelled
        if not session_obj.is_cancelled() and retry_queue:
            logging.info(f"Retrying {len(retry_queue)} failed tiles")
            futures = {}
            for tile in list(retry_queue):
                if session_obj.is_cancelled():
                    break
                future = executor.submit(
                    download_tile,
                    tile,
                    map_style,
                    style_cache_dir,
                    convert_to_8bit,
                    session_obj,
                    get_thread_session(),
                    normal_cache_dir
                )
                futures[future] = tile
            
            for future in as_completed(futures):
                future.result()  # Just ensure completion
    
    # Final progress update
    emit_progress_update(session_obj)
    
    # Check if download was cancelled due to error image
    if session_obj.error_image_detected:
        with app.app_context():
            socketio.emit('error_image_detected', {
                'message': session_obj.error_message
            }, to=session_obj.session_id)
        return
    
    if not session_obj.is_cancelled():
        with app.app_context():
            socketio.emit('tiles_downloaded', to=session_obj.session_id)

def create_zip(style_cache_dir, style_name):
    """Create a zip file from the style-specific cache directory."""
    sanitized_name = sanitize_style_name(style_name)
    zip_path = DOWNLOADS_DIR / f'{sanitized_name}.zip'
    
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipf:
        for root, _, files in os.walk(style_cache_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(style_cache_dir)
                zipf.write(file_path, arcname)
    
    return str(zip_path)

@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html')

@app.route('/get_map_sources')
def get_map_sources():
    """Return the list of map sources from the config file."""
    return jsonify(MAP_SOURCES)

@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logging.info(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection and cleanup."""
    logging.info(f"Client disconnected: {request.sid}")
    cleanup_session(request.sid)

@socketio.on('start_download')
def handle_start_download(data):
    """Handle download request for tiles within polygons."""
    session_id = request.sid

    try:
        # Validate required fields exist
        polygons_data = data.get('polygons')
        min_zoom = data.get('min_zoom')
        max_zoom = data.get('max_zoom')
        map_style_url = data.get('map_style')
        convert_to_8bit = data.get('convert_to_8bit', False)
        create_zip_file = data.get('create_zip', True)

        # Validate polygons
        if not polygons_data:
            emit('error', {'message': 'No polygons provided'})
            return

        if not isinstance(polygons_data, list) or len(polygons_data) == 0:
            emit('error', {'message': 'Invalid polygons data format'})
            return

        # Validate each polygon has at least 3 points
        for i, poly in enumerate(polygons_data):
            if not isinstance(poly, list) or len(poly) < 3:
                emit('error', {'message': f'Polygon {i+1} must have at least 3 points'})
                return

        # Validate zoom levels
        try:
            min_zoom = int(min_zoom)
            max_zoom = int(max_zoom)
            if not (0 <= min_zoom <= 19 and 0 <= max_zoom <= 19):
                raise ValueError("Zoom levels must be 0-19")
            if min_zoom > max_zoom:
                raise ValueError("Min zoom must be <= max zoom")
        except (ValueError, TypeError) as e:
            emit('error', {'message': f'Invalid zoom levels: {e}'})
            return

        # Validate map style
        style_name = next((name for name, url in MAP_SOURCES.items() if url == map_style_url), None)
        if not style_name:
            emit('error', {'message': 'Invalid map style'})
            return

        style_cache_dir = get_style_cache_dir(style_name, convert_to_8bit)

        # If converting to 8bit, also get normal cache directory
        normal_cache_dir = get_style_cache_dir(style_name, False) if convert_to_8bit else None

        tiles = get_tiles_for_polygons(polygons_data, min_zoom, max_zoom)

        # Create/reset session
        session_obj = get_session(session_id)
        session_obj.cancel_event.clear()  # Clear event (not cancelled)
        session_obj.stats = {'total': 0, 'completed': 0, 'skipped': 0, 'failed': 0, 'rate_limited': 0}
        
        def download_task():
            try:
                download_tiles_optimized(tiles, map_style_url, style_cache_dir, convert_to_8bit, session_obj, normal_cache_dir)

                if not session_obj.is_cancelled():
                    if create_zip_file:
                        zip_path = create_zip(style_cache_dir, style_name)
                        with app.app_context():
                            socketio.emit('download_complete', {'zip_url': f'/download_zip?path={zip_path}'}, to=session_id)
                    else:
                        with app.app_context():
                            socketio.emit('download_complete', {'zip_url': None, 'message': 'Tiles cached successfully'}, to=session_id)

            except Exception as e:
                logging.error(f"Download task failed for session {session_id}: {e}", exc_info=True)
                try:
                    with app.app_context():
                        socketio.emit('error', {
                            'message': f'Download failed: {str(e)}'
                        }, to=session_id)
                except Exception as emit_err:
                    logging.error(f"Failed to emit error to client: {emit_err}")
            finally:
                cleanup_session(session_id)
        
        socketio.start_background_task(download_task)
        
    except Exception as e:
        logging.error(f"Error processing download: {e}")
        emit('error', {'message': 'An error occurred while processing your request'})

@socketio.on('start_world_download')
def handle_start_world_download(data):
    """Handle download request for world basemap tiles (zoom 0-7)."""
    session_id = request.sid
    
    try:
        map_style_url = data['map_style']
        convert_to_8bit = data.get('convert_to_8bit', False)
        create_zip_file = data.get('create_zip', True)
        
        style_name = next((name for name, url in MAP_SOURCES.items() if url == map_style_url), None)
        if not style_name:
            emit('error', {'message': 'Invalid map style'})
            return
        
        style_cache_dir = get_style_cache_dir(style_name, convert_to_8bit)
        tiles = get_world_tiles()
        
        # If converting to 8bit, also get normal cache directory
        normal_cache_dir = get_style_cache_dir(style_name, False) if convert_to_8bit else None
        
        # Create/reset session
        session_obj = get_session(session_id)
        session_obj.cancel_event.set()
        session_obj.stats = {'total': 0, 'completed': 0, 'skipped': 0, 'failed': 0, 'rate_limited': 0}
        
        def download_task():
            try:
                download_tiles_optimized(tiles, map_style_url, style_cache_dir, convert_to_8bit, session_obj, normal_cache_dir)

                if not session_obj.is_cancelled():
                    if create_zip_file:
                        zip_path = create_zip(style_cache_dir, style_name)
                        with app.app_context():
                            socketio.emit('download_complete', {'zip_url': f'/download_zip?path={zip_path}'}, to=session_id)
                    else:
                        with app.app_context():
                            socketio.emit('download_complete', {'zip_url': None, 'message': 'Tiles cached successfully'}, to=session_id)

            except Exception as e:
                logging.error(f"Download task failed for session {session_id}: {e}", exc_info=True)
                try:
                    with app.app_context():
                        socketio.emit('error', {
                            'message': f'Download failed: {str(e)}'
                        }, to=session_id)
                except Exception as emit_err:
                    logging.error(f"Failed to emit error to client: {emit_err}")
            finally:
                cleanup_session(session_id)
        
        socketio.start_background_task(download_task)
        
    except Exception as e:
        logging.error(f"Error processing world download: {e}")
        emit('error', {'message': 'An error occurred while processing your request'})

@socketio.on('cancel_download')
def handle_cancel_download():
    """Handle cancellation of the download."""
    session_id = request.sid
    session_obj = get_session(session_id)
    session_obj.cancel()  # User-initiated cancellation has no error message
    emit('download_cancelled')

@app.route('/download_zip')
def download_zip():
    """Send the zip file to the user."""
    zip_path = request.args.get('path')
    
    timeout = 30
    start_time = time.time()
    while not Path(zip_path).exists():
        if time.time() - start_time > timeout:
            return 'Zip file not found', 404
        time.sleep(0.5)
    
    return send_file(zip_path, as_attachment=True, download_name=Path(zip_path).name)

@app.route('/tiles/<style_name>/<int:z>/<int:x>/<int:y>.png')
def serve_tile(style_name, z, x, y):
    """Serve a cached tile if it exists, checking both 8-bit and normal directories."""
    # Validate tile coordinates
    if not (0 <= z <= 19):
        return 'Invalid zoom level (must be 0-19)', 400

    max_coord = 2 ** z
    if not (0 <= x < max_coord and 0 <= y < max_coord):
        return f'Invalid tile coordinates for zoom {z} (must be 0-{max_coord-1})', 400

    # Try 8-bit first, then normal
    for convert_to_8bit in [True, False]:
        style_cache_dir = get_style_cache_dir(style_name, convert_to_8bit)
        tile_path = style_cache_dir / str(z) / str(x) / f"{y}.png"

        if tile_path.exists():
            return send_file(tile_path)

    return '', 404

@app.route('/delete_cache/<style_name>', methods=['DELETE'])
def delete_cache(style_name):
    """Delete the cache directory for a specific style (both 8-bit and normal)."""
    sanitized_name = sanitize_style_name(style_name)
    
    # Delete both 8bit and normal directories
    normal_cache = CACHE_DIR / 'normal' / sanitized_name
    eightbit_cache = CACHE_DIR / '8bit' / sanitized_name
    
    deleted = False
    if normal_cache.exists():
        shutil.rmtree(normal_cache)
        deleted = True
    
    if eightbit_cache.exists():
        shutil.rmtree(eightbit_cache)
        deleted = True
    
    if deleted:
        return '', 204
    
    return 'Cache not found', 404

@app.route('/get_cached_tiles/<style_name>')
def get_cached_tiles_route(style_name):
    """Return a list of [z, x, y] for cached tiles of the given style, with optional filtering."""
    # Get optional query parameters for filtering
    zoom = request.args.get('zoom', type=int)
    min_x = request.args.get('min_x', type=int)
    max_x = request.args.get('max_x', type=int)
    min_y = request.args.get('min_y', type=int)
    max_y = request.args.get('max_y', type=int)
    zoom_range = request.args.get('zoom_range', default=1, type=int)  # ±zoom_range
    
    cached_tiles = set()  # Use set to avoid duplicates from 8-bit and normal caches
    
    # Check both 8-bit and normal directories
    for convert_to_8bit in [True, False]:
        style_cache_dir = get_style_cache_dir(style_name, convert_to_8bit)
        
        if not style_cache_dir.exists():
            continue
        
        for z_dir in style_cache_dir.iterdir():
            if z_dir.is_dir():
                try:
                    z = int(z_dir.name)
                    
                    # Filter by zoom level if specified
                    # zoom_range parameter means "show tiles within ±zoom_range levels"
                    # For zoom_range=1: shows current zoom ±1 (3 levels total)
                    # For zoom_range=0: shows only current zoom (1 level)
                    if zoom is not None and abs(z - zoom) > zoom_range:
                        continue
                    
                    for x_dir in z_dir.iterdir():
                        if x_dir.is_dir():
                            try:
                                x = int(x_dir.name)
                                
                                # Filter by x coordinate if specified
                                if min_x is not None and x < min_x:
                                    continue
                                if max_x is not None and x > max_x:
                                    continue
                                
                                for y_file in x_dir.glob('*.png'):
                                    try:
                                        y = int(y_file.stem)
                                        
                                        # Filter by y coordinate if specified
                                        if min_y is not None and y < min_y:
                                            continue
                                        if max_y is not None and y > max_y:
                                            continue
                                        
                                        cached_tiles.add((z, x, y))
                                    except ValueError:
                                        pass
                            except ValueError:
                                pass
                except ValueError:
                    pass
    
    # Convert set back to list format
    return jsonify([[z, x, y] for z, x, y in sorted(cached_tiles)])

if __name__ == '__main__':
    CACHE_DIR.mkdir(exist_ok=True)
    CONFIG_DIR.mkdir(exist_ok=True)
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
