# mini-vinyl

A tiny NFC-triggered record player. Each vinyl has an NTAG213/215/216 tag
glued in, written (via a phone NFC-writer app, e.g. "NFC Tools") with a
single URI record holding a YouTube URL. Tapping it to the PN532 reader
plays that video's audio out to a paired Bluetooth speaker. Lifting the
vinyl stops playback. There's no on-Pi tag-to-song mapping file - the Pi
just reads whatever URL is written on the tag.

Every song downloaded to the Pi is saved as `<song_title>-<artist>.wav`
(e.g. `the_scientist-coldplay.wav`) and recorded in a `library.json`
catalog alongside the audio files, with each entry's title, artist, and
source YouTube link.

There's also a phone-facing web UI for adding songs without manually
copying URLs: search YouTube from your phone's browser, tap Add, and the
Pi starts downloading it while handing back a short code (e.g.
`the_scientist-coldplay` - literally the filename above, minus `.wav`).
Place a blank tag on the Pi's own reader and it burns that code straight
onto the tag - no separate NFC-writer app needed for these. See
[Adding songs from your phone](#adding-songs-from-your-phone) below. The
same web UI is also the only way to build a **playlist** tag - a
hand-picked, shuffle-played set of songs you've already downloaded; see
[Building playlists](#building-playlists).

## Hardware

- Raspberry Pi Zero W
- PN532 NFC reader module, wired over **I2C**:

  | PN532 | Pi Zero W        |
  |-------|------------------|
  | VCC   | 3V3 (pin 1)      |
  | GND   | GND (pin 6)      |
  | SDA   | GPIO2 / SDA (pin 3) |
  | SCL   | GPIO3 / SCL (pin 5) |

  Set the PN532's DIP switches/jumpers to I2C mode (check your board's
  silkscreen/manual).

- A Bluetooth speaker, paired ahead of time.

## Software setup (on the Pi)

> **Audio stack note:** Raspberry Pi OS Bookworm/Trixie use **PipeWire +
> WirePlumber** for audio by default, including Bluetooth A2DP. This
> project relies on that - it does **not** use `bluealsa`. If you
> installed `bluealsa`/`bluez-alsa-utils` already, disable it so it
> doesn't fight PipeWire over the Bluetooth audio profile:
>
> ```bash
> sudo systemctl disable --now bluealsa.service bluealsa-aplay.service
> ```

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip i2c-tools bluez bluez-tools \
  mpv ffmpeg pipewire pipewire-alsa wireplumber

sudo raspi-config   # Interface Options -> I2C -> enable, then reboot
i2cdetect -y 1        # confirm the PN532 shows up (usually addr 0x24)

git clone <this-repo> ~/mini-vinyl
cd ~/mini-vinyl
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# yt-dlp is used by mpv to resolve YouTube audio streams
pip install -U yt-dlp
```

### Bluetooth pairing

```bash
bluetoothctl
> power on
> agent on
> scan on
# note your speaker's MAC once it shows up, then:
> scan off
> pair AA:BB:CC:DD:EE:FF
> trust AA:BB:CC:DD:EE:FF
> connect AA:BB:CC:DD:EE:FF
> quit
```

Confirm PipeWire/WirePlumber picked it up as an audio sink:

```bash
wpctl status   # look for the speaker under "Sinks"
```

If it doesn't show up as the *default* sink, set it explicitly (grab the
ID from `wpctl status`):

```bash
wpctl set-default <sink-id>
```

### Config

```bash
cp config/secrets.example.env config/secrets.env
```

Fill in `config/secrets.env` (Bluetooth MAC, etc).

### Writing tags manually

If you'd rather not use the web UI for single songs, use a phone
NFC-writer app (e.g. "NFC Tools" on iOS/Android) to write a single
**URL/URI record** - the full YouTube video URL, e.g.
`https://www.youtube.com/watch?v=...` - to each NTAG213/215/216 tag.
Playlist tags can only be created through the web UI (see
[Building playlists](#building-playlists)); a YouTube playlist *URL*
written to a tag this way is not specially handled.

Verify a tag was written correctly:

```bash
python -m mini_vinyl.main --scan
```

Hold the tag to the reader - it should print the UID and the decoded URI.
If `URI: None`, the tag has no NDEF record (or isn't an NTAG21x tag).

### Adding songs from your phone

Once `mini-vinyl.service` is running (see below), a small search-and-add
web UI is available on port 8080 to anyone on the same Wi-Fi as the Pi -
no separate setup needed, and no account/login. Raspberry Pi OS
advertises the Pi's hostname over mDNS out of the box, so from a phone on
the same network, open:

```
http://<hostname>.local:8080
```

(`<hostname>` is whatever you set in `sudo raspi-config` -> System
Options -> Hostname, e.g. `mini-vinyl.local`. Run `hostname` on the Pi if
you're not sure what it's currently set to.)

Search for a song and tap **Add** - it's queued instantly, so you can
search and add a whole stack of songs back to back (or across several
searches) without waiting on any of them. The Pi works through the queue
one song at a time in the background - fetching its info, then
downloading it - entirely on its own, so it's fine to close the page or
turn your phone off once everything you want is queued; it'll all still
be there, further along, whenever you check back.

The **Your library** section on the same page lists everything you've
added and its current status (queued / fetching info / downloading /
ready), refreshing every few seconds. Once a song reaches **Ready**, tap
**Write tag** next to it and place a **blank** NTAG213/215/216 tag on the
Pi's own reader; it writes that song's code onto the tag directly
(refusing if the tag already has data on it, so it won't overwrite an
existing vinyl by accident). Tap that tag to the reader any time
afterward to play the song - no further pairing needed, the code is all
it takes. If a song shows **Failed**, hit **Retry** - it picks back up
without re-fetching info it already has.

Only single videos can be added this way - there's no way to add a
YouTube playlist through this flow; build a playlist from
already-downloaded songs instead (see [Building
playlists](#building-playlists)). No auth, no HTTPS - this is meant for a
trusted home network only, not for exposing beyond it. Fetching a song's
info (needed before it can even be queued for download) can itself take
the better part of a minute per song on this hardware, so a big batch add
is genuinely a "queue it and come back later" operation, not an
instant one.

### Building playlists

You can build your own playlists out of songs you've already downloaded -
this is the only way to make a playlist tag; a YouTube playlist URL
written directly to a tag is not specially handled. From the home
screen, **Create Playlist** takes you to a list of your playlists with a
name field above it to start a new one. Opening a playlist shows its
current songs (each with a **Remove** button) and a search box that
filters your already-downloaded library by title/artist - tap **Add** on
a match to drop it in. There's no reordering, since playback always
shuffles.

Once it has at least one song, **Write playlist to tag** works exactly
like writing a single song's tag (same "place a blank tag on the reader"
flow, same overwrite-refusal safety), just for the whole playlist at
once. Tapping that tag later shuffle-plays straight through every song in
it, back to back, re-shuffling on every fresh placement.

### Run it

```bash
python -m mini_vinyl.main
```

### Run on boot

Both services run as **user** systemd units, not system ones -
`mini-vinyl.service` needs to reach your PipeWire audio session, and
`bt-autoconnect.service` needs to run *after* PipeWire/WirePlumber have
registered the A2DP audio profile with BlueZ (a system-level unit races
ahead of that and fails to connect).

```bash
mkdir -p ~/.config/systemd/user
cp systemd/mini-vinyl.service systemd/bt-autoconnect.service \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bt-autoconnect.service
systemctl --user enable --now mini-vinyl.service

# let user services start at boot even before you log in
sudo loginctl enable-linger "$USER"
```

Edit `WorkingDirectory=`/`ExecStart=` in `systemd/mini-vinyl.service` and
the speaker MAC in `systemd/bt-autoconnect.service` if your install path
or speaker address differ from the placeholders.

## How it works

- `mini_vinyl/nfc_reader.py` polls the PN532 for a tag UID, then reads the
  NDEF URI record directly off the tag (`mini_vinyl/ndef.py` does the
  parsing); it can also write one back (`write_ndef_uri`), used by the web
  UI's tag-writing flow.
- `mini_vinyl/main.py` wraps every tag's content into a `youtube`-type
  `TagEntry` and tells the `PlayerManager` to start playback; when the tag
  is lifted (a few consecutive empty polls, to ignore momentary read
  misses) it stops playback. It also starts the web UI (`mini_vinyl/web.py`)
  in a background thread, and - on every poll where a tag is present -
  checks whether the web UI has armed a pending tag write
  (`mini_vinyl/tag_writer.py`) before falling through to normal
  read-and-play handling.
- `mini_vinyl/library.py`'s `Library` owns the song catalog
  (`library.json`) and all downloading: looking up a cached file by URL or
  by code, and running the actual `yt-dlp` downloads (for a
  tapped-and-cached URL-tag or the web UI's Add flow) in the background.
  The web UI's Add flow (`enqueue()`) just records a url and returns
  immediately; a single persistent background worker thread drains that
  queue one song at a time - fetching its info, claiming a unique
  `<song_title>-<artist>.wav` filename/code, then downloading - since
  running more than one `yt-dlp`/`ffmpeg` process at once wouldn't
  actually go any faster on a Pi Zero W's single weak core, just contend
  for it. `Library` is shared between `YoutubePlayer` and `web.py` so
  both read and write the same in-memory catalog with no cross-process
  locking needed.
- `mini_vinyl/players/youtube_player.py` shells out to `mpv` (which uses
  `yt-dlp` under the hood) and plays audio out through PipeWire, which
  owns the Bluetooth speaker's A2DP sink. A tag's content is a raw
  YouTube video URL, a bare library "code", or a local playlist code
  (`playlist:<slug>`, see below); resolving a YouTube URL live is slow on
  Zero W hardware, so an uncached URL-tag streams live via `mpv` first,
  and if it's still playing after a few seconds, a background download
  starts too - later plays of that tag (or of a code-tag, once its
  download has finished) find the cached file and start instantly via
  `pw-play`, with no live resolution involved. A playlist code has no
  live/caching phase at all - it's always shuffle-played by resolving its
  songs to on-disk paths and feeding them through the same queue
  mechanism, one track at a time, since every song in it is already a
  "ready" download by construction.
- `mini_vinyl/playlists.py`'s `PlaylistStore` owns locally-built
  playlists (`playlists.json`, alongside `library.json`) - just an
  ordered list of song codes per playlist, resolved to on-disk paths
  through `Library` at playback time. Writing a playlist to a tag reuses
  the exact same write flow as a song (`mini_vinyl/tag_writer.py`
  doesn't care what the code string is), just with the `playlist:` prefix
  telling `YoutubePlayer` which store to look it up in.
