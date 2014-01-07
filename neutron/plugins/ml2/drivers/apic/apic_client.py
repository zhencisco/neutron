# Copyright (c) 2013 Cisco Systems
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# @author: Henry Gessau, Cisco Systems

from collections import namedtuple

import json
import requests
from webob import exc as wexc

from neutron.plugins.ml2.drivers.cisco import exceptions as cexc

# Info about a MO's RN format and container class
MoPath = namedtuple('MoPath', ['container', 'rn_fmt'])

supported_mos = {
    'fvTenant': MoPath(None, 'tn-%s'),
    'fvBD': MoPath('fvTenant', 'BD-%s'),
    'fvRsBd': MoPath('fvAEPg', 'rsbd'),
    'fvSubnet': MoPath('fvBD', 'subnet-[%s]'),
    'fvCtx': MoPath('fvTenant', 'ctx-%s'),
    'fvRsCtx': MoPath('fvBD', 'rsctx'),
    'fvAp': MoPath('fvTenant', 'ap-%s'),
    'fvAEPg': MoPath('fvAp', 'epg-%s'),
    'fvRsProv': MoPath('fvAEPg', 'rsprov-%s'),
    'fvRsCons': MoPath('fvAEPg', 'rscons-%s'),
    'fvRsVmmDomAtt': MoPath('fvAEPg', 'rsvmmDomAtt-[%s]'),

    'vzBrCP': MoPath('fvTenant', 'brc-%s'),
    'vzSubj': MoPath('vzBrCP', 'subj-%s'),
    'vzFilter': MoPath('fvTenant', 'flt-%s'),
    'vzRsFiltAtt': MoPath('vzSubj', 'rsfiltAtt-%s'),
    'vzEntry': MoPath('vzFilter', 'e-%s'),
    'vzInTerm': MoPath('vzSubj', 'intmnl'),
    'vzOutTerm': MoPath('vzSubj', 'outtmnl'),

    'vmmProvP': MoPath(None, 'vmmp-%s'),
    'vmmDomP': MoPath('vmmProvP', 'dom-%s'),

    'infra': MoPath(None, 'infra'),
    'infraNodeP': MoPath('infra', 'nprof-%s'),
    'infraLeafS': MoPath('infraNodeP', 'leaves-%s-typ-%s'),
    'infraNodeBlk': MoPath('infraLeafS', 'nodeblk-%s'),
    'infraRsAccPortP': MoPath('infraNodeP', 'rsaccPortP-[%s]'),
    'infraAccPortP': MoPath('infra', 'accportprof-%s'),
    'infraHPortS': MoPath('infraAccPortP', 'hports-%s-typ-%s'),
    'infraPortBlk': MoPath('infraHPortS', 'portblk-%s'),
    'infraRsAccBaseGrp': MoPath('infraHPortS', 'rsaccBaseGrp'),
    'infraFuncP': MoPath('infra', 'funcprof'),
    'infraAccPortGrp': MoPath('infraFuncP', 'accportgrp-%s'),
    'infraRsAttEntP': MoPath('infraAccPortGrp', 'rsattEntP'),
    'infraAttEntityP': MoPath('infra', 'attentp-%s'),
    'infraRsDomP': MoPath('infraAttEntityP', 'rsdomP-[%s]'),

    'fvnsVlanInstP': MoPath('infra', 'vlanns-%s-%s'),
    'fvnsEncapBlk_vlan': MoPath('fvnsVlanInstP', 'from-%s-to-%s'),
    'fvnsVxlanInstP': MoPath('infra', 'vxlanns-%s'),
    'fvnsEncapBlk_vxlan': MoPath('fvnsVxlanInstP', 'from-%s-to-%s'),
}


class MoClass(object):

    # Note(Henry): Yes, I am using a mutable default argument _inst_cache
    # here. It is not a design flaw, it is exactly what I want: for it to
    # persist for the life of MoClass to cache instances.
    # noinspection PyDefaultArgument
    def __new__(cls, mo_class, _inst_cache={}):
        """Ensure we create only one instance per mo_class."""
        try:
            return _inst_cache[mo_class]
        except KeyError:
            new_inst = super(MoClass, cls).__new__(cls)
            new_inst.__init__(mo_class)
            _inst_cache[mo_class] = new_inst
            return new_inst

    def __init__(self, mo_class):
        global supported_mos
        self.klass = mo_class
        self.container = supported_mos[mo_class].container
        self.rn_fmt = supported_mos[mo_class].rn_fmt
        self.dn_fmt, self.params = self._dn_fmt()

    def _dn_fmt(self):
        """Recursively build the DN format using container and RN.

        Also make a list of the required parameters.
        Note: Call this method only once at init.
        """
        param = [self.klass] if '%s' in self.rn_fmt else []
        if self.container:
            container = MoClass(self.container)
            dn_fmt = '/'.join([container.dn_fmt, self.rn_fmt])
            params = container.params + param
            return dn_fmt, params
        return 'uni/' + self.rn_fmt, param

    def dn(self, *params):
        """Return the distinguished name for a managed object."""
        return self.dn_fmt % params

    @staticmethod
    def ux_name(*params):
        """Name for user-readable display in errors, logs, etc."""
        return ', '.join(params)  # TODO(Henry): something nicer?


def unicode2str(data):
    """
    Recursively convert all unicode strings to byte strings in data.

    This converter is intended for use with data that has been decoded from
    a json stream, so it only recurses into dictionaries and lists.
    """
    if isinstance(data, dict):
        return {unicode2str(k): unicode2str(v) for k, v in data.iteritems()}
    elif isinstance(data, list):
        return [unicode2str(e) for e in data]
    elif isinstance(data, unicode):
        return data.encode('utf-8')
    else:
        return data


def requestdata(request_func):
    """Decorator for REST requests.

    Before:
        Verify there is an authenticated session (logged in to APIC)
    After:
        Verify we got a response and it is HTTP OK.
        Extract the data from the response and return it.
    """
    def wrapper(self, *args, **kwargs):
        if self.client.username and not self.client.authentication:
            raise cexc.ApicSessionNotLoggedIn
        url, data, response = request_func(self, *args, **kwargs)
        if response is None:
            raise cexc.ApicHostNoResponse(url=url)
        if data is None:
            request = url
        else:
            request = '%s, data=%s' % (url, data)
        imdata = unicode2str(response.json()).get('imdata')
        if response.status_code != wexc.HTTPOk.code:
            try:
                err_text = imdata[0]['error']['attributes']['text']
            except (IndexError, KeyError):
                err_text = '[text for APIC error not found]'
            raise cexc.ApicResponseNotOk(request=request,
                                         status_code=response.status_code,
                                         reason=response.reason,
                                         text=err_text)
        if not imdata:
            raise cexc.ApicManagedObjectNoData(request=request)
        return imdata
    return wrapper


class ApicSession(object):

    def __init__(self, client):
        self.client = client
        self.api_base = client.api_base
        self.session = client.session

    @staticmethod
    def _make_data(key, **attrs):
        """Build the body for a msg out of a key and some attributes."""
        return json.dumps({key: {'attributes': attrs}})

    def _api_url(self, api):
        """Create the URL for a simple API."""
        return '%s/%s.json' % (self.api_base, api)

    def _mo_url(self, mo, *args):
        """Create a URL for a MO lookup by DN."""
        dn = mo.dn(*args)
        return '%s/mo/%s.json' % (self.api_base, dn)

    def _qry_url(self, mo):
        """Create a URL for a query lookup by MO class."""
        return '%s/class/%s.json' % (self.api_base, mo.klass)

    # REST requests

    @requestdata
    def get_data(self, request):
        """Retrieve generic data from the server."""
        url = self._api_url(request)
        return url, None, self.session.get(url)

    @requestdata
    def _get_mo(self, mo, *args):
        """Retrieve a MO by DN."""
        url = self._mo_url(mo, *args) + '?query-target=self'
        return url, None, self.session.get(url)

    @requestdata
    def _list_mo(self, mo):
        """Retrieve the list of MOs for a class."""
        url = self._qry_url(mo)
        return url, None, self.session.get(url)

    @requestdata
    def post_data(self, request, data):
        """Post generic data to the server."""
        url = self._api_url(request)
        return url, data, self.session.post(url, data=data)

    @requestdata
    def _post_mo(self, mo, *args, **data):
        """Post data for MO to the server."""
        url = self._mo_url(mo, *args)
        data = self._make_data(mo.klass, **data)
        return url, data, self.session.post(url, data=data)


class MoManager(ApicSession):
    """CRUD operations on APIC Managed Objects."""

    def __init__(self, client, mo_class):
        super(MoManager, self).__init__(client)
        self.client = client
        self.mo = MoClass(mo_class)

    def _mo_names(self, mo_list):
        """Extract a list of just the names of the managed objects."""
        return [mo[self.mo.klass]['attributes']['name'] for mo in mo_list]

    def attr(self, obj, key):
        return obj[0][self.mo.klass]['attributes'][key]

    def _ensure_status(self, obj, status):
        """Ensure that the status of a Managed Object is as expected."""
        if self.attr(obj, 'status') != status:
            name = self.attr(obj, 'name')
            raise cexc.ApicMoStatusChangeFailed(
                mo_class=self.mo.klass, name=name, status=status)

    def _create_prereqs(self, *params):
        if self.mo.container:
            prereq = MoManager(self.client, self.mo.container)
            prereq.create(*(params[0: prereq.mo.dn_fmt.count('%s')]))

    def create(self, *params, **attrs):
        self._create_prereqs(*params)
        try:
            # Use existing object if it's already created
            obj = self.get(*params)
        except cexc.ApicManagedObjectNoData:
            obj = self._post_mo(self.mo, *params, **attrs)
            self._ensure_status(obj, 'created')
        else:
            # MO found. Does the caller want to update some attrs?
            if attrs:
                # OK, but only update attrs that differ
                updated_attrs = {}
                for attr, new_val in attrs.items():
                    old_val = self.attr(obj, attr)
                    if new_val != old_val:
                        updated_attrs[attr] = new_val
                if updated_attrs:
                    obj = self._post_mo(self.mo, *params, **updated_attrs)
        return obj

    def get(self, *params):
        return self._get_mo(self.mo, *params)

    def list_all(self):
        mo_list = self._list_mo(self.mo)
        return self._mo_names(mo_list)

    def update(self, *params, **attrs):
        self.get(*params)  # Raises if not found
        return self._post_mo(self.mo, *params, **attrs)

    def delete(self, *params):
        try:
            self.get(*params)
        except cexc.ApicManagedObjectNoData:
            return True
        obj = self._post_mo(self.mo, *params, status='deleted')
        self._ensure_status(obj, 'deleted')
        return obj


class RestClient(ApicSession):
    """
    APIC REST client class.

    Attributes:
        api_base        e.g. 'http://10.2.3.45:8000/api'
        session         The session between client and controller
        authentication  Login info. None if not logged in to controller.
    """

    def __init__(self, host, port=80, usr=None, pwd=None, api='api',
                 ssl=False):
        """Establish a session with the APIC."""
        protocol = ssl and 'https' or 'http'
        self.api_base = '%s://%s:%s/%s' % (protocol, host, port, api)
        self.session = requests.Session()

        # Initialize the session methods
        super(RestClient, self).__init__(self)

        # Log in
        self.authentication = None
        self.username = None
        if usr and pwd:
            self.login(usr, pwd)

        # Supported objects
        for mo_class in supported_mos:
            self.__dict__[mo_class] = MoManager(self, mo_class)

    def login(self, usr, pwd):
        """Log in to server. Save user name and authentication."""
        name_pwd = self._make_data('aaaUser', name=usr, pwd=pwd)
        self.authentication = self.post_data('aaaLogin', data=name_pwd)
        login_data = self.authentication[0]
        if login_data and 'error' in login_data:
            self.authentication = None
            raise cexc.ApicLoginFailed(user=usr)
        self.username = usr
        return self.authentication

    def logout(self):
        """End session with server."""
        if not self.username:
            self.authentication = None
        if self.authentication:
            data = self._make_data('aaaUser', name=self.username)
            try:
                self.post_data('aaaLogout', data=data)
            except cexc.ApicManagedObjectNoData:
                # expected for aaaLogout
                pass
            self.authentication = None