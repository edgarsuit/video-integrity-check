# Video Integrity Check
Batch video integrity file check using ffmpeg. Check all video files in a folder or set of folders for errors using ffmpeg and stores results in a sqlite database file.

## Usage
`vic_test.py [-h] [-d D] [-p P] [--force-hash-check] [--skip-db-clean] paths [paths ...]`

**Positional arguments:**

- `paths`               Path(s) to video files to be validated

**Optional arguments:**

- `-h`, `--help`          Show this help message and exit
- `-d D`                Database path and filename (default: "./vic.db")
- `-p P`                Number of worker processes to spawn (default: 1)
- `--force-hash-check`  Force hash calculation on all files
- `--skip-db-clean`     Skip the database clean operation

## Details
VIC uses ffmpeg to check a set of video files for encoding errors. The results are stored in a sqlite database. Video files are stored with file size, modification time, and hashed with SHA1 so the ffmpeg check can be skipped if a file match is found. If a non-matching file is found for a video file in the database (i.e., file content changed but file name is the same), the old DB row will be updated with updated hash and ffmpeg results. The database is also checked for non-existent files and hash collisions.

The database stores the following information on each video file:

 - Full path to the video file
 - SHA1 digest of the video file
 - The UNIX timestamp of when that digest was calculated
 - The file's modification time
 - The size (in bytes) of the file
 - A boolean value indicating if there is are any warnings in the file
 - A boolean value indicating if there is are any errors in the file
 - A boolean value indicating if there is are video errors in the file
 - A boolean value indicating if there is are audio errors in the file
 - A boolean value indicating if there is are container errors in the file
 - The full text output from the ffmpeg call
 - The number of files in the database with matching hashes
 - A list of files with matching hashes (seperated by a pipe | symbol)

VIC will ignore files with extensions that might commonly coexist with video files (.nfo, .txt, .srt, etc). Any invalid files not included in this list will be entered into the database with the error text "ffmpeg error". Database rows with this error text will be automatically removed from the database when VIC is re-run so it can attempt to process them again.

VIC is uses `concurrent.futures` to implement multi-CPU support. ffmpeg is multi-threaded, so one subprocess can kick of multiple threads and occupy a lot of CPU time (especially with high-resolution video files).

The ffmpeg call uses the following syntax:

`ffmpeg -v repeat+level+warning -i <video> -max_muxing_queue_size 4096 -f null -`

This "converts" the video using a null format and dumps the output to /dev/null. The max_muxing_queue_size is set to 4096 to support larger video files (4K+), otherwise ffmpeg will error out because the frame buffer won't be able to keep up.

## Copyright Information
Copyright 2020

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
