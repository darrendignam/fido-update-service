"""Tests for the fidosigs FastAPI service, including fido's own update client against it."""
import zipfile
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from fido import versions as fido_versions

from fidosigs import main

RELEASES = (9, 10, 100)
BASE_URL = 'http://testserver/'


def release_body(action, number):
    return '{} v{}'.format(action, number).encode()


@pytest.fixture
def format_dir(tmp_path, monkeypatch):
    for number in RELEASES:
        release = tmp_path / 'v{}'.format(number)
        release.mkdir()
        for action, template in main.RELEASE_FILE_NAMES.items():
            (release / template.format(number)).write_bytes(release_body(action, number))
    (tmp_path / 'extensions').mkdir()
    (tmp_path / 'vnext').mkdir()
    (tmp_path / '.v101.partial').mkdir()
    (tmp_path / 'v200').write_text('a file, not a release directory')
    monkeypatch.setenv(main.FORMAT_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def client(format_dir):
    return TestClient(main.APP, follow_redirects=False)


def parse(response):
    assert response.status_code == 200, '{} -> {} {}'.format(response.url, response.status_code, response.text)
    assert response.headers['content-type'] == 'application/xml', response.headers
    return ElementTree.fromstring(response.content)


def test_root_lists_only_the_format_service(client):
    root = parse(client.get('/'))
    assert [(child.tag, child.get('url')) for child in root] == [('format', 'format')]


@pytest.mark.parametrize('path', ['/format', '/format/'])
def test_formats_lists_release_directories_in_numeric_order(client, path):
    versions = [element.get('version') for element in parse(client.get(path)).iter('signature')]
    assert versions == ['v9', 'v10', 'v100'], 'non-release entries and lexical order must not leak in'


@pytest.mark.parametrize('path', ['/format/latest', '/format/latest/', '/format/LATEST/'])
def test_latest_returns_highest_numbered_release(client, path):
    root = parse(client.get(path))
    assert (root.tag, root.get('version')) == ('signature', 'v100')


def test_new_release_directory_is_served_without_restart(client, format_dir):
    (format_dir / 'v125').mkdir()
    assert parse(client.get('/format/latest/')).get('version') == 'v125'


def test_latest_is_404_when_no_releases_exist(tmp_path, monkeypatch):
    monkeypatch.setenv(main.FORMAT_DIR_ENV, str(tmp_path))
    response = TestClient(main.APP).get('/format/latest/')
    assert response.status_code == 404, response.text


def test_missing_format_directory_serves_404_not_500(tmp_path, monkeypatch):
    monkeypatch.setenv(main.FORMAT_DIR_ENV, str(tmp_path / 'absent'))
    client = TestClient(main.APP)
    assert client.get('/format/latest').status_code == 404
    assert parse(client.get('/format')).find('signatures') is not None


@pytest.mark.parametrize('version', ['v10', '10', 'V10', '10/'])
def test_version_details_accepts_prefixed_and_bare_numbers(client, version):
    root = parse(client.get('/format/{}'.format(version)))
    assert root.get('version') == 'v10'
    assert [(child.tag, child.get('url')) for child in root] == [
        ('droid', 'DROID_SignatureFile-v10.xml'),
        ('formats', 'formats-v10.xml'),
        ('pronom', 'pronom-xml-v10.zip'),
    ]


@pytest.mark.parametrize('version', ['v11', 'abc', 'v', 'v10a', '-10'])
def test_version_details_is_404_for_unknown_versions(client, version):
    response = client.get('/format/{}'.format(version))
    assert response.status_code == 404, '{} -> {}'.format(version, response.status_code)


@pytest.mark.parametrize('path, file_name, body', [
    ('/format/v10/droid', 'DROID_SignatureFile-v10.xml', release_body('droid', 10)),
    ('/format/10/fido/', 'formats-v10.xml', release_body('fido', 10)),
    ('/format/9/pronom/', 'pronom-xml-v9.zip', release_body('pronom', 9)),
    ('/format/latest/pronom', 'pronom-xml-v100.zip', release_body('pronom', 100)),
    ('/format/LATEST/FIDO/', 'formats-v100.xml', release_body('fido', 100)),
])
def test_release_files_download_by_version_and_action(client, path, file_name, body):
    response = client.get(path)
    assert response.status_code == 200, '{} -> {} {}'.format(path, response.status_code, response.text)
    assert response.content == body
    assert 'filename="{}"'.format(file_name) in response.headers['content-disposition']


def test_unknown_action_is_404(client):
    response = client.get('/format/v10/container')
    assert response.status_code == 404
    assert 'droid | fido | pronom' in response.json()['detail']


def test_release_missing_a_file_is_404(client, format_dir):
    (format_dir / 'v10' / 'formats-v10.xml').unlink()
    assert client.get('/format/v10/fido').status_code == 404


@pytest.mark.parametrize('path', ['/docs', '/redoc', '/openapi.json', '/container', '/container/'])
def test_removed_and_internal_endpoints_are_not_exposed(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize('path', ['/format/', '/format/latest/', '/format/v10/', '/format/10/fido/'])
def test_trailing_slash_paths_answer_directly_without_redirect(client, path):
    response = client.get(path)
    assert response.status_code == 200, '{} -> {} location={}'.format(
        path, response.status_code, response.headers.get('location'))


@pytest.mark.parametrize('path, expected_length', [
    ('/', None),
    ('/format/', None),
    ('/format/latest/', None),
    ('/format/v10/', None),
    ('/format/latest/fido/', len(release_body('fido', 100))),
])
def test_head_requests_answer_like_get_without_a_body(client, path, expected_length):
    response = client.head(path)
    assert response.status_code == 200, '{} HEAD -> {}'.format(path, response.status_code)
    assert response.content == b''
    if expected_length is not None:
        assert int(response.headers['content-length']) == expected_length


def test_fido_client_check_and_update_against_service(client, tmp_path, monkeypatch):
    """Drive fido 1.6.1's real update code (fido -sigs check / update) against the service."""
    conf_dir = tmp_path / 'fido' / 'conf'
    conf_dir.mkdir(parents=True)
    versions_file = conf_dir / 'versions.xml'
    versions_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?><versions>'
        '<pronomVersion>9</pronomVersion><pronomSignature>formats-v9.xml</pronomSignature>'
        '<pronomContainerSignature>container-signature-20231127.xml</pronomContainerSignature>'
        '<fidoExtensionSignature>format_extensions.xml</fidoExtensionSignature>'
        '<updateScript>1.6.1</updateScript><updateSite>{}</updateSite></versions>'.format(BASE_URL))
    monkeypatch.setattr(fido_versions.requests, 'get', client.get)
    monkeypatch.setattr(fido_versions.importlib_resources, 'files', lambda _: conf_dir.parent)

    assert fido_versions._version_check('9', BASE_URL) == (True, '100')
    assert fido_versions._version_check('100', BASE_URL) == (False, '100')

    local_versions = fido_versions.LocalVersions(str(versions_file))
    fido_versions._output_details('100', BASE_URL, local_versions)
    for action, template in main.RELEASE_FILE_NAMES.items():
        assert (conf_dir / template.format(100)).read_bytes() == release_body(action, 100)
    updated = fido_versions.LocalVersions(str(versions_file))
    assert (updated.pronom_version, updated.pronom_signature) == ('100', 'formats-v100.xml')


def test_bundled_releases_are_complete():
    releases = [path for path in main.BUNDLED_FORMAT_DIR.iterdir() if main.RELEASE_DIR_PATTERN.match(path.name)]
    assert len(releases) >= 27, 'expected at least v70..v109 bundled, found {}'.format(len(releases))
    for release in releases:
        number = int(main.RELEASE_DIR_PATTERN.match(release.name).group(1))
        expected = {template.format(number) for template in main.RELEASE_FILE_NAMES.values()}
        actual = {path.name for path in release.iterdir()}
        assert actual == expected, '{}: expected {}, found {}'.format(release.name, sorted(expected), sorted(actual))
        for path in release.iterdir():
            assert path.stat().st_size > 0, '{} is empty'.format(path)
        assert zipfile.is_zipfile(release / main.RELEASE_FILE_NAMES['pronom'].format(number)), release.name
