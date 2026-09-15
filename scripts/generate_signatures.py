#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Generate a fidosigs format signature release from PRONOM, non-interactively.

Copyright 2026 The Open Preservation Foundation

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Produces <format-dir>/vNNN/ holding the three files the service publishes:
DROID_SignatureFile-vNNN.xml, pronom-xml-vNNN.zip and formats-vNNN.xml.

This replaces fido.update_signatures for CI use. That module prompts on stdin,
writes into fido's own package directory, and reaches PRONOM over plain http,
which PRONOM now answers with a redirect urllib refuses to follow for a POST.
The PRONOM record conversion itself is still fido's (fido.prepare.FormatInfo),
so the output matches what fido users would build locally.
"""
from argparse import ArgumentParser
import logging
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
import zipfile
from xml.etree import ElementTree

from fido.prepare import FormatInfo
from fido.pronom.soap import NS

PRONOM_ROOT = 'https://www.nationalarchives.gov.uk'
SOAP_URL = PRONOM_ROOT + '/pronom/service.asmx'
DROID_URL = PRONOM_ROOT + '/documents/DROID_SignatureFile_V{}.xml'
PUID_URL = PRONOM_ROOT + '/pronom/{}.xml'
USER_AGENT = 'fidosigs signature generator (Open Preservation Foundation)'

DROID_NAME = 'DROID_SignatureFile-v{}.xml'
PRONOM_ZIP_NAME = 'pronom-xml-v{}.zip'
FORMATS_NAME = 'formats-v{}.xml'

# Delay between per-PUID requests, to avoid a DoS of the PRONOM server.
DEFAULT_THROTTLE = 0.5
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3

LOGGER = logging.getLogger('generate_signatures')


class PronomError(Exception):
    """PRONOM could not be reached or returned something unusable."""


class ReleaseExistsError(Exception):
    """The requested version is already present in the format directory."""


def fetch(url, data=None, headers=None, retries=DEFAULT_RETRIES, timeout=DEFAULT_TIMEOUT):
    """Return the body of url, retrying transient failures with linear backoff."""
    request_headers = {'User-Agent': USER_AGENT}
    request_headers.update(headers or {})
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, data=data, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, socket.timeout, ConnectionError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code == 404:
                raise PronomError('{} returned 404'.format(url)) from error
            if attempt == retries:
                raise PronomError('{} failed after {} attempts: {}'.format(url, retries, error)) from error
            LOGGER.warning('%s failed (attempt %d/%d): %s', url, attempt, retries, error)
            time.sleep(attempt * 5)
    raise PronomError('{} was not attempted, retries must be at least 1'.format(url))


def latest_pronom_version():
    """Return the current PRONOM signature file version as an int, via the SOAP service."""
    action = 'getSignatureFileVersionV1'
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="{soap}"><soap:Body><{action} xmlns="{pronom}" />'
        '</soap:Body></soap:Envelope>'
    ).format(soap=NS['soap'], pronom=NS['pronom'], action=action).encode('utf-8')
    headers = {
        'Content-Type': 'text/xml; charset=utf-8',
        'SOAPAction': '"{}:{}In"'.format(NS['pronom'], action),
    }
    body = fetch(SOAP_URL, data=envelope, headers=headers)
    return parse_version_response(body)


def parse_version_response(body):
    """Extract the version number from a getSignatureFileVersionV1 SOAP response."""
    try:
        version = ElementTree.fromstring(body).find('.//pronom:Version/pronom:Version', NS)
    except ElementTree.ParseError as error:
        raise PronomError('SOAP version response is not XML: {}'.format(error)) from error
    if version is None or not (version.text or '').strip().isdigit():
        raise PronomError('SOAP version response holds no version number')
    return int(version.text.strip())


def puids_from_droid(droid_xml):
    """Return the PUIDs of every FileFormat in a DROID signature file, in file order."""
    try:
        root = ElementTree.fromstring(droid_xml)
    except ElementTree.ParseError as error:
        raise PronomError('DROID signature file is not XML: {}'.format(error)) from error
    puids = [element.get('PUID') for element in root.iterfind('.//sig:FileFormat', NS)]
    if not puids:
        raise PronomError('DROID signature file lists no FileFormat elements')
    return puids


def puid_file_name(puid):
    """Return fido's zip member name for a PUID, e.g. fmt/1 -> puid.fmt.1.xml."""
    registry, number = puid.split('/')
    return 'puid.{}.{}.xml'.format(registry, number)


def download_records(puids, records_dir, throttle=DEFAULT_THROTTLE):
    """Download each PUID's PRONOM XML into records_dir, skipping records already held."""
    os.makedirs(records_dir, exist_ok=True)
    total = len(puids)
    for index, puid in enumerate(puids, start=1):
        path = os.path.join(records_dir, puid_file_name(puid))
        if os.path.isfile(path):
            continue
        record = fetch(PUID_URL.format(puid))
        try:
            ElementTree.fromstring(record)
        except ElementTree.ParseError as error:
            raise PronomError('PRONOM record for {} is not XML: {}'.format(puid, error)) from error
        partial = path + '.part'
        with open(partial, 'wb') as handle:
            handle.write(record)
        os.replace(partial, path)
        if index % 100 == 0 or index == total:
            LOGGER.info('Downloaded %d/%d PRONOM records', index, total)
        time.sleep(throttle)


def write_pronom_zip(puids, records_dir, zip_path):
    """Zip the PRONOM records in PUID order, as fido.update_signatures does."""
    with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
        for puid in puids:
            name = puid_file_name(puid)
            archive.write(os.path.join(records_dir, name), arcname=name)


def convert_to_fido(zip_path, formats_path):
    """Convert the PRONOM zip to fido's formats XML, returning the format count."""
    info = FormatInfo(zip_path)
    info.load_pronom_xml()
    info.save(formats_path)
    return len(info.formats)


def uncompilable_signatures(formats_path):
    """
    Return (puid, error) for each fido signature regex Python cannot compile.

    fido skips these at identification time, so the format cannot be matched
    by signature. The known cause is in fido.prepare: it writes .{Offset,MaxOffset}
    although PRONOM's MaxOffset is relative to Offset (DROID uses Offset+MaxOffset),
    so any MaxOffset smaller than Offset yields an invalid repeat.
    """
    failures = []
    for format_element in ElementTree.parse(formats_path).getroot().iter('format'):
        for regex in format_element.iter('regex'):
            try:
                re.compile(regex.text or '')
            except re.error as error:
                failures.append((format_element.findtext('puid'), str(error)))
    return failures


def generate(format_dir, work_dir, version=None, throttle=DEFAULT_THROTTLE, force=False):
    """
    Build format_dir/vNNN for the given PRONOM version, or the latest if None.

    Downloads are cached in work_dir, so an interrupted run resumes where it
    stopped. The release is staged inside format_dir under a name the service
    ignores, then renamed, so a live service never sees a partial release.
    Return the path of the release directory.
    """
    if version is None:
        version = latest_pronom_version()
        LOGGER.info('PRONOM latest signature version is v%d', version)
    release_dir = os.path.join(format_dir, 'v{}'.format(version))
    if os.path.isdir(release_dir) and not force:
        raise ReleaseExistsError('{} already exists, use --force to rebuild it'.format(release_dir))

    version_work = os.path.join(work_dir, 'v{}'.format(version))
    staging_dir = os.path.join(format_dir, '.v{}.partial'.format(version))
    shutil.rmtree(staging_dir, ignore_errors=True)
    os.makedirs(staging_dir)

    droid_xml = fetch(DROID_URL.format(version))
    puids = puids_from_droid(droid_xml)
    LOGGER.info('DROID signature file v%d lists %d formats', version, len(puids))
    with open(os.path.join(staging_dir, DROID_NAME.format(version)), 'wb') as handle:
        handle.write(droid_xml)

    records_dir = os.path.join(version_work, 'records')
    download_records(puids, records_dir, throttle)
    zip_path = os.path.join(staging_dir, PRONOM_ZIP_NAME.format(version))
    write_pronom_zip(puids, records_dir, zip_path)

    formats_path = os.path.join(staging_dir, FORMATS_NAME.format(version))
    converted = convert_to_fido(zip_path, formats_path)
    LOGGER.info('Converted %d PRONOM formats to fido signatures', converted)
    if converted != len(puids):
        shutil.rmtree(staging_dir)
        raise PronomError('Converted {} formats but the DROID file lists {}'.format(converted, len(puids)))
    for puid, error in uncompilable_signatures(formats_path):
        LOGGER.warning('%s has a signature fido cannot compile (%s); fido will not match it by signature', puid, error)

    if os.path.isdir(release_dir):
        shutil.rmtree(release_dir)
    os.replace(staging_dir, release_dir)
    return release_dir


def main(args=None):
    """CLI entrypoint. Exit 0 on a new release, 2 if it already exists, 1 on PRONOM failure."""
    parser = ArgumentParser(description='Generate a fidosigs format signature release from PRONOM')
    parser.add_argument('--format-dir', required=True, help='Directory holding the vNNN release directories')
    parser.add_argument('--work-dir', default='.work', help='Download cache, reused to resume interrupted runs')
    parser.add_argument('--version', type=int, default=None, help='PRONOM version to build (default: latest)')
    parser.add_argument('--throttle', type=float, default=DEFAULT_THROTTLE, help='Seconds between PRONOM record requests')
    parser.add_argument('--force', action='store_true', help='Rebuild a release that already exists')
    parsed = parser.parse_args(args)

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # fido.prepare fetches reference file URLs with no timeout of its own.
    socket.setdefaulttimeout(DEFAULT_TIMEOUT)
    try:
        release_dir = generate(parsed.format_dir, parsed.work_dir, parsed.version, parsed.throttle, parsed.force)
    except ReleaseExistsError as error:
        LOGGER.info('%s', error)
        return 2
    except PronomError as error:
        LOGGER.error('%s', error)
        return 1
    LOGGER.info('Release written to %s', release_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
