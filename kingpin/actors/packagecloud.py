# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright 2014 Nextdoor.com, Inc

"""
:mod:`kingpin.actors.packagecloud`
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The packagecloud actor allows you to perform maintenance operations on
repositories hosted by packagecloud.io using their API:

https://packagecloud.io/docs/api

**Required Environment Variables**

:PACKAGECLOUD_ACCOUNT:
  packagecloud account name, i.e. https://packagecloud.io/PACKAGECLOUD_ACCOUNT

:PACKAGECLOUD_TOKEN:
  packagecloud API Token
"""

import datetime
import logging
import os
import re
import sys

from tornado import gen
from tornado_rest_client import api

from kingpin.actors import base
from kingpin.actors import exceptions
from kingpin.constants import REQUIRED

log = logging.getLogger(__name__)

__author__ = 'Charles McLaughlin <charles@nextdoor.com>'

ACCOUNT = os.getenv('PACKAGECLOUD_ACCOUNT', None)
TOKEN = os.getenv('PACKAGECLOUD_TOKEN', None)


class PackagecloudAPI(api.RestConsumer):

    ENDPOINT = 'https://packagecloud.io/api/v1/'
    CONFIG = {
        'attrs': {
            'packages': {
                'path': ('repos/%account%/%repo%/packages.json'
                         '?per_page={}'.format(sys.maxint)),
                'http_methods': {'get': {}}
            },
            'delete': {
                'path': 'repos/%account%/%repo%/%distro_version%/%filename%',
                'http_methods': {'delete': {}}
            },
        },
        'auth': {
            'user': TOKEN,
            'pass': ''
        }
    }


class PackagecloudBase(base.BaseActor):

    """Simple packagecloud Abstract Base Object"""

    def __init__(self, *args, **kwargs):
        """Check required environment variables."""
        super(PackagecloudBase, self).__init__(*args, **kwargs)

        if not ACCOUNT:
            raise exceptions.InvalidCredentials(
                'Missing the "PACKAGECLOUD_ACCOUNT" environment variable.')

        if not TOKEN:
            raise exceptions.InvalidCredentials(
                'Missing the "PACKAGECLOUD_TOKEN" environment variable.')

        rest_client = api.RestClient()
        self._packagecloud_client = PackagecloudAPI(client=rest_client)

    @gen.coroutine
    def _get_all_packages(self, repo):
        """Simple method for fetching a dictionary of all packages in a repo

        Args:
            repo: name of the packagecloud repo to fetch from

        Returns:
            A hash of the packages.
        """
        all_packages = yield self._packagecloud_client.packages(
            token=TOKEN, account=ACCOUNT, repo=repo).http_get()
        raise gen.Return(all_packages)

    def _get_package_versions(self, name, all_packages):
        """Find all versions of a given package.

        Args:
            name: name of the package to look for
            all_packages: hash of all the packages, as returned by the API

        Returns:
            A hash of package versions sorted by creation date
        """
        versions = [{
            'created_at': datetime.datetime.strptime(
                package['created_at'], '%Y-%m-%dT%H:%M:%S.%fZ'),
            'distro_version': package['distro_version'],
            'filename': package['package_html_url'].split('/')[-1],
            'name': package['name']
        } for package in all_packages if package['name'] == name]

        versions.sort(key=lambda x: x.get('created_at'), reverse=True)
        return versions

    @gen.coroutine
    def _get_packages_list_to_delete(self, packages_to_delete, all_packages):
        """Extracts a list of unique package names to delete

        Args:
            packages_to_delete: regex of package names to delete
            all_packages: hash of all the packages, as returned by the API

        Returns:
            A list of unique package names that match the delete pattern.
            """
        pattern = re.compile(packages_to_delete)
        packages_list_to_delete = [package['name'] for package in all_packages
                                   if pattern.match(package['name'])]
        packages_list_to_delete = set(packages_list_to_delete)

        self.log.debug('List of packages matching regex (%s): %s' %
                       (packages_to_delete, packages_list_to_delete))

        raise gen.Return(packages_list_to_delete)

    @gen.coroutine
    def _delete(self, packages_to_delete, repo, older_than=0,
                number_to_keep=0):
        """Generic packagecloud delete method, optionally supporting deleting
        old packages by date and/or keeping a certain number of packages.

        Args:
            packages_to_delete: Regex of packages to delete, e.g. pkg1|pkg2
            repo: name of the packagecloud repo to delete from
            older_than: Delete packages created before this number of seconds
            number_to_keep: Keep at least this number of each package

        Returns:
            A list of the packages that were deleted
        """
        all_packages = yield self._get_all_packages(repo=repo)
        packages_list_to_delete = yield self._get_packages_list_to_delete(
            packages_to_delete, all_packages)
        deleted_packages = []

        # Loop through each unique package to delete
        for name in packages_list_to_delete:
            package_versions = self._get_package_versions(
                name, all_packages)

            # Delete individual packages if they meet our criteria
            number_in_repo = len(package_versions)
            for package in package_versions:

                # Safety check -- if there aren't more than the number_to_keep
                # in the repo, then don't bother continuing through the loop
                # for this package. Break out and move to the next name in
                # packages_list_to_delete.
                if number_in_repo <= number_to_keep:
                    break

                if older_than:
                    package_age = (datetime.datetime.now() -
                                   package['created_at'])
                    allowed_age = datetime.timedelta(seconds=older_than)
                    if package_age <= allowed_age:
                        continue

                msg = '%s/%s/%s' % (
                    repo, package['distro_version'], package['filename'])
                if self._dry:
                    self.log.info('Would have deleted %s' % msg)
                else:
                    self.log.info('Deleting %s' % msg)

                    yield self._packagecloud_client.delete(
                        token=TOKEN, account=ACCOUNT, repo=repo,
                        distro_version=package['distro_version'],
                        filename=package['filename']
                    ).http_delete()

                # Decrement list of packages to track how many are left
                number_in_repo = number_in_repo - 1
                # Keeping track of removed packages for return value
                deleted_packages.append(package)

        raise gen.Return(deleted_packages)


class Delete(PackagecloudBase):

    """Deletes packages from a PackageCloud repo.

    Searches for packages that match the `packages_to_delete` regex pattern and
    deletes them.  If `number_to_keep` is set, we always at least this number
    of versions of the given package intact in the repo.  Also if
    `number_to_keep` is set, the older versions of a package (based on upload
    time) packages will be deleted first effectively leaving newer packages
    in the repo.

    **Options**

    :number_to_keep:
      Keep at least this number of each package
      (defaults to *0*)

    :packages_to_delete:
      Regex of packages to delete, e.g. pkg1|pkg2

    :repo:
      Which packagecloud repo to delete from

    **Examples**

    .. code-block:: json

      { "desc": "packagecloud Delete example",
        "actor": "packagecloud.Delete",
        "options": {
          "number_to_keep": 10,
          "packages_to_delete": "deleteme",
          "repo": "test"
        }
      }

    """

    all_options = {
        'number_to_keep': (
            int, 0,
            'Keep at least this number of each package'),
        'packages_to_delete': (
            str, REQUIRED,
            'Regex of packages to delete, e.g. pkg1|pkg2'),
        'repo': (
            str, REQUIRED,
            'Which packagecloud repo to delete from'),
    }

    def __init__(self, *args, **kwargs):
        """Check required environment variables."""
        super(Delete, self).__init__(*args, **kwargs)

        try:
            re.compile(self.option('packages_to_delete'))
        except re.error:
            raise exceptions.InvalidOptions(
                'packages_to_delete is an invalid regex')

    @gen.coroutine
    def _execute(self):
        """Deletes all packages that match the `packages_to_delete` pattern"""
        deleted_packages = yield self._delete(
            packages_to_delete=self.option('packages_to_delete'),
            number_to_keep=self.option('number_to_keep'),
            repo=self.option('repo'))
        raise gen.Return(deleted_packages)


class DeleteByDate(PackagecloudBase):

    """Deletes packages from a PackageCloud repo older than X.

    Adds additional functionality to the `Delete` class with a `older_than`
    option.  Only packages older than that number of seconds will be deleted.

    **Options**

    :number_to_keep:
      Keep at least this number of each package
      (defaults to *0*)

    :older_than:
      Delete packages created before this number of seconds

    :packages_to_delete:
      Regex of packages to delete, e.g. pkg1|pkg2

    :repo:
      Which packagecloud repo to delete from

    **Examples**

    .. code-block:: json

      { "desc": "packagecloud DeleteByDate example",
        "actor": "packagecloud.DeleteByDate",
        "options": {
          "number_to_keep": 10,
          "older_than": 600,
          "packages_to_delete": "deleteme",
          "repo": "test"
        }
      }

    """

    all_options = {
        'number_to_keep': (
            int, 0,
            'Keep at least this number of each package'),
        'older_than': (
            int, REQUIRED,
            'Delete packages created before this number of seconds'),
        'packages_to_delete': (
            str, REQUIRED,
            'Regex of packages to delete, e.g. pkg1|pkg2'),
        'repo': (str, REQUIRED,
                 'Which packagecloud repo to delete from')
    }

    @gen.coroutine
    def _execute(self):
        deleted_packages = yield self._delete(
            packages_to_delete=self.option('packages_to_delete'),
            number_to_keep=self.option('number_to_keep'),
            older_than=self.option('older_than'),
            repo=self.option('repo'))
        raise gen.Return(deleted_packages)


class WaitForPackage(PackagecloudBase):

    """Searches for a package that matches `name` and `version` until found or
    a timeout occurs.

    **Options**

    :name:
      Name of the package to search for as a regex

    :version:
      Version of the package to search for as a regex

    :repo:
      Which packagecloud repo to delete from

    :sleep:
      Number of seconds to sleep for between each search

    **Examples**

    .. code-block:: json

      { "desc": "packagecloud WaitForPackage example",
        "actor": "packagecloud.WaitForPackage",
        "options": {
          "name": "findme",
          "version": "0.1",
          "repo": "test",
          "sleep": 10,
        }
      }

    """

    all_options = {
        'name': (
            str, REQUIRED, 'Name of the package to search for as a regex'),
        'version': (
            str, '.*', 'Version of the package to search for as a regex'),
        'repo': (
            str, REQUIRED, 'Which packagecloud repo to search'),
        'sleep': (
            int, 10, 'Number of seconds to sleep for between each search')
    }

    def __init__(self, *args, **kwargs):
        """Check required environment variables."""
        super(WaitForPackage, self).__init__(*args, **kwargs)

        try:
            re.compile(self.option('name'))
        except re.error:
            raise exceptions.InvalidOptions(
                'name is an invalid regex')

        try:
            re.compile(self.option('version'))
        except re.error:
            raise exceptions.InvalidOptions(
                'version is an invalid regex')

    @gen.coroutine
    def _search(self, repo, name, version):
        """Searches for a given package until found or a timeout occurs.

        Args:
            repo: name of the repo to search
            name: Name of the package to search for as a regex
            version: Version of the package to search for as a regex

        Returns:
            A list of the packages that were found
        """

        all_packages = yield self._get_all_packages(repo=repo)
        self.log.debug('Found all packages: %s' % all_packages)

        name_pattern = re.compile(name)
        version_pattern = re.compile(version)

        matched_packages = [p for p in all_packages
                            if name_pattern.match(p['name']) and
                            version_pattern.match(p['version'])]

        raise gen.Return(matched_packages)

    @gen.coroutine
    def _execute(self):
        """Execute method for the WaitForPackage actor"""
        while True:
            self.log.info('Searching for %s %s...' %
                          (self.option('name'), self.option('version')))

            matched_packages = yield self._search(
                repo=self.option('repo'),
                name=self.option('name'),
                version=self.option('version'))

            if len(matched_packages) > 0:
                self.log.info('Found it!')
                raise gen.Return(matched_packages)

            yield gen.sleep(self.option('sleep'))
