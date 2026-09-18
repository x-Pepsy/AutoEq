# -*- coding: utf-8 -*-
import re
import sys
import urllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
import json
import requests
from tqdm.auto import tqdm
from autoeq.csv import CsvParseError
from autoeq.frequency_response import FrequencyResponse
from autoeq.utils import make_file_name_allowed
from dbtools.crinacle_crawler_base import CrinacleCrawlerBase
ROOT_PATH = Path(__file__).parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(1, str(ROOT_PATH))
from dbtools.name_index import NameIndex, NameItem
from dbtools.crawler import InvalidResponseCodeError
from dbtools.constants import MEASUREMENTS_PATH


_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0'
}

# squig.link database type to AutoEq form
_squig_forms = {
    'IEMs': 'in-ear',
    'Headphones': 'over-ear',
    'Earbuds': 'earbud',
}


_squig_rigs = {
    'Auriculares Argentina': {'in-ear': '711', 'over-ear': 'KB501x + 711'},
    'Bakkwatan': {'in-ear': '711'},
    'DHRME': {'in-ear': '711'},
    'Fahryst': {'in-ear': '711'},
    'Filk': {'in-ear': '711', 'over-ear': 'KB006x + 711'},
    'freeryder05': {'in-ear': '711'},
    'Harpo': {'in-ear': '711'},
    'Hi End Portable': {'in-ear': '711'},
    'Jaytiss': {'in-ear': '711'},
    'Kazi': {'in-ear': '711'},
    'kr0mka': {'in-ear': '711', 'over-ear': 'KB501x + 711'},
    'Kuulokenurkka': {'in-ear': '711', 'over-ear': 'KB501x + 711'},
    'Regan Cipher': {'in-ear': '711', 'over-ear': 'KB500x + 711'},
    'RikudouGoku': {'in-ear': '711'},
    'Super Review': {'in-ear': '711', 'over-ear': 'KB006x + 711', 'earbud': 'KB006x + 711'},
    'Ted\'s Squig Hoard': {'in-ear': '711', 'over-ear': 'KB500x + 711'},
    'ToneDeafMonk': {'in-ear': '711'},
}


class SquigCrawlerManager:
    def __init__(self):
        sites = requests.get('https://squig.link/squigsites.json', headers=_HEADERS).json()
        self._crawlers = [
            SquigCrawler(
                username=site['username'],
                name=make_file_name_allowed(site['name']),
                dbs=site['dbs'],
                url_type=site.get('urlType', 'subdomain'),
            ) for site in sites
        ]

    @property
    def crawlers(self):
        return iter(self._crawlers)

    def crawler(self, name):
        for crawler in self.crawlers:
            if name == crawler.name:
                return crawler

    def run(self, name):
        for crawler in self.crawlers:
            if crawler.name == name:
                crawler.run()
                return crawler
        raise ValueError(f'Unknown squig.link site "{name}"')

    def process(self, name=None, new_only=True):
        for crawler in self.crawlers:
            if name is None or crawler.name == name:
                crawler.process(new_only=new_only)
                if name is not None:
                    return


class SquigCrawler(CrinacleCrawlerBase):
    def __init__(
            self, driver=None, delete_existing_on_prompt=True, redownload=False,
            username=None, name=None, dbs=None, url_type='subdomain'):
        if username is None:
            raise ValueError('name must be given')
        if name is None:
            raise ValueError('name must be given')
        if dbs is None:
            raise ValueError('dbs must be given')
        self.username = username
        self.name = name
        self.dbs = dbs
        self.url_type = url_type
        self.book_maps = None
        self.db_folders = {}
        self._configs = None
        super().__init__(driver=driver, delete_existing_on_prompt=delete_existing_on_prompt, redownload=redownload)

    @property
    def measurements_path(self):
        return MEASUREMENTS_PATH.joinpath(make_file_name_allowed(self.name))

    @property
    def base_url(self):
        if self.url_type == 'root':
            return 'https://squig.link'
        return f'https://{self.username}.squig.link'

    def db_url(self, db):
        return f'{self.base_url}{db["folder"]}data'

    def data_dir_url(self, folder):
        """URL of the data directory for a database folder. Always with trailing slash."""
        return f'{self.base_url}{folder}data/'

    def parse_books(self):
        """Downloads and parses phone books to get names.

        Databases without a phone book (e.g. removed or renamed) are skipped.

        Returns:
            NameIndex
        """
        book_maps = {}
        self.db_folders = {}
        for db in self.dbs:
            form = _squig_forms.get(db['type'])
            if form is None or form in book_maps:
                continue
            try:
                raw = self.download(
                    f'{self.db_url(db)}/phone_book.json',
                    self.measurements_path.joinpath(f'phone_book_{db["type"]}.json'), headers=_HEADERS)
            except InvalidResponseCodeError as err:
                print(f'No phone book for "{self.name}" {db["type"]}: {err}')
                continue
            book_maps[form] = self.parse_book(json.loads(raw.decode('utf-8')))
            self.db_folders[form] = db['folder']
        return book_maps

    def get_config(self, folder):
        """Reads default channels and number of samples from the site's config.js.

        Args:
            folder: Database folder, e.g. "/" or "/headphones/"

        Returns:
            Tuple of channel list (e.g. ["L", "R"]) and number of samples (int or None)
        """
        if self._configs is None:
            self._configs = {}
        if folder in self._configs:
            return self._configs[folder]
        channels, num_samples = ['L', 'R'], None
        try:
            res = requests.get(f'{self.base_url}{folder}config.js', headers=_HEADERS, timeout=30)
            if res.ok:
                match = re.search(r'default_channels\s*=\s*(\[[^\]]*\])', res.text)
                if match:
                    channels = json.loads(match.group(1))
                match = re.search(r'num_samples\s*=\s*(\d+)', res.text)
                if match:
                    num_samples = int(match.group(1))
        except (requests.RequestException, ValueError) as err:
            print(f'Failed to read config for "{self.name}" {folder}: {err}')
        self._configs[folder] = (channels, num_samples)
        return self._configs[folder]

    def known_source_names(self):
        """Maps data directory paths to the set of already known measurement base names.

        Returns:
            Dict of directory path to set of normalized file names
        """
        by_dir = {}
        for item in self.name_index.items:
            if not item.url:
                continue
            path = urllib.parse.urlparse(item.url).path
            parts = path.split('/')
            dir_key = '/'.join(parts[:-1]).rstrip('/')
            name = self.normalize_file_name(urllib.parse.unquote(parts[-1]))
            by_dir.setdefault(dir_key, set()).add(name)
        return by_dir

    def file_name_variants(self, file_name, channels, num_samples):
        """Builds possible data file names for a phone book entry.

        squig.link graph tool loads "{file} {channel}{sample}.txt" when the site
        defines num_samples and "{file} {channel}.txt" otherwise. Both are tried
        because sites mix sample-numbered and plain channel files.
        """
        variants = []
        for channel in channels:
            variants.append(f'{file_name} {channel}.txt')
        if num_samples is not None:
            for channel in channels:
                for sample in range(1, num_samples + 1):
                    variants.append(f'{file_name} {channel}{sample}.txt')
        variants.append(f'{file_name}.txt')
        return variants

    def file_exists(self, url):
        """Checks if a measurement file exists. Falls back to GET if HEAD is not allowed."""
        try:
            res = requests.head(url, headers=_HEADERS, timeout=30, allow_redirects=True)
            if res.status_code in (403, 405, 501):
                res.close()
                res = requests.get(url, headers=_HEADERS, timeout=30, stream=True)
            ok = res.status_code == 200
            res.close()
            return ok
        except requests.RequestException:
            return False

    def locate_files(self, folder, entries, channels, num_samples):
        """Probes the data directory for the measurement files of new phone book entries.

        Args:
            folder: Database folder
            entries: List of (file_name, source_name) tuples
            channels: Channel suffixes to try
            num_samples: Number of samples or None

        Returns:
            List of (file_name, source_name, url) tuples for files that exist
        """
        base_url = self.data_dir_url(folder)
        jobs = []
        for file_name, source_name in entries:
            for variant in self.file_name_variants(file_name, channels, num_samples):
                jobs.append((file_name, source_name, base_url + urllib.parse.quote(variant)))
        found = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            exists = list(tqdm(
                executor.map(lambda job: self.file_exists(job[2]), jobs),
                total=len(jobs), desc=f'{self.name} {folder}', leave=False))
        for job, ok in zip(jobs, exists):
            if ok:
                found.append(job)
        return found

    def source_group_key(self, item):
        return '/'.join(item.url.split('/')[:-1] + [self.normalize_file_name(item.url.split('/')[-1])])

    def crawl(self):
        self.book_maps = self.parse_books()
        self.name_index = self.read_name_index()
        self.crawl_index = NameIndex()
        known = self.known_source_names()
        for form, book in self.book_maps.items():
            folder = self.db_folders.get(form)
            if folder is None:
                continue
            try:
                rig = _squig_rigs[self.name][form]
            except KeyError:
                # No known rig for this form, cannot use these measurements
                continue
            channels, num_samples = self.get_config(folder)
            dir_key = urllib.parse.urlparse(self.data_dir_url(folder)).path.rstrip('/')
            known_names = known.get(dir_key, set())
            entries = [
                (file_name, source_name) for file_name, source_name in book.items()
                if self.normalize_file_name(file_name) not in known_names
            ]
            if not entries:
                continue
            print(f'{self.name} {form}: locating files for {len(entries)} new entries')
            for _, source_name, url in self.locate_files(folder, entries, channels, num_samples):
                item = NameItem(url=url, source_name=source_name, form=form, rig=rig)
                self.resolve(item)
                self.crawl_index.add(item)
        return self.crawl_index

    def raw_data_path(self, item):
        return self.measurements_path.joinpath('raw_data', item.form, make_file_name_allowed(urllib.parse.unquote(item.url.split('/')[-1])))

    def get_item_from_url(self, url):
        index_item = self.name_index.find_one(url=url)
        if index_item is not None:  # Existing item in the name index, ground truth
            item = index_item.copy()
        else:
            item = NameItem(url=url)
        return item

    def guess_name(self, item):
        """Gets intermediate name with false name."""
        name = item.source_name
        if name is None:  # This checks if a known item already exists in name index
            name = self.get_item_from_url(item.url).source_name
        if name is None:  # This looks for a name in the phone book
            source_name = self.url_to_source_name(item.url.split('/')[-1])
            if item.form is not None:  # Form is known, use the phone book
                if item.form in self.book_maps and source_name in self.book_maps[item.form]:
                    name = self.book_maps[item.form][source_name]
            else:  # Form is not know, iterate through all phone books
                for book_map in self.book_maps.values():
                    if source_name in book_map:
                        name = book_map[source_name]
                        break
        if name is None:  # Name still not known, resort to (normalized) file name
            name = self.url_to_source_name(item.url.split('/')[-1])
        if name is not None:
            name = self.normalize_file_name(name)
        return name

    def process_group(self, items, new_only=True):
        if items[0].is_ignored:
            return
        file_path = self.target_path(items[0])
        if file_path is None:
            return
        if new_only and file_path.exists():
            return
        avg_fr = FrequencyResponse(name=items[0].name)
        avg_fr.raw = np.zeros(avg_fr.frequency.shape)
        count = 0
        for item in items:
            try:
                self.download(item.url, self.raw_data_path(item), headers=_HEADERS)
            except InvalidResponseCodeError as err:
                print(f'Failed to download "{item.url}": {str(err)}')
                continue
            try:
                fr = FrequencyResponse.read_csv(self.raw_data_path(item))
            except (CsvParseError, ValueError) as err:
                print(f'Failed to parse "{self.raw_data_path(item)}": {str(err)}')
                continue
            fr.interpolate()
            fr.center()
            avg_fr.raw += fr.raw
            count += 1
        if count == 0:
            print(f'No data found for "{items[0].name}"')
            return
        avg_fr.raw /= count
        Path(file_path.parent).mkdir(exist_ok=True, parents=True)
        avg_fr.write_csv(file_path)
