# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 Cisco Systems, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Abhishek Raut, Cisco Systems Inc.

import httplib
import unittest

from mock import patch

from quantum.plugins.cisco.db import n1kv_db_v2
from quantum.plugins.cisco.db import n1kv_models_v2
from quantum.plugins.cisco.db import network_db_v2 as cdb
from quantum.plugins.cisco.extensions import n1kv_profile
from quantum.plugins.cisco.n1kv import n1kv_client
from quantum.plugins.cisco.n1kv import n1kv_quantum_plugin
from quantum.tests.unit import test_db_plugin as test_plugin

from quantum import context
import quantum.db.api as db


class FakeResponse(object):

    """
    This object is returned by mocked httplib instead of a normal response.

    Initialize it with the status code, content type and buffer contents
    you wish to return.

    """
    def __init__(self, status, response_text, content_type):
        self.buffer = response_text
        self.status = status
        self.status_int = status
        self.content_type = content_type

    def read(self, *args, **kwargs):
        return self.buffer

    def getheader(self, *args, **kwargs):
        return self.content_type


def _fake_add_dummy_profile_for_test(self, obj):
    """
    Replacement for a function in the N1KV quantum plugin module.

    Since VSM is not available at the time of tests, we have no
    policy profiles. Hence we inject a dummy policy/network profile into the
    port/network object.
    """
    dummy_profile_name = "dummy_profile"
    dummy_tenant_id = "test-tenant"
    if 'port' in obj:
        dummy_profile_id = "00000000-1111-1111-1111-000000000000"
        self._add_policy_profile(dummy_profile_name,
                                 dummy_profile_id,
                                 dummy_tenant_id)
        obj['port'][n1kv_profile.PROFILE_ID] = dummy_profile_id
    elif 'network' in obj:
        profile = {'name': 'dummy_profile',
                   'segment_type': 'vlan',
                   'physical_network': 'phsy1',
                   'segment_range': '3968-4047'}
        self.network_vlan_ranges = {profile[
            'physical_network']: [(3968, 4047)]}
        n1kv_db_v2.sync_vlan_allocations(self.network_vlan_ranges)
        np = n1kv_db_v2.create_network_profile(profile)
        obj['network'][n1kv_profile.PROFILE_ID] = np.id


def _fake_setup_vsm(self):
    """Fake establish Communication with Cisco Nexus1000V VSM."""
    self.agent_vsm = True
    self._poll_policies(event_type="port_profile")


class N1kvPluginTestCase(test_plugin.QuantumDbPluginV2TestCase):

    _plugin_name = ('quantum.plugins.cisco.n1kv.'
                    'n1kv_quantum_plugin.N1kvQuantumPluginV2')

    _default_tenant = "some_tenant"

    DEFAULT_RESP_BODY = ""
    DEFAULT_RESP_CODE = httplib.OK
    DEFAULT_CONTENT_TYPE = ""

    def _make_test_policy_profile(self, id):
        """
        Creates a policy profile record for testing purpose.

        """
        profile = {'id': id,
                   'name': 'TestGrizzlyPP'}
        profile_obj = n1kv_db_v2.create_policy_profile(profile)
        return profile_obj

    def _make_test_profile(self, tenant_id):
        """
        Create a profile record for testing purposes.

        """
        alloc_obj = n1kv_models_v2.N1kvVlanAllocation("foo", 123)
        alloc_obj.allocated = False
        segment_range = "100-900"
        segment_type = 'vlan'
        physical_network = 'phys1'
        profile_obj = n1kv_models_v2.NetworkProfile("test_np",
                                                    segment_type,
                                                    segment_range,
                                                    physical_network)
        session = db.get_session()
        session.add(profile_obj)
        session.flush()
        return profile_obj

    def setUp(self):
        """
        First step is to define an acceptable response from the VSM to
        our requests. This needs to be done BEFORE the setUp() function
        of the super-class is called.

        This default here works for many cases. If you need something
        extra, please define your own setUp() function in your test class,
        and set your DEFAULT_RESPONSE value also BEFORE calling the
        setUp() of the super-function (this one here). If you have set
        a value already, it will not be overwritten by this code.

        """
        if not self.DEFAULT_RESP_BODY:
            self.DEFAULT_RESP_BODY = \
                """<?xml version="1.0" encoding="utf-8"?>
               <set name="events_set">
               <instance name="1" url="/api/hyper-v/events/1">
               <properties>
               <cmd>configure terminal ; port-profile type vethernet grizzlyPP
                   (SUCCESS)
               </cmd>
               <id>42227269-e348-72ed-bdb7-7ce91cd1423c</id>
               <time>1369223611</time>
               <name>grizzlyPP</name>
               </properties>
               </instance>
               <instance name="2" url="/api/hyper-v/events/2">
               <properties>
               <cmd>configure terminal ; port-profile type vethernet havanaPP
                   (SUCCESS)
               </cmd>
               <id>3fc83608-ae36-70e7-9d22-dec745623d06</id>
               <time>1369223661</time>
               <name>havanaPP</name>
               </properties>
               </instance>
               </set>
               """
        # Creating a mock HTTP connection object for httplib. The N1KV client
        # interacts with the VSM via HTTP. Since we don't have a VSM running
        # in the unit tests, we need to 'fake' it by patching the HTTP library
        # itself. We install a patch for a fake HTTP connection class.
        # Using __name__ to avoid having to enter the full module path.
        http_patcher = patch(n1kv_client.httplib.__name__ + ".HTTPConnection")
        FakeHttpConnection = http_patcher.start()
        self.addCleanup(http_patcher.stop)
        # Now define the return values for a few functions that may be called
        # on any instance of the fake HTTP connection class.
        instance = FakeHttpConnection.return_value
        instance.getresponse.return_value = \
            FakeResponse(self.DEFAULT_RESP_CODE,
                         self.DEFAULT_RESP_BODY,
                         'application/xml')
        instance.request.return_value = None

        # Patch some internal functions in a few other parts of the system.
        # These help us move along, without having to mock up even more systems
        # in the background.

        # Return a dummy VSM IP address
        get_vsm_hosts_patcher = patch(n1kv_client.__name__ +
                                      ".Client._get_vsm_hosts")
        fake_get_vsm_hosts = get_vsm_hosts_patcher.start()
        self.addCleanup(get_vsm_hosts_patcher.stop)
        fake_get_vsm_hosts.return_value = ["127.0.0.1"]

        # Return dummy user profiles
        get_cred_name_patcher = patch(cdb.__name__ + ".get_credential_name")
        fake_get_cred_name = get_cred_name_patcher.start()
        self.addCleanup(get_cred_name_patcher.stop)
        fake_get_cred_name.return_value = \
            {"user_name": "admin", "password": "admin_password"}

        # Patch a dummy profile creation into the N1K plugin code. The original
        # function in the plugin is a noop for production, but during test, we
        # need it to return a dummy network profile.
        n1kv_quantum_plugin.N1kvQuantumPluginV2.\
            _add_dummy_profile_only_if_testing = \
            _fake_add_dummy_profile_for_test

        n1kv_quantum_plugin.N1kvQuantumPluginV2._setup_vsm = _fake_setup_vsm

        super(N1kvPluginTestCase, self).setUp(self._plugin_name)
        # Create some of the database entries that we require.
        self.tenant_id = self._default_tenant
        profile_obj = self._make_test_profile(self.tenant_id)
        policy_profile_obj = \
            self._make_test_policy_profile(
                '41548d21-7f89-4da0-9131-3d4fd4e8BBB8')
        # Additional args for create_network(), create_port(), etc.
        self.more_args = {
            "network": {"n1kv:profile_id": profile_obj.id},
            "port": {"n1kv:profile_id": policy_profile_obj.id}
        }

    def test_plugin(self):
        self._make_network('json',
                           'some_net',
                           True,
                           tenant_id=self.tenant_id,
                           set_context=True)

        req = self.new_list_request('networks', params="fields=tenant_id")
        req.environ['quantum.context'] = context.Context('', self.tenant_id)
        res = req.get_response(self.api)
        self.assertEqual(res.status_int, 200)
        body = self.deserialize('json', res)
        self.assertIn('tenant_id', body['networks'][0])


class TestN1kvBasicGet(test_plugin.TestBasicGet,
                       N1kvPluginTestCase):

    def setUp(self):
        """
        Any non-default responses from the VSM required? Set them
        here:
        FakeHTTPConnection.DEFAULT_RESP_BODY = "...."
        FakeHTTPConnection.DEFAULT_RESP_CODE = <num>

        """
        super(TestN1kvBasicGet, self).setUp()


class TestN1kvHTTPResponse(test_plugin.TestV2HTTPResponse,
                           N1kvPluginTestCase):

    def setUp(self):
        """
        Any non-default responses from the VSM required? Set them
        here:
        FakeHTTPConnection.DEFAULT_RESP_BODY = "...."
        FakeHTTPConnection.DEFAULT_RESP_CODE = <num>

        """
        super(TestN1kvHTTPResponse, self).setUp()


class TestN1kvPorts(test_plugin.TestPortsV2,
                    N1kvPluginTestCase):

    def setUp(self):
        """
        Any non-default responses from the VSM required? Set them
        here:
        FakeHTTPConnection.DEFAULT_RESP_BODY = "...."
        FakeHTTPConnection.DEFAULT_RESP_CODE = <num>

        """
        super(TestN1kvPorts, self).setUp()

    def _make_other_tenant_profile(self, tenant_id):
        """
        Underlying test uses other tenant Id for tests: Make profile for it.

        """
        profile_obj = self._make_test_profile(tenant_id)
        policy_profile_obj = self._make_test_policy_profile(
            '41548d21-7f89-4da0-9131-3d4fd4e8BBB9')
        self.more_args = {
            "network": {"n1kv:profile_id": profile_obj.id},
            "port": {"n1kv:profile_id": policy_profile_obj.id}
        }

    def test_create_port_public_network(self):
        # The underlying test function needs a profile for a different tenant.
        self._make_other_tenant_profile("another_tenant")
        super(TestN1kvPorts, self).test_create_port_public_network()

    def test_create_port_public_network_with_ip(self):
        # The underlying test function needs a profile for a different tenant.
        self._make_other_tenant_profile("another_tenant")
        super(TestN1kvPorts, self).test_create_port_public_network_with_ip()

    def test_create_ports_bulk_emulated(self):
        # The underlying test function needs a profile for a different tenant.
        self._make_other_tenant_profile("test-tenant")
        super(TestN1kvPorts,
              self).test_create_ports_bulk_emulated()

    def test_create_ports_bulk_emulated_plugin_failure(self):
        # The underlying test function needs a profile for a different tenant.
        self._make_other_tenant_profile("test-tenant")
        super(TestN1kvPorts,
              self).test_create_ports_bulk_emulated_plugin_failure()

    def test_delete_port_public_network(self):
        self._make_other_tenant_profile("another_tenant")
        super(TestN1kvPorts, self).test_delete_port_public_network()


class TestN1kvNetworks(test_plugin.TestNetworksV2,
                       N1kvPluginTestCase):

    _default_tenant = "somebody_else"  # Tenant-id determined by underlying
                                       # DB-plugin test cases. Need to use this
                                       # one for profile creation

    def setUp(self):
        """
        Any non-default responses from the VSM required? Set them
        here:
        FakeHTTPConnection.DEFAULT_RESP_BODY = "...."
        FakeHTTPConnection.DEFAULT_RESP_CODE = <num>

        """
        super(TestN1kvNetworks, self).setUp()

    def test_update_network_set_not_shared_single_tenant(self):
        # The underlying test function needs a profile for a different tenant.
        profile_obj = self._make_test_profile("test-tenant")
        policy_profile_obj = self._make_test_policy_profile(
            '41548d21-7f89-4da0-9131-3d4fd4e8BBB9')
        self.more_args = {
            "network": {"n1kv:profile_id": profile_obj.id},
            "port": {"n1kv:profile_id": policy_profile_obj.id}
        }
        super(TestN1kvNetworks,
              self).test_update_network_set_not_shared_single_tenant()

    def test_update_network_set_not_shared_multi_tenants_returns_409(self):
        """
        This is mostly a copy of the code from the basic test case. We
        had to create a profile with a special tenant ID somewhere in the
        middle. To do this, we copied the code for now and inserted our
        profile creation in there. Can't create the profile ahead of time,
        since we are passing special values to the _create_port() function
        via object attributes. Need to clean this up a bit to avoid this
        sort of duplication.  FIXIT!   @@@@

        """
        with self.network(shared=True) as network:
            res1 = self._create_port('json',
                                     network['network']['id'],
                                     201,
                                     tenant_id='somebody_else',
                                     set_context=True)
            profile_obj = self._make_test_profile("test-tenant")
            policy_profile_obj = self._make_test_policy_profile(
                '41548d21-7f89-4da0-9131-3d4fd4e8BB99')
            self.more_args = {
                "network": {"n1kv:profile_id": profile_obj.id},
                "port": {"n1kv:profile_id": policy_profile_obj.id}
            }
            res2 = self._create_port('json',
                                     network['network']['id'],
                                     201,
                                     tenant_id=network['network']['tenant_id'],
                                     set_context=True)
            data = {'network': {'shared': False}}
            req = self.new_update_request('networks',
                                          data,
                                          network['network']['id'])
            self.assertEqual(req.get_response(self.api).status_int, 409)
            port1 = self.deserialize('json', res1)
            port2 = self.deserialize('json', res2)
            self._delete('ports', port1['port']['id'])
            self._delete('ports', port2['port']['id'])


class TestN1kvNonDbTest(unittest.TestCase):

    """
    This test class here can be used to test the plugin directly,
    without going through the DB plugin test cases.

    None of the set-up done in N1kvPluginTestCase applies here.

    """
    def setUp(self):
        pass

    def test_foo(self):
        self.assertTrue(1 == 1)

    def test_db(self):
        n1kv_db_v2.initialize()
