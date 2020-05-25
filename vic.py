#!/usr/bin/python3
#
# vic.py
#
# usage: vic_test.py [-h] [-p P] [--skip-hash-check] paths [paths ...]
#
# Check all video files in a folder or set of folders for errors using ffmpeg and stores results in a sqlite3
# database file.
#
# Positional arguments:
#  paths              Path(s) to video files to be validated
#
# Optional arguments:
#  -h, --help         show this help message and exit
#  -p P               Number of worker processes to spawn (default: 1)
#  --skip-hash-check  Skip hash check, assume all hashes match
#
# Details:
# Uses ffmpeg to check a set of video files for encoding errors. The results are stored in a sqlite3 database in the
# script directory. Video files are hashed with SHA1 so the ffmpeg check can be skipped if a hash match is found. If a
# non-matching hash is found for a video file in the database (i.e., file content changed but file name is the same),
# old DB row will be removed and replaced with updated hash and ffmpeg results. The database is also checked for
# non-existent files before the ffmpeg checks start. The database stores the full path of the video file, the
# SHA1 digest, a boolean value indicating if the ffmpeg test passed, and the output of the ffmpeg run (which will be
# empty if the run passed, otherwise it will contain the error output).
#
# vic.py is uses concurrent.futures to implement multi-CPU support. ffmpeg is multi-threaded, so one subprocess can
# kick of multiple threads and occupy a lot of CPU time (especially with high-resolution video files).
#
# The ffmpeg call uses the following syntax:
#
# 	ffmpeg -v error -i <video> -max_muxing_queue_size 4096 -f null -
#
# This "converts" the video using a null format and dumps the output to /dev/null. The max_muxing_queue_size is set to
# 4096 to support larger video files (4K+), otherwise ffmpeg will error out because the frame buffer won't be able to
# keep up.
#
# Copyright 2020 Jason Rose <jason@jro.io>
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
SQL QUERIES

-- PRIORITY 1
SELECT * FROM vic WHERE err_text LIKE '%File ended prematurely%' ORDER BY full_path ASC

SELECT * FROM vic WHERE err_text LIKE '%Invalid data found when processing input%' ORDER BY full_path ASC

-- PRIORITY 2
SELECT * FROM vic
WHERE pass = 0
	AND err_text NOT LIKE '%File ended prematurely%'
	AND err_text LIKE '%error while decoding MB %'
ORDER BY full_path ASC

## PRIORITY 3

## PRIORITY 4

## PRIORITY 5

SELECT GROUP_CONCAT(full_path,"|") as f, digest, COUNT(*) c FROM vic GROUP BY digest HAVING c > 1
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

# Track total execution time
overall_start = time.perf_counter()

# Global variables
vid_list = []
vic_db = None
db = None
force_hash_check = None
stop_hash = False
debug = False
executor = None

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
	stop_hash = True

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
			print()
			time.sleep(1)
			print("sleeping")
	return cursor

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
	mod_time = os.path.getmtime(video)
	file_size = os.path.getsize(video)

	# Fetch all rows with matching path, store the path name and digest for later comparisons
	q = "SELECT full_path, digest, digest_time, mod_time, file_size FROM vic WHERE full_path = \"" + video + "\""
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
	if len(vid_data) == 1 and mod_time == vid_data[0][3] and file_size == vid_data[0][4] and mod_time < vid_data[0][2]:
		digest = vid_data[0][1]
		digest_time = vid_data[0][2]
		run_hash = False
	elif len(vid_data) != 0 and debug:
		if len(vid_data) != 1:
			print(" > " + vid_name + " Hashing, len(vid_data) > 1")
		if mod_time != vid_data[0][3]:
			print(" > " + vid_name + " Hashing, mod_time != vid_data[0][3]: " + str(mod_time) + " != "
				+ str(vid_data[0][3]))
		if file_size != vid_data[0][4]:
			print(" > " + vid_name + " Hashing, file_size != vid_data[0][4]: " + str(file_size) + " != "
				+ str(vid_data[0][4]))
		if mod_time >= vid_data[0][2]:
			print(" > " + vid_name + " Hashing, mod_time >= vid_data[0][2]: " + str(mod_time) + " >= "
				+ str(vid_data[0][2]))

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
			digest_time = time.time()
		except:
			digest = "Error"
		stop = time.perf_counter()
		t = conv_time(start,stop)
		print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
			+ str(tot_file_count) + " ] " + vid_name + " hashed in " + t + ", checking...")
	elif (file_count % 100) == 0:
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
			q = "DELETE FROM vic WHERE digest = \"" + vid[1] + "\""
			my_db = execute_sql(my_db,q)
			deleted_rows += 1
		elif not stop_hash and (digest_time != vid[2] or mod_time != vid[3] or file_size != vid[4]):
			q = "UPDATE vic SET mod_time = " + str(mod_time) + ", file_size = " + str(file_size) + ", digest_time = " \
				+ str(digest_time) + " WHERE full_path = \"" + video + "\" AND digest = \"" + digest + "\""
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
	#q = "SELECT full_path, digest, collision_videos FROM vic WHERE digest = \"" + digest + "\""
	q = "SELECT * FROM vic WHERE digest = \"" + digest + "\" AND full_path = \"" + video + "\""
	my_db = execute_sql(my_db,q)
	vid_data = my_db.fetchall()

	"""
	# Check if hash is in the DB with a different video file name
	rows_with_collisions = []
	video_in_db = False
	for vid in vid_data:
		if vid[0] != video and vid[1] == digest:
			# Save the name of the video and the list of collision videos for that row for later comparison
			rows_with_collisions.append([vid[0],vid[11]])
		if vid[0] == video and vid[1] == digest:
			video_in_db = True

	# Save the number of collisions found. If it's not zero, we need to do collision processing
	collisions = len(rows_with_collisions)
	new_collisions = 0
	if collisions != 0:
		# First, we create a space-separated list of all the file names with hash collisions
		list_of_rows_with_collisions = " ".join([file_name[0] for file_name in rows_with_collisions])

		# Insert the video we're currently checking into the database as well. First, check if this file is in the
		# database. If it isn't, some other file with a matching hash is. We can assume these are the same files and
		# thus the ffmpeg output for these files will be the same, so we can add it to the database with that ffmpeg
		# output. The list of file names should exclude itself and be cleaned of double spaces, trailing spaces, and
		# leading spaces.
		if not video_in_db:
			collision_videos = list_of_rows_with_collisions.replace(video,"").replace("  "," ").strip()
			new_vid_data = list(vid_data[0])
			new_vid_data[0] = video
			new_vid_data[2] = digest_time
			new_vid_data[10] = collisions
			new_vid_data[11] = collision_videos
			q = ("INSERT INTO vic VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",tuple(new_vid_data))
			my_db = execute_sql(my_db,q)

		for row in rows_with_collisions:
			# As above, for each video, the list of file names should exclude itself and be cleaned of double spaces,
			# trailing spaces, and leading spaces
			collision_videos = list_of_rows_with_collisions.replace(row[0],"").replace("  "," ").strip()

			# If we have detected this collision before, the text we pulled from the database will match the string
			# we just generated. If we haven't, we need to re-write that row with the updated collision entries.
			if collision_videos != row[1]:
				new_collisions += 1
				q = "UPDATE vic SET collisions = " + str(collisions) + ", collision_videos = \"" + collision_videos \
					+ "\" WHERE full_path = \"" + row[0] + "\""
				my_db = execute_sql(my_db,q)

		# Report on collisions
		new_collisions -= 1
		if new_collisions == 1:
			print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
				+ str(tot_file_count) + " ] " + vid_name + " - " + str(new_collisions) + " new hash collision found")
		elif new_collisions >= 1:
			print("  @ " + conv_time(overall_start,time.perf_counter()) + " > [ " + str(file_count) + " / "
				+ str(tot_file_count) + " ] " + vid_name + " - " + str(new_collisions) + " new hash collisions found")
	else:
		# If no collisions were detected, we will store 0, "" in the database for collisions, collision_videos
		collision_videos = ""
	"""

	if vid_data == []:
		# Run 'ffmpeg -v error -i <video> -f null -' to convert to a null format and report any errors.
		# Use '-max_muxing_queue_size 4096' to prevent buffer underruns.
		# Outputs with '-v error' flag go to stderr, so they're redirected to stdout with 2>&1.
		# Output is utf-8 encoded, so needs to be decoded to be usable.
		# If no errors found, output will be an empty string.
		start = time.perf_counter()
		cmd = shlex.split("ffmpeg -v error -i " + shlex.quote(video) + " -max_muxing_queue_size 4096 -f null -")
		try:
			ffmpeg_output = subprocess.run(
				cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True).stderr.decode("utf-8")
		except:
			ffmpeg_output = "PYTHON ERROR"

		# If ffmpeg_output is not empty, we got some coding error and the video did not pass the test
		err = 1 if ffmpeg_output != "" else 0

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
		err_video_re = re.compile(r"(\[h264 @ 0x.+\])|(\[hevc @ 0x.+\])|(\[mpeg4 @ 0x.+\])")
		err_audio_re = re.compile(r"(\[mp3float @ 0x.+\])|(\[aac @ 0x.+\])|(\[ac3 @ 0x.+\])|"
			+ r"(\[truehd @ 0x.+\])|(\[flac @ 0x.+\])")
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
		q = ("INSERT INTO vic VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (video,digest,digest_time,mod_time,file_size,err
			,err_video,err_audio,err_container,ffmpeg_output,collisions,collision_videos))
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
		db.execute("")
		db.execute("""
			PRAGMA journal_mode = WAL;
			CREATE TABLE vic (
				full_path text,
				digest text,
				digest_time real,
				mod_time real,
				file_size int,
				err int,
				err_video int,
				err_audio int,
				err_container int,
				err_text text,
				collisions int,
				collision_videos text
			);
			CREATE INDEX idx_full_path ON vic(full_path);
			CREATE INDEX idx_digest ON vic(digest);
			CREATE INDEX idx_err ON vic(err);
			CREATE INDEX idx_err_video ON vic(err_video);
			CREATE INDEX idx_err_audio ON vic(err_audio);
			CREATE INDEX idx_err_container ON vic(err_container);
			CREATE INDEX idx_collisions ON vic(collisions);
		""")
	else:
		# If DB does exist already, connect to it, disable journaling...
		vic_db = sqlite3.connect(db_path, timeout=10, isolation_level=None)
		db = vic_db.cursor()
		db.execute("PRAGMA journal_mode = WAL")

		if not skip_db_clean:
			# ...and check DB for stale entries (files that no longer exist on disk) by fetching all full_path entries
			# from the DB table and checking each one with os.path.isfile(). If it's not a file, add it to a list.
			print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Cleaning database... ")
			start = time.perf_counter()
			rows_to_delete = []
			db.execute("SELECT full_path FROM vic")
			vid_data = db.fetchall()
			for vid in vid_data:
				if not os.path.isfile(vid[0]):
					rows_to_delete.append(vid[0])

			# Go through each entry in that list and remove it from the database table.
			for row in rows_to_delete:
				db.execute("DELETE FROM vic WHERE full_path = \"" + row + "\"")
				vic_db.commit()

			stop = time.perf_counter()
			t = conv_time(start,stop)
			print("  @ " + conv_time(overall_start,time.perf_counter()) + " > Cleaned " + str(len(rows_to_delete))
				+ " rows in " + t)

		# Delete rows with errors so they get reprocessed
		db.execute("DELETE FROM vic WHERE err_text = \"PYTHON ERROR\" OR digest = \"Error\"")
		vic_db.commit()

	# All videos can be checked in parallel using concurrent.futures.
	# Spawn some number of workers as determined by -t flag from arguments.
	try:
		executor = concurrent.futures.ProcessPoolExecutor(max_workers=procs)
		for video in zip(vid_list,executor.map(check_vid,vid_list)):
			pass
		executor.shutdown(wait=True)
	except OSError:
		pass

	# Output final status and execution time
	overall_stop = time.perf_counter()
	t = conv_time(overall_start,overall_stop)
	print("  > Finished in " + t)
	vic_db.close()
	subprocess.run(shlex.split("stty sane"))

if __name__ == '__main__':
	# SIGINT and SIGTERM handlers
	signal.signal(signal.SIGTERM,kill_vic)
	signal.signal(signal.SIGINT,kill_vic)

	# -p number of worker processes to spawn
	# --skip-hash-check will bypass the hash computation and assume on-file hash in DB is accurate if filename matches
	# paths is a list of paths to be checked for video files
	parser = argparse.ArgumentParser(
		description="Check all video files in a folder or set of folders for errors using ffmpeg and stores results"
		+ " in a sqlite3 database file.")
	parser.add_argument('-d', default="vic.db",help="Database path and filename")
	parser.add_argument('-p', type=int, default=1,help="Number of worker processes to spawn (default: 1)")
	parser.add_argument('--force-hash-check', action="store_true",help="Force hash calculation on all files")
	parser.add_argument('--skip-db-clean', action="store_true",help="Skip the database clean operation")
	parser.add_argument('paths', nargs="+",help="Path(s) to video files to be validated")
	args = parser.parse_args()

	# Run main function with passed arguments
	vic(args.paths,args.d,args.p,args.force_hash_check,args.skip_db_clean)
