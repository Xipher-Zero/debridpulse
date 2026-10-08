# Media Downloads finalization fixtures

Small committed media for the Media Downloads finalization tests
(`tests/test_v113_media_downloads_runtime.py`). They are committed rather than
synthesized at test time because DebridPulse's FFmpeg (`packaging/ffmpeg/`)
has no encoder by design. Generated once with a full FFmpeg build:

```sh
q="-hide_banner -loglevel error -y"
ffmpeg $q -f lavfi -i testsrc=duration=2:size=320x240:rate=10 -c:v libx264 -an video.mp4
ffmpeg $q -f lavfi -i sine=duration=2 -c:a aac -vn audio.m4a
ffmpeg $q -f lavfi -i testsrc=duration=2:size=320x240:rate=10 -f lavfi -i sine=duration=2 \
  -c:v libx264 -c:a aac -shortest clip.mp4
ffmpeg $q -i clip.mp4 -c copy -f mpegts hls.ts            # HLS-style segment stream, ADTS AAC
ffmpeg $q -f lavfi -i testsrc=duration=2:size=320x240:rate=10 -c:v libvpx-vp9 -b:v 200k -an video.webm
ffmpeg $q -f lavfi -i sine=duration=2 -c:a libopus -vn audio.webm
ffmpeg $q -f lavfi -i sine=duration=2 -c:a libopus -vn audio.opus
```
