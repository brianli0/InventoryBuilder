"""Bounded batch ExifTool adapter. Every supplied path names one validated file."""

import json
import os
from decimal import Decimal,InvalidOperation
from pathlib import Path
import shutil
import tempfile

from .bounded_process import run_bounded


class ExifTool:
    def __init__(self, config):
        self.config = config
        self.executable = shutil.which(str(config.exiftool_path))
        if not self.executable:
            raise ValueError('ExifTool executable not found; configure exiftool_path')
        directory = config.temp_directory or config.database.parent/'tmp'
        directory.mkdir(parents=True,exist_ok=True)
        # Keep executable/runtime extraction caches inside mandatory exclusions,
        # including when the selected target is an entire internal drive.
        self.environment = dict(os.environ,TEMP=str(directory),TMP=str(directory),TMPDIR=str(directory))
        status, data, errors = run_bounded([self.executable, '-config', '', '-ver'], timeout=10, output_limit=4096,env=self.environment)
        self.version = data.decode('utf-8', 'replace').strip()
        if status or not self.version or not self.version.replace('.', '').isdigit():
            raise ValueError('Could not verify ExifTool version: '+errors.decode('utf-8', 'replace'))

    def extract_batch(self, paths):
        if not paths or len(paths) > self.config.metadata_batch_size:
            raise ValueError('Invalid metadata batch size')
        directory = self.config.temp_directory or self.config.database.parent / 'tmp'
        directory.mkdir(parents=True, exist_ok=True)
        argfile = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=directory, suffix='.args', delete=False) as stream:
                argfile = Path(stream.name)
                # Grouped instance-qualified keys retain duplicates/provenance.
                stream.write('-j\n-G1:4\n-a\n-n\n-struct\n-api\nQuickTimeUTC=0\n-api\nLargeFileSupport=1\n--\n')
                for path in paths:
                    if not path.is_absolute() or '\n' in str(path) or '\r' in str(path):
                        raise ValueError('Expected an absolute single-line media path')
                    stream.write(str(path)+'\n')
            code, data, stderr = run_bounded([self.executable, '-config', '', '-charset', 'filename=UTF8', '-@', str(argfile)], timeout=self.config.metadata_timeout_seconds, output_limit=self.config.metadata_output_limit,heartbeat=getattr(self,'heartbeat',None),env=self.environment)
            try:
                values = json.loads(data.decode('utf-8-sig'))
            except (ValueError, UnicodeError) as exc:
                raise ValueError('ExifTool returned invalid JSON: '+stderr.decode('utf-8', 'replace')[:2000]) from exc
            if not isinstance(values, list):
                raise ValueError('ExifTool JSON must be a list')
            from .paths import compare
            results = []
            for path in paths:
                matched = [value for value in values if isinstance(value, dict) and isinstance(value.get('SourceFile'), str) and compare(str(Path(value['SourceFile'])), str(path)) == 0]
                if len(matched) != 1:
                    results.append(ValueError('Missing or ambiguous ExifTool response for '+str(path)))
                    continue
                value = matched[0]
                error = next((v for k, v in value.items() if k.split(':')[-1] == 'Error'), None)
                mime = next((v for k,v in value.items() if k.split(':')[-1]=='MIMEType'),None)
                reported_version = next((v for k,v in value.items() if k.split(':')[-1]=='ExifToolVersion'),None)
                try:
                    version_matches = reported_version is not None and Decimal(str(reported_version))==Decimal(self.version)
                except InvalidOperation:
                    version_matches = False
                if error:
                    results.append(ValueError(str(error)))
                elif not version_matches:
                    results.append(ValueError('ExifTool version changed or provenance is missing; use an explicit successor scan'))
                elif mime and not str(mime).lower().startswith(('image/','video/')):
                    results.append(ValueError(f'Not recognized as supported image/video content: {mime}'))
                elif code and not any(k.split(':')[-1] == 'FileType' for k in value):
                    results.append(ValueError('ExifTool failed: '+stderr.decode('utf-8', 'replace')[:2000]))
                else:
                    results.append(value)
            return results
        finally:
            if argfile is not None:
                argfile.unlink(missing_ok=True)
