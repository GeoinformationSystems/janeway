__copyright__ = "Copyright 2023 Birkbeck, University of London"
__author__ = "Joseph Muller"
__license__ = "AGPL v3"
__maintainer__ = "Open Library of Humanities"

import os
from re import search
import time
import requests
from urllib.parse import urlparse, urljoin

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from journal import models as journal_models
from repository import models as repository_models
from cms import models as models
from bs4 import BeautifulSoup, NavigableString, Tag
from django.core.files.base import ContentFile
from press import models as press_models
from utils.logger import get_logger

import json

logger = get_logger(__name__)

SITE_SEARCH_PATH = os.path.join(
    settings.MEDIA_ROOT,
    'press',
    settings.SITE_SEARCH_DIR,
)

search_documents = []
fetched_urls = set()


def add_search_index_document(url, name, text):
    """
    Adds the data for a part or page to the search index
    """
    data = {}
    data['id'] = len(search_documents)
    data['url'] = url
    data['name'] = name
    data['text'] = text
    search_documents.append(data)


def gobble_sibling_text(sibling, original_part):
    """
    Recursively collects a series of parallel elements for
    indexing together, stopping before another heading
    """

    # Handle no sibling
    if not sibling:
        return ''

    # Handle another heading
    if sibling.name == original_part.name:
        return ''
    if isinstance(sibling, Tag) and sibling.find(original_part.name):
        return ''

    # Recursively get sibling text
    if isinstance(sibling, NavigableString):
        sibling_text = sibling
    else:
        sibling_text = sibling.get_text()
    next_sibling_text = gobble_sibling_text(
        sibling.next_sibling,
        original_part
    )

    # Decompose
    if isinstance(sibling, NavigableString):
        sibling.replace_with('')
    else:
        sibling.decompose()

    return sibling_text + ' ' + next_sibling_text


def get_text_for_parent(parent, original_part):
    if not parent:
        return ''

    text = gobble_sibling_text(parent.next_sibling, original_part)
    if len(text.strip()) > len(original_part.get_text().strip()):
        return text
    else:
        return get_text_for_parent(parent.parent, original_part)


def get_text_for_header(part):
    """
    Gets the wrapping section or div
    when given an h2, h3, or h4
    """

    # Try gobbling first
    text = gobble_sibling_text(part.next_sibling, part)
    if len(text.strip()) > 0:
        return text

    # Otherwise go to parent
    return get_text_for_parent(part.parent, part)


def add_part_as_doc(part, part_url, part_name, part_text):
    if not part_name or not part_text:
        # HTML tree with id has no heading
        # or no text content
        return
    add_search_index_document(part_url, part_name, part_text)
    part.decompose()


def add_searchable_page_parts(url, body):

    headings = ['h4', 'h3', 'h2']

    for h in headings:
        for part in body.find_all(h, id=True):
            try:
                part_id = part['id']
                part_url = urljoin(url, '#' + part_id)
                part_name = part.get_text().strip()
                part_text = get_text_for_header(part)
                add_part_as_doc(part, part_url, part_name, part_text)
            except TypeError:
                continue

    for part in body.find_all(id=True):
        try:
            part_id = part['id']
            part_url = urljoin(url, '#' + part_id)
            part_name = ''
            for h in headings:
                if part.find(h):
                    part_name = part.find(h).get_text().strip()
            part_text = part.get_text()
            add_part_as_doc(part, part_url, part_name, part_text)
        except TypeError:
            continue


def get_page(url):
    time.sleep(.1)
    try:
        fetched_urls.add(url)
        headers = {
            'Accept': 'text/html; charset=utf-8'
        }
        response = requests.get(url, headers=headers)
    except requests.exceptions.ConnectionError:
        logger.warn(f'Could not access {url}')
        logger.warn('Please run server to index site search')
        return

    if response.status_code != 200:
        return

    if 'text/html' not in response.headers['Content-Type']:
        return

    return BeautifulSoup(response.text, 'html.parser')


def get_name(html):
    try:
        return html.find('h1').get_text().strip()
    except AttributeError:
        return html.title.get_text().strip()


def get_body(html):
    body = html.find('body')
    if not isinstance(body, Tag):
        return
    for non_content_selector in ['script', '.djdt-hidden']:
        for element in body.select(non_content_selector):
            element.decompose()
    return body


def get_base():
    scheme = 'http' if settings.DEBUG else 'https'
    press = press_models.Press.objects.first()
    return f'{scheme}://{press.domain}'


def excluded_urls():
    return [
        journal.site_url() for journal in journal_models.Journal.objects.all()
    ] + [
        repo.site_url() for repo in repository_models.Repository.objects.all()
    ]


def url_in_scope(deeper_url):
    base = get_base()
    if urlparse(deeper_url).hostname != urlparse(base).hostname:
        return False
    for excluded_url in excluded_urls():
        if excluded_url in deeper_url:
            return False
    return True


def url_is_unique(deeper_url):
    def normalize(url):
        fragment = urlparse(url).fragment
        url = url.replace('#' + fragment, '')
        if url.endswith('/'):
            url = url[:-1]
        return url

    if deeper_url in fetched_urls:
        return False
    for fetched_url in fetched_urls:
        if normalize(fetched_url) == normalize(deeper_url):
            return False
    return True


def decompose_non_content_page_regions(body):
    for non_content_selector in ['header', 'footer', 'h1']:
        for element in body.select(non_content_selector):
            element.decompose()


def add_searchable_page(url):

    html = get_page(url)
    if not html:
        return

    body = get_body(html)
    if not body:
        return

    for anchor in body.find_all('a'):
        href = anchor.get('href', '').strip()
        deeper_url = urljoin(url, href)
        if url_in_scope(deeper_url) and url_is_unique(deeper_url):
            add_searchable_page(deeper_url)

    decompose_non_content_page_regions(body)

    add_searchable_page_parts(url, body)
    name = get_name(html)
    add_search_index_document(url, name, body.get_text())


def get_press_site_search_data():
    """
    Generates data for press-level site search index
    to be used by MiniSearch
    """

    base = get_base()
    add_searchable_page(base)

    if not len(search_documents) > 0:
        logger.error('Search data store is empty')

    return search_documents


def update_search_data(press_id=1):
    press = press_models.Press.objects.get(pk=press_id)
    docs_filename = os.path.join(
        settings.SITE_SEARCH_DIR,
        f'_press_{ press.pk }_documents.json'
    )
    docs_file, created = models.MediaFile.objects.get_or_create(
        label=docs_filename
    )
    if not created:
        docs_file.unlink()

    documents = get_press_site_search_data()
    docs_json = json.dumps(documents, separators=(',', ':'))

    content_file = ContentFile(docs_json.encode('utf-8'))
    docs_file.file.save(docs_filename, content_file, save=True)
    return docs_file


def delete_search_data(press_id=1):
    press = press_models.Press.objects.get(pk=press_id)
    files_deleted = []
    path = os.path.join(
        SITE_SEARCH_PATH,
        f'_press_{ press.pk }_documents.json',
    )
    if os.path.exists(path):
        os.unlink(path)
        files_deleted.append(path)
    if settings.IN_TEST_RUNNER:
        if os.listdir(SITE_SEARCH_PATH):
            logger.warning(
                f'Left-over test files: {os.listdir(SITE_SEARCH_PATH)}'
            )
        else:
            os.rmdir(SITE_SEARCH_PATH)

    return files_deleted


def get_search_data_file(press):
    docs_filename = os.path.join(
        settings.SITE_SEARCH_DIR,
        f'_press_{ press.pk }_documents.json'
    )
    try:
        docs_file = models.MediaFile.objects.get(label=docs_filename)
    except models.MediaFile.DoesNotExist:
        raise ImproperlyConfigured(
            'Site search indexing is turned on, but there is no data file. '
            'Set settings.SITE_SEARCH_INDEXING_FREQUENCY to None to turn off, '
            'or run manage.py generate_site_search_data.'
        )
    return docs_file
