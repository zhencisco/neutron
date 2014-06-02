# Copyright (c) 2013 OpenStack Foundation
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

from oslo.config import cfg
from six import moves
import sqlalchemy as sa
from sqlalchemy.orm import exc as sa_exc
from sqlalchemy import sql

from neutron.common import exceptions as exc
from neutron.db import api as db_api
from neutron.db import model_base
from neutron.openstack.common import log
from neutron.plugins.common import constants as p_const
from neutron.plugins.ml2 import driver_api as api
from neutron.plugins.ml2.drivers import type_tunnel
from neutron.plugins.ml2.drivers.type_driver_common import TypeDriverMixin

LOG = log.getLogger(__name__)

gre_opts = [
    cfg.ListOpt('tunnel_id_ranges',
                default=[],
                help=_("Comma-separated list of <tun_min>:<tun_max> tuples "
                       "enumerating ranges of GRE tunnel IDs that are "
                       "available for tenant network allocation"))
]

cfg.CONF.register_opts(gre_opts, "ml2_type_gre")


class GreAllocation(model_base.BASEV2):

    __tablename__ = 'ml2_gre_allocations'

    gre_id = sa.Column(sa.Integer, nullable=False, primary_key=True,
                       autoincrement=False)
    allocated = sa.Column(sa.Boolean, nullable=False, default=False,
                          server_default=sql.false())
    network_id = sa.Column(sa.String(36),
                           sa.ForeignKey('networks.id', ondelete="CASCADE"),
                           nullable=True)
    provider_network = sa.Column(sa.Boolean, default=False)


class GreEndpoints(model_base.BASEV2):
    """Represents tunnel endpoint in RPC mode."""
    __tablename__ = 'ml2_gre_endpoints'

    ip_address = sa.Column(sa.String(64), primary_key=True)

    def __repr__(self):
        return "<GreTunnelEndpoint(%s)>" % self.ip_address


class GreTypeDriver(type_tunnel.TunnelTypeDriver, TypeDriverMixin):

    def get_type(self):
        return p_const.TYPE_GRE

    def initialize(self):
        self.gre_id_ranges = []
        self._parse_tunnel_ranges(
            cfg.CONF.ml2_type_gre.tunnel_id_ranges,
            self.gre_id_ranges,
            p_const.TYPE_GRE
        )
        self._sync_gre_allocations()

    def allocate_static_segment(self, session, net_data):
        segments = self._process_provider_create(net_data)
        net_id = net_data.get('id')

        if segments:
            all_segments = []
            for segment in segments:
                one_seg = self.reserve_provider_segment(session, net_id,
                                                        segment)
                all_segments.append(one_seg)
            return all_segments
        else:
            return [self.allocate_tenant_segment(session, net_id)]

    def delete_network(self, session, context):
        net_data = context._network
        net_id = net_data.get('id')
        self.release_static_segment(session, net_id)

    def get_segment(self, context, network_id):
        LOG.debug(_("Returning segments for network %s") % network_id)
        alloc = (context.session.query(GreAllocation).
                 filter_by(network_id=network_id).one())

        return {api.NETWORK_TYPE: p_const.TYPE_GRE,
                api.PHYSICAL_NETWORK: None,
                api.SEGMENTATION_ID: alloc.gre_id}

    def reserve_provider_segment(self, session, network_id, segment):
        segmentation_id = segment.get(api.SEGMENTATION_ID)
        with session.begin(subtransactions=True):
            try:
                alloc = (session.query(GreAllocation).
                         filter_by(gre_id=segmentation_id).
                         with_lockmode('update').
                         one())
                if alloc.allocated:
                    raise exc.TunnelIdInUse(tunnel_id=segmentation_id)
                LOG.debug(_("Reserving specific gre tunnel %s from pool"),
                          segmentation_id)
                alloc.allocated = True
                alloc.network_id = network_id
                alloc.provider_network = True
                return {api.NETWORK_TYPE: p_const.TYPE_GRE,
                        api.PHYSICAL_NETWORK: None,
                        api.SEGMENTATION_ID: alloc.gre_id}
            except sa_exc.NoResultFound:
                LOG.debug(_("Reserving specific gre tunnel %s outside pool"),
                          segmentation_id)
                alloc = GreAllocation(gre_id=segmentation_id,
                                      allocated=True,
                                      provider_network=True)
                session.add(alloc)
                return {api.NETWORK_TYPE: p_const.TYPE_GRE,
                        api.PHYSICAL_NETWORK: None,
                        api.SEGMENTATION_ID: alloc.gre_id}

    def allocate_tenant_segment(self, session, network_id):
        with session.begin(subtransactions=True):
            alloc = (session.query(GreAllocation).
                     filter_by(allocated=False).
                     with_lockmode('update').
                     first())
            if alloc:
                LOG.debug(_("Allocating gre tunnel id  %(gre_id)s"),
                          {'gre_id': alloc.gre_id})
                alloc.allocated = True
                alloc.network_id = network_id
                return {api.NETWORK_TYPE: p_const.TYPE_GRE,
                        api.PHYSICAL_NETWORK: None,
                        api.SEGMENTATION_ID: alloc.gre_id}

    def release_static_segment(self, session, network_id):
        with session.begin(subtransactions=True):
            try:
                alloc = (session.query(GreAllocation).
                         filter_by(network_id=network_id).
                         with_lockmode('update').
                         one())
                alloc.allocated = False
                gre_id = alloc['gre_id']
                for lo, hi in self.gre_id_ranges:
                    if lo <= gre_id <= hi:
                        LOG.debug(_("Releasing gre tunnel %s to pool"),
                                  gre_id)
                        break
                else:
                    session.delete(alloc)
                    LOG.debug(_("Releasing gre tunnel %s outside pool"),
                              gre_id)
            except sa_exc.NoResultFound:
                LOG.warning(_("gre_id %s not found"), gre_id)

    def _sync_gre_allocations(self):
        """Synchronize gre_allocations table with configured tunnel ranges."""

        # determine current configured allocatable gres
        gre_ids = set()
        for gre_id_range in self.gre_id_ranges:
            tun_min, tun_max = gre_id_range
            if tun_max + 1 - tun_min > 1000000:
                LOG.error(_("Skipping unreasonable gre ID range "
                            "%(tun_min)s:%(tun_max)s"),
                          {'tun_min': tun_min, 'tun_max': tun_max})
            else:
                gre_ids |= set(moves.xrange(tun_min, tun_max + 1))

        session = db_api.get_session()
        with session.begin(subtransactions=True):
            # remove from table unallocated tunnels not currently allocatable
            allocs = (session.query(GreAllocation).all())
            for alloc in allocs:
                try:
                    # see if tunnel is allocatable
                    gre_ids.remove(alloc.gre_id)
                except KeyError:
                    # it's not allocatable, so check if its allocated
                    if not alloc.allocated:
                        # it's not, so remove it from table
                        LOG.debug(_("Removing tunnel %s from pool"),
                                  alloc.gre_id)
                        session.delete(alloc)

            # add missing allocatable tunnels to table
            for gre_id in sorted(gre_ids):
                alloc = GreAllocation(gre_id=gre_id)
                session.add(alloc)

    def get_endpoints(self):
        """Get every gre endpoints from database."""

        LOG.debug(_("get_gre_endpoints() called"))
        session = db_api.get_session()

        with session.begin(subtransactions=True):
            gre_endpoints = session.query(GreEndpoints)
            return [{'ip_address': gre_endpoint.ip_address}
                    for gre_endpoint in gre_endpoints]

    def add_endpoint(self, ip):
        LOG.debug(_("add_gre_endpoint() called for ip %s"), ip)
        session = db_api.get_session()
        with session.begin(subtransactions=True):
            try:
                gre_endpoint = (session.query(GreEndpoints).
                                filter_by(ip_address=ip).one())
                LOG.warning(_("Gre endpoint with ip %s already exists"), ip)
            except sa_exc.NoResultFound:
                gre_endpoint = GreEndpoints(ip_address=ip)
                session.add(gre_endpoint)
            return gre_endpoint
