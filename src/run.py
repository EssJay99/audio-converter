#!/usr/bin/env python3
"""YouTube/SoundCloud to Lossless Audio Converter

Convert YouTube and SoundCloud links to high-quality lossless audio files (FLAC/ALAC).

Usage:
    python run.py          # Start the web server on port 5000

Dependencies:
    - Flask (Python web framework)
    - yt-dlp (audio downloader for YouTube and SoundCloud)
    - ffmpeg (audio codec conversion)

Install requirements:
    pip install -r requirements.txt

For macOS, install FFmpeg:
    brew install ffmpeg

System Requirements:
    - Python 3.8+ installed
    - FFmpeg must be available in your system PATH

"""

import os
import sys
import subprocess


def main():
    """Run the audio converter application."""
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
    except FileNotFoundError:
        print("Error: FFmpeg is not installed!")
        print("\nPlease install FFmpeg first:")
        print("  macOS:     brew install ffmpeg")
        print("  Windows:   Download from https://ffmpeg.org/")
        print("  Ubuntu:    sudo apt install ffmpeg")

        answer = input("\nWould you like to continue anyway? (y/N): ").strip().lower()
        if answer != 'y':
            sys.exit(1)

    from app import app

    port = int(os.environ.get('PORT', 5000))

    print("YouTube/SoundCloud to Lossless Audio Converter")
    print("=" * 50)
    print(f"Starting web server at http://localhost:{port}")
    print("=" * 50)

    with app.app_context():
        from app import _setup_db
        _setup_db()

    app.run(debug=True, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
