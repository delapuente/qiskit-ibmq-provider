# -*- coding: utf-8 -*-

# Copyright 2017, IBM.
#
# This source code is licensed under the Apache License, Version 2.0 found in
# the LICENSE.txt file in the root directory of this source tree.

"""Module for interfacing with an IBMQ Backend."""

import logging
import warnings

from marshmallow import ValidationError

from qiskit.providers import BaseBackend, JobStatus
from qiskit.providers.models import (BackendStatus, BackendProperties,
                                     PulseDefaults)

from .api import ApiError
from .exceptions import IBMQBackendError, IBMQBackendValueError
from .ibmqjob import IBMQJob

logger = logging.getLogger(__name__)


class IBMQBackend(BaseBackend):
    """Backend class interfacing with an IBMQ backend."""

    def __init__(self, configuration, provider, credentials, api):
        """Initialize remote backend for IBM Quantum Experience.

        Args:
            configuration (BackendConfiguration): configuration of backend.
            provider (IBMQProvider): provider.
            credentials (Credentials): credentials.
            api (IBMQConnector):
                api for communicating with the Quantum Experience.
        """
        super().__init__(provider=provider, configuration=configuration)

        self._api = api
        self._credentials = credentials
        self.hub = credentials.hub
        self.group = credentials.group
        self.project = credentials.project

    def run(self, qobj):
        """Run qobj asynchronously.

        Args:
            qobj (Qobj): description of job

        Returns:
            IBMQJob: an instance derived from BaseJob
        """
        job = IBMQJob(self, None, self._api, qobj=qobj)
        job.submit()
        return job

    def properties(self):
        """Return the online backend properties.

        The return is via QX API call.

        Returns:
            BackendProperties: The properties of the backend. If the backend
            is a simulator, it returns ``None``.
        """
        if self.configuration().simulator:
            return None

        api_properties = self._api.backend_properties(self.name())

        return BackendProperties.from_dict(api_properties)

    def status(self):
        """Return the online backend status.

        Returns:
            BackendStatus: The status of the backend.

        Raises:
            LookupError: If status for the backend can't be found.
            IBMQBackendError: If the status can't be formatted properly.
        """
        api_status = self._api.backend_status(self.name())

        try:
            return BackendStatus.from_dict(api_status)
        except ValidationError as ex:
            raise LookupError(
                "Couldn't get backend status: {0}".format(ex))

    def defaults(self):
        """Return the pulse defaults for the backend.

        Returns:
            PulseDefaults: the pulse defaults for the backend. IF the backend
            does not support defaults, it returns ``None``.
        """
        backend_defaults = self._api.backend_defaults(self.name())

        if backend_defaults:
            return PulseDefaults.from_dict(backend_defaults)

        return None

    def jobs(self, limit=50, skip=0, status=None, db_filter=None):
        """Attempt to get the jobs submitted to the backend.

        Args:
            limit (int): number of jobs to retrieve
            skip (int): starting index of retrieval
            status (None or qiskit.providers.JobStatus or str): only get jobs
                with this status, where status is e.g. `JobStatus.RUNNING` or
                `'RUNNING'`
            db_filter (dict): `loopback-based filter
                <https://loopback.io/doc/en/lb2/Querying-data.html>`_.
                This is an interface to a database ``where`` filter. Some
                examples of its usage are:

                Filter last five jobs with errors::

                   job_list = backend.jobs(limit=5, status=JobStatus.ERROR)

                Filter last five jobs with counts=1024, and counts for
                states ``00`` and ``11`` each exceeding 400::

                  cnts_filter = {'shots': 1024,
                                 'qasms.result.data.counts.00': {'gt': 400},
                                 'qasms.result.data.counts.11': {'gt': 400}}
                  job_list = backend.jobs(limit=5, db_filter=cnts_filter)

                Filter last five jobs from 30 days ago::

                   past_date = datetime.datetime.now() - datetime.timedelta(days=30)
                   date_filter = {'creationDate': {'lt': past_date.isoformat()}}
                   job_list = backend.jobs(limit=5, db_filter=date_filter)

        Returns:
            list(IBMQJob): list of IBMQJob instances

        Raises:
            IBMQBackendValueError: status keyword value unrecognized
        """
        backend_name = self.name()
        api_filter = {'backend.name': backend_name}
        if status:
            if isinstance(status, str):
                status = JobStatus[status]
            if status == JobStatus.RUNNING:
                this_filter = {'status': 'RUNNING',
                               'infoQueue': {'exists': False}}
            elif status == JobStatus.QUEUED:
                this_filter = {'status': 'RUNNING',
                               'infoQueue.status': 'PENDING_IN_QUEUE'}
            elif status == JobStatus.CANCELLED:
                this_filter = {'status': 'CANCELLED'}
            elif status == JobStatus.DONE:
                this_filter = {'status': 'COMPLETED'}
            elif status == JobStatus.ERROR:
                this_filter = {'status': {'regexp': '^ERROR'}}
            else:
                raise IBMQBackendValueError('unrecognized value for "status" keyword '
                                            'in job filter')
            api_filter.update(this_filter)
        if db_filter:
            # status takes precedence over db_filter for same keys
            api_filter = {**db_filter, **api_filter}
        job_info_list = self._api.get_status_jobs(limit=limit, skip=skip,
                                                  filter=api_filter)
        job_list = []
        old_format_jobs = []
        for job_info in job_info_list:
            if job_info.get('kind', None) != 'q-object':
                old_format_jobs.append(job_info.get('id'))
                break

            job = IBMQJob(self, job_info.get('id'), self._api,
                          creation_date=job_info.get('creationDate'),
                          api_status=job_info.get('status'))
            job_list.append(job)

        if old_format_jobs:
            job_ids = '\n - '.join(old_format_jobs)
            warnings.warn('Some jobs are in a no-longer supported format. '
                          'Please send the job using Qiskit 0.8+. Old jobs:'
                          '\n - {}'.format(job_ids), DeprecationWarning)
        return job_list

    def retrieve_job(self, job_id):
        """Attempt to get the specified job by job_id

        Args:
            job_id (str): the job id of the job to retrieve

        Returns:
            IBMQJob: class instance

        Raises:
            IBMQBackendError: if retrieval failed
        """
        try:
            job_info = self._api.get_job(job_id)

            # Check for generic errors.
            if 'error' in job_info:
                raise IBMQBackendError('Failed to get job "{}": {}'
                                       .format(job_id, job_info['error']))

            # Check for jobs from a different backend.
            if job_info['backend']['name'] != self.name():
                warnings.warn('Job "{}" belongs to another backend than the one queried. '
                              'The query was made on backend "{}", '
                              'but the job actually belongs to backend "{}".'
                              .format(job_id, job_info['backend']['name'], self.name()))
                raise IBMQBackendError('Failed to get job "{}": '
                                       'job does not belong to backend "{}".'
                                       .format(job_id, job_info['backend']['name']))

            # Check for pre-qobj jobs.
            if job_info.get('kind', None) != 'q-object':
                warnings.warn('The result of job {} is in a no longer supported format. '
                              'Please send the job using Qiskit 0.8+.'.format(job_id),
                              DeprecationWarning)
                raise IBMQBackendError('Failed to get job "{}": {}'
                                       .format(job_id, 'job in pre-qobj format'))
        except ApiError as ex:
            raise IBMQBackendError('Failed to get job "{}": {}'
                                   .format(job_id, str(ex)))

        job = IBMQJob(self, job_info.get('id'), self._api,
                      creation_date=job_info.get('creationDate'),
                      api_status=job_info.get('status'))
        return job

    def __repr__(self):
        credentials_info = ''
        if self.hub:
            credentials_info = '{}, {}, {}'.format(self.hub, self.group,
                                                   self.project)
        return "<{}('{}') from IBMQ({})>".format(
            self.__class__.__name__, self.name(), credentials_info)
