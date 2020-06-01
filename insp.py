#!/usr/bin/python3

import os
import sqlite3
files = [
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E06 - Clefairy and The Moon Stone DVD.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E06 - Clefairy and the Moon Stone DVD.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E43 - The March of The Exeggutor Squad HDTV-720p.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E43 - The March of the Exeggutor Squad HDTV-720p.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E50 - Who Gets To Keep Togepi DVD.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E50 - Who gets to keep Togepi DVD.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E60 - Beach Blank-Out Blastoise DVD.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E60 - Beach Blank-out Blastoise DVD.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E80 - A Friend In Deed DVD.mkv",
	"/media/london/TV/Pokémon/Season 1/Pokémon - S01E80 - A Friend in Deed DVD.mkv",
	"/media/london/TV/Pokémon/Season 2/Pokémon - S02E08 - In The Pink DVD.mkv",
	"/media/london/TV/Pokémon/Season 2/Pokémon - S02E08 - In the Pink DVD.mkv",
	"/media/london/TV/Pokémon/Season 2/Pokémon - S02E24 - Bound For Trouble DVD.mkv",
	"/media/london/TV/Pokémon/Season 2/Pokémon - S02E24 - Bound for Trouble DVD.mkv",
	"/media/london/TV/Pokémon/Season 2/Pokémon - S02E32 - Enter The Dragonite DVD.mkv",
	"/media/london/TV/Pokémon/Season 2/Pokémon - S02E32 - Enter the Dragonite DVD.mkv",
]

vic_db = sqlite3.connect("vic.bak", timeout=10, isolation_level=None)
db = vic_db.cursor()
db.execute("PRAGMA journal_mode = WAL")

rows_to_delete = []
db.execute("SELECT full_path FROM vic WHERE full_path LIKE \"%Pokémon/Season 1/%\"")
vid_data = db.fetchall()
for vid in vid_data:
	directory, filename = os.path.split(vid[0])
	print(filename + ": " + str(filename in os.listdir(directory)))

	if filename not in os.listdir(directory):
		rows_to_delete.append(vid[0])

print(rows_to_delete)

"""
for file in files:
	d, f = os.path.split(file)
	if f not in os.listdir(d):
		print(f)
"""
