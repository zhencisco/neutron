# vim: tabstop=4 shiftwidth=4 softtabstop=4
# Copyright 2011 Nicira Networks, Inc.
# All Rights Reserved.
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
# @author: Aaron Rosen, Nicira Networks, Inc.
# @author: Bob Kukura, Red Hat, Inc.
# @author: Aruna Kushwaha, Cisco Systems Inc.
# @author: Abhishek Raut, Cisco Systems Inc.
# @author: Rudrajit Tapadar, Cisco Systems Inc.
# @author: Sergey Sudakovich, Cisco Systems Inc.


import logging
import re

from sqlalchemy.orm import exc
from sqlalchemy.sql import and_

from quantum.common import exceptions as q_exc
from quantum.db import models_v2
from quantum.extensions import securitygroup as ext_sg
from quantum import manager
import quantum.db.api as db
from quantum.plugins.cisco.common import cisco_constants as const
from quantum.plugins.cisco.db import n1kv_models_v2
from quantum.plugins.cisco.common import cisco_exceptions as c_exc
from quantum.plugins.cisco.common import config as conf
from quantum.api.v2.attributes import _validate_ip_address

LOG = logging.getLogger(__name__)


def initialize():
    db.configure_db()


def get_network_binding(session, network_id):
    session = session or db.get_session()
    try:
        binding = (session.query(n1kv_models_v2.N1kvNetworkBinding).
                   filter_by(network_id=network_id).
                   one())
        return binding
    except exc.NoResultFound:
        return


def add_network_binding(session, network_id, network_type,
                        physical_network, segmentation_id,
                        multicast_ip, profile_id):
    """
    Explanation for the parameters

    network_type : Whether its a VLAN or VXLAN based network
    physical_network : Only applicable for VLAN networks. It represents a
                       L2 Domain
    segmentation_id : VLAN / VXLAN ID
    multicast IP : VXLAN technology needs a multicast IP to be associated
                   with every VXLAN ID to deal with broadcast packets. A
                   single Multicast IP can be shared by multiple VXLAN IDs.
    profile_id : Network Profile ID based by which this network is created
    """
    with session.begin(subtransactions=True):
        binding = n1kv_models_v2.N1kvNetworkBinding(network_id,
                                                    network_type,
                                                    physical_network,
                                                    segmentation_id,
                                                    multicast_ip,
                                                    profile_id)
        session.add(binding)


def get_port_binding(session, port_id):
    session = session or db.get_session()
    try:
        binding = (session.query(n1kv_models_v2.N1kvPortBinding).
                   filter_by(port_id=port_id).
                   one())
        return binding
    except exc.NoResultFound:
        return


def add_port_binding(session, port_id, profile_id):
    with session.begin(subtransactions=True):
        binding = n1kv_models_v2.N1kvPortBinding(port_id, profile_id)
        session.add(binding)


def sync_vlan_allocations(network_vlan_ranges):
    """Synchronize vlan_allocations table with configured VLAN ranges"""

    session = db.get_session()
    with session.begin():
        # process vlan ranges for each physical network separately
        for physical_network, vlan_ranges in network_vlan_ranges.iteritems():

            # determine current configured allocatable vlans for this
            # physical network
            vlan_ids = set()
            for vlan_range in vlan_ranges:
                vlan_ids |= set(xrange(vlan_range[0], vlan_range[1] + 1))

            # add missing allocatable vlans to table
            for vlan_id in sorted(vlan_ids):
                try:
                    alloc = (session.query(n1kv_models_v2.N1kvVlanAllocation).
                             filter_by(physical_network=physical_network).
                             filter_by(vlan_id=vlan_id).one())
                except exc.NoResultFound:
                    alloc = n1kv_models_v2.N1kvVlanAllocation(physical_network,
                                                              vlan_id)
                    session.add(alloc)


def delete_vlan_allocations(network_vlan_ranges):
    """Delete vlan_allocations for deleted network profile range"""

    session = db.get_session()
    with session.begin():
        # process vlan ranges for each physical network separately
        for physical_network, vlan_ranges in network_vlan_ranges.iteritems():
            # Determine the set of vlan ids which need to be deleted.
            vlan_ids = set()
            for vlan_range in vlan_ranges:
                vlan_ids |= set(xrange(vlan_range[0], vlan_range[1] + 1))

            allocs = (session.query(n1kv_models_v2.N1kvVlanAllocation).
                      filter_by(physical_network=physical_network).
                      all())
            for alloc in allocs:
                if alloc.vlan_id in vlan_ids:
                    if not alloc.allocated:
                        LOG.debug("removing vlan %s on physical network "
                                  "%s from pool" %
                                 (alloc.vlan_id, physical_network))
                        session.delete(alloc)


def get_vlan_allocation(physical_network, vlan_id):
    session = db.get_session()
    try:
        alloc = (session.query(n1kv_models_v2.N1kvVlanAllocation).
                 filter_by(physical_network=physical_network,
                           vlan_id=vlan_id).
                 one())
        return alloc
    except exc.NoResultFound:
        return


def reserve_vlan(session, profile):
    seg_min, seg_max = profile.get_segment_range(session)
    segment_type = 'vlan'

    with session.begin(subtransactions=True):
        alloc = (session.query(n1kv_models_v2.N1kvVlanAllocation).
                 filter(and_(
                        n1kv_models_v2.N1kvVlanAllocation.vlan_id >= seg_min,
                        n1kv_models_v2.N1kvVlanAllocation.vlan_id <= seg_max,
                        n1kv_models_v2.N1kvVlanAllocation.allocated == False)
                        )).first()
        if alloc:
            segment_id = alloc.vlan_id
            physical_network = alloc.physical_network
            alloc.allocated = True
            return (physical_network, segment_type, segment_id, '0.0.0.0')
        raise q_exc.NoNetworkAvailable()


def reserve_vxlan(session, profile):
    seg_min, seg_max = profile.get_segment_range(session)
    segment_type = 'vxlan'
    physical_network = ""

    with session.begin(subtransactions=True):
        alloc = (session.query(n1kv_models_v2.N1kvVxlanAllocation).
                 filter(and_(
                        n1kv_models_v2.N1kvVxlanAllocation.vxlan_id >=
                        seg_min,
                        n1kv_models_v2.N1kvVxlanAllocation.vxlan_id <=
                        seg_max,
                        n1kv_models_v2.N1kvVxlanAllocation.allocated == False)
                        ).first())
        if alloc:
            segment_id = alloc.vxlan_id
            alloc.allocated = True
            return (physical_network, segment_type,
                    segment_id, profile.get_multicast_ip(session))
        raise q_exc.NoNetworkAvailable()


def alloc_network(session, profile_id):
    with session.begin(subtransactions=True):
        try:
            profile = get_network_profile(profile_id)
            if profile:
                if profile.segment_type == 'vlan':
                    return reserve_vlan(session, profile)
                else:
                    return reserve_vxlan(session, profile)
        except q_exc.NotFound:
            LOG.debug("NetworkProfile not found")


def reserve_specific_vlan(session, physical_network, vlan_id):
    with session.begin(subtransactions=True):
        try:
            alloc = (session.query(n1kv_models_v2.N1kvVlanAllocation).
                     filter_by(physical_network=physical_network,
                               vlan_id=vlan_id).
                     one())
            if alloc.allocated:
                if vlan_id == const.FLAT_VLAN_ID:
                    raise q_exc.FlatNetworkInUse(
                        physical_network=physical_network)
                else:
                    raise q_exc.VlanIdInUse(vlan_id=vlan_id,
                                            physical_network=physical_network)
            LOG.debug("reserving specific vlan %s on physical network %s "
                      "from pool" % (vlan_id, physical_network))
            alloc.allocated = True
        except exc.NoResultFound:
            LOG.debug("reserving specific vlan %s on physical network %s "
                      "outside pool" % (vlan_id, physical_network))
            alloc = n1kv_models_v2.N1kvVlanAllocation(physical_network,
                                                      vlan_id)
            alloc.allocated = True
            session.add(alloc)


def release_vlan(session, physical_network, vlan_id, network_vlan_ranges):
    with session.begin(subtransactions=True):
        try:
            alloc = (session.query(n1kv_models_v2.N1kvVlanAllocation).
                     filter_by(physical_network=physical_network,
                               vlan_id=vlan_id).
                     one())
            alloc.allocated = False
            inside = False
            for vlan_range in network_vlan_ranges.get(physical_network, []):
                if vlan_id >= vlan_range[0] and vlan_id <= vlan_range[1]:
                    inside = True
                    break
            LOG.debug("releasing vlan %s on physical network %s %s pool" %
                      (vlan_id, physical_network,
                       inside and "to" or "outside"))
        except exc.NoResultFound:
            LOG.warning("vlan_id %s on physical network %s not found" %
                        (vlan_id, physical_network))


def sync_vxlan_allocations(vxlan_id_ranges):
    """Synchronize vxlan_allocations table with configured vxlan ranges"""

    vxlan_ids = set()
    for vxlan_id_range in vxlan_id_ranges:
        tun_min, tun_max = vxlan_id_range
        if tun_max + 1 - tun_min > 1000000:
            LOG.error("Skipping unreasonable vxlan ID range %s:%s" %
                      vxlan_id_range)
        else:
            vxlan_ids |= set(xrange(tun_min, tun_max + 1))

    session = db.get_session()
    with session.begin():
        for vxlan_id in sorted(vxlan_ids):
            try:
                alloc = (session.query(n1kv_models_v2.N1kvVxlanAllocation).
                         filter_by(vxlan_id=vxlan_id).one())
            except exc.NoResultFound:
                alloc = n1kv_models_v2.N1kvVxlanAllocation(vxlan_id)
                session.add(alloc)


def delete_vxlan_allocations(vxlan_id_ranges):
    """Delete vxlan_allocations for deleted network profile range"""
    vxlan_ids = set()
    for vxlan_id_range in vxlan_id_ranges:
        tun_min, tun_max = vxlan_id_range
        if tun_max + 1 - tun_min > 1000000:
            LOG.error("Skipping unreasonable vxlan ID range %s:%s" %
                      vxlan_id_range)
        else:
            vxlan_ids |= set(xrange(tun_min, tun_max + 1))

    session = db.get_session()
    with session.begin():
        allocs = (session.query(n1kv_models_v2.N1kvVxlanAllocation).all())
        for alloc in allocs:
            if alloc.vxlan_id in vxlan_ids:
                if not alloc.allocated:
                    LOG.debug("removing vxlan %s from pool" %
                              alloc.vxlan_id)
                    session.delete(alloc)


def get_vxlan_allocation(vxlan_id):
    session = db.get_session()
    try:
        alloc = (session.query(n1kv_models_v2.N1kvVxlanAllocation).
                 filter_by(vxlan_id=vxlan_id).
                 one())
        return alloc
    except exc.NoResultFound:
        return


def reserve_specific_vxlan(session, vxlan_id):
    with session.begin(subtransactions=True):
        try:
            alloc = (session.query(n1kv_models_v2.N1kvVxlanAllocation).
                     filter_by(vxlan_id=vxlan_id).
                     one())
            if alloc.allocated:
                raise c_exc.VxlanIdInUse(vxlan_id=vxlan_id)
            LOG.debug("reserving specific vxlan %s from pool" % vxlan_id)
            alloc.allocated = True
        except exc.NoResultFound:
            LOG.debug("reserving specific vxlan %s outside pool" % vxlan_id)
            alloc = n1kv_models_v2.N1kvVxlanAllocation(vxlan_id)
            alloc.allocated = True
            session.add(alloc)


def release_vxlan(session, vxlan_id, vxlan_id_ranges):
    with session.begin(subtransactions=True):
        try:
            alloc = (session.query(n1kv_models_v2.N1kvVxlanAllocation).
                     filter_by(vxlan_id=vxlan_id).
                     one())
            alloc.allocated = False
            inside = False
            for vxlan_id_range in vxlan_id_ranges:
                if (vxlan_id >= vxlan_id_range[0]
                        and vxlan_id <= vxlan_id_range[1]):
                    inside = True
                    break
            LOG.debug("releasing vxlan %s %s pool" %
                      (vxlan_id, inside and "to" or "outside"))
        except exc.NoResultFound:
            LOG.warning("vxlan_id %s not found" % vxlan_id)


def get_port(port_id):
    session = db.get_session()
    try:
        port = session.query(models_v2.Port).filter_by(id=port_id).one()
    except exc.NoResultFound:
        port = None
    return port


def get_port_from_device(port_id):
    """Get port from database."""
    LOG.debug(_("get_port_with_securitygroups() called:port_id=%s"), port_id)
    session = db.get_session()

    query = session.query(models_v2.Port)
    query = query.filter(models_v2.Port.id == port_id)
    port = query.first()
    if not port:
        return None
    plugin = manager.QuantumManager.get_plugin()
    port_dict = plugin._make_port_dict(port)
    port_dict[ext_sg.SECURITYGROUPS] = []
    port_dict['security_group_rules'] = []
    port_dict['security_group_source_groups'] = []
    port_dict['fixed_ips'] = [ip['ip_address']
                              for ip in port['fixed_ips']]
    return port_dict


def set_port_status(port_id, status):
    session = db.get_session()
    try:
        port = session.query(models_v2.Port).filter_by(id=port_id).one()
        port['status'] = status
        session.merge(port)
        session.flush()
    except exc.NoResultFound:
        raise q_exc.PortNotFound(port_id=port_id)


def get_vxlan_endpoints():
    session = db.get_session()
    try:
        vxlans = session.query(n1kv_models_v2.N1kvVxlanEndpoint).all()
    except exc.NoResultFound:
        return []
    return [{'id': vxlan.id,
             'ip_address': vxlan.ip_address} for vxlan in vxlans]


def _generate_vxlan_id(session):
    try:
        vxlans = session.query(n1kv_models_v2.N1kvVxlanEndpoint).all()
    except exc.NoResultFound:
        return 0
    vxlan_ids = ([vxlan['id'] for vxlan in vxlans])
    if vxlan_ids:
        id = max(vxlan_ids)
    else:
        id = 0
    return id + 1


def add_vxlan_endpoint(ip):
    session = db.get_session()
    try:
        vxlan = (session.query(n1kv_models_v2.N1kvVxlanEndpoint).
                 filter_by(ip_address=ip).one())
    except exc.NoResultFound:
        id = _generate_vxlan_id(session)
        vxlan = n1kv_models_v2.N1kvVxlanEndpoint(ip, id)
        session.add(vxlan)
        session.flush()
    return vxlan


def get_vm_network(profile_id, network_id):
    """Retrieve a vm_network based on profile and network id"""
    session = db.get_session()
    try:
        vm_network = (session.query(n1kv_models_v2.N1kVmNetwork).
                      filter_by(profile_id=profile_id).
                      filter_by(network_id=network_id).one())
        return vm_network
    except exc.NoResultFound:
        return None


def add_vm_network(name, profile_id, network_id, port_count):
    """
    Add a vm_network for a unique combination of network and
    policy profile. All ports having the same policy profile
    on one network will be associated with one vm network.
    Port count represents the number ports on one vm network.
    """
    session = db.get_session()
    try:
        vm_network = (session.query(n1kv_models_v2.N1kVmNetwork).
                      filter_by(name=name).one())
    except exc.NoResultFound:
        with session.begin(subtransactions=True):
            vm_network = n1kv_models_v2.N1kVmNetwork(name,
                                                     profile_id,
                                                     network_id,
                                                     port_count)
            session.add(vm_network)
            session.flush()


def update_vm_network(name, port_count):
    """Updates a vm network with new port count"""
    session = db.get_session()
    try:
        vm_network = (session.query(n1kv_models_v2.N1kVmNetwork).
                      filter_by(name=name).one())
        if port_count:
            vm_network['port_count'] = port_count
        session.merge(vm_network)
        session.flush()
        return vm_network
    except exc.NoResultFound:
        raise c_exc.VMNetworkNotFound(name=name)


def delete_vm_network(profile_id, network_id):
    """Deletes a vm network"""
    session = db.get_session()
    vm_network = get_vm_network(profile_id, network_id)
    with session.begin(subtransactions=True):
        session.delete(vm_network)
        session.query(n1kv_models_v2.N1kVmNetwork).filter_by(
            name=vm_network['name']).delete()
    return vm_network


def create_network_profile(profile):
    """
    Create Network Profile
    """
    LOG.debug("create_network_profile()")
    session = db.get_session()
    with session.begin(subtransactions=True):
        if profile['segment_type'] == 'vlan':
            net_profile = n1kv_models_v2.NetworkProfile(
                name=profile['name'],
                segment_type=profile['segment_type'],
                segment_range=profile['segment_range'],
                physical_network=profile['physical_network'])
        elif profile['segment_type'] == 'vxlan':
            net_profile = n1kv_models_v2.NetworkProfile(
                name=profile['name'],
                segment_type=profile['segment_type'],
                mcast_ip_index=0,
                segment_range=profile['segment_range'],
                mcast_ip_range=profile['multicast_ip_range'])
        session.add(net_profile)
        return net_profile


def delete_network_profile(id):
    """
    Delete Network Profile
    """
    LOG.debug("delete_network_profile()")
    session = db.get_session()
    profile = get_network_profile(id)
    with session.begin(subtransactions=True):
        session.delete(profile)
        session.query(n1kv_models_v2.ProfileBinding).filter(
            n1kv_models_v2.ProfileBinding.profile_id == id).delete()
    return profile


def update_network_profile(id, profile):
    """
    Update Network Profile
    """
    LOG.debug("update_network_profile()")
    session = db.get_session()
    with session.begin(subtransactions=True):
        _profile = get_network_profile(id)
        _profile.update(profile)
        session.merge(_profile)
        return _profile


def get_network_profile(id, fields=None):
    """
    Get Network Profile
    """
    LOG.debug("get_network_profile()")
    session = db.get_session()
    try:
        profile = session.query(n1kv_models_v2.NetworkProfile).filter_by(
            id=id).one()
        return profile
    except exc.NoResultFound:
        raise c_exc.NetworkProfileIdNotFound(profile_id=id)


def get_network_profile_by_name(name):
    """
    Get Network Profile by name.
    """
    LOG.debug("get_network_profile_by_name")
    session = db.get_session()
    try:
        profile = session.query(
            n1kv_models_v2.NetworkProfile).filter_by(name=name).one()
        return profile
    except exc.NoResultFound:
        return None


def _get_network_profiles(**kwargs):
    """
    Get Network Profiles on a particular physical network, if physical
    network is specified. If no physical network is specified, return
    all network profiles.
    """
    session = db.get_session()
    if "physical_network" in kwargs:
        try:
            profiles = session.query(n1kv_models_v2.NetworkProfile).\
                filter_by(physical_network=kwargs[
                          'physical_network']).all()
            return profiles
        except exc.NoResultFound:
            return None
    else:
        return session.query(n1kv_models_v2.NetworkProfile).all()


def create_policy_profile(profile):
    """
    Create Policy Profile
    """
    LOG.debug("create_policy_profile()")
    session = db.get_session()
    with session.begin(subtransactions=True):
        p_profile = n1kv_models_v2.PolicyProfile(id=profile['id'],
                                                 name=profile['name'])
        session.add(p_profile)
        return p_profile


def delete_policy_profile(id):
    """
    Delete Policy Profile
    """
    LOG.debug("delete_policy_profile()")
    session = db.get_session()
    profile = get_policy_profile(id)
    with session.begin(subtransactions=True):
        session.delete(profile)


def update_policy_profile(id, profile):
    """
    Update a policy profile.
    """
    LOG.debug("update_policy_profile()")
    session = db.get_session()
    with session.begin(subtransactions=True):
        _profile = get_policy_profile(id)
        _profile.update(profile)
        session.merge(_profile)
        return _profile


def get_policy_profile(id, fields=None):
    """
    Get Policy Profile
    """
    LOG.debug("get_policy_profile()")
    session = db.get_session()
    try:
        profile = session.query(n1kv_models_v2.PolicyProfile).filter_by(
            id=id).one()
        return profile
    except exc.NoResultFound:
        raise c_exc.PolicyProfileIdNotFound(profile_id=id)


def create_profile_binding(tenant_id, profile_id, profile_type):
    """
    Create Network/Policy Profile association with a tenant.
    """
    if profile_type not in ['network', 'policy']:
        raise q_exc.QuantumException("Invalid profile type")

    if _profile_binding_exists(tenant_id, profile_id, profile_type):
        return get_profile_binding(tenant_id, profile_id)

    session = db.get_session()
    with session.begin(subtransactions=True):
        binding = n1kv_models_v2.ProfileBinding(profile_type=profile_type,
                                                profile_id=profile_id,
                                                tenant_id=tenant_id)
        session.add(binding)
        session.flush()
        return binding


def _profile_binding_exists(tenant_id, profile_id, profile_type):
    LOG.debug("_profile_binding_exists()")
    try:
        binding = _get_profile_binding(tenant_id, profile_id)
        return binding.profile_type == profile_type
    except exc.NoResultFound:
        return False
    except Exception, e:
        LOG.debug("Error in get_profile_binding(): %s" % e)


def _get_profile_binding(tenant_id, profile_id):
    LOG.debug("_get_profile_binding")
    session = db.get_session()
    binding = session.query(n1kv_models_v2.ProfileBinding).filter_by(
        tenant_id=tenant_id, profile_id=profile_id).one()
    return binding


def get_profile_binding(tenant_id, profile_id):
    """
    Get Network/Policy Profile - Tenant binding
    """
    LOG.debug("get_profile_binding()")
    try:
        return _get_profile_binding(tenant_id, profile_id)
    except exc.NoResultFound:
        raise q_exc.QuantumException("Profile-Tenant binding not found")
    except exc.MultipleResultsFound:
        raise q_exc.QuantumException("Profile-Tenant binding must be unique")


def delete_profile_binding(tenant_id, profile_id):
    """
    Delete Policy Binding
    """
    LOG.debug("delete_profile_binding()")
    session = db.get_session()
    binding = get_profile_binding(tenant_id, profile_id)
    with session.begin(subtransactions=True):
        session.delete(binding)


def _get_profile_bindings(profile_type=None):
    """
    Get all profile-tenant bindings based on profile type.
    If profile type is None, return profile-tenant binding for all
    profile types.
    """
    LOG.debug("_get_profile_bindings()")
    session = db.get_session()
    if profile_type:
        try:
            profile_bindings = session.query(n1kv_models_v2.ProfileBinding).\
                filter_by(profile_type=profile_type).all()
            return profile_bindings
        except exc.NoResultFound:
            return None
    else:
        return session.query(n1kv_models_v2.ProfileBinding).all()


class NetworkProfile_db_mixin(object):

    """
    Network Profile Mixin
    """

    def _get_network_collection_for_tenant(self, model, tenant_id):
        session = db.get_session()
        profile_ids = (session.query(n1kv_models_v2.ProfileBinding.profile_id)
                       .filter_by(tenant_id=tenant_id).
                       filter_by(profile_type='network').all())
        profiles = []
        for pid in profile_ids:
            try:
                profiles.append(session.query(model).
                                filter_by(id=pid[0]).one())
            except exc.NoResultFound:
                return []
        return [self._make_network_profile_dict(p) for p in profiles]

    def _make_profile_bindings_dict(self, profile_binding, fields=None):
        res = {'profile_id': profile_binding['profile_id'],
               'tenant_id': profile_binding['tenant_id']}
        return self._fields(res, fields)

    def _make_network_profile_dict(self, profile, fields=None):
        res = {'id': profile['id'],
               'name': profile['name'],
               'segment_type': profile['segment_type'],
               'segment_range': profile['segment_range'],
               'multicast_ip_index': profile['multicast_ip_index'],
               'multicast_ip_range': profile['multicast_ip_range'],
               'physical_network': profile['physical_network']}
        return self._fields(res, fields)

    def get_network_profile_bindings(self, context, filters=None, fields=None):
        if context.is_admin:
            profile_bindings = _get_profile_bindings(profile_type='network')
            return [self._make_profile_bindings_dict(pb)
                    for pb in profile_bindings]

    def create_network_profile(self, context, network_profile):
        p = network_profile['network_profile']
        self._validate_network_profile_args(context, p)
        tenant_id = self._get_tenant_id_for_create(context, p)
        net_profile = create_network_profile(p)
        create_profile_binding(tenant_id, net_profile.id, 'network')
        if p.get('add_tenant'):
            self.add_network_profile_tenant(net_profile.id, p['add_tenant'])
        return self._make_network_profile_dict(net_profile)

    def delete_network_profile(self, context, id):
        _profile = delete_network_profile(id)
        return self._make_network_profile_dict(_profile)

    def update_network_profile(self, context, id, network_profile):
        p = network_profile['network_profile']
        if context.is_admin and 'add_tenant' in p:
            self.add_network_profile_tenant(id, p['add_tenant'])
            return self._make_network_profile_dict(get_network_profile(id))
        elif context.is_admin and 'remove_tenant' in p:
            delete_profile_binding(p['remove_tenant'], id)
            return self._make_network_profile_dict(get_network_profile(id))
        else:
            return self._make_network_profile_dict(
                update_network_profile(id, p))

    def get_network_profile(self, context, id, fields=None):
        try:
            profile = self._get_by_id(context, n1kv_models_v2.NetworkProfile,
                                      id)
        except exc.NoResultFound:
            raise q_exc.NetworkProfileNotFound(profile_id=id)
        except exc.MultipleResultsFound:
            LOG.error(_('Multiple network profiles match for %s'), id)
            raise q_exc.NetworkProfileNotFound(profile_id=id)
        return self._make_network_profile_dict(profile, fields)

    def get_network_profiles(self, context, filters=None, fields=None):
        if context.is_admin:
            return self._get_collection(context, n1kv_models_v2.NetworkProfile,
                                        self._make_network_profile_dict,
                                        filters=filters, fields=fields)
        else:
            return self._get_network_collection_for_tenant(n1kv_models_v2.
                                                           NetworkProfile,
                                                           context.tenant_id)

    def add_network_profile_tenant(self, profile_id, tenant_id):
        """
        Add a tenant to a network profile
        """
        return create_profile_binding(tenant_id, profile_id, 'network')

    def network_profile_exists(self, context, id):
        try:
            profile = get_network_profile(id)
            return profile and True or False
        except exc.NoResultFound:
            raise c_exc.NetworkProfileIdNotFound(profile_id=id)

    def _get_segment_range(self, data):
        # Sort the range to ensure min, max is in order
        seg_min, seg_max = sorted(map(int, data.split('-')))
        return (seg_min, seg_max)

    def _validate_network_profile_args(self, context, p):
        """
        Validate completeness of Nexus1000V network profile arguments.
        """
        # TODO Cleanup validation logic
        self._validate_network_profile(p)
        self._validate_segment_range_uniqueness(context, p)


    def _validate_segment_range(self, p):
        """
        Validate segment range values.
        :param p:
        :return:
        """
        mo = re.match(r"(\d+)\-(\d+)", p['segment_range'])
        if mo is None:
            msg = _("invalid segment range. example range: 500-550")
            raise q_exc.InvalidInput(error_message=msg)

    def _validate_network_profile(self, p):
        """
        Validate completeness of a network profile arguments.
        :param p:
        :return:
        """
        if any(p[arg] == '' for arg in ('segment_type', 'segment_range')):
            msg = _("arguments segment_type and segment_range missing"
                    " for network profile")
            LOG.exception(msg)
            raise q_exc.InvalidInput(error_message=msg)
        _segment_type = p['segment_type'].lower()
        if _segment_type not in ['vlan', 'vxlan']:
            msg = _("segment_type should either be vlan or vxlan")
            LOG.exception(msg)
            raise q_exc.InvalidInput(error_message=msg)
        self._validate_segment_range(p)
        if _segment_type == n1kv_models_v2.SEGMENT_TYPE_VLAN:
            p['multicast_ip_range'] = '0.0.0.0'

    def _validate_segment_range_uniqueness(self, context, p):
        """
        Validate that segment range doesn't overlap.
        :param context:
        :param p:
        :return:
        """
        _segment_type = p['segment_type'].lower()
        if _segment_type == n1kv_models_v2.SEGMENT_TYPE_VLAN:
            profiles = _get_network_profiles(
                physical_network=p['physical_network'])
        elif _segment_type == n1kv_models_v2.SEGMENT_TYPE_VXLAN:
            profiles = _get_network_profiles()
        if profiles:
            for prfl in profiles:
                _name = prfl.name
                _segment_range = prfl.segment_range
                if p['name'] == _name:
                    msg = _("NetworkProfile name %s already exists" % p[
                            'name'])
                    LOG.exception(msg)
                    raise q_exc.InvalidInput(error_message=msg)
                seg_min, seg_max = self._get_segment_range(p['segment_range'])
                prfl_seg_min, prfl_seg_max = self._get_segment_range(
                    _segment_range)
                if (((seg_min >= prfl_seg_min) and
                     (seg_min <= prfl_seg_max)) or
                    ((seg_max >= prfl_seg_min) and
                     (seg_max <= prfl_seg_max)) or
                    ((seg_min <= prfl_seg_min) and
                     (seg_max >= prfl_seg_max))):
                    msg = _("segment range overlaps with another profile")
                    LOG.exception(msg)
                    raise q_exc.InvalidInput(error_message=msg)


class PolicyProfile_db_mixin(object):

    """
    Policy Profile Mixin
    """

    def _get_policy_collection_for_tenant(self, model, tenant_id):
        session = db.get_session()
        profile_ids = (session.query(n1kv_models_v2.ProfileBinding.profile_id)
                       .filter_by(tenant_id=tenant_id).
                       filter_by(profile_type='policy').all())
        profiles = []
        for pid in profile_ids:
            try:
                profiles.append(session.query(model).
                                filter_by(id=pid[0]).one())
            except exc.NoResultFound:
                return []
        return [self._make_policy_profile_dict(p) for p in profiles]

    def _make_policy_profile_dict(self, profile, fields=None):
        res = {'id': profile['id'], 'name': profile['name']}
        return self._fields(res, fields)

    def _make_profile_bindings_dict(self, profile_binding, fields=None):
        res = {'profile_id': profile_binding['profile_id'],
               'tenant_id': profile_binding['tenant_id']}
        return self._fields(res, fields)

    def _policy_profile_exists(self, id):
        session = db.get_session()
        return session.query(n1kv_models_v2.PolicyProfile).\
            filter_by(id=id).count() and True or False

    def get_policy_profile(self, context, id, fields=None):
        profile = self._get_by_id(context, n1kv_models_v2.PolicyProfile, id)
        return self._make_policy_profile_dict(profile, fields)

    def get_policy_profile(self, context, id, fields=None):
        try:
            profile = self._get_by_id(context, n1kv_models_v2.PolicyProfile,
                                      id)
        except exc.NoResultFound:
            raise q_exc.PolicyProfileNotFound(profile_id=id)
        except exc.MultipleResultsFound:
            LOG.error(_('Multiple policy profiles match for %s'), id)
            raise q_exc.PolicyProfileNotFound(profile_id=id)
        return self._make_policy_profile_dict(profile, fields)

    def get_policy_profiles(self, context, filters=None, fields=None):
        if context.is_admin:
            return self._get_collection(context, n1kv_models_v2.PolicyProfile,
                                        self._make_policy_profile_dict,
                                        filters=filters, fields=fields)
        else:
            return self._get_policy_collection_for_tenant(n1kv_models_v2.
                                                          PolicyProfile,
                                                          context.tenant_id)

    def get_policy_profile_bindings(self, context, filters=None, fields=None):
        if context.is_admin:
            profile_bindings = _get_profile_bindings(profile_type='policy')
            return [self._make_profile_bindings_dict(pb)
                    for pb in profile_bindings]

    def update_policy_profile(self, context, id, policy_profile):
        p = policy_profile['policy_profile']
        if context.is_admin and 'add_tenant' in p:
            self.add_policy_profile_tenant(id, p['add_tenant'])
            return self._make_policy_profile_dict(get_policy_profile(id))
        elif context.is_admin and 'remove_tenant' in p:
            delete_profile_binding(p['remove_tenant'], id)
            return self._make_policy_profile_dict(get_policy_profile(id))
        else:
            return self._make_policy_profile_dict(update_policy_profile(id,
                                                                        p))

    def policy_profile_exists(self, context, id):
        try:
            profile = get_policy_profile(id)
            return profile and True or False
        except exc.NoResultFound:
            raise c_exc.PolicyProfileIdNotFound(profile_id=id)

    def add_policy_profile_tenant(self, profile_id, tenant_id):
        """
        Add tenant to a policy profile
        """
        return create_profile_binding(tenant_id, profile_id, 'policy')

    def remove_policy_profile_tenant(self, profile_id, tenant_id):
        delete_profile_binding(tenant_id, profile_id)

    def _delete_policy_profile(self, profile_id):
        """
        Delete policy profile and associated binding
        """
        session = db.get_session()
        with session.begin(subtransactions=True):
            session.query(n1kv_models_v2.PolicyProfile).\
                filter(n1kv_models_v2.PolicyProfile.id ==
                       profile_id).delete()
            session.query(n1kv_models_v2.ProfileBinding).\
                filter(n1kv_models_v2.ProfileBinding.profile_id ==
                       profile_id).delete()

    def _get_policy_profile_by_name(self, name):
        """
        Get policy profile based on name
        """
        session = db.get_session()
        try:
            with session.begin(subtransactions=True):
                profile = session.query(n1kv_models_v2.PolicyProfile).\
                    filter(n1kv_models_v2.PolicyProfile.name ==
                           name).one()
                return profile
        except exc.NoResultFound:
            return None

    def _remove_all_fake_policy_profiles(self):
        """
        Remove all policy profiles associated with fake tenant id

        This will find all Profile ID where tenant is not set yet - set A
        and profiles where tenant was already set - set B
        and remove what is in both and no tenant id set

        :return:
        """
        session = db.get_session()
        with session.begin(subtransactions=True):
            a_set_q = session.query(n1kv_models_v2.ProfileBinding).\
                filter_by(tenant_id=n1kv_models_v2.TENANT_ID_NOT_SET,
                          profile_type='policy').all()
            a_set = {i.profile_id for i in a_set_q}
            b_set_q = session.query(n1kv_models_v2.ProfileBinding).\
                filter(and_(n1kv_models_v2.ProfileBinding.tenant_id !=
                            n1kv_models_v2.TENANT_ID_NOT_SET,
                            n1kv_models_v2.ProfileBinding.profile_type ==
                            'policy')).all()
            b_set = {i.profile_id for i in b_set_q}
            session.query(n1kv_models_v2.ProfileBinding).\
                filter(and_(n1kv_models_v2.ProfileBinding.
                            profile_id.in_(a_set & b_set), n1kv_models_v2.
                            ProfileBinding.tenant_id == n1kv_models_v2.
                            TENANT_ID_NOT_SET)).\
                delete(synchronize_session='fetch')

    def _replace_fake_tenant_id_with_real(self, context):
        """
        Replace fake tenant id for all Policy Profile
        binding with real admin tenant ID
        """
        if context.is_admin and context.tenant_id:
            tenant_id = context.tenant_id
            session = db.get_session()
            with session.begin(subtransactions=True):
                session.query(n1kv_models_v2.ProfileBinding).\
                    filter_by(tenant_id=n1kv_models_v2.TENANT_ID_NOT_SET).\
                    update({'tenant_id': tenant_id})

    def _add_policy_profile(self, profile_name, profile_id, tenant_id=None):
        """
        Add Policy profile and tenant binding
        """
        profile = {'id': profile_id, 'name': profile_name}
        tenant_id = tenant_id or n1kv_models_v2.TENANT_ID_NOT_SET
        if not self._policy_profile_exists(profile_id):
            create_policy_profile(profile)
        create_profile_binding(tenant_id, profile['id'], 'policy')
