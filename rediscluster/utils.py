# -*- coding: utf-8 -*-
from socket import gethostbyaddr
from functools import wraps
import threading
from collections import defaultdict

# rediscluster imports
from .exceptions import (
    RedisClusterException, ClusterDownError
)

# 3rd party imports
from redis._compat import basestring, nativestr


def bool_ok(response, *args, **kwargs):
    """
    Borrowed from redis._compat becuase that method to not support extra arguments
    when used in a cluster environment.
    """
    return nativestr(response) == 'OK'


def string_keys_to_dict(key_strings, callback):
    """
    Maps each string in `key_strings` to `callback` function
    and return as a dict.
    """
    return dict.fromkeys(key_strings, callback)


def dict_merge(*dicts):
    """
    Merge all provided dicts into 1 dict.
    """
    merged = {}

    for d in dicts:
        if not isinstance(d, dict):
            raise ValueError('Value should be of dict type')
        else:
            merged.update(d)

    return merged


def blocked_command(self, command):
    """
    Raises a `RedisClusterException` mentioning the command is blocked.
    """
    raise RedisClusterException("Command: {0} is blocked in redis cluster mode".format(command))


def merge_result(command, res):
    """
    Merge all items in `res` into a list.

    This command is used when sending a command to multiple nodes
    and they result from each node should be merged into a single list.
    """
    if not isinstance(res, dict):
        raise ValueError('Value should be of dict type')

    result = set([])

    for _, v in res.items():
        for value in v:
            result.add(value)

    return list(result)


def first_key(command, res):
    """
    Returns the first result for the given command.

    If more then 1 result is returned then a `RedisClusterException` is raised.
    """
    if not isinstance(res, dict):
        raise ValueError('Value should be of dict type')

    if len(res.keys()) != 1:
        raise RedisClusterException("More then 1 result from command: {0}".format(command))

    return list(res.values())[0]


def clusterdown_wrapper(func):
    """
    Wrapper for CLUSTERDOWN error handling.

    If the cluster reports it is down it is assumed that:
     - connection_pool was disconnected
     - connection_pool was reseted
     - refereh_table_asap set to True

    It will try 3 times to rerun the command and raises ClusterDownException if it continues to fail.
    """
    @wraps(func)
    def inner(*args, **kwargs):
        for _ in range(0, 3):
            try:
                return func(*args, **kwargs)
            except ClusterDownError:
                # Try again with the new cluster setup. All other errors
                # should be raised.
                pass

        # If it fails 3 times then raise exception back to caller
        raise ClusterDownError("CLUSTERDOWN error. Unable to rebuild the cluster")
    return inner


def nslookup(node_ip):
    """
    """
    if ':' not in node_ip:
        return gethostbyaddr(node_ip)[0]

    ip, port = node_ip.split(':')

    return '{0}:{1}'.format(gethostbyaddr(ip)[0], port)


def parse_cluster_slots(resp, **options):
    """
    """
    current_host = options.get('current_host', '')

    def fix_server(*args):
        return (nativestr(args[0]) or current_host, args[1])

    slots = {}
    for slot in resp:
        start, end, master = slot[:3]
        slaves = slot[3:]
        slots[start, end] = {
            'master': fix_server(*master),
            'slaves': [fix_server(*slave) for slave in slaves],
        }

    return slots


def parse_cluster_nodes(resp, **options):
    """
    @see: http://redis.io/commands/cluster-nodes  # string
    @see: http://redis.io/commands/cluster-slaves # list of string
    """
    resp = nativestr(resp)
    current_host = options.get('current_host', '')

    def parse_slots(s):
        slots, migrations = [], []
        for r in s.split(' '):
            if '->-' in r:
                slot_id, dst_node_id = r[1:-1].split('->-', 1)
                migrations.append({
                    'slot': int(slot_id),
                    'node_id': dst_node_id,
                    'state': 'migrating'
                })
            elif '-<-' in r:
                slot_id, src_node_id = r[1:-1].split('-<-', 1)
                migrations.append({
                    'slot': int(slot_id),
                    'node_id': src_node_id,
                    'state': 'importing'
                })
            elif '-' in r:
                start, end = r.split('-')
                slots.extend(range(int(start), int(end) + 1))
            else:
                slots.append(int(r))

        return slots, migrations

    if isinstance(resp, basestring):
        resp = resp.splitlines()

    nodes = []
    for line in resp:
        parts = line.split(' ', 8)
        self_id, addr, flags, master_id, ping_sent, \
            pong_recv, config_epoch, link_state = parts[:8]

        host, ports = addr.rsplit(':', 1)
        port, _, cluster_port = ports.partition('@')

        node = {
            'id': self_id,
            'host': host or current_host,
            'port': int(port),
            'cluster-bus-port': int(cluster_port) if cluster_port else 10000 + int(port),
            'flags': tuple(flags.split(',')),
            'master': master_id if master_id != '-' else None,
            'ping-sent': int(ping_sent),
            'pong-recv': int(pong_recv),
            'link-state': link_state,
            'slots': [],
            'migrations': [],
        }

        if len(parts) >= 9:
            slots, migrations = parse_slots(parts[8])
            node['slots'], node['migrations'] = tuple(slots), migrations

        nodes.append(node)

    return nodes


def parse_pubsub_channels(command, res, **options):
    """
    Result callback, handles different return types
    switchable by the `aggregate` flag.
    """
    aggregate = options.get('aggregate', True)
    if not aggregate:
        return res
    return merge_result(command, res)


def parse_pubsub_numpat(command, res, **options):
    """
    Result callback, handles different return types
    switchable by the `aggregate` flag.
    """
    aggregate = options.get('aggregate', True)
    if not aggregate:
        return res

    numpat = 0
    for node, node_numpat in res.items():
        numpat += node_numpat
    return numpat


def parse_pubsub_numsub(command, res, **options):
    """
    Result callback, handles different return types
    switchable by the `aggregate` flag.
    """
    aggregate = options.get('aggregate', True)
    if not aggregate:
        return res

    numsub_d = dict()
    for _, numsub_tups in res.items():
        for channel, numsubbed in numsub_tups:
            try:
                numsub_d[channel] += numsubbed
            except KeyError:
                numsub_d[channel] = numsubbed

    ret_numsub = []
    for channel, numsub in numsub_d.items():
        ret_numsub.append((channel, numsub))
    return ret_numsub


class BlockingDict():
    def __init__(self, max_total_items, max_items_per_key):
        """Performs all configuration."""
        self.max_total_items = max_total_items
        self.max_items_per_key = max_items_per_key
        self.clear()

    def clear(self):
        """Resets state entirely. Be careful if there are still threads waiting."""
        self.num_items = 0
        self._store = defaultdict(list)

        # Note that the three Condition Variables use the same lock,
        # so that we can selectively wake at the key and the total level.
        self._lock = threading.Lock()
        self.total_capacity_cv = threading.Condition(self._lock)

        # Keep track of keys that are full
        self.full_cv_by_key = defaultdict(lambda: threading.Condition(self._lock))

        # Keep track of keys that are empty
        self.empty_cv_by_key = defaultdict(lambda: threading.Condition(self._lock))

    def num_items_for_key(self, key):
        """Returns number of items in key."""
        with self._lock:
            return self._num_items_for_key

    def _num_items_for_key(self, key):
        """Returns number of items in key. Should be used with mutual exclusion."""
        return len(self._store[key])

    def get(self, key, timeout=None):
        with self._lock:
            while self._num_items_for_key(key) == 0:
                wait_complete = self.empty_cv_by_key[key].wait(timeout)
                if not wait_complete:
                    pass  # can raise here for timeout
            try:
                ret_value = self._store[key].pop()
            except IndexError:
                # failed to remove an element with the given key,
                # so no waiting threads are affected.
                ret_value = None

            if ret_value is not None:
                self.num_items -= 1
                self.full_cv_by_key[key].notify()
                self.total_capacity_cv.notify_all()

        return ret_value

    def put(self, key, val, timeout=None):
        with self._lock:
            while self.num_items >= self.max_total_items or self._num_items_for_key(key) >= self.max_items_per_key:
                per_item_wait_complete = True
                total_wait_complete = True
                if self._num_items_for_key(key) >= self.max_items_per_key:
                    # if the key is full, we wait for a ``get`` to occur on that key.
                    per_item_wait_complete = self.full_cv_by_key[key].wait(timeout)
                if self.num_items >= self.max_total_items:
                    # if the total num items has reach capacity we wake on any ``get``.
                    total_wait_complete = self.total_capacity_cv.wait(timeout)

                if not per_item_wait_complete or not total_wait_complete:
                    pass  # can raise here for timeout

            self._store[key].append(val)
            self.num_items += 1
            self.empty_cv_by_key[key].notify()
