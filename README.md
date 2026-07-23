# mini-vinyl

A tiny NFC-triggered record player. Each vinyl has an NTAG213/215/216 tag
glued in, written (via a phone NFC-writer app, e.g. "NFC Tools") with a
single URI record holding a YouTube URL. Tapping it to the PN532 reader
plays that video's audio out to a paired Bluetooth speaker. Lifting the
vinyl stops playback. There's no on-Pi tag-to-song mapping file - the Pi
just reads whatever URL is written on the tag.

A tag can also hold a YouTube **playlist** URL (any URL with a `list=`
parameter, e.g. copied from "Share -> Copy link" while viewing a
playlist). The first tap streams it live in shuffled order while
downloading every track in the background; once that download finishes,
every later tap re-shuffles and plays the whole playlist from the
downloaded files, starting from track one each time.

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
[Adding songs from your phone](#adding-songs-from-your-phone) below.

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

For playlists, or if you'd rather not use the web UI, use a phone
NFC-writer app (e.g. "NFC Tools" on iOS/Android) to write a single
**URL/URI record** to each NTAG213/215/216 tag:

- YouTube video: the full video URL, e.g. `https://www.youtube.com/watch?v=...`
- YouTube playlist: the full playlist URL, e.g.
  `https://www.youtube.com/playlist?list=...` (any URL with a `list=`
  parameter works, including a `watch?v=...&list=...` link)

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

Search for a song, tap **Add** - the Pi starts downloading it in the
background and the page shows a short code (the eventual filename, minus
`.wav`). While that's up, place a **blank** NTAG213/215/216 tag on the
Pi's own reader; it writes the code onto the tag directly (refusing if
the tag already has data on it, so it won't overwrite an existing vinyl
by accident). Tap that tag to the reader any time afterward to play the
song - no further pairing needed, the code is all it takes.

Only single videos can be added this way; playlists still need to be
written manually (see above). No auth, no HTTPS - this is meant for a
trusted home network only, not for exposing beyond it.

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
  by code, reserving a `<song_title>-<artist>.wav` filename/code up front
  so the web UI can hand it back before the real download finishes, and
  running the actual `yt-dlp` downloads (for both a tapped-and-cached
  URL-tag and the web UI's Add flow) and playlist caching in the
  background. It's shared between `YoutubePlayer` and `web.py` so both
  read and write the same in-memory catalog with no cross-process locking
  needed.
- `mini_vinyl/players/youtube_player.py` shells out to `mpv` (which uses
  `yt-dlp` under the hood) and plays audio out through PipeWire, which
  owns the Bluetooth speaker's A2DP sink. A tag's content is either a raw
  YouTube URL or a bare library "code" (see below); resolving a YouTube
  URL live is slow on Zero W hardware, so an uncached URL-tag streams live
  via `mpv` first, and if it's still playing after a few seconds, a
  background download starts too - later plays of that tag (or of a
  code-tag, once its download has finished) find the cached file and
  start instantly via `pw-play`, with no live resolution involved. A
  playlist URL (`list=...`) follows the same live-then-cache pattern, but
  caches every track in the playlist to
  `~/.cache/mini-vinyl/youtube/playlists/` and adds each one to the same
  `library.json`; every tap - cached or not - plays the tracks back in a
  freshly shuffled order. Playlists have no code-tag equivalent; they're
  only addable by writing the URL manually.
