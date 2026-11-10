"""Complete photo SHA-256 only; interrupted attempts have no persistent digest."""

import hashlib
import os


class FileChangedError(OSError):
    pass


def video_signature(header):
    if header[4:8]==b'ftyp':
        image_brands = (b'heic',b'heix',b'hevc',b'hevx',b'mif1',b'msf1',b'avif',b'avis',b'crx ',b'heis',b'heim',b'hevs',b'hevm')
        return header[8:12] not in image_brands
    if header[4:8] in (b'moov',b'mdat',b'wide',b'free',b'skip'):
        return True
    if header[:4] in (b'RIFF',b'RIFX') and header[8:12]==b'AVI ':
        return True
    if header.startswith((b'\x1a\x45\xdf\xa3',b'\x30\x26\xb2\x75\x8e\x66\xcf\x11',b'\0\0\x01\xba',b'\0\0\x01\xb3')):
        return True
    return (len(header)>188 and header[0]==header[188]==0x47) or (len(header)>196 and header[4]==header[196]==0x47)


def fingerprint(info):
    return (info.st_size,info.st_mtime_ns,str(info.st_dev),str(info.st_ino))


def hash_photo(path, expected, *, media_type, chunk_size=8388608, event=lambda name:None):
    if media_type != 'photo':
        raise ValueError('Videos must never be hashed')
    count = 0
    with open(path,'rb',buffering=0) as stream:
        if fingerprint(os.fstat(stream.fileno())) != expected:
            raise FileChangedError('Photo changed before hashing')
        # Reject known supported video containers even when a filename is wrong
        # or metadata extraction failed. No video bytes reach SHA-256.update.
        if video_signature(stream.read(4096)):
            raise ValueError('Video signature under a photo extension: hashing refused')
        stream.seek(0)
        digest = hashlib.sha256()
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            count += len(chunk)
            event('hash_chunk')
        if fingerprint(os.fstat(stream.fileno())) != expected or fingerprint(path.stat()) != expected or count != expected[0]:
            raise FileChangedError('Photo changed during hashing')
    return digest.hexdigest(),count
