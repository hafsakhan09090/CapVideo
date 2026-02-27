import os
import subprocess
import logging
import uuid
import threading
import tempfile
import shutil
import time
from datetime import datetime, timedelta
import gc
import psutil
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
import hashlib
import jwt
import json

# ========== AMD LIBRARIES WITH COMPLETE ISOLATION ==========
AMD_ACCELERATOR_AVAILABLE = False
amd_accelerator = None
amd_monitor = None

# Try to import AMD modules, but don't fail if they're not available
try:
    # First check if amdsmi is installed without importing it
    import importlib
    amdsmi_spec = importlib.util.find_spec("amdsmi")
    if amdsmi_spec is not None:
        print("✅ AMD SMI package found")
    else:
        print("ℹ️ AMD SMI package not found - running without GPU monitoring")
    
    # Now try to import our modules
    from amd_accelerator import AMDAccelerator
    from amd_monitor import AMDMonitor
    AMD_ACCELERATOR_AVAILABLE = True
    print("✅ AMD Accelerator modules loaded successfully")
except ImportError as e:
    print(f"⚠️ AMD Accelerator modules not available: {e}")
    print("   Running in standard mode (all features work, AMD optional)")
except Exception as e:
    print(f"⚠️ Unexpected error loading AMD modules: {e}")
    print("   Continuing in standard mode")

# Initialize AMD components only if available
if AMD_ACCELERATOR_AVAILABLE:
    try:
        amd_accelerator = AMDAccelerator()
        try:
            amd_monitor = AMDMonitor()
            amd_monitor.start_monitoring()
            print("✅ AMD Monitor started")
        except Exception as e:
            print(f"⚠️ AMD Monitor not available: {e}")
            amd_monitor = None
        
        # Log AMD system info
        system_report = amd_accelerator.get_system_report()
        print(f"🎯 AMD System Report: {json.dumps(system_report, indent=2)}")
    except Exception as e:
        print(f"⚠️ AMD Accelerator init failed: {e}")
        print("   Continuing without AMD acceleration")
        amd_accelerator = None

# AI imports
try:
    import whisper
    WHISPER_AVAILABLE = True
    print("✅ Whisper imported successfully")
except ImportError as e:
    WHISPER_AVAILABLE = False
    print(f"❌ Whisper import failed: {e}")

app = Flask(__name__)
CORS(app)

# Hugging Face specific paths
BASE_DIR = "/app"
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
PROCESSED_FOLDER = os.path.join(BASE_DIR, 'processed')
AMD_METRICS_FOLDER = os.path.join(BASE_DIR, 'amd_metrics')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)
os.makedirs(AMD_METRICS_FOLDER, exist_ok=True)

# Storage
TEMP_STORAGE_LIMIT = 500 * 1024 * 1024  # 500MB
job_status = {}
users = {}
user_jobs = {}
processing_lock = threading.Lock()

# JWT Secret
SECRET_KEY = os.environ.get('SECRET_KEY', 'capvideo-amd-challenge-2024')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Whisper model with optimizations
whisper_model = None
if WHISPER_AVAILABLE:
    try:
        print("🚀 Loading Whisper model...")
        
        # Check device availability
        import torch
        if torch.cuda.is_available():
            # Check if it's ROCm or CUDA
            if hasattr(torch.version, 'hip'):
                device = "cuda"
                print(f"✅ ROCm detected! Using AMD GPU: {torch.cuda.get_device_name(0)}")
            else:
                device = "cuda"
                print(f"⚠️ CUDA detected (not ROCm) - using GPU: {torch.cuda.get_device_name(0)}")
        else:
            device = "cpu"
            print("ℹ️ No GPU detected - using CPU mode")
        
        # Load model with appropriate device
        whisper_model = whisper.load_model("tiny", device=device)
        print(f"✅ Whisper tiny model loaded successfully on {device}!")
        
    except Exception as e:
        print(f"❌ Whisper loading failed: {e}")
        WHISPER_AVAILABLE = False

# ========== HELPER FUNCTIONS ==========

def get_directory_size(directory):
    total = 0
    try:
        for entry in os.scandir(directory):
            if entry.is_file():
                total += entry.stat().st_size
    except:
        pass
    return total

def cleanup_old_files():
    current_time = datetime.now()
    cutoff_time = current_time - timedelta(hours=2)
    
    with processing_lock:
        jobs_to_remove = []
        for job_id, job_info in list(job_status.items()):
            if job_info.get('status') in ['completed', 'failed']:
                jobs_to_remove.append(job_id)
        
        for job_id in jobs_to_remove[5:]:  # Keep last 5 jobs
            cleanup_job_files(job_id)

def cleanup_job_files(job_id):
    with processing_lock:
        job_status.pop(job_id, None)
        user_jobs.pop(job_id, None)
    
    for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER]:
        for filename in os.listdir(folder):
            if filename.startswith(job_id):
                try:
                    os.remove(os.path.join(folder, filename))
                except:
                    pass

def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_srt(segments, srt_path):
    try:
        with open(srt_path, "w", encoding="utf-8") as f:
            idx = 1
            for seg in segments:
                text = seg['text'].strip()
                if not text:
                    continue
                f.write(f"{idx}\n")
                f.write(f"{format_time(seg['start'])} --> {format_time(seg['end'])}\n")
                f.write(f"{text}\n\n")
                idx += 1
        return True
    except Exception as e:
        logger.error(f"SRT generation failed: {e}")
        raise

def overlay_subtitles(input_path, srt_path, output_path, caption_settings=None):
    try:
        if caption_settings is None:
            caption_settings = {}
        
        # Get caption settings with defaults
        font_size = caption_settings.get('size', '20')
        font_color = caption_settings.get('color', 'white')
        
        # Build FFmpeg command with subtitle styling
        # Using simple subtitle filter for compatibility
        cmd = [
            'ffmpeg', '-y',
            '-i', input_path,
            '-vf', f"subtitles={srt_path}:force_style='Fontsize={font_size}'",
            '-c:v', 'libx264',
            '-c:a', 'copy',
            '-preset', 'fast',
            output_path
        ]
        
        logger.info(f"🔧 Running FFmpeg command")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            logger.error(f"FFmpeg error: {result.stderr}")
            raise Exception(f"FFmpeg failed: {result.stderr}")
        
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("✅ Subtitles embedded successfully!")
            return True
        else:
            raise Exception("Output file not created")
        
    except Exception as e:
        logger.error(f"Subtitle overlay failed: {e}")
        raise

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_token(token):
    try:
        decoded = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return decoded['username']
    except:
        return None

def download_youtube_video(youtube_url, job_id):
    try:
        temp_video = os.path.join(UPLOAD_FOLDER, f"{job_id}_youtube.mp4")
        
        ydl_opts = {
            'format': 'best[height<=720]',
            'outtmpl': temp_video,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            title = info.get('title', 'youtube_video')
            
        return temp_video, f"{title}.mp4"
        
    except Exception as e:
        logger.error(f"YouTube download failed: {e}")
        raise Exception(f"YouTube download failed: {str(e)}")

# ========== AMD-SPECIFIC ROUTES WITH SAFE FALLBACKS ==========

@app.route('/amd/status')
def amd_status():
    """Get AMD hardware and library status (safe fallback)"""
    if amd_accelerator:
        try:
            return jsonify(amd_accelerator.get_system_report())
        except Exception as e:
            return jsonify({
                'error': f'AMD status error: {e}',
                'note': 'Running in standard mode'
            })
    else:
        return jsonify({
            'amd_libraries': {
                'amdsmi': False,
                'aocl_optimized': False,
                'rocm': False,
            },
            'hardware': {
                'cpu': 'Standard',
                'gpu_count': 0
            },
            'optimization_level': 'Standard Mode',
            'environment': 'Hugging Face Space',
            'note': 'AMD features are optional - app works perfectly without them'
        })

@app.route('/amd/metrics')
def amd_metrics():
    """Get real-time AMD GPU metrics (safe fallback)"""
    if amd_monitor:
        try:
            return jsonify(amd_monitor.get_current_metrics())
        except:
            pass
    
    return jsonify({
        'gpu_count': 0,
        'gpus': [],
        'note': 'AMD monitoring not available - running in standard mode',
        'message': 'Your video processing is still working! AMD features are optional.'
    })

@app.route('/amd/performance')
def amd_performance():
    """Get AMD performance summary (safe fallback)"""
    if amd_monitor:
        try:
            return jsonify({
                'summary': amd_monitor.get_performance_summary(),
                'metrics': amd_monitor.get_current_metrics()
            })
        except:
            pass
    
    return jsonify({
        'summary': 'Running in standard mode - no AMD acceleration detected',
        'message': 'This is expected in Hugging Face Spaces. The app works perfectly!',
        'metrics': {'note': 'AMD libraries are optional - core functionality unaffected'}
    })

@app.route('/amd/optimize/text', methods=['POST'])
def amd_optimize_text():
    """Use AMD optimizations for text processing (safe fallback)"""
    data = request.get_json()
    text = data.get('text', '')
    
    if amd_accelerator:
        try:
            result = amd_accelerator.fast_text_processing(text)
            return jsonify(result)
        except:
            pass
    
    # Fallback to standard processing
    result = {
        'word_count': len(text.split()),
        'char_count': len(text),
        'optimization': 'standard (no AMD)',
        'note': 'AMD optimizations optional - results are identical'
    }
    
    return jsonify(result)

# ========== MAIN APPLICATION ROUTES ==========

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/health')
def health():
    """Enhanced health check with AMD info (safe fallback)"""
    health_data = {
        "status": "ok",
        "platform": "huggingface",
        "whisper_available": WHISPER_AVAILABLE,
        "model_loaded": whisper_model is not None,
        "storage_used_mb": round(get_directory_size(BASE_DIR) / 1024 / 1024, 2),
        "amd_acceleration": {
            "available": amd_accelerator is not None,
            "gpu_count": amd_accelerator.amd_gpu_count if amd_accelerator else 0,
            "cpu_optimized": amd_accelerator.amd_cpu_optimized if amd_accelerator else False,
            "rocm_version": getattr(amd_accelerator, 'rocm_version', 'N/A') if amd_accelerator else "N/A",
            "note": "AMD features are optional - app runs fine without them"
        }
    }
    
    # Add AMD metrics if available
    if amd_monitor:
        try:
            health_data["amd_metrics"] = amd_monitor.get_current_metrics()
        except:
            pass
    
    return jsonify(health_data)

@app.route('/signup', methods=['POST'])
def signup():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
            
        username = data.get('username', '').strip()
        password = data.get('password', '')
        
        if not username or not password:
            return jsonify({'error': 'Username and password required'}), 400
        if len(username) < 3:
            return jsonify({'error': 'Username must be at least 3 characters'}), 400
        
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        with processing_lock:
            if username in users:
                return jsonify({'error': 'Username already exists'}), 400
            
            users[username] = {
                'password_hash': password_hash,
                'history': [],
                'favorites': set()
            }
            
            token = jwt.encode({'username': username}, SECRET_KEY, algorithm='HS256')
            
        logger.info(f"✅ User signed up: {username}")
        return jsonify({'token': token}), 201
        
    except Exception as e:
        logger.error(f"Signup error: {e}")
        return jsonify({'error': 'Signup failed'}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
            
        username = data.get('username', '').strip()
        password = data.get('password', '')
        
        if not username or not password:
            return jsonify({'error': 'Username and password required'}), 400
        
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        with processing_lock:
            user = users.get(username)
            if not user or user['password_hash'] != password_hash:
                return jsonify({'error': 'Invalid credentials'}), 401
            
            token = jwt.encode({'username': username}, SECRET_KEY, algorithm='HS256')
            
        logger.info(f"✅ User logged in: {username}")
        return jsonify({'token': token}), 200
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'error': 'Login failed'}), 500

@app.route('/upload', methods=['POST'])
def upload_video():
    if 'video' not in request.files:
        return jsonify({'error': 'No video file provided'}), 400

    video = request.files['video']
    if video.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    # Check file extension
    allowed_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.3gp'}
    file_ext = os.path.splitext(video.filename.lower())[1]
    if file_ext not in allowed_extensions:
        return jsonify({'error': 'Only video files are allowed'}), 400

    current_storage = get_directory_size(BASE_DIR)
    if current_storage > TEMP_STORAGE_LIMIT * 0.8:
        cleanup_old_files()

    job_id = str(uuid.uuid4())
    filename = f"{job_id}_{video.filename}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    
    try:
        video.save(filepath)
        
        with processing_lock:
            job_status[job_id] = {'status': 'uploaded', 'filename': video.filename}
        
        # Get caption settings from form data
        caption_settings = {}
        if 'captionSettings' in request.form:
            try:
                caption_settings = json.loads(request.form['captionSettings'])
                logger.info(f"🎨 Caption settings received: {caption_settings}")
            except Exception as e:
                logger.warning(f"Could not parse caption settings: {e}")
        
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        thread = threading.Thread(
            target=process_video_task, 
            args=(job_id, filepath, video.filename, False, token, caption_settings), 
            daemon=True
        )
        thread.start()

        return jsonify({'job_id': job_id}), 202
        
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500

@app.route('/transcribe', methods=['POST'])
def transcribe_youtube():
    try:
        data = request.get_json()
        youtube_url = data.get('url')
        caption_settings = data.get('captionSettings', {})
        
        if not youtube_url:
            return jsonify({'error': 'YouTube URL required'}), 400
        
        # Validate YouTube URL
        if 'youtube.com' not in youtube_url and 'youtu.be' not in youtube_url:
            return jsonify({'error': 'Invalid YouTube URL'}), 400
        
        current_storage = get_directory_size(BASE_DIR)
        if current_storage > TEMP_STORAGE_LIMIT * 0.8:
            cleanup_old_files()
        
        job_id = str(uuid.uuid4())
        
        with processing_lock:
            job_status[job_id] = {'status': 'downloading', 'filename': 'YouTube Video'}
        
        # Start download in background
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        
        def download_and_process():
            try:
                video_path, filename = download_youtube_video(youtube_url, job_id)
                thread = threading.Thread(
                    target=process_video_task,
                    args=(job_id, video_path, filename, True, token, caption_settings),
                    daemon=True
                )
                thread.start()
            except Exception as e:
                logger.error(f"YouTube download failed: {e}")
                with processing_lock:
                    job_status[job_id] = {'status': 'failed', 'error': str(e)}
        
        download_thread = threading.Thread(target=download_and_process, daemon=True)
        download_thread.start()

        return jsonify({'job_id': job_id}), 202
        
    except Exception as e:
        logger.error(f"YouTube processing failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/status/<job_id>', methods=['GET'])
def get_status(job_id):
    with processing_lock:
        if job_id not in job_status:
            return jsonify({'error': 'Job not found'}), 404
        return jsonify(job_status[job_id])

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    path = os.path.join(PROCESSED_FOLDER, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'File not found'}), 404
    
    return send_from_directory(PROCESSED_FOLDER, filename, as_attachment=True)

@app.route('/profile', methods=['GET'])
def get_profile():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    username = verify_token(token)
    if not username:
        return jsonify({'error': 'Unauthorized'}), 401
    
    with processing_lock:
        user = users.get(username, {})
        job_ids = user.get('history', [])
        favorites = user.get('favorites', set())
        history = []
        
        for job_id in job_ids:
            job_info = user_jobs.get(job_id, {}).copy()
            if job_info:
                job_info['job_id'] = job_id
                job_info['favorited'] = job_id in favorites
                history.append(job_info)
        
        history.sort(key=lambda x: (x.get('date', ''), x.get('time', '')), reverse=True)
        
        return jsonify({
            'username': username,
            'job_count': len(job_ids),
            'favorite_count': len(favorites),
            'history': history
        }), 200

@app.route('/history/<job_id>/favorite', methods=['POST'])
def toggle_favorite(job_id):
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    username = verify_token(token)
    if not username:
        return jsonify({'error': 'Unauthorized'}), 401
    
    with processing_lock:
        user = users.get(username)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        if job_id not in user.get('history', []):
            return jsonify({'error': 'Job not found in user history'}), 404
        
        if 'favorites' not in user:
            user['favorites'] = set()
        
        if job_id in user['favorites']:
            user['favorites'].discard(job_id)
            favorited = False
        else:
            user['favorites'].add(job_id)
            favorited = True
        
        return jsonify({'favorited': favorited}), 200

@app.route('/history/<job_id>', methods=['DELETE'])
def delete_history_item(job_id):
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    username = verify_token(token)
    if not username:
        return jsonify({'error': 'Unauthorized'}), 401
    
    with processing_lock:
        user = users.get(username)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        if job_id in user['history']:
            user['history'].remove(job_id)
        
        if 'favorites' in user and job_id in user['favorites']:
            user['favorites'].discard(job_id)
        
        if job_id in job_status:
            del job_status[job_id]
        if job_id in user_jobs:
            del user_jobs[job_id]
    
    cleanup_job_files(job_id)
    
    return jsonify({'message': 'History item deleted successfully'}), 200

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

# ========== VIDEO PROCESSING TASK ==========

def process_video_task(job_id, filepath, filename, is_youtube=False, token=None, caption_settings=None):
    """Enhanced video processing with AMD optimizations (safe fallback)"""
    try:
        logger.info(f"Starting processing for job {job_id}")
        
        # Record start metrics if AMD monitor available
        if amd_monitor:
            try:
                start_metrics = amd_monitor.get_current_metrics()
                logger.info(f"AMD metrics: {start_metrics}")
            except:
                pass
        
        with processing_lock:
            job_status[job_id] = {'status': 'transcribing', 'filename': filename}

        if not WHISPER_AVAILABLE or whisper_model is None:
            error_msg = "Whisper model not loaded. Please check server logs."
            logger.error(error_msg)
            raise Exception(error_msg)
        
        # Transcribe with whisper
        logger.info(f"🎤 Starting transcription with whisper for {filename}...")
        
        result = whisper_model.transcribe(filepath, word_timestamps=True)
        
        if not result or 'segments' not in result:
            raise Exception("No speech detected in the video")
        
        # Optimize transcription text with AMD libraries if available
        if amd_accelerator:
            try:
                text_metrics = amd_accelerator.fast_text_processing(
                    " ".join([seg['text'] for seg in result['segments']])
                )
                logger.info(f"AMD text processing: {text_metrics}")
            except:
                pass

        with processing_lock:
            job_status[job_id] = {'status': 'generating_captions', 'filename': filename}
        
        srt_path = os.path.join(PROCESSED_FOLDER, f"{job_id}_captions.srt")
        output_filename = f"{job_id}_with_subtitles.mp4"
        output_path = os.path.join(PROCESSED_FOLDER, output_filename)

        generate_srt(result["segments"], srt_path)
        
        transcription_text = " ".join([seg['text'].strip() for seg in result["segments"]])
        video_duration = result['segments'][-1]['end'] if result['segments'] else 0
        
        with processing_lock:
            job_status[job_id] = {'status': 'embedding_subtitles', 'filename': filename}
        
        logger.info(f"🎯 Using caption settings: {caption_settings}")
        overlay_subtitles(filepath, srt_path, output_path, caption_settings)
        
        if os.path.exists(output_path):
            end_time = datetime.now()
            
            with processing_lock:
                job_info = {
                    'status': 'completed',
                    'filename': filename,
                    'download_url': f"/download/{output_filename}",
                    'transcription': transcription_text,
                    'date': end_time.strftime('%Y-%m-%d'),
                    'time': end_time.strftime('%H:%M:%S'),
                    'duration': f"{int(video_duration // 60)}:{int(video_duration % 60):02d}",
                    'amd_accelerated': amd_accelerator is not None
                }
                job_status[job_id] = job_info
                user_jobs[job_id] = job_info
                if token:
                    username = verify_token(token)
                    if username and username in users:
                        users[username]['history'].append(job_id)
            
            # Cleanup
            try:
                os.remove(srt_path)
                if filepath != output_path:
                    os.remove(filepath)
            except:
                pass
                
            logger.info(f"✅ Processing completed for job {job_id}")
        else:
            raise Exception("Output video not created")
            
    except Exception as e:
        logger.error(f"❌ Processing failed for job {job_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        start_time = datetime.now()
        with processing_lock:
            job_info = {
                'status': 'failed',
                'filename': filename,
                'error': str(e),
                'date': start_time.strftime('%Y-%m-%d'),
                'time': start_time.strftime('%H:%M:%S'),
                'duration': 'N/A'
            }
            job_status[job_id] = job_info
            user_jobs[job_id] = job_info
            if token:
                username = verify_token(token)
                if username and username in users:
                    users[username]['history'].append(job_id)

# ========== CLEANUP LOOP ==========

def cleanup_loop():
    while True:
        time.sleep(1800)  # 30 minutes
        cleanup_old_files()
        
        # Log AMD metrics periodically if available
        if amd_monitor:
            try:
                metrics = amd_monitor.get_current_metrics()
                logger.info(f"AMD Periodic Metrics: {metrics}")
            except:
                pass

cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7860))
    print("=" * 60)
    print("🚀 CapVideo Starting...")
    print("=" * 60)
    print(f"🔊 Whisper: {'✅ Available' if WHISPER_AVAILABLE else '❌ Not Available'}")
    if WHISPER_AVAILABLE and whisper_model:
        import torch
        if torch.cuda.is_available():
            if hasattr(torch.version, 'hip'):
                device = "AMD GPU (ROCm)"
            else:
                device = "GPU (CUDA)"
        else:
            device = "CPU"
        print(f"   Model: tiny on {device}")
    
    print(f"\n🎯 AMD Status:")
    if amd_accelerator:
        print(f"   ✅ AMD libraries loaded")
        try:
            report = amd_accelerator.get_system_report()
            print(f"   Mode: {report.get('optimization_level', 'Unknown')}")
        except:
            print(f"   Mode: Active")
    else:
        print(f"   ⚠️ AMD libraries not available (normal in HF Spaces)")
        print(f"   ✅ App running in standard mode - all features work!")
    
    print(f"\n🌐 Server: http://0.0.0.0:{port}")
    print(f"💾 Storage: {TEMP_STORAGE_LIMIT/1024/1024}MB limit")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=False)
