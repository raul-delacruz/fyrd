# -*- coding: utf-8 -*-
"""
SLURM parsing functions.
"""
import os as _os
import re as _re
import sys as _sys
import pwd as _pwd     # Used to get usernames for queue

from six import text_type as _txt
from six import string_types as _str
from six import integer_types as _int

import Pyro4

from .. import run as _run
from .. import conf as _conf
from .. import logme as _logme
from .. import ClusterError as _ClusterError
from .. import script_runners as _scrpts
from .. import submission_scripts as _sscrpt

from .base import BatchSystemClient, BatchSystemServer

_Script = _sscrpt.Script

SUFFIX = 'sbatch'


@Pyro4.expose
class SlurmServer(BatchSystemServer):
    NAME = 'slurm'

    def metrics(self, job_id=None):
        _logme.log('Getting job metrics', 'debug')

        fields = (
            'JobID', 'Partition', 'AllocCPUs', 'AllocNodes', 'AllocTres',
            'AveCPUFreq', 'AveDiskRead', 'AveDiskWrite', 'AveRSS',
            'ConsumedEnergy', 'Submit', 'Start', 'End', 'Elapsed'
            )
        qargs = [
            'sacct', '-p', '--noheader', '--noconvert',
            '--format={}'.format(','.join(fields))
            ]
        if job_id:
            qargs.append('-j {}'.format(job_id))
        try:
            sacct = [tuple(i.strip(' |').split('|')) for i in
                     _run.cmd(qargs)[1].split('\n')]
        except Exception as e:
            _logme.log('Error running sacct to get the metrics', 'error')
            sacct = []

        for line in sacct:
            yield line

    ###########################################################################
    #                           Functionality Test                            #
    ###########################################################################
    def queue_test(self, warn=True):
        """Check that slurm can be used.

        Just looks for sbatch and squeue.

        Parameters
        ----------
        warn : bool
            log a warning on fail

        Returns
        -------
        batch_system_functional : bool
        """
        log_level = 'error' if warn else 'debug'
        sbatch = _conf.get_option('queue', 'sbatch')
        if (sbatch is not None and _os.path.dirname(sbatch)
                and not _run.is_exe(sbatch)):
            _logme.log(
                'Cannot use slurm as sbatch path set in conf to {0}'
                .format(sbatch) + ' but that path is not an executable',
                log_level
            )
            return False
        sbatch = sbatch if sbatch else 'sbatch'
        sbatch = _run.which(sbatch) if not _os.path.dirname(sbatch) else sbatch
        if not sbatch:
            _logme.log(
                'Cannot use slurm as cannot find sbatch', log_level
            )
            return False
        qpath = _os.path.dirname(sbatch)
        squeue = _os.path.join(qpath, 'squeue')
        return _run.is_exe(squeue)

    ###########################################################################
    #                         Normalization Functions                         #
    ###########################################################################
    def normalize_job_id(self, job_id):
        """Convert the job id into job_id, array_id."""
        if '_' in job_id:
            job_id, array_id = job_id.split('_')
            job_id = job_id.strip()
            array_id = array_id.strip()
        else:
            array_id = None
        return job_id, array_id

    def normalize_state(self, state):
        """Convert state into standadized (slurm style) state."""
        return state

    ###########################################################################
    #                             Job Submission                              #
    ###########################################################################
    def gen_scripts(self, job_object, command, args, precmd, modstr):
        """Can't create the scripts in server side since Job and Script object
        won't be serialized correctly by Pyro4.

        Parameters
        ---------
        job_object : fyrd.job.Job
        command : str
            Command to execute
        args : list
            List of additional arguments, not used in this script.
        precmd : str
            String from options_to_string() to add at the top of the file,
            should contain batch system directives
        modstr : str
            String to add after precmd, should contain module directives.

        Returns
        -------
        fyrd.script_runners.Script
            The submission script
        fyrd.script_runners.Script
            The execution script
        """
        raise NotImplementedError()

    def submit(self, script_file_name, dependencies=None):
        """Submit any file with dependencies to Slurm.

        Parameters
        ----------
        script_file_name : str
            Path of the script to be submitted
        dependencies : list
            List of dependencies

        Returns
        -------
        results: dict
            Dictionary containing the results and/or errors.
            If the execution have no errors, it returns the job_id as the
            result.
        """
        _logme.log('Submitting to slurm', 'debug')
        if dependencies:
            deps = '--dependency=afterok:{}'.format(
                ':'.join([str(d) for d in dependencies]))
            args = ['sbatch', deps, script_file_name]
        else:
            args = ['sbatch', script_file_name]
        # Try to submit job 5 times
        code, stdout, stderr = _run.cmd(args, tries=5)
        if code == 0:
            job_id, _ = self.normalize_job_id(stdout.split(' ')[-1])
        else:
            _logme.log('sbatch failed with code {}\n'.format(code) +
                       'stdout: {}\nstderr: {}'.format(stdout, stderr),
                       'critical')
            # raise _CalledProcessError(code, args, stdout, stderr)
            # XXX: ?????
            # Pyro4 can't serialize CalledProcessError
            return {'error': True, 'stdout': stdout, 'stderr': stderr}

        return {'error': False, 'result': job_id}

    ###########################################################################
    #                            Job Management                               #
    ###########################################################################
    def kill(self, job_ids):
        """Terminate all jobs in job_ids.

        Parameters
        ----------
        job_ids : list or str
            A list of valid job ids or a single valid job id

        Returns
        -------
        success : bool
        """
        o = _run.cmd('scancel {0}'.format(' '.join(_run.listify(job_ids))),
                     tries=5)
        return o[0] == 0

    ###########################################################################
    #                              Queue Parsing                              #
    ###########################################################################
    def queue_parser(self, user=None, partition=None, job_id=None):
        """Iterator for slurm queues.

        Use the `squeue -O` command to get standard data across implementation,
        supplement this data with the results of `sacct`. sacct returns data
        only for the current user but retains a much longer job history. Only
        jobs not returned by squeue are added with sacct, and they are added
        to *the end* of the returned queue, i.e. *out of order with respect to
        the actual queue*.

        Parameters
        ----------
        user : str, optional
            User name to pass to qstat to filter queue with
        partiton : str, optional
            Partition to filter the queue with
        job_id: str, optional
            Job ID to filter the queue with

        Yields
        ------
        job_id : str
        array_id : str or None
        name : str
        userid : str
        partition : str
        state :str
        nodelist : list
        numnodes : int
        cntpernode : int or None
        exit_code : int or Nonw
        """
        try:
            if job_id:
                int(job_id)
        except ValueError:
            job_id = None
        nodequery = _re.compile(r'([^\[,]+)(\[[^\[]+\])?')
        fwdth = 400  # Used for fixed-width parsing of squeue
        fields = [
            'jobid', 'arraytaskid', 'name', 'userid', 'partition',
            'state', 'nodelist', 'numnodes', 'numcpus', 'exit_code'
        ]
        flen = len(fields)
        qargs = [
            'squeue', '-h', '-O',
            ','.join(['{0}:{1}'.format(field, fwdth) for field in fields])
        ]
        # Parse queue info by length
        squeue = [
            tuple(
                [k[i:i+fwdth].rstrip() for i in range(0, fwdth*flen, fwdth)]
            ) for k in _run.cmd(qargs)[1].split('\n')
        ]
        # SLURM sometimes clears the queue extremely fast, so we use sacct
        # to get old jobs by the current user
        qargs = ['sacct', '-p',
                 '--format=jobid,jobname,user,partition,state,' +
                 'nodelist,reqnodes,ncpus,exitcode']
        try:
            sacct = [tuple(i.strip(' |').split('|')) for i in
                     _run.cmd(qargs)[1].split('\n')]
            sacct = sacct[1:]
        # This command isn't super stable and we don't care that much, so I
        # will just let it die no matter what
        except Exception as e:
            if _logme.MIN_LEVEL == 'debug':
                raise e
            else:
                sacct = []

        if sacct:
            if len(sacct[0]) != 9:
                _logme.log('sacct parsing failed unexpectedly as there  ' +
                           'are not 9 columns, aborting.', 'critical')
                raise ValueError('sacct output does not have 9 columns. Has:' +
                                 '{}: {}'.format(len(sacct[0]), sacct[0]))
            jobids = [i[0] for i in squeue]
            for sinfo in sacct:
                # Skip job steps, only index whole jobs
                if '.' in sinfo[0]:
                    _logme.log('Skipping {} '.format(sinfo[0]) +
                               "in sacct processing as it is a job part.",
                               'verbose')
                    continue
                # These are the values I expect
                try:
                    [sid, sname, suser, spartition, sstate,
                     snodelist, snodes, scpus, scode] = sinfo
                    sid, sarr = self.normalize_job_id(sid)
                except ValueError as err:
                    _logme.log(
                            'sacct parsing failed with error {} '.format(err) +
                            'due to an incorrect number of entries.\n' +
                            'Contents of sinfo:\n{}\n'.format(sinfo) +
                            'Expected 10 values\n:' +
                            '[sid, sarr, sname, suser, spartition, sstate, ' +
                            'snodelist, snodes, scpus, scode]',
                            'critical')
                    raise
                # Skip jobs that were already in squeue
                if sid in jobids:
                    _logme.log('{} still in squeue output'.format(sid),
                               'verbose')
                    continue
                scode = int(scode.split(':')[-1])
                squeue.append((sid, sarr, sname, suser, spartition, sstate,
                               snodelist, snodes, scpus, scode))
        else:
            _logme.log('No job info in sacct', 'debug')

        # Sanitize data
        for sinfo in squeue:
            if len(sinfo) == 10:
                [sid, sarr, sname, suser, spartition, sstate, sndlst,
                 snodes, scpus, scode] = sinfo
            else:
                _sys.stderr.write('{}'.format(repr(sinfo)))
                raise _ClusterError('Queue parsing error, expected 10 items '
                                    'in output of squeue and sacct, got {}\n'
                                    .format(len(sinfo)))
            if partition and spartition != partition:
                continue
            if not isinstance(sid, (_str, _txt)):
                sid = str(sid) if sid else None
            else:
                sarr = None
            if not isinstance(snodes, _int):
                snodes = int(snodes) if snodes else None
            if not isinstance(scpus, _int):
                scpus = int(scpus) if scpus else None
            if not isinstance(scode, _int):
                scode = int(scode) if scode else None
            sstate = sstate.lower()
            # Convert user from ID to name
            if suser.isdigit():
                suser = _pwd.getpwuid(int(suser)).pw_name
            if user and suser != user:
                continue
            if job_id and (job_id != sid):
                continue
            # Attempt to parse nodelist
            snodelist = []
            if sndlst:
                if nodequery.search(sndlst):
                    nsplit = nodequery.findall(sndlst)
                    for nrg in nsplit:
                        node, rge = nrg
                        if not rge:
                            snodelist.append(node)
                        else:
                            for reg in rge.strip('[]').split(','):
                                # Node range
                                if '-' in reg:
                                    start, end = [
                                            int(i) for i in reg.split('-')
                                            ]
                                    for i in range(start, end):
                                        snodelist.append(
                                                '{}{}'.format(node, i)
                                                )
                                else:
                                    snodelist.append('{}{}'.format(node, reg))
                else:
                    snodelist = sndlst.split(',')

            yield (sid, sarr, sname, suser, spartition, sstate, snodelist,
                   snodes, scpus, scode)

    def parse_strange_options(self, option_dict):
        """Parse all options that cannot be handled by the regular function.
        Handled on client side.

        Parameters
        ----------
        option_dict : dict
            All keyword arguments passed by the user that are not already
            defined in the Job object

        Returns
        -------
        list
            A list of strings to be added at the top of the script file
        dict
            Altered version of option_dict with all options that can't be
            handled by `fyrd.batch_systems.options.option_to_string()` removed.
        None
            Would contain additional arguments to pass to sbatch, but these
            are not needed so we just return None
        """
        raise NotImplementedError


class SlurmClient(BatchSystemClient):
    """Overwrite simple methods that can be executed in localhost, to avoid
    some network overhead.
    """

    NAME = 'slurm'
    PREFIX = '#SBATCH'
    PARALLEL = 'srun'

    def metrics(self, job_id=None):
        server = self.get_server()
        return server.metrics(job_id=job_id)

    def normalize_job_id(self, job_id):
        """Convert the job id into job_id, array_id."""
        if '_' in job_id:
            job_id, array_id = job_id.split('_')
            job_id = job_id.strip()
            array_id = array_id.strip()
        else:
            array_id = None
        return job_id, array_id

    def normalize_state(self, state):
        """Convert state into standadized (slurm style) state."""
        return state

    def gen_scripts(self, job_object, command, args, precmd, modstr):
        """Build the submission script objects.

        Creates an exec script as well as a submission script.

        Parameters
        ---------
        job_object : fyrd.job.Job
        command : str
            Command to execute
        args : list
            List of additional arguments, not used in this script.
        precmd : str
            String from options_to_string() to add at the top of the file,
            should contain batch system directives
        modstr : str
            String to add after precmd, should contain module directives.

        Returns
        -------
        fyrd.script_runners.Script
            The submission script
        fyrd.script_runners.Script
            The execution script
        """
        scrpt = '{}.{}.{}'.format(job_object.name, job_object.suffix, SUFFIX)

        # Use a single script to run the job and avoid using srun in order to
        # allow sequential and parallel executions to live together in job.
        # NOTE: the job is initially executed in sequential mode, and the
        # programmer is responsible of calling their parallel codes by means
        # of self.PARALLEL preffix.
        job_object._mode = 'remote'
        sub_script = _scrpts.CMND_RUNNER_TRACK.format(
            precmd=precmd, usedir=job_object.runpath, name=job_object.name,
            command=command
        )
        job_object._mode = 'local'

        # Create the sub_script Script object
        sub_script_obj = _Script(
            script=sub_script, file_name=scrpt, job=job_object
        )

        return sub_script_obj, None

    def submit(self, script, dependencies=None,
               job=None, args=None, kwds=None):
        """Submit any file with dependencies to Slurm.

        Parameters
        ----------
        script : fyrd.Script
            Script to be submitted
        dependencies : list
            List of dependencies
        job : fyrd.job.Job, not implemented
            A job object for the calling job, not used by this functions
        args : list, not implemented
            A list of additional command line arguments to pass when
            submitting, not used by this function
        kwds : dict or str, not implemented
            A dictionary of keyword arguments to parse with options_to_string,
            or a string of option:value,option,option:value,.... Not used by
            this function.

        Returns
        -------
        job_id : str
        """
        script.job_object._mode = 'remote'
        result = self.get_server().submit(
                script.file_name, dependencies=dependencies
                )
        script.job_object._mode = 'local'
        return result

    def parse_strange_options(self, option_dict):
        """Parse all options that cannot be handled by the regular function.

        Parameters
        ----------
        option_dict : dict
            All keyword arguments passed by the user that are not already
            defined in the Job object

        Returns
        -------
        list
            A list of strings to be added at the top of the script file
        dict
            Altered version of option_dict with all options that can't be
            handled by `fyrd.batch_systems.options.option_to_string()` removed.
        None
            Would contain additional arguments to pass to sbatch, but these
            are not needed so we just return None
        """
        outlist = []

        nodes = None
        if 'nodes' in option_dict:
            n = option_dict.pop('nodes')
            if isinstance(n, str) and n.isdigit():
                nodes = int(n)
                outlist.append('#SBATCH --nodes {}'.format(nodes))

        tasks = None
        if 'tasks' in option_dict:
            tasks = int(option_dict.pop('tasks'))
            outlist.append('#SBATCH --ntasks {}'.format(tasks))

        if 'cpus_per_task' in option_dict:
            cores = int(option_dict.pop('cpus_per_task'))
            outlist.append('#SBATCH --cpus-per-task {}'.format(cores))

        # First look for tasks_per_node, if it's not there change to cores
        # Cores refers to the max number of processors to use per node (ppn)
        if 'tasks_per_node' in option_dict:
            tpn = int(option_dict.pop('tasks_per_node'))
            outlist.append('#SBATCH --tasks-per-node {}'.format(tpn))
            if 'cores' in option_dict:
                # Avoid option parser to raise errors
                option_dict.pop('cores')
        elif 'cores' in option_dict:
            cores = int(option_dict.pop('cores'))
            if not nodes:
                outlist.append('#SBATCH --nodes 1')
            outlist.append('#SBATCH --tasks-per-node {}'.format(cores))

        if 'exclusive' in option_dict:
            option_dict.pop('exclusive')
            outlist.append('#SBATCH --exclusive')

        return outlist, option_dict, None
