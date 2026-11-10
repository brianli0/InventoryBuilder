from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from inventory_builder.bounded_process import run_bounded
from inventory_builder.config import Config
from inventory_builder.exiftool import ExifTool
from inventory_builder.metadata import normalize,METADATA_POLICY
from inventory_builder.scanner import scan_folder
from inventory_builder.storage.database import connect
from inventory_builder.volumes import Volume
from test_collection import mov

FIXTURES=Path(__file__).parent/'fixtures/exiftool-13.42'


@pytest.fixture
def review(tmp_path,database,monkeypatch):
    root=tmp_path/'disposable review'
    root.mkdir()
    config=Config(database,tmp_path/'logs',metadata_batch_size=32,retry_delay_seconds=0)
    tool=ExifTool(config)
    monkeypatch.setattr('inventory_builder.scanner.selected_volume',lambda path:Volume(root,'review-guid','review-serial','TESTFS',1000000))
    return root,config,tool


def disposable(root,name,output=None):
    result=root/(output or name)
    shutil.copyfile(FIXTURES/name,result)
    return result


def populate_tags(tool,path,*arguments):
    # This helper is only used during creation of a disposable fixture copy.
    # Production extraction continues to use fixed read-only options.
    assert not path.is_relative_to(FIXTURES.resolve())
    code,data,error=run_bounded([tool.executable,'-config','','-overwrite_original',*arguments,str(path)],env=tool.environment)
    assert code==0,(data,error)


def canon_auto(root,tool,iso):
    path=disposable(root,'Canon.jpg',f'disposable-S50-auto-{iso}.jpg')
    populate_tags(tool,path,'-EXIF:ISO=','-Canon:CameraISO=Auto',f'-Canon:AutoISO#={iso}','-Canon:BaseISO#=100','-Model=Canon PowerShot S50','-IFD0:Compression#=6')
    return path


@pytest.mark.parametrize('iso',[141.421356237309,59.4603557501361])
def test_original_numeric_tags_explain_fractional_iso_without_rounding(review,iso):
    root,config,tool=review
    path=canon_auto(root,tool,iso)
    original=path.read_bytes()
    raw=tool.extract_batch([path])[0]
    code,data,error=run_bounded([tool.executable,'-config','','-j','-G1:4','-a',str(path)],env=tool.environment)
    assert code==0
    display=json.loads(data)[0]
    assert raw['Canon:CameraISO']=='Auto'
    assert raw['Canon:BaseISO']==100
    assert raw['Composite:ISO']==pytest.approx(iso,rel=1e-12)
    assert display['Composite:ISO']==(141 if iso>100 else 59)
    before=deepcopy(raw)
    normalized=normalize(raw)['attributes']
    assert normalized['iso']==raw['Composite:ISO']
    assert normalized['iso_is_derived'] is True
    assert normalized['sources']['iso']=='Composite:ISO'
    assert normalized['lens_model'] is None
    assert normalized['video_codec'] is None
    assert raw['Composite:LensID']==65535 and raw['IFD0:Compression']==6
    assert raw==before and path.read_bytes()==original


def test_standard_iso_precedes_composite_regardless_of_tag_order():
    raw={'Composite:ISO':141.421356237309,'Canon:ISO':59.4603557501361,'ExifIFD:ISO':100,'XMP-exif:ISO':200}
    result=normalize(raw)['attributes']
    assert result['iso']==100 and result['iso_is_derived'] is False
    assert result['sources']['iso']=='ExifIFD:ISO'
    assert normalize({'Composite:ISO':59.4603557501361})['attributes']['iso']==59.4603557501361


def test_only_real_lens_model_and_media_specific_codec_tags_are_selected():
    raw={'File:MIMEType':'image/jpeg','IFD0:Compression':6,'Composite:LensID':65535,'Canon:LensType':65535}
    assert normalize(raw)['attributes']['lens_model'] is None
    assert normalize(raw)['attributes']['video_codec'] is None
    raw['ExifIFD:LensModel']='Embedded lens description'
    assert normalize(raw)['attributes']['lens_model']=='Embedded lens description'
    assert normalize(raw)['attributes']['sources']['lens_model']=='ExifIFD:LensModel'
    assert normalize({'File:MIMEType':'video/quicktime','IFD0:Compression':6})['attributes']['video_codec'] is None
    video=normalize({'File:MIMEType':'video/mp4','Track1:CompressorID':'avc1','IFD0:Compression':6})['attributes']
    assert video['video_codec']=='avc1' and video['sources']['video_codec']=='Track1:CompressorID'


@pytest.mark.parametrize('name,dimensions,date_prefix',[
    ('CanonRaw.cr2',(3456,2304),'2005-08-03T18:59:18'),
    ('CanonRaw.cr3',(6000,4000),'2018-02-21T12:08:56'),
    ('Nikon.nef',(3040,2014),'2004-06-09T16:02:35'),
    ('DNG.dng',(3456,2304),'2005-08-03T18:59:18'),
])
def test_authentic_raw_metadata_uses_full_image_not_preview(review,name,dimensions,date_prefix):
    root,config,tool=review
    path=disposable(root,name)
    original=path.read_bytes()
    raw=tool.extract_batch([path])[0]
    parsed=normalize(raw)
    attributes=parsed['attributes']
    assert (attributes['width'],attributes['height'])==dimensions
    assert parsed['selected']['datetime'].startswith(date_prefix)
    assert attributes['camera_make'] is not None
    assert attributes['video_codec'] is None
    assert attributes['duration_seconds'] is None
    assert attributes['frame_rate'] is None
    assert attributes['video_stream_count'] is None
    assert attributes['sources']['width'] is not None
    assert attributes['gps_latitude'] is None and attributes['gps_longitude'] is None
    assert path.read_bytes()==original
    if name=='CanonRaw.cr3':
        assert parsed['selected']['datetime'].endswith('+00:00')
        assert raw['Track1:Copy1:CompressorID']=='CRAW' # Container track is not a video.
    if name=='DNG.dng':
        assert attributes['dimensions_kind']=='declared_default_crop'
    if name=='Nikon.nef':
        assert attributes['dimensions_kind']=='main_raw_plane'


def test_heic_with_and_without_embedded_dates_and_gps(review):
    root,config,tool=review
    plain=disposable(root,'QuickTime.heic')
    parsed=normalize(tool.extract_batch([plain])[0])
    assert (parsed['attributes']['width'],parsed['attributes']['height'])==(1596,1064)
    assert parsed['selected'] is None and parsed['attributes']['gps_latitude'] is None
    with_exif=disposable(root,'QuickTime.heic','disposable-tagged.heic')
    populate_tags(tool,with_exif,'-Make=Disposable','-Model=HEIC Fixture','-DateTimeOriginal=2024:02:03 04:05:06','-OffsetTimeOriginal=-08:00','-GPSLatitude=47.5','-GPSLatitudeRef=N','-GPSLongitude=122.25','-GPSLongitudeRef=W')
    before=with_exif.read_bytes()
    raw=tool.extract_batch([with_exif])[0]
    parsed=normalize(raw)
    assert parsed['selected']['datetime']=='2024-02-03T04:05:06-08:00'
    assert parsed['attributes']['gps_latitude']==47.5
    assert parsed['attributes']['gps_longitude']==-122.25
    assert parsed['attributes']['video_codec'] is None
    assert parsed['dates']['ExifIFD:DateTimeOriginal']=='2024:02:03 04:05:06'
    assert with_exif.read_bytes()==before


def test_real_mov_codec_dates_dimensions_and_stream_counts(review):
    root,config,tool=review
    path=disposable(root,'QuickTime.mov')
    raw=tool.extract_batch([path])[0]
    parsed=normalize(raw)
    attributes=parsed['attributes']
    assert attributes['container']=='MOV'
    assert (attributes['width'],attributes['height'])==(320,240)
    assert attributes['duration_seconds']==pytest.approx(4.96666666666667)
    assert attributes['frame_rate']==30 and attributes['video_codec']=='jpeg'
    assert attributes['audio_codec']=='raw '
    assert attributes['video_stream_count']==1 and attributes['audio_stream_count']==1
    assert parsed['selected']['datetime']=='2005-08-11T14:03:54'
    assert parsed['selected']['confidence']=='embedded_timezone_unknown'
    assert attributes['gps_latitude'] is None and attributes['gps_longitude'] is None


def test_generated_mp4_dates_dimensions_gps_and_video_attributes(review):
    root,config,tool=review
    path=mov(root/'disposable.mp4',brand=b'mp42')
    before=path.read_bytes()
    raw=tool.extract_batch([path])[0]
    parsed=normalize(raw)
    attributes=parsed['attributes']
    assert attributes['container']=='MP4' and attributes['mime_type']=='video/mp4'
    assert (attributes['width'],attributes['height'])==(640,480)
    assert attributes['duration_seconds']==2.5 and attributes['frame_rate']==.4
    assert attributes['video_codec']=='raw '
    assert attributes['gps_latitude']==47.5 and attributes['gps_longitude']==-122.25
    assert parsed['selected']['datetime']=='2024-02-03T04:05:06'
    assert parsed['selected']['confidence']=='embedded_timezone_unknown'
    assert path.read_bytes()==before


def test_family_four_copy_qualifiers_preserve_date_companion_offsets():
    raw={'ExifIFD:Copy1:DateTimeOriginal':'2024:02:03 04:05:06','ExifIFD:OffsetTimeOriginal':'-08:00'}
    assert normalize(raw)['selected']['datetime']=='2024-02-03T04:05:06-08:00'


def test_mixed_formats_comprehensive_collection_and_provenance(review):
    root,config,tool=review
    for name in ('Canon.jpg','CanonRaw.cr2','CanonRaw.cr3','Nikon.nef','DNG.dng','QuickTime.heic','QuickTime.mov'):
        disposable(root,name)
    mov(root/'generated.mp4',brand=b'mp42')
    originals={path:path.read_bytes() for path in root.iterdir()}
    assert scan_folder(root,config,progress=lambda text:None)==0
    connection=connect(config.database)
    try:
        assert connection.execute('SELECT count(*) FROM media_files').fetchone()[0]==8
        assert connection.execute('SELECT count(*) FROM file_hashes').fetchone()[0]==6
        assert connection.execute('SELECT count(*) FROM metadata_results WHERE legacy=0').fetchone()[0]==8
        assert not connection.execute('PRAGMA foreign_key_check').fetchone()
        for result in connection.execute('SELECT * FROM metadata_results WHERE legacy=0'):
            assert result['extractor']=='ExifTool' and result['extractor_version']==tool.version
            assert METADATA_POLICY in result['policy_version']
            assert json.loads(result['raw_metadata_json'])['SourceFile']
        assert not connection.execute("SELECT h.id FROM file_hashes h JOIN file_revisions r ON r.id=h.revision_id JOIN media_files m ON m.id=r.media_id WHERE m.media_type='video'").fetchone()
    finally:
        connection.close()
    assert originals=={path:path.read_bytes() for path in root.iterdir()}


def test_normalization_version_preserves_old_raw_results_and_reuses_hash(review,monkeypatch):
    root,config,tool=review
    path=canon_auto(root,tool,141.421356237309)
    original=path.read_bytes()
    def legacy_normalizer(raw,precedence):
        result=normalize(raw,precedence)
        result['attributes']['lens_model']=raw['Composite:LensID']
        result['attributes']['video_codec']=raw['IFD0:Compression']
        return result
    with monkeypatch.context() as patch:
        patch.setattr('inventory_builder.scanner.METADATA_POLICY','embedded-v1')
        patch.setattr('inventory_builder.scanner.DATE_POLICY','camera-video-v1')
        patch.setattr('inventory_builder.storage.processing.METADATA_POLICY','embedded-v1')
        patch.setattr('inventory_builder.storage.processing.DATE_POLICY','camera-video-v1')
        patch.setattr('inventory_builder.processing.normalize',legacy_normalizer)
        assert scan_folder(root,config,progress=lambda text:None)==0
    connection=connect(config.database)
    try:
        old=dict(connection.execute('SELECT * FROM metadata_results WHERE legacy=0').fetchone())
        assert json.loads(old['attributes_json'])['lens_model']==65535
        hashes=[dict(row) for row in connection.execute('SELECT * FROM file_hashes')]
    finally:
        connection.close()
    assert scan_folder(root,config,progress=lambda text:None)==0
    connection=connect(config.database)
    try:
        results=[dict(row) for row in connection.execute('SELECT * FROM metadata_results WHERE legacy=0 ORDER BY id')]
        assert len(results)==2 and results[0]==old
        current=json.loads(results[1]['attributes_json'])
        assert current['lens_model'] is None and current['video_codec'] is None
        assert current['iso']==json.loads(results[1]['raw_metadata_json'])['Composite:ISO']
        assert current['sources']['iso']=='Composite:ISO'
        assert [dict(row) for row in connection.execute('SELECT * FROM file_hashes')]==hashes
        assert 'embedded-v2' in results[1]['policy_version']
        assert connection.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]==4
    finally:
        connection.close()
    assert path.read_bytes()==original
