#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FIDO Signature service.

Copyright 2022 The Open Preservation Foundation

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

FastAPI application routes for the FIDO sig service.

Releases are directories named vNNN under the format directory, which is
FIDOSIGS_FORMAT_DIR when set and the image-bundled resources otherwise.
Adding a release is adding a directory; no code or restart is involved.
"""
import logging
import os
import re
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse

FORMAT_DIR_ENV = 'FIDOSIGS_FORMAT_DIR'
BUNDLED_FORMAT_DIR = Path(__file__).parent / 'resources' / 'format'
RELEASE_DIR_PATTERN = re.compile(r'^v(\d+)$')
VERSION_PATTERN = re.compile(r'^v?(\d+)$', re.IGNORECASE)
LATEST = 'latest'
# HEAD as well as GET: uptime monitors and `curl -I` probe with HEAD.
READ_METHODS = ['GET', 'HEAD']
RELEASE_FILE_NAMES = {
    'droid': 'DROID_SignatureFile-v{}.xml',
    'fido': 'formats-v{}.xml',
    'pronom': 'pronom-xml-v{}.zip',
}
# Element names in the version details document, keyed by download action.
RELEASE_ELEMENTS = {'droid': 'droid', 'fido': 'formats', 'pronom': 'pronom'}

LOGGER = logging.getLogger(__name__)


class XMLResponse(Response):
    media_type = 'application/xml'


class TrailingSlashMiddleware:
    """
    Route /path/ exactly as /path.

    fido requests every URL with a trailing slash. Starlette's default answer is
    a redirect built from the request scheme, which behind a TLS-terminating
    proxy is http://, costing clients two extra hops per request.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and len(scope['path']) > 1 and scope['path'].endswith('/'):
            scope = dict(scope, path=scope['path'].rstrip('/') or '/')
        await self.app(scope, receive, send)


APP = FastAPI(title='fidosigs', docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
APP.add_middleware(TrailingSlashMiddleware)


@APP.api_route('/', methods=READ_METHODS, response_class=XMLResponse)
def root() -> XMLResponse:
    """Return a list of the available services as XML."""
    services_xml = Element('services')
    SubElement(services_xml, 'format', url='format')
    return _xml_response(services_xml)


@APP.api_route('/format', methods=READ_METHODS, response_class=XMLResponse)
def formats() -> XMLResponse:
    """Return a list of the available format signature files as XML, oldest first."""
    format_xml = Element('format')
    signatures = SubElement(format_xml, 'signatures')
    for number in available_versions():
        SubElement(signatures, 'signature', version=_release_name(number))
    return _xml_response(format_xml)


@APP.api_route('/format/latest', methods=READ_METHODS, response_class=XMLResponse)
def latest_version() -> XMLResponse:
    """Return the latest available format signature file version number as XML."""
    return _xml_response(Element('signature', version=_release_name(_latest_number())))


@APP.api_route('/format/{version}', methods=READ_METHODS, response_class=XMLResponse)
def version_details(version: str) -> XMLResponse:
    """List the file resources available for a version (NNN or vNNN) as XML."""
    number = resolve_version(version)
    version_xml = Element('signature', version=_release_name(number))
    for action, file_name in RELEASE_FILE_NAMES.items():
        SubElement(version_xml, RELEASE_ELEMENTS[action], url=file_name.format(number))
    return _xml_response(version_xml)


@APP.api_route('/format/{version}/{action}', methods=READ_METHODS, response_class=FileResponse)
def version_collateral(version: str, action: str) -> FileResponse:
    """
    Return a release file for a version (NNN, vNNN or latest).

    Action is one of fido | droid | pronom.
    """
    file_name_template = RELEASE_FILE_NAMES.get(action.lower())
    if file_name_template is None:
        raise HTTPException(status_code=404, detail='Unknown action {}, expected one of {}'.format(
            action, ' | '.join(RELEASE_FILE_NAMES)))
    number = resolve_version(version)
    file_name = file_name_template.format(number)
    path = format_dir() / _release_name(number) / file_name
    if not path.is_file():
        LOGGER.error('Release %s is missing %s', _release_name(number), file_name)
        raise HTTPException(status_code=404, detail='No {} file found for version {}'.format(action, version))
    return FileResponse(path, filename=file_name)


def format_dir() -> Path:
    """Return the directory holding the vNNN release directories."""
    return Path(os.environ.get(FORMAT_DIR_ENV) or BUNDLED_FORMAT_DIR)


def available_versions() -> list[int]:
    """Return the number of every vNNN release directory, ascending."""
    try:
        with os.scandir(format_dir()) as entries:
            return sorted(
                int(match.group(1))
                for entry in entries
                if entry.is_dir() and (match := RELEASE_DIR_PATTERN.match(entry.name))
            )
    except FileNotFoundError:
        LOGGER.error('Format directory %s does not exist', format_dir())
        return []


def resolve_version(version: str) -> int:
    """Return the release number for NNN, vNNN or latest, raising a 404 if there is no such release."""
    if version.lower() == LATEST:
        return _latest_number()
    match = VERSION_PATTERN.match(version)
    if match is None or int(match.group(1)) not in available_versions():
        raise HTTPException(status_code=404, detail='No sig files found for version {}'.format(version))
    return int(match.group(1))


def _latest_number() -> int:
    versions = available_versions()
    if not versions:
        raise HTTPException(status_code=404, detail='No signature releases available')
    return versions[-1]


def _release_name(number: int) -> str:
    return 'v{}'.format(number)


def _xml_response(element: Element) -> XMLResponse:
    return XMLResponse(tostring(element, encoding='utf8', method='xml'))
