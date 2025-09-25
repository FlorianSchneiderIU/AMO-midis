#!/usr/bin/env python3
"""
A *single-file* Flask app to run on a Raspberry Pi that lets you
1. **Upload MuseScore `mscz` files** at `http://<pi‑ip>:5000/upload` (password-protected)
2. **Rate** the converted tracks 1–10 at `http://<pi‑ip>:5000/rate` (open)

Quick start (on RasPi OS / Debian):

```bash
sudo apt install python3-venv  # if virtualenv not yet installed
python3 -m venv venv && source venv/bin/activate
pip install flask werkzeug

export UPLOAD_PASSWORD="mySecret"   # choose your own!
python app.py
```

Then browse to:
  • Upload page ……   http://<pi‑ip>:5000/upload  (password required)
  • Rating page ……   http://<pi‑ip>:5000/rate

All converted files are saved in `uploads/` as `.ogg` and `.musicxml`
All ratings are appended to `ratings.csv`             (CSV columns: timestamp,filename,score,ip,email,remark)

You can analyse the CSV later with pandas / Excel.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import random
import subprocess
from pathlib import Path

from flask import (Flask, render_template_string, request,
                   redirect, url_for, send_from_directory, abort)
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPLOAD_FOLDER = Path(__file__).parent / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"mscz"}
PASSWORD = os.environ.get("UPLOAD_PASSWORD", "changeme")
MUSESCORE_BIN = os.environ.get("MUSESCORE_BIN", "musescore")
RATINGS_CSV = Path(__file__).parent / "ratings.csv"
METADATA_CSV = Path(__file__).parent / "metadata.csv"
ARENA_MATCHES_CSV = Path(__file__).parent / "model_arena_matches.csv"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB max file size
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def save_rating(filename: str, score: int, ip: str, email: str, remark: str = "") -> None:
    new = not RATINGS_CSV.exists()
    with RATINGS_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(["timestamp", "filename", "score", "ip", "email", "remark"])
        writer.writerow([dt.datetime.utcnow().isoformat(), filename, score, ip, email, remark])


def save_metadata(filename: str, model_name: str = "", composer: str = "", piece_name: str = "", score_filename: str = "") -> None:
    """Save metadata for an uploaded file."""
    new = not METADATA_CSV.exists()
    with METADATA_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(["filename", "model_name", "composer", "piece_name", "score_filename", "upload_timestamp"])
        writer.writerow([filename, model_name, composer, piece_name, score_filename, dt.datetime.utcnow().isoformat()])


def get_file_metadata(filename: str) -> dict:
    """Get metadata for a specific file."""
    if not METADATA_CSV.exists():
        return {}
    
    with METADATA_CSV.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["filename"] == filename:
                return row
    return {}


def get_user_rated_tracks(email: str) -> set[str]:
    """Get the set of filenames that a user has already rated."""
    if not RATINGS_CSV.exists():
        return set()

    rated_tracks = set()
    with RATINGS_CSV.open("r", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # Skip header row
        if not header:
            return set()
        
        # Find the indices of the columns we need
        try:
            filename_idx = header.index("filename")
            email_idx = header.index("email") if "email" in header else None
        except ValueError:
            return set()
        
        for row in reader:
            # Skip rows that don't have enough columns
            if len(row) <= filename_idx:
                continue
                
            # If this row has an email column and it matches our user
            if email_idx is not None and len(row) > email_idx:
                row_email = row[email_idx].strip()
                if row_email.lower() == email.lower():
                    rated_tracks.add(row[filename_idx])
            # If there's no email column, skip this row (old format)

    return rated_tracks


def save_model_arena_match(
    *,
    email: str,
    piece_key: str,
    piece_label: str,
    track_a: str,
    track_b: str,
    model_a: str,
    model_b: str,
    chosen_label: str,
    chosen_track: str,
    chosen_model: str,
    feedback: str,
    ip: str,
) -> None:
    """Persist the result of a model arena comparison."""

    new = not ARENA_MATCHES_CSV.exists()
    with ARENA_MATCHES_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(
                [
                    "timestamp",
                    "email",
                    "piece_key",
                    "piece_label",
                    "track_a",
                    "track_b",
                    "model_a",
                    "model_b",
                    "winner_label",
                    "winner_track",
                    "winner_model",
                    "feedback",
                    "ip",
                ]
            )
        writer.writerow(
            [
                dt.datetime.utcnow().isoformat(),
                email,
                piece_key,
                piece_label,
                track_a,
                track_b,
                model_a,
                model_b,
                chosen_label,
                chosen_track,
                chosen_model,
                feedback,
                ip,
            ]
        )


def _derive_piece_identity(filename: str, metadata: dict) -> tuple[str, str, str, str]:
    """Return (key, display_label, piece_name, composer) for grouping tracks."""

    piece_name = (metadata.get("piece_name") or metadata.get("score_filename") or "").strip()
    composer = (metadata.get("composer") or "").strip()

    if piece_name and composer:
        key = f"composer::{composer}|piece::{piece_name}"
        display_label = f"{composer} — {piece_name}"
    elif piece_name:
        key = f"piece::{piece_name}"
        display_label = piece_name
    elif composer:
        key = f"composer_only::{composer}"
        display_label = composer
    else:
        stem = Path(filename).stem
        key = f"file::{stem}"
        display_label = stem
        piece_name = stem

    return key, display_label, piece_name, composer


def collect_piece_groups() -> dict[str, dict]:
    """Group available tracks by piece identity for the model arena."""

    groups: dict[str, dict] = {}
    for ogg_file in sorted(UPLOAD_FOLDER.glob("*.ogg")):
        filename = ogg_file.name
        metadata = get_file_metadata(filename)
        key, display_label, piece_name, composer = _derive_piece_identity(filename, metadata)
        model_name = (metadata.get("model_name") or "").strip()

        entry = {
            "filename": filename,
            "metadata": metadata,
            "model_name": model_name,
            "piece_name": piece_name,
            "composer": composer,
        }

        group = groups.setdefault(
            key,
            {
                "tracks": [],
                "display_label": display_label,
                "piece_name": piece_name,
                "composer": composer,
            },
        )

        group["tracks"].append(entry)

        # Keep the richest available label information.
        if composer and piece_name:
            group["display_label"] = f"{composer} — {piece_name}"
        elif piece_name and not group.get("display_label"):
            group["display_label"] = piece_name

        if composer and not group.get("composer"):
            group["composer"] = composer
        if piece_name and not group.get("piece_name"):
            group["piece_name"] = piece_name

    # Retain only groups with at least two tracks for comparison.
    return {key: data for key, data in groups.items() if len(data["tracks"]) >= 2}

# ---------------------------------------------------------------------------
# Routes — File upload (password protected)
# ---------------------------------------------------------------------------
UPLOAD_FORM_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AMO Music Upload</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        
        .container {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 25px 50px rgba(0, 0, 0, 0.15);
            max-width: 500px;
            width: 100%;
        }
        
        h2 {
            color: #2d3748;
            font-size: 2rem;
            margin-bottom: 30px;
            text-align: center;
            font-weight: 600;
        }
        
        .form-group {
            margin-bottom: 25px;
        }
        
        label {
            display: block;
            color: #4a5568;
            font-weight: 500;
            margin-bottom: 8px;
        }
          input[type="password"], input[type="file"], input[type="text"] {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e2e8f0;
            border-radius: 10px;
            font-size: 16px;
            transition: all 0.3s ease;
            background: white;
        }
        
        input[type="password"]:focus, input[type="file"]:focus, input[type="text"]:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        button {
            width: 100%;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 15px;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 25px rgba(102, 126, 234, 0.3);
        }
        
        .message {
            margin: 20px 0;
            padding: 12px 16px;
            border-radius: 8px;
            text-align: center;
            font-weight: 500;
        }
        
        .message.error {
            background: #fed7d7;
            color: #c53030;
            border: 1px solid #feb2b2;
        }
        
        .message.success {
            background: #c6f6d5;
            color: #2f855a;
            border: 1px solid #9ae6b4;
        }
        
        .nav-link {
            display: block;
            text-align: center;
            margin-top: 30px;
            color: #667eea;
            text-decoration: none;
            font-weight: 500;
            transition: color 0.3s ease;
        }
        
        .nav-link:hover {
            color: #764ba2;
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>🎵 Upload Scores</h2>
        <form method="post" enctype="multipart/form-data">            <div class="form-group">
                <label for="password">Password:</label>
                <input type="password" id="password" name="password" required>
            </div>
            <div class="form-group">
                <label for="composer">Composer (optional):</label>
                <input type="text" id="composer" name="composer" placeholder="e.g., Bach, Mozart, Chopin">
            </div>
            <div class="form-group">
                <label for="piece_name">Piece Name (optional):</label>
                <input type="text" id="piece_name" name="piece_name" placeholder="e.g., Moonlight Sonata, Canon in D">
            </div>
            <div class="form-group">
                <label for="model_name">Model Name (optional):</label>
                <input type="text" id="model_name" name="model_name" placeholder="e.g., GPT-4, Claude, etc.">
            </div>
            <div class="form-group">
                <label for="file">Choose MuseScore file:</label>
                <input type="file" id="file" name="file" accept=".mscz" required>
                </div>
            <button type="submit">Upload File</button>
        </form>
        {% if message %}
            <div class="message {% if 'Uploaded' in message %}success{% else %}error{% endif %}">
                {{ message }}
            </div>
        {% endif %}
        <a href="{{ url_for('rate') }}" class="nav-link">Go to rating page →</a>
    </div>
</body>
</html>
"""

@app.route("/upload", methods=["GET", "POST"])
def upload():
    message = ""
    if request.method == "POST":
        if request.form.get("password") != PASSWORD:
            message = "Incorrect password."
        else:
            file = request.files.get("file")
            if not file or not file.filename:
                message = "No score selected."
            elif not allowed_file(file.filename):
                message = "File type not allowed."
            else:
                filename = secure_filename(file.filename)
                mscz_path = UPLOAD_FOLDER / filename
                file.save(mscz_path)

                base = mscz_path.stem
                ogg_path = UPLOAD_FOLDER / f"{base}.ogg"
                xml_path = UPLOAD_FOLDER / f"{base}.musicxml"
                try:
                    subprocess.run([MUSESCORE_BIN, str(mscz_path), "-o", str(ogg_path)], check=True)
                    subprocess.run([MUSESCORE_BIN, str(mscz_path), "-o", str(xml_path)], check=True)
                except Exception as e:
                    message = f"Conversion failed: {e}"
                else:
                    model_name = request.form.get("model_name", "").strip()
                    composer = request.form.get("composer", "").strip()
                    piece_name = request.form.get("piece_name", "").strip()

                    if model_name or composer or piece_name:
                        save_metadata(ogg_path.name, model_name, composer, piece_name)

                    message = f"✔ Uploaded {filename}"
    return render_template_string(UPLOAD_FORM_HTML, message=message)

# ---------------------------------------------------------------------------
# Routes — Rating page (open)
# ---------------------------------------------------------------------------

RATING_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AMO Music Rating</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
          body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            padding-top: 120px; /* Add space for sticky filter bar */
        }
        
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 25px 50px rgba(0, 0, 0, 0.15);
        }
        
        h2 {
            color: #2d3748;
            font-size: 2.5rem;
            margin-bottom: 30px;
            text-align: center;
            font-weight: 600;
        }
        
        .email-section {
            background: white;
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 30px;
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.1);
        }
        
        .email-form {
            display: flex;
            gap: 15px;
            align-items: end;
        }
        
        .form-group {
            flex: 1;
        }
        
        label {
            display: block;
            color: #4a5568;
            font-weight: 500;
            margin-bottom: 8px;
        }
        
        input[type="email"] {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e2e8f0;
            border-radius: 10px;
            font-size: 16px;
            transition: all 0.3s ease;
            background: white;
        }
        
        input[type="email"]:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .email-btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 10px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            white-space: nowrap;
        }
        
        .email-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(102, 126, 234, 0.3);
        }
        
        .current-user {
            color: #2d3748;
            font-weight: 600;
            text-align: center;
            margin-bottom: 20px;
            padding: 12px;
            background: #e6fffa;
            border-radius: 8px;
            border-left: 4px solid #38b2ac;
        }

        .arena-invite {
            margin-top: 20px;
            background: white;
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 15px 35px rgba(102, 126, 234, 0.2);
            text-align: center;
        }

        .arena-invite h3 {
            color: #4c51bf;
            font-size: 1.4rem;
            margin-bottom: 8px;
        }

        .arena-invite p {
            color: #4a5568;
            margin-bottom: 16px;
        }

        .arena-button {
            display: inline-block;
            padding: 12px 28px;
            border-radius: 999px;
            background: linear-gradient(135deg, #805ad5 0%, #667eea 100%);
            color: white;
            text-decoration: none;
            font-weight: 600;
            letter-spacing: 0.5px;
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }

        .arena-button:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 30px rgba(128, 90, 213, 0.3);
        }
        
        .no-tracks {
            text-align: center;
            color: #718096;
            font-size: 1.2rem;
            margin: 40px 0;
            font-style: italic;
        }
          .track-card {
            background: white;
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 25px;
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.1);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
            position: relative;
        }
        
        .track-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.15);
        }
        
        .jump-btn {
            position: absolute;
            top: 15px;
            right: 15px;
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            color: white;
            border: none;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            cursor: pointer;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            box-shadow: 0 4px 15px rgba(245, 87, 108, 0.3);
        }
        
        .jump-btn:hover {
            transform: scale(1.1);
            box-shadow: 0 6px 20px rgba(245, 87, 108, 0.4);
        }
          .jump-btn:active {
            transform: scale(0.95);
        }
        
        .score-download-btn {
            background: linear-gradient(135deg, #4299e1 0%, #3182ce 100%);
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.3s ease;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 15px;
        }
        
        .score-download-btn:hover {
            background: linear-gradient(135deg, #3182ce 0%, #2c5282 100%);
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(66, 153, 225, 0.3);
        }
        
        .score-download-btn:active {
            transform: translateY(0);
        }
          .track-title {
            color: #2d3748;
            font-size: 1.4rem;
            font-weight: 600;
            margin-bottom: 15px;
            word-break: break-word;
        }
        
        .track-info {
            background: #f7fafc;
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 15px;
            border-left: 4px solid #667eea;
        }
        
        .track-info .model-name {
            color: #4a5568;
            font-size: 0.9rem;
            font-weight: 500;
        }
        
        .track-info .model-value {
            color: #667eea;
            font-weight: 600;
        }
        
        audio {
            width: 100%;
            margin-bottom: 20px;
            border-radius: 8px;
        }

        .score-btn {
            display: inline-block;
            margin: 10px 0;
            padding: 8px 16px;
            background: #667eea;
            color: white;
            border-radius: 6px;
            text-decoration: none;
            font-size: 0.9rem;
        }

        .score-btn:hover {
            background: #5a67d8;
        }
          .rating-form {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 15px;
        }
        
        .rating-section {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 15px;
            margin-bottom: 15px;
        }
        
        .remark-section {
            width: 100%;
            margin-top: 15px;
        }
        
        .remark-label {
            display: block;
            color: #4a5568;
            font-weight: 500;
            margin-bottom: 8px;
            font-size: 0.9rem;
        }
        
        .remark-textarea {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 14px;
            font-family: inherit;
            resize: vertical;
            min-height: 80px;
            transition: all 0.3s ease;
            background: white;
        }
        
        .remark-textarea:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .remark-textarea::placeholder {
            color: #a0aec0;
            font-style: italic;
        }
        
        .rating-options {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            flex: 1;
        }
        
        .rating-option {
            position: relative;
        }
        
        .rating-option input[type="radio"] {
            position: absolute;
            opacity: 0;
            cursor: pointer;
        }
        
        .rating-label {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.3s ease;
            font-weight: 600;
            color: #718096;
            background: white;
        }
        
        .rating-option input[type="radio"]:checked + .rating-label {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border-color: #667eea;
            transform: scale(1.1);
        }
        
        .rating-label:hover {
            border-color: #667eea;
            transform: scale(1.05);
        }
          .submit-btn {
            background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .submit-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(72, 187, 120, 0.3);
        }
        
        .submit-section {
            background: white;
            border-radius: 15px;
            padding: 30px;
            margin-top: 20px;
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.1);
            text-align: center;
        }
        
        .batch-submit-btn {
            background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
            color: white;
            border: none;
            padding: 18px 40px;
            border-radius: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-size: 18px;
            min-width: 250px;
        }
        
        .batch-submit-btn:hover {
            transform: translateY(-3px);
            box-shadow: 0 12px 30px rgba(72, 187, 120, 0.4);
        }
        
        .batch-submit-btn:disabled {
            background: #a0aec0;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
        
        .nav-link {
            display: inline-block;
            margin-top: 30px;
            color: #667eea;
            text-decoration: none;
            font-weight: 500;
            transition: color 0.3s ease;
            text-align: center;
            width: 100%;
        }
          .nav-link:hover {
            color: #764ba2;
        }
          .filter-section {
            background: rgba(255, 255, 255, 0.98);
            backdrop-filter: blur(15px);
            border-radius: 0 0 15px 15px;
            padding: 15px 25px;
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.15);
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 1000;
            border-bottom: 2px solid #e2e8f0;
        }
        
        .filter-title {
            color: #2d3748;
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 15px;
            text-align: center;
        }
        
        .filter-controls {
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            align-items: center;
            justify-content: center;
            margin-bottom: 15px;
        }
        
        .filter-group {
            display: flex;
            flex-direction: column;
            min-width: 150px;
        }
        
        .filter-label {
            color: #4a5568;
            font-weight: 500;
            margin-bottom: 5px;
            font-size: 0.8rem;
            text-align: center;
        }
        
        .filter-select {
            padding: 8px 10px;
            border: 2px solid #e2e8f0;
            border-radius: 6px;
            font-size: 13px;
            background: white;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        
        .filter-select:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 2px rgba(102, 126, 234, 0.1);
        }
        
        .filter-buttons {
            display: flex;
            gap: 8px;
            justify-content: center;
            flex-wrap: wrap;
        }
        
        .filter-btn {
            background: #667eea;
            color: white;
            border: none;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        
        .filter-btn:hover {
            background: #5a67d8;
            transform: translateY(-1px);
        }
        
        .clear-btn {
            background: #718096;
        }
        
        .clear-btn:hover {
            background: #4a5568;
        }
        
        .hidden {
            display: none !important;
        }
        
        .special-rating {
            position: relative;
        }
        
        .special-rating::after {
            content: attr(data-emoji);
            position: absolute;
            top: -5px;
            right: -5px;
            font-size: 0.8rem;
        }          @media (max-width: 768px) {
            body {
                padding-top: 160px; /* More space for mobile sticky filter */
            }
            
            .container {
                padding: 20px;
            }
            
            .filter-controls {
                flex-direction: column;
                gap: 10px;
            }
            
            .filter-group {
                min-width: auto;
                width: 100%;
                max-width: 200px;
            }
            
            .filter-buttons {
                width: 100%;
                justify-content: space-around;
            }
            
            .email-form {
                flex-direction: column;
                align-items: stretch;
            }
            
            .email-btn {
                margin-top: 10px;
            }
            
            .rating-options {
                justify-content: center;
            }
            
            .rating-form {
                flex-direction: column;
                align-items: stretch;
            }
            
            .submit-btn {
                width: 100%;
                margin-top: 10px;
            }
            
            .batch-submit-btn {
                width: 100%;
                min-width: auto;
            }
            
            .jump-btn {
                top: 10px;
                right: 10px;
                width: 35px;
                height: 35px;
                font-size: 16px;
            }
        }
    </style>
</head>
<body>
    <!-- Sticky Filter Bar -->
    {% if user_email and tracks %}
    <div class="filter-section">
        <h3 class="filter-title">🔍 Filter & Sort Tracks</h3>
        <div class="filter-controls">            <div class="filter-group">
                <label class="filter-label" for="composer-filter">Composer:</label>
                <select id="composer-filter" class="filter-select">
                    <option value="">All Composers</option>
                </select>
            </div>
            <div class="filter-group">
                <label class="filter-label" for="piece-filter">Piece:</label>
                <select id="piece-filter" class="filter-select">
                    <option value="">All Pieces</option>
                </select>
            </div>
            <div class="filter-group">
                <label class="filter-label" for="sort-by">Sort by:</label>
                <select id="sort-by" class="filter-select">
                    <option value="filename">Filename</option>
                    <option value="composer">Composer</option>
                    <option value="piece">Piece Name</option>
                </select>
            </div>
        </div>
        <div class="filter-buttons">
            <button type="button" class="filter-btn" onclick="applyFilters()">Apply</button>
            <button type="button" class="filter-btn clear-btn" onclick="clearFilters()">Clear</button>
        </div>
    </div>
    {% endif %}

    <div class="container">
        <h2>🎵 Rate the Tracks</h2>
        
        <div class="email-section">
            {% if not user_email %}
                <form method="get" class="email-form">
                    <div class="form-group">
                        <label for="email">Your Email Address:</label>
                        <input type="email" id="email" name="email" required placeholder="Enter your email to start rating">
                    </div>
                    <button type="submit" class="email-btn">Start Rating</button>
                </form>
            {% else %}
                <div class="current-user">
                    👤 Rating as: {{ user_email }}
                    <a href="{{ url_for('rate') }}" style="margin-left: 15px; color: #667eea; text-decoration: none;">Change User</a>
                </div>
                {% if error_message %}
                    <div style="background: #fed7d7; color: #c53030; border: 1px solid #feb2b2; padding: 12px; border-radius: 8px; margin-top: 15px; text-align: center;">
                        {{ error_message }}
                    </div>
                {% endif %}
            {% endif %}
        </div>

        {% if user_email %}
        <div class="arena-invite">
            <h3>⚔️ Try the Model Arena</h3>
            <p>Blind-test two model performances of the same song and tell us which one wins.</p>
            <a class="arena-button" href="{{ url_for('arena', email=user_email) }}">Enter Model Arena</a>
        </div>
        {% endif %}
          {% if user_email %}
            {% if not tracks %}
                <div class="no-tracks">
                    {% if total_tracks > 0 %}
                        🎉 Great job! You've rated all {{ total_tracks }} tracks. Thanks for your participation!
                    {% else %}
                        No scores uploaded yet. Upload some to get started!
                    {% endif %}
                </div>            {% else %}
                <form method="post" id="batchRatingForm">
                    <input type="hidden" name="email" value="{{ user_email }}">
                    <input type="hidden" name="batch_submit" value="true">
                      {% for t in tracks %}                    <div class="track-card" 
                         data-filename="{{ t }}" 
                         data-composer="{{ track_metadata[t].get('composer', '') }}" 
                         data-piece="{{ track_metadata[t].get('piece_name', '') }}">
                        <button type="button" class="jump-btn" onclick="jumpToSubmit()" title="Jump to submit button">⬇</button>
                        <h3 class="track-title">{{ t }}</h3>
                        {% if track_metadata[t] %}
                        <div class="track-info">
                            {% if track_metadata[t].get('composer') %}
                            <div><span class="model-name">Composer: </span><span class="model-value">{{ track_metadata[t]['composer'] }}</span></div>
                            {% endif %}                            {% if track_metadata[t].get('piece_name') %}
                            <div><span class="model-name">Piece: </span><span class="model-value">{{ track_metadata[t]['piece_name'] }}</span></div>
                            {% endif %}
                        </div>                        {% endif %}
                        <a href="{{ url_for('score', basename=t.rsplit('.', 1)[0]) }}"
                           class="score-download-btn"
                           target="_blank"
                           title="View score">
                            🎼 View Score
                        </a>
                        <audio controls preload="none" src="{{ url_for('uploaded_file', filename=t) }}"></audio><div class="rating-form">
                            <input type="hidden" name="filenames" value="{{ t }}">
                            <div class="rating-section">
                                <div class="rating-options">
                                    {% for i in range(1,11) %}                                    <div class="rating-option">
                                        <input type="radio" name="score_{{ t }}" value="{{ i }}" id="score_{{ t }}_{{ i }}">
                                        <label for="score_{{ t }}_{{ i }}" class="rating-label {% if i==1 %}special-rating{% elif i==10 %}special-rating{% endif %}" {% if i==1 %}data-emoji="🗑"{% elif i==10 %}data-emoji="⭐"{% endif %}>{{ i }}</label>
                                    </div>
                                    {% endfor %}
                                </div>
                            </div>
                            <div class="remark-section">
                                <label for="remark_{{ t }}" class="remark-label">Optional remark:</label>
                                <textarea name="remark_{{ t }}" id="remark_{{ t }}" class="remark-textarea" placeholder="Why did you rate it this way? What could be improved? (optional)"></textarea>
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                      <div class="submit-section">
                        <button type="submit" class="batch-submit-btn">Submit Ratings</button>
                        <p style="color: #718096; font-size: 0.9rem; margin-top: 10px;">Only tracks with ratings will be submitted</p>
                    </div>
                </form>            {% endif %}
        {% endif %}        
    </div>

    <script>
        // Populate filter options when page loads
        document.addEventListener('DOMContentLoaded', function() {
            populateFilterOptions();
        });        function populateFilterOptions() {
            const tracks = document.querySelectorAll('.track-card');
            const composers = new Set();
            const pieces = new Set();

            tracks.forEach(track => {
                const composer = track.getAttribute('data-composer');
                const piece = track.getAttribute('data-piece');

                if (composer) composers.add(composer);
                if (piece) pieces.add(piece);
            });

            // Populate composer filter
            const composerSelect = document.getElementById('composer-filter');
            composers.forEach(composer => {
                const option = document.createElement('option');
                option.value = composer;
                option.textContent = composer;
                composerSelect.appendChild(option);
            });

            // Populate piece filter
            const pieceSelect = document.getElementById('piece-filter');
            pieces.forEach(piece => {
                const option = document.createElement('option');
                option.value = piece;
                option.textContent = piece;
                pieceSelect.appendChild(option);
            });
        }        function applyFilters() {
            const composerFilter = document.getElementById('composer-filter').value;
            const pieceFilter = document.getElementById('piece-filter').value;
            const sortBy = document.getElementById('sort-by').value;

            const tracks = Array.from(document.querySelectorAll('.track-card'));

            // Filter tracks
            tracks.forEach(track => {
                const composer = track.getAttribute('data-composer') || '';
                const piece = track.getAttribute('data-piece') || '';

                const matchesComposer = !composerFilter || composer === composerFilter;
                const matchesPiece = !pieceFilter || piece === pieceFilter;

                if (matchesComposer && matchesPiece) {
                    track.classList.remove('hidden');
                } else {
                    track.classList.add('hidden');
                }
            });

            // Sort visible tracks
            const visibleTracks = tracks.filter(track => !track.classList.contains('hidden'));
            visibleTracks.sort((a, b) => {
                let aValue, bValue;
                
                switch(sortBy) {
                    case 'composer':
                        aValue = a.getAttribute('data-composer') || '';
                        bValue = b.getAttribute('data-composer') || '';
                        break;                    case 'piece':
                        aValue = a.getAttribute('data-piece') || '';
                        bValue = b.getAttribute('data-piece') || '';
                        break;
                    default:
                        aValue = a.getAttribute('data-filename') || '';
                        bValue = b.getAttribute('data-filename') || '';
                }

                return aValue.localeCompare(bValue);
            });

            // Reorder tracks in DOM
            const form = document.getElementById('batchRatingForm');
            const submitSection = document.querySelector('.submit-section');
            
            // Remove all track cards first
            tracks.forEach(track => track.remove());
            
            // Add back visible tracks in sorted order
            visibleTracks.forEach(track => {
                form.insertBefore(track, submitSection);
            });
            
            // Add hidden tracks at the end
            tracks.filter(track => track.classList.contains('hidden')).forEach(track => {
                form.insertBefore(track, submitSection);
            });
        }        function clearFilters() {
            document.getElementById('composer-filter').value = '';
            document.getElementById('piece-filter').value = '';
            document.getElementById('sort-by').value = 'filename';
            
            // Show all tracks
            document.querySelectorAll('.track-card').forEach(track => {
                track.classList.remove('hidden');
            });
            
            applyFilters(); // Apply default sorting
        }// Auto-update piece filter when composer changes
        const composerFilter = document.getElementById('composer-filter');
        if (composerFilter) {
            composerFilter.addEventListener('change', function() {
                const selectedComposer = this.value;
                const pieceSelect = document.getElementById('piece-filter');
                
                // Clear current piece options except "All Pieces"
                pieceSelect.innerHTML = '<option value="">All Pieces</option>';
                
                if (selectedComposer) {
                    const tracks = document.querySelectorAll('.track-card');
                    const pieces = new Set();
                    
                    tracks.forEach(track => {
                        const composer = track.getAttribute('data-composer');
                        const piece = track.getAttribute('data-piece');
                        
                        if (composer === selectedComposer && piece) {
                            pieces.add(piece);
                        }
                    });
                    
                    pieces.forEach(piece => {
                        const option = document.createElement('option');
                        option.value = piece;
                        option.textContent = piece;
                        pieceSelect.appendChild(option);
                    });
                } else {
                    // If no composer selected, show all pieces
                    populateFilterOptions();
                }
            });
        }

        // Jump to submit button function
        function jumpToSubmit() {
            const submitSection = document.querySelector('.submit-section');
            if (submitSection) {
                submitSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }
    </script>
</body>
</html>
"""


ARENA_PAGE_HTML = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>Model Arena</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #5a67d8 0%, #805ad5 100%);
            min-height: 100vh;
            padding: 30px;
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 24px;
            padding: 40px;
            box-shadow: 0 25px 50px rgba(0, 0, 0, 0.15);
        }

        h1 {
            font-size: 2.8rem;
            margin-bottom: 10px;
            text-align: center;
            color: #2d3748;
        }

        .subtitle {
            text-align: center;
            color: #4a5568;
            margin-bottom: 30px;
            font-size: 1.1rem;
        }

        .message {
            background: #e6fffa;
            color: #2c7a7b;
            border-left: 4px solid #38b2ac;
            padding: 15px 20px;
            border-radius: 12px;
            margin-bottom: 25px;
        }

        .email-card, .arena-card {
            background: white;
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 15px 40px rgba(0, 0, 0, 0.12);
        }

        .email-form {
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
        }

        .email-form label {
            width: 100%;
            color: #4a5568;
            font-weight: 500;
            margin-bottom: 8px;
        }

        .email-form input[type=\"email\"] {
            flex: 1;
            min-width: 260px;
            padding: 14px 18px;
            border-radius: 12px;
            border: 2px solid #e2e8f0;
            font-size: 1rem;
            transition: all 0.3s ease;
        }

        .email-form input[type=\"email\"]:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.2);
        }

        .email-form button {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 14px 26px;
            border-radius: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .email-form button:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 30px rgba(102, 126, 234, 0.25);
        }

        .arena-card {
            margin-top: 30px;
        }

        .audio-comparison {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 25px;
            margin-bottom: 30px;
        }

        .audio-panel {
            background: #f7fafc;
            border-radius: 16px;
            padding: 24px;
            display: flex;
            flex-direction: column;
            gap: 15px;
            border: 2px solid transparent;
        }

        .audio-panel h2 {
            font-size: 1.5rem;
            color: #2d3748;
            text-align: center;
        }

        .audio-panel audio {
            width: 100%;
            outline: none;
        }

        .choice-grid {
            display: grid;
            gap: 15px;
            margin-bottom: 25px;
        }

        .choice-option {
            border: 2px solid #cbd5f5;
            padding: 18px;
            border-radius: 14px;
            cursor: pointer;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .choice-option input[type=\"radio\"] {
            width: 20px;
            height: 20px;
        }

        .choice-option:hover {
            border-color: #667eea;
            box-shadow: 0 10px 25px rgba(102, 126, 234, 0.15);
        }

        .feedback-label {
            font-weight: 600;
            color: #4a5568;
            margin-bottom: 10px;
        }

        textarea {
            width: 100%;
            min-height: 120px;
            border-radius: 14px;
            border: 2px solid #e2e8f0;
            padding: 14px 16px;
            font-size: 1rem;
            resize: vertical;
            transition: border 0.3s ease, box-shadow 0.3s ease;
        }

        textarea:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.2);
        }

        .action-buttons {
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            justify-content: center;
            margin-top: 25px;
        }

        .action-buttons button {
            flex: 1;
            min-width: 200px;
            border: none;
            border-radius: 12px;
            padding: 16px;
            font-weight: 600;
            color: white;
            cursor: pointer;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }

        .action-buttons button.primary {
            background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
        }

        .action-buttons button.secondary {
            background: linear-gradient(135deg, #4299e1 0%, #3182ce 100%);
        }

        .action-buttons button:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 30px rgba(0, 0, 0, 0.15);
        }

        .nav-link {
            display: inline-block;
            margin-top: 30px;
            text-align: center;
            width: 100%;
            color: #667eea;
            text-decoration: none;
            font-weight: 500;
        }

        .nav-link:hover {
            color: #764ba2;
        }

        .no-pairs {
            text-align: center;
            color: #4a5568;
            font-size: 1.1rem;
        }

        @media (max-width: 600px) {
            body {
                padding: 20px 15px;
            }

            .container {
                padding: 28px 22px;
            }
        }
    </style>
</head>
<body>
    <div class=\"container\">
        <h1>Model Arena</h1>
        <p class=\"subtitle\">Blind A/B comparisons to find the stronger model for each song.</p>

        {% if message %}
        <div class=\"message\">{{ message }}</div>
        {% endif %}

        {% if not user_email %}
        <div class=\"email-card\">
            <form class=\"email-form\" method=\"get\">
                <label for=\"email\">Enter your email to join the arena:</label>
                <input type=\"email\" id=\"email\" name=\"email\" placeholder=\"you@example.com\" required>
                <button type=\"submit\">Start Testing</button>
            </form>
        </div>
        {% elif not has_pair %}
        <div class=\"arena-card\">
            <p class=\"no-pairs\">We need at least two arrangements of the same song to run a match. Upload more models to continue.</p>
        </div>
        {% else %}
        <div class=\"arena-card\">
            <div class=\"audio-comparison\">
                <div class=\"audio-panel\">
                    <h2>Model A</h2>
                    <audio controls preload=\"metadata\">
                        <source src=\"{{ audio_a }}\" type=\"audio/ogg\">
                        Your browser does not support the audio element.
                    </audio>
                </div>
                <div class=\"audio-panel\">
                    <h2>Model B</h2>
                    <audio controls preload=\"metadata\">
                        <source src=\"{{ audio_b }}\" type=\"audio/ogg\">
                        Your browser does not support the audio element.
                    </audio>
                </div>
            </div>

            <form method=\"post\">
                <input type=\"hidden\" name=\"email\" value=\"{{ user_email }}\">
                <input type=\"hidden\" name=\"piece_key\" value=\"{{ piece_key }}\">
                <input type=\"hidden\" name=\"piece_label\" value=\"{{ piece_label }}\">
                <input type=\"hidden\" name=\"track_a\" value=\"{{ track_a }}\">
                <input type=\"hidden\" name=\"track_b\" value=\"{{ track_b }}\">
                <input type=\"hidden\" name=\"model_a\" value=\"{{ model_a }}\">
                <input type=\"hidden\" name=\"model_b\" value=\"{{ model_b }}\">

                <div class=\"choice-grid\">
                    <label class=\"choice-option\">
                        <input type=\"radio\" name=\"winner\" value=\"A\" required>
                        <span>Model A sounds better</span>
                    </label>
                    <label class=\"choice-option\">
                        <input type=\"radio\" name=\"winner\" value=\"B\" required>
                        <span>Model B sounds better</span>
                    </label>
                </div>

                <p class=\"feedback-label\">Why did the winner feel better? Share any details that stood out.</p>
                <textarea name=\"feedback\" placeholder=\"Describe tone, dynamics, phrasing, mistakes…\"></textarea>

                <div class=\"action-buttons\">
                    <button type=\"submit\" name=\"next_action\" value=\"new\" class=\"primary\">Submit &amp; New Song</button>
                    <button type=\"submit\" name=\"next_action\" value=\"same\" class=\"secondary\">Submit &amp; Same Song</button>
                </div>
            </form>
        </div>
        {% endif %}

        <a class=\"nav-link\" href=\"{{ url_for('rate', email=user_email) }}\">Back to track ratings</a>
    </div>
</body>
</html>
"""

THANKS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Thank You - AMO Music</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        
        .container {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 25px 50px rgba(0, 0, 0, 0.15);
            text-align: center;
            max-width: 400px;
            width: 100%;
        }
        
        .success-icon {
            font-size: 4rem;
            margin-bottom: 20px;
        }
        
        h1 {
            color: #2d3748;
            font-size: 1.8rem;
            margin-bottom: 20px;
            font-weight: 600;
        }
        
        .nav-link {
            display: inline-block;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            text-decoration: none;
            padding: 12px 24px;
            border-radius: 10px;
            font-weight: 600;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .nav-link:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 25px rgba(102, 126, 234, 0.3);
        }
    </style>
</head>
<body>    <div class="container">
        <div class="success-icon">✨</div>
        <h1>Thanks for your rating!</h1>
        {% if submitted_count %}
            <p style="color: #4a5568; margin-bottom: 20px;">Successfully submitted {{ submitted_count }} rating{{ 's' if submitted_count != 1 else '' }}.</p>
        {% endif %}
        <a href="{{ url_for('rate', email=user_email) }}" class="nav-link">Rate More Tracks</a>
    </div>
</body>
</html>
"""

SCORE_PAGE_HTML = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <title>Score View</title>
    <script src=\"https://cdn.jsdelivr.net/npm/opensheetmusicdisplay@1.7.6/build/opensheetmusicdisplay.min.js\"></script>
</head>
<body>
    <div id=\"osmd-container\"></div>
    <audio id=\"player\" controls src=\"{{ url_for('uploaded_file', filename=base + '.ogg') }}\"></audio>
    <script>
        const osmd = new opensheetmusicdisplay.OpenSheetMusicDisplay("osmd-container", {followCursor: true});
        osmd.load("{{ url_for('uploaded_file', filename=base + '.musicxml') }}").then(() => {
            osmd.render();
            const cursor = osmd.cursor;
            cursor.show();
            const audio = document.getElementById('player');
            const timestamps = [];
            const it = cursor.Iterator;
            cursor.reset();
            timestamps.push(0);
            while (!it.EndReached) {
                it.moveToNext();
                timestamps.push(it.CurrentSourceTimestamp.RealValue);
            }
            const total = timestamps[timestamps.length - 1];
            let secs = [];
            audio.addEventListener('loadedmetadata', () => {
                const ratio = audio.duration / total;
                secs = timestamps.map(t => t * ratio);
            });
            let idx = 0;
            audio.addEventListener('timeupdate', () => {
                while (idx < secs.length - 1 && audio.currentTime >= secs[idx + 1]) {
                    cursor.next();
                    idx++;
                }
            });
            audio.addEventListener('ended', () => {
                cursor.reset();
                idx = 0;
            });
        });
    </script>
</body>
</html>
"""

@app.route("/rate", methods=["GET", "POST"])
def rate():
    if request.method == "POST":
        email = request.form.get("email")
        if not email:
            abort(400)
              # Check if this is a batch submission
        if request.form.get("batch_submit"):
            filenames = request.form.getlist("filenames")
            if not filenames:
                abort(400)
                
            # Process each track rating - only submit tracks that have ratings
            submitted_count = 0
            for filename in filenames:
                score = request.form.get(f"score_{filename}")
                remark = request.form.get(f"remark_{filename}", "").strip()
                if score:  # Only process if user provided a rating
                    try:
                        score_int = int(score)
                        assert 1 <= score_int <= 10
                        save_rating(filename, score_int, request.remote_addr or "-", email, remark)
                        submitted_count += 1
                    except (ValueError, AssertionError):
                        continue  # Skip invalid ratings
            
            # If no ratings were submitted, redirect back with message
            if submitted_count == 0:
                # Redirect back to rating page with error message
                return redirect(url_for("rate", email=email, error="no_ratings"))
            
            return render_template_string(THANKS_HTML, user_email=email, submitted_count=submitted_count)
        else:
            # Handle single rating (legacy support)
            filename = request.form.get("filename")
            score = request.form.get("score")
            remark = request.form.get("remark", "").strip()
            if not filename or not score:
                abort(400)
            try:
                score_int = int(score)
                assert 1 <= score_int <= 10
            except (ValueError, AssertionError):
                abort(400)
            save_rating(filename, score_int, request.remote_addr or "-", email, remark)
            return render_template_string(THANKS_HTML, user_email=email)    # Get user email from query parameter
    user_email = request.args.get("email")
    
    # Check for error messages
    error = request.args.get("error")
    error_message = ""
    if error == "no_ratings":
        error_message = "Please rate at least one track before submitting."
      # Get all tracks and filter by user's unrated tracks
    all_tracks = sorted(f.name for f in UPLOAD_FOLDER.glob("*.ogg"))
    
    if user_email:
        # Get tracks this user has already rated
        rated_tracks = get_user_rated_tracks(user_email)
        # Show only unrated tracks
        tracks = [track for track in all_tracks if track not in rated_tracks]
    else:
        tracks = []
    
    # Get metadata for all tracks and prepare for sorting/filtering
    track_metadata = {}
    all_composers = set()
    all_pieces = set()
    all_models = set()
    
    for track in tracks:
        metadata = get_file_metadata(track)
        track_metadata[track] = metadata
        
        # Collect unique values for filters
        if metadata.get('composer'):
            all_composers.add(metadata['composer'])
        if metadata.get('piece_name'):
            all_pieces.add(metadata['piece_name'])
        if metadata.get('model_name'):
            all_models.add(metadata['model_name'])

    # Apply any requested sorting/filtering from query parameters
    sort_by = request.args.get('sort', 'filename')
    composer_filter = request.args.get('composer', '')
    piece_filter = request.args.get('piece', '')
    model_filter = request.args.get('model', '')
    
    # Filter tracks based on query parameters
    if composer_filter or piece_filter or model_filter:
        filtered_tracks = []
        for track in tracks:
            metadata = track_metadata[track]
            composer_match = not composer_filter or metadata.get('composer', '') == composer_filter
            piece_match = not piece_filter or metadata.get('piece_name', '') == piece_filter
            model_match = not model_filter or metadata.get('model_name', '') == model_filter
            
            if composer_match and piece_match and model_match:
                filtered_tracks.append(track)
        tracks = filtered_tracks
    
    # Sort tracks based on sort parameter
    if sort_by == 'composer':
        tracks.sort(key=lambda t: track_metadata[t].get('composer', ''))
    elif sort_by == 'piece':
        tracks.sort(key=lambda t: track_metadata[t].get('piece_name', ''))
    elif sort_by == 'model':
        tracks.sort(key=lambda t: track_metadata[t].get('model_name', ''))
    else:  # default to filename
        tracks.sort()

    return render_template_string(
        RATING_PAGE_HTML,
        tracks=tracks,
        user_email=user_email,
        total_tracks=len(all_tracks),
        track_metadata=track_metadata,
        error_message=error_message,
        all_composers=sorted(all_composers),
        all_pieces=sorted(all_pieces),
        all_models=sorted(all_models)
    )


@app.route("/arena", methods=["GET", "POST"])
def arena():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email:
            abort(400)

        piece_key = request.form.get("piece_key", "")
        piece_label = request.form.get("piece_label", "")
        track_a = request.form.get("track_a")
        track_b = request.form.get("track_b")
        model_a = request.form.get("model_a", "")
        model_b = request.form.get("model_b", "")
        winner = request.form.get("winner")
        feedback = (request.form.get("feedback") or "").strip()
        next_action = request.form.get("next_action", "new")

        if winner not in {"A", "B"} or not track_a or not track_b:
            abort(400)

        chosen_track = track_a if winner == "A" else track_b
        chosen_model = model_a if winner == "A" else model_b

        save_model_arena_match(
            email=email,
            piece_key=piece_key,
            piece_label=piece_label,
            track_a=track_a,
            track_b=track_b,
            model_a=model_a,
            model_b=model_b,
            chosen_label=winner,
            chosen_track=chosen_track,
            chosen_model=chosen_model,
            feedback=feedback,
            ip=request.remote_addr or "-",
        )

        if next_action == "same" and piece_key:
            return redirect(url_for("arena", email=email, piece=piece_key, status="recorded"))

        return redirect(url_for("arena", email=email, status="recorded"))

    user_email = (request.args.get("email") or "").strip()
    status = request.args.get("status")
    message = ""
    if status == "recorded":
        message = "Match recorded! Ready for another comparison."

    piece_groups = collect_piece_groups()
    has_pair = bool(piece_groups)

    piece_key = request.args.get("piece") or ""
    selected_group = None

    if user_email and has_pair:
        if piece_key and piece_key in piece_groups:
            selected_group = piece_groups[piece_key]
        else:
            piece_key = ""

        if not selected_group:
            piece_key, selected_group = random.choice(list(piece_groups.items()))

        tracks = selected_group["tracks"]
        first, second = random.sample(tracks, 2)
        if random.choice([True, False]):
            track_a = first
            track_b = second
        else:
            track_a = second
            track_b = first

        context = {
            "user_email": user_email,
            "has_pair": True,
            "audio_a": url_for("uploaded_file", filename=track_a["filename"]),
            "audio_b": url_for("uploaded_file", filename=track_b["filename"]),
            "track_a": track_a["filename"],
            "track_b": track_b["filename"],
            "model_a": track_a.get("model_name", ""),
            "model_b": track_b.get("model_name", ""),
            "piece_key": piece_key,
            "piece_label": selected_group.get("display_label", ""),
            "message": message,
        }
    else:
        context = {
            "user_email": user_email,
            "has_pair": False,
            "message": message,
        }

    return render_template_string(ARENA_PAGE_HTML, **context)

# ---------------------------------------------------------------------------
# Static serving of uploaded files
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# Page to display score with OpenSheetMusicDisplay
@app.route("/score/<basename>")
def score(basename):
    return render_template_string(SCORE_PAGE_HTML, base=basename)
@app.route("/")
def index():
    return redirect(url_for("rate"))

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("★ Running local rating server — http://localhost:5000 (Ctrl+C to stop)")
    app.run(host="0.0.0.0", port=5000, debug=False)
