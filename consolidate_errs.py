#!/usr/bin/python3

db_path = "vic.db"

import re
import sqlite3

dts_re = re.compile(r"^.*\b(Application provided invalid, non monotonically increasing dts to muxer)\b.*$\n",re.MULTILINE)

vic_db = sqlite3.connect(db_path, timeout=10)
db = vic_db.cursor()
db.execute("PRAGMA journal_mode = OFF")
db.execute("PRAGMA foreign_keys = OFF")

db.execute("ALTER TABLE vic RENAME TO vic_old")

db.execute("""
	CREATE TABLE vic (
		full_path text,
		digest text,
		pass int,
		err_text text,
		collisions int,
		collision_videos text
	)
""")

db.execute("DROP INDEX idx_full_path")
db.execute("DROP INDEX idx_digest")
db.execute("DROP INDEX idx_pass")
db.execute("DROP INDEX idx_collisions")

db.execute("CREATE INDEX idx_full_path ON vic(full_path)")
db.execute("CREATE INDEX idx_digest ON vic(digest)")
db.execute("CREATE INDEX idx_pass ON vic(pass)")
db.execute("CREATE INDEX idx_collisions ON vic(collisions)")

db.execute("""
	SELECT
		full_path,
		digest,
		pass,
		err_text,
		collisions,
		collision_videos
	FROM
		vic_old
""")
videos = db.fetchall()

for video in videos:
	print(video[0])
	if not video[2]:
		ffmpeg_output = video[3]
		(ffmpeg_output, dts_err_ct) = dts_re.subn("",ffmpeg_output)
		if dts_err_ct > 0:
			ffmpeg_output += "Invalid, non monotonically increasing dts * " + str(dts_err_ct)
	else:
		ffmpeg_output = None

	db.execute("INSERT INTO vic VALUES(?,?,?,?,?,?)",(video[0],video[1],video[2],ffmpeg_output,video[4],video[5]))

db.execute("DROP TABLE vic_old")
db.execute("PRAGMA foreign_keys = ON")

vic_db.commit()
vic_db.close()