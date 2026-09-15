"""Tests for scripts/generate_signatures.py, with PRONOM replaced by recorded fixtures."""
import io
import os
import urllib.error
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

import generate_signatures as generator

FIXTURES = Path(__file__).parent / 'fixtures'
DROID_FIXTURE = FIXTURES / 'DROID_SignatureFile-sample.xml'
SAMPLE_PUIDS = ['x-fmt/2', 'x-fmt/3', 'fmt/3']
RELEASE_FILES = ['DROID_SignatureFile-v109.xml', 'formats-v109.xml', 'pronom-xml-v109.zip']
SOAP_VERSION_RESPONSE = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
    b'<getSignatureFileVersionV1Response xmlns="http://pronom.nationalarchives.gov.uk">'
    b'<Version><Version>125</Version></Version><Deprecated>false</Deprecated>'
    b'</getSignatureFileVersionV1Response></soap:Body></soap:Envelope>'
)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(generator.time, 'sleep', recorded.append)
    return recorded


@pytest.fixture
def pronom(monkeypatch):
    """Serve the DROID fixture and PRONOM record fixtures, recording each requested URL."""
    requested = []
    responses = {generator.DROID_URL.format(109): DROID_FIXTURE.read_bytes()}
    for puid in SAMPLE_PUIDS:
        responses[generator.PUID_URL.format(puid)] = (FIXTURES / 'pronom' / generator.puid_file_name(puid)).read_bytes()

    def fake_fetch(url, data=None, headers=None, **_):
        requested.append(url)
        if url not in responses:
            raise generator.PronomError('unexpected request to {}'.format(url))
        return responses[url]

    monkeypatch.setattr(generator, 'fetch', fake_fetch)
    return requested


def fake_urlopen(monkeypatch, outcomes, seen=None):
    def urlopen(request, timeout):
        if seen is not None:
            seen.append(request)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return io.BytesIO(outcome)

    monkeypatch.setattr(generator.urllib.request, 'urlopen', urlopen)


def test_fetch_returns_body_and_identifies_itself(monkeypatch):
    seen = []
    fake_urlopen(monkeypatch, [b'body'], seen)
    assert generator.fetch('https://example.test/record') == b'body'
    assert seen[0].get_header('User-agent') == generator.USER_AGENT


def test_fetch_retries_transient_failures_with_backoff(monkeypatch, sleeps):
    fake_urlopen(monkeypatch, [urllib.error.URLError('reset'), TimeoutError('slow'), b'body'])
    assert generator.fetch('https://example.test/record') == b'body'
    assert sleeps == [5, 10]


def test_fetch_raises_pronom_error_when_retries_are_exhausted(monkeypatch, sleeps):
    fake_urlopen(monkeypatch, [ConnectionResetError('reset')] * 3)
    with pytest.raises(generator.PronomError, match='after 3 attempts'):
        generator.fetch('https://example.test/record')
    assert sleeps == [5, 10]


def test_fetch_does_not_retry_404(monkeypatch, sleeps):
    not_found = urllib.error.HTTPError('https://example.test/record', 404, 'Not Found', {}, None)
    fake_urlopen(monkeypatch, [not_found])
    with pytest.raises(generator.PronomError, match='404'):
        generator.fetch('https://example.test/record')
    assert sleeps == []


def test_latest_pronom_version_posts_soap_over_https(monkeypatch):
    calls = []

    def fake_fetch(url, data=None, headers=None, **_):
        calls.append((url, data, headers))
        return SOAP_VERSION_RESPONSE

    monkeypatch.setattr(generator, 'fetch', fake_fetch)
    assert generator.latest_pronom_version() == 125
    url, data, headers = calls[0]
    assert url.startswith('https://'), 'PRONOM redirects http POSTs, which urllib will not follow'
    assert b'getSignatureFileVersionV1' in data
    assert headers['SOAPAction'] == '"http://pronom.nationalarchives.gov.uk:getSignatureFileVersionV1In"'


@pytest.mark.parametrize('body, message', [
    (b'<html>maintenance</html', 'not XML'),
    (b'<Envelope><Body/></Envelope>', 'no version number'),
])
def test_parse_version_response_rejects_unusable_responses(body, message):
    with pytest.raises(generator.PronomError, match=message):
        generator.parse_version_response(body)


def test_puids_from_droid_preserves_file_order():
    assert generator.puids_from_droid(DROID_FIXTURE.read_bytes()) == SAMPLE_PUIDS


@pytest.mark.parametrize('body, message', [
    (b'<FFSignatureFile xmlns="http://www.nationalarchives.gov.uk/pronom/SignatureFile"/>', 'no FileFormat'),
    (b'not xml', 'not XML'),
])
def test_puids_from_droid_rejects_unusable_files(body, message):
    with pytest.raises(generator.PronomError, match=message):
        generator.puids_from_droid(body)


@pytest.mark.parametrize('puid, name', [('fmt/1', 'puid.fmt.1.xml'), ('x-fmt/263', 'puid.x-fmt.263.xml')])
def test_puid_file_name_matches_fido_convention(puid, name):
    assert generator.puid_file_name(puid) == name


def test_download_records_skips_records_already_held(tmp_path, pronom, sleeps):
    (tmp_path / 'puid.x-fmt.2.xml').write_bytes(b'<cached/>')
    generator.download_records(SAMPLE_PUIDS, str(tmp_path), throttle=0.5)
    assert pronom == [generator.PUID_URL.format('x-fmt/3'), generator.PUID_URL.format('fmt/3')]
    assert sleeps == [0.5, 0.5], 'throttle applies after each real request only'
    assert (tmp_path / 'puid.x-fmt.2.xml').read_bytes() == b'<cached/>'
    assert sorted(os.listdir(tmp_path)) == sorted(generator.puid_file_name(puid) for puid in SAMPLE_PUIDS)


def test_download_records_rejects_non_xml_without_leaving_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, 'fetch', lambda url, **_: b'<html>error page')
    with pytest.raises(generator.PronomError, match='fmt/3 is not XML'):
        generator.download_records(['fmt/3'], str(tmp_path))
    assert os.listdir(tmp_path) == []


def test_write_pronom_zip_keeps_droid_order(tmp_path):
    for puid in SAMPLE_PUIDS:
        (tmp_path / generator.puid_file_name(puid)).write_bytes(b'<record/>')
    zip_path = tmp_path / 'records.zip'
    generator.write_pronom_zip(SAMPLE_PUIDS, str(tmp_path), str(zip_path))
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == [generator.puid_file_name(puid) for puid in SAMPLE_PUIDS]


def test_generate_builds_a_complete_release_with_fido_conversion(tmp_path, pronom):
    format_dir = tmp_path / 'format'
    release_dir = generator.generate(str(format_dir), str(tmp_path / 'work'), version=109, throttle=0)

    assert release_dir == str(format_dir / 'v109')
    assert sorted(os.listdir(release_dir)) == RELEASE_FILES
    assert os.listdir(format_dir) == ['v109'], 'staging directory must not be left behind'
    assert (format_dir / 'v109' / RELEASE_FILES[0]).read_bytes() == DROID_FIXTURE.read_bytes()
    formats = ElementTree.parse(format_dir / 'v109' / 'formats-v109.xml').getroot()
    assert sorted(element.findtext('puid') for element in formats.iter('format')) == sorted(SAMPLE_PUIDS)


def test_generate_uses_pronom_latest_when_no_version_given(tmp_path, pronom, monkeypatch):
    monkeypatch.setattr(generator, 'latest_pronom_version', lambda: 109)
    assert generator.generate(str(tmp_path / 'format'), str(tmp_path / 'work'), throttle=0).endswith('v109')


def test_generate_refuses_existing_release_before_any_download(tmp_path, pronom):
    (tmp_path / 'format' / 'v109').mkdir(parents=True)
    with pytest.raises(generator.ReleaseExistsError):
        generator.generate(str(tmp_path / 'format'), str(tmp_path / 'work'), version=109)
    assert pronom == []


def test_generate_force_replaces_existing_release(tmp_path, pronom):
    stale = tmp_path / 'format' / 'v109' / 'stale.txt'
    stale.parent.mkdir(parents=True)
    stale.write_text('old')
    generator.generate(str(tmp_path / 'format'), str(tmp_path / 'work'), version=109, throttle=0, force=True)
    assert sorted(os.listdir(stale.parent)) == RELEASE_FILES


def test_generate_resumes_from_cached_records(tmp_path, pronom):
    records = tmp_path / 'work' / 'v109' / 'records'
    records.mkdir(parents=True)
    cached = generator.puid_file_name('fmt/3')
    (records / cached).write_bytes((FIXTURES / 'pronom' / cached).read_bytes())
    generator.generate(str(tmp_path / 'format'), str(tmp_path / 'work'), version=109, throttle=0)
    assert generator.PUID_URL.format('fmt/3') not in pronom


def test_generate_publishes_nothing_when_conversion_loses_formats(tmp_path, pronom, monkeypatch):
    monkeypatch.setattr(generator, 'convert_to_fido', lambda zip_path, formats_path: 2)
    with pytest.raises(generator.PronomError, match='Converted 2 formats but the DROID file lists 3'):
        generator.generate(str(tmp_path / 'format'), str(tmp_path / 'work'), version=109, throttle=0)
    assert os.listdir(tmp_path / 'format') == []


@pytest.mark.parametrize('outcome, exit_code', [
    ('/format/v125', 0),
    (generator.ReleaseExistsError('present'), 2),
    (generator.PronomError('down'), 1),
])
def test_main_exit_codes(tmp_path, monkeypatch, outcome, exit_code):
    def fake_generate(*_):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(generator, 'generate', fake_generate)
    monkeypatch.setattr(generator.socket, 'setdefaulttimeout', lambda _: None)
    assert generator.main(['--format-dir', str(tmp_path)]) == exit_code
