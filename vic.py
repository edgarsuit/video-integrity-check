#!/usr/bin/python3
#
# Video Integrity Check
# Batch video integrity file check using ffmpeg. Check all video files in a folder or set of folders for errors using
# ffmpeg and stores results in a sqlite database file.
#
# Usage
# vic_test.py [-h] [-d D] [-p P] [--force-hash-check] [--skip-db-clean] paths [paths ...]
#
# Positional arguments:
# paths               Path(s) to video files to be validated
#
# Optional arguments:
# -h, --help          Show this help message and exit
# -d D                Database path and filename (default: "./vic.db")
# -p P                Number of worker processes to spawn (default: 1)
# --force-hash-check  Force hash calculation on all files
# --skip-db-clean     Skip the database clean operation
#
# Details
# VIC uses ffmpeg to check a set of video files for encoding errors. The results are stored in a sqlite database. Video
# files are stored with file size, modification time, and hashed with SHA1 so the ffmpeg check can be skipped if a file
# match is found. If a non-matching file is found for a video file in the database (i.e., file content changed but file
# name is the same), the old DB row will be updated with updated hash and ffmpeg results. The database is also checked
# for non-existent files and hash collisions.
#
# The database stores the following information on each video file:
#
# - Full path to the video file
# - SHA1 digest of the video file
# - The UNIX timestamp of when that digest was calculated
# - The file's modification time
# - The size (in bytes) of the file
# - A boolean value indicating if there is are any warnings in the file
# - A boolean value indicating if there is are any errors in the file
# - A boolean value indicating if there is are video errors in the file
# - A boolean value indicating if there is are audio errors in the file
# - A boolean value indicating if there is are container errors in the file
# - The full text output from the ffmpeg call
# - The number of files in the database with matching hashes
# - A list of files with matching hashes (seperated by a pipe | symbol)
#
# VIC will ignore files with extensions that might commonly coexist with video files (.nfo, .txt, .srt, etc). Any
# invalid files not included in this list will be entered into the database with the error text "ffmpeg error".
# Database rows with this error text will be automatically removed from the database when VIC is re-run so it can
# attempt to process them again.
#
# vic.py is uses concurrent.futures to implement multi-CPU support. ffmpeg is multi-threaded, so one subprocess can
# kick of multiple threads and occupy a lot of CPU time (especially with high-resolution video files).
#
# The ffmpeg call uses the following syntax:
#
# ffmpeg -v repeat+level+warning -i <video> -max_muxing_queue_size 4096 -f null -
#
# This "converts" the video using a null format and dumps the output to /dev/null. The max_muxing_queue_size is set to
# 4096 to support larger video files (4K+), otherwise ffmpeg will error out because the frame buffer won't be able to
# keep up.
#
# Copyright 2020
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the
# following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following
#    disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the
#    following disclaimer in the documentation and/or other materials provided with the distribution
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
HELPFUL SQL QUERIES

SELECT * FROM vic WHERE err_text LIKE '%File ended prematurely%' ORDER BY full_path ASC

SELECT * FROM vic WHERE err_text LIKE '%Error while decoding stream #0:0%' ORDER BY full_path ASC

SELECT * FROM vic WHERE err_text LIKE '%[h264 @ %] [error] error while decoding MB %' ORDER BY full_path ASC

SELECT full_path, collisions, collision_videos FROM vic WHERE collisions != 0 ORDER BY full_path ASC

SELECT * FROM vic WHERE full_path LIKE '/media/delsca/%' AND collisions = 0 ORDER BY full_path ASC

SELECT full_path,err_text FROM vic
	WHERE err_text LIKE "%[warning] %: corrupt decoded frame in stream %"
	ORDER BY full_path ASC

"""

import os
import subprocess
import shlex
import time
import signal
import sys
import re
import hashlib
import sqlite3
import argparse
import concurrent.futures
import psutil
from shutil import copyfile

# Track total execution time
overall_start = time.perf_counter()

# File extension blacklist
EXT_BLACKLIST = ["txt","ifo","bup","py","jpg","nfo","sub","sh","srt"]

# Global variables
vid_list = []
vic_db = None
db = None
force_hash_check = None
stop_hash = False
executor = None

# Manual debug flag, outputs reason hash wasn't skipped if enabled
debug = True

# Take a start and a stop time from time.perf_counter and convert them to a string that describes elapsed time as
# ##d ##h ##m ##.##s. When we add a new time field (i.e., advance from 59m to 1h 0m), we pad all the smaller units with
# leading zeros so the output aligns nicely. For example, we would have '8m 34.27s' but when we add an hour to that, we
# would have '1h 08m 34.27s' so the full string length stays consistent as the minutes and seconds tick up. Seconds are
# set to always use two decimal places for the same reason.
def conv_time(start,stop):
	[d,h,m,s] = [0,0,0,0]
	m, s = divmod(stop - start, 60)
	h, m = divmod(m, 60)
	d, h = divmod(h, 24)
	# Format seconds output with two decimal places so it aligns with row above
	if d != 0:
		t = str(round(d)) + "d " + "{:02}".format(round(h)) + "h " + "{:02}".format(round(m)) + "m " \
			+ "{:05.2f}".format(round(s,2)) + "s"
	elif h != 0:
		t = str(round(h)) + "h " + "{:02}".format(round(m)) + "m " + "{:05.2f}".format(round(s,2)) + "s"
	elif m != 0:
		t = str(round(m)) + "m " + "{:05.2f}".format(round(s,2)) + "s"
	else:
		t = "{:.2f}".format(round(s,2)) + "s"
	return t

# Return the SHA1 hash of a file. Using SHA1 since it's faster than RSA and SHA2
def get_sha1(video):
	# stop_hash flag is used to break the hash computation loop below in the event of a SIGTERM or SIGINT
	global stop_hash
	# Compute hash on 1MiB blocks
	blocksize = 65536
	sha1 = hashlib.sha1()
	# Open the file as a byte stream and read 65536 bytes at a time, update the hash with those bytes
	with open(video,"rb") as video_file:
		buf = None
		while buf != b'' and not stop_hash:
			buf = video_file.read(blocksize)
			sha1.update(buf)

	# Return the hexidecimal digest if we didn't bail early
	if stop_hash:
		return "Hash computation stopped early"
	else:
		return sha1.hexdigest()

# Kill all ffmpeg and vic.py processes on sigint and sigterm
def kill_vic(signum, frame):
	# Print status
	print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Subprocess terminating")

	# Raise the stop_hash flag to break the SHA1 hash computation loop(s)
	global stop_hash
	global executor
	global db
	stop_hash = True
	db.execute("PRAGMA wal_checkpoint;")

	# Start by shutting down the concurrent.futures process pool (otherwise new subprocesses will be started as soon
	# as the current ones are killed).
	try:
		executor.shutdown(wait=False)
	except:
		pass

	# Send SIGKILL to any child processes (SIGTERM doesn't kill them for some reason)
	pid = psutil.Process(os.getpid())
	try:
		children = pid.children(recursive=True)
		for process in children:
			process.send_signal(signal.SIGKILL)
	except:
		pass

	# ffmpeg uses some strange line ending syntax and often leaves the shell broken; run stty sane to fix it
	subprocess.run(shlex.split("stty sane"))
	sys.exit(0)

# To write DB entries, DB must be locked for a few milliseconds. That locking happens automatically on the db.execute
# call for "INSERT" and "DELETE" operations. Since this is multi-threaded program, the DB can occasionally be locked by
# another process when we attempt to read from it or write to it. If that happens, we sleep for 1 second and try the
# operation again. up to 20 times This is used anywhere a db operation occurs in parallel.
def execute_sql(cursor,q):
	global vic_db
	db_ok = False
	max_tries = 20
	tries = 0
	while not db_ok and tries < max_tries:
		try:
			# DB "INSERT" calls pass a tuple and need to be scattered into cursor.execute(). All other calls can be
			# passed as a string value
			if isinstance(q,tuple):
				cursor.execute(*q)
			else:
				cursor.execute(q)
			db_ok = True
		except:
			tries+=1
			time.sleep(1)
	return cursor

# Find files with the same digest but different file names (hash collisions). The DB rows with these collisions are
# updated with the number of collisions and a list of the colliding files.
def find_collisions(db):
	# Clear all collision data first (it all gets recalculated anyway)
	db.execute("UPDATE vic SET collisions = 0, collision_videos = \"\" WHERE collisions != 0;")

	# Find rows with duplicate hash values, group full_path value by concatinating with the | symbol. Update each row
	db.execute("SELECT GROUP_CONCAT(full_path,\"|\"), digest, COUNT(*) c FROM vic GROUP BY digest HAVING c > 1;")
	videos = db.fetchall()
	len(videos)
	for vid in videos:
		full_list_of_collisions = vid[0].split("|")
		for collision in full_list_of_collisions:
			list_of_collisions = "|".join(full_list_of_collisions)
			list_of_collisions = list_of_collisions.replace(collision,"").replace("||","|").strip("|")
			num_collisions = str(len(full_list_of_collisions)-1)
			db_ok = False
			db.execute("UPDATE vic SET collisions = " + num_collisions + ", collision_videos = \"" + list_of_collisions
				+ "\" WHERE full_path = \"" + collision + "\" AND digest = \"" + vid[1] + "\";")
	return db

# Check video for encoding errors with ffmpeg. This also computes the SHA1 hash of each video file to store in DB for
# future reference. All output from ffmpeg is stored in database file along with the file's full path, its SHA1 hash,
# if it passed the ffmpeg test, and the output from that test
def check_vid(video):
	# Use global versions of vid_list, vic_db, db, and skip_hash_check values
	global vid_list
	global vic_db
	global db
	global args

	# Make a local-only copy of the database cursor object
	my_db = db

	# Keep track of what number video we're on and the video file name to print status output
	tot_file_count = len(vid_list)
	file_count = vid_list.index(video) + 1
	vid_name = video.split("/")[-1]
	mod_time = str(os.path.getmtime(video))
	file_size = os.path.getsize(video)

	# Fetch all rows with matching path, store the path name and digest for later comparisons
	q = "SELECT full_path, digest, digest_time, mod_time, file_size FROM vic WHERE full_path = \"" + video + "\";"
	my_db = execute_sql(my_db,q)
	vid_data = my_db.fetchall()

	# We run the hash if and only if:
	#	1) There is exactly 1 video in the DB with a matching file name
	#	2) The modified time of the file on disk matches what we have for it in the DB
	#	3) The file size on disk matches what we have in the DB
	#	4) It was hashed after its modification time (mod_time < DB's digest_time)
	# If all of those are true, we can skip the hash. If any of them are false, we re-run the hash. If the hash matches,
	# we correct the values in the database. With the debug flag, we output which of these 4 checks fail.
	run_hash = True
	if len(vid_data) == 1 \
			and round(float(mod_time),2) == round(float(vid_data[0][3]),2) \
			and file_size == vid_data[0][4] \
			and float(mod_time) < float(vid_data[0][2]):
		digest = vid_data[0][1]
		digest_time = vid_data[0][2]
		run_hash = False
	elif len(vid_data) != 0 and debug:
		try:
			if len(vid_data) != 1:
				print(" > " + vid_name + " Hashing, len(vid_data) > 1")
			if round(float(mod_time),2) != round(float(vid_data[0][3]),2):
				print(" > " + vid_name + " Hashing, mod_time != vid_data[0][3]: " + str(round(float(mod_time),2)) + " != "
					+ str(round(float(vid_data[0][3]),2)))
			if file_size != vid_data[0][4]:
				print(" > " + vid_name + " Hashing, file_size != vid_data[0][4]: " + str(file_size) + " != "
					+ str(vid_data[0][4]))
			if float(mod_time) >= float(vid_data[0][2]):
				print(" > " + vid_name + " Hashing, mod_time >= vid_data[0][2]: " + mod_time + " >= " + vid_data[0][2])
		except:
			pass

	# If force_hash_check flag is raised, we over-write whatever we determined above and run the hash anyway
	if force_hash_check: run_hash = True

	if run_hash:
		# Hash the video file with get_sha1() and print status
		start = time.perf_counter()
		digest_time = None
		print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
			+ str(tot_file_count) + " ] " + vid_name + " hashing... ")
		try:
			# Run the hash function and store the UNIX time that the hash completed
			digest = get_sha1(video)
			digest_time = str(time.time())
		except:
			digest = "Error"
		stop = time.perf_counter()
		t = conv_time(start,stop)
		print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
			+ str(tot_file_count) + " ] " + vid_name + " hashed in " + t + ", checking...")
	elif (file_count % 100) == 0 or file_count == tot_file_count:
		print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
			+ str(tot_file_count) + " ] Files found in DB, skipping hash")

	# Check if the video file is in the DB with a different hash (i.e., video file has been updated). Check the stored
	# SHA1 digest against the SHA1 digest we just computed. If they don't match (hash has changed), add the file to a
	# list and delete that row below, then rerun ffmpeg check. If the hashes do match, the metadata for the video is
	# out-of-date and will be updated. If stop_hash was raised (from passing SIGINT or SIGTERM), hash will bail early
	# with garbage result. If that's the case, don't touch the database.
	deleted_rows, updated_rows = 0, 0
	for vid in vid_data:
		if not stop_hash and vid[1] != digest:
			q = "DELETE FROM vic WHERE digest = \"" + vid[1] + "\";"
			my_db = execute_sql(my_db,q)
			deleted_rows += 1
		elif not stop_hash and ( \
									digest_time != vid[2] \
									or round(float(mod_time),2) != round(float(vid[3]),2) \
									or file_size != vid[4] \
								):
			q = "UPDATE vic SET mod_time = " + mod_time + ", file_size = " + str(file_size) + ", digest_time = " \
				+ digest_time + " WHERE full_path = \"" + video + "\" AND digest = \"" + digest + "\";"
			my_db = execute_sql(my_db,q)
			updated_rows += 1

	# Report on deleted DB row(s)
	if deleted_rows >= 1 or updated_rows >= 1:
		print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
			+ str(tot_file_count) + " ] " + vid_name + " deleted " + str(deleted_rows) + ", updated "
			+ str(updated_rows) + " DB row(s)")

	# If hash matches an entry in DB, then we've already done the ffmpeg test on that file and we can skip it. If not,
	# we need to run ffmpeg and add results of run to database. Even if the digest we computed matches what we have
	# in the DB for that video, we run this to check for hash collisions (exact same file with different file names).
	q = "SELECT * FROM vic WHERE digest = \"" + digest + "\" AND full_path = \"" + video + "\";"
	my_db = execute_sql(my_db,q)
	vid_data = my_db.fetchall()

	if vid_data == []:
		# Run 'ffmpeg -v error -i <video> -f null -' to convert to a null format and report any errors.
		# Use '-max_muxing_queue_size 4096' to prevent buffer underruns.
		# Outputs with '-v error' flag go to stderr, so they're redirected to stdout with 2>&1.
		# Output is utf-8 encoded, so needs to be decoded to be usable.
		# If no errors found, output will be an empty string.
		start = time.perf_counter()
		cmd_raw = "ffmpeg -v repeat+level+warning -i " + shlex.quote(video) + " -max_muxing_queue_size 4096 -f null -"
		cmd = shlex.split(cmd_raw)
		try:
			ffmpeg_output = subprocess.run(cmd,stderr=subprocess.PIPE,check=True).stderr.decode("utf-8")
		except:
			ffmpeg_output = "ffmpeg error"

		# If ffmpeg_output has text [error], then it had errors during the ffmpeg test
		err_re = re.compile(r"(\[error\])")
		err = 1 if err_re.search(ffmpeg_output) is not None else 0

		# If ffmpeg_output has text [warning], then it had errors during the ffmpeg test
		warn_re = re.compile(r"(\[warning\])")
		warn = 1 if warn_re.search(ffmpeg_output) is not None else 0


		# If there is an ffmpeg error, check if it's a non monotonically increasing dts error and clean up output if it
		# is. This output can be repeated 10,000+ times on some videos.
		if err:
			# Regex statement to detect errors
			dts_re = re.compile(
				r"^.*\b(Application provided invalid, non monotonically increasing dts to muxer)\b.*$\n",re.MULTILINE)
			(ffmpeg_output, dts_err_ct) = dts_re.subn("",ffmpeg_output)
			if dts_err_ct > 0:
				ffmpeg_output += "Invalid, non monotonically increasing dts * " + str(dts_err_ct)

		# Regex statements to detect error types
		err_video_re = re.compile(r"(\[h264 @ 0x.+\])|(\[hevc @ 0x.+\])|(\[mpeg4 @ 0x.+\])|(\[msmpeg4 @ 0x.+\])"
			+ r"|(\[wmv2 @ 0x.+\])")
		err_audio_re = re.compile(r"(\[mp3float @ 0x.+\])|(\[aac @ 0x.+\])|(\[ac3 @ 0x.+\])|(\[truehd @ 0x.+\])"
			+ r"|(\[flac @ 0x.+\])|(\[dca @ 0x.+\])|(\[mp2 @ 0x.+\])|(\[eac3 @ 0x.+\])|(Invalid, non monotonically"
			+ r" increasing dts)")
		err_container_re = re.compile(r"(\[matroska,webm @ 0x.+\])")

		# Detect labels for error types in ffmpeg message
		err_video = 1 if err_video_re.search(ffmpeg_output) is not None else 0
		err_audio = 1 if err_audio_re.search(ffmpeg_output) is not None else 0
		err_container = 1 if err_container_re.search(ffmpeg_output) is not None else 0

		# Write all the test data to the database, including full video file path, the SHA1 digest, the time that the
		# digest was calculated, the file modification time, the file size, a boolean value that indicates if the test
		#  passed, boolean values indicating if we found errors in the video track, audio track(s), or the container,
		#  the output of the ffmpeg run (which will only contain text if we encountered a coding error, otherwise it
		# will be an empty string), as well as the number of hash collisions detected and colliding files.
		q = ("INSERT INTO vic VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?);", (video,digest,digest_time,mod_time,file_size,warn
			,err,err_video,err_audio,err_container,ffmpeg_output,0,""))
		my_db = execute_sql(my_db,q)

		# Output status of check
		stop = time.perf_counter()
		t = conv_time(start,stop)
		print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
			+ str(tot_file_count) + " ] " + vid_name + " checked in " + t)

	else:
		# If video is found in database after a hash computation, we can skip it (if hash was skipped, we said so above)
		if run_hash and updated_rows == 0:
			print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
				+ str(tot_file_count) + " ] " + vid_name + " found in DB")

def vic(paths,db_path="vic.db",procs=1,force_hash=False,skip_db_clean=False):
	# Use global versions of vid_list, vic_db, db, and skip_hash_check values
	global vid_list
	global vic_db
	global db
	global force_hash_check
	global executor

	# skip_hash_check is the global version of this flag, skip_hash is the local version passed from the argument
	force_hash_check = force_hash

	# Walk through all the passed paths and create a list of all the files (with full path)
	start = time.perf_counter()
	print("          > Gathering videos... ",end="\r",flush=True)
	for path in paths:
		for root, dirs, files in os.walk(path):
			for file in files:
				ext = file.split(".")[-1].lower()
				if ext not in EXT_BLACKLIST:
					vid_list.append(os.path.join(root,file))
					print("          > Gathering videos... " + str(len(vid_list)) + " found",end="\r",flush=True)
	stop = time.perf_counter()
	t = conv_time(start,stop)
	tot_file_count = len(vid_list)
	print()
	print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Found " + str(tot_file_count) + " videos in " + t)

	# Create the database in present working dir if it doesn't exist
	if not os.path.isfile(db_path):
		print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Creating database... ")
		open(db_path, 'w').close()
		vic_db = sqlite3.connect(db_path, timeout=10, isolation_level=None)
		db = vic_db.cursor()
		db.execute("PRAGMA journal_mode = WAL;")
		db.execute("""
			CREATE TABLE vic (
				full_path text,
				digest text,
				digest_time text,
				mod_time text,
				file_size int,
				warn int,
				err int,
				err_video int,
				err_audio int,
				err_container int,
				err_text text,
				collisions int,
				collision_videos text
			);
		""")
		db.execute("CREATE INDEX idx_full_path ON vic(full_path);")
		db.execute("CREATE INDEX idx_digest ON vic(digest);")
		db.execute("CREATE INDEX idx_warn ON vic(warn);")
		db.execute("CREATE INDEX idx_err ON vic(err);")
		db.execute("CREATE INDEX idx_err_video ON vic(err_video);")
		db.execute("CREATE INDEX idx_err_audio ON vic(err_audio);")
		db.execute("CREATE INDEX idx_err_container ON vic(err_container);")
		db.execute("CREATE INDEX idx_collisions ON vic(collisions);")
	else:
		# If DB does exist already, connect to it, disable journaling...
		vic_db = sqlite3.connect(db_path, timeout=10, isolation_level=None)
		db = vic_db.cursor()
		db.execute("PRAGMA journal_mode = WAL;")

		if not skip_db_clean:
			# ...and check DB for stale entries (files that no longer exist on disk) by fetching all full_path entries
			# from the DB table and checking each one with os.path.isfile(). If it's not a file, add it to a list.
			print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Cleaning database... ")
			start = time.perf_counter()
			rows_to_delete = []
			db.execute("SELECT full_path FROM vic")
			vid_data = db.fetchall()
			directory = ""
			for vid in vid_data:
				old_dir = directory
				directory, filename = os.path.split(vid[0])
				if directory != old_dir:
					try:
						dir_list = os.listdir(directory)
					except FileNotFoundError:
						dir_list = []
				if filename not in dir_list:
					rows_to_delete.append(vid[0])

			# Go through each entry in that list and remove it from the database table.
			for row in rows_to_delete:
				db.execute("DELETE FROM vic WHERE full_path = \"" + row + "\";")

			stop = time.perf_counter()
			t = conv_time(start,stop)
			print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Cleaned " + str(len(rows_to_delete))
				+ " rows in " + t)

		# Delete rows with errors so they get reprocessed
		db.execute("DELETE FROM vic WHERE err_text = \"ffmpeg error\" OR digest = \"Error\";")

		# Check for hash collisions before run
		print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Checking for hash collisions")
		db = find_collisions(db)

	# All videos can be checked in parallel using concurrent.futures.
	# Spawn some number of workers as determined by -t flag from arguments.
	try:
		executor = concurrent.futures.ProcessPoolExecutor(max_workers=procs)
		for video in zip(vid_list,executor.map(check_vid,vid_list)):
			pass
		executor.shutdown(wait=True)
	except OSError:
		pass

	# Check for hash collisions after run
	print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Checking for hash collisions")
	db = find_collisions(db)

	# Checkpoint and close DB
	db.execute("PRAGMA wal_checkpoint")
	vic_db.close()
	# Make a working copy of the database (not strictly necessary, but if you have the database open and run the script,
	# none of the database changes will be properly comitted).
	copyfile(db_path,db_path.replace("vic.db","vic_work.db"))
	# Reset terminal
	subprocess.run(shlex.split("stty sane"))

	# Output final status and execution time
	overall_stop = time.perf_counter()
	t = conv_time(overall_start,overall_stop)
	print("  > Finished in " + t)

if __name__ == '__main__':
	# SIGINT and SIGTERM handlers
	signal.signal(signal.SIGTERM,kill_vic)
	signal.signal(signal.SIGINT,kill_vic)

	# CLI arguments
	parser = argparse.ArgumentParser(
		description="Check all video files in a folder or set of folders for errors using ffmpeg and stores results"
		+ " in a sqlite3 database file.")
	parser.add_argument("-d", default="vic.db",help="Database path and filename (default: ./vic.db)")
	parser.add_argument("-p", type=int, default=1,help="Number of worker processes to spawn (default: 1)")
	parser.add_argument("--force-hash-check", action="store_true",help="Force hash calculation on all files")
	parser.add_argument("--skip-db-clean", action="store_true",help="Skip the database clean operation")
	parser.add_argument("paths", nargs="+",help="Path(s) to video files to be validated")
	args = parser.parse_args()

	# Run main function with passed arguments
	vic(args.paths,args.d,args.p,args.force_hash_check,args.skip_db_clean)
