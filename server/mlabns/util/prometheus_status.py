import json
import logging
import re
import urllib
import urllib2

from mlabns.db import model
from mlabns.util import constants
from mlabns.util import message


class Error(Exception):
    pass


class PrometheusStatusUnparseableError(Error):
    """Indicates that there was an error parsing Prometheus status information."""

    def __init__(self, cause):
        super(PrometheusStatusUnparseableError, self).__init__(cause)


class PrometheusSliceInfo(object):
    """Represents the information necessary to query Prometheus by slice.

    Attributes:
        slice_url: URL for querying Prometheus.
        tool_id: Name of a specific tool.
        address_family: Formatted string for specifying ipv4 or ipv6.
    """

    def __init__(self, slice_url, tool_id, address_family):
        self._slice_url = slice_url
        self._tool_id = tool_id
        self._address_family = address_family

    def __eq__(self, other):
        return all([self.slice_url == other.slice_url,
                    self.tool_id == other.tool_id,
                    self.address_family == other.address_family])

    def __ne__(self, other):
        return any([self.slice_url != other.slice_url,
                    self.tool_id != other.tool_id,
                    self.address_family != other.address_family])

    @property
    def slice_url(self):
        return self._slice_url

    @property
    def tool_id(self):
        return self._tool_id

    @property
    def address_family(self):
        return self._address_family


def authenticate_prometheus(prometheus):
    """Configures urllib to do HTTP Password authentication for Prometheus URLs.

    Args:
        prometheus: object containing Prometheus auth information
    """
    password_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, prometheus.url, prometheus.username,
                                  prometheus.password)

    authhandler = urllib2.HTTPDigestAuthHandler(password_manager)
    opener = urllib2.build_opener(authhandler)
    urllib2.install_opener(opener)


def parse_sliver_tool_status(status, slice_id):
    """Parses the status of a single sliver tool.

    Args:
        status: dict, status of a sliver tool as returned by Prometheus.
        slice_id: str, the name of the slice (e.g., iupui_ndt).

    Returns:
        Tuple of the form (sliver fqdn, current state, extra information)
    """
    # Turns 'iupui_ndt' into 'ndt.iupui', for example.
    experiment = '.'.join(slice_id.split('_')[::-1])

    # Joins the experiment name with the machine name to form the FQDN fo the
    # experiment.
    slice_fqdn = '.'.join(experiment, status['metric']['machine'])
    state = status['value'][1]
    # Prometheus doesn't return any sort of "tool_extra" like baseList.pl does
    # for Nagios, so instead we drop in the timestamp returned by Prometheus,
    # just in case it could ever be useful.
    tool_extra = status['value'][0]

    return sliver_fqdn, state, tool_extra


def get_slice_info(prometheus_base_url):
    """Builds a list of PrometheusSliceInfo objects to query Prometheus for all slices.

    Args:
        prometheus_base_url: Base URL to get Prometheus slice information.

    Returns:
         List of PrometheusSliceInfo objects.
    """
    # This dict maps tool_ids to the corresponding Prometheus query that will
    # return the status for the tool.
    queries = {
        'ndt': (
            'min by (machine) ( '
              'probe_success{service="ndt_raw"} OR '
              'script_success{service="ndt_e2e"} OR '
              '(vdlimit_used{experiment="ndt.iupui"} / '
                 'vdlimit_total{experiment="ndt.iupui"}) < bool 0.95 OR '
              'lame_duck_node{} != bool 1)'
        ),
        'ndt_ipv6': '',
        'ndt_ssl': (
            'min by (machine) ( '
              'probe_success{service="ndt_ssl"} OR '
              'script_success{service="ndt_e2e"} OR '
              '(vdlimit_used{experiment="ndt.iupui"} / '
                 'vdlimit_total{experiment="ndt.iupui"}) < bool 0.95 OR '
              'lame_duck_node{} != bool 1)'
        ),
        'ndt_ssl_ipv6': '',
        'neubot': 'probe_success{service="neubot"}',
        'neubot_ipv6': 'probe_success{service="neubot_ipv6"}',
        'mobiperf': (
            'min by (machine) ( '
              'probe_success{service="mobiperf", instance=~".*:6001"} OR '
              'probe_success{service="mobiperf", instance=~".*:6002"} OR '
              'probe_success{service="mobiperf", instance=~".*:6003"})'
        ),
        'mobiperf_ipv6': (
            'min by (machine) ( '
            'probe_success{service="mobiperf_ipv6", instance=~".*:6001"} OR '
            'probe_success{service="mobiperf_ipv6", instance=~".*:6002"} OR '
            'probe_success{service="mobiperf_ipv6", instance=~".*:6003"})'
        )
    }

    slice_objects = []
    for tool_id in model.get_all_tool_ids():
        for address_family in ['', '_ipv6']:
            query = urllib.quote_plus(queries[tool_id + address_family])
            slice_url = prometheus_base_url + query
            slice_objects.append(PrometheusSliceInfo(slice_url, tool_id,
                                                     address_family))

    return slice_objects


def get_slice_status(url, slice_id):
    """Read slice status from Prometheus.

    Args:
        url: str, the API URL for Prometheus for a single tool.
        slice_id: str, the name of the slice (e.g., iupui_ndt).

    Returns:
        A dict mapping sliver fqdn to a dictionary representing the sliver's
        status. For example:

        {
            'foo.mlab1.site1.measurement-lab.org': {
                'status': 'online',
                'tool_extra': 'example tool extra'
            }
        }

        None if Prometheus status is blank or url is inaccessible.
    """
    status = {}
    try:
        raw_data = urllib2.urlopen(url).read()
    except urllib2.HTTPError:
        logging.error('Cannot open %s.', url)
        return None

    statuses = json.loads(raw_data)

    if statuses['status'] == 'error':
        logging.info('Prometheus returned error "%s" for URL %s.'
                     % (statuses['error'], url)
        return None

    for status in statuses['data']['result']:
        try:
            sliver_fqdn, state, tool_extra = parse_sliver_tool_status(status,
                    slice_id)
        except PrometheusStatusUnparseableError as e:
            logging.error('Unable to parse Prometheus sliver status. %s', e)
            continue

        status[sliver_fqdn] = {'tool_extra': tool_extra}
        if state == constants.PROMETHEUS_SERVICE_STATUS_OK:
            status[sliver_fqdn]['status'] = message.STATUS_ONLINE
        else:
            status[sliver_fqdn]['status'] = message.STATUS_OFFLINE

    return status