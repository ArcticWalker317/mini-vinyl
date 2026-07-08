# mini-vinyl

A tiny NFC-triggered record player. Each vinyl has an NTAG213/215/216 tag
glued in, written (via a phone NFC-writer app, e.g. "NFC Tools") with a
single URI record - either a YouTube URL or a `spotify:track:...` /
`spotify:album:...` / `spotify:playlist:...` URI. Tapping it to the PN532
reader plays that URI's audio out to a paired Bluetooth speaker. Lifting
the vinyl stops playback. There's no on-Pi tag-to-song mapping file - the
Pi just reads whatever URI is written on the tag.

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

### librespot (Spotify Connect)

Only needed if you want Spotify tags to work. Requires a **Spotify
Premium** account. Install a prebuilt `librespot` binary (see
https://github.com/librespot-org/librespot for Pi/armhf builds - check
whether the build has the `pipewire` backend compiled in; if not, use
`--backend alsa --device default` in `systemd/librespot.service` instead,
which PipeWire intercepts automatically via `pipewire-alsa`).

Create a Spotify app at https://developer.spotify.com/dashboard, add
`http://127.0.0.1:8080/callback` as a redirect URI, and copy the client
ID/secret into `config/secrets.env`.

### Config

```bash
cp config/secrets.example.env config/secrets.env
```

Fill in `config/secrets.env` (Bluetooth MAC, Spotify credentials).

### Writing tags

Using a phone NFC-writer app (e.g. "NFC Tools" on iOS/Android), write a
single **URL/URI record** to each NTAG213/215/216 tag:

- YouTube: the full video URL, e.g. `https://www.youtube.com/watch?v=...`
- Spotify: a Spotify URI, e.g. `spotify:album:4LH4d3cOWNNsVw41Gqt2kv`
  (get this from the Spotify app: Share -> Copy Spotify URI)

Verify a tag was written correctly:

```bash
python -m mini_vinyl.main --scan
```

Hold the tag to the reader - it should print the UID and the decoded URI.
If `URI: None`, the tag has no NDEF record (or isn't an NTAG21x tag).

### Run it

```bash
python -m mini_vinyl.main
```

First Spotify playback will open a browser auth URL in the terminal -
complete that once; the token is cached in `.spotify_token_cache` after.

### Run on boot

All three services run as **user** systemd units, not system ones -
`mini-vinyl.service` and `librespot.service` need to reach your PipeWire
audio session, and `bt-autoconnect.service` needs to run *after*
PipeWire/WirePlumber have registered the A2DP audio profile with BlueZ
(a system-level unit races ahead of that and fails to connect).

```bash
mkdir -p ~/.config/systemd/user
cp systemd/mini-vinyl.service systemd/librespot.service systemd/bt-autoconnect.service \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bt-autoconnect.service
systemctl --user enable --now librespot.service   # if using Spotify
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
  parsing).
- `mini_vinyl/main.py` picks a player based on the URI's scheme
  (`spotify:...` -> Spotify, everything else -> YouTube) and tells the
  `PlayerManager` to start playback; when the tag is lifted (a few
  consecutive empty polls, to ignore momentary read misses) it stops
  playback.
- `mini_vinyl/players/youtube_player.py` shells out to `mpv` (which uses
  `yt-dlp` under the hood) and plays audio out through PipeWire, which
  owns the Bluetooth speaker's A2DP sink. Resolving a YouTube URL live is
  slow on Zero W hardware, so the first play of a tag also downloads the
  full audio to `~/.cache/mini-vinyl/youtube/` in the background; later
  plays of that tag find the cached file and start instantly, with no
  live resolution involved.
- `mini_vinyl/players/spotify_player.py` uses the Spotify Web API
  (`spotipy`) to tell the `librespot` Spotify Connect instance running on
  the Pi what to play; `librespot` itself does the audio decode/output,
  also through PipeWire.
