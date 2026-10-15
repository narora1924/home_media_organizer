# Home media organizer

A local organizer for photos and videos. It finds duplicate files, skips anything already in the output library, converts supported media to JPG or H.264/AAC MP4, and copies the results into year and month folders.

The wizard stops for confirmation before deleting extras and again before writing the output folder.

## What it converts

Images become `.jpg`: `.heic`, `.jpeg`, `.jpg`, `.png`, `.gif`, `.webp`, `.bmp`, `.tiff`, `.tif`.

Videos become H.264/AAC `.mp4`: `.mov`, `.m4v`, `.avi`, `.mts`, `.mpg`, `.mpeg`, `.mp4`, `.mkv`, `.wmv`, `.3gp`, `.flv`, `.webm`.

A JPEG is copied without re-encoding. An MP4 that is already H.264 and AAC is copied as-is. Another container that is already H.264 and AAC is remuxed into MP4. Everything else in the video list is transcoded with ffmpeg (`libx264`, AAC, CRF 18, `yuv420p`). Other file types are skipped and listed.

The folder date comes from metadata first: image `DateTimeOriginal`, then `CreateDate`, then `DateTimeDigitized`, then `DateTime`. Videos use the container creation time. If none of those exist, the file creation time is used. Output looks like `2024/06/IMG_1234.jpg`.

## Setup

Install Python 3.11 or newer, then install ffmpeg and ffprobe and make sure both are on your Windows `PATH`.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

A current ffmpeg build from [https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/) works. After unzipping it, add the `bin` folder to `PATH`, then open a new terminal and check:

```powershell
ffmpeg -version
ffprobe -version
```

`pillow-heif` is the fallback when ffmpeg cannot read a HEIC photo.

## Run

From this folder:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

1. Choose the input and output folders. They must be separate.
2. Review duplicate groups. Keep one file. Ignore or delete the extras. Deletes happen only after the confirmation list.
3. Review input files whose contents are already in the output folder, then continue.
4. Review the files that will be written, including the count, then start the copy.

The output folder gets a `.media_organizer_cache.sqlite` file so later runs can skip unchanged files and files this app has already converted.

Conversion goes through `app/convert.py`. A later remote converter can replace those ffmpeg calls without changing the wizard.
