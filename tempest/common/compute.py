# Copyright (c) 2015 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
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

import base64
import socket
from ssl import SSLContext as sslc
import struct
import textwrap
from urllib import parse as urlparse

from oslo_log import log as logging
from oslo_utils import excutils

from tempest.common.utils.linux import remote_client
from tempest.common import waiters
from tempest import config
from tempest import exceptions
from tempest.lib.common import fixed_network
from tempest.lib.common import rest_client
from tempest.lib.common.utils import data_utils
from tempest.lib import exceptions as lib_exc
import ssl

CONF = config.CONF

LOG = logging.getLogger(__name__)


def is_scheduler_filter_enabled(filter_name):
    """Check the list of enabled compute scheduler filters from config.

    This function checks whether the given compute scheduler filter is enabled
    in the nova config file. If the scheduler_enabled_filters option is set to
    'all' in tempest.conf then, this function returns True with assumption that
    requested filter 'filter_name' is one of the enabled filters in nova
    ("nova.scheduler.filters.all_filters").
    """

    filters = CONF.compute_feature_enabled.scheduler_enabled_filters
    if not filters:
        return False
    if 'all' in filters:
        return True
    if filter_name in filters:
        return True
    return False


def get_server_ip(server, validation_resources=None):
    """Get the server fixed or floating IP.

    Based on the configuration we're in, return a correct ip
    address for validating that a guest is up.

    :param server: The server dict as returned by the API
    :param validation_resources: The dict of validation resources
        provisioned for the server.
    """
    if CONF.validation.connect_method == 'floating':
        if validation_resources:
            return validation_resources['floating_ip']['ip']
        else:
            msg = ('When validation.connect_method equals floating, '
                   'validation_resources cannot be None')
            raise lib_exc.InvalidParam(invalid_param=msg)
    elif CONF.validation.connect_method == 'fixed':
        addresses = server['addresses'][CONF.validation.network_for_ssh]
        for address in addresses:
            if address['version'] == CONF.validation.ip_version_for_ssh:
                return address['addr']
        raise exceptions.ServerUnreachable(server_id=server['id'])
    else:
        raise lib_exc.InvalidConfiguration()


def _setup_validation_fip(
        server, clients, tenant_network, validation_resources):
    if CONF.service_available.neutron:
        ifaces = clients.interfaces_client.list_interfaces(server['id'])
        validation_port = None
        for iface in ifaces['interfaceAttachments']:
            if iface['net_id'] == tenant_network['id']:
                validation_port = iface['port_id']
                break
        if not validation_port:
            # NOTE(artom) This will get caught by the catch-all clause in
            # the wait_until loop below
            raise ValueError('Unable to setup floating IP for validation: '
                             'port not found on tenant network')
        clients.floating_ips_client.update_floatingip(
            validation_resources['floating_ip']['id'],
            port_id=validation_port)
    else:
        fip_client = clients.compute_floating_ips_client
        fip_client.associate_floating_ip_to_server(
            floating_ip=validation_resources['floating_ip']['ip'],
            server_id=server['id'])


def wait_for_ssh_or_ping(server, clients, tenant_network,
                         validatable, validation_resources, wait_until,
                         set_floatingip):
    """Wait for the server for SSH or Ping as requested.

    :param server: The server dict as returned by the API
    :param clients: Client manager which provides OpenStack Tempest clients.
    :param tenant_network: Tenant network to be used for creating a server.
    :param validatable: Whether the server will be pingable or sshable.
    :param validation_resources: Resources created for the connection to the
        server. Include a keypair, a security group and an IP.
    :param wait_until: Server status to wait for the server to reach.
        It can be PINGABLE and SSHABLE states when the server is both
        validatable and has the required validation_resources provided.
    :param set_floatingip: If FIP needs to be associated to server
    """
    if set_floatingip and CONF.validation.connect_method == 'floating':
        _setup_validation_fip(
            server, clients, tenant_network, validation_resources)

    server_ip = get_server_ip(
        server, validation_resources=validation_resources)
    if wait_until == 'PINGABLE':
        waiters.wait_for_ping(
            server_ip,
            clients.servers_client.build_timeout,
            clients.servers_client.build_interval
        )
    if wait_until == 'SSHABLE':
        pkey = validation_resources['keypair']['private_key']
        ssh_client = remote_client.RemoteClient(
            server_ip,
            CONF.validation.image_ssh_user,
            pkey=pkey,
            server=server,
            servers_client=clients.servers_client
        )
        waiters.wait_for_ssh(
            ssh_client,
            clients.servers_client.build_timeout
        )


def create_test_server(clients, validatable=False, validation_resources=None,
                       tenant_network=None, wait_until=None,
                       volume_backed=False, name=None, flavor=None,
                       image_id=None, **kwargs):
    """Common wrapper utility returning a test server.

    This method is a common wrapper returning a test server that can be
    pingable or sshable.

    :param clients: Client manager which provides OpenStack Tempest clients.
    :param validatable: Whether the server will be pingable or sshable.
    :param validation_resources: Resources created for the connection to the
        server. Include a keypair, a security group and an IP.
    :param tenant_network: Tenant network to be used for creating a server.
    :param wait_until: Server status to wait for the server to reach after
        its creation. Additionally PINGABLE and SSHABLE states are also
        accepted when the server is both validatable and has the required
        validation_resources provided.
    :param volume_backed: Whether the server is volume backed or not.
        If this is true, a volume will be created and create server will be
        requested with 'block_device_mapping_v2' populated with below values:

        .. code-block:: python

            bd_map_v2 = [{
                'uuid': volume['volume']['id'],
                'source_type': 'volume',
                'destination_type': 'volume',
                'boot_index': 0,
                'delete_on_termination': True}]
            kwargs['block_device_mapping_v2'] = bd_map_v2

        If server needs to be booted from volume with other combination of bdm
        inputs than mentioned above, then pass the bdm inputs explicitly as
        kwargs and image_id as empty string ('').
    :param name: Name of the server to be provisioned. If not defined a random
        string ending with '-instance' will be generated.
    :param flavor: Flavor of the server to be provisioned. If not defined,
        CONF.compute.flavor_ref will be used instead.
    :param image_id: ID of the image to be used to provision the server. If not
        defined, CONF.compute.image_ref will be used instead.
    :returns: a tuple
    """

    if name is None:
        name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix, name=__name__ + "-instance")
    if flavor is None:
        flavor = CONF.compute.flavor_ref
    if image_id is None:
        image_id = CONF.compute.image_ref

    kwargs = fixed_network.set_networks_kwarg(
        tenant_network, kwargs) or {}

    multiple_create_request = (max(kwargs.get('min_count', 0),
                                   kwargs.get('max_count', 0)) > 1)

    if CONF.validation.run_validation and validatable:
        LOG.debug("Provisioning test server with validation resources %s",
                  validation_resources)
        if 'security_groups' in kwargs:
            kwargs['security_groups'].append(
                {'name': validation_resources['security_group']['name']})
        else:
            try:
                kwargs['security_groups'] = [
                    {'name': validation_resources['security_group']['name']}]
            except KeyError:
                LOG.debug("No security group provided.")

        if 'key_name' not in kwargs:
            try:
                kwargs['key_name'] = validation_resources['keypair']['name']
            except KeyError:
                LOG.debug("No key provided.")

        if CONF.validation.connect_method == 'floating':
            if wait_until is None:
                wait_until = 'ACTIVE'

        if 'user_data' not in kwargs:
            # If nothing overrides the default user data script then run
            # a simple script on the host to print networking info. This is
            # to aid in debugging ssh failures.
            script = '''
                     #!/bin/sh
                     echo "Printing {user} user authorized keys"
                     cat ~{user}/.ssh/authorized_keys || true
                     '''.format(user=CONF.validation.image_ssh_user)
            script_clean = textwrap.dedent(script).lstrip().encode('utf8')
            script_b64 = base64.b64encode(script_clean)
            kwargs['user_data'] = script_b64

    if volume_backed:
        volume_name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix, name=__name__ + '-volume')
        volumes_client = clients.volumes_client_latest
        params = {'name': volume_name,
                  'imageRef': image_id,
                  'size': CONF.volume.volume_size}
        if CONF.compute.compute_volume_common_az:
            params.setdefault('availability_zone',
                              CONF.compute.compute_volume_common_az)
        volume = volumes_client.create_volume(**params)
        try:
            waiters.wait_for_volume_resource_status(volumes_client,
                                                    volume['volume']['id'],
                                                    'available')
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    volumes_client.delete_volume(volume['volume']['id'])
                    volumes_client.wait_for_resource_deletion(
                        volume['volume']['id'])
                except Exception as exc:
                    LOG.exception("Deleting volume %s failed, exception %s",
                                  volume['volume']['id'], exc)
        bd_map_v2 = [{
            'uuid': volume['volume']['id'],
            'source_type': 'volume',
            'destination_type': 'volume',
            'boot_index': 0,
            'delete_on_termination': True}]
        kwargs['block_device_mapping_v2'] = bd_map_v2

        # Since this is boot from volume an image does not need
        # to be specified.
        image_id = ''

    if CONF.compute.compute_volume_common_az:
        kwargs.setdefault('availability_zone',
                          CONF.compute.compute_volume_common_az)
    body = clients.servers_client.create_server(name=name, imageRef=image_id,
                                                flavorRef=flavor,
                                                **kwargs)
    request_id = body.response['x-openstack-request-id']

    # handle the case of multiple servers
    if multiple_create_request:
        # Get servers created which name match with name param.
        body_servers = clients.servers_client.list_servers()
        created_servers = \
            [s for s in body_servers['servers'] if s['name'].startswith(name)]
    else:
        body = rest_client.ResponseBody(body.response, body['server'])
        created_servers = [body]

    if wait_until:

        # NOTE(lyarwood): PINGABLE and SSHABLE both require the instance to
        # go ACTIVE initially before we can setup the fip(s) etc so stash
        # this additional wait state for later use.
        wait_until_extra = None
        if wait_until in ['PINGABLE', 'SSHABLE']:
            if not validatable and validation_resources is None:
                raise RuntimeError(
                    'SSHABLE/PINGABLE requires validatable=True '
                    'and validation_resources to be passed')
            wait_until_extra = wait_until
            wait_until = 'ACTIVE'

        servers = []
        try:
            # Wait for server to be in active state and populate servers list
            # with those full server response so that we will have addresses
            # field present in server which is needed to be used for wait for
            # ssh
            for server in created_servers:
                server = waiters.wait_for_server_status(
                    clients.servers_client, server['id'], wait_until,
                    request_id=request_id)
                servers.append(server)

            for server in servers:
                if CONF.validation.run_validation and validatable:
                    if CONF.validation.connect_method == 'floating':
                        _setup_validation_fip(
                            server, clients, tenant_network,
                            validation_resources)
                    if wait_until_extra:
                        wait_for_ssh_or_ping(
                            server, clients, tenant_network,
                            validatable, validation_resources,
                            wait_until_extra, False)
        except Exception:
            with excutils.save_and_reraise_exception():
                for server in created_servers:
                    try:
                        clients.servers_client.delete_server(
                            server['id'])
                    except Exception:
                        LOG.exception('Deleting server %s failed',
                                      server['id'])
                for server in created_servers:
                    # NOTE(artom) If the servers were booted with volumes
                    # and with delete_on_termination=False we need to wait
                    # for the servers to go away before proceeding with
                    # cleanup, otherwise we'll attempt to delete the
                    # volumes while they're still attached to servers that
                    # are in the process of being deleted.
                    try:
                        waiters.wait_for_server_termination(
                            clients.servers_client, server['id'])
                    except Exception:
                        LOG.exception('Server %s failed to delete in time',
                                      server['id'])
        if servers and not multiple_create_request:
            body = rest_client.ResponseBody(body.response, servers[0])
        return body, servers

    return body, created_servers


def shelve_server(servers_client, server_id, force_shelve_offload=False):
    """Common wrapper utility to shelve server.

    This method is a common wrapper to make server in 'SHELVED'
    or 'SHELVED_OFFLOADED' state.

    :param servers_clients: Compute servers client instance.
    :param server_id: Server to make in shelve state
    :param force_shelve_offload: Forcefully offload shelve server if it
                                 is configured not to offload server
                                 automatically after offload time.
    """
    body = servers_client.shelve_server(server_id)
    request_id = body.response['x-openstack-request-id']

    offload_time = CONF.compute.shelved_offload_time
    if offload_time >= 0:
        waiters.wait_for_server_status(servers_client, server_id,
                                       'SHELVED_OFFLOADED',
                                       extra_timeout=offload_time,
                                       request_id=request_id)
    else:
        waiters.wait_for_server_status(servers_client, server_id, 'SHELVED')
        if force_shelve_offload:
            servers_client.shelve_offload_server(server_id)
            waiters.wait_for_server_status(servers_client, server_id,
                                           'SHELVED_OFFLOADED',
                                           request_id=request_id)


def create_websocket(url):
    url = urlparse.urlparse(url)

    # NOTE(mnaser): It is possible that there is no port specified, so fall
    #               back to the default port based on the scheme.
    port = url.port or (443 if url.scheme == 'https' else 80)

    for res in socket.getaddrinfo(url.hostname, port,
                                  socket.AF_UNSPEC, socket.SOCK_STREAM):
        af, socktype, proto, _, sa = res
        client_socket = socket.socket(af, socktype, proto)
        if url.scheme == 'https':
            client_socket = sslc(protocol=ssl.PROTOCOL_TLS_CLIENT).wrap_socket(client_socket,
                                               server_hostname=url.hostname)
        client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            client_socket.connect(sa)
        except socket.error:
            client_socket.close()
            continue
        break
    else:
        raise socket.error('WebSocket creation failed')
    # Turn the Socket into a WebSocket to do the communication
    return _WebSocket(client_socket, url)


class _WebSocket(object):
    def __init__(self, client_socket, url):
        """Contructor for the WebSocket wrapper to the socket."""
        self._socket = client_socket
        # cached stream for early frames.
        self.cached_stream = b''
        # Upgrade the HTTP connection to a WebSocket
        self._upgrade(url)

    def _recv(self, recv_size):
        """Wrapper to receive data from the cached stream or socket."""
        if recv_size <= 0:
            return None

        data_from_cached = b''
        data_from_socket = b''
        if len(self.cached_stream) > 0:
            read_from_cached = min(len(self.cached_stream), recv_size)
            data_from_cached += self.cached_stream[:read_from_cached]
            self.cached_stream = self.cached_stream[read_from_cached:]
            recv_size -= read_from_cached
        if recv_size > 0:
            data_from_socket = self._socket.recv(recv_size)
        return data_from_cached + data_from_socket

    def receive_frame(self):
        """Wrapper for receiving data to parse the WebSocket frame format"""
        # We need to loop until we either get some bytes back in the frame
        # or no data was received (meaning the socket was closed).  This is
        # done to handle the case where we get back some empty frames
        while True:
            header = self._recv(2)
            # If we didn't receive any data, just return None
            if not header:
                return None
            # We will make the assumption that we are only dealing with
            # frames less than 125 bytes here (for the negotiation) and
            # that only the 2nd byte contains the length, and since the
            # server doesn't do masking, we can just read the data length
            if int(header[1]) & 127 > 0:
                return self._recv(int(header[1]) & 127)

    def send_frame(self, data):
        """Wrapper for sending data to add in the WebSocket frame format."""
        frame_bytes = list()
        # For the first byte, want to say we are sending binary data (130)
        frame_bytes.append(130)
        # Only sending negotiation data so don't need to worry about > 125
        # We do need to add the bit that says we are masking the data
        frame_bytes.append(len(data) | 128)
        # We don't really care about providing a random mask for security
        # So we will just hard-code a value since a test program
        mask = [7, 2, 1, 9]
        for i in range(len(mask)):
            frame_bytes.append(mask[i])
        # Mask each of the actual data bytes that we are going to send
        for i in range(len(data)):
            frame_bytes.append(int(data[i]) ^ mask[i % 4])
        # Convert our integer list to a binary array of bytes
        frame_bytes = struct.pack('!%iB' % len(frame_bytes), * frame_bytes)
        self._socket.sendall(frame_bytes)

    def close(self):
        """Helper method to close the connection."""
        # Close down the real socket connection and exit the test program
        if self._socket is not None:
            self._socket.shutdown(1)
            self._socket.close()
            self._socket = None

    def _upgrade(self, url):
        """Upgrade the HTTP connection to a WebSocket and verify."""
        # It is possible to pass the path as a query parameter in the request,
        # so use it if present
        # Given noVNC format
        # https://x.com/vnc_auto.html?path=%3Ftoken%3Dxxx,
        # url format is
        # ParseResult(scheme='https', netloc='x.com',
        # path='/vnc_auto.html', params='',
        # query='path=%3Ftoken%3Dxxx', fragment='').
        # qparams format is {'path': ['?token=xxx']}
        qparams = urlparse.parse_qs(url.query)
        # according to references
        # https://docs.python.org/3/library/urllib.parse.html
        # https://tools.ietf.org/html/rfc3986#section-3.4
        # qparams['path'][0] format is '?token=xxx' without / prefix
        # remove / in /websockify to comply to references.
        path = qparams['path'][0] if 'path' in qparams else 'websockify'
        # Fix websocket request format by adding / prefix.
        # Updated request format: GET /?token=xxx HTTP/1.1
        # or GET /websockify HTTP/1.1
        reqdata = 'GET /%s HTTP/1.1\r\n' % path
        reqdata += 'Host: %s' % url.hostname
        # Add port only if we have one specified
        if url.port:
            reqdata += ':%s' % url.port
        # Line-ending for Host header
        reqdata += '\r\n'
        # Tell the HTTP Server to Upgrade the connection to a WebSocket
        reqdata += 'Upgrade: websocket\r\nConnection: Upgrade\r\n'
        # The token=xxx is sent as a Cookie not in the URI for noVNC < v1.1.0
        reqdata += 'Cookie: %s\r\n' % url.query
        # Use a hard-coded WebSocket key since a test program
        reqdata += 'Sec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw==\r\n'
        reqdata += 'Sec-WebSocket-Version: 13\r\n'
        # We are choosing to use binary even though browser may do Base64
        reqdata += 'Sec-WebSocket-Protocol: binary\r\n\r\n'
        # Send the HTTP GET request and get the response back
        self._socket.sendall(reqdata.encode('utf8'))
        self.response = data = self._socket.recv(4096)
        # Loop through & concatenate all of the data in the response body
        end_loc = self.response.find(b'\r\n\r\n')
        while data and end_loc < 0:
            data = self._socket.recv(4096)
            self.response += data
            end_loc = self.response.find(b'\r\n\r\n')

        if len(self.response) > end_loc + 4:
            # In case some frames (e.g. the first RFP negotiation) have
            # arrived, cache it for next reading.
            self.cached_stream = self.response[end_loc + 4:]
            # ensure response ends with '\r\n\r\n'.
            self.response = self.response[:end_loc + 4]
