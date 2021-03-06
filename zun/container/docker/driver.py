# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import eventlet
import functools
import six

from docker import errors
from oslo_log import log as logging
from oslo_utils import timeutils

from zun.common import consts
from zun.common import exception
from zun.common.i18n import _
from zun.common import utils
from zun.common.utils import check_container_id
import zun.conf
from zun.container.docker import host
from zun.container.docker import utils as docker_utils
from zun.container import driver
from zun.network import network as zun_network
from zun import objects
from zun.volume import driver as vol_driver


CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)
ATTACH_FLAG = "/attach/ws?logs=0&stream=1&stdin=1&stdout=1&stderr=1"


def is_not_found(e):
    return '404' in str(e)


def is_conflict(e):
    conflict_infos = ['not running', 'not paused', 'paused']
    for info in conflict_infos:
        if info in str(e):
            return True
    return False


def handle_not_found(e, context, container, do_not_raise=False):
    if container.status == consts.DELETING:
        return

    if container.auto_remove:
        container.status = consts.DELETED
    else:
        container.status = consts.ERROR
        container.status_reason = six.text_type(e)
    container.save(context)
    if do_not_raise:
        return

    raise exception.Conflict(message=_(
        "Cannot act on container in '%s' state") % container.status)


def wrap_docker_error(function):

    @functools.wraps(function)
    def decorated_function(*args, **kwargs):
        context = args[1]
        container = args[2]
        try:
            return function(*args, **kwargs)
        except exception.DockerError as e:
            if is_not_found(e):
                handle_not_found(e, context, container)
            if is_conflict(e):
                raise exception.Conflict(_("%s") % str(e))
            raise

    return decorated_function


class DockerDriver(driver.ContainerDriver):
    """Implementation of container drivers for Docker."""
    capabilities = {
        "support_sandbox": True,
        "support_standalone": True,
    }

    def __init__(self):
        super(DockerDriver, self).__init__()
        self._host = host.Host()

    def load_image(self, image_path=None):
        with docker_utils.docker_client() as docker:
            if image_path:
                with open(image_path, 'rb') as fd:
                    LOG.debug('Loading local image %s into docker', image_path)
                    docker.load_image(fd)

    def inspect_image(self, image):
        with docker_utils.docker_client() as docker:
            LOG.debug('Inspecting image %s', image)
            return docker.inspect_image(image)

    def get_image(self, name):
        LOG.debug('Obtaining image %s', name)
        with docker_utils.docker_client() as docker:
            return docker.get_image(name)

    def delete_image(self, image):
        with docker_utils.docker_client() as docker:
            LOG.debug('Deleting image %s', image)
            return docker.remove_image(image)

    def images(self, repo, quiet=False):
        with docker_utils.docker_client() as docker:
            return docker.images(repo, quiet)

    def read_tar_image(self, image):
        with docker_utils.docker_client() as docker:
            LOG.debug('Reading local tar image %s ', image['path'])
            try:
                docker.read_tar_image(image)
            except Exception:
                LOG.warning("Unable to read image data from tarfile")

    def create(self, context, container, image, requested_networks,
               requested_volumes):
        sandbox_id = container.get_sandbox_id()

        with docker_utils.docker_client() as docker:
            network_api = zun_network.api(context=context, docker_api=docker)
            name = container.name
            LOG.debug('Creating container with image %(image)s name %(name)s',
                      {'image': image['image'], 'name': name})
            self._provision_network(context, network_api, requested_networks)
            binds = self._get_binds(context, requested_volumes)
            kwargs = {
                'name': self.get_container_name(container),
                'command': container.command,
                'environment': container.environment,
                'working_dir': container.workdir,
                'labels': container.labels,
                'tty': container.interactive,
                'stdin_open': container.interactive,
            }
            if not sandbox_id:
                # Sandbox is not used so it is legitimate to customize
                # the container's hostname
                kwargs['hostname'] = container.hostname

            runtime = container.runtime if container.runtime\
                else CONF.container_runtime

            host_config = {}
            host_config['runtime'] = runtime
            if sandbox_id:
                host_config['network_mode'] = 'container:%s' % sandbox_id
                # TODO(hongbin): Uncomment this after docker-py add support for
                # container mode for pid namespace.
                # host_config['pid_mode'] = 'container:%s' % sandbox_id
                host_config['ipc_mode'] = 'container:%s' % sandbox_id
                host_config['volumes_from'] = sandbox_id
            else:
                host_config['binds'] = binds
                kwargs['volumes'] = [b['bind'] for b in binds.values()]
            if container.auto_remove:
                host_config['auto_remove'] = container.auto_remove
            if container.memory is not None:
                host_config['mem_limit'] = container.memory
            if container.cpu is not None:
                host_config['cpu_quota'] = int(100000 * container.cpu)
                host_config['cpu_period'] = 100000
            if container.restart_policy is not None:
                count = int(container.restart_policy['MaximumRetryCount'])
                name = container.restart_policy['Name']
                host_config['restart_policy'] = {'Name': name,
                                                 'MaximumRetryCount': count}
            kwargs['host_config'] = docker.create_host_config(**host_config)
            image_repo = image['repo'] + ":" + image['tag']
            response = docker.create_container(image_repo, **kwargs)
            container.container_id = response['Id']

            if not container.addresses:
                addresses = self._setup_network_for_container(
                    context, container, requested_networks, network_api)
                container.addresses = addresses

            container.status = consts.CREATED
            container.status_reason = None
            container.save(context)
            return container

    def _provision_network(self, context, network_api, requested_networks):
        for rq_network in requested_networks:
            self._get_or_create_docker_network(
                context, network_api, rq_network['network'])

    def _get_binds(self, context, requested_volumes):
        binds = {}
        for volume in requested_volumes:
            volume_driver = vol_driver.driver(provider=volume.volume_provider,
                                              context=context)
            source, destination = volume_driver.bind_mount(volume)
            binds[source] = {'bind': destination}
        return binds

    def _setup_network_for_container(self, context, container,
                                     requested_networks, network_api):
        security_group_ids = utils.get_security_group_ids(context, container.
                                                          security_groups)
        # Container connects to the bridge network by default so disconnect
        # the container from it before connecting it to neutron network.
        # This avoids potential conflict between these two networks.
        network_api.disconnect_container_from_network(container, 'bridge')
        addresses = {}
        for network in requested_networks:
            docker_net_name = self._get_docker_network_name(
                context, network['network'])
            addrs = network_api.connect_container_to_network(
                container, docker_net_name, network,
                security_groups=security_group_ids)
            addresses[network['network']] = addrs

        return addresses

    def delete(self, context, container, force):
        teardown_network = True
        if container.get_sandbox_id():
            teardown_network = False

        with docker_utils.docker_client() as docker:
            if teardown_network:
                network_api = zun_network.api(context=context,
                                              docker_api=docker)
                self._cleanup_network_for_container(context, container,
                                                    network_api)

            if container.container_id:
                try:
                    docker.remove_container(container.container_id,
                                            force=force)
                except errors.APIError as api_error:
                    if is_not_found(api_error):
                        return
                    raise

    def _cleanup_network_for_container(self, context, container, network_api):
        if not container.addresses:
            return
        for neutron_net in container.addresses:
            docker_net = self._get_docker_network_name(context, neutron_net)
            network_api.disconnect_container_from_network(
                container, docker_net, neutron_network_id=neutron_net)

    def list(self, context):
        id_to_container_map = {}
        with docker_utils.docker_client() as docker:
            id_to_container_map = {c['Id']: c
                                   for c in docker.list_containers()}

        db_containers = objects.Container.list_by_host(context, CONF.host)
        for db_container in db_containers:
            if db_container.status in (consts.CREATING, consts.DELETING,
                                       consts.DELETED):
                # Skip populating db record since the container is in a
                # unstable state.
                continue

            container_id = db_container.container_id
            docker_container = id_to_container_map.get(container_id)
            if not docker_container:
                if db_container.auto_remove:
                    db_container.status = consts.DELETED
                    db_container.save(context)
                else:
                    LOG.warning("Container was recorded in DB but missing in "
                                "docker")
                continue

            self._populate_container(db_container, docker_container)

        return db_containers

    def update_containers_states(self, context, containers):
        db_containers = self.list(context)
        if not db_containers:
            return

        id_to_db_container_map = {container.container_id: container
                                  for container in db_containers}
        id_to_container_map = {container.container_id: container
                               for container in containers}

        for cid in (six.viewkeys(id_to_container_map) &
                    six.viewkeys(id_to_db_container_map)):
            container = id_to_container_map[cid]
            # sync status
            db_container = id_to_db_container_map[cid]
            if container.status != db_container.status:
                old_status = container.status
                container.status = db_container.status
                container.save(context)
                LOG.info('Status of container %s changed from %s to %s',
                         container.uuid, old_status, container.status)
            # sync host
            # Note(kiennt): Current host.
            cur_host = CONF.host
            if container.host != cur_host:
                old_host = container.host
                container.host = cur_host
                container.save(context)
                LOG.info('Host of container %s changed from %s to %s',
                         container.uuid, old_host, container.host)

    def show(self, context, container):
        with docker_utils.docker_client() as docker:
            if container.container_id is None:
                return container

            response = None
            try:
                response = docker.inspect_container(container.container_id)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    handle_not_found(api_error, context, container,
                                     do_not_raise=True)
                    return container
                raise

            self._populate_container(container, response)
            return container

    def format_status_detail(self, status_time):
        try:
            st = datetime.datetime.strptime((status_time[:19]),
                                            '%Y-%m-%dT%H:%M:%S')
        except ValueError as e:
            LOG.exception("Error on parse {} : {}".format(status_time, e))
            return

        if st == datetime.datetime(1, 1, 1):
            # return empty string if the time is January 1, year 1, 00:00:00
            return ""

        delta = timeutils.utcnow() - st
        time_dict = {}
        time_dict['days'] = delta.days
        time_dict['hours'] = delta.seconds // 3600
        time_dict['minutes'] = (delta.seconds % 3600) // 60
        time_dict['seconds'] = delta.seconds
        if time_dict['days']:
            return '{} days'.format(time_dict['days'])
        if time_dict['hours']:
            return '{} hours'.format(time_dict['hours'])
        if time_dict['minutes']:
            return '{} mins'.format(time_dict['minutes'])
        if time_dict['seconds']:
            return '{} seconds'.format(time_dict['seconds'])
        return

    def _populate_container(self, container, response):
        state = response.get('State')
        self._populate_container_state(container, state)

        config = response.get('Config')
        if config:
            self._populate_hostname_and_ports(container, config)
            self._populate_command(container, config)

        hostconfig = response.get('HostConfig')
        if hostconfig:
            container.runtime = hostconfig.get('Runtime')

    def _populate_container_state(self, container, state):
        if not state:
            container.status = consts.UNKNOWN
            container.status_detail = None
        elif type(state) is dict:
            status_detail = ''
            if state.get('Error'):
                container.status = consts.ERROR
                status_detail = self.format_status_detail(
                    state.get('FinishedAt'))
                container.status_detail = "Exited({}) {} ago " \
                    "(error)".format(state.get('ExitCode'), status_detail)
            elif state.get('Paused'):
                container.status = consts.PAUSED
                status_detail = self.format_status_detail(
                    state.get('StartedAt'))
                container.status_detail = "Up {} (paused)".format(
                    status_detail)
            elif state.get('Running'):
                container.status = consts.RUNNING
                status_detail = self.format_status_detail(
                    state.get('StartedAt'))
                container.status_detail = "Up {}".format(
                    status_detail)
            else:
                started_at = self.format_status_detail(state.get('StartedAt'))
                finished_at = self.format_status_detail(
                    state.get('FinishedAt'))
                if started_at == "" and container.status == consts.CREATING:
                    container.status = consts.CREATED
                    container.status_detail = "Created"
                elif (started_at == "" and
                        container.status in (consts.CREATED, consts.ERROR)):
                    pass
                elif started_at != "" and finished_at == "":
                    container.status = consts.UNKNOWN
                    container.status_detail = ""
                elif started_at != "" and finished_at != "":
                    container.status = consts.STOPPED
                    container.status_detail = "Exited({}) {} ago ".format(
                        state.get('ExitCode'), finished_at)
            if status_detail is None:
                container.status_detail = None
        else:
            state = state.lower()
            if state == 'created' and container.status == consts.CREATING:
                container.status = consts.CREATED
            elif (state == 'created' and
                    container.status in (consts.CREATED, consts.ERROR)):
                pass
            elif state == 'ERROR':
                container.status = consts.PAUSED
            elif state == 'running':
                container.status = consts.RUNNING
            elif state == 'dead':
                container.status = consts.ERROR
            elif state in ('restarting', 'exited', 'removing'):
                container.status = consts.STOPPED
            else:
                container.status = consts.UNKNOWN
            container.status_detail = None

    def _populate_command(self, container, config):
        command_list = config.get('Cmd')
        command_str = None
        if command_list:
            command_str = ' '.join(command_list)
        container.command = command_str

    def _populate_hostname_and_ports(self, container, config):
        # populate hostname only when container.hostname wasn't set
        if container.hostname is None:
            container.hostname = config.get('Hostname')
        # populate ports
        ports = []
        exposed_ports = config.get('ExposedPorts')
        if exposed_ports:
            for key in exposed_ports:
                port = key.split('/')[0]
                ports.append(int(port))
        container.ports = ports

    @check_container_id
    @wrap_docker_error
    def reboot(self, context, container, timeout):
        with docker_utils.docker_client() as docker:
            if timeout:
                docker.restart(container.container_id,
                               timeout=int(timeout))
            else:
                docker.restart(container.container_id)
            container.status = consts.RUNNING
            container.status_reason = None
            return container

    @check_container_id
    @wrap_docker_error
    def stop(self, context, container, timeout):
        with docker_utils.docker_client() as docker:
            if timeout:
                docker.stop(container.container_id,
                            timeout=int(timeout))
            else:
                docker.stop(container.container_id)
            container.status = consts.STOPPED
            container.status_reason = None
            return container

    @check_container_id
    @wrap_docker_error
    def start(self, context, container):
        with docker_utils.docker_client() as docker:
            docker.start(container.container_id)
            container.status = consts.RUNNING
            container.status_reason = None
            return container

    @check_container_id
    @wrap_docker_error
    def pause(self, context, container):
        with docker_utils.docker_client() as docker:
            docker.pause(container.container_id)
            container.status = consts.PAUSED
            container.status_reason = None
            return container

    @check_container_id
    @wrap_docker_error
    def unpause(self, context, container):
        with docker_utils.docker_client() as docker:
            docker.unpause(container.container_id)
            container.status = consts.RUNNING
            container.status_reason = None
            return container

    @check_container_id
    @wrap_docker_error
    def show_logs(self, context, container, stdout=True, stderr=True,
                  timestamps=False, tail='all', since=None):
        with docker_utils.docker_client() as docker:
            try:
                tail = int(tail)
            except ValueError:
                tail = 'all'

            if since is None or since == 'None':
                return docker.logs(container.container_id, stdout, stderr,
                                   False, timestamps, tail, None)
            else:
                try:
                    since = int(since)
                except ValueError:
                    try:
                        since = datetime.datetime.strptime(
                            since, '%Y-%m-%d %H:%M:%S,%f')
                    except Exception:
                        raise
                return docker.logs(container.container_id, stdout, stderr,
                                   False, timestamps, tail, since)

    @check_container_id
    @wrap_docker_error
    def execute_create(self, context, container, command, interactive=False):
        stdin = True if interactive else False
        tty = True if interactive else False
        with docker_utils.docker_client() as docker:
            create_res = docker.exec_create(
                container.container_id, command, stdin=stdin, tty=tty)
            exec_id = create_res['Id']
            return exec_id

    def execute_run(self, exec_id, command):
        with docker_utils.docker_client() as docker:
            try:
                with eventlet.Timeout(CONF.docker.execute_timeout):
                    output = docker.exec_start(exec_id, False, False, False)
            except eventlet.Timeout:
                raise exception.Conflict(_(
                    "Timeout on executing command: %s") % command)
            inspect_res = docker.exec_inspect(exec_id)
            return {"output": output, "exit_code": inspect_res['ExitCode']}

    def execute_resize(self, exec_id, height, width):
        height = int(height)
        width = int(width)
        with docker_utils.docker_client() as docker:
            try:
                docker.exec_resize(exec_id, height=height, width=width)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    raise exception.Invalid(_(
                        "no such exec instance: %s") % str(api_error))
                raise

    @check_container_id
    @wrap_docker_error
    def kill(self, context, container, signal=None):
        with docker_utils.docker_client() as docker:
            if signal is None or signal == 'None':
                docker.kill(container.container_id)
            else:
                docker.kill(container.container_id, signal)
            return container

    @check_container_id
    @wrap_docker_error
    def update(self, context, container):
        patch = container.obj_get_changes()

        args = {}
        memory = patch.get('memory')
        if memory is not None:
            args['mem_limit'] = memory
        cpu = patch.get('cpu')
        if cpu is not None:
            args['cpu_quota'] = int(100000 * cpu)
            args['cpu_period'] = 100000

        with docker_utils.docker_client() as docker:
            return docker.update_container(container.container_id, **args)

    @check_container_id
    def get_websocket_url(self, context, container):
        version = CONF.docker.docker_remote_api_version
        remote_api_host = CONF.docker.docker_remote_api_host
        remote_api_port = CONF.docker.docker_remote_api_port
        url = "ws://" + remote_api_host + ":" + remote_api_port + \
              "/v" + version + "/containers/" + container.container_id \
              + ATTACH_FLAG
        return url

    @check_container_id
    @wrap_docker_error
    def resize(self, context, container, height, width):
        with docker_utils.docker_client() as docker:
            height = int(height)
            width = int(width)
            docker.resize(container.container_id, height, width)
            return container

    @check_container_id
    @wrap_docker_error
    def top(self, context, container, ps_args=None):
        with docker_utils.docker_client() as docker:
            if ps_args is None or ps_args == 'None':
                return docker.top(container.container_id)
            else:
                return docker.top(container.container_id, ps_args)

    @check_container_id
    @wrap_docker_error
    def get_archive(self, context, container, path):
        with docker_utils.docker_client() as docker:
            try:
                stream, stat = docker.get_archive(
                    container.container_id, path)
                filedata = stream.read()
                return filedata, stat
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    raise exception.Invalid(_("%s") % str(api_error))
                raise

    @check_container_id
    @wrap_docker_error
    def put_archive(self, context, container, path, data):
        with docker_utils.docker_client() as docker:
            try:
                docker.put_archive(container.container_id, path, data)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    raise exception.Invalid(_("%s") % str(api_error))
                raise

    @check_container_id
    @wrap_docker_error
    def stats(self, context, container):
        with docker_utils.docker_client() as docker:
            res = docker.stats(container.container_id, decode=False,
                               stream=False)

            cpu_usage = res['cpu_stats']['cpu_usage']['total_usage']
            system_cpu_usage = res['cpu_stats']['system_cpu_usage']
            cpu_percent = float(cpu_usage) / float(system_cpu_usage) * 100
            mem_usage = res['memory_stats']['usage'] / 1024 / 1024
            mem_limit = res['memory_stats']['limit'] / 1024 / 1024
            mem_percent = float(mem_usage) / float(mem_limit) * 100

            blk_stats = res['blkio_stats']['io_service_bytes_recursive']
            io_read = 0
            io_write = 0
            for item in blk_stats:
                if 'Read' == item['op']:
                    io_read = io_read + item['value']
                if 'Write' == item['op']:
                    io_write = io_write + item['value']

            net_stats = res['networks']
            net_rxb = 0
            net_txb = 0
            for k, v in net_stats.items():
                net_rxb = net_rxb + v['rx_bytes']
                net_txb = net_txb + v['tx_bytes']

            stats = {"CONTAINER": container.name,
                     "CPU %": cpu_percent,
                     "MEM USAGE(MiB)": mem_usage,
                     "MEM LIMIT(MiB)": mem_limit,
                     "MEM %": mem_percent,
                     "BLOCK I/O(B)": str(io_read) + "/" + str(io_write),
                     "NET I/O(B)": str(net_rxb) + "/" + str(net_txb)}
            return stats

    @check_container_id
    @wrap_docker_error
    def commit(self, context, container, repository=None, tag=None):
        with docker_utils.docker_client() as docker:
            repository = str(repository)
            if tag is None or tag == "None":
                return docker.commit(container.container_id, repository)
            else:
                tag = str(tag)
                return docker.commit(container.container_id, repository, tag)

    def _encode_utf8(self, value):
        if six.PY2 and not isinstance(value, six.text_type):
            value = six.text_type(value)
        return value.encode('utf-8')

    def create_sandbox(self, context, container, requested_networks,
                       requested_volumes,
                       image='kubernetes/pause'):
        with docker_utils.docker_client() as docker:
            network_api = zun_network.api(context=context, docker_api=docker)
            self._provision_network(context, network_api, requested_networks)
            binds = self._get_binds(context, requested_volumes)
            host_config = docker.create_host_config(binds=binds)
            name = self.get_sandbox_name(container)
            volumes = [b['bind'] for b in binds.values()]
            sandbox = docker.create_container(image, name=name,
                                              hostname=name[:63],
                                              volumes=volumes,
                                              host_config=host_config)
            container.set_sandbox_id(sandbox['Id'])
            addresses = self._setup_network_for_container(
                context, container, requested_networks, network_api)
            if addresses is None:
                raise exception.ZunException(_(
                    "Unexpected missing of addresses"))

            container.addresses = addresses
            container.save(context)

            docker.start(sandbox['Id'])
            return sandbox['Id']

    def attach_volume(self, context, volume_mapping):
        volume_driver = vol_driver.driver(
            provider=volume_mapping.volume_provider,
            context=context)
        volume_driver.attach(volume_mapping)

    def detach_volume(self, context, volume_mapping):
        volume_driver = vol_driver.driver(
            provider=volume_mapping.volume_provider,
            context=context)
        volume_driver.detach(volume_mapping)

    def _get_or_create_docker_network(self, context, network_api,
                                      neutron_net_id):
        docker_net_name = self._get_docker_network_name(context,
                                                        neutron_net_id)
        docker_networks = network_api.list_networks(names=[docker_net_name])
        if not docker_networks:
            network_api.create_network(neutron_net_id=neutron_net_id,
                                       name=docker_net_name)

    def _get_docker_network_name(self, context, neutron_net_id):
        # Append project_id to the network name to avoid name collision
        # across projects.
        return neutron_net_id + '-' + context.project_id

    def delete_sandbox(self, context, container):
        sandbox_id = container.get_sandbox_id()
        with docker_utils.docker_client() as docker:
            network_api = zun_network.api(context=context, docker_api=docker)
            self._cleanup_network_for_container(context, container,
                                                network_api)
            try:
                docker.remove_container(sandbox_id, force=True)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    return
                raise

    def stop_sandbox(self, context, sandbox_id):
        with docker_utils.docker_client() as docker:
            docker.stop(sandbox_id)

    def get_sandbox_name(self, container):
        return 'zun-sandbox-' + container.uuid

    def get_container_name(self, container):
        return 'zun-' + container.uuid

    def get_host_info(self):
        with docker_utils.docker_client() as docker:
            info = docker.info()
            total = info['Containers']
            paused = info['ContainersPaused']
            running = info['ContainersRunning']
            stopped = info['ContainersStopped']
            cpus = info['NCPU']
            architecture = info['Architecture']
            os_type = info['OSType']
            os = info['OperatingSystem']
            kernel_version = info['KernelVersion']
            labels = {}
            slabels = info['Labels']
            if slabels:
                for l in slabels:
                    kv = l.split("=")
                    label = {kv[0]: kv[1]}
                    labels.update(label)
            return (total, running, paused, stopped, cpus,
                    architecture, os_type, os, kernel_version, labels)

    def get_cpu_used(self):
        cpu_used = 0
        with docker_utils.docker_client() as docker:
            containers = docker.containers()
            for container in containers:
                cnt_id = container['Id']
                # Fixme: if there is a way to get all container inspect info
                # for one call only?
                inspect = docker.inspect_container(cnt_id)
                cpu_period = inspect['HostConfig']['CpuPeriod']
                cpu_quota = inspect['HostConfig']['CpuQuota']
                if cpu_period and cpu_quota:
                    cpu_used += float(cpu_quota) / cpu_period
                else:
                    if 'NanoCpus' in inspect['HostConfig']:
                        nanocpus = inspect['HostConfig']['NanoCpus']
                        cpu_used += float(nanocpus) / 1e9
            return cpu_used

    def add_security_group(self, context, container, security_group):

        with docker_utils.docker_client() as docker:
            network_api = zun_network.api(context=context,
                                          docker_api=docker)
            network_api.add_security_groups_to_ports(container,
                                                     [security_group])

    def get_available_nodes(self):
        return [self._host.get_hostname()]

    def network_detach(self, context, container, network):
        with docker_utils.docker_client() as docker:
            network_api = zun_network.api(context,
                                          docker_api=docker)
            docker_net = self._get_docker_network_name(context, network)
            network_api.disconnect_container_from_network(container,
                                                          docker_net, network)

            # Only clear network info related to this network
            # Cannot del container.address directly which will not update
            # changed fields of the container objects as the del operate on
            # the addresses object, only base.getter will called.
            update = container.addresses
            del update[network]
            container.addresses = update
            container.save(context)

    def network_attach(self, context, container, network):
        with docker_utils.docker_client() as docker:
            network_api = zun_network.api(context,
                                          docker_api=docker)
            if network in container.addresses:
                raise exception.ZunException('Container %(container)s has'
                                             ' alreay connected to the network'
                                             '%(network)s.'
                                             % {'container': container['uuid'],
                                                'network': network})
            self._get_or_create_docker_network(context, network_api, network)
            requested_network = {'network': network,
                                 'port': '',
                                 'v4-fixed-ip': '',
                                 'v6-fixed-ip': ''}
            docker_net_name = self._get_docker_network_name(context, network)
            addrs = network_api.connect_container_to_network(
                container, docker_net_name, requested_network,
                security_groups=None)
            if addrs is None:
                raise exception.ZunException(_(
                    'Unexpected missing of addresses'))
            update = {}
            update[network] = addrs
            addresses = container.addresses
            addresses.update(update)
            container.addresses = addresses
            container.save(context)
