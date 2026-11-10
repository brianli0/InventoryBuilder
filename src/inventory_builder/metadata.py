"""Source-preserving metadata normalization; filesystem times never become capture dates."""

from datetime import datetime
import math
import re

METADATA_POLICY = 'embedded-v2'
DATE_POLICY = 'camera-video-v2'

RAW_TYPES = {'CR2','CR3','CRW','NEF','NRW','DNG','ARW','SR2','SRF','RW2','ORF','RAF','PEF','SRW','RAW'}
VIDEO_TYPES = {'MOV','MP4','M4V','AVI','MKV','WMV','ASF','MPEG','MPG','MTS','M2TS','3GP','3G2','WEBM','VOB'}


def source_group(key):
    # Family 4 CopyN qualifiers distinguish duplicate tags, not separate tracks.
    return key.split(':')[0]


def positive_number(value):
    try:
        return type(value) in (int,float) and value>0 and math.isfinite(value)
    except OverflowError:
        return False


def normalize(raw,precedence=('DateTimeOriginal','CreationDate','CreateDate','MediaCreateDate','TrackCreateDate')):
    values = {}
    dates = {}
    warnings = []
    for key, value in raw.items():
        tag = key.split(':')[-1]
        if tag == 'Warning':
            warnings.append(str(value))
        # Exclude filesystem and derived Composite date values from source selection.
        if key.split(':')[0] not in ('System','File','ExifTool','Composite'):
            if any(part in tag.lower() for part in ('date','time','offset')):
                dates[key] = value
        values.setdefault(tag, []).append((key,value))

    def choose(*tags, groups=None, valid=lambda value: value is not None):
        for tag in tags:
            for key, value in values.get(tag, []):
                if (groups is None or source_group(key) in groups) and valid(value):
                    return key,value
        return None,None

    def pick(*tags):
        return choose(*tags)[1]

    mime = str(pick('MIMEType') or '').lower()
    file_type = str(pick('FileType') or '').upper()
    if mime.startswith('image/'):
        is_video = False
    elif mime.startswith('video/'):
        is_video = True
    elif file_type:
        is_video = file_type in VIDEO_TYPES
    else:
        is_video = any(source_group(key) in ('Matroska','ASF','RIFF','QuickTime') and key.split(':')[-1] in ('VideoCodec','VideoCodecID','VideoCodecName','VideoFrameRate','CompressorID') for key in raw)

    attributes = {}
    mapping = {
        'orientation': ('Orientation',), 'rotation': ('Rotation',),
        'camera_make': ('Make','CameraMake'), 'camera_model': ('Model','CameraModelName','CameraModel'),
        'aperture': ('FNumber','Aperture'),
        'exposure_time': ('ExposureTime','ShutterSpeed'), 'focal_length': ('FocalLength',),
        'software': ('Software','Encoder'), 'color_space': ('ColorSpace',),
        'gps_latitude': ('GPSLatitude',), 'gps_longitude': ('GPSLongitude',),
        'gps_altitude': ('GPSAltitude',), 'gps_timestamp': ('GPSDateTime','GPSTimeStamp'),
        'duration_seconds': ('Duration',), 'frame_rate': ('VideoFrameRate','FrameRate'),
        'video_codec': ('CompressorID','VideoCodec','VideoCodecID','VideoCodecName','CompressorName'),
        'audio_codec': ('AudioFormat','AudioCodec','AudioCodecID','AudioCodecName','AudioCompressor'),
        'container': ('FileType',), 'mime_type': ('MIMEType',),
        'bit_rate': ('AvgBitrate','VideoAvgBitrate','Bitrate'),
        'audio_channels': ('AudioChannels',),
    }
    sources = {}
    video_fields = {'duration_seconds','frame_rate','video_codec','audio_codec','bit_rate','audio_channels'}
    for field,tags in mapping.items():
        key,value = (None,None) if field in video_fields and not is_video else choose(*tags)
        attributes[field] = value
        sources[field] = key

    # LensID is an identifier/lookup input, not a model name. In numeric mode,
    # Canon's documented n/a LensType sentinel also appears as LensID=65535.
    model = lambda value: isinstance(value,str) and bool(value.strip()) and re.fullmatch(r'[+-]?\d+(\.\d+)?',value.strip()) is None
    key,value = choose('LensModel',groups={'ExifIFD'},valid=model)
    if key is None:
        key,value = choose('LensModel',valid=model)
    attributes['lens_model'],sources['lens_model'] = value,key

    # Prefer explicit standard sensitivity over maker-note/computed ISO. Preserve
    # exact numeric Composite ISO (including fractional Canon Auto ISO estimates),
    # rather than inventing a nearby nominal setting or applying PrintConv rounding.
    numeric_iso = positive_number
    key,value = None,None
    for groups in ({'ExifIFD'},{'IFD0'},{'XMP-exif'},set(source_group(k) for k,_ in values.get('ISO',[]) if source_group(k)!='Composite'),{'Composite'}):
        key,value = choose('ISO',groups=groups,valid=numeric_iso)
        if key is not None:
            break
    attributes['iso'],sources['iso'] = value,key
    attributes['iso_is_derived'] = source_group(key)=='Composite' if key else None

    # Select paired dimensions from a declared image source, never combine one
    # IFD's width with another IFD's height or guess from the largest sensor size.
    dimension = None
    positive = positive_number
    def pair(width_tag,height_tag,groups=None):
        for width_key,width in values.get(width_tag,[]):
            group = source_group(width_key)
            if (groups is None or group in groups) and positive(width):
                for height_key,height in values.get(height_tag,[]):
                    if source_group(height_key)==group and positive(height):
                        return width,height,width_key,height_key
        return None

    if file_type in RAW_TYPES and not is_video:
        if file_type=='DNG':
            for key,value in values.get('DefaultCropSize',[]):
                parts = value if isinstance(value,(list,tuple)) else str(value).split()
                if len(parts)==2:
                    try:
                        w,h = (float(v) for v in parts)
                    except (TypeError,ValueError):
                        continue
                    if positive(w) and positive(h) and w.is_integer() and h.is_integer():
                        dimension = int(w),int(h),key,key
                        attributes['dimensions_kind'] = 'declared_default_crop'
                        break
        if dimension is None:
            dimension = pair('ExifImageWidth','ExifImageHeight',{'ExifIFD'})
            if dimension:
                attributes['dimensions_kind'] = 'exif_pixel_dimensions'
        if dimension is None:
            main_raw = {source_group(k) for k,v in values.get('SubfileType',[]) if v==0} & {source_group(k) for k,v in values.get('PhotometricInterpretation',[]) if v in (32803,34892)}
            dimension = pair('ImageWidth','ImageHeight',main_raw)
            if dimension:
                attributes['dimensions_kind'] = 'main_raw_plane'
    if dimension is None:
        dimension = pair('ImageWidth','ImageHeight',{'File'})
    if dimension is None and is_video:
        video_tracks = {source_group(k) for k,v in values.get('HandlerType',[]) if v in ('vide','Video Track')}
        dimension = pair('ImageWidth','ImageHeight',video_tracks)
    if dimension is None:
        dimension = pair('ImageWidth','ImageHeight') or pair('ExifImageWidth','ExifImageHeight') or pair('SourceImageWidth','SourceImageHeight')
    attributes['width'],attributes['height'],sources['width'],sources['height'] = dimension or (None,None,None,None)
    attributes['sources'] = sources
    for field,ref in (('gps_latitude','GPSLatitudeRef'),('gps_longitude','GPSLongitudeRef')):
        value = attributes[field]
        if isinstance(value,(int,float)) and str(pick(ref)).upper() in ('S','W'):
            attributes[field] = -abs(value)
    if isinstance(attributes['gps_altitude'],(int,float)) and pick('GPSAltitudeRef') == 1:
        attributes['gps_altitude'] = -abs(attributes['gps_altitude'])
    w,h = attributes['width'],attributes['height']
    if isinstance(w,(int,float)) and isinstance(h,(int,float)):
        swap = attributes['orientation'] in (5,6,7,8) or attributes['rotation'] in (90,270,-90)
        attributes['display_width'],attributes['display_height'] = (h,w) if swap else (w,h)
        attributes['megapixels'] = w*h/1_000_000
    tracks = {}
    for key,value in raw.items() if is_video else ():
        group,_,tag = key.rpartition(':')
        if group.startswith('Track'):
            tracks.setdefault(group,{})[tag] = value
    attributes['streams'] = tracks
    attributes['video_stream_count'] = len({source_group(k) for k,v in raw.items() if k.split(':')[-1]=='HandlerType' and v in ('vide','Video Track')}) or None if is_video else None
    attributes['audio_stream_count'] = len({source_group(k) for k,v in raw.items() if k.split(':')[-1]=='HandlerType' and v in ('soun','Audio Track')}) or None if is_video else None

    selected = None
    # Embedded original camera dates precede suitable video dates. Never select
    # ModifyDate, FileCreateDate or FileModifyDate as a capture-time substitute.
    for tag in precedence:
        candidates = []
        for key,value in dates.items():
            if key.split(':')[-1] != tag or not isinstance(value,str):
                continue
            match = re.match(r'^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(.*)$',value)
            text = f'{match[1]}-{match[2]}-{match[3]}T{match[4]}:{match[5]}:{match[6]}{match[7]}' if match else value.replace(' ','T',1)
            offset_tag = 'OffsetTimeOriginal' if tag == 'DateTimeOriginal' else 'OffsetTimeDigitized' if tag == 'CreateDate' else 'OffsetTime'
            def companion(tag_name):
                exact = [v for k,v in dates.items() if k.rpartition(':')[0]==key.rpartition(':')[0] and k.split(':')[-1]==tag_name]
                if exact:
                    return exact[0]
                related = [v for k,v in dates.items() if source_group(k)==source_group(key) and k.split(':')[-1]==tag_name]
                return related[0] if related and all(v==related[0] for v in related) else None
            offset = companion(offset_tag)
            if offset and re.fullmatch(r'[+-]\d{2}:\d{2}',str(offset)) and not re.search(r'(Z|[+-]\d{2}:\d{2})$',text):
                text += str(offset)
            subsec_tag = 'SubSecTimeOriginal' if tag=='DateTimeOriginal' else 'SubSecTimeDigitized'
            subseconds = companion(subsec_tag)
            if subseconds is not None and str(subseconds).isdigit() and '.' not in text:
                text = text[:19]+'.'+str(subseconds)+text[19:]
            try:
                parsed = datetime.fromisoformat(text.replace('Z','+00:00'))
                if parsed.year < 1900:
                    continue
            except ValueError:
                warnings.append(f'Invalid embedded date: {key}')
                continue
            candidates.append((parsed.tzinfo is not None,parsed,key))
        if candidates:
            aware,parsed,source = max(candidates,key=lambda item:item[0])
            selected = dict(datetime=parsed.isoformat(),date=parsed.date().isoformat(),source=source,confidence='embedded_with_offset' if aware else 'embedded_timezone_unknown')
            break
    return dict(attributes=attributes, dates=dates, selected=selected, warnings=warnings)
