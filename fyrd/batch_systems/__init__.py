# -*- coding: utf-8 -*-
"""
Modular batch system handling.

All batch system specific functions are contained within files in the
batch_systems folder.  The files must have the same name as the batch system,
and possible batch systems are set in the DEFINED_SYSTEMS set. Most batch
system functions are set in the modules in this package, but system detection
methods are hardcoded into get_cluster_environment() also.

To add new systems, create a new batch system with identical function and
classes names and return/yield values to those in an existing definition. You
will also need to update the options.py script to include keywords for your
system and the get_cluster_environment() function to include autodetection.
"""
from importlib import import_module as _import

from . import slurm as _slurm
from . import torque as _torque
from . import lsf as _lsf
from .base import BatchSystemError, BatchSystemClient

from .. import run as _run
from .. import logme as _logme
from .. import ClusterError as _ClusterError

DEFINED_SYSTEMS = {'torque', 'slurm', 'lsf', 'local', 'auto'}

MODE = None

# Define job states all batch systems must return one of these states
GOOD_STATES      = ['complete', 'completed', 'special_exit']
ACTIVE_STATES    = ['configuring', 'completing', 'pending',
                    'held', 'running', 'submitted']
BAD_STATES       = ['boot_fail', 'cancelled', 'failed', 'killed',
                    'node_fail', 'timeout', 'disappeared']
UNCERTAIN_STATES = ['preempted', 'stopped',
                    'suspended']
ALL_STATES = GOOD_STATES + ACTIVE_STATES + BAD_STATES + UNCERTAIN_STATES
DONE_STATES = GOOD_STATES + BAD_STATES

_default_batches = {'slurm': [_slurm.SlurmClient, _slurm.SlurmServer],
                    'torque': [_torque.TorqueClient, _torque.TorqueServer],
                    'lsf': [_lsf.LSFClient, _lsf.LSFServer],
                    'local': [None, None]}

# Save the client instances
_client_batches = {'local': {}, 'remote': {}}


def get_batch_system(qtype=None, remote=True, uri=None):
    """Return a batch_system module."""
    qtype = qtype if qtype else get_cluster_environment()
    if qtype not in DEFINED_SYSTEMS:
        raise _ClusterError(
            'qtype value {0} is not recognized, '.format(qtype) +
            'should be one of {0}'.format(DEFINED_SYSTEMS)
        )
    global _default_batches
    global _client_batches
    global MODE

    # First time connect with a generic client to figure out queue type
    if qtype == 'auto':
        if remote and uri:
            tmp_cli = BatchSystemClient(remote=remote, uri=uri)
            if not tmp_cli.connected:
                raise BatchSystemError(
                    'BatchSystemClient not connected. Can not get batch type'
                    )
                return None
            qtype = tmp_cli.qtype
            MODE = qtype
            tmp_cli.release()
        else:
            raise ValueError(
                'To automatically get queue type, there must be a remote '
                'server and the URI must be specified.'
                )

    # If a client and server already exist for the qtype return these if:
    # - URI passed matches with the one configured for them, or
    # - No URI has been specified and server connection does need to change.
    remote_str = 'remote' if remote else 'local'
    if qtype in _client_batches[remote_str].keys():
        if not uri or uri == _client_batches[remote_str][qtype].uri:
            return _client_batches[remote_str][qtype]
        _client_batches[remote_str][qtype].release()

    # Otherwise create a new batch client and server instance to that URI.
    client, server = _default_batches[qtype]
    if remote:
        _client_batches['remote'][qtype] = client(remote=True, uri=uri)
    else:
        _client_batches['local'][qtype] = client(remote=False,
                                                 server_class=server)

    return _client_batches[remote_str][qtype]

def get_batch_classes(qtype=None):
    """Return a batch_system client and server classes."""
    qtype = qtype if qtype else get_cluster_environment()
    if qtype not in DEFINED_SYSTEMS:
        raise _ClusterError(
            'qtype value {0} is not recognized, '.format(qtype) +
            'should be one of {0}'.format(DEFINED_SYSTEMS)
        )
    global _default_batches

    client, server = _default_batches[qtype]
    return client, server


#################################
#  Set the global cluster type  #
#################################


def get_cluster_environment(overwrite=False):
    """Detect the local cluster environment and set MODE globally.

    Detect the current batch system by looking for command line utilities.
    Order is important here, so we hard code the batch system lookups.

    Paths to files can also be set in the config file.

    Parameters
    ----------
    overwrite : bool, optional
        If True, run checks anyway, otherwise just accept MODE if it is
        already set.

    Returns
    -------
    MODE : str
    """
    global MODE
    if not overwrite and MODE and MODE in list(DEFINED_SYSTEMS):
        return MODE
    from .. import conf as _conf
    conf_queue = _conf.get_option('queue', 'queue_type', 'auto')
    if conf_queue not in list(DEFINED_SYSTEMS) + ['auto']:
        _logme.log('queue_type in the config file is {}, '.format(conf_queue) +
                   'but it should be one of {}'.format(DEFINED_SYSTEMS) +
                   ' or auto. Resetting it to auto', 'warn')
        _conf.set_option('queue', 'queue_type', 'auto')
        conf_queue = 'auto'
    if conf_queue == 'auto':
        # Hardcode queue lookups here
        sbatch_cmnd = _conf.get_option('queue', 'sbatch')
        qsub_cmnd   = _conf.get_option('queue', 'qsub')
        bsub_cmnd   = _conf.get_option('queue', 'bsub')
        sbatch_cmnd = sbatch_cmnd if sbatch_cmnd else 'sbatch'
        qsub_cmnd   = qsub_cmnd if qsub_cmnd else 'qsub'
        bsub_cmnd   = bsub_cmnd if bsub_cmnd else 'bsub'
        if _run.which(sbatch_cmnd):
            MODE = 'slurm'
        elif _run.which(qsub_cmnd):
            MODE = 'torque'
        elif _run.which(bsub_cmnd):
            MODE = 'lsf'
        else:
            MODE = 'local'
    else:
        MODE = conf_queue
    if MODE is None:
        _logme.log('No functional batch system detected, will not be able to'
                   'run', 'error')
    elif MODE == 'local':
        _logme.log('No cluster environment detected, using multiprocessing',
                   'debug')
    else:
        _logme.log('{0} detected, using for cluster submissions'.format(MODE),
                   'debug')
    return MODE


##############################
#  Check if queue is usable  #
##############################


def check_queue(qtype=None, remote=True, uri=None):
    """Check if *both* MODE and qtype are valid.

    First checks the MODE global and autodetects its value, if that fails, no
    other tests are done, the qtype argument is ignored.

    After MODE is found to be a reasonable value, the queried queue is tested
    for functionality. If qtype is defined, this queue is tested, else the
    queue in MODE is tested.

    Tests are defined per batch system.

    Parameters
    ----------
    qtype : str

    Returns
    ------
    batch_system_functional : bool

    Raises
    ------
    ClusterError
        If MODE or qtype is not in DEFINED_SYSTEMS

    See Also
    --------
    get_cluster_environment : Auto detect the batch environment
    get_batch_system : Return the batch system module
    """
    if 'MODE' not in globals():
        global MODE
        MODE = get_cluster_environment()
    if not MODE:
        MODE = get_cluster_environment()
    if not MODE:
        _logme.log('Queue system not detected', 'error')
        return False
    if MODE not in DEFINED_SYSTEMS:
        raise _ClusterError(
            'MODE value {0} is not recognized, '.format(MODE) +
            'should be one of {0}'.format(DEFINED_SYSTEMS)
        )
    if qtype and qtype not in DEFINED_SYSTEMS:
        raise _ClusterError(
            'qtype value {0} is not recognized, '.format(qtype) +
            'should be one of {0}'.format(DEFINED_SYSTEMS)
        )
    qtype = qtype if qtype else MODE
    batch_system = get_batch_system(qtype, remote=remote, uri=uri)
    return batch_system.queue_test(warn=True)


# Make options easily available everywhere
from . import options
