# Metadata normalization review — 0.3.1

Reviewed 2026-11-09 using ExifTool 13.42. No production drive, original user media,
or existing production database was read or modified. Findings were established
from original ExifTool output on disposable copies before application mappings changed.

## Findings and corrections

| Observation | Original tags / cause | Current behavior |
|---|---|---|
| ISO 141.421356 / 59.460355 | Reproduced `Canon:CameraISO=Auto`, `Canon:BaseISO=100`, `Canon:AutoISO=141.421356237309` or `59.4603557501361`; ExifTool emits matching `Composite:ISO` with `-n`. Its print conversion displays 141/59. | Preserve the exact numeric value. Prefer explicit standard EXIF ISO when present; record `attributes.sources.iso` and `iso_is_derived` so computed Composite ISO is distinguishable. Do not substitute a guessed nominal ISO or round it. |
| Lens model 65535 | Numeric `Composite:LensID=65535`, corresponding to Canon LensType's documented n/a value, was incorrectly used as a `LensModel` fallback. | Use an actual textual `LensModel`; otherwise null. All numeric LensID/LensType values remain unchanged in raw JSON. Do not infer a fixed lens description from the camera name or focal range. |
| JPEG video codec 6 | `IFD0:Compression=6` describes TIFF/EXIF JPEG compression, not a video codec. The normalizer used generic `Compression` as a codec fallback. | Remove that fallback and populate video-only fields only for detected video content. JPEG/HEIC/RAW images have null video fields, including CR3 image containers with QuickTime-style tracks. Genuine video-specific codec IDs/names retain their original values and sources. |
| RAW thumbnail dimensions | CR2 IFD0 was 1536×1024 while EXIF pixel dimensions were 3456×2304; NEF IFD0 was 160×106 while its declared main RAW plane was 3040×2014. | Select paired explicit full-image metadata, DNG DefaultCropSize, or a declared non-reduced CFA/linear RAW plane. Preserve selected source tags and dimension semantics, without guessing from sensor maxima or camera specifications. |

The ISO reproduction uses copied Canon MakerNotes. The upstream JPEG is an EOS
DIGITAL REBEL fixture; a disposable model-label change reproduces the PowerShot S50
tag shape. It is not represented as an authentic S50 photograph. The same numeric
patterns establish the extraction/normalization mechanism, not the contents of the
user's 13 JPEGs, which were not accessed.

Primary references:

- [ExifTool Canon tags](https://exiftool.org/TagNames/Canon.html): AutoISO/BaseISO
  conversion and LensType sentinel meanings.
- [ExifTool Composite tags](https://exiftool.org/TagNames/Composite.html): Canon ISO
  uses numeric CameraISO or BaseISO × AutoISO / 100; LensID is a derived identifier.
- Installed ExifTool 13.42 `Canon.pm`: AutoISO/BaseISO exponential ValueConv and
  `sprintf("%.0f", ...)` PrintConv explain the numeric/display difference.

Global numeric extraction remains enabled: disabling `-n` would also change GPS,
orientation, exposure, duration and other useful numeric fields. Original grouped
metadata is preserved; normalization never rewrites it.

## Additional provenance and date handling

`attributes.sources` records the full selected ExifTool key for normalized fields,
including ISO, lens model, dimensions and codecs. `iso_is_derived` identifies selection
from ExifTool's Composite group; it is not a claim that every manufacturer value is
an unprocessed physical measurement. `dimensions_kind` distinguishes explicit EXIF
pixel dimensions, a declared default crop, or the main RAW plane. The NEF RAW-plane
size is not guessed to be its final developed/cropped display size.

Family-4 `CopyN` qualifiers do not create separate physical tracks. Stream counts
deduplicate TrackN groups. Date offsets/subseconds may pair across CopyN keys in the
same source group only when unambiguous; conflicting unmatched offsets stay unknown.
Original date strings and timezone offsets are retained. No filesystem/filename
capture-date fallback was added.

## Format validation

Public, unmodified upstream fixtures are vendored under `tests/fixtures/exiftool-13.42`
with source/version/license information. Tests always copy them to disposable folders;
tag-writing commands are limited to fixture creation on those new copies. Production
extraction still uses only fixed read-only options.

| Format | Validated metadata |
|---|---|
| JPEG / Canon MakerNotes | Both fractional Auto ISO examples; n/a LensID; JPEG compression; standard ISO priority; preserved originals |
| HEIC/HEIF | Real container, 1596×1064 dimensions; absent dates/GPS remain null; disposable EXIF-enriched copy preserves original date, −08:00 offset and 47.5/−122.25 coordinates |
| Canon CR2 | 3456×2304 EXIF pixel dimensions, camera data, 2005-08-03 capture timestamp; absent GPS remains null |
| Canon CR3 | 6000×4000 dimensions, camera/lens fields, 2018 capture timestamp with +00:00 offset; CRAW image tracks do not become video attributes |
| Nikon NEF | Declared 3040×2014 main RAW plane instead of thumbnail, camera/date fields, absent GPS null |
| DNG | Explicit 3456×2304 default crop, camera/date fields with preserved source timezone ambiguity |
| MOV | Upstream camera container: dimensions, duration, 30 fps, JPEG video codec, raw audio, creation date and one video/audio stream each; absent GPS null |
| MP4 | Generated `mp42` ISO-BMFF metadata fixture: dimensions, duration, frame rate, codec, creation date and QuickTime GPS coordinates |

The upstream assets intentionally contain reduced/omitted pixel or frame payloads;
the MP4 fixture uses an uncompressed sample, not an H.264/H.265 decoder test. These
tests validate metadata parsing, not playback/pixel decoding or every device variant.
Full-sized media, additional RAW families (such as ARW/RW2/ORF), further MP4 codecs,
unusual multi-image primary selection and time-series camera GPS telemetry still
need representative compatibility testing. No live production-drive validation occurred.

## Versioning and existing data

Application version: 0.3.1. Metadata policy: `embedded-v2`; date policy:
`camera-video-v2`. Migrations 001–004 are unchanged and were compared against the
previous 0.3.0 wheel. No data migration rewrites old attributes or raw results.

The next explicitly authorized scan creates results under the corrected policy;
an unfinished older session requires an explicit new/reconciled successor. Existing
raw metadata/history remains intact, and unchanged completed photo hashes are reused.
The user's existing 13-photo results were not rewritten by this review.

Validation: 112 tests passed, including real ExifTool extraction for the formats above,
raw/result history preservation and hash reuse across normalization versions. The
0.3.1 wheel built successfully. No production scan was run.
