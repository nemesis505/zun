#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import pecan
from pecan import rest

from zun.api.controllers import base
from zun.api.controllers import experimental
from zun.api.controllers import link
from zun.api.controllers import v1
from zun.api.controllers import versions


class Version(base.APIBase):
    """An API version representation."""

    fields = (
        'id',
        'links',
        'status',
        'max_version',
        'min_version'
    )

    @staticmethod
    def convert(id, status, max, min):
        version = Version()
        version.id = id
        version.links = [link.make_link('self', pecan.request.host_url,
                                        id, '', bookmark=True)]
        version.status = status
        version.max_version = max
        version.min_version = min
        return version


class Root(base.APIBase):

    fields = (
        'name',
        'description',
        'versions',
        'default_version',
    )

    @staticmethod
    def convert():
        root = Root()
        root.name = "OpenStack Zun API"
        root.description = ("Zun is an OpenStack project which aims to "
                            "provide container management.")

        root.versions = [Version.convert('v1', "CURRENT",
                                         versions.CURRENT_MAX_VER,
                                         versions.BASE_VER)]
        root.default_version = Version.convert('v1', "CURRENT",
                                               versions.CURRENT_MAX_VER,
                                               versions.BASE_VER)
        return root


class RootController(rest.RestController):

    _versions = ['v1', 'experimental']
    """All supported API versions"""

    _default_version = 'v1'
    """The default API version"""

    v1 = v1.Controller()
    experimental = experimental.Controller()

    @pecan.expose('json')
    def get(self):
        # NOTE: The reason why convert() it's being called for every
        #       request is because we need to get the host url from
        #       the request object to make the links.
        return Root.convert()

    @pecan.expose()
    def _route(self, args):
        """Overrides the default routing behavior.

        It redirects the request to the default version of the zun API
        if the version number is not specified in the url.
        """

        if args[0] and args[0] not in self._versions:
            args = [self._default_version] + args
        return super(RootController, self)._route(args)
