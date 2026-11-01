import os
import re
import threading
import json
import uuid
import webbrowser
import time
import base64
from flask import Flask, render_template_string, request, jsonify, send_from_directory, abort
import yt_dlp

app = Flask(__name__)

# ดึงตำแหน่งที่อยู่ปัจจุบันของไฟล์โปรแกรมนี้
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# สร้างโฟลเดอร์ชื่อ 'downloads' ไว้ในจุดเดียวกับไฟล์โปรแกรมเพื่อรองรับทุกเครื่อง
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, "downloads")
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# === ระบบจัดการงานดาวน์โหลดแบบหลายงานพร้อมกัน (แก้ Race Condition) ===
# แต่ละงานจะได้ job_id ของตัวเอง สถานะไม่ชนกันอีกต่อไป
jobs = {}
jobs_lock = threading.Lock()
MAX_JOBS_KEPT = 50  # เก็บประวัติงานล่าสุดไว้ไม่เกินนี้ กันหน่วยความจำบวม


def create_job():
    """สร้างงานใหม่พร้อมสถานะเริ่มต้น คืนค่า job_id"""
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "status": "starting",
            "percent": 0,
            "speed": "0 MB/s",
            "size": "0 MB",
            "message": "กำลังเข้าควบคุมเครือข่ายสตรีมมิ่งของ YouTube...",
            "last_file": "",
            "created_at": time.time(),
        }
        # ล้างงานเก่าที่จบแล้วออกเมื่อมีงานค้างในระบบเยอะเกิน
        if len(jobs) > MAX_JOBS_KEPT:
            finished = sorted(
                ((jid, j) for jid, j in jobs.items() if j["status"] in ("success", "failed")),
                key=lambda x: x[1]["created_at"],
            )
            for jid, _ in finished[: len(jobs) - MAX_JOBS_KEPT]:
                jobs.pop(jid, None)
    return job_id


def update_job(job_id, **kwargs):
    """อัปเดตสถานะงานแบบ thread-safe"""
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)


def make_progress_hook(job_id):
    """สร้างฟังก์ชันดักจับสถานะจาก yt-dlp ผูกกับ job_id เฉพาะงานนั้น"""
    def progress_hook(d):
        if d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('max_bytes') or 0
            downloaded_bytes = d.get('downloaded_bytes', 0)
            speed = d.get('speed', 0)

            if total_bytes > 0:
                percent = (downloaded_bytes / total_bytes) * 100
                total_mb = total_bytes / (1024 * 1024)
                speed_mb = speed / (1024 * 1024) if speed else 0

                update_job(
                    job_id,
                    status="downloading",
                    percent=round(percent, 1),
                    speed=f"{speed_mb:.2f} MB/s",
                    size=f"{total_mb:.1f} MB",
                    message=f"กำลังสกัดข้อมูลสตรีม... {percent:.1f}%",
                )

        elif d['status'] == 'finished':
            update_job(
                job_id,
                status="processing",
                message="ดาวน์โหลดคอร์หลักเสร็จสิ้น กำลังผสานรวมไฟล์ขั้นสุดท้ายด้วย FFmpeg...",
            )
    return progress_hook


def run_download(url, mode, resolution, job_id):
    """ฟังก์ชันแยกเธรดดาวน์โหลดที่ฉากหลัง ป้องกันหน้าเว็บค้าง"""

    # ตั้งค่าเซฟไฟล์ลงโฟลเดอร์ downloads บนเซิร์ฟเวอร์หลักของคุณ
    output_template = os.path.join(DOWNLOAD_FOLDER, "%(title)s.%(ext)s")

    ydl_opts = {
        'progress_hooks': [make_progress_hook(job_id)],
        'outtmpl': output_template,
        'cookiefile': 'cookies.txt',
        'extractor_args': {'youtube': {'player_client': ['web_safari', 'android_vr']}},
    }

    if mode == 'audio':
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    elif mode == 'resolution':
        ydl_opts.update({
            'format': f'bestvideo[height<={resolution.replace("p", "")}]+bestaudio/best',
            'merge_output_format': 'mp4',
        })
    else:
        ydl_opts.update({
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
        })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # วิเคราะห์โครงสร้างเพื่อหาชื่อไฟล์จริงส่งกลับไปให้หน้าเว็บเบราว์เซอร์
            info_dict = ydl.extract_info(url, download=False)
            filename = ydl.prepare_filename(info_dict)

            if mode == 'audio':
                filename = os.path.splitext(filename)[0] + ".mp3"
            else:
                filename = os.path.splitext(filename)[0] + ".mp4"

            just_filename = os.path.basename(filename)

            # เริ่มทำงานดาวน์โหลดจริง
            ydl.download([url])

        update_job(
            job_id,
            last_file=just_filename,
            status="success",
            percent=100,
            message="🎉 QUANTUM DOWNLOAD SUCCESSFUL! สกัดสตรีมไฟล์พร้อมส่งเข้าเครื่องของคุณแล้ว",
        )
    except Exception as e:
        clean_error = re.sub(r'color:.*?;', '', str(e))
        update_job(
            job_id,
            status="failed",
            message=f"❌ เกิดข้อผิดพลาดในคอร์ระบบ: {clean_error}",
        )


# หน้าจอออกแบบระดับอนาคตล้ำยุค สไตล์ Cyberpunk HUD (รวมเครดิต JackZaa AI)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JACK LOADER - By JackZaa AI</title>
    <!-- โหลด Bootstrap จริงจาก CDN -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        /* โหลดฟอนต์ Orbitron / Rajdhani จริงจาก Google Fonts */
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;500;700&display=swap');

        body { 
            background: radial-gradient(circle at 50% 50%, #0b0f19 0%, #020408 100%); 
            color: #00f2fe; font-family: 'Rajdhani', sans-serif; min-height: 100vh; overflow-x: hidden; position: relative;
        }
        .matrix-bg {
            position: fixed; width: 100%; height: 100%; top:0; left:0;
            background-image: linear-gradient(rgba(0, 242, 254, 0.02) 1px, transparent 1px), linear-gradient(90deg, rgba(0, 242, 254, 0.02) 1px, transparent 1px);
            background-size: 30px 30px; z-index: -1; animation: gridMove 20s linear infinite;
        }
        @keyframes gridMove { 0% { background-position: 0 0; } 100% { background-position: 30px 60px; } }
        .main-card { 
            max-width: 750px; margin: 60px auto; background: rgba(6, 11, 25, 0.75); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(0, 242, 254, 0.3); border-radius: 16px; box-shadow: 0 0 50px rgba(0, 242, 254, 0.1); padding: 40px; position: relative;
        }
        .main-card::before { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 4px; background: linear-gradient(90deg, #ff007f, #00f2fe, #39ff14); }
        .cyber-header { font-family: 'Orbitron', sans-serif; text-shadow: 0 0 10px rgba(0, 242, 254, 0.8), 0 0 20px rgba(0, 114, 255, 0.5); font-weight: 900; letter-spacing: 3px; text-transform: uppercase; font-size: 2.2rem; color: #ffffff; }
        .cyber-label { font-family: 'Orbitron', sans-serif; font-size: 0.85rem; letter-spacing: 1px; color: #00f2fe; text-transform: uppercase; }
        .form-control-cyber { background: rgba(2, 6, 23, 0.9) !important; border: 1px solid rgba(0, 242, 254, 0.5) !important; color: #39ff14 !important; font-family: 'Courier New', monospace; border-radius: 6px; }
        .cyber-radio-group { background: rgba(2, 6, 23, 0.4); border: 1px solid rgba(0, 242, 254, 0.15); padding: 15px; border-radius: 8px; }
        .btn-cyber { font-family: 'Orbitron', sans-serif; background: linear-gradient(135deg, rgba(0, 242, 254, 0.1), rgba(0, 114, 255, 0.1)); border: 1px solid #00f2fe; color: #00f2fe; font-weight: 700; letter-spacing: 2px; padding: 16px; border-radius: 6px; width: 100%; text-transform: uppercase; transition: all 0.4s ease; }
        .btn-cyber:hover { background: #00f2fe; color: #020617; box-shadow: 0 0 30px rgba(0, 242, 254, 0.6); }
        .btn-save-device { font-family: 'Orbitron', sans-serif; background: linear-gradient(135deg, #ff007f, #ff0055); border: none; color: white; font-weight: 700; padding: 14px; border-radius: 6px; width: 100%; text-transform: uppercase; box-shadow: 0 0 20px rgba(255, 0, 127, 0.4); transition: all 0.3s ease; text-decoration: none; display: block; text-align: center; }
        .btn-save-device:hover { box-shadow: 0 0 35px #ff007f; color: white; transform: scale(1.01); }
        .progress-cyber { background: rgba(2, 6, 23, 0.9); border: 1px solid rgba(0, 242, 254, 0.3); height: 34px; }
        .progress-bar-cyber { background: linear-gradient(90deg, #00f2fe, #0072ff, #ff007f); font-family: 'Orbitron', sans-serif; font-weight: 700; }
        .status-container { border: 1px dashed rgba(0, 242, 254, 0.25); background: rgba(2, 6, 23, 0.6); }
        .glow-text-green { color: #39ff14; text-shadow: 0 0 8px rgba(57, 255, 20, 0.6); }
        .glow-text-pink { color: #ff007f; text-shadow: 0 0 8px rgba(255, 0, 127, 0.6); font-family: 'Orbitron', sans-serif; }
        .credit-text { text-align: center; font-size: 0.85rem; color: #6c757d; margin-top: 30px; font-weight: 500; font-family: 'Orbitron', sans-serif; }
    </style>
</head>
<body>
<div class="matrix-bg"></div>
<div class="container">
    <div class="card main-card">
        <div class="text-center mb-5">
            <h1 class="cyber-header">JACK LOADER</h1>
            <div class="text-white-50 small font-monospace mt-2">QUANTUM SYSTEM PROTOCOL // CORE ENGINE v4.5 // STATUS: ONLINE</div>
        </div>
        <div class="card-body p-0">
            <div class="mb-4">
                <label for="url" class="form-label cyber-label mb-2">⚡ INPUT QUANTUM STREAM DATA URL:</label>
                <input type="text" id="url" class="form-control form-control-cyber form-control-lg" placeholder="PARSING YOUTUBE LINK STREAM HERE...">
            </div>
            <div class="mb-4">
                <label class="cyber-label mb-2">🎛️ EXTRACTION FORMAT CONFIGURATION:</label>
                <div class="cyber-radio-group d-flex gap-4 flex-wrap">
                    <div class="form-check">
                        <input class="form-check-input" style="background-color:#020617; border-color:#00f2fe;" type="radio" name="mode" id="mode_best" value="video" checked>
                        <label class="form-check-label text-white" for="mode_best">MAX QUALITY VIDEO</label>
                    </div>
                    <div class="form-check">
                        <input class="form-check-input" style="background-color:#020617; border-color:#00f2fe;" type="radio" name="mode" id="mode_1080p" value="1080p">
                        <label class="form-check-label text-white" for="mode_1080p">1080p FHD RESOLUTION</label>
                    </div>
                    <div class="form-check">
                        <input class="form-check-input" style="background-color:#020617; border-color:#00f2fe;" type="radio" name="mode" id="mode_audio" value="audio">
                        <label class="form-check-label text-white" for="mode_audio">DECODE AUDIO ONLY (MP3)</label>
                    </div>
                </div>
            </div>
            <button id="start-btn" class="btn btn-cyber mb-4" onclick="startDownload()">INITIALIZE CORE DOWNLOAD ⚡</button>
            <div id="progress-area" class="d-none status-container p-4 rounded mb-4">
                <div class="d-flex justify-content-between mb-3 align-items-center">
                    <span id="status-msg" class="glow-text-green">AWAITING SYSTEM SYNCHRONIZATION...</span>
                    <span id="speed-msg" class="glow-text-pink">0.00 MB/s</span>
                </div>
                <div class="progress progress-cyber mb-3">
                    <div id="bar-fill" class="progress-bar progress-bar-cyber progress-bar-striped progress-bar-animated" role="progressbar" style="width: 0%">0%</div>
                </div>
                <div id="size-msg" class="text-end text-white-50 font-monospace small">STREAM DATA SIZE: 0 MB</div>
            </div>
            <div id="device-save-area" class="d-none border border-info p-4 rounded bg-dark bg-opacity-50 mb-3 text-center">
                <label class="cyber-label mb-3 text-white">⚡ SYSTEM CORE MESSAGE: EXTRACTION COMPLETED! ⚡</label>
                <a id="download-link-btn" href="#" class="btn btn-save-device">📥 DOWNLOAD FILE TO YOUR DEVICE NOW</a>
            </div>
            <div class="credit-text">DEVELOPED BY <span style="color: #ff007f; text-shadow: 0 0 5px #ff007f; font-weight: bold;">JackZaa AI</span></div>
        </div>
    </div>
</div>
<script>
    let timer;
    let currentJobId = null;

    function startDownload() {
        const url = document.getElementById('url').value.trim();
        if (!url) {
            alert('SYSTEM ALARM: TARGET LINK PARAMETER CANNOT BE EMPTY.'); return;
        }
        const modeVal = document.querySelector('input[name="mode"]:checked').value;
        // แก้บั๊ก: ส่งค่า resolution เฉพาะตอนที่เลือกโหมด 1080p จริงเท่านั้น
        const mode = modeVal === '1080p' ? 'resolution' : modeVal;
        const resolution = modeVal === '1080p' ? '1080p' : '';

        document.getElementById('start-btn').disabled = true;
        document.getElementById('progress-area').classList.remove('d-none');
        document.getElementById('device-save-area').classList.add('d-none'); 

        fetch('/api/download', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ url: url, mode: mode, resolution: resolution })
        })
        .then(res => res.json())
        .then(data => {
            currentJobId = data.job_id;
            timer = setInterval(updateProgress, 1000);
        });
    }

    function updateProgress() {
        if (!currentJobId) return;
        fetch('/api/status?job_id=' + encodeURIComponent(currentJobId))
            .then(res => {
                if (!res.ok) { clearInterval(timer); return null; }
                return res.json();
            })
            .then(data => {
                if (!data) return;
                const bar = document.getElementById('bar-fill');
                bar.style.width = data.percent + '%'; bar.innerText = data.percent + '%';
                document.getElementById('status-msg').innerText = data.message;
                document.getElementById('speed-msg').innerText = data.speed;
                document.getElementById('size-msg').innerText = 'STREAM DATA SIZE: ' + data.size;

                if (data.status === 'success') {
                    clearInterval(timer);
                    document.getElementById('start-btn').disabled = false;
                    document.getElementById('download-link-btn').href = "/get_file/" + currentJobId + "/" + encodeBase64Th(data.last_file);
                    document.getElementById('device-save-area').classList.remove('d-none');
                    alert(data.message);
                } else if (data.status === 'failed') {
                    clearInterval(timer);
                    document.getElementById('start-btn').disabled = false;
                    alert(data.message);
                }
            });
    }

    function encodeBase64Th(str) {
        return btoa(encodeURIComponent(str).replace(/%([0-9A-F]{2})/g, function(match, p1) {
            return String.fromCharCode('0x' + p1);
        }));
    }
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/get_file/<job_id>/<base64_filename>')
def get_file(job_id, base64_filename):
    """ฟังก์ชันเปิดท่อสตรีมส่งไฟล์วิดีโอยิงข้ามเครือข่ายกลับไปเครื่องคนใช้งานโดยตรง
    ปลอดภัยขึ้น: ต้องมี job_id ที่ถูกต้อง และชื่อไฟล์ต้องตรงกับไฟล์ที่งานนั้นโหลดจริงเท่านั้น"""
    try:
        decrypted_bytes = base64.b64decode(base64_filename)
        filename = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', decrypted_bytes.decode('utf-8'))
        filename = os.path.basename(filename)

        # ตรวจสอบว่า job มีอยู่จริง สำเร็จแล้ว และชื่อไฟล์ตรงกับที่งานนั้นสร้างไว้
        with jobs_lock:
            job = jobs.get(job_id)
        if not job or job.get("status") != "success" or job.get("last_file") != filename:
            abort(403)

        # ยืนยันอีกชั้นว่า path สุดท้ายอยู่ในโฟลเดอร์ downloads จริง (กัน path traversal)
        safe_path = os.path.realpath(os.path.join(DOWNLOAD_FOLDER, filename))
        if not safe_path.startswith(os.path.realpath(DOWNLOAD_FOLDER) + os.sep):
            abort(403)

        return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)
    except Exception:
        return "เกิดข้อผิดพลาดในการดึงไฟล์กลับเครื่อง: ไม่พบไฟล์หรือไม่มีสิทธิ์เข้าถึง", 404


@app.route('/api/status', methods=['GET'])
def api_status():
    job_id = request.args.get('job_id', '')
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    # ส่งเฉพาะข้อมูลสถานะ ไม่ส่ง created_at
    return jsonify({k: v for k, v in job.items() if k != "created_at"})


@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.json or {}
    url = data.get('url')
    mode = data.get('mode', 'video')
    resolution = data.get('resolution', '1080p')

    if not url:
        return jsonify({"error": "url is required"}), 400

    # สร้างงานใหม่แยกอิสระต่อผู้ใช้แต่ละคน สถานะจะไม่ชนกัน
    job_id = create_job()
    t = threading.Thread(target=run_download, args=(url, mode, resolution, job_id))
    t.start()
    return jsonify({"result": "started", "job_id": job_id})


def open_browser():
    time.sleep(1.5)
    webbrowser.open('http://127.0.0.1:5000')


if __name__ == '__main__':
    print("\n" + "=" * 50)
    print("  JACK LOADER - YOUTUBE DOWNLOADER WEB PRO")
    print("  PROJECT DEVELOPED BY: JackZaa AI")
    print("=" * 50 + "\n")

    threading.Thread(target=open_browser, daemon=True).start()
    # ผูกเฉพาะ 127.0.0.1 เพื่อความปลอดภัย เครื่องอื่นในเครือข่ายเข้าถึงไม่ได้
    # หากต้องการเปิดให้เครื่องอื่นใน LAN ใช้ ให้เปลี่ยนกลับเป็น host='0.0.0.0'
    app.run()
