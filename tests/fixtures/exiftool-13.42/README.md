# ExifTool metadata fixtures

These unmodified, reduced metadata test files come from Phil Harvey's ExifTool
13.42 release, `t/images/`:

https://github.com/exiftool/exiftool/tree/13.42/t/images

Download source: https://codeload.github.com/exiftool/exiftool/zip/refs/tags/13.42

Included: Canon.jpg, CanonRaw.cr2, CanonRaw.cr3, Nikon.nef, DNG.dng,
QuickTime.heic and QuickTime.mov. Source copyright: Phil Harvey, 2003–2025;
upstream README distributes ExifTool under the same terms as Perl (Artistic or GPL).
The release's LICENSE is reproduced alongside the fixtures. These are separate
upstream test assets, not InventoryBuilder runtime code or user media.

The files intentionally have reduced/omitted pixel/video payloads. They validate
real ExifTool container and metadata parsing, not full pixel decoding, playback,
or coverage of every camera/codec variant. Tests copy them into temporary folders
before extraction. Tests needing a particular ISO/date/GPS value write only freshly
created disposable copies during fixture setup; these originals remain unchanged.

Canon floating Auto ISO examples use Canon ShotInfo AutoISO and BaseISO, remove
the standard EXIF ISO on the disposable copy, and set CameraISO to Auto. This
reproduces numeric-mode Composite:ISO values 141.421356237309 and 59.4603557501361.
ExifTool print conversion displays 141 and 59. No nominal ISO is inferred by tests
or normalization. Model-label changes on copies reproduce a PowerShot S50 tag shape;
they do not claim that the upstream Canon.jpg was captured by that model.
